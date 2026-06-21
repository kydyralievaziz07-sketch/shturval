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
    # логин владельца (необязателен; вход логин+пароль). Если пусто — владелец входит по паролю.
    cfg["OWNER_LOGIN"] = env("OWNER_LOGIN")
    # доп. пользователи с ролями (JSON-список): [{"login":"...","pw":"...","name":"...","sections":["chats"]}]
    cfg["USERS_JSON"] = env("USERS_JSON")
    # ИИ-помощник магазина (Anthropic Claude)
    cfg["ANTHROPIC_API_KEY"] = env("ANTHROPIC_API_KEY")
    cfg["ANTHROPIC_MODEL"] = env("ANTHROPIC_MODEL") or "claude-haiku-4-5-20251001"
    # База данных Supabase (постоянное хранение)
    cfg["SUPABASE_URL"] = env("SUPABASE_URL")
    cfg["SUPABASE_KEY"] = env("SUPABASE_KEY")
    # Instagram напрямую (Meta Graph API) — без ChatPlace
    cfg["IG_VERIFY_TOKEN"] = env("IG_VERIFY_TOKEN") or "shturval-ig-2026"   # слово-пароль для webhook (вписать в Meta)
    cfg["IG_TOKEN"] = env("IG_TOKEN")            # токен доступа Instagram/страницы (заполнит владелец после настройки Meta)
    cfg["IG_APP_SECRET"] = env("IG_APP_SECRET")  # секрет приложения Meta (для проверки подписи и обновления токена)
    cfg["IG_ACCOUNT_ID"] = env("IG_ACCOUNT_ID")  # id бизнес-аккаунта Instagram
    return cfg

CFG = load_secret()
BASE = "https://{}.amocrm.ru/api/v4".format(CFG.get("AMO_SUBDOMAIN", ""))

# --- пользователи и роли ---
# каждый пользователь: пароль -> {name, sections}. sections=["all"] = видит всё.
# разделы: dash, crm, chats, prod, sales, clients, market, analytics, fin, ai, set
def load_users():
    """Возвращает два индекса:
      users     — по паролю (обратная совместимость: вход только по паролю);
      by_login  — по логину в нижнем регистре (вход логин+пароль).
    Один и тот же объект пользователя кладём в оба индекса."""
    users = {}
    by_login = {}
    def _add(pw, info, login=""):
        info = dict(info)
        info["pw"] = pw
        if login:
            info["login"] = login
            by_login[login.lower()] = info
        if pw:
            users[pw] = info
    owner = CFG.get("SITE_PASSWORD", "").strip()
    if owner:
        _add(owner, {"name": "Владелец", "sections": ["all"], "role": "all"},
             CFG.get("OWNER_LOGIN", "").strip())
    raw = CFG.get("USERS_JSON", "").strip()
    if raw:
        try:
            for u in json.loads(raw):
                pw = (u.get("pw") or "").strip()
                if not pw:
                    continue
                _add(pw, {"name": u.get("name", "Сотрудник"),
                          "sections": u.get("sections", []),
                          "role": u.get("role", ""),
                          "department": u.get("department", ""),
                          "plan_day": u.get("plan_day", 0),
                          "salary_month": u.get("salary_month", 0),
                          "daily_rate": u.get("daily_rate", 0),
                          "bonus_month": u.get("bonus_month", 0)},
                     (u.get("login") or "").strip())
        except Exception:
            pass
    # --- сотрудники, заведённые руководителем из интерфейса (таблица employees) ---
    # Перекрывают/дополняют записи из env. active=false → сотрудник «убран» (скрыт).
    try:
        rows = _supa("GET", "employees",
                     "?company_id=eq.%s&select=*" % _q(COMPANY_ID))
        for u in (rows or []):
            login = (u.get("login") or "").strip()
            if not login:
                continue
            ll = login.lower()
            old = by_login.get(ll)
            if not u.get("active", True):                 # «убрали» — спрятать
                if old:
                    by_login.pop(ll, None)
                    if old.get("pw"):
                        users.pop(old["pw"], None)
                continue
            secs = u.get("sections")
            if isinstance(secs, str):
                secs = [s.strip() for s in secs.split(",") if s.strip()]
            if not secs:
                secs = ["myday"]
            if old and old.get("pw"):                      # убрать старую парольную запись
                users.pop(old["pw"], None)
            _add((u.get("pw") or "").strip(),
                 {"name": u.get("name", "Сотрудник"), "sections": secs,
                  "role": u.get("role", ""), "department": u.get("department", ""),
                  "plan_day": u.get("plan_day", 0),
                  "salary_month": u.get("salary_month", 0),
                  "daily_rate": u.get("daily_rate", 0),
                  "bonus_month": u.get("bonus_month", 0)},
                 login)
    except Exception:
        pass
    return users, by_login

USERS, USERS_BY_LOGIN = load_users()

def all_users():
    """Все пользователи без дублей. ВАЖНО: USERS ключуется по паролю, поэтому
    сотрудники с общим паролем там схлопываются в одного — берём по логину."""
    seen = set(); out = []
    for v in list(USERS_BY_LOGIN.values()) + list(USERS.values()):
        if id(v) in seen:
            continue
        seen.add(id(v)); out.append(v)
    return out

def _pkey(user):
    """Ключ зарплаты — логин (уникален). Если логина нет — имя (старое поведение)."""
    return (user.get("login") or user.get("name") or "").strip()

# какой раздел нужен для каждого data-эндпоинта (для проверки прав)
SECTION_OF = [
    ("/api/overview", "crm"),
    ("/api/inventory", "prod"), ("/api/products", "prod"),
    ("/api/categories", "prod"), ("/api/add-product", "prod"),
    ("/api/sales-history", "sales"), ("/api/sales", "sales"),
    ("/api/suppliers", "supl"), ("/api/expenses", "fin"),
    ("/api/market", "market"),
    ("/api/chats", "chats"), ("/api/chat", "chats"), ("/api/send", "chats"),
    ("/api/bot-feedback", "chats"),
    ("/api/ig/conversations", "chats"), ("/api/ig/thread", "chats"),
    ("/api/ig/reply", "chats"), ("/api/ig/accounts", "chats"),
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
    # JSON-RPC-ошибка
    if resp.get("error"):
        err = resp["error"]
        raise RuntimeError(str(err.get("message") or err))
    result = resp.get("result", {})
    # ошибка на уровне инструмента (например отправка: "Chat not found",
    # "вне 24-часового окна" и т.п.) — ChatPlace отдаёт isError=true + текст
    if isinstance(result, dict) and result.get("isError"):
        txt = "ошибка ChatPlace"
        try:
            txt = result["content"][0]["text"]
        except Exception:
            pass
        raise RuntimeError(txt)
    try:
        return json.loads(result["content"][0]["text"])
    except Exception:
        return result

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

def last_from_client(arr):
    """True, если последнее СОДЕРЖАТЕЛЬНОЕ сообщение — от клиента (мы не ответили).
    arr приходит от chats_messages (новые сверху)."""
    for m in arr:
        x = (m.get("message") or "").strip()
        if not x or x.endswith("Label") or x.endswith("StatusLabel"):
            continue
        return m.get("side") == "client"
    return False

_chat_stages = {}          # chat_id -> стадия воронки
_chat_unread = {}          # chat_id -> True, если ждёт ответа (последнее сообщение от клиента)
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
                            _chat_unread[cid] = last_from_client(arr)
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
                      "stage": _chat_stages.get(it.get("id"), ""),
                      "unread": bool(_chat_unread.get(it.get("id"), False))})
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

# ===== Instagram напрямую (Meta Graph API), без ChatPlace =====
# Подключение через «Instagram Login» (graph.instagram.com). Один Штурвал — много аккаунтов.
IG_GRAPH = "https://graph.instagram.com/v21.0"

def ig_accounts():
    """Все подключённые Instagram-аккаунты компании: {ig_id, username, token}."""
    if not supa_on():
        return []
    try:
        return _supa("GET", "ig_accounts",
                     "?company_id=eq.%s&select=*" % _q(COMPANY_ID)) or []
    except Exception:
        return []

