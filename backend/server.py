#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Штурвал — бэкенд-«посредник» между amoCRM и сайтом.
Токен хранится здесь, на сервере, и НЕ попадает в код сайта.
Запуск: python3 server.py   (или двойной клик по «Запустить-сервер.command»)
"""
import os, json, time, threading, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Часовой пояс — Бишкек (UTC+6). Чтобы «сегодня», время сообщений, явка, касса
# и отчёты считались по бишкекскому времени, а не по времени сервера (UTC).
os.environ["TZ"] = "Asia/Bishkek"
try:
    time.tzset()
except Exception:
    pass

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
    # ИИ-помощник магазина (Anthropic Claude)
    cfg["ANTHROPIC_API_KEY"] = env("ANTHROPIC_API_KEY")
    cfg["ANTHROPIC_MODEL"] = env("ANTHROPIC_MODEL") or "claude-haiku-4-5-20251001"
    # База данных Supabase (постоянное хранение)
    cfg["SUPABASE_URL"] = env("SUPABASE_URL")
    cfg["SUPABASE_KEY"] = env("SUPABASE_KEY")
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
                                 "plan_day": u.get("plan_day", 0),
                                 "salary_month": u.get("salary_month", 0),
                                 "daily_rate": u.get("daily_rate", 0),
                                 "bonus_month": u.get("bonus_month", 0)}
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
    ("/api/assistant", "ai"),
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
    """Простое и точное правило по запросу владельца:
    - ответил ЖИВОЙ менеджер (side == "operator") → «Связались»;
    - ответил только бот (ai_assistant) или никто → остаётся «Новая заявка».
    Стадии Оплачено/Выставили счёт/Отказ ставит менеджер вручную (перетаскиванием)."""
    has_human = any(m.get("side") == "operator" for m in arr)
    return "Связались" if has_human else "Новая заявка"

_chat_stages = {}          # chat_id -> стадия воронки
_chat_stages_t = 0

_analyze_idx = 0  # курсор по списку чатов — анализируем малыми порциями по кругу

def _chat_analyzer():
    """Фоновый поток: БЕРЕЖНО (по чуть-чуть) анализирует чаты и раскладывает по воронке.
    ChatPlace жёстко лимитирует запросы (429), поэтому: переиспользуем кэш списка чатов,
    обрабатываем по 5 чатов за цикл, последовательно, с паузами; на 429 — отступаем."""
    global _analyze_idx
    time.sleep(20)  # дать серверу прогреться, не стучаться сразу
    while True:
        slept = 60
        try:
            if CFG.get("CHATPLACE_KEY"):
                data = cached("chats", 30, build_chats)      # тот же кэш, без лишнего вызова
                items = data.get("chats", []) if isinstance(data, dict) else []
                if items:
                    if _analyze_idx >= len(items):
                        _analyze_idx = 0
                    batch = items[_analyze_idx:_analyze_idx + 5]
                    _analyze_idx += 5
                    for it in batch:
                        cid = it.get("id")
                        if not cid:
                            continue
                        try:
                            r = chatplace_call("chats_messages", {"chatId": cid, "limit": 20})
                            arr = r if isinstance(r, list) else r.get("items", [])
                            _chat_stages[cid] = classify_chat(arr)
                        except urllib.error.HTTPError as e:
                            if e.code == 429:
                                slept = 180  # лимит — отдыхаем подольше
                                break
                        except Exception:
                            pass
                        time.sleep(2.5)      # пауза между запросами — бережём лимит
        except Exception:
            pass
        time.sleep(slept)

_chats_last = {"source": "Instagram", "updated": "", "today": 0, "active": 0,
               "total": 0, "analyzed": 0, "chats": []}

def build_chats():
    """Список чатов. Если ChatPlace ответил 429/ошибкой — отдаём прошлый успешный
    результат, чтобы платформа не падала в 502."""
    global _chats_last
    try:
        res = chatplace_call("chats_list", {"limit": 200})
    except Exception:
        return _chats_last
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
    _chats_last = {"source": "Instagram", "updated": time.strftime("%H:%M:%S"),
                   "today": today, "active": active, "total": len(items),
                   "analyzed": len(_chat_stages), "chats": chats}
    return _chats_last

def build_chat_messages(cid):
    res = chatplace_call("chats_messages", {"chatId": cid, "limit": 40})
    arr = res if isinstance(res, list) else res.get("items", [])
    msgs = []
    for m in reversed(arr):
        x = (m.get("message") or "").strip()
        # пропускаем пустые и служебные технические сообщения
        if not x or x.endswith("Label") or x.endswith("StatusLabel"):
            continue
        ts = m.get("createdAt") or 0
        tm = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
        msgs.append({"t": ("in" if m.get("side") == "client" else "out"),
                     "x": x, "ts": ts, "tm": tm, "id": m.get("id", "")})
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


# ====== ИИ-ПОМОЩНИК МАГАЗИНА (Anthropic Claude) ======
AI_SYSTEM = (
    "Ты — деловой ИИ-помощник магазина «Бизмарт» (товары для дома, Кыргызстан). "
    "Отвечай ТОЛЬКО на вопросы про этот магазин: товары, остатки, цены, продажи, "
    "клиенты, переписки, воронка, сотрудники, финансы, маркетинг этого бизнеса. "
    "Если вопрос не про магазин (погода, политика, общие темы) — вежливо откажись "
    "одной фразой и предложи спросить про магазин. "
    "Валюта — сом (KGS), НЕ тенге и НЕ рубли. Отвечай по-русски, кратко и по делу, "
    "с конкретными цифрами из данных ниже. Если данных не хватает — честно скажи. "
    "Когда уместно — давай практичные рекомендации владельцу."
)

def _ai_context(crm, want_chats):
    """Собирает сводку по магазину для ИИ из 1С, amoCRM, чатов и CRM."""
    parts = []
    try:
        inv = cached("inventory", 120, build_inventory)
        parts.append("СКЛАД (1С): позиций {}, в наличии {}, штук {}, склад в рознице {} сом, "
                     "себестоимость {} сом, потенц. наценка {} сом.".format(
            inv.get("total_sku"), inv.get("in_stock"), inv.get("total_units"),
            inv.get("retail_value"), inv.get("cost_value"), inv.get("margin_value")))
        cv = ", ".join("{} ({} сом)".format(c["title"], c["value"]) for c in (inv.get("cats_value") or [])[:8])
        cu = ", ".join("{} ({} шт)".format(c["title"], c["units"]) for c in (inv.get("cats_units") or [])[:8])
        parts.append("Категории по стоимости: " + cv)
        parts.append("Категории по количеству: " + cu)
        low = inv.get("low") or []
        parts.append("Товаров на исходе (1-3 шт): {}.".format(len(low)))
    except Exception as e:
        parts.append("Склад (1С): данные недоступны (%s)." % e)
    try:
        ov = get_overview()
        per = (ov.get("periods") or {})
        for k, lbl in (("today", "сегодня"), ("week", "неделя"), ("month", "месяц")):
            p = per.get(k) or {}
            parts.append("amoCRM {}: сделок {}, сумма {} сом.".format(lbl, p.get("count"), p.get("sum")))
    except Exception:
        pass
    try:
        dist = {}
        for st in _chat_stages.values():
            dist[st] = dist.get(st, 0) + 1
        if dist:
            parts.append("Авто-воронка по перепискам: " + ", ".join("{}: {}".format(k, v) for k, v in dist.items()))
    except Exception:
        pass
    if isinstance(crm, dict):
        parts.append("CRM (наша): " + json.dumps(crm, ensure_ascii=False))
    if want_chats:
        try:
            res = chatplace_call("chats_list", {"limit": 12})
            items = res.get("items", []) if isinstance(res, dict) else []
            samples = []
            for it in items[:12]:
                cid = it.get("id")
                if not cid:
                    continue
                m = build_chat_messages(cid).get("msgs", [])[-6:]
                txt = " | ".join(("клиент: " if x["t"] == "in" else "мы: ") + x["x"] for x in m)
                if txt:
                    samples.append("[{}] {}".format(it.get("clientName", "клиент"), txt[:400]))
            if samples:
                parts.append("ОБРАЗЦЫ ПЕРЕПИСОК (последние сообщения):\n" + "\n".join(samples))
        except Exception:
            pass
    return "\n".join(parts)

import re as _re
_AI_STOP = set("сколько скольк сколка товар товара товаров товары позиц позиция позиции есть мне мои моих "
               "для это эта этот отдел отделе отдела категория категории магазин магазине покажи показать "
               "какой какие какая что где когда сейчас всего вот про над под при как нам наш наша "
               "штук шт сом сума сумма деньги денег было будет".split())

def _money(n):
    try:
        n = int(round(float(n)))
    except Exception:
        return str(n)
    return "{:,}".format(n).replace(",", " ")

def ai_local_answer(question, crm):
    """Бесплатный режим: отвечает по данным магазина без ИИ-движка."""
    q = (question or "").lower()
    crm = crm if isinstance(crm, dict) else {}
    inv, ov = {}, {}
    try:
        inv = cached("inventory", 120, build_inventory)
    except Exception:
        pass
    try:
        ov = get_overview()
    except Exception:
        pass
    has = lambda *ws: any(w in q for w in ws)
    period = "week"
    if has("сегодня", "за день"):
        period = "today"
    elif has("месяц"):
        period = "month"
    plabel = {"today": "сегодня", "week": "за неделю", "month": "за месяц"}[period]

    is_top = has("топ", "популярн", "ходов", "больше всего", "лучшие", "хит")
    # 1) Категории / отдел / «сколько товаров»
    if not is_top and has("товар", "позиц", "ассортимент", "отдел", "категор", "наличии"):
        cats = inv.get("all_cats") or []
        qwords = [w for w in _re.findall(r"[а-яёa-z]+", q) if len(w) >= 3 and w not in _AI_STOP]
        matched = []
        for c in cats:
            tw = [w for w in _re.findall(r"[а-яёa-z]+", c["title"].lower()) if len(w) >= 3]
            if any((qw[:4] in t or t[:4] in qw) for qw in qwords for t in tw):
                matched.append(c)
        if matched:
            matched = sorted(matched, key=lambda c: -c["count"])[:15]
            tc = sum(c["count"] for c in matched); tu = sum(c["units"] for c in matched)
            lines = ["Нашёл по твоему запросу — всего {} позиций ({} шт на складе):".format(tc, tu)]
            lines += ["• {} — {} поз., {} шт".format(c["title"], c["count"], c["units"]) for c in matched]
            return "\n".join(lines)
        if has("сколько", "всего", "общее"):
            return ("Всего в каталоге 1С: {} позиций, в наличии {}, штук на складе {}. "
                    "Уточни отдел/категорию (например: «сколько в мужской обуви») — посчитаю точнее."
                    ).format(inv.get("total_sku"), inv.get("in_stock"), inv.get("total_units"))

    # 2) На исходе / пополнить
    if has("исход", "заканч", "пополн", "мало остал", "остаток мал", "дефицит"):
        low = inv.get("low") or []
        sample = low[:15]
        lines = ["⚠️ На исходе (остаток 1–3 шт): {} позиций.".format(len(low))]
        lines += ["• {} — {} шт ({})".format(x["title"], x["qty"], x.get("category", "")) for x in sample]
        if len(low) > len(sample):
            lines.append("…и ещё {}. В разделе «Товары» можно отфильтровать по категории.".format(len(low) - len(sample)))
        return "\n".join(lines)

    # 3) Продажи / выручка / сумма
    if has("продаж", "выручк", "сумм", "оборот", "заработал", "доход"):
        out = []
        p = (ov.get("periods") or {}).get(period) or {}
        if p:
            out.append("amoCRM {}: сделок {}, сумма {} сом.".format(plabel, p.get("count"), _money(p.get("sum"))))
        if crm.get("paid_count") is not None:
            out.append("Наша CRM: оплачено {} сделок на {} сом.".format(crm.get("paid_count"), _money(crm.get("paid_sum"))))
        return "\n".join(out) if out else "Пока нет данных по продажам за этот период."

    # 4) Заявки / лиды
    if has("заявк", "лид", "обращен", "написал"):
        bs = crm.get("by_stage") or {}
        out = ["Новых заявок (необработанных): {}.".format(crm.get("new_leads", 0))]
        if bs:
            out.append("По стадиям: " + ", ".join("{}: {}".format(k, v) for k, v in bs.items() if v))
        return "\n".join(out)

    # 5) Отказы / почему сливаются
    if has("отказ", "сливают", "сливаются", "уход", "теряем", "почему не", "возраж", "бросают"):
        rr = crm.get("reject_reasons") or {}
        if rr:
            top = sorted(rr.items(), key=lambda i: -i[1])
            lines = ["Причины отказов (из CRM):"] + ["• {} — {}".format(k, v) for k, v in top]
            lines.append("\n💡 Для глубокого разбора переписок («почему именно сливаются») нужен ИИ-движок — "
                         "скажи, когда захочешь подключить ключ Anthropic.")
            return "\n".join(lines)
        return ("Пока нет отмеченных отказов в CRM. Глубокий разбор переписок (почему клиенты уходят) "
                "умеет полноценный ИИ — подключается ключом Anthropic.")

    # 6) Топ / популярные / больше всего
    if is_top:
        if has("деньг", "стоимост", "выручк", "сумм", "прибыл"):
            cv = inv.get("cats_value") or []
            return "Категории, где больше всего денег:\n" + "\n".join(
                "• {} — {} сом ({} шт)".format(c["title"], _money(c["value"]), c["units"]) for c in cv[:8])
        cu = inv.get("cats_units") or []
        return "Категории, где больше всего товара (по количеству):\n" + "\n".join(
            "• {} — {} шт".format(c["title"], c["units"]) for c in cu[:8])

    # 7) Склад / стоимость / себестоимость / наценка
    if has("склад", "стоимост", "себестоим", "наценк", "маржа", "капитал"):
        return ("Склад (1С): {} позиций, {} шт в наличии.\n"
                "Стоимость в рознице: {} сом\nСебестоимость: {} сом\nПотенц. наценка: {} сом."
                ).format(inv.get("total_sku"), inv.get("total_units"),
                         _money(inv.get("retail_value")), _money(inv.get("cost_value")),
                         _money(inv.get("margin_value")))

    # Не распознал — подсказываем возможности
    return ("Я отвечаю по данным магазина. Спроси, например:\n"
            "• «сколько товаров в мужской обуви»\n• «что на исходе»\n"
            "• «сколько продаж за неделю»\n• «сколько новых заявок»\n"
            "• «причины отказов»\n• «топ категорий по деньгам»\n• «стоимость склада»\n\n"
            "💬 Свободный диалог и анализ переписок включатся, когда подключим ИИ-движок (ключ Anthropic).")

def ai_answer(question, crm, history):
    key = CFG.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"free_mode": True, "answer": ai_local_answer(question, crm)}
    ql = (question or "").lower()
    want_chats = any(w in ql for w in ["перепис", "диалог", "чат", "клиент", "сливают", "сливаются",
                                       "уход", "отказ", "почему не", "возраж"])
    context = _ai_context(crm, want_chats)
    msgs = []
    for h in (history or [])[-6:]:
        role = h.get("role"); content = h.get("content")
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": str(content)})
    msgs.append({"role": "user",
                 "content": "ДАННЫЕ МАГАЗИНА (на сейчас):\n" + context + "\n\nВОПРОС: " + question})
    body = json.dumps({
        "model": CFG.get("ANTHROPIC_MODEL"),
        "max_tokens": 1024,
        "system": AI_SYSTEM,
        "messages": msgs,
    }).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "User-Agent": "Shturval/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        return {"answer": text or "Пустой ответ от ИИ."}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        return {"error": "ИИ вернул ошибку HTTP {}. {}".format(e.code, detail)}
    except Exception as e:
        return {"error": "Не удалось обратиться к ИИ: %s" % e}


# ====== БАЗА ДАННЫХ (Supabase / Postgres, REST API) ======
from urllib.parse import quote as _q

COMPANY_ID = "bizmart"   # пока одна компания; мульти-тенант — позже (поле company_id уже заложено)

def supa_on():
    return bool(CFG.get("SUPABASE_URL") and CFG.get("SUPABASE_KEY"))

def _supa(method, table, params="", body=None):
    """Запрос к Supabase REST (PostgREST). Секретный ключ обходит RLS (полный доступ)."""
    base = CFG.get("SUPABASE_URL", "").rstrip("/")
    key = CFG.get("SUPABASE_KEY", "")
    url = base + "/rest/v1/" + table + params
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"apikey": key, "Authorization": "Bearer " + key,
               "Content-Type": "application/json", "User-Agent": "Shturval/1.0"}
    if method in ("POST", "PATCH", "PUT"):
        headers["Prefer"] = "return=representation,resolution=merge-duplicates"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        txt = r.read().decode("utf-8")
        return json.loads(txt) if txt.strip() else []

# ====== ЗАРПЛАТА / КАДРЫ ======
# Хранение: Supabase (постоянно). Файл — резервный вариант, если база не настроена.
PAYROLL_FILE = os.path.join(HERE, "payroll.json")

def _payroll_load():
    try:
        return json.load(open(PAYROLL_FILE, encoding="utf-8"))
    except Exception:
        return {}

PAYROLL = _payroll_load()

def _payroll_save():
    try:
        json.dump(PAYROLL, open(PAYROLL_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass

def payroll_upsert(rec):
    """Сохранить запись зарплаты: в Supabase (постоянно) или в файл (резерв)."""
    if supa_on():
        try:
            r = dict(rec); r.setdefault("company_id", COMPANY_ID); r.pop("updated_at", None)
            _supa("POST", "payroll", "?on_conflict=company_id,name,month", r)
            return
        except Exception:
            try:   # вдруг колонка days ещё не добавлена — сохраним без неё
                r2 = dict(rec); r2.setdefault("company_id", COMPANY_ID)
                r2.pop("updated_at", None); r2.pop("days", None)
                _supa("POST", "payroll", "?on_conflict=company_id,name,month", r2)
                return
            except Exception:
                pass
    _payroll_save()

def _cur_month():
    lt = time.localtime(); return "%04d-%02d" % (lt.tm_year, lt.tm_mon)

def _today_str():
    lt = time.localtime(); return "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)

def payroll_rec(name):
    """Запись зарплаты сотрудника за текущий месяц (помесячно). Источник — Supabase."""
    m = _cur_month()
    if supa_on():
        try:
            rows = _supa("GET", "payroll",
                         "?company_id=eq.%s&name=eq.%s&month=eq.%s&select=*"
                         % (_q(COMPANY_ID), _q(name), _q(m)))
            if rows:
                return rows[0]
            rec = {"company_id": COMPANY_ID, "name": name, "month": m,
                   "present_days": 0, "advance": 0, "bonus": 0, "last_present": ""}
            _supa("POST", "payroll", "", rec)
            return rec
        except Exception:
            pass
    # резерв: файл/память
    r = PAYROLL.get(name)
    if not r or r.get("month") != m:
        r = {"month": m, "present_days": 0, "advance": 0, "bonus": 0, "last_present": ""}
        PAYROLL[name] = r
    return r

def user_by_name(name):
    for v in USERS.values():
        if v.get("name") == name:
            return v
    return None

def _present_in_month(days, m):
    return sum(1 for d, st in (days or {}).items() if isinstance(d, str) and d[:7] == m and st == "p")

def payroll_view(user):
    name = user.get("name", "")
    r = payroll_rec(name)
    days = r.get("days") or {}
    m = _cur_month(); today = _today_str()
    pd = _present_in_month(days, m) if days else int(r.get("present_days") or 0)
    sal = float(user.get("salary_month") or 0)
    rate = float(user.get("daily_rate") or 0)
    if rate <= 0 and sal > 0:
        rate = round(sal / 26.0)            # ~26 рабочих дней в месяце
    accrued = round(pd * rate)
    bonus = float(r.get("bonus") or 0); adv = float(r.get("advance") or 0)
    return {"name": name, "role": user.get("role", ""), "department": user.get("department", ""),
            "salary_month": sal, "daily_rate": rate, "bonus_month": float(user.get("bonus_month") or 0),
            "present_days": pd, "accrued": accrued, "bonus": bonus,
            "advance": adv, "to_receive": round(accrued + bonus - adv), "days": days,
            "marked_today": days.get(today) == "p", "marked_absent_today": days.get(today) == "a"}

def payroll_all():
    seen = set(); out = []
    for v in USERS.values():
        nm = v.get("name", "")
        if nm in seen or "all" in v.get("sections", []):  # владельца не показываем
            continue
        seen.add(nm); out.append(payroll_view(v))
    return out


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
            try:
                chatplace_call("chats_open", {"chatId": cid})    # берём диалог на себя (пауза ИИ)
                chatplace_call("chats_send_message", {"chatId": cid, "text": text})
            except Exception as e:
                return self._send(200, {"ok": False, "error": "ChatPlace: " + str(e)})
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
        if self.path.startswith("/api/assistant"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            q = (body.get("question") or "").strip()
            if not q:
                return self._send(400, {"error": "пустой вопрос"})
            return self._send(200, ai_answer(q, body.get("crm"), body.get("history")))
        if self.path.startswith("/api/payroll"):
            u = self._user()
            secs = u.get("sections", []) if u else []
            if not (u and ("all" in secs or "hr" in secs)):   # владелец или HR-менеджер
                return self._send(403, {"error": "Только владелец/HR может отмечать явку и зарплаты"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            action = body.get("action"); name = (body.get("name") or "").strip()
            tu = user_by_name(name)
            if not tu:
                return self._send(400, {"error": "сотрудник не найден"})
            r = payroll_rec(name)
            days = r.get("days") or {}
            today = _today_str()
            if action == "present":
                days[today] = "p"
            elif action == "absent":           # «не пришёл»
                days[today] = "a"
            elif action == "unpresent":        # снять отметку за сегодня
                days.pop(today, None)
            elif action == "advance":
                r["advance"] = round(float(r.get("advance") or 0) + float(body.get("amount") or 0))
            elif action == "bonus":
                r["bonus"] = round(float(r.get("bonus") or 0) + float(body.get("amount") or 0))
            else:
                return self._send(400, {"error": "неизвестное действие"})
            r["days"] = days
            r["present_days"] = _present_in_month(days, _cur_month())
            r["last_present"] = today if days.get(today) == "p" else r.get("last_present", "")
            payroll_upsert(r)
            return self._send(200, {"ok": True, "view": payroll_view(tu)})
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
        if self.path.startswith("/api/payroll"):
            u = self._user()
            if not u:
                return self._send(401, {"error": "вход"})
            secs = u.get("sections", [])
            if "all" in secs or "hr" in secs:      # владелец и HR-менеджер видят всех
                return self._send(200, {"all": payroll_all()})
            return self._send(200, {"me": payroll_view(u)})
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
                try:
                    return self._send(200, cached("chats", 30, build_chats))
                except Exception as e:
                    return self._send(200, {"chats": [], "total": 0, "error": "ChatPlace: " + str(e)})
            p = os.path.join(HERE, "chats-data.json")
            data = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"chats": []}
            return self._send(200, data)
        if self.path.startswith("/api/chat"):
            from urllib.parse import urlparse, parse_qs
            cid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not cid:
                return self._send(400, {"error": "нет id"})
            try:
                return self._send(200, build_chat_messages(cid))
            except Exception as e:
                return self._send(200, {"msgs": [], "error": "ChatPlace: " + str(e)})
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
