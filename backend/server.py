#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Штурвал — бэкенд-«посредник» между amoCRM и сайтом.
Токен хранится здесь, на сервере, и НЕ попадает в код сайта.
Запуск: python3 server.py   (или двойной клик по «Запустить-сервер.command»)
"""
import os, json, time, threading, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# порт: локально 8787, на хостинге (Render и т.п.) берётся из переменной PORT
PORT = int(os.environ.get("PORT", "8787"))
HERE = os.path.dirname(os.path.abspath(__file__))

# --- читаем секрет (токен + поддомен) ---
def load_secret():
    cfg = {}
    path = os.path.join(HERE, "secret.env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    # переменные окружения имеют приоритет (понадобится для облака)
    # переменные окружения имеют приоритет (для облака/хостинга); .strip() убирает случайные пробелы/переносы
    def env(name):
        return os.environ.get(name, cfg.get(name, "")).strip()
    cfg["AMO_TOKEN"] = env("AMO_TOKEN")
    cfg["AMO_SUBDOMAIN"] = env("AMO_SUBDOMAIN")
    # 1С (Yaros DataGate) — товары/остатки/цены
    cfg["YAROS_URL"] = env("YAROS_URL")
    cfg["YAROS_LOGIN"] = env("YAROS_LOGIN")
    cfg["YAROS_PASS"] = env("YAROS_PASS")
    # ChatPlace — чаты (раньше читался только из файла, теперь и из окружения)
    cfg["CHATPLACE_KEY"] = env("CHATPLACE_KEY")
    # адрес метода создания товара в 1С (появится, когда программист сделает запись)
    cfg["YAROS_CREATE_URL"] = env("YAROS_CREATE_URL")
    # пароль владельца (видит всё). Если пусто — защита выключена.
    cfg["SITE_PASSWORD"] = env("SITE_PASSWORD")
    # доп. пользователи с ролями (JSON-список): [{"pw":"...","name":"...","sections":["chats"]}]
    cfg["USERS_JSON"] = env("USERS_JSON")
    return cfg

CFG = load_secret()
BASE = "https://{}.amocrm.ru/api/v4".format(CFG.get("AMO_SUBDOMAIN", ""))

# --- пользователи и роли ---
# каждый пользователь: пароль -> {name, sections}. sections=["all"] = видит всё.
# разделы: dash, crm, chats, prod, sales, clients, market, analytics, fin, ai, set
def load_users():
    users = {}
    owner = CFG.get("SITE_PASSWORD", "").strip()
    if owner:
        users[owner] = {"name": "Владелец", "sections": ["all"]}
    raw = CFG.get("USERS_JSON", "").strip()
    if raw:
        try:
            for u in json.loads(raw):
                pw = (u.get("pw") or "").strip()
                if pw:
                    users[pw] = {"name": u.get("name", "Сотрудник"),
                                 "sections": u.get("sections", []),
                                 "role": u.get("role", ""),
                                 "department": u.get("department", ""),
                                 "plan_day": u.get("plan_day", 0)}
        except Exception:
            pass
    return users

USERS = load_users()

# какой раздел нужен для каждого data-эндпоинта (для проверки прав)
SECTION_OF = [
    ("/api/overview", "crm"),
    ("/api/inventory", "prod"), ("/api/products", "prod"),
    ("/api/categories", "prod"), ("/api/add-product", "prod"),
    ("/api/chats", "chats"), ("/api/chat", "chats"), ("/api/send", "chats"),
    ("/api/bot-feedback", "chats"),
]

# Исправления для бота (обучение). Хранится в памяти процесса + дозапись в файл.
BOT_FEEDBACK = []

def _section_for(path):
    for pref, sec in SECTION_OF:
        if path.startswith(pref):
            return sec
    return None

def _can(user, section):
    if not user:
        return False
    s = user.get("sections", [])
    return "all" in s or section in s

def _allowed(user, path):
    # дашборд показывает сводку amoCRM → доступ к /api/overview даём и роли "dash", и "crm"
    if path.startswith("/api/overview"):
        return _can(user, "crm") or _can(user, "dash")
    sec = _section_for(path)
    return (sec is None) or _can(user, sec)

# --- простой кэш на 60 секунд, чтобы не дёргать amoCRM лишний раз ---
_cache = {}
def cached(key, ttl, producer):
    now = time.time()
    if key in _cache and now - _cache[key][0] < ttl:
        return _cache[key][1]
    val = producer()
    _cache[key] = (now, val)
    return val

def amo_get(path):
    """GET-запрос к amoCRM с токеном. Возвращает dict (или {} если пусто)."""
    url = BASE + path
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + CFG.get("AMO_TOKEN", ""),
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8").strip()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"_error": "HTTP {}".format(e.code)}
    except Exception as e:
        return {"_error": str(e)}

def build_overview():
    # 1) воронки и этапы: status_id -> название
    pipes = amo_get("/leads/pipelines")
    status_name = {}
    pipelines = []
    for p in pipes.get("_embedded", {}).get("pipelines", []):
        stages = []
        for s in p.get("_embedded", {}).get("statuses", []):
            status_name[s["id"]] = s["name"]
            stages.append(s["name"])
        pipelines.append({"name": p["name"], "stages": stages})

    # 2+3) сделки: тянем до 6 страниц (1500 шт) ПАРАЛЛЕЛЬНО — это ~1 запрос по времени вместо 6
    pages = {}
    def _fetch_page(n):
        pages[n] = amo_get("/leads?limit=250&order[created_at]=desc&page={}".format(n))
    ths = [threading.Thread(target=_fetch_page, args=(n,)) for n in range(1, 7)]
    for t in ths: t.start()
    for t in ths: t.join()

    all_leads = []
    for n in range(1, 7):
        all_leads.extend(pages.get(n, {}).get("_embedded", {}).get("leads", []))
    sampled = len(pages.get(6, {}).get("_embedded", {}).get("leads", [])) >= 250

    def summarize(leads):
        by_stage = {}; tc = 0; ts = 0
        for l in leads:
            st = status_name.get(l.get("status_id"), "—"); pr = l.get("price") or 0
            agg = by_stage.setdefault(st, {"count": 0, "sum": 0})
            agg["count"] += 1; agg["sum"] += pr; tc += 1; ts += pr
        stage_summary = [{"stage": k, "count": v["count"], "sum": v["sum"]}
                         for k, v in sorted(by_stage.items(), key=lambda x: -x[1]["sum"])]
        recent = [{"name": l.get("name") or "(без названия)", "price": l.get("price") or 0,
                   "stage": status_name.get(l.get("status_id"), "—")} for l in leads[:20]]
        return {"count": tc, "sum": ts, "stage_summary": stage_summary, "recent": recent}

    # разбивка по периодам (по дате создания сделки)
    now = time.time()
    lt = time.localtime(now)
    start_today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    def created(l): return l.get("created_at") or 0
    periods = {
        "all":   summarize(all_leads),
        "today": summarize([l for l in all_leads if created(l) >= start_today]),
        "week":  summarize([l for l in all_leads if created(l) >= now - 7 * 86400]),
        "month": summarize([l for l in all_leads if created(l) >= now - 30 * 86400]),
    }
    allp = periods["all"]
    return {
        "account": CFG.get("AMO_SUBDOMAIN"),
        "currency": "сом",
        "pipelines": pipelines,
        "periods": periods,
        # верхний уровень = все сделки (для совместимости)
        "recent": allp["recent"],
        "stage_summary": allp["stage_summary"],
        "total_count": allp["count"],
        "total_sum": allp["sum"],
        "sampled": sampled,
        "updated": time.strftime("%H:%M:%S"),
    }

# кэш сводки amoCRM с фоновым обновлением (как у каталога — отвечаем мгновенно)
_ov = {"t": 0.0, "data": None, "refreshing": False}

def _refresh_overview():
    if _ov["refreshing"]:
        return
    _ov["refreshing"] = True
    try:
        _ov["data"] = build_overview()
        _ov["t"] = time.time()
    except Exception:
        pass
    finally:
        _ov["refreshing"] = False

def get_overview():
    """Мгновенно отдаёт сводку amoCRM из памяти; устаревшую обновляет фоном.
    Ждать приходится только при самой первой загрузке."""
    if _ov["data"] is not None:
        if time.time() - _ov["t"] > 60 and not _ov["refreshing"]:
            threading.Thread(target=_refresh_overview, daemon=True).start()
        return _ov["data"]
    _ov["data"] = build_overview()
    _ov["t"] = time.time()
    return _ov["data"]

# --- ChatPlace: общение по протоколу MCP ---
def chatplace_call(name, arguments):
    key = CFG.get("CHATPLACE_KEY", "")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode("utf-8")
    req = urllib.request.Request("https://mcp.chatplace.io/mcp", data=body, headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Shturval/1.0",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        resp = json.loads(r.read().decode("utf-8"))
    try:
        return json.loads(resp["result"]["content"][0]["text"])
    except Exception:
        return resp.get("result", {})

# ====== АВТО-ВОРОНКА: классификация чатов по содержанию переписки ======
# Ключевые слова (нижний регистр). Достаточно вхождения подстроки.
PAY_KW = ["оплат", "оплачено", "оплатил", "перевел", "перевёл", "перевела", "перечислил",
          "скинул чек", "скинула чек", "отправил чек", "отправила чек", "чек отправ",
          "квитанц", "вот чек", "чек вот", "perevel", "paid", "төлөдү", "акча салдым"]
REJECT_KW = ["не надо", "не нужно", "ненужно", "не интерес", "неинтерес", "передума",
             "не буду", "откаж", "спасибо, не", "спасибо не", "в другой раз", "не подойд",
             "не актуально", "уже купил", "купил в другом", "дорого для меня", "керек эмес"]
PRICE_KW = ["цена", "цену", "сколько стоит", "сколько за", "сколько будет", "стоит",
            "по чем", "почем", "почём", "прайс", "стоимост", "канча турат", "канча",
            "баасы"]

def classify_chat(arr):
    """Определяет стадию воронки по сообщениям чата. arr — как из chats_messages."""
    msgs = [m for m in arr if (m.get("message") or "").strip()]
    has_reply = any(m.get("side") == "operator" for m in arr)
    # самые свежие сообщения — первыми (свежий смысл важнее)
    msgs_sorted = sorted(msgs, key=lambda m: m.get("createdAt") or 0, reverse=True)
    for m in msgs_sorted:
        t = (m.get("message") or "").lower()
        if any(k in t for k in PAY_KW):
            return "Оплачено"
        if any(k in t for k in REJECT_KW):
            return "Отказ"
        if any(k in t for k in PRICE_KW):
            return "Выставили счёт"
    return "Связались" if has_reply else "Новая заявка"

_chat_stages = {}          # chat_id -> стадия воронки
_chat_stages_t = 0

def _analyze_chats(items):
    """Прогоняет чаты через классификатор (в несколько потоков). Кэширует результат."""
    global _chat_stages_t
    from concurrent.futures import ThreadPoolExecutor
    def one(it):
        cid = it.get("id")
        if not cid:
            return None
        try:
            res = chatplace_call("chats_messages", {"chatId": cid, "limit": 30})
            arr = res if isinstance(res, list) else res.get("items", [])
            return (cid, classify_chat(arr))
        except Exception:
            return None
    out = {}
    try:
        with ThreadPoolExecutor(max_workers=5) as ex:
            for r in ex.map(one, items):
                if r:
                    out[r[0]] = r[1]
    except Exception:
        pass
    if out:
        _chat_stages.update(out)
        _chat_stages_t = time.time()

def _chat_analyzer():
    """Фоновый поток: постоянно анализирует чаты и раскладывает их по воронке."""
    while True:
        try:
            if CFG.get("CHATPLACE_KEY"):
                res = chatplace_call("chats_list", {"limit": 200})
                items = res.get("items", []) if isinstance(res, dict) else []
                _analyze_chats(items)
        except Exception:
            pass
        time.sleep(180)

def build_chats():
    res = chatplace_call("chats_list", {"limit": 200})
    items = res.get("items", []) if isinstance(res, dict) else []
    now = time.time(); lt = time.localtime(now)
    start_today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    today = sum(1 for it in items if (it.get("lastMessageAt") or 0) >= start_today)
    active = sum(1 for it in items if it.get("statusName") == "active")
    chats = []
    for it in items:
        ts = it.get("lastMessageAt")
        tm = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
        chats.append({"id": it.get("id"), "name": it.get("clientName", "клиент"),
                      "time": tm, "status": it.get("statusName", ""), "ts": ts or 0,
                      "stage": _chat_stages.get(it.get("id"), "")})
    return {"source": "Instagram", "updated": time.strftime("%H:%M:%S"),
            "today": today, "active": active, "total": len(items),
            "analyzed": len(_chat_stages), "chats": chats}

def build_chat_messages(cid):
    res = chatplace_call("chats_messages", {"chatId": cid, "limit": 40})
    arr = res if isinstance(res, list) else res.get("items", [])
    msgs = []
    for m in reversed(arr):
        x = (m.get("message") or "").strip()
        # пропускаем пустые и служебные технические сообщения
        if not x or x.endswith("Label") or x.endswith("StatusLabel"):
            continue
        msgs.append({"t": ("in" if m.get("side") == "client" else "out"), "x": x})
    return {"msgs": msgs}

# --- 1С (Yaros DataGate): товары, остатки, цены ---
import base64

def yaros_get(path):
    """GET к 1С с Basic-авторизацией. Каталог большой (~20 МБ), поэтому таймаут щедрый."""
    url = CFG.get("YAROS_URL", "").rstrip("/") + path
    auth = base64.b64encode(
        (CFG.get("YAROS_LOGIN", "") + ":" + CFG.get("YAROS_PASS", "")).encode("utf-8")
    ).decode("ascii")
    req = urllib.request.Request(url, headers={
        "Authorization": "Basic " + auth,
        "User-Agent": "Shturval/1.0",
    })
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))

def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

def _price_by_type(item, type_name):
    for p in item.get("PRICES", []):
        if p.get("TYPE") == type_name:
            return _num(p.get("PRICE"))
    return 0.0

# каталог тяжёлый (~20 МБ, ~50 сек на загрузку) — держим в памяти, обновляем фоном
_goods = {"t": 0.0, "goods": None, "cats": None, "cats_t": 0.0, "refreshing": False}
_goods_lock = threading.Lock()

def _fetch_goods():
    return yaros_get("/goods").get("goods", [])

def _fetch_cats():
    data = yaros_get("/categories")
    return {c.get("ID"): (c.get("TITLE") or "").strip() for c in data.get("categories", [])}

def _refresh_catalog():
    """Качает свежий каталог и АТОМАРНО заменяет кэш. НЕ держит общую блокировку —
    поэтому пользовательские запросы во время обновления отвечают мгновенно из старого кэша."""
    if _goods["refreshing"]:
        return
    _goods["refreshing"] = True
    try:
        goods = _fetch_goods()           # ~50 сек, но в фоне
        _goods["goods"] = goods          # подменяем разом
        _goods["t"] = time.time()
        try:
            _goods["cats"] = _fetch_cats()
            _goods["cats_t"] = time.time()
        except Exception:
            pass
    except Exception:
        pass
    finally:
        _goods["refreshing"] = False

def get_goods():
    """Мгновенно отдаёт каталог из памяти. Если устарел — обновляет ФОНОМ, не задерживая ответ.
    Ждать (~50 сек) приходится только при самой первой загрузке, когда кэша ещё нет."""
    if _goods["goods"] is not None:
        if time.time() - _goods["t"] > 120 and not _goods["refreshing"]:
            threading.Thread(target=_refresh_catalog, daemon=True).start()
        return _goods["goods"]
    with _goods_lock:                    # самый первый раз — приходится дождаться
        if _goods["goods"] is not None:
            return _goods["goods"]
        _goods["goods"] = _fetch_goods()
        _goods["t"] = time.time()
        return _goods["goods"]

def get_categories():
    if _goods["cats"] is None:
        try:
            _goods["cats"] = _fetch_cats()
            _goods["cats_t"] = time.time()
        except Exception:
            _goods["cats"] = {}
    return _goods["cats"]

def _warmer():
    """Фоновый поток: держит каталог 1С и сводку amoCRM тёплыми, чтобы запросы отвечали мгновенно."""
    while True:
        try:
            if CFG.get("YAROS_URL"):
                _refresh_catalog()
        except Exception:
            pass
        try:
            if CFG.get("AMO_TOKEN"):
                _refresh_overview()
        except Exception:
            pass
        time.sleep(110)

def build_inventory():
    """Лёгкая сводка по складу для дашборда «Товары»."""
    goods = get_goods()
    cats = get_categories()
    total_units = 0.0
    retail_value = 0.0
    cost_value = 0.0
    in_stock = 0
    by_cat = {}
    low = []
    no_cost_units = 0  # штук без закупочной цены (себестоимость по ним неизвестна)
    for x in goods:
        qty = _num(x.get("QUANTITY"))
        if qty <= 0:
            continue  # отрицательные/нулевые остатки не входят в стоимость склада (как в 1С)
        price = _num(x.get("PRICE"))
        cost = _price_by_type(x, "Закупочная")
        total_units += qty
        retail_value += qty * price
        cost_value += qty * cost
        if cost == 0:
            no_cost_units += qty
        in_stock += 1
        cat = cats.get(x.get("CATEGORY_ID"), "Без категории") or "Без категории"
        agg = by_cat.setdefault(cat, {"value": 0.0, "units": 0.0, "count": 0})
        agg["value"] += qty * price
        agg["units"] += qty
        agg["count"] += 1
        if qty <= 3:
            low.append({"title": x.get("TITLE", ""), "qty": int(qty),
                        "category": cat})
    categories = [{"title": k, "value": round(v["value"]),
                   "units": int(v["units"]), "count": v["count"]}
                  for k, v in sorted(by_cat.items(), key=lambda i: -i[1]["value"])][:8]
    # топ категорий по КОЛИЧЕСТВУ штук (для круговой диаграммы «чего больше всего»)
    cats_units = [{"title": k, "units": int(v["units"]), "count": v["count"]}
                  for k, v in sorted(by_cat.items(), key=lambda i: -i[1]["units"])][:8]
    # топ категорий по СТОИМОСТИ (для диаграммы «где больше всего денег»)
    cats_value = [{"title": k, "value": round(v["value"]), "units": int(v["units"]),
                   "count": v["count"]}
                  for k, v in sorted(by_cat.items(), key=lambda i: -i[1]["value"])][:8]
    # ВСЕ категории (для браузера категорий и фильтров)
    all_cats = [{"title": k, "value": round(v["value"]), "units": int(v["units"]),
                 "count": v["count"]}
                for k, v in sorted(by_cat.items(), key=lambda i: -i[1]["count"])]
    # «на исходе»: отдаём весь список (qty 1–3) с категорией — фильтрацию делает фронт
    low_out = sorted(low, key=lambda x: x["qty"])[:800]
    return {
        "cats_units": cats_units,
        "cats_value": cats_value,
        "all_cats": all_cats,
        "total_sku": len(goods),
        "in_stock": in_stock,
        "total_units": int(total_units),
        "retail_value": round(retail_value),
        "cost_value": round(cost_value),
        "margin_value": round(retail_value - cost_value),
        "no_cost_units": int(no_cost_units),
        "categories": categories,
        "low": low_out,
        "updated": time.strftime("%H:%M:%S"),
    }

def build_categories():
    cats = get_categories()
    items = [{"id": k, "title": v} for k, v in cats.items() if v]
    items.sort(key=lambda i: i["title"])
    return {"categories": items}

def yaros_create_good(payload):
    """Создать товар в 1С. Работает, когда задан YAROS_CREATE_URL (метод от 1С-программиста)."""
    url = CFG.get("YAROS_CREATE_URL", "").strip()
    if not url:
        return {"ok": False, "pending": True,
                "error": "Добавление в 1С ещё не подключено — ждём метод записи от 1С-программиста."}
    auth = base64.b64encode((CFG.get("YAROS_LOGIN", "") + ":" + CFG.get("YAROS_PASS", "")).encode()).decode()
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Authorization": "Basic " + auth,
                                          "Content-Type": "application/json",
                                          "User-Agent": "Shturval/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode("utf-8"))
        _goods["t"] = 0.0  # сбросить кэш, чтобы новый товар появился в списке
        return {"ok": True, "result": res}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "1С ответила ошибкой HTTP {}".format(e.code)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def search_products(q, page, per=50, cat=None, in_stock=False):
    goods = get_goods()
    cats = get_categories()
    q = (q or "").strip().lower()
    cat = (cat or "").strip().lower()
    items = goods
    if q:
        items = [x for x in items if q in (x.get("TITLE", "") or "").lower()]
    if cat:
        items = [x for x in items
                 if cat in (cats.get(x.get("CATEGORY_ID"), "") or "").lower()]
    if in_stock:
        items = [x for x in items if _num(x.get("QUANTITY")) > 0]
    total = len(items)
    start = max(0, (page - 1) * per)
    out = []
    for x in items[start:start + per]:
        out.append({
            "title": x.get("TITLE", ""),
            "category": cats.get(x.get("CATEGORY_ID"), "—") or "—",
            "price": _num(x.get("PRICE")),
            "cost": _price_by_type(x, "Закупочная"),
            "qty": int(_num(x.get("QUANTITY"))),
        })
    return {"total": total, "page": page, "per": per, "items": out}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(200, {})

    def _user(self):
        # если пользователи не заданы вовсе — защита выключена (гость видит всё)
        if not USERS:
            return {"name": "Гость", "sections": ["all"]}
        given = (self.headers.get("X-Auth") or "").strip()
        return USERS.get(given)

    def _authed(self):
        return self._user() is not None

    def do_POST(self):
        if self.path.startswith("/api/"):
            u = self._user()
            if not u:
                return self._send(401, {"error": "Требуется вход"})
            if not _allowed(u, self.path):
                return self._send(403, {"error": "Нет доступа к этому разделу"})
        if self.path.startswith("/api/send"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            cid = body.get("chatId"); text = (body.get("text") or "").strip()
            if not cid or not text:
                return self._send(400, {"error": "нужны chatId и text"})
            chatplace_call("chats_open", {"chatId": cid})        # берём диалог на себя (пауза ИИ)
            chatplace_call("chats_send_message", {"chatId": cid, "text": text})
            _cache.pop("chats", None)                            # сбросить кэш списка
            return self._send(200, {"ok": True})
        if self.path.startswith("/api/add-product"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"ok": False, "error": "плохой запрос"})
            title = (body.get("title") or "").strip()
            if not title:
                return self._send(400, {"ok": False, "error": "Нужно название товара"})
            payload = {
                "TITLE": title,
                "CATEGORY_ID": (body.get("category_id") or "").strip(),
                "PRICE": body.get("price") or 0,
                "PURCHASE_PRICE": body.get("cost") or 0,
                "MEASURE": (body.get("measure") or "шт").strip(),
                "BARCODE": (body.get("barcode") or "").strip(),
                "ARTICLE": (body.get("article") or "").strip(),
                "QUANTITY": body.get("qty") or 0,
            }
            return self._send(200, yaros_create_good(payload))
        if self.path.startswith("/api/bot-feedback"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"ok": False, "error": "плохой запрос"})
            fixes = body.get("fixes") or []
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            for f in fixes:
                f["received_at"] = stamp
                BOT_FEEDBACK.append(f)
            # дозапись в файл (на бесплатном Render диск временный, но в пределах сессии сохранится)
            try:
                with open("bot_feedback.log", "a", encoding="utf-8") as fh:
                    for f in fixes:
                        fh.write(json.dumps(f, ensure_ascii=False) + "\n")
            except Exception:
                pass
            return self._send(200, {"ok": True, "saved": len(fixes), "total": len(BOT_FEEDBACK)})
        self._send(404, {"error": "не найдено"})

    def do_GET(self):
        if self.path.startswith("/api/health"):
            ok = bool(CFG.get("AMO_TOKEN")) and bool(CFG.get("AMO_SUBDOMAIN"))
            return self._send(200, {"ok": ok, "account": CFG.get("AMO_SUBDOMAIN")})
        if self.path.startswith("/api/auth"):
            u = self._user()
            return self._send(200 if u else 401, {"ok": bool(u),
                "name": (u or {}).get("name", ""), "sections": (u or {}).get("sections", []),
                "role": (u or {}).get("role", ""), "department": (u or {}).get("department", ""),
                "plan_day": (u or {}).get("plan_day", 0)})
        # всё остальное под /api/ — нужен вход и доступ к разделу
        if self.path.startswith("/api/"):
            u = self._user()
            if not u:
                return self._send(401, {"error": "Требуется вход"})
            if not _allowed(u, self.path):
                return self._send(403, {"error": "Нет доступа к этому разделу"})
        if self.path.startswith("/api/bot-feedback"):
            return self._send(200, {"fixes": BOT_FEEDBACK, "total": len(BOT_FEEDBACK)})
        if self.path.startswith("/api/staff"):
            u = self._user()
            if not (u and "all" in u.get("sections", [])):
                return self._send(403, {"error": "Только владелец видит персонал"})
            staff = [{"name": v.get("name", ""), "sections": v.get("sections", []),
                      "role": v.get("role", ""), "department": v.get("department", ""),
                      "plan_day": v.get("plan_day", 0)} for v in USERS.values()]
            return self._send(200, {"staff": staff, "total": len(staff)})
        if self.path.startswith("/api/overview"):
            return self._send(200, get_overview())
        if self.path.startswith("/api/categories"):
            try:
                return self._send(200, build_categories())
            except Exception as e:
                return self._send(200, {"categories": [], "error": str(e)})
        if self.path.startswith("/api/inventory"):
            try:
                return self._send(200, cached("inventory", 120, build_inventory))
            except Exception as e:
                return self._send(200, {"error": "Нет связи с 1С: " + str(e)})
        if self.path.startswith("/api/products"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            q = qs.get("q", [""])[0]
            cat = qs.get("cat", [""])[0]
            in_stock = qs.get("in_stock", ["0"])[0] in ("1", "true", "yes")
            try:
                page = int(qs.get("page", ["1"])[0])
            except ValueError:
                page = 1
            try:
                return self._send(200, search_products(q, max(1, page), cat=cat, in_stock=in_stock))
            except Exception as e:
                return self._send(200, {"error": "Нет связи с 1С: " + str(e), "items": [], "total": 0})
        if self.path.startswith("/api/chats"):
            if CFG.get("CHATPLACE_KEY"):
                return self._send(200, cached("chats", 8, build_chats))
            p = os.path.join(HERE, "chats-data.json")
            data = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"chats": []}
            return self._send(200, data)
        if self.path.startswith("/api/chat"):
            from urllib.parse import urlparse, parse_qs
            cid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not cid:
                return self._send(400, {"error": "нет id"})
            return self._send(200, build_chat_messages(cid))
        self._send(404, {"error": "не найдено"})

    def log_message(self, *a):
        pass  # тихий режим

if __name__ == "__main__":
    if not CFG.get("AMO_TOKEN"):
        print("⚠️  Токен не найден. Проверь файл backend/secret.env")
    print("=" * 48)
    print("  ШТУРВАЛ — сервер-посредник запущен ✅")
    print("  Аккаунт amoCRM:", CFG.get("AMO_SUBDOMAIN"))
    print("  Адрес: http://localhost:{}".format(PORT))
    print("  Чтобы остановить — закрой это окно или нажми Ctrl+C")
    print("=" * 48)
    # фоновый прогрев каталога 1С и сводки amoCRM (чтобы запросы отвечали мгновенно)
    if CFG.get("YAROS_URL") or CFG.get("AMO_TOKEN"):
        threading.Thread(target=_warmer, daemon=True).start()
    # фоновый анализ чатов → авто-раскладка по воронке
    if CFG.get("CHATPLACE_KEY"):
        threading.Thread(target=_chat_analyzer, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