def ig_account_map():
    """ig_id -> запись аккаунта (для маршрутизации сообщений по аккаунтам)."""
    return {str(a.get("ig_id")): a for a in ig_accounts() if a.get("ig_id")}

def ig_token_for(account_id):
    """Токен того аккаунта, НА который пришло сообщение (account_id = recipient_id из webhook)."""
    a = ig_account_map().get(str(account_id))
    if a and a.get("token"):
        return a["token"]
    return CFG.get("IG_TOKEN", "")              # запасной (если один аккаунт в конфиге)

def ig_resolve(token):
    """Узнать ig_id и username по токену (graph.instagram.com/me)."""
    url = IG_GRAPH + "/me?fields=user_id,username&access_token=" + _q(token)
    req = urllib.request.Request(url, headers={"User-Agent": "Shturval/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode() or "{}")
    return str(d.get("user_id") or d.get("id") or ""), d.get("username", "")

def ig_upsert_account(token):
    """Сохранить/обновить аккаунт по токену (узнаём ig_id и username сами)."""
    ig_id, username = ig_resolve(token)
    if not ig_id:
        raise RuntimeError("Токен не дал user_id — проверьте его")
    _supa("POST", "ig_accounts", "?on_conflict=company_id,ig_id",
          {"company_id": COMPANY_ID, "ig_id": ig_id, "username": username, "token": token})
    return {"ig_id": ig_id, "username": username}

def _ig_fetch_name_async(igsid, account_id):
    """В фоне узнать ник нового клиента и сохранить (чтобы webhook отвечал быстро)."""
    def run():
        try:
            tok = ig_token_for(account_id)
            if not tok:
                return
            url = IG_GRAPH + "/" + _q(str(igsid)) + "?fields=username,name&access_token=" + _q(tok)
            d = json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "Shturval/1.0"}), timeout=15).read().decode())
            ig_save_name(igsid, d.get("username") or d.get("name") or "")
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()

def ig_store_event(evt):
    """Разобрать webhook-событие Instagram и сохранить входящие сообщения в таблицу ig_inbox."""
    if not isinstance(evt, dict):
        return
    for entry in (evt.get("entry") or []):
        for m in (entry.get("messaging") or []):
            msg = m.get("message") or {}
            if not msg or msg.get("is_echo"):       # echo = наши же отправленные, пропускаем
                continue
            sender = str((m.get("sender") or {}).get("id", ""))
            recipient = str((m.get("recipient") or {}).get("id", ""))  # наш аккаунт
            row = {"company_id": COMPANY_ID, "sender_id": sender, "recipient_id": recipient,
                   "mid": msg.get("mid", ""), "text": msg.get("text", ""),
                   "ts": int(m.get("timestamp") or 0), "direction": "in", "raw": m}
            try:
                _supa("POST", "ig_inbox", "", row)
            except Exception:
                pass
            if sender and sender not in _ig_names:   # новый клиент — узнаём ник в фоне
                _ig_fetch_name_async(sender, recipient)

def ig_send(recipient_id, text, from_account_id=None):
    """Отправить сообщение в Instagram. from_account_id = НАШ аккаунт (тот, на который писал клиент)."""
    token = ig_token_for(from_account_id) if from_account_id else CFG.get("IG_TOKEN", "")
    if not token:
        raise RuntimeError("Instagram пока не подключён (нет токена). Сначала настройте приложение Meta.")
    url = IG_GRAPH + "/me/messages?access_token=" + _q(token)
    body = json.dumps({"recipient": {"id": recipient_id}, "message": {"text": text}}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "Shturval/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")

# имена клиентов по их IGSID. Кэш в памяти + таблица ig_names (БЕЗ живых запросов в момент загрузки).
_ig_names = {}
_ig_names_ts = 0
def ig_load_names():
    """Подгрузить имена из БД в память; обновлять не чаще раза в 5 минут."""
    global _ig_names_ts
    if time.time() - _ig_names_ts < 300 and _ig_names:
        return
    try:
        off = 0
        while True:                          # БД отдаёт максимум 1000 строк — тянем постранично
            rows = _supa("GET", "ig_names",
                         "?company_id=eq.%s&select=igsid,name&order=igsid&limit=1000&offset=%d"
                         % (_q(COMPANY_ID), off)) or []
            for row in rows:
                _ig_names[str(row.get("igsid"))] = row.get("name") or ""
            if len(rows) < 1000:
                break
            off += 1000
        _ig_names_ts = time.time()
    except Exception:
        pass

def ig_customer_name(igsid, account_id=None):
    """Имя клиента из кэша/БД. НЕ делает живых запросов — загрузка списка должна быть быстрой."""
    ig_load_names()
    return _ig_names.get(str(igsid), "")

def ig_save_name(igsid, name):
    if not name:
        return
    _ig_names[str(igsid)] = name
    try:
        _supa("POST", "ig_names", "?on_conflict=company_id,igsid",
              {"company_id": COMPANY_ID, "igsid": str(igsid), "name": name})
    except Exception:
        pass

def _ig_atts(r):
    """Вложения сообщения (фото/видео/посты) из сырого webhook."""
    raw = r.get("raw") or {}
    out = []
    for a in ((raw.get("message") or {}).get("attachments") or []):
        t = a.get("type", "")
        url = (a.get("payload") or {}).get("url") or ""
        if url:
            out.append({"type": t, "url": url})
    return out

_ATT_LABEL = {"image": "📷 фото", "video": "🎬 видео", "audio": "🎤 голосовое",
              "share": "🔗 пост", "ig_reel": "🎬 reels", "story_mention": "📖 история",
              "story_reply": "📖 ответ на историю", "file": "📎 файл"}

def _ig_disp_text(r):
    """Текст для списка диалогов: сам текст, иначе пометка о вложении."""
    t = r.get("text") or ""
    if t:
        return t
    atts = _ig_atts(r)
    return _ATT_LABEL.get(atts[0]["type"], "📎 вложение") if atts else ""

def _ig_pair(r):
    """(наш аккаунт, клиент) из строки ig_inbox независимо от направления."""
    if r.get("direction") == "out":
        return str(r.get("sender_id")), str(r.get("recipient_id"))
    return str(r.get("recipient_id")), str(r.get("sender_id"))

def ig_rows(limit=1000):
    if not supa_on():
        return []
    try:
        return _supa("GET", "ig_inbox",
                     "?company_id=eq.%s&select=*&order=ts.desc,id.desc&limit=%d" % (_q(COMPANY_ID), limit)) or []
    except Exception:
        return []

def _build_ig_conversations():
    amap = ig_account_map()
    rows = ig_rows()
    convos = {}
    for r in rows:                           # rows уже по убыванию времени — первый = последний
        acc, cust = _ig_pair(r)
        key = acc + "|" + cust
        if key not in convos:
            convos[key] = {"account_id": acc, "customer_id": cust,
                           "account": (amap.get(acc) or {}).get("username", acc),
                           "last_text": _ig_disp_text(r), "last_ts": int(r.get("ts") or 0),
                           "last_dir": r.get("direction", "in"), "count": 0}
        convos[key]["count"] += 1
    out = sorted(convos.values(), key=lambda c: c["last_ts"], reverse=True)
    for c in out:
        c["customer"] = ig_customer_name(c["customer_id"]) or c["customer_id"]
    return out

def ig_conversations():
    """Список диалогов (кэш 5с, имена — из БД, без живых запросов → быстро)."""
    return cached("ig_convos", 5, _build_ig_conversations)

def ig_thread(account_id, customer_id):
    """Сообщения одного диалога по возрастанию времени."""
    msgs = []
    for r in ig_rows():
        acc, cust = _ig_pair(r)
        if acc == str(account_id) and cust == str(customer_id):
            ts = int(r.get("ts") or 0)
            msgs.append({"t": ("out" if r.get("direction") == "out" else "in"),
                         "x": r.get("text", ""), "ts": ts, "att": _ig_atts(r),
                         "tm": time.strftime("%d.%m %H:%M", time.localtime(ts / 1000)) if ts > 1e12
                               else (time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "")})
    msgs.sort(key=lambda m: m["ts"])
    return {"msgs": msgs, "customer": ig_customer_name(customer_id, account_id) or customer_id}

def ig_reply(account_id, customer_id, text):
    """Ответить клиенту с нужного аккаунта и сохранить в историю."""
    ig_send(customer_id, text, account_id)
    try:
        _supa("POST", "ig_inbox", "",
              {"company_id": COMPANY_ID, "sender_id": str(account_id),
               "recipient_id": str(customer_id), "text": text,
               "ts": int(time.time() * 1000), "direction": "out", "raw": {}})
    except Exception:
        pass

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

def _warm_sales():
    try:
        if CFG.get("YAROS_URL"):
            s = cached("sales", 30, build_sales)
            _save_sales_daily(s)
    except Exception:
        pass

def _warmer():
    """Фоновый поток: держит каталог 1С, сводку amoCRM и продажи тёплыми, чтобы запросы
    отвечали мгновенно. Все три прогреваются ПАРАЛЛЕЛЬНО — на холодном старте это ~50с
    (самый долгий — каталог 20 МБ) вместо суммы ~60с при последовательной загрузке."""
    while True:
        jobs = []
        if CFG.get("YAROS_URL"):
            jobs.append(_refresh_catalog)
            jobs.append(_warm_sales)
        if CFG.get("AMO_TOKEN"):
            jobs.append(_refresh_overview)
        ths = [threading.Thread(target=j, daemon=True) for j in jobs]
        for t in ths: t.start()
        for t in ths: t.join()
        time.sleep(110)

# ====== Крупные категории товаров (по запросу владельца) ======
# 1С-категории «грязные» (бренды/этажи/группы), поэтому раскладываем товары
# по 14 понятным категориям эвристикой: по словам в названии группы + товара.
SUPER_CATS = ["Одежда", "Обувь", "Детские товары", "Аксессуары", "Бытовая техника",
              "Книги", "Личная гигиена и бытхимия", "Электроника", "Дом и сад",
              "Религиозные товары", "Канцтовары", "Автотовары", "Красота и уход",
              "Спорт и отдых"]

# порядок = приоритет (специфичные раньше общих)
_SCAT_RULES = [
    ("Религиозные товары", ["мусулман", "тасбих", "миск", "намаз", "коран", "четк", "чётк",
                            "жайнамаз", "хиджаб", "ислам", "религ", "сажда", "сурма"]),
    ("Автотовары", ["автотовар", "автомобиль", "для авто", "для машины", "моторное масло",
                    "омыватель", "авто ", "автомоб"]),
    ("Детские товары", ["игрушк", "кукла", "коляск", "памперс", "подгузник", "соск",
                        "пустышк", "погремушк", "детское питание", "манеж", "ходунк", "конструктор"]),
    ("Электроника", ["телефон", "смартфон", "наушник", "зарядк", "кабель", "повербанк",
                     "power bank", "флешк", "колонка", "ноутбук", "планшет", "мышк",
                     "клавиатур", "смарт-час", "смарт час", "адаптер", "роутер"]),
    ("Бытовая техника", ["бытовая техник", "утюг", "пылесос", "фен ", "чайник", "блендер",
                         "миксер", "мясорубк", "микроволнов", "плита", "холодильник",
                         "стиральн", "обогреват", "вентилятор", "тостер", "кофеварк",
                         "мультиварк", "электрочайник", "соковыжимал", "электробритв"]),
    ("Канцтовары", ["ручка", "тетрад", "карандаш", "канцтовар", "блокнот", "маркер", "клей",
                    "ножниц", "степлер", "папка", "дневник", "точилк", "ластик", "фломастер",
                    "альбом для рис"]),
    ("Книги", ["книга", "книги", "журнал", "учебник"]),
    ("Красота и уход", ["косметик", "помада", "тушь", "крем для лица", "маска для лица",
                        "лак для ногт", "духи", "парфюм", "макияж", "тени для век", "пудра",
                        "румяна", "для бровей", "ресниц", "маникюр", "сыворотк", "тональн",
                        "бальзам для губ"]),
    ("Личная гигиена и бытхимия", ["мыло", "гель для душа", "шампунь", "зубн", "паста",
                                   "прокладк", "влажные салф", "туалетная бумага", "порошок",
                                   "моющ", "чистящ", "отбеливат", "освежитель", "бытхими",
                                   "хоз товар", "хозтовар", "дезодорант", "кондиционер для бель",
                                   "средство для", "салфетк", "солфетк"]),
    ("Спорт и отдых", ["спорт", "мяч", "гантел", "скакалк", "велосипед", "палатк", "термос",
                       "бутылка для воды", "фитнес", "йога", "самокат", "ракетк", "эспандер"]),
    ("Обувь", ["обувь", "туфли", "кроссовк", "кеды", "ботин", "сапог", "тапочк", "сандал",
               "сланц", "балетк", "угги", "бут кийим", "босонож"]),
    ("Дом и сад", ["полотенц", "палотенц", "одеяло", "плед", "подушк", "простын", "постельн",
                   "скатерт", "фартук", "штор", "ковер", "ковёр", "посуд", "тарелк", "кастрюл",
                   "сковород", "кружк", "стакан", "горшок", "лейк", "банный набор", "комплект бель",
                   "покрывал", "наматрасник", "зеркал", "веник", "швабр", "корзин"]),
    ("Аксессуары", ["сумк", "кошелек", "кошелёк", "ремень", "очки", "зонт", "бижутер", "браслет",
                    "серьг", "серёж", "цепочк", "заколк", "резинк для волос", "часы", "перчатк",
                    "шарф", "ремешок", "бейсболк", "кепк", "рюкзак"]),
    ("Одежда", ["одежда", "футболк", "толстовк", "кофта", "трус", "бюстгалтер", "джинс", "штаны",
                "трико", "колготк", "лосин", "шапк", "носк", "пальто", "плащ", "безрукавк",
                "куртк", "топик", "пижам", "кольсон", "двойка", "костюм", "плать", "юбка",
                "рубашк", "свитер", "водолазк", "шорт", "нижнего бель", "нижнее бель", "брюки",
                "халат", "термобель", "белье", "бельё", "комбинезон", "сарафан", "блузк",
                "жилет", "манто", "комплект"]),
]

def super_category(text):
    t = (text or "").lower()
    for name, kws in _SCAT_RULES:
        for kw in kws:
            if kw in t:
                return name
    return "Прочее"

def _good_scat(x, cats):
    return super_category((cats.get(x.get("CATEGORY_ID"), "") or "") + " " + (x.get("TITLE", "") or ""))

def build_inventory():
    """Лёгкая сводка по складу для дашборда «Товары»."""
    goods = get_goods()
    cats = get_categories()
    total_units = 0.0
    retail_value = 0.0
    cost_value = 0.0
    in_stock = 0
    by_cat = {}
    by_scat = {}        # по 14 крупным категориям
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
        sc = _good_scat(x, cats)
        sa = by_scat.setdefault(sc, {"value": 0.0, "units": 0.0, "count": 0})
        sa["value"] += qty * price
        sa["units"] += qty
        sa["count"] += 1
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
    # 14 крупных категорий — в фиксированном порядке владельца, плюс «Прочее» в конце
    super_cats = []
    for name in SUPER_CATS + ["Прочее"]:
        v = by_scat.get(name)
        if v and v["count"] > 0:
            super_cats.append({"title": name, "value": round(v["value"]),
                               "units": int(v["units"]), "count": v["count"]})
    # «на исходе»: отдаём весь список (qty 1–3) с категорией — фильтрацию делает фронт
    low_out = sorted(low, key=lambda x: x["qty"])[:800]
    _inv_res = {
        "cats_units": cats_units,
        "cats_value": cats_value,
        "all_cats": all_cats,
        "super_cats": super_cats,
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
    kv_save("inventory", _inv_res)     # запоминаем последний удачный снимок склада
    return _inv_res

def get_inventory():
    """Сводка склада, устойчивая к сбоям 1С: при ошибке отдаёт последний снимок из базы."""
    try:
        return cached("inventory", 120, build_inventory)
    except Exception:
        d = kv_load("inventory")
        if d:
            d = dict(d); d["stale"] = True
            return d
        raise

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

def search_products(q, page, per=50, cat=None, in_stock=False, scat=None):
    goods = get_goods()
    cats = get_categories()
    q = (q or "").strip().lower()
    cat = (cat or "").strip().lower()
    scat = (scat or "").strip()
    items = goods
    if q:
        items = [x for x in items if q in (x.get("TITLE", "") or "").lower()]
    if cat:
        items = [x for x in items
                 if cat in (cats.get(x.get("CATEGORY_ID"), "") or "").lower()]
    if scat:                          # фильтр по крупной категории (одна из 14 + Прочее)
        items = [x for x in items if _good_scat(x, cats) == scat]
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


# ====== ПРОДАЖИ (чеки из 1С, /receipts/v2) ======
# Эндпоинт отдаёт чеки ТОЛЬКО за сегодня (параметры дат игнорирует). Историю по дням
# копим сами: периодически сохраняем дневной итог в Supabase (таблица sales_daily).
def _recv_ts(v):
    """Время чека → Unix-секунды. Сейчас 1С отдаёт число (сек), но в доках указан ISO 8601 —
    поддержим оба формата, чтобы не сломаться при возможной смене."""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v:
        s = v.strip().replace("Z", "+00:00")
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return int(time.mktime(time.strptime(s[:19], fmt[:19].replace("%z", ""))))
            except Exception:
                pass
    return 0

def build_sales(from_ts=None, to_ts=None):
    """Сводка продаж из чеков 1С. По умолчанию — за сегодня. Если переданы from_ts/to_ts
    (Unix-секунды) — за период (когда 1С включит поддержку параметров; см. док /receipts/v2).
    Возвраты идут с минусом (qty/сумма/прибыль), поэтому чистая выручка = простая сумма receiptTotal."""
    path = "/receipts/v2"
    if from_ts:
        path += "?from=%d" % int(from_ts) + (("&to=%d" % int(to_ts)) if to_ts else "")
    data = yaros_get(path)
    receipts = data.get("receipts", []) if isinstance(data, dict) else []
    sales_count = 0; gross = 0.0; net = 0.0; profit = 0.0
    returns_count = 0; returns_sum = 0.0
    by_pay = {}; by_seller = {}; by_product = {}
    rows = []
    for x in receipts:
        total = _num(x.get("receiptTotal"))
        is_return = x.get("operationType") == "Возврат" or total < 0
        net += total
        for it in x.get("items", []):
            profit += _num(it.get("profit"))
            nm = (it.get("name") or "").strip() or "—"
            p = by_product.setdefault(nm, {"qty": 0.0, "sum": 0.0, "profit": 0.0})
            p["qty"] += _num(it.get("qty"))
            p["sum"] += _num(it.get("saleAmount"))
            p["profit"] += _num(it.get("profit"))
        if is_return:
            returns_count += 1; returns_sum += -total
        else:
            sales_count += 1; gross += total
        pays = x.get("AllPaymentMethods") or [{"name": x.get("paymentMethod"), "summ": total}]
        for pm in pays:
            nm = (pm.get("name") or "—").strip() or "—"
            by_pay[nm] = by_pay.get(nm, 0.0) + _num(pm.get("summ"))
        s = (x.get("seller") or "—").strip() or "—"
        agg = by_seller.setdefault(s, {"count": 0, "sum": 0.0})
        agg["count"] += 1; agg["sum"] += total
        ts = _recv_ts(x.get("dateTime"))
        items_text = "; ".join("{} ×{}".format((it.get("name") or "").strip(),
                               int(_num(it.get("qty")))) for it in x.get("items", [])[:6])
        rows.append({"num": x.get("receiptNumber"),
                     "time": time.strftime("%H:%M", time.localtime(ts)) if ts else "",
                     "ts": ts, "total": round(total, 2), "pay": x.get("paymentMethod") or "—",
                     "seller": s, "type": "Возврат" if is_return else "Продажа",
                     "items": items_text})
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    pay_list = [{"name": k, "sum": round(v)} for k, v in sorted(by_pay.items(), key=lambda i: -i[1])]
    seller_list = [{"name": k, "count": v["count"], "sum": round(v["sum"])}
                   for k, v in sorted(by_seller.items(), key=lambda i: -i[1]["sum"])]
    top = [{"name": k, "qty": int(v["qty"]), "sum": round(v["sum"]), "profit": round(v["profit"])}
           for k, v in sorted(by_product.items(), key=lambda i: -i[1]["sum"])][:20]
    return {
        "date": _today_str(),
        "sales_count": sales_count, "net_sales": round(net), "gross_sales": round(gross),
        "profit": round(profit), "avg_check": round(gross / sales_count) if sales_count else 0,
        "returns_count": returns_count, "returns_sum": round(returns_sum),
        "by_pay": pay_list, "by_seller": seller_list, "top_products": top,
        "receipts": rows[:80], "total_receipts": len(receipts),
        "updated": time.strftime("%H:%M:%S"),
    }

def _save_sales_daily(s):
    """Сохранить день в Supabase: итог (для графика) + полную детализацию `detail`
    (кассы, оплаты, топ товаров, чеки) — чтобы можно было открыть любой прошлый день целиком."""
    if not supa_on() or not s:
        return
    try:
        rec = {"company_id": COMPANY_ID, "date": s.get("date"),
               "receipts": s.get("sales_count", 0), "sales": s.get("net_sales", 0),
               "profit": s.get("profit", 0), "returns_count": s.get("returns_count", 0),
               "returns_sum": s.get("returns_sum", 0), "detail": s}
        _supa("POST", "sales_daily", "?on_conflict=company_id,date", rec)
    except Exception:
        try:   # вдруг колонка detail ещё не добавлена — сохраним хотя бы итог
            rec.pop("detail", None)
            _supa("POST", "sales_daily", "?on_conflict=company_id,date", rec)
        except Exception:
            pass

_sales_last = {"data": None}   # последний удачный снимок продаж за сегодня (на случай сбоя 1С)

def sales_day(date):
    """Сводка продаж за конкретный день. Сегодня — живьём из 1С; прошлые дни — из нашей
    базы (Supabase), куда мы сохраняем полную детализацию каждый день.
    Если 1С временно недоступна (бывает «Истекло время ожидания сеанса», HTTP 406) —
    отдаём последний сохранённый снимок за сегодня, помечая stale=True, а не ошибку."""
    if not date or date == _today_str():
        try:
            s = cached("sales", 30, build_sales)
            _sales_last["data"] = s
            return s
        except Exception:
            if _sales_last.get("data"):
                d = dict(_sales_last["data"]); d["stale"] = True; return d
            if supa_on():
                try:
                    # самый свежий сохранённый день (сегодняшний, если есть; иначе последний доступный)
                    rows = _supa("GET", "sales_daily",
                                 "?company_id=eq.%s&order=date.desc&limit=1&select=date,detail"
                                 % _q(COMPANY_ID))
                    if rows and rows[0].get("detail"):
                        d = dict(rows[0]["detail"]); d["stale"] = True
                        d["stale_date"] = rows[0].get("date")
                        return d
                except Exception:
                    pass
            raise
    if not supa_on():
        return {"error": "База данных не настроена", "receipts": [], "date": date}
    try:
        rows = _supa("GET", "sales_daily",
                     "?company_id=eq.%s&date=eq.%s&select=detail" % (_q(COMPANY_ID), _q(date)))
        if rows and rows[0].get("detail"):
            d = dict(rows[0]["detail"]); d["from_db"] = True; d["date"] = date
            return d
        return {"empty": True, "date": date, "receipts": [],
                "error": "За этот день у нас ещё нет сохранённых данных."}
    except Exception as e:
        return {"error": str(e), "receipts": [], "date": date}

def sales_history(days=14):
    """История продаж по дням из Supabase (последние N дней, по возрастанию даты)."""
    if not supa_on():
        return {"days": []}
    try:
        rows = _supa("GET", "sales_daily",
                     "?company_id=eq.%s&select=*&order=date.desc&limit=%d" % (_q(COMPANY_ID), int(days)))
        rows = sorted(rows or [], key=lambda r: r.get("date") or "")
        out = [{"date": r.get("date"), "sales": round(_num(r.get("sales"))),
                "profit": round(_num(r.get("profit"))), "receipts": int(_num(r.get("receipts"))),
                "returns_sum": round(_num(r.get("returns_sum")))} for r in rows]
        return {"days": out}
    except Exception as e:
        return {"days": [], "error": str(e)}


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
from urllib.parse import quote as _q, unquote as _unq, parse_qs as _parse_qs

COMPANY_ID = "bizmart"   # пока одна компания; мульти-тенант — позже (поле company_id уже заложено)

def supa_on():
    return bool(CFG.get("SUPABASE_URL") and CFG.get("SUPABASE_KEY"))

def kv_save(key, value):
    """Сохранить последний удачный снимок (склад/продажи) в базу — чтобы пережить и
    сбой 1С, и перезапуск сервера."""
    if not supa_on():
        return
    try:
        _supa("POST", "kv_cache", "?on_conflict=k", {"k": key, "v": value})
    except Exception:
        pass

def kv_load(key):
    if not supa_on():
        return None
    try:
        rows = _supa("GET", "kv_cache", "?k=eq.%s&select=v" % _q(key))
        return rows[0]["v"] if rows else None
    except Exception:
        return None

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

def _supa_upload(raw, content_type, name, folder="receipts"):
    """Загрузить файл (байты) в Supabase Storage (бакет receipts) и вернуть публичный URL.
    Путь: {company_id}/{unix}_{безопасное_имя}. Возвращает '' при неудаче."""
    base = CFG.get("SUPABASE_URL", "").rstrip("/")
    key = CFG.get("SUPABASE_KEY", "")
    if not base or not key:
        return ""
    safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "file"))[-60:] or "file"
    path = "%s/%s_%s" % (COMPANY_ID, int(time.time()), safe)
    url = base + "/storage/v1/object/" + folder + "/" + _q(path)
    req = urllib.request.Request(url, data=raw, method="POST", headers={
        "apikey": key, "Authorization": "Bearer " + key,
        "Content-Type": content_type or "application/octet-stream",
        "x-upsert": "true", "User-Agent": "Shturval/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    return base + "/storage/v1/object/public/" + folder + "/" + _q(path)

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
    for v in all_users():
        if v.get("name") == name:
            return v
    return None

def reload_users():
    """Перечитать пользователей (env + таблица employees) в память."""
    global USERS, USERS_BY_LOGIN
    USERS, USERS_BY_LOGIN = load_users()

_TRANSLIT = {
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z",
    "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
    "с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch",
    "ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"}

def _slug_login(name):
    """Логин из имени: транслит, латиница/цифры, нижний регистр."""
    s = (name or "").strip().lower()
    out = "".join(_TRANSLIT.get(ch, ch) for ch in s)
    out = _re.sub(r"[^a-z0-9]+", "", out)
    return out or "user"

def _unique_login(base):
    base = base or "user"
    cand = base; i = 1
    while cand.lower() in USERS_BY_LOGIN:
        i += 1; cand = "%s%d" % (base, i)
    return cand

def _emp_row(login, **f):
    """Полная строка для таблицы employees (берём текущие поля юзера + изменения)."""
    cur = USERS_BY_LOGIN.get(login.lower()) or {}
    secs = f.get("sections", cur.get("sections") or ["myday"])
    if isinstance(secs, (list, tuple)):
        secs = ",".join(secs)
    return {"company_id": COMPANY_ID, "login": login,
            "pw": f.get("pw", cur.get("pw", "") or ""),
            "name": f.get("name", cur.get("name", "")),
            "role": f.get("role", cur.get("role", "") or ""),
            "department": f.get("department", cur.get("department", "") or ""),
            "sections": secs or "myday",
            "salary_month": f.get("salary_month", cur.get("salary_month", 0) or 0),
            "daily_rate": f.get("daily_rate", cur.get("daily_rate", 0) or 0),
            "bonus_month": f.get("bonus_month", cur.get("bonus_month", 0) or 0),
            "plan_day": f.get("plan_day", cur.get("plan_day", 0) or 0),
            "active": f.get("active", True)}

def team_save(row):
    """Записать/обновить сотрудника в Supabase и перечитать в память."""
    _supa("POST", "employees", "?on_conflict=company_id,login", row)
    reload_users()

def _present_in_month(days, m):
    return sum(1 for d, st in (days or {}).items() if isinstance(d, str) and d[:7] == m and st == "p")

SHIFT_HOURS = 10.5                          # часов в смене (цена часа = ставка/день ÷ это)

def payroll_view(user, rec=None):
    name = user.get("name", "")
    key = _pkey(user)                       # зарплата ведётся по логину (уникален)
    r = rec if rec is not None else payroll_rec(key)
    days = r.get("days") or {}
    hrs = r.get("hours") or {}
    m = _cur_month(); today = _today_str()
    sal = float(user.get("salary_month") or 0)
    rate = float(user.get("daily_rate") or 0)
    if rate <= 0 and sal > 0:
        rate = round(sal / 30.0)            # оклад ÷ 30 (магазин работает каждый день)
    hr_precise = (rate / SHIFT_HOURS) if rate > 0 else 0.0
    hourly_rate = round(hr_precise)         # цена часа (для показа)
    # По каждому дню: если есть часы — день частичный, считаем по часам;
    # иначе если отмечен «пришёл» — полный день по ставке. («пришёл» + часы → по часам.)
    dates = set([d for d in days.keys() if isinstance(d, str) and d[:7] == m] +
                [d for d in hrs.keys() if isinstance(d, str) and d[:7] == m])
    present_days = 0; partial_days = 0; hours_month = 0.0; accrued = 0.0
    for d in dates:
        h = float(hrs.get(d) or 0)
        if h > 0:
            accrued += h * hr_precise; hours_month += h; partial_days += 1
        elif days.get(d) == "p":
            accrued += rate; present_days += 1
    accrued = round(accrued); hours_month = round(hours_month, 1)
    bonus = float(r.get("bonus") or 0); adv = float(r.get("advance") or 0)
    return {"name": name, "login": user.get("login", ""),
            "role": user.get("role", ""), "department": user.get("department", ""),
            "salary_month": sal, "daily_rate": rate, "bonus_month": float(user.get("bonus_month") or 0),
            "present_days": present_days, "partial_days": partial_days,
            "accrued": accrued, "bonus": bonus,
            "hourly_rate": hourly_rate, "shift_hours": SHIFT_HOURS,
            "hours_month": hours_month, "hours_today": float(hrs.get(today) or 0),
            "hours": hrs,
            "advance": adv, "to_receive": round(accrued + bonus - adv), "days": days,
            "marked_today": days.get(today) == "p", "marked_absent_today": days.get(today) == "a"}

def payroll_all():
    # ВСЕ записи зарплаты за месяц — ОДНИМ запросом (а не по запросу на каждого: было N+1, ~13с)
    recs = None
    if supa_on():
        recs = {}
        try:
            m = _cur_month()
            rows = _supa("GET", "payroll",
                         "?company_id=eq.%s&month=eq.%s&select=*" % (_q(COMPANY_ID), _q(m)))
            for row in (rows or []):
                recs[row.get("name")] = row
        except Exception:
            recs = None                     # не вышло — откат на поштучный режим
    seen = set(); out = []
    for v in all_users():
        if "all" in v.get("sections", []):  # владельца не показываем
            continue
        key = _pkey(v)
        if not key or key in seen:
            continue
        seen.add(key)
        if recs is not None:
            out.append(payroll_view(v, recs.get(key) or {}))   # без обращения к базе
        else:
            out.append(payroll_view(v))
    return out

# --- Планы по отделам (продажи/день) ---
DEPTPLAN = {}                # резерв в памяти/файле
DEFAULT_DEPT_PLAN = 250000   # план по умолчанию на отдел в день

def _dept_list():
    out = []
    for v in all_users():
        if "all" in v.get("sections", []):
            continue
        d = (v.get("department") or "").strip()
        if d and d not in out:
            out.append(d)
    return out

def _dept_count(dep):
    return sum(1 for v in all_users()
               if (v.get("department") or "").strip() == dep and "all" not in v.get("sections", []))

def dept_plan_rec(dep):
    if supa_on():
        try:
            rows = _supa("GET", "dept_plan",
                         "?company_id=eq.%s&department=eq.%s&select=*" % (_q(COMPANY_ID), _q(dep)))
            if rows:
                return rows[0]
            rec = {"company_id": COMPANY_ID, "department": dep,
                   "plan_day": DEFAULT_DEPT_PLAN, "facts": {}}
            _supa("POST", "dept_plan", "", rec)
            return rec
        except Exception:
            pass
    r = DEPTPLAN.get(dep)
    if not r:
        r = {"department": dep, "plan_day": DEFAULT_DEPT_PLAN, "facts": {}}
        DEPTPLAN[dep] = r
    return r

def dept_plan_upsert(rec):
    if supa_on():
        try:
            r = dict(rec); r.setdefault("company_id", COMPANY_ID); r.pop("updated_at", None)
            _supa("POST", "dept_plan", "?on_conflict=company_id,department", r)
            return
        except Exception:
            pass
    DEPTPLAN[rec.get("department")] = rec

def dept_plan_all():
    today = _today_str(); out = []
    recs = None
    if supa_on():
        recs = {}
        try:
            rows = _supa("GET", "dept_plan", "?company_id=eq.%s&select=*" % _q(COMPANY_ID))
            for row in (rows or []):
                recs[row.get("department")] = row
        except Exception:
            recs = None
    for dep in _dept_list():
        if dep == "Администраторы":      # админы не продают — плана нет
            continue
        if recs is not None:
            r = recs.get(dep) or {"plan_day": DEFAULT_DEPT_PLAN, "facts": {}}
        else:
            r = dept_plan_rec(dep)
        plan = float(r.get("plan_day") or 0)
        facts = r.get("facts") or {}
        fact = float(facts.get(today) or 0)
        pct = round(fact / plan * 100) if plan > 0 else 0
        out.append({"department": dep, "plan_day": plan, "fact": fact,
                    "pct": pct, "staff": _dept_count(dep)})
    return out


# ====== ПОСТАВЩИКИ (долги) и РАСХОДЫ ======
def _days_ago(n):
    t = time.localtime(time.time() - n * 86400)
    return "%04d-%02d-%02d" % (t.tm_year, t.tm_mon, t.tm_mday)

def suppliers_list():
    """Поставщики с подсчётом текущего долга: долг = (взял в долг) − (отдал деньги)."""
    sups, txns = [], []
    if supa_on():
        try:
            sups = _supa("GET", "suppliers", "?company_id=eq.%s&select=*&order=name" % _q(COMPANY_ID))
            txns = _supa("GET", "supplier_txns",
                         "?company_id=eq.%s&select=*&order=date.desc,id.desc" % _q(COMPANY_ID))
        except Exception:
            pass
    bs = {}
    for t in txns:
        bs.setdefault(t.get("supplier_id"), []).append(t)
    out = []
    for s in sups:
        ts = bs.get(s.get("id"), [])
        taken = sum(_num(t.get("amount")) for t in ts if t.get("type") == "debt")
        paid = sum(_num(t.get("amount")) for t in ts if t.get("type") == "payment")
        out.append({"id": s.get("id"), "name": s.get("name", ""), "note": s.get("note", ""),
                    "debt": round(taken - paid), "total_taken": round(taken),
                    "total_paid": round(paid), "txns": ts[:60]})
    return {"suppliers": out, "total_debt": round(sum(s["debt"] for s in out)), "count": len(out)}

def sales_sums():
    """Выручка и прибыль по периодам (день/неделя/месяц): сохранённые дни + сегодня живьём."""
    today = _today_str()
    days = {}
    if supa_on():
        try:
            rows = _supa("GET", "sales_daily",
                         "?company_id=eq.%s&select=date,sales,profit" % _q(COMPANY_ID))
            for r in rows:
                days[r.get("date")] = {"sales": _num(r.get("sales")), "profit": _num(r.get("profit"))}
        except Exception:
            pass
    try:
        s = cached("sales", 30, build_sales)
        if not s.get("stale"):
            days[today] = {"sales": _num(s.get("net_sales")), "profit": _num(s.get("profit"))}
    except Exception:
        pass
    wk, mo = _days_ago(6), today[:7]
    def agg(pred):
        return (sum(v["sales"] for d, v in days.items() if pred(d)),
                sum(v["profit"] for d, v in days.items() if pred(d)))
    res = {}
    for key, pred in (("today", lambda d: d == today),
                      ("week", lambda d: d >= wk),
                      ("month", lambda d: (d or "")[:7] == mo)):
        sa, pr = agg(pred)
        res[key] = {"sales": round(sa), "profit": round(pr)}
    return res

def expenses_view():
    """Расходы: список, суммы по периодам, по категориям и ЧИСТАЯ ПРИБЫЛЬ = прибыль − расходы."""
    rows = []
    if supa_on():
        try:
            rows = _supa("GET", "expenses",
                         "?company_id=eq.%s&select=*&order=date.desc,id.desc" % _q(COMPANY_ID))
        except Exception:
            rows = []
    today = _today_str(); wk = _days_ago(6); mo = today[:7]
    def esum(pred):
        return round(sum(_num(r.get("amount")) for r in rows if pred(r.get("date", ""))))
    e = {"today": esum(lambda d: d == today),
         "week": esum(lambda d: d >= wk),
         "month": esum(lambda d: (d or "")[:7] == mo)}
    cat = {}
    for r in rows:
        if (r.get("date", "") or "")[:7] == mo:
            c = (r.get("category") or "Без категории")
            cat[c] = cat.get(c, 0) + _num(r.get("amount"))
    by_cat = [{"category": k, "amount": round(v)} for k, v in sorted(cat.items(), key=lambda i: -i[1])]
    ss = sales_sums()
    periods = {}
    for p in ("today", "week", "month"):
        periods[p] = {"sales": ss[p]["sales"], "profit": ss[p]["profit"],
                      "expense": e[p], "net": round(ss[p]["profit"] - e[p])}
    return {"expenses": rows[:200], "by_category": by_cat, "periods": periods, "total_count": len(rows)}


# ====== РЫНОК: цены товаров на маркетплейсах (Wildberries) ======
def rub_to_kgs():
    """Курс рубль→сом (для перевода цен WB). Кэш 6 часов, запасной ~1.2."""
    def fetch():
        try:
            req = urllib.request.Request("https://open.er-api.com/v6/latest/RUB",
                                         headers={"User-Agent": "Shturval/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode("utf-8"))
            v = float(d.get("rates", {}).get("KGS") or 0)
            return v if v > 0 else 1.2
        except Exception:
            return 1.2
    return cached("rub_kgs", 21600, fetch)

def market_search(q):
    """Поиск цен товара на Wildberries (открытый поиск, без ключей). Цены WB в рублях
    переводим в сом. Результат кэшируем на 5 мин на запрос (бережём лимит WB)."""
    q = (q or "").strip()
    if not q:
        return {"items": [], "query": "", "source": "Wildberries"}
    def fetch():
        url = ("https://search.wb.ru/exactmatch/ru/common/v5/search"
               "?appType=1&curr=rub&dest=-1257786&resultset=catalog&sort=popular&spp=30&query="
               + _q(q))
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Shturval/1.0",
            "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    try:
        data = cached("mkt:" + q.lower(), 300, fetch)
    except urllib.error.HTTPError as e:
        return {"items": [], "query": q, "source": "Wildberries",
                "error": "WB ответил HTTP %s — попробуй чуть позже" % e.code}
    except Exception as e:
        return {"items": [], "query": q, "source": "Wildberries", "error": str(e)}
    products = (data or {}).get("products", []) or []
    rate = rub_to_kgs()
    items = []
    for x in products[:40]:
        sizes = x.get("sizes", []) or []
        pr = 0
        if sizes:
            pr = (sizes[0].get("price") or {}).get("product") or (sizes[0].get("price") or {}).get("basic") or 0
        rub = (pr or 0) / 100.0
        items.append({"id": x.get("id"), "name": x.get("name", ""), "brand": x.get("brand", ""),
                      "price_rub": round(rub), "price_kgs": round(rub * rate),
                      "rating": x.get("reviewRating") or x.get("rating"),
                      "feedbacks": x.get("feedbacks") or 0,
                      "supplier": x.get("supplier", ""),
                      "link": "https://www.wildberries.ru/catalog/%s/detail.aspx" % x.get("id")})
    # дешёвые/дорогие для ориентира
    prices = [i["price_kgs"] for i in items if i["price_kgs"] > 0]
    return {"items": items, "query": q, "source": "Wildberries", "rate": round(rate, 3),
            "count": len(items),
            "min_kgs": min(prices) if prices else 0, "max_kgs": max(prices) if prices else 0,
            "avg_kgs": round(sum(prices) / len(prices)) if prices else 0}


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
        if not USERS and not USERS_BY_LOGIN:
            return {"name": "Гость", "sections": ["all"]}
        pw = (self.headers.get("X-Auth") or "").strip()
        login = (self.headers.get("X-Login") or "").strip().lower()
        if login:                                  # вход логин+пароль
            u = USERS_BY_LOGIN.get(login)
            return u if (u and pw and u.get("pw") == pw) else None
        return USERS.get(pw)                        # обратная совместимость: только пароль

    def _authed(self):
        return self._user() is not None

    def do_POST(self):
        # Instagram webhook — ПУБЛИЧНЫЙ (Meta шлёт события без нашего логина). Отвечаем быстро 200.
        if self.path.startswith("/api/ig/webhook"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                evt = json.loads(raw or b"{}")
            except Exception:
                evt = {}
            try:
                ig_store_event(evt)
            except Exception:
                pass
            self.send_response(200); self.send_header("Content-Type", "text/plain")
            self.end_headers(); self.wfile.write(b"EVENT_RECEIVED"); return
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
        if self.path.startswith("/api/ig/reply"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            acc = str(body.get("account_id") or ""); cust = str(body.get("customer_id") or "")
            text = (body.get("text") or "").strip()
            if not acc or not cust or not text:
                return self._send(400, {"error": "нужны account_id, customer_id и text"})
            try:
                ig_reply(acc, cust, text)
            except Exception as e:
                return self._send(200, {"ok": False, "error": "Instagram: " + str(e)})
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
            action = body.get("action")
            login = (body.get("login") or "").strip().lower()
            name = (body.get("name") or "").strip()
            tu = USERS_BY_LOGIN.get(login) if login else None
            if not tu:
                tu = user_by_name(name)
            if not tu:
                return self._send(400, {"error": "сотрудник не найден"})
            r = payroll_rec(_pkey(tu))
            days = r.get("days") or {}
            hrs = r.get("hours") or {}
            today = _today_str()
            # дата отметки: можно отметить любой день месяца (для календаря); по умолчанию сегодня
            day = (body.get("date") or "").strip()
            if not (len(day) == 10 and day[4] == "-" and day[7] == "-"):
                day = today
            amt = float(body.get("amount") or 0)
            if action == "present":
                days[day] = "p"
            elif action == "absent":           # «не пришёл»
                days[day] = "a"
            elif action == "unpresent":        # снять отметку за день
                days.pop(day, None)
            elif action == "advance":          # ДОБАВИТЬ к авансу
                r["advance"] = round(float(r.get("advance") or 0) + amt)
            elif action == "bonus":            # ДОБАВИТЬ к премии
                r["bonus"] = round(float(r.get("bonus") or 0) + amt)
            elif action == "set_advance":      # ЗАДАТЬ аванс (редактирование/исправление)
                r["advance"] = round(amt)
            elif action == "set_bonus":        # ЗАДАТЬ премию (редактирование/исправление)
                r["bonus"] = round(amt)
            elif action == "set_hours":        # ЗАДАТЬ часы за день (почасовая оплата)
                h = float(body.get("hours") or 0)
                if h <= 0:
                    hrs.pop(day, None)         # 0 часов = снять отметку
                else:
                    hrs[day] = round(h, 2)
                r["hours"] = hrs
            else:
                return self._send(400, {"error": "неизвестное действие"})
            r["days"] = days
            r["hours"] = hrs
            r["present_days"] = _present_in_month(days, _cur_month())
            r["last_present"] = today if days.get(today) == "p" else r.get("last_present", "")
            payroll_upsert(r)
            return self._send(200, {"ok": True, "view": payroll_view(tu, r)})
        if self.path.startswith("/api/deptplan"):
            u = self._user(); secs = u.get("sections", []) if u else []
            if not (u and ("all" in secs or "hr" in secs)):   # планы отделов — владелец и HR/Кадры
                return self._send(403, {"error": "Менять план может только владелец или HR"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            dep = (body.get("department") or "").strip(); action = body.get("action")
            if not dep:
                return self._send(400, {"error": "нет отдела"})
            r = dept_plan_rec(dep)
            if action == "setplan":
                r["plan_day"] = round(float(body.get("plan_day") or 0))
            elif action == "setfact":
                facts = r.get("facts") or {}
                facts[_today_str()] = round(float(body.get("fact") or 0))
                r["facts"] = facts
            else:
                return self._send(400, {"error": "неизвестное действие"})
            dept_plan_upsert(r)
            return self._send(200, {"ok": True, "departments": dept_plan_all()})
        if self.path.startswith("/api/team"):
            u = self._user(); secs = u.get("sections", []) if u else []
            if not (u and ("all" in secs or "hr" in secs)):   # команду ведёт владелец/HR
                return self._send(403, {"error": "Управлять командой может только владелец или HR"})
            if not supa_on():
                return self._send(503, {"error": "База не настроена — добавление недоступно"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            action = body.get("action")
            try:
                if action == "add":
                    name = (body.get("name") or "").strip()
                    if not name:
                        return self._send(400, {"error": "Впишите имя сотрудника"})
                    login = (body.get("login") or "").strip() or _unique_login(_slug_login(name))
                    if login.lower() in USERS_BY_LOGIN:
                        return self._send(400, {"error": "Логин «%s» уже занят — укажите другой" % login})
                    secs_in = body.get("sections")
                    if not secs_in:
                        secs_in = ["myday"]
                    row = _emp_row(login, name=name,
                                   role=(body.get("role") or "").strip(),
                                   department=(body.get("department") or "").strip(),
                                   pw=(body.get("pw") or "").strip(),
                                   sections=secs_in,
                                   salary_month=round(float(body.get("salary_month") or 0)))
                    team_save(row)
                    return self._send(200, {"ok": True, "login": login})
                # для остальных действий нужен существующий сотрудник
                login = (body.get("login") or "").strip()
                tu = USERS_BY_LOGIN.get(login.lower()) if login else None
                if not tu and (body.get("name") or "").strip():
                    tu = user_by_name((body.get("name") or "").strip())
                    login = tu.get("login", "") if tu else ""
                if not tu or not login:
                    return self._send(400, {"error": "Сотрудник не найден"})
                if "all" in tu.get("sections", []):
                    return self._send(400, {"error": "Владельца нельзя менять отсюда"})
                if action == "set_salary":
                    team_save(_emp_row(login,
                                       salary_month=round(float(body.get("salary_month") or 0))))
                    return self._send(200, {"ok": True})
                elif action == "update":
                    over = {}
                    for k in ("name", "role", "department"):
                        if k in body:
                            over[k] = (body.get(k) or "").strip()
                    if "salary_month" in body:
                        over["salary_month"] = round(float(body.get("salary_month") or 0))
                    if "sections" in body and body.get("sections"):
                        over["sections"] = body.get("sections")
                    if "pw" in body and (body.get("pw") or "").strip():
                        over["pw"] = (body.get("pw") or "").strip()
                    team_save(_emp_row(login, **over))
                    return self._send(200, {"ok": True})
                elif action == "remove":
                    team_save(_emp_row(login, active=False))
                    return self._send(200, {"ok": True})
                else:
                    return self._send(400, {"error": "неизвестное действие"})
            except Exception as e:
                return self._send(500, {"error": "Не удалось сохранить: %s" % e})
        if self.path.startswith("/api/suppliers"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            action = body.get("action")
            who = (self._user() or {}).get("name", "")   # кто вносит изменение
            try:
                if action == "add_supplier":
                    name = (body.get("name") or "").strip()
                    if not name:
                        return self._send(400, {"error": "нужно название поставщика"})
                    _supa("POST", "suppliers", "",
                          {"company_id": COMPANY_ID, "name": name,
                           "note": (body.get("note") or "").strip(), "created_by": who})
                elif action == "add_txn":
                    sid = body.get("supplier_id")
                    typ = body.get("type")
                    if not sid or typ not in ("debt", "payment"):
                        return self._send(400, {"error": "нужны поставщик и тип операции"})
                    day = (body.get("date") or "").strip()
                    if not (len(day) == 10 and day[4] == "-"):
                        day = _today_str()
                    # прикреплённый чек (фото/файл) — грузим в Storage, сохраняем ссылку
                    receipt_url = ""
                    rb64 = body.get("receipt_b64") or ""
                    if rb64:
                        try:
                            if "," in rb64 and rb64[:5] == "data:":
                                rb64 = rb64.split(",", 1)[1]
                            raw = base64.b64decode(rb64)
                            if len(raw) > 10 * 1024 * 1024:
                                return self._send(200, {"ok": False, "error": "Файл больше 10 МБ — сожмите или выберите меньше"})
                            receipt_url = _supa_upload(
                                raw, body.get("receipt_type") or "application/octet-stream",
                                body.get("receipt_name") or "cheque")
                        except Exception as ue:
                            return self._send(200, {"ok": False, "error": "Не удалось загрузить чек: " + str(ue)})
                    _supa("POST", "supplier_txns", "",
                          {"company_id": COMPANY_ID, "supplier_id": sid, "type": typ,
                           "amount": round(_num(body.get("amount"))), "qty": _num(body.get("qty")),
                           "note": (body.get("note") or "").strip(), "date": day,
                           "receipt_url": receipt_url, "created_by": who})
                elif action in ("del_txn", "del_supplier"):
                    # удаление истории поставщиков отключено по решению владельца —
                    # записи защищены, удалять нельзя ни через интерфейс, ни напрямую
                    return self._send(403, {"error": "Удаление истории отключено — записи защищены"})
                else:
                    return self._send(400, {"error": "неизвестное действие"})
                return self._send(200, {"ok": True, "data": suppliers_list()})
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        if self.path.startswith("/api/expenses"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            action = body.get("action")
            try:
                if action == "add":
                    amt = round(_num(body.get("amount")))
                    if amt <= 0:
                        return self._send(400, {"error": "нужна сумма расхода"})
                    day = (body.get("date") or "").strip()
                    if not (len(day) == 10 and day[4] == "-"):
                        day = _today_str()
                    _supa("POST", "expenses", "",
                          {"company_id": COMPANY_ID, "amount": amt,
                           "category": (body.get("category") or "").strip(),
                           "note": (body.get("note") or "").strip(), "date": day})
                elif action == "del":
                    _supa("DELETE", "expenses",
                          "?company_id=eq.%s&id=eq.%s" % (_q(COMPANY_ID), _q(str(body.get("id")))))
                else:
                    return self._send(400, {"error": "неизвестное действие"})
                return self._send(200, {"ok": True, "data": expenses_view()})
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        self._send(404, {"error": "не найдено"})

    def do_GET(self):
        if self.path.startswith("/api/health"):
            ok = bool(CFG.get("AMO_TOKEN")) and bool(CFG.get("AMO_SUBDOMAIN"))
            return self._send(200, {"ok": ok, "account": CFG.get("AMO_SUBDOMAIN")})
        # Instagram webhook — ПУБЛИЧНЫЙ (Meta проверяет адрес). Должно отвечать сырым challenge.
        if self.path.startswith("/api/ig/webhook"):
            p = _parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            mode = (p.get("hub.mode") or [""])[0]
            token = (p.get("hub.verify_token") or [""])[0]
            challenge = (p.get("hub.challenge") or [""])[0]
            if mode == "subscribe" and token == CFG.get("IG_VERIFY_TOKEN", ""):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain"); self.end_headers()
                self.wfile.write(challenge.encode("utf-8")); return
            self.send_response(403); self.end_headers(); return
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
        if self.path.startswith("/api/deptplan"):
            u = self._user(); secs = u.get("sections", []) if u else []
            if not (u and ("all" in secs or "hr" in secs)):   # планы отделов — владелец и HR
                return self._send(403, {"error": "Только владелец или HR"})
            return self._send(200, {"departments": dept_plan_all()})
        if self.path.startswith("/api/staff"):
            u = self._user()
            if not (u and "all" in u.get("sections", [])):
                return self._send(403, {"error": "Только владелец видит персонал"})
            staff = [{"name": v.get("name", ""), "login": v.get("login", ""),
                      "sections": v.get("sections", []),
                      "role": v.get("role", ""), "department": v.get("department", ""),
                      "plan_day": v.get("plan_day", 0)}
                     for v in all_users() if "all" not in v.get("sections", [])]
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
                return self._send(200, get_inventory())
            except Exception as e:
                return self._send(200, {"error": "Нет связи с 1С: " + str(e)})
        if self.path.startswith("/api/sales-history"):
            from urllib.parse import urlparse, parse_qs
            try:
                days = int(parse_qs(urlparse(self.path).query).get("days", ["14"])[0])
            except ValueError:
                days = 14
            return self._send(200, sales_history(max(1, min(days, 90))))
        if self.path.startswith("/api/sales"):
            from urllib.parse import urlparse, parse_qs
            date = (parse_qs(urlparse(self.path).query).get("date", [""])[0] or "").strip()
            try:
                return self._send(200, sales_day(date))
            except Exception as e:
                return self._send(200, {"error": "Нет связи с 1С: " + str(e), "receipts": []})
        if self.path.startswith("/api/market"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            try:
                return self._send(200, market_search(q))
            except Exception as e:
                return self._send(200, {"items": [], "error": str(e)})
        if self.path.startswith("/api/suppliers"):
            try:
                return self._send(200, suppliers_list())
            except Exception as e:
                return self._send(200, {"suppliers": [], "total_debt": 0, "error": str(e)})
        if self.path.startswith("/api/expenses"):
            try:
                return self._send(200, expenses_view())
            except Exception as e:
                return self._send(200, {"expenses": [], "error": str(e)})
        if self.path.startswith("/api/products"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            q = qs.get("q", [""])[0]
            cat = qs.get("cat", [""])[0]
            scat = qs.get("scat", [""])[0]
            in_stock = qs.get("in_stock", ["0"])[0] in ("1", "true", "yes")
            try:
                page = int(qs.get("page", ["1"])[0])
            except ValueError:
                page = 1
            try:
                return self._send(200, search_products(q, max(1, page), cat=cat, in_stock=in_stock, scat=scat))
            except Exception as e:
                return self._send(200, {"error": "Нет связи с 1С: " + str(e), "items": [], "total": 0})
        if self.path.startswith("/api/ig/conversations"):
            try:
                return self._send(200, {"conversations": ig_conversations(),
                                        "accounts": [{"username": a.get("username"), "ig_id": a.get("ig_id")}
                                                     for a in ig_accounts()]})
            except Exception as e:
                return self._send(200, {"conversations": [], "error": str(e)})
        if self.path.startswith("/api/ig/thread"):
            p = _parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            acc = (p.get("account") or [""])[0]; cust = (p.get("customer") or [""])[0]
            if not acc or not cust:
                return self._send(400, {"error": "нужны account и customer"})
            try:
                return self._send(200, ig_thread(acc, cust))
            except Exception as e:
                return self._send(200, {"msgs": [], "error": str(e)})
        if self.path.startswith("/api/ig/accounts"):
            return self._send(200, {"accounts": [{"username": a.get("username"), "ig_id": a.get("ig_id")}
                                                 for a in ig_accounts()]})
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
    reload_users()   # подхватить сотрудников из базы (таблица employees)
    # фоновый прогрев каталога 1С и сводки amoCRM (чтобы запросы отвечали мгновенно)
    if CFG.get("YAROS_URL") or CFG.get("AMO_TOKEN"):
        threading.Thread(target=_warmer, daemon=True).start()
    # фоновый анализ чатов → авто-раскладка по воронке
    if CFG.get("CHATPLACE_KEY"):
        threading.Thread(target=_chat_analyzer, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
