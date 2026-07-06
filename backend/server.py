#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Штурвал — бэкенд-«посредник» между amoCRM и сайтом.
Токен хранится здесь, на сервере, и НЕ попадает в код сайта.
Запуск: python3 server.py   (или двойной клик по «Запустить-сервер.command»)
"""
import os, json, time, threading, urllib.request, urllib.error, urllib.parse, hmac, hashlib, gzip, re
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
    # WhatsApp напрямую (WhatsApp Cloud API от Meta) — официальное подключение своего номера
    cfg["WA_VERIFY_TOKEN"] = env("WA_VERIFY_TOKEN") or "shturval-wa-2026"  # слово-пароль для webhook (вписать в Meta)
    cfg["WA_TOKEN"] = env("WA_TOKEN")            # постоянный токен (System User) с whatsapp_business_messaging
    cfg["WA_PHONE_ID"] = env("WA_PHONE_ID")      # id номера телефона (Phone number ID из кабинета Meta)
    cfg["WA_DISPLAY"] = env("WA_DISPLAY")        # как показывать номер в интерфейсе (необязательно, напр. «+996…»)
    cfg["WA_APP_SECRET"] = env("WA_APP_SECRET")  # секрет приложения Meta (проверка подписи); если пусто — берём IG_APP_SECRET
    # Авто Рент — приватное чтение таблицы аренды через сервисный аккаунт Google
    cfg["GOOGLE_SA_JSON"] = env("GOOGLE_SA_JSON")   # JSON сервис-аккаунта (читает таблицу)
    cfg["RENT_SHEET_ID"] = env("RENT_SHEET_ID") or "1dEUZijK02ho264jrMBmXBzAZf18ITmLCXVMkdvqLr28"
    # Реклама (Meta Marketing API — таргет прямо из «Штурвала»)
    cfg["META_ADS_TOKEN"] = env("META_ADS_TOKEN")    # токен системного пользователя (СЕКРЕТ) с ads_management
    cfg["META_AD_ACCOUNT"] = env("META_AD_ACCOUNT")  # id рекламного аккаунта (act_XXXX или просто число)
    cfg["META_PAGE_ID"] = env("META_PAGE_ID")        # id Страницы Facebook (реклама привязана к странице)
    cfg["META_IG_ID"] = env("META_IG_ID")            # id бизнес-аккаунта Instagram для рекламы (если пусто — берём IG_ACCOUNT_ID)
    cfg["IG_USERNAME"] = env("IG_USERNAME")          # ник Instagram (для ссылки по умолчанию)
    return cfg

CFG = load_secret()
BASE = "https://{}.amocrm.ru/api/v4".format(CFG.get("AMO_SUBDOMAIN", ""))

# --- пользователи и роли ---
# каждый пользователь: пароль -> {name, sections}. sections=["all"] = видит всё.
# разделы: dash, crm, chats, prod, sales, clients, market, analytics, fin, ai, set
COMPANY_ID = "bizmart"   # компания по умолчанию; у пользователя может быть своя (мульти-тенант)
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
        info.setdefault("company", COMPANY_ID)   # к какой компании относится (мульти-тенант)
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
                          "company": (u.get("company") or COMPANY_ID),
                          "owner": bool(u.get("owner")),
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
        # грузим сотрудников ВСЕХ компаний (мульти-тенант): каждого помечаем его компанией
        rows = _supa("GET", "employees", "?select=*")
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
            # ВАЖНО: флаг владельца сохраняем из env-записи (USERS_JSON), даже если строка из
            # таблицы перекрывает (в employees колонки owner нет). Иначе смена пароля владельца
            # из интерфейса стирала бы его права (он попадал в обычные сотрудники).
            _owner_flag = bool(u.get("owner") or (old.get("owner") if old else False))
            _add((u.get("pw") or "").strip(),
                 {"name": u.get("name", "Сотрудник"), "sections": secs,
                  "role": u.get("role", ""), "department": u.get("department", ""),
                  "phone": u.get("phone", "") or "",
                  "company": (u.get("company_id") or COMPANY_ID),
                  "owner": _owner_flag,
                  "plan_day": u.get("plan_day", 0),
                  "salary_month": u.get("salary_month", 0),
                  "daily_rate": u.get("daily_rate", 0),
                  "bonus_month": u.get("bonus_month", 0),
                  "video_rate": u.get("video_rate", 0)},
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

def company_users(co):
    """Пользователи одной компании (мульти-тенант). co=None → компания по умолчанию."""
    co = co or COMPANY_ID
    return [v for v in all_users() if (v.get("company") or COMPANY_ID) == co]

def _pkey(user):
    """Ключ зарплаты — логин (уникален). Если логина нет — имя (старое поведение)."""
    return (user.get("login") or user.get("name") or "").strip()

# --- пароли: новые храним хешем (pbkdf2), старые (открытый текст) ещё принимаем ---
def _hash_pw(pw):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode("utf-8"), salt, 120000)
    return "pbkdf2$%s$%s" % (salt.hex(), dk.hex())

def _verify_pw(stored, pw):
    stored = str(stored or "")
    if not stored or not pw:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, salt_hex, hash_hex = stored.split("$", 2)
            dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), 120000)
            return hmac.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    return hmac.compare_digest(stored, pw)   # старый формат — открытый текст

# какой раздел нужен для каждого data-эндпоинта (для проверки прав)
SECTION_OF = [
    ("/api/overview", "crm"),
    ("/api/inventory", "prod"), ("/api/products", "prod"),
    ("/api/categories", "prod"), ("/api/add-product", "prod"),
    ("/api/assortment", "sales"),
    ("/api/sales-history", "sales"), ("/api/sales", "sales"),
    ("/api/suppliers", "supl"), ("/api/expenses", "fin"),
    ("/api/rent", "rent"),
    ("/api/market", "market"),
    ("/api/chats", "chats"), ("/api/chat", "chats"), ("/api/send", "chats"),
    ("/api/bot-feedback", "chats"),
    ("/api/ig/clients", "clients"), ("/api/wa/clients", "clients"),
    ("/api/ig/conversations", "chats"), ("/api/ig/thread", "chats"),
    ("/api/ig/reply", "chats"), ("/api/ig/accounts", "chats"),
    ("/api/ig/broadcast", "chats"), ("/api/ig/bot", "chats"), ("/api/ig/media", "chats"),
    ("/api/wa/conversations", "chats"), ("/api/wa/thread", "chats"),
    ("/api/wa/reply", "chats"), ("/api/wa/botreply", "chats"), ("/api/wa/accounts", "chats"),
    ("/api/wa/template", "chats"),
    ("/api/wa/media", "chats"), ("/api/wa/profile", "chats"), ("/api/wa/read", "chats"),
    ("/api/assistant", "ai"),
    ("/api/ads", "ads"),
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
    # Авто Рент: доступ даёт «rent» (весь раздел) ИЛИ любое под-разрешение «rent_*» (отдельные вкладки)
    if path.startswith("/api/rent"):
        s = (user or {}).get("sections", [])
        return ("all" in s) or ("rent" in s) or any(str(x).startswith("rent_") for x in s)
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

# --- АВТО РЕНТ: приватное чтение таблицы аренды авто через сервисный аккаунт ---
def _rent_clean(s):
    return (str(s) if s is not None else "").replace("\xa0", " ").strip()

def _rent_rows(values, keys, n, must_idx):
    out = []
    for r in values:
        rr = [_rent_clean(r[i]) if i < len(r) else "" for i in range(n)]
        if not any(rr[j] for j in must_idx):
            continue
        out.append(dict(zip(keys, rr)))
    return out

def rent_fetch():
    """Читает таблицу «Аренда авто — учёт» сервис-аккаунтом и собирает JSON.
    Данные отдаются только через /api/rent за логином — в публичный код сайта не попадают."""
    sa_raw = CFG.get("GOOGLE_SA_JSON", "").strip()
    if not sa_raw:
        return {"error": "Не настроен доступ к таблице (GOOGLE_SA_JSON). Добавьте ключ в настройках сервера (Render)."}
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except Exception as e:
        return {"error": "На сервере не установлены Google-библиотеки: %s" % e}
    try:
        info = json.loads(sa_raw)
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        sh = build("sheets", "v4", credentials=creds, cache_discovery=False)
        sid = CFG.get("RENT_SHEET_ID", "")
        def g(rng):
            return sh.spreadsheets().values().get(
                spreadsheetId=sid, range=rng,
                valueRenderOption="FORMATTED_VALUE").execute().get("values", [])
        D = {}
        D["cars"] = _rent_rows(g("'Машины'!A3:K40"),
            ["id","model","year","plate","color","price","deposit","cost","status","bought","note"], 11, [0])
        D["rentals"] = _rent_rows(g("'Аренды'!A3:O230"),
            ["id","carid","model","renter","phone","start","end","days","price","sum","deposit","got","debt","status","note"], 15, [0])
        D["expenses"] = _rent_rows(g("'Расходы'!A3:G300"),
            ["date","carid","model","cat","desc","sum","note"], 7, [3,5])
        D["handed"] = _rent_rows(g("'Сдано'!A3:C120"), ["date","sum","comment"], 3, [1])
        D["salary"] = _rent_rows(g("'Зарплата'!A3:C30"), ["date","sum","comment"], 3, [1])
        D["undercollect"] = _rent_rows(g("'Недосбор'!A3:C30"), ["date","sum","comment"], 3, [1])
        months_all = _rent_rows(g("'Отчёт по месяцам'!A3:K20"),
            ["month","start","end","count","days","revenue","debts","expenses","profit","margin","avg"], 11, [0])
        # строку ИТОГО из списка месяцев убираем (итог показываем отдельно в сводке)
        D["months"] = [m for m in months_all if (m.get("month") or "").upper() != "ИТОГО"]
        # Сводка — берём прямо из вашего «Дашборда» (как в таблице), плюс выбранный период.
        dash = g("'Дашборд'!A1:P20")
        def cell(rr, ci):
            row = dash[rr-1] if rr-1 < len(dash) else []
            return _rent_clean(row[ci]) if ci < len(row) else ""
        D["summary"] = {
            "profit": cell(5,1), "revenue": cell(5,5), "debts": cell(5,9), "expenses": cell(5,13),
            "handed": cell(7,1), "balance": cell(7,5), "salary": cell(7,9), "undercollect": cell(7,13),
            "cars_total": cell(11,5), "cars_free": cell(12,5), "cars_rented": cell(13,5), "cars_repair": cell(14,5),
            "rev_accrued": cell(11,13), "avg_check": cell(12,13), "avg_days": cell(13,13), "margin": cell(14,13),
            "rentals_total": cell(17,5), "rentals_active": cell(18,5),
            "plan": cell(8,1), "period": cell(3,5) or "ИТОГО",
        }
        D["synced"] = time.strftime("%d.%m.%Y %H:%M")
        return D
    except Exception as e:
        return {"error": "Не удалось прочитать таблицу: %s" % e}

def rent_data():
    """Кэш на 5 минут; ошибки не кэшируем, чтобы быстро подхватить починку."""
    now = time.time()
    c = _cache.get("rent_data")
    if c and now - c[0] < 300 and not c[1].get("error"):
        return c[1]
    val = rent_fetch()
    if not val.get("error"):
        _cache["rent_data"] = (now, val)
    return val

# ====== АВТО РЕНТ: собственная база (Supabase KV) — ввод учёта прямо на сайте ======
# Данные хранятся одним документом в kv_cache (ключ rent_<company>). Таблица Google
# больше не нужна — сайт самостоятельная система. Импорт из таблицы — разовый (seed).
def _rent_key(company=None):
    # ключ данных аренды зависит от КОМПАНИИ вошедшего пользователя (мульти-тенант):
    # у Бизмарта rent_bizmart, у демо-клиента rent_<его компания> — данные не пересекаются.
    return "rent_" + (company or COMPANY_ID)   # COMPANY_ID определён ниже; вызывается в рантайме
RENT_MONTHS_RU = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
RENT_WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

def _rnum(s):
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s or "")
    neg = "-" in s
    d = "".join(ch for ch in s if ch.isdigit())
    n = int(d) if d else 0
    return -n if neg else n

def _rdate(s):
    s = (str(s or "")).strip()
    if not s:
        return None
    import datetime
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None

def _rmoney(n):
    return "{:,}".format(int(n)).replace(",", " ")

# кэш документа аренды в памяти: убирает поход в Supabase на каждый запрос/действие.
# Запись (rent_save) обновляет кэш сразу, поэтому правки видны мгновенно; TTL — на случай
# внешних изменений базы.
_RENT_DOC_CACHE = {}
_RENT_DOC_TTL = 60

def rent_doc(company=None):
    key = _rent_key(company)
    ent = _RENT_DOC_CACHE.get(key)
    if ent and (time.time() - ent[0]) < _RENT_DOC_TTL:
        return ent[1]
    d = kv_load(key)
    if not isinstance(d, dict):
        d = {}
    for k in ("cars", "rentals", "expenses", "handed", "salary", "undercollect", "notes", "rtasks"):
        if not isinstance(d.get(k), list):
            d[k] = []
    d.setdefault("seq", 0)
    _RENT_DOC_CACHE[key] = (time.time(), d)
    return d

def _rent_persist_bg(key, snapshot):
    """Фоновое сохранение в Supabase с повторами — чтобы ответ пользователю был мгновенным,
    но запись не терялась при разовом сбое сети."""
    def go():
        for attempt in range(4):
            try:
                _supa("POST", "kv_cache", "?on_conflict=k", {"k": key, "v": snapshot})
                return
            except Exception:
                time.sleep(0.6 * (attempt + 1))
        print("[rent] WARN: не удалось сохранить %s в Supabase после повторов" % key)
    threading.Thread(target=go, daemon=True).start()

def rent_save(d, company=None):
    key = _rent_key(company)
    _RENT_DOC_CACHE[key] = (time.time(), d)   # свежий снимок в кэш — чтение сразу видит правку
    _rent_build_bust(company)                 # готовые ответы устарели — пересоберём при первом чтении
    # сохранение в Supabase — в фоне (сериализация тоже на фоновом потоке, вне критического пути)
    if supa_on():
        _rent_persist_bg(key, d)
    else:
        kv_save(key, d)

def _rent_newid(d, pref):
    d["seq"] = int(d.get("seq", 0)) + 1
    return "%s%03d" % (pref, d["seq"])

# кэш готового ответа по (компания, период): повторное чтение и смена периода — мгновенно.
# Сбрасывается при любой записи (rent_save) и по TTL (просрочка зависит от текущей даты).
_RENT_BUILD_CACHE = {}
_RENT_BUILD_TTL = 120

def _rent_build_bust(company=None):
    pref = _rent_key(company) + "|"
    for k in [k for k in _RENT_BUILD_CACHE if k.startswith(pref)]:
        _RENT_BUILD_CACHE.pop(k, None)

def rent_build(period=None, company=None):
    """Грузит документ из базы, считает производные поля (дни/сумма/долг) и сводку
    за выбранный период (period='YYYY-MM' или None/'ИТОГО'). Списки — новые сверху."""
    ckey = _rent_key(company) + "|" + str(period or "")
    ent = _RENT_BUILD_CACHE.get(ckey)
    if ent and (time.time() - ent[0]) < _RENT_BUILD_TTL:
        return ent[1]
    d = rent_doc(company)
    cars = d["cars"]
    # ── Связь «Машины ↔ Аренды» ──────────────────────────────────────────────
    # «Живой» статус машины считается по арендам, а не хранится вручную:
    #   • есть открытая аренда на эту модель (статус НЕ «Завершена») → «В аренде»
    #   • нет открытых аренд → «Свободна»
    # Как только аренду отмечают «Завершена» — машина сразу снова «Свободна».
    # Ручные статусы «На ремонте» и «Продана» приоритетнее (аренды их не трогают).
    _active_models = set()
    for _r in d["rentals"]:
        _st = str(_r.get("status") or "").lower()
        if _st and "заверш" not in _st:
            _md = (_r.get("model") or "").strip().lower()
            if _md:
                _active_models.add(_md)
    for _c in cars:
        _man = (_c.get("status") or "").strip()
        _manl = _man.lower()
        if "продан" in _manl or "ремонт" in _manl:
            _c["status_live"] = _man               # ручной статус сохраняем
        elif (_c.get("model") or "").strip().lower() in _active_models:
            _c["status_live"] = "В аренде"
        else:
            _c["status_live"] = "Свободна"
    import datetime
    _lt = time.localtime()
    today = datetime.date(_lt.tm_year, _lt.tm_mon, _lt.tm_mday)
    rentals = []
    for r in d["rentals"]:
        x = dict(r)
        sd, ed = _rdate(x.get("start")), _rdate(x.get("end"))
        days = (ed - sd).days if (sd and ed and ed >= sd) else _rnum(x.get("days"))
        price = _rnum(x.get("price"))
        got = _rnum(x.get("got"))
        summ = days * price if (days and price) else _rnum(x.get("sum"))
        # просрочка: активная аренда, дата сдачи прошла → автодолг = дни просрочки × цена
        active = "заверш" not in (str(x.get("status") or "").lower())
        odays = (today - ed).days if (active and ed and ed < today) else 0
        overdue = odays * price if (odays > 0 and price) else 0
        x["_days"] = days
        x["_sum"] = summ
        x["_got"] = got
        x["_overdue_days"] = odays
        x["_overdue"] = overdue
        x["_debt"] = max(summ - got, 0) + overdue
        rentals.append(x)
    ts = lambda r: r.get("ts", 0)
    rentals.sort(key=ts, reverse=True)
    exps = sorted(d["expenses"], key=ts, reverse=True)
    handed = sorted(d["handed"], key=ts, reverse=True)
    salary = sorted(d["salary"], key=ts, reverse=True)
    under = sorted(d["undercollect"], key=ts, reverse=True)
    # помесячно (по всем данным) — для разбивки и списка периодов
    months = {}
    for r in rentals:
        sd = _rdate(r.get("start"))
        if not sd:
            continue
        k = "%04d-%02d" % (sd.year, sd.month)
        m = months.setdefault(k, {"count": 0, "revenue": 0, "expenses": 0})
        m["count"] += 1
        m["revenue"] += r["_got"]
    for e in exps:
        ed = _rdate(e.get("date"))
        if not ed:
            continue
        k = "%04d-%02d" % (ed.year, ed.month)
        m = months.setdefault(k, {"count": 0, "revenue": 0, "expenses": 0})
        m["expenses"] += _rnum(e.get("sum"))
    months_list = []
    for k in sorted(months.keys(), reverse=True):
        m = months[k]
        y, mo = k.split("-")
        rev, exx = m["revenue"], m["expenses"]
        months_list.append({"month": "%s %s" % (RENT_MONTHS_RU[int(mo)], y), "count": str(m["count"]),
                            "revenue": _rmoney(rev), "expenses": _rmoney(exx), "profit": _rmoney(rev - exx),
                            "margin": (str(round((rev - exx) / rev * 100)) + "%") if rev else "—"})
    # варианты периода для выбора на дашборде
    periods = [{"key": "ИТОГО", "label": "ИТОГО"}]
    for k in sorted(months.keys(), reverse=True):
        y, mo = k.split("-")
        periods.append({"key": k, "label": "%s %s" % (RENT_MONTHS_RU[int(mo)], y)})
    # фильтр по выбранному периоду:
    #   None / 'ИТОГО'           — всё
    #   'YYYY-MM'                — месяц
    #   'YYYY-MM-DD:YYYY-MM-DD'  — произвольный диапазон (день/неделя/период)
    sel = period if (period and period != "ИТОГО") else None
    rng = None
    if sel and ":" in sel:
        a, _, b = sel.partition(":")
        rng = (_rdate(a.strip()), _rdate(b.strip()))
        if rng[0] and rng[1] and rng[1] < rng[0]:
            rng = (rng[1], rng[0])
    def inper(datestr):
        if not sel:
            return True
        dt = _rdate(datestr)
        if not dt:
            return False
        if rng:
            lo, hi = rng
            return bool((not lo or dt >= lo) and (not hi or dt <= hi))
        return ("%04d-%02d" % (dt.year, dt.month)) == sel
    frent = [r for r in rentals if inper(r.get("start"))]
    fexp = [e for e in exps if inper(e.get("date"))]
    fhanded = [h for h in handed if inper(h.get("date"))]
    fsalary = [s for s in salary if inper(s.get("date"))]
    funder = [u for u in under if inper(u.get("date"))]
    # суммы за выбранный период
    revenue = sum(r["_got"] for r in frent)
    accrued = sum(r["_sum"] for r in frent)
    debts = sum(r["_debt"] for r in frent)
    exp_sum = sum(_rnum(e.get("sum")) for e in fexp)
    handed_sum = sum(_rnum(h.get("sum")) for h in fhanded)
    salary_sum = sum(_rnum(s.get("sum")) for s in fsalary)
    under_sum = sum(_rnum(u.get("sum")) for u in funder)
    profit = revenue - exp_sum
    balance = profit - handed_sum - salary_sum - under_sum
    cnt = len(frent)
    dl = [r["_days"] for r in frent if r["_days"] > 0]
    avg_days = round(sum(dl) / len(dl), 1) if dl else 0
    scnt = lambda w: sum(1 for c in cars if w in (c.get("status_live") or c.get("status") or "").lower())
    active = sum(1 for r in frent if "заверш" not in (r.get("status") or "").lower() and (r.get("status") or ""))
    if rng:
        _fl = lambda dt: dt.strftime("%d.%m.%Y") if dt else "…"
        per_label = "%s — %s" % (_fl(rng[0]), _fl(rng[1])) if rng[0] != rng[1] else _fl(rng[0])
    else:
        per_label = next((p["label"] for p in periods if p["key"] == (sel or "ИТОГО")), "ИТОГО")
    # анализ по машинам (за выбранный период): какая машина сколько заработала
    exp_by_model = {}
    for e in fexp:
        md = (e.get("model") or "").strip()
        if md:
            exp_by_model[md] = exp_by_model.get(md, 0) + _rnum(e.get("sum"))
    car_an = []
    for c in cars:
        if "продан" in (c.get("status") or "").lower():   # проданные — в архиве, не в анализе
            continue
        md = (c.get("model") or "").strip()
        cr = [r for r in frent if (r.get("model") or "").strip() == md]
        crev = sum(r["_got"] for r in cr)
        cdebt = sum(r["_debt"] for r in cr)
        cdays = sum(r["_days"] for r in cr if r["_days"] > 0)
        cexp = exp_by_model.get(md, 0)
        cprof = crev - cexp
        ccost = _rnum(c.get("cost"))
        car_an.append({"id": c.get("id", ""), "model": md, "count": str(len(cr)), "days": str(cdays),
                       "revenue": _rmoney(crev), "debts": _rmoney(cdebt), "expenses": _rmoney(cexp),
                       "profit": _rmoney(cprof), "margin": (str(round(cprof / crev * 100)) + "%") if crev else "—",
                       "cost": _rmoney(ccost) if ccost else "", "payback": (str(round(crev / ccost * 100)) + "%") if ccost else "—",
                       "_profit": cprof, "_rev": crev})
    car_an.sort(key=lambda x: x["_profit"], reverse=True)
    # ABC-анализ парка по выручке (A ≤80%, B ≤95%, C — остальное)
    abc = []
    tot_rev_cars = sum(x["_rev"] for x in car_an) or 0
    cum = 0
    for c in sorted(car_an, key=lambda x: x["_rev"], reverse=True):
        if c["_rev"] <= 0:
            continue
        share = c["_rev"] / tot_rev_cars * 100 if tot_rev_cars else 0
        cum += share
        cls = "A" if cum <= 80 else ("B" if cum <= 95 else "C")
        abc.append({"model": c["model"], "revenue": c["revenue"], "share": str(round(share)) + "%",
                    "cum": str(round(cum)) + "%", "cls": cls})
    # выручка по дням недели (по дате старта аренды, за период)
    wd = [{"d": RENT_WEEKDAYS_RU[i], "revenue": 0, "count": 0} for i in range(7)]
    for r in frent:
        sd = _rdate(r.get("start"))
        if sd:
            wd[sd.weekday()]["revenue"] += r["_got"]
            wd[sd.weekday()]["count"] += 1
    wd_max = max([x["revenue"] for x in wd]) if wd else 0
    weekdays = [{"d": x["d"], "count": str(x["count"]), "revenue": _rmoney(x["revenue"]),
                 "pct": (round(x["revenue"] / wd_max * 100) if wd_max else 0)} for x in wd]
    best_wd = max(wd, key=lambda x: x["revenue"])["d"] if wd_max else "—"
    # выручка по дням (для диаграммы по дням месяца), по дате старта
    dd_map = {}
    for r in frent:
        sd = _rdate(r.get("start"))
        if sd:
            kk = "%04d-%02d-%02d" % (sd.year, sd.month, sd.day)
            dd_map[kk] = dd_map.get(kk, 0) + r["_got"]
    dd_max = max(dd_map.values()) if dd_map else 0
    daily = []
    for kk in sorted(dd_map.keys()):
        y, mo, da = kk.split("-")
        daily.append({"day": da, "date": "%s.%s" % (da, mo), "revenue": dd_map[kk],
                      "revenue_f": _rmoney(dd_map[kk]), "pct": (round(dd_map[kk] / dd_max * 100) if dd_max else 0)})
    # КЛИЕНТЫ (арендаторы) — агрегат из аренд за период: кто, телефон, сколько раз, выручка, долг, последняя
    cl_map = {}
    for r in frent:
        nm = (r.get("renter") or "").strip()
        ph = (r.get("phone") or "").strip()
        if not nm and not ph:
            continue
        key = (nm.lower() + "|" + ph)
        c = cl_map.get(key)
        if not c:
            c = {"name": nm, "phone": ph, "count": 0, "rev": 0, "debt": 0, "last": "", "_ord": None, "cars": set()}
            cl_map[key] = c
        c["count"] += 1
        c["rev"] += r["_got"]
        c["debt"] += r["_debt"]
        if r.get("model"):
            c["cars"].add(r.get("model"))
        sd = _rdate(r.get("start"))
        if sd and (c["_ord"] is None or sd > c["_ord"]):
            c["_ord"] = sd
            c["last"] = r.get("start", "")
    clients = [{"name": c["name"] or "—", "phone": c["phone"], "count": str(c["count"]),
                "revenue": _rmoney(c["rev"]), "debt": _rmoney(c["debt"]), "_debt": c["debt"],
                "last": c["last"], "cars": ", ".join(sorted(c["cars"]))}
               for c in sorted(cl_map.values(), key=lambda x: x["rev"], reverse=True)]
    # план выручки: цель на месяц и % выполнения по каждому месяцу (по всем данным)
    plan_target = _rnum(d.get("plan_target")) or 400000
    plan_rows = []
    plan_fact_tot = 0
    for k in sorted(months.keys()):
        m = months[k]
        y, mo = k.split("-")
        fact = m["revenue"]
        plan_fact_tot += fact
        plan_rows.append({"month": "%s %s" % (RENT_MONTHS_RU[int(mo)], y), "plan": _rmoney(plan_target),
                          "fact": _rmoney(fact), "pct": (str(round(fact / plan_target * 100)) + "%") if plan_target else "—",
                          "pct_num": round(fact / plan_target * 100) if plan_target else 0})
    plan_block = {"target": _rmoney(plan_target), "target_raw": plan_target, "rows": plan_rows,
                  "total_plan": _rmoney(plan_target * len(plan_rows)), "total_fact": _rmoney(plan_fact_tot),
                  "total_pct": (str(round(plan_fact_tot / (plan_target * len(plan_rows)) * 100)) + "%") if (plan_target and plan_rows) else "—"}
    summary = {
        "profit": _rmoney(profit), "revenue": _rmoney(revenue), "debts": _rmoney(debts), "expenses": _rmoney(exp_sum),
        "handed": _rmoney(handed_sum), "balance": _rmoney(balance), "salary": _rmoney(salary_sum), "undercollect": _rmoney(under_sum),
        "cars_total": str(sum(1 for c in cars if "продан" not in (c.get("status") or "").lower())),
        "cars_free": str(scnt("свобод")), "cars_rented": str(scnt("аренд")), "cars_repair": str(scnt("ремонт")),
        "rev_accrued": _rmoney(accrued), "avg_check": _rmoney(round(accrued / cnt) if cnt else 0),
        "avg_days": str(avg_days).replace(".", ","), "margin": (str(round(profit / revenue * 100)) + "%") if revenue else "—",
        "rentals_total": str(cnt), "rentals_active": str(active), "plan": "", "period": per_label,
        "period_key": (sel or "ИТОГО"),
        "commission_pct": (int(float(d.get("commission_pct"))) if d.get("commission_pct") not in (None, "") else 10),
        "commissions": d.get("commissions") or {},
        "currency": (d.get("currency") or "сом"),
        "som_per_usd": (float(d.get("som_per_usd")) if d.get("som_per_usd") not in (None, "") else 87.0),
    }
    # форматируем для показа (а raw — для редактирования формой)
    def fr(r):
        return {"id": r.get("id", ""), "model": r.get("model", ""), "renter": r.get("renter", ""),
                "phone": r.get("phone", ""), "start": r.get("start", ""), "end": r.get("end", ""),
                "days": str(r["_days"] or ""), "price": _rmoney(_rnum(r.get("price"))) if _rnum(r.get("price")) else "",
                "sum": _rmoney(r["_sum"]), "got": _rmoney(r["_got"]) if r.get("got") not in (None, "") else "",
                "debt": _rmoney(r["_debt"]), "status": r.get("status", ""), "note": r.get("note", ""),
                "overdue": _rmoney(r["_overdue"]) if r["_overdue"] else "", "overdue_days": r["_overdue_days"],
                "by": r.get("by", ""), "edit_by": r.get("edit_by", ""),
                "price_raw": _rnum(r.get("price")), "got_raw": _rnum(r.get("got"))}
    def fe(e):
        return {"id": e.get("id", ""), "date": e.get("date", ""), "model": e.get("model", ""),
                "cat": e.get("cat", ""), "desc": e.get("desc", ""), "sum": _rmoney(_rnum(e.get("sum"))),
                "by": e.get("by", ""), "edit_by": e.get("edit_by", ""), "sum_raw": _rnum(e.get("sum"))}
    def fh(h):
        return {"id": h.get("id", ""), "date": h.get("date", ""), "sum": _rmoney(_rnum(h.get("sum"))),
                "comment": h.get("comment", ""), "by": h.get("by", ""), "sum_raw": _rnum(h.get("sum"))}
    result = {"data": {"cars": cars, "rentals": [fr(r) for r in rentals], "expenses": [fe(e) for e in exps],
                     "handed": [fh(h) for h in handed], "salary": [fh(s) for s in salary],
                     "undercollect": [fh(u) for u in under], "months": months_list,
                     "caranalysis": car_an, "plan": plan_block,
                     "abc": abc, "weekdays": weekdays, "best_weekday": best_wd, "daily": daily,
                     "clients": clients,
                     "notes": sorted(d.get("notes", []), key=lambda x: x.get("ts", 0), reverse=True),
                     "rtasks": sorted(d.get("rtasks", []), key=lambda x: (x.get("status") == "done", -x.get("ts", 0))),
                     "staff": [{"login": (v.get("login") or "").lower(), "name": (v.get("name") or v.get("login") or ""),
                                "role": (v.get("role") or "")}
                               for v in company_users(company or COMPANY_ID) if not v.get("owner")]},
            "summary": summary, "periods": periods, "synced": time.strftime("%d.%m.%Y %H:%M")}
    _RENT_BUILD_CACHE[ckey] = (time.time(), result)
    return result

def rent_apply(action, p, company=None, user=None):
    """Изменение данных аренды. Возвращает свежий rent_build().
    Автор записи фиксируется в поле `by`. Машины и план правит только владелец."""
    d = rent_doc(company)
    user = user or {}
    is_owner = bool(user.get("owner")) or ("all" in (user.get("sections") or []))
    who = user.get("name") or user.get("login") or ""
    now_ms = int(time.time() * 1000)
    # Заметки и задачи: владелец ИЛИ администратор (роль содержит «админ»).
    # (доп. совместимость: старый токен rent_notes_edit в sections тоже даёт право)
    role_l = (user.get("role") or "").lower()
    is_admin = is_owner or ("админ" in role_l) or ("admin" in role_l) or ("rent_notes_edit" in (user.get("sections") or []))
    if action in ("add_note", "edit_note", "del_note",
                  "add_rtask", "edit_rtask", "del_rtask") and not is_admin:
        return {"error": "Заметки и задачи может ставить и менять владелец или администратор"}
    # Машины: владелец ИЛИ сотрудник с полным доступом «rent_cars_full»
    can_cars = is_owner or ("rent_cars_full" in (user.get("sections") or []))
    if action in ("add_car", "edit_car", "del_car") and not can_cars:
        return {"error": "Машины может менять владелец или сотрудник с доступом «Машины: полный»"}
    # План и оплата — только владелец
    if action in ("set_plan", "set_commission", "set_currency", "set_fx") and not is_owner:
        return {"error": "План и оплату может менять только владелец"}
    def keep(lst, _id):
        return [x for x in lst if x.get("id") != _id]
    def find(lst, _id):
        for x in lst:
            if x.get("id") == _id:
                return x
        return None
    def stamp_new(rec):
        rec["by"] = who; rec["by_ts"] = now_ms
        return rec
    def stamp_edit(rec):
        if rec is not None:
            rec["edit_by"] = who; rec["edit_ts"] = now_ms
        return rec
    if action == "add_rental":
        d["rentals"].append(stamp_new({"id": _rent_newid(d, "RN"), "ts": now_ms,
            "model": p.get("model", ""), "renter": p.get("renter", ""), "phone": p.get("phone", ""),
            "start": p.get("start", ""), "end": p.get("end", ""), "price": p.get("price", ""),
            "got": p.get("got", ""), "status": (p.get("status") or "Активна"), "note": p.get("note", "")}))
    elif action == "edit_rental":
        r = find(d["rentals"], p.get("id"))
        if r:
            for f in ("model", "renter", "phone", "start", "end", "price", "got", "status", "note"):
                if f in p:
                    r[f] = p[f]
            stamp_edit(r)
    elif action == "del_rental":
        d["rentals"] = keep(d["rentals"], p.get("id"))
    elif action == "add_expense":
        d["expenses"].append(stamp_new({"id": _rent_newid(d, "EX"), "ts": now_ms,
            "date": p.get("date", ""), "model": p.get("model", ""), "cat": p.get("cat", ""),
            "desc": p.get("desc", ""), "sum": p.get("sum", "")}))
    elif action == "edit_expense":
        e = find(d["expenses"], p.get("id"))
        if e:
            for f in ("date", "model", "cat", "desc", "sum"):
                if f in p:
                    e[f] = p[f]
            stamp_edit(e)
    elif action == "del_expense":
        d["expenses"] = keep(d["expenses"], p.get("id"))
    elif action == "add_handed":
        d["handed"].append(stamp_new({"id": _rent_newid(d, "HD"), "ts": now_ms,
            "date": p.get("date", ""), "sum": p.get("sum", ""), "comment": p.get("comment", "")}))
    elif action == "edit_handed":
        h = find(d["handed"], p.get("id"))
        if h:
            for f in ("date", "sum", "comment"):
                if f in p:
                    h[f] = p[f]
            stamp_edit(h)
    elif action == "del_handed":
        d["handed"] = keep(d["handed"], p.get("id"))
    elif action == "add_car":
        d["cars"].append(stamp_new({"id": _rent_newid(d, "M"), "ts": now_ms,
            "model": p.get("model", ""), "plate": p.get("plate", ""), "price": p.get("price", ""),
            "deposit": p.get("deposit", ""), "cost": p.get("cost", ""),
            "sold_price": p.get("sold_price", ""), "sold_date": p.get("sold_date", ""),
            "status": (p.get("status") or "Свободна"), "note": p.get("note", "")}))
    elif action == "edit_car":
        c = find(d["cars"], p.get("id"))
        if c:
            for f in ("model", "plate", "price", "deposit", "cost", "sold_price", "sold_date", "status", "note"):
                if f in p:
                    c[f] = p[f]
            stamp_edit(c)
    elif action == "del_car":
        d["cars"] = keep(d["cars"], p.get("id"))
    elif action == "set_plan":
        d["plan_target"] = _rnum(p.get("plan_target"))
    elif action == "set_currency":
        cur = (str(p.get("currency") or "").strip())[:8] or "сом"
        d["currency"] = cur
    elif action == "set_fx":                       # курс сом за 1$ (для перевода оклада в зарплате)
        fx = _rnum(p.get("som_per_usd"))
        d["som_per_usd"] = fx if fx and fx > 0 else 87
    elif action == "set_commission":
        # общий % по умолчанию + персональные % по логинам
        dp = p.get("default_pct")
        if dp not in (None, ""):
            d["commission_pct"] = _rnum(dp)
        comm = d.get("commissions") or {}
        for lg, pct in (p.get("map") or {}).items():
            comm[str(lg).lower()] = _rnum(pct)
        d["commissions"] = comm
    elif action == "add_note":
        d["notes"].append(stamp_new({"id": _rent_newid(d, "NT"), "ts": now_ms,
            "text": p.get("text", "")}))
    elif action == "edit_note":
        n = find(d["notes"], p.get("id"))
        if n:
            if "text" in p:
                n["text"] = p["text"]
            stamp_edit(n)
    elif action == "del_note":
        d["notes"] = keep(d["notes"], p.get("id"))
    elif action == "add_rtask":
        d["rtasks"].append(stamp_new({"id": _rent_newid(d, "TK"), "ts": now_ms,
            "text": p.get("text", ""), "assignee": (str(p.get("assignee") or "")).lower(),
            "due": p.get("due", ""), "status": "open"}))
    elif action == "edit_rtask":
        t = find(d["rtasks"], p.get("id"))
        if t:
            for f in ("text", "assignee", "due", "status"):
                if f in p:
                    t[f] = (str(p[f]).lower() if f == "assignee" else p[f])
            stamp_edit(t)
    elif action == "del_rtask":
        d["rtasks"] = keep(d["rtasks"], p.get("id"))
    elif action == "done_rtask":
        # отметить выполнение может владелец/администратор ИЛИ исполнитель своей задачи
        t = find(d["rtasks"], p.get("id"))
        if t:
            mylogin = (user.get("login") or "").lower()
            if not is_admin and (t.get("assignee") or "") != mylogin:
                return {"error": "Можно отмечать только свои задачи"}
            t["status"] = "open" if (t.get("status") == "done") else "done"
            t["done_by"] = who; t["done_ts"] = now_ms
    elif action == "import":
        return rent_import(d, p, company, who, now_ms)
    else:
        return {"error": "неизвестное действие"}
    rent_save(d, company)
    return rent_build(None, company)

RENT_IMPORT_FIELDS = {
    "cars": ("M", ["model", "plate", "price", "deposit", "cost", "status", "note"]),
    "rentals": ("RN", ["model", "renter", "phone", "start", "end", "price", "got", "status", "note"]),
    "expenses": ("EX", ["date", "model", "cat", "desc", "sum"]),
    "handed": ("HD", ["date", "sum", "comment"]),
}

def rent_import(d, p, company, who, now_ms):
    """Импорт из Excel/CSV. p={cars:[],rentals:[],expenses:[],handed:[],mode}.
    mode='replace' — заменить раздел целиком; иначе (по умолчанию) — добавить/обновить по id.
    Возвращает {ok, build, report}. Данные не теряются: пустые разделы не трогаем."""
    mode = (p.get("mode") or "merge").lower()
    report = {}
    for sec, (prefix, fields) in RENT_IMPORT_FIELDS.items():
        rows = p.get(sec)
        if not isinstance(rows, list) or not rows:
            continue
        cleaned = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            rec = {f: ("" if raw.get(f) is None else str(raw.get(f)).strip()) for f in fields}
            if not any(rec.values()):
                continue
            rec["id"] = (str(raw.get("id")).strip() if raw.get("id") else "") or _rent_newid(d, prefix)
            rec["ts"] = raw.get("ts") or now_ms
            rec["by"] = who
            cleaned.append(rec)
        if not cleaned:
            continue
        if mode == "replace":
            d[sec] = cleaned
            report[sec] = {"added": len(cleaned), "updated": 0, "mode": "replace"}
        else:
            byid = {x.get("id"): x for x in d.get(sec, [])}
            added = updated = 0
            for rec in cleaned:
                if rec["id"] in byid:
                    byid[rec["id"]].update(rec); updated += 1
                else:
                    d.setdefault(sec, []).append(rec); added += 1
            report[sec] = {"added": added, "updated": updated, "mode": "merge"}
    rent_save(d, company)
    return {"ok": True, "report": report, "build": rent_build(None, company)}

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

# --- Импорт из amoCRM в CRM Штурвала ---
# сопоставление этапов amoCRM → стадии Штурвала (CRM_STATUSES). Отказ проверяем ДО «закрыто».
AMO_STATUS_MAP = [
    (("неразобр", "новая", "входящ", "первичн"), "Новая заявка"),
    (("обработ", "думает", "связал", "перезвон", "квалифи", "переговор"), "В обработке"),
    (("счет", "счёт", "ожидание оплат", "ожидает оплат", "выставл"), "Ожидает оплаты"),
    (("оплач", "готов к отправ", "предоплат"), "Готов к отправке"),
    (("пути", "отправл", "доставк", "отгруж"), "В пути"),
    (("получил", "успешно", "реализова", "выполн", "завершён"), "Клиент получил товар"),
]

def _amo_map_status(name):
    n = (name or "").lower()
    for kws, st in AMO_STATUS_MAP:
        for kw in kws:
            if kw in n:
                return st
    return "Новая заявка"

def _amo_iso(ts):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if ts else ""
    except Exception:
        return ""

def _amo_contacts_index(max_pages=12):
    """id контакта -> {name, phone} (тянем постранично, по 250)."""
    idx = {}
    for n in range(1, max_pages + 1):
        r = amo_get("/contacts?limit=250&page=%d" % n)
        cs = r.get("_embedded", {}).get("contacts", [])
        if not cs:
            break
        for c in cs:
            phone = ""
            for f in (c.get("custom_fields_values") or []):
                if f.get("field_code") == "PHONE":
                    vals = f.get("values") or []
                    if vals:
                        phone = vals[0].get("value", "")
                    break
            idx[c.get("id")] = {"name": c.get("name") or "", "phone": phone}
        if len(cs) < 250:
            break
    return idx

def amo_import():
    """Выгрузить из amoCRM контакты + сделки и привести к формату CRM Штурвала."""
    pipes = amo_get("/leads/pipelines")
    status_name = {}
    for p in pipes.get("_embedded", {}).get("pipelines", []):
        for s in p.get("_embedded", {}).get("statuses", []):
            status_name[s["id"]] = s["name"]
    contacts = _amo_contacts_index()
    leads = []
    for n in range(1, 13):                       # до 3000 сделок
        r = amo_get("/leads?limit=250&with=contacts&order[created_at]=desc&page=%d" % n)
        ls = r.get("_embedded", {}).get("leads", [])
        if not ls:
            break
        leads.extend(ls)
        if len(ls) < 250:
            break
    clients = {}
    deals = []
    for l in leads:
        cid = None
        for c in (l.get("_embedded", {}).get("contacts") or []):
            cid = c.get("id")
            if c.get("is_main"):
                break
        cinfo = contacts.get(cid, {}) if cid else {}
        cname = cinfo.get("name") or l.get("name") or "Без имени"
        ckey = "amo_c_%s" % (cid if cid else ("lead_%s" % l.get("id")))
        if ckey not in clients:
            clients[ckey] = {"amo_id": ckey, "name": cname,
                             "phone": cinfo.get("phone", ""), "source": "amocrm",
                             "created_at": _amo_iso(l.get("created_at"))}
        deals.append({"amo_id": "amo_l_%s" % l.get("id"), "client_key": ckey,
                      "name": l.get("name") or cname,
                      "status": _amo_map_status(status_name.get(l.get("status_id"))),
                      "amount": l.get("price") or 0,
                      "created_at": _amo_iso(l.get("created_at"))})
    return {"clients": list(clients.values()), "deals": deals,
            "count_clients": len(clients), "count_deals": len(deals),
            "account": CFG.get("AMO_SUBDOMAIN")}

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
    - ответил ЖИВОЙ менеджер (side == "operator") → «В обработке»;
    - ответил только бот (ai_assistant) или никто → остаётся «Новая заявка».
    Стадии Ожидает оплаты/Готов к отправке/В пути/Клиент получил товар ставит менеджер вручную (перетаскиванием)."""
    has_human = any(m.get("side") == "operator" for m in arr)
    return "В обработке" if has_human else "Новая заявка"

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
            # сохраняем И имя, И @username: имя — для показа, username — для ссылки на профиль
            ig_save_name(igsid, d.get("name") or d.get("username") or "", username=d.get("username") or "")
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
            if not msg:
                continue
            if msg.get("is_echo"):
                # ИСХОДЯЩЕЕ от нас. sender=наш аккаунт, recipient=клиент. Если это НЕ эхо нашего бота —
                # значит ответил оператор (через приложение Instagram/др.) → сохраняем как ответ человека,
                # и бот в этом диалоге дальше молчит (передача человеку).
                acct = str((m.get("sender") or {}).get("id", ""))
                cust = str((m.get("recipient") or {}).get("id", ""))
                etext = msg.get("text", "")
                if etext and cust and not _igbot_is_own_echo(cust, etext):
                    _inbox_save("ig_inbox",
                                {"company_id": COMPANY_ID, "sender_id": acct, "recipient_id": cust,
                                 "text": etext, "ts": int(m.get("timestamp") or time.time() * 1000),
                                 "direction": "out", "raw": {"by": "human"}})
                continue
            sender = str((m.get("sender") or {}).get("id", ""))
            recipient = str((m.get("recipient") or {}).get("id", ""))  # наш аккаунт
            row = {"company_id": COMPANY_ID, "sender_id": sender, "recipient_id": recipient,
                   "mid": msg.get("mid", ""), "text": msg.get("text", ""),
                   "ts": int(m.get("timestamp") or 0), "direction": "in", "raw": m}
            _inbox_save("ig_inbox", row)
            if sender and sender not in _ig_names:   # новый клиент — узнаём ник в фоне
                _ig_fetch_name_async(sender, recipient)
            # ИИ-бот: если включён — Claude отвечает клиенту (в фоне, чтобы webhook был быстрым)
            if msg.get("text") and sender and recipient:
                threading.Thread(target=igbot_handle,
                                 args=(sender, recipient, msg.get("text", "")), daemon=True).start()

def ig_send(recipient_id, text, from_account_id=None):
    """Отправить сообщение в Instagram. from_account_id = НАШ аккаунт (тот, на который писал клиент)."""
    token = ig_token_for(from_account_id) if from_account_id else CFG.get("IG_TOKEN", "")
    if not token:
        raise RuntimeError("Instagram пока не подключён (нет токена). Сначала настройте приложение Meta.")
    url = IG_GRAPH + "/me/messages?access_token=" + _q(token)
    def _post(payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "Shturval/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    base = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    try:
        return _post(base)                       # обычная отправка (окно 24 часа)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()
        except Exception:
            pass
        # Вне 24 часов Instagram блокирует обычный ответ. Официальный режим «ответ живого
        # агента» (HUMAN_AGENT) продлевает окно до 7 дней — пробуем повтором.
        if e.code in (400, 403) and _re.search(r"24|outside|window|reengag|re-engag|allowed", detail, _re.I):
            tagged = {"recipient": {"id": recipient_id}, "messaging_type": "MESSAGE_TAG",
                      "tag": "HUMAN_AGENT", "message": {"text": text}}
            return _post(tagged)                  # окно до 7 дней
        raise

# имена клиентов по их IGSID. Кэш в памяти + таблица ig_names (БЕЗ живых запросов в момент загрузки).
_ig_names = {}
_ig_users = {}          # igsid -> @username (для ссылки на профиль Instagram)
_ig_names_ts = 0
def ig_load_names():
    """Подгрузить имена и @username из БД в память; обновлять не чаще раза в 5 минут."""
    global _ig_names_ts
    if time.time() - _ig_names_ts < 300 and _ig_names:
        return
    try:
        off = 0
        sel = "igsid,name,username"          # пробуем с колонкой username
        while True:                          # БД отдаёт максимум 1000 строк — тянем постранично
            try:
                rows = _supa("GET", "ig_names",
                             "?company_id=eq.%s&select=%s&order=igsid&limit=1000&offset=%d"
                             % (_q(COMPANY_ID), sel, off)) or []
            except Exception:
                # колонки username ещё нет в таблице — читаем без неё (без ссылок на профиль)
                sel = "igsid,name"
                rows = _supa("GET", "ig_names",
                             "?company_id=eq.%s&select=%s&order=igsid&limit=1000&offset=%d"
                             % (_q(COMPANY_ID), sel, off)) or []
            for row in rows:
                gid = str(row.get("igsid"))
                _ig_names[gid] = row.get("name") or ""
                if row.get("username"):
                    _ig_users[gid] = row.get("username")
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

def ig_customer_username(igsid):
    """@username клиента (для ссылки на профиль), '' если неизвестен."""
    ig_load_names()
    return _ig_users.get(str(igsid), "")

def ig_save_name(igsid, name, username=None):
    gid = str(igsid)
    if name:
        _ig_names[gid] = name
    if username:
        _ig_users[gid] = username
    if not (name or username):
        return
    row = {"company_id": COMPANY_ID, "igsid": gid}
    if name:
        row["name"] = name
    if username:
        row["username"] = username
    try:
        _supa("POST", "ig_names", "?on_conflict=company_id,igsid", row)
    except Exception:
        # колонки username может не быть в таблице — сохраняем хотя бы имя
        if username and "username" in row:
            row.pop("username", None)
            if row.get("name"):
                try:
                    _supa("POST", "ig_names", "?on_conflict=company_id,igsid", row)
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
                           "channel": "ig",
                           "last_text": _ig_disp_text(r), "last_ts": int(r.get("ts") or 0),
                           "last_dir": r.get("direction", "in"), "count": 0}
        convos[key]["count"] += 1
    out = sorted(convos.values(), key=lambda c: c["last_ts"], reverse=True)
    for c in out:
        nm = ig_customer_name(c["customer_id"]) or c["customer_id"]
        c["customer"] = nm
        # username: из отдельной колонки, а если её нет — из имени, когда оно похоже на ник инстаграма
        uname = ig_customer_username(c["customer_id"])
        if not uname and _re.match(r"^[A-Za-z0-9._]{1,30}$", nm):
            uname = nm
        c["username"] = uname
        c["profile_url"] = ("https://instagram.com/" + uname) if uname else ""
    return out

def ig_conversations():
    """Список диалогов (кэш 5с, имена — из БД, без живых запросов → быстро)."""
    return cached("ig_convos", 5, _build_ig_conversations)

def ig_clients_base():
    """База клиентов, писавших в Instagram: имя, @username, ссылка на профиль,
    последнее сообщение и число сообщений. Один клиент = одна строка."""
    out = []
    for c in ig_conversations():
        out.append({"customer_id": c["customer_id"], "name": c.get("customer") or "",
                    "username": c.get("username") or "", "profile_url": c.get("profile_url") or "",
                    "account": c.get("account") or "", "last_ts": c.get("last_ts") or 0,
                    "count": c.get("count") or 0})
    return out

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

def ig_reply(account_id, customer_id, text, by="human"):
    """Ответить клиенту с нужного аккаунта и сохранить в историю.
    by='human' (ручной ответ оператора) → ИИ-бот делает паузу в этом диалоге."""
    ig_send(customer_id, text, account_id)
    try:
        _supa("POST", "ig_inbox", "",
              {"company_id": COMPANY_ID, "sender_id": str(account_id),
               "recipient_id": str(customer_id), "text": text,
               "ts": int(time.time() * 1000), "direction": "out", "raw": {"by": by}})
    except Exception:
        pass

# ====== Instagram: медиа (входящие фото/аудио через прокси; исходящие голосовые/файлы) ======
# У WhatsApp медиа идёт через /api/wa/media (прокси + перекодировка). У Instagram раньше
# вложения показывались по «сырой» CDN-ссылке (payload.url) — браузер их не тянет (CORS/срок).
# Тут: скачиваем на сервере и отдаём фронту (как у WA), а для отправки — публичная ссылка,
# которую Meta сама скачивает.
IG_MEDIA_HOSTS = ("cdninstagram.com", "fbcdn.net", "fbsbx.com", "lookaside", "scontent", "instagram.com")

def _ig_host_ok(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    return bool(host) and any(h in host for h in IG_MEDIA_HOSTS)

def ig_media_type(mime):
    """Свести MIME-тип к типу вложения Instagram."""
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"
    if m.startswith("video/"):
        return "video"
    if m.startswith("audio/"):
        return "audio"
    return "file"

def ig_media_download(url, account_id=None):
    """Скачать вложение Instagram по CDN-ссылке (сервер её видит, браузер — нет).
    Возвращает (байты, mime). Голосовые перекодируем в MP3 для всех браузеров."""
    if not url or not _ig_host_ok(url):
        raise RuntimeError("недопустимая ссылка медиа")
    hdr = {"User-Agent": "Shturval/1.0"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60) as r:
            data = r.read(); mime = r.headers.get("Content-Type", "application/octet-stream")
    except Exception:
        # некоторым ссылкам нужен токен аккаунта
        token = ig_token_for(account_id) if account_id else CFG.get("IG_TOKEN", "")
        u2 = url + (("&" if "?" in url else "?") + "access_token=" + _q(token)) if token else url
        with urllib.request.urlopen(urllib.request.Request(u2, headers=hdr), timeout=60) as r:
            data = r.read(); mime = r.headers.get("Content-Type", "application/octet-stream")
    low = (mime or "").lower()
    if low.startswith("audio/") and not low.startswith("audio/mpeg") and "mp3" not in low:
        mp3, m2 = wa_audio_to_mp3(data)
        if m2:
            return mp3, m2
    return data, mime

def ig_send_media(recipient_id, media_url, mtype="image", from_account_id=None):
    """Отправить медиа-вложение в Instagram по публичной ссылке (Meta сама её скачивает)."""
    token = ig_token_for(from_account_id) if from_account_id else CFG.get("IG_TOKEN", "")
    if not token:
        raise RuntimeError("Instagram пока не подключён (нет токена).")
    url = IG_GRAPH + "/me/messages?access_token=" + _q(token)
    body = json.dumps({"recipient": {"id": recipient_id},
                       "message": {"attachment": {"type": mtype,
                                   "payload": {"url": media_url, "is_reusable": False}}}}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "Shturval/1.0"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode() or "{}")

# исходящие медиа держим в памяти под случайным токеном — Meta скачивает их по публичной ссылке,
# и фронт проигрывает отправленное голосовое из этого же хранилища.
_ig_outmedia = {}   # token -> (bytes, mime, ts)
def _ig_outmedia_put(data, mime):
    tok = hashlib.sha256(os.urandom(24)).hexdigest()[:32]
    now = time.time()
    _ig_outmedia[tok] = (data, mime, now)
    # 1) убираем протухшие (старше 1 часа) — Meta и фронт их давно скачали
    for k in [k for k, v in list(_ig_outmedia.items()) if now - v[2] > 3600]:
        _ig_outmedia.pop(k, None)
    # 2) если всё ещё много — вытесняем самые старые, но НЕ трогаем свежие (<2 мин),
    #    чтобы Meta гарантированно успела скачать только что отправленный файл
    if len(_ig_outmedia) > 200:
        old = sorted([k for k, v in _ig_outmedia.items() if now - v[2] > 120],
                     key=lambda k: _ig_outmedia[k][2])
        for k in old[:max(0, len(_ig_outmedia) - 140)]:
            _ig_outmedia.pop(k, None)
    return tok

def ig_reply_media(account_id, customer_id, media_url, mtype, mime="", by="human"):
    """Оператор отправил клиенту медиа в Instagram: отправить и сохранить в историю (чтобы отрисовалось)."""
    resp = ig_send_media(customer_id, media_url, mtype, account_id)
    try:
        _supa("POST", "ig_inbox", "",
              {"company_id": COMPANY_ID, "sender_id": str(account_id),
               "recipient_id": str(customer_id), "text": "", "mid": "",
               "ts": int(time.time() * 1000), "direction": "out",
               "raw": {"by": by, "message": {"attachments": [
                   {"type": mtype, "payload": {"url": media_url}}]}}})
    except Exception:
        pass
    return resp

# ====== ИИ-БОТ Instagram (Claude отвечает клиентам автоматически) ======
IGBOT_DEFAULT_PROMPT = (
    "Ты — дружелюбный консультант интернет-магазина «Бизмарт» (Бишкек, Кыргызстан). "
    "Отвечай клиентам в Instagram коротко, тепло и по делу, на языке клиента (русский или кыргызский). "
    "Магазин продаёт женскую, мужскую и детскую одежду, обувь, косметику и товары для дома. "
    "Помогай: подскажи режим работы, как сделать заказ, общую информацию о товарах. "
    "Если точно не знаешь (наличие конкретного товара, размер, цена, бронь, доставка по адресу) — "
    "НЕ выдумывай: вежливо скажи, что уточнишь у коллег, и попроси оставить номер телефона. "
    "Не обещай скидок и условий, которых не знаешь. Пиши кратко — 1–3 предложения, без формальностей."
)

# Правила языка ВСЕГДА добавляются к промпту (нельзя случайно потерять при переписывании правил).
# Гарантируют грамотный кыргызский (сингармонизм, аффиксы) и живой человеческий тон.
IGBOT_LANG_RULES = (
    "\n\n=== ЯЗЫК И ТОН (соблюдай строго) ===\n"
    "• Отвечай на ЯЗЫКЕ КЛИЕНТА: написал по-русски → отвечай по-русски; по-кыргызски → по-кыргызски; "
    "вперемешку → как ему удобнее. Никогда не переключай язык сам.\n"
    "• КЫРГЫЗСКИЙ — пиши ГРАМОТНО, соблюдай сингармонизм (гармонию гласных) в аффиксах:\n"
    "   — мн. число/падежи подбирай по последней гласной основы: -лар/-лер/-лор/-лөр, -дар/-дер/-дор/-дөр, "
    "-тар/-тер/-тор/-төр (китеп→китептер, бассейн→бассейндер, баа→баалар, көйнөк→көйнөктөр).\n"
    "   — «есть/в наличии» = «бар», «нет» = «жок», «цена» = «баасы», «сколько» = «канча», «заказ» = «заказ/буйрутма».\n"
    "   — вежливое обращение: «Саламатсызбы!», «Кош келиңиз!», «рахмат», на «Сиз».\n"
    "   — НЕ калькируй с русского дословно, пиши как живой бишкекский продавец-кыргыз.\n"
    "• Без канцелярита и роботных фраз. Тепло, коротко, по-человечески — как живой консультант в директе.\n"
    "• Эмодзи — уместно и редко (1, максимум 2 на сообщение), не в каждой строке."
)

# Если в настройках lang='ru' — отвечаем ТОЛЬКО на русском (для продающего бота WhatsApp).
IGBOT_RU_RULES = (
    "\n\n=== ЯЗЫК И ТОН (соблюдай строго) ===\n"
    "• Отвечай ВСЕГДА ТОЛЬКО на русском языке, даже если клиент пишет на кыргызском, "
    "английском или другом языке. Никогда не переходи на другой язык.\n"
    "• Без канцелярита и роботных фраз. Тепло, коротко, по-человечески — как живой продавец.\n"
    "• Эмодзи — уместно и редко (максимум 1 на сообщение)."
)

def _igbot_defaults():
    return {"enabled": False, "prompt": IGBOT_DEFAULT_PROMPT, "model": CFG.get("ANTHROPIC_MODEL"),
            "handoff_hours": 6, "kb": "", "teach": "", "sources": [], "learn_every_days": 0,
            "learn_last": 0, "learn_next": 0, "lang": "client",
            # WhatsApp: бот отвечает не сразу, а только если человек не ответил wa_fallback_min минут
            "wa_fallback": True, "wa_fallback_min": 60}

def _igbot_get():
    s = kv_load("igbot_settings") or {}
    return {"enabled": bool(s.get("enabled")), "prompt": s.get("prompt") or IGBOT_DEFAULT_PROMPT,
            "model": s.get("model") or CFG.get("ANTHROPIC_MODEL"),
            "handoff_hours": int(s.get("handoff_hours") or 6),
            "kb": s.get("kb") or "",
            "teach": s.get("teach") or "",
            "sources": s.get("sources") or [],
            "learn_every_days": int(s.get("learn_every_days") or 0),
            "learn_last": int(s.get("learn_last") or 0),
            "learn_next": int(s.get("learn_next") or 0),
            "lang": s.get("lang") or "client",
            "wa_fallback": bool(s.get("wa_fallback", True)),
            "wa_fallback_min": int(s.get("wa_fallback_min") or 60)}

def _igbot_set(patch):
    cur = _igbot_get()
    for k in ("enabled", "prompt", "model", "handoff_hours", "kb", "teach", "sources",
              "learn_every_days", "learn_last", "learn_next", "lang",
              "wa_fallback", "wa_fallback_min"):
        if k in patch:
            cur[k] = patch[k]
    cur["enabled"] = bool(cur["enabled"])
    cur["handoff_hours"] = int(_num(cur["handoff_hours"]) or 6)
    cur["wa_fallback"] = bool(cur.get("wa_fallback"))
    cur["wa_fallback_min"] = max(5, int(_num(cur.get("wa_fallback_min")) or 60))
    cur["learn_every_days"] = int(_num(cur.get("learn_every_days")) or 0)
    if not isinstance(cur.get("sources"), list):
        cur["sources"] = []
    kv_save("igbot_settings", cur)
    return cur

# ===== Журнал ошибок и статистика бота =====
def _igbot_log_error(stage, msg):
    """Записать ошибку бота в журнал (последние 40), чтобы показать в пульте."""
    try:
        log = kv_load("igbot_errors") or []
        if not isinstance(log, list):
            log = []
        log.insert(0, {"t": int(time.time()), "stage": stage, "msg": (str(msg) or "")[:300]})
        kv_save("igbot_errors", log[:40])
    except Exception:
        pass

def _igbot_bump(field):
    """Счётчики статистики: replies (ответов бота), handoffs (передач человеку)."""
    try:
        st = kv_load("igbot_stats") or {}
        st[field] = int(st.get(field) or 0) + 1
        st["last_" + field] = int(time.time())
        kv_save("igbot_stats", st)
    except Exception:
        pass

# Товары для бота: тёплый кэш 1С, иначе — ПОСТОЯННАЯ резервная копия каталога из БД (catalog_v1).
# Так бот ВСЕГДА знает ассортимент и цены, даже когда 1С недоступна.
_igbot_goods_cache = {"goods": None, "t": 0.0}
def _igbot_goods():
    live = (_goods or {}).get("goods")
    if live:
        return live
    now = time.time()
    if _igbot_goods_cache["goods"] is not None and now - _igbot_goods_cache["t"] < 600:
        return _igbot_goods_cache["goods"]
    try:
        b = _load_catalog_backup()        # быстрый чтение из БД + распаковка, кэшируем на 10 мин
    except Exception:
        b = None
    g = (b or {}).get("goods") or None
    _igbot_goods_cache["goods"] = g
    _igbot_goods_cache["t"] = now
    return g

def _igbot_history(account, customer, n=12):
    """Последние сообщения диалога с клиентом (по возрастанию времени)."""
    if not supa_on():
        return []
    try:
        rows = _supa("GET", "ig_inbox",
                     "?company_id=eq.%s&or=(sender_id.eq.%s,recipient_id.eq.%s)&order=ts.desc&limit=%d&select=text,direction,raw,ts"
                     % (_q(COMPANY_ID), _q(str(customer)), _q(str(customer)), int(n))) or []
        return list(reversed(rows))
    except Exception:
        return []

def igbot_find_products(text, limit=15):
    """Поиск реальных товаров в каталоге 1С по словам из сообщения клиента (наличие+цена).
    Это «синхронизация с Товарами». ВАЖНО: берём только УЖЕ загруженный кэш каталога —
    НЕ дёргаем 1С во время ответа (иначе ответ клиенту завис бы на тяжёлом каталоге).
    Когда 1С лежит — берём товары из ПОСТОЯННОЙ резервной копии в БД (бот всегда знает ассортимент)."""
    goods = _igbot_goods()
    if not goods:
        return []
    words = [w for w in re.findall(r"[а-яёa-z0-9]+", (text or "").lower()) if len(w) >= 4]
    if not words:
        return []
    stems = set()
    for w in words:
        stems.add(w)
        if len(w) >= 5: stems.add(w[:-1])     # бассейны → бассейн
        if len(w) >= 6: stems.add(w[:-2])
    found = []
    for g in goods:
        title = (g.get("TITLE") or "").lower()
        if title and any(s in title for s in stems):
            found.append(g)
            if len(found) >= limit * 4:
                break
    # сначала то, что в наличии, затем по убыванию остатка
    found.sort(key=lambda g: (0 if _num(g.get("QUANTITY")) > 0 else 1, -_num(g.get("QUANTITY"))))
    out = []
    for g in found[:limit]:
        out.append({"name": (g.get("TITLE") or "").strip(),
                    "price": round(_num(g.get("PRICE"))), "qty": int(_num(g.get("QUANTITY")))})
    return out

def _igbot_generate(history, settings):
    key = CFG.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    msgs = []
    for r in history[-10:]:
        txt = (r.get("text") or "").strip()
        if not txt:
            continue
        msgs.append({"role": "user" if r.get("direction") == "in" else "assistant", "content": txt})
    # схлопнуть подряд идущие одинаковые роли (Anthropic требует чередование)
    merged = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(dict(m))
    if not merged or merged[-1]["role"] != "user":
        return None
    system = settings["prompt"] + (IGBOT_RU_RULES if settings.get("lang") == "ru" else IGBOT_LANG_RULES)
    if settings.get("teach"):
        system += ("\n\n=== ГЛАВНЫЕ УКАЗАНИЯ ВЛАДЕЛЬЦА (это приказы — выполняй в ПЕРВУЮ очередь, они важнее "
                   "базы знаний и всего остального; если тут сказано отвечать определённым образом — отвечай "
                   "именно так) ===\n" + settings["teach"])
    if settings.get("kb"):
        system += ("\n\n=== ЗНАНИЯ ИЗ ПРОШЛЫХ ПЕРЕПИСОК (реальные товары, цены, ответы, политика — "
                   "опирайся на них; если тут есть цена/факт — отвечай уверенно) ===\n" + settings["kb"])
    # Знания из прикреплённых ссылок (видео/посты Instagram — описания товаров/акций)
    srcs = [s for s in (settings.get("sources") or []) if (s.get("text") or "").strip()]
    if srcs:
        sblock = "\n\n".join("• %s\n%s" % ((s.get("title") or s.get("url") or "пост"),
                                           (s.get("text") or "").strip()[:1500]) for s in srcs)
        system += ("\n\n=== ЗНАНИЯ ИЗ ВИДЕО/ПОСТОВ INSTAGRAM (описания товаров и акций — используй их) ===\n" + sblock)
    # ЖИВАЯ СИНХРОНИЗАЦИЯ С ТОВАРАМИ 1С — товары по запросу клиента (наличие + цена)
    try:
        prods = igbot_find_products(merged[-1]["content"])
    except Exception:
        prods = []
    if prods:
        plist = "\n".join("• %s — %s сом — %s" % (
            p["name"], p["price"], ("в наличии: %d шт" % p["qty"] if p["qty"] > 0 else "под заказ"))
            for p in prods)
        system += ("\n\n=== ТОВАРЫ ИЗ КАТАЛОГА (1С, по запросу клиента — РЕАЛЬНЫЕ наличие и цены, "
                   "называй их уверенно, предлагай конкретные позиции) ===\n" + plist)
    # С какой рекламы пришёл клиент (Click-to-WhatsApp) — сразу ведём по этому товару, НЕ переспрашиваем
    try:
        ref = wa_referral_info(history)
    except Exception:
        ref = None
    if ref:
        note = "\n\n=== ОТКУДА ПРИШЁЛ КЛИЕНТ (реклама) ===\n"
        if ref.get("product"):
            note += ("Клиент пришёл с рекламы про «%s». НЕ спрашивай, что ему нужно — ты уже знаешь. "
                     "Сразу веди разговор про %s: предложи подходящую модель, цену и помоги оформить.\n"
                     % (ref["product"], ref["product"].lower()))
        note += "Текст объявления: " + (ref.get("body") or ref.get("headline") or "")[:300]
        system += note
    body = json.dumps({"model": settings["model"], "max_tokens": 400,
                       "system": system, "messages": merged}).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01",
        "content-type": "application/json", "User-Agent": "Shturval/1.0"})
    # Повторяем при ВРЕМЕННЫХ ошибках Claude (перегрузка 529, лимит 429, 500/502/503) —
    # иначе бот молча роняет сообщение клиента и больше не отвечает. Запрос идёт в фоне,
    # поэтому небольшая задержка на повторы не мешает вебхуку.
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01",
                "content-type": "application/json", "User-Agent": "Shturval/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                resp = json.loads(r.read().decode("utf-8"))
            return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
        except urllib.error.HTTPError as e:
            try: detail = e.read().decode()[:200]
            except Exception: detail = str(e)
            last_err = "HTTP %s: %s" % (e.code, detail)
            if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                time.sleep(1.5 * (attempt + 1)); continue
            break
        except Exception as e:
            last_err = str(e)
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1)); continue
            break
    _igbot_log_error("Ответ Claude", last_err or "нет ответа")
    return None

_bot_sent = {}   # (customer, text) -> ts: что бот отправлял (чтобы отличить эхо бота от ответа человека)
def _igbot_mark_sent(customer, text):
    now = time.time()
    _bot_sent[(str(customer), (text or "").strip())] = now
    for k, t in list(_bot_sent.items()):   # чистим старше 10 мин
        if now - t > 600:
            _bot_sent.pop(k, None)

def _igbot_is_own_echo(customer, text):
    return (str(customer), (text or "").strip()) in _bot_sent

def igbot_handle(sender, recipient, text):
    """Сгенерировать и отправить ответ ИИ клиенту. sender = клиент, recipient = наш аккаунт.
    Молчит, если бот выключен ИЛИ в диалоге УЖЕ отвечал человек-оператор (передача человеку — навсегда
    для этого диалога: менеджер ответил хоть раз → бот больше не вмешивается)."""
    try:
        s = _igbot_get()
        if not s["enabled"] or not (CFG.get("ANTHROPIC_API_KEY") and supa_on()):
            return
        hist = _igbot_history(recipient, sender, 40)
        # передача человеку ПО ВРЕМЕНИ: бот молчит, только если ЖИВОЙ оператор отвечал НЕДАВНО
        # (в пределах handoff_hours). Если человек давно не отвечал, а клиент написал снова —
        # бот опять ведёт диалог (раньше он замолкал НАВСЕГДА после единственного ответа менеджера).
        hh = int(_num(s.get("handoff_hours")) or 6)
        cutoff = (time.time() - hh * 3600) * 1000
        for r in hist:
            if (r.get("direction") == "out" and (r.get("raw") or {}).get("by") == "human"
                    and int(_num(r.get("ts")) or 0) >= cutoff):
                _igbot_bump("handoffs")
                return
        reply = _igbot_generate(hist, s)
        if not reply:
            return
        _igbot_mark_sent(sender, reply)
        try:
            ig_send(sender, reply, recipient)
        except Exception as e:
            _igbot_log_error("Отправка в Instagram", str(e))
            return
        _igbot_bump("replies")
        try:
            _supa("POST", "ig_inbox", "",
                  {"company_id": COMPANY_ID, "sender_id": str(recipient), "recipient_id": str(sender),
                   "text": reply, "ts": int(time.time() * 1000), "direction": "out", "raw": {"by": "bot"}})
        except Exception:
            pass
    except Exception as e:
        _igbot_log_error("Обработка сообщения", str(e))

def igbot_learn(limit_msgs=2000):
    """Проанализировать прошлые переписки и собрать БАЗУ ЗНАНИЙ (товары, цены, частые ответы,
    политика) через Claude — подключается к боту как справочник для точных ответов."""
    if not (CFG.get("ANTHROPIC_API_KEY") and supa_on()):
        return {"ok": False, "error": "Нет ключа Claude или базы данных"}
    try:
        rows = _supa("GET", "ig_inbox", "?company_id=eq.%s&order=ts.desc&limit=%d&select=text,direction"
                     % (_q(COMPANY_ID), int(limit_msgs))) or []
    except Exception as e:
        return {"ok": False, "error": str(e)}
    rows = list(reversed(rows))
    lines = []
    for r in rows:
        t = (r.get("text") or "").strip()
        if not t or t.startswith(("📷", "🎬", "📎")):
            continue
        lines.append(("Клиент: " if r.get("direction") == "in" else "Магазин: ") + t)
    transcript = "\n".join(lines)
    if len(transcript) > 70000:
        transcript = transcript[-70000:]
    if not transcript.strip():
        return {"ok": False, "error": "Нет переписок для анализа"}
    instr = (
        "Ниже — реальные переписки магазина BIZMART (Бишкек) с клиентами в Instagram. "
        "Извлеки компактный СПРАВОЧНИК для бота-консультанта. Разделы:\n"
        "1) ТОВАРЫ И ЦЕНЫ — конкретные товары и реально названные цены (с валютой, как в переписках).\n"
        "2) ЧАСТЫЕ ВОПРОСЫ → ЛУЧШИЕ ОТВЕТЫ (доставка, оплата, размеры, наличие, бронь, возврат, гарантия).\n"
        "3) ПОЛИТИКА И ФАКТЫ (доставка, оплата, бонусы, приложение, часы, адрес).\n"
        "4) ВОЗРАЖЕНИЯ и как магазин их закрывает.\n"
        "Только факты из переписок, НЕ выдумывай. По-русски, кратко, списками. Это пойдёт в подсказку боту.\n\n"
        "ПЕРЕПИСКИ:\n" + transcript)
    body = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 3500,
                       "messages": [{"role": "user", "content": instr}]}).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": CFG.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01",
        "content-type": "application/json", "User-Agent": "Shturval/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode("utf-8"))
        kb = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "Claude: " + e.read().decode()[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not kb:
        _igbot_log_error("Обучение", "Пустой ответ анализа")
        return {"ok": False, "error": "Пустой ответ анализа"}
    now = int(time.time())
    s = _igbot_get()
    nxt = (now + s["learn_every_days"] * 86400) if s.get("learn_every_days") else 0
    _igbot_set({"kb": kb, "learn_last": now, "learn_next": nxt})
    return {"ok": True, "kb_len": len(kb), "msgs_analyzed": len(lines)}

# ===== Видео/посты Instagram как источник знаний =====
def _ig_shortcode(url):
    """Достать shortcode из ссылки вида instagram.com/reel/XXXX/ или /p/XXXX/."""
    m = re.search(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ""

def igbot_fetch_source(url):
    """По ссылке на наш пост/Reels Instagram вытащить ОПИСАНИЕ (caption) → в базу знаний бота.
    Бот не «смотрит» видео, но использует текст описания (товары, цены, акции)."""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "Пустая ссылка"}
    code = _ig_shortcode(url)
    title, text = "", ""
    # 1) наш пост в кабинете — берём caption через Graph API (надёжно для своих постов)
    if code and ads_on():
        try:
            iid = ads_ig_id()
            if iid:
                d = _ads_req("GET", str(iid) + "/media",
                             {"fields": "caption,permalink,media_type,timestamp", "limit": 200})
                for m in (d.get("data") or []):
                    if code and code in (m.get("permalink") or ""):
                        text = (m.get("caption") or "").strip()
                        title = "%s · %s" % (m.get("media_type") or "пост", (m.get("timestamp") or "")[:10])
                        break
        except Exception as e:
            _igbot_log_error("Источник (Graph)", str(e))
    # 2) запасной путь — публичное oEmbed-описание страницы
    if not text:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Shturval bot)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                html = r.read(200000).decode("utf-8", "ignore")
            m = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html)
            if m:
                text = re.sub(r"&quot;", '"', re.sub(r"&#039;", "'", m.group(1))).strip()
        except Exception as e:
            _igbot_log_error("Источник (страница)", str(e))
    if not text:
        return {"ok": False, "error": "Не удалось получить описание поста. Это ваш пост? Можно вставить текст вручную."}
    s = _igbot_get()
    srcs = [x for x in (s.get("sources") or []) if x.get("url") != url]
    srcs.insert(0, {"url": url, "title": title or "Пост Instagram", "text": text[:2000], "added": int(time.time())})
    _igbot_set({"sources": srcs[:30]})
    return {"ok": True, "title": title, "text": text[:400], "count": len(srcs[:30])}

def igbot_source_del(url):
    s = _igbot_get()
    srcs = [x for x in (s.get("sources") or []) if x.get("url") != (url or "")]
    _igbot_set({"sources": srcs})
    return {"ok": True, "count": len(srcs)}

def _igbot_scheduler():
    """Фоновый поток: авто-обучение по расписанию (learn_every_days), запасной ответ
    бота в WhatsApp (через час без ответа человека) и подстраховка каталога в БД."""
    while True:
        try:
            s = _igbot_get()
            now = int(time.time())
            if s.get("learn_every_days") and s.get("learn_next") and now >= s["learn_next"]:
                igbot_learn()
        except Exception:
            pass
        try:
            _wa_fallback_sweep()      # WhatsApp: ответить клиентам, ждущим дольше wa_fallback_min
        except Exception:
            pass
        time.sleep(300)   # проверяем каждые 5 минут

def _ig_eligible_targets(account=None, max_age_hours=24):
    """Диалоги в пределах 24-часового окна Instagram (можно писать первым)."""
    now = time.time(); cutoff = now - max_age_hours * 3600
    out = []
    for c in ig_conversations():
        ts = c.get("last_ts") or 0
        ts_sec = ts / 1000.0 if ts > 1e12 else ts
        if ts_sec < cutoff:
            continue
        if account and account not in ("all", "") and c.get("account") != account:
            continue
        out.append(c)
    return out

def ig_broadcast_count(account=None):
    targets = _ig_eligible_targets(account)
    per = {}
    for c in targets:
        per[c["account"]] = per.get(c["account"], 0) + 1
    return {"eligible": len(targets), "per_account": per}

def ig_broadcast(text, account=None, max_age_hours=24, limit=500):
    targets = _ig_eligible_targets(account, max_age_hours)[:limit]
    sent = 0; failed = 0
    for c in targets:
        try:
            ig_reply(c["account_id"], c["customer_id"], text)
            sent += 1
            time.sleep(0.5)               # бережно к лимитам Instagram
        except Exception:
            failed += 1
    return {"eligible": len(targets), "sent": sent, "failed": failed}

# ===== WhatsApp напрямую (WhatsApp Cloud API от Meta), без посредников =====
# Тот же Meta Graph API, что и реклама. Один Штурвал — несколько номеров (phone_id).
# Хранение в БД зеркалит Instagram: таблицы wa_accounts / wa_inbox / wa_names.
WA_GRAPH = "https://graph.facebook.com/v21.0"

def wa_accounts():
    """Все подключённые WhatsApp-номера компании: {phone_id, display, token}.
    Если в БД пусто, но номер задан в окружении (WA_PHONE_ID) — показываем его (одиночный режим)."""
    rows = []
    if supa_on():
        try:
            rows = _supa("GET", "wa_accounts",
                         "?company_id=eq.%s&select=*" % _q(COMPANY_ID)) or []
        except Exception:
            rows = []
    if not rows and CFG.get("WA_PHONE_ID"):
        rows = [{"phone_id": CFG.get("WA_PHONE_ID"),
                 "display": CFG.get("WA_DISPLAY") or "WhatsApp",
                 "token": CFG.get("WA_TOKEN", "")}]
    return rows

def wa_account_map():
    """phone_id -> запись номера (для маршрутизации сообщений по номерам)."""
    return {str(a.get("phone_id")): a for a in wa_accounts() if a.get("phone_id")}

def wa_token_for(phone_id):
    """Токен того номера, НА который пришло сообщение (phone_id из webhook)."""
    a = wa_account_map().get(str(phone_id))
    if a and a.get("token"):
        return a["token"]
    return CFG.get("WA_TOKEN", "")              # запасной (одиночный номер из конфига)

def wa_send(to, text, from_phone_id=None):
    """Отправить сообщение в WhatsApp. from_phone_id = НАШ номер (тот, на который писал клиент)."""
    phone_id = str(from_phone_id or CFG.get("WA_PHONE_ID", "") or "")
    token = wa_token_for(phone_id) if phone_id else CFG.get("WA_TOKEN", "")
    if not (token and phone_id):
        raise RuntimeError("WhatsApp пока не подключён (нет токена или номера). Сначала настройте приложение Meta.")
    url = WA_GRAPH + "/" + _q(phone_id) + "/messages"
    body = json.dumps({"messaging_product": "whatsapp", "to": str(to),
                       "type": "text", "text": {"body": text}}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token,
                                          "User-Agent": "Shturval/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")

# поддерживаемые WhatsApp типы аудио (для голосовых). webm (Chrome) Meta НЕ принимает.
WA_AUDIO_OK = ("audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/amr")

def wa_media_type(mime):
    """Свести MIME-тип файла к типу сообщения WhatsApp."""
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "sticker" if "webp" in m else "image"
    if m.startswith("video/"):
        return "video"
    if m.startswith("audio/"):
        return "audio"
    return "document"

def wa_upload_media(phone_id, data, mime, filename="file"):
    """Загрузить байты в Meta (/media) и вернуть media_id для последующей отправки."""
    token = wa_token_for(phone_id) if phone_id else CFG.get("WA_TOKEN", "")
    if not (token and phone_id):
        raise RuntimeError("WhatsApp не подключён (нет токена или номера).")
    boundary = "----shturval%d" % int(time.time() * 1000)
    def field(name, val):
        return ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary, name, val)).encode("utf-8")
    head = field("messaging_product", "whatsapp") + field("type", mime or "application/octet-stream")
    fhdr = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
            % (boundary, filename, mime or "application/octet-stream")).encode("utf-8")
    body = head + fhdr + data + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    url = WA_GRAPH + "/" + _q(phone_id) + "/media"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "multipart/form-data; boundary=" + boundary,
                                          "User-Agent": "Shturval/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}").get("id", "")

def wa_send_media(to, mtype, media_id, from_phone_id=None, caption="", filename=""):
    """Отправить медиа-сообщение по уже загруженному media_id."""
    phone_id = str(from_phone_id or CFG.get("WA_PHONE_ID", "") or "")
    token = wa_token_for(phone_id) if phone_id else CFG.get("WA_TOKEN", "")
    if not (token and phone_id):
        raise RuntimeError("WhatsApp не подключён (нет токена или номера).")
    obj = {"id": media_id}
    if caption and mtype in ("image", "video", "document"):
        obj["caption"] = caption
    if filename and mtype == "document":
        obj["filename"] = filename
    url = WA_GRAPH + "/" + _q(phone_id) + "/messages"
    body = json.dumps({"messaging_product": "whatsapp", "to": str(to),
                       "type": mtype, mtype: obj}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token,
                                          "User-Agent": "Shturval/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")

def wa_reply_media(account_id, customer_id, mtype, media_id, caption="", filename="", mime="", by="human"):
    """Оператор отправил клиенту медиа: отправить и сохранить в историю."""
    resp = wa_send_media(customer_id, mtype, media_id, account_id, caption=caption, filename=filename)
    try:
        _supa("POST", "wa_inbox", "",
              {"company_id": COMPANY_ID, "sender_id": str(account_id),
               "recipient_id": str(customer_id), "text": caption or "", "mid": _wamid_of(resp),
               "ts": int(time.time() * 1000), "direction": "out", "status": "sent",
               "raw": {"by": by, "type": mtype, "media_id": media_id,
                       "mime": mime, "filename": filename, "caption": caption}})
    except Exception:
        pass

_FFMPEG_EXE = None
_FFMPEG_TRIED = False
def _ffmpeg_exe():
    """Путь к статическому ffmpeg (из pip-пакета imageio-ffmpeg). None, если недоступен."""
    global _FFMPEG_EXE, _FFMPEG_TRIED
    if _FFMPEG_TRIED:
        return _FFMPEG_EXE
    _FFMPEG_TRIED = True
    try:
        import imageio_ffmpeg
        _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        try:
            import shutil
            _FFMPEG_EXE = shutil.which("ffmpeg")
        except Exception:
            _FFMPEG_EXE = None
    return _FFMPEG_EXE

_audio_mp3_cache = {}   # media_id -> mp3 bytes (чтобы не перекодировать при каждом проигрывании)
def wa_audio_to_mp3(data, media_id=None):
    """Перекодировать аудио (ogg/opus, webm, amr…) в MP3 для Safari/всех браузеров.
    Возвращает (mp3_bytes, 'audio/mpeg') либо (data, None) если перекодировать не удалось."""
    if media_id and media_id in _audio_mp3_cache:
        return _audio_mp3_cache[media_id], "audio/mpeg"
    exe = _ffmpeg_exe()
    if not exe:
        return data, None
    try:
        import subprocess
        p = subprocess.run([exe, "-hide_banner", "-loglevel", "error",
                            "-i", "pipe:0", "-vn", "-c:a", "libmp3lame", "-b:a", "96k",
                            "-f", "mp3", "pipe:1"],
                           input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        out = p.stdout
        if p.returncode == 0 and out and len(out) > 200:
            if media_id and len(out) < 5_000_000:
                _audio_mp3_cache[media_id] = out
                if len(_audio_mp3_cache) > 200:
                    _audio_mp3_cache.pop(next(iter(_audio_mp3_cache)), None)
            return out, "audio/mpeg"
    except Exception as e:
        try: print("ffmpeg transcode failed:", e)
        except Exception: pass
    return data, None

def wa_media_download(media_id, phone_id=None):
    """Скачать медиа по media_id из Meta. Возвращает (байты, mime).
    Голосовые WhatsApp приходят в ogg/opus — Safari их не играет, поэтому перекодируем в MP3."""
    token = wa_token_for(str(phone_id or "")) if phone_id else CFG.get("WA_TOKEN", "")
    if not token:
        token = CFG.get("WA_TOKEN", "")
    if not token:
        raise RuntimeError("нет токена WhatsApp")
    req1 = urllib.request.Request(WA_GRAPH + "/" + _q(str(media_id)),
                                  headers={"Authorization": "Bearer " + token, "User-Agent": "Shturval/1.0"})
    with urllib.request.urlopen(req1, timeout=30) as r:
        meta = json.loads(r.read().decode() or "{}")
    murl = meta.get("url", ""); mime = meta.get("mime_type", "application/octet-stream")
    if not murl:
        raise RuntimeError("медиа недоступно")
    req2 = urllib.request.Request(murl, headers={"Authorization": "Bearer " + token, "User-Agent": "Shturval/1.0"})
    with urllib.request.urlopen(req2, timeout=60) as r:
        data = r.read()
    # Перекодируем «неудобные» аудиоформаты в MP3 (ogg/opus от клиентов, webm, amr, aac в ADTS).
    low = (mime or "").lower()
    if low.startswith("audio/") and not low.startswith("audio/mpeg") and "mp3" not in low:
        mp3, m2 = wa_audio_to_mp3(data, str(media_id))
        if m2:
            return mp3, m2
    return data, mime

def wa_business_profile(phone_id=None):
    """Бизнес-профиль номера (как видят клиенты) + сведения о номере."""
    phone_id = str(phone_id or CFG.get("WA_PHONE_ID", "") or "")
    token = wa_token_for(phone_id) if phone_id else CFG.get("WA_TOKEN", "")
    if not (token and phone_id):
        raise RuntimeError("WhatsApp не подключён (нет токена или номера).")
    hdr = {"Authorization": "Bearer " + token, "User-Agent": "Shturval/1.0"}
    fields = "about,address,description,email,profile_picture_url,websites,vertical"
    req = urllib.request.Request(WA_GRAPH + "/" + _q(phone_id) + "/whatsapp_business_profile?fields=" + fields, headers=hdr)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = (json.loads(r.read().decode() or "{}").get("data") or [])
    prof = data[0] if data else {}
    req2 = urllib.request.Request(WA_GRAPH + "/" + _q(phone_id)
                                  + "?fields=verified_name,display_phone_number,quality_rating,code_verification_status,platform_type", headers=hdr)
    with urllib.request.urlopen(req2, timeout=20) as r:
        num = json.loads(r.read().decode() or "{}")
    return {"profile": prof, "number": num}

# имена клиентов по их WhatsApp-номеру (приходят прямо в webhook → живых запросов не нужно).
_wa_names = {}
_wa_names_ts = 0
def wa_load_names():
    global _wa_names_ts
    if time.time() - _wa_names_ts < 300 and _wa_names:
        return
    try:
        off = 0
        while True:
            rows = _supa("GET", "wa_names",
                         "?company_id=eq.%s&select=waid,name&order=waid&limit=1000&offset=%d"
                         % (_q(COMPANY_ID), off)) or []
            for row in rows:
                _wa_names[str(row.get("waid"))] = row.get("name") or ""
            if len(rows) < 1000:
                break
            off += 1000
        _wa_names_ts = time.time()
    except Exception:
        pass

def wa_customer_name(waid):
    wa_load_names()
    return _wa_names.get(str(waid), "")

def wa_save_name(waid, name):
    if not name:
        return
    _wa_names[str(waid)] = name
    try:
        _supa("POST", "wa_names", "?on_conflict=company_id,waid",
              {"company_id": COMPANY_ID, "waid": str(waid), "name": name})
    except Exception:
        pass

# защита от повторов: Meta переотправляет webhook, пока не получит 200 → не дублируем входящие
_wa_seen = set()
def _wa_seen_mid(mid):
    if not mid:
        return False
    if mid in _wa_seen:
        return True
    # ПЕРЕЖИВАЕМ РЕСТАРТ: память обнуляется на free-плане Render, а Meta переотправляет
    # webhook, пока не получит 200. Поэтому при промахе памяти проверяем БД — если это
    # сообщение уже сохранено, значит это повтор, и мы его не дублируем.
    try:
        if supa_on():
            rows = _supa("GET", "wa_inbox", "?mid=eq.%s&select=mid&limit=1" % _q(str(mid)))
            if rows:
                _wa_seen.add(mid)
                return True
    except Exception:
        pass
    _wa_seen.add(mid)
    if len(_wa_seen) > 5000:
        _wa_seen.clear()
    return False

_WA_TYPE_LABEL = {"image": "📷 фото", "video": "🎬 видео", "audio": "🎤 голосовое",
                  "voice": "🎤 голосовое", "document": "📎 документ", "sticker": "💟 стикер",
                  "location": "📍 геолокация", "contacts": "👤 контакт"}

def wa_referral_info(rows):
    """Из строк диалога достаёт рекламный referral (Click-to-WhatsApp) и определяет товар.
    referral приходит в первом сообщении клиента, пришедшего по рекламе WhatsApp/Instagram."""
    for r in rows:
        ref = (r.get("raw") or {}).get("referral")
        if ref:
            t = ((ref.get("headline") or "") + " " + (ref.get("body") or "") + " "
                 + (ref.get("source_url") or "")).lower()
            prod = "Бассейн" if ("бассей" in t or "басей" in t) else ("Чемоданы" if "чемодан" in t else "")
            return {"product": prod, "headline": ref.get("headline", "") or "",
                    "body": (ref.get("body", "") or ""), "url": ref.get("source_url", "") or "",
                    "ad_id": ref.get("source_id", "") or ""}
    return None

def wa_store_event(evt):
    """Разобрать webhook WhatsApp Cloud API и сохранить входящие сообщения в wa_inbox.
    Статусы (доставлено/прочитано) игнорируем — это не сообщения."""
    if not isinstance(evt, dict):
        return
    for entry in (evt.get("entry") or []):
        for ch in (entry.get("changes") or []):
            val = ch.get("value") or {}
            if val.get("messaging_product") != "whatsapp":
                continue
            phone_id = str((val.get("metadata") or {}).get("phone_number_id") or "")
            # имена клиентов приходят прямо в webhook (contacts) — сохраняем сразу, без доп. запросов
            for ct in (val.get("contacts") or []):
                waid = str(ct.get("wa_id") or "")
                nm = ((ct.get("profile") or {}).get("name") or "").strip()
                if waid and nm and _wa_names.get(waid) != nm:
                    wa_save_name(waid, nm)
            # Статусы НАШИХ исходящих (доставлено/прочитано) — обновляем по wamid, без понижения
            _ST_RANK = {"sent": 1, "delivered": 2, "read": 3}
            for st in (val.get("statuses") or []):
                wamid = st.get("id"); status = st.get("status")
                if not (wamid and status in _ST_RANK):
                    continue
                lower = [s for s, rk in _ST_RANK.items() if rk < _ST_RANK[status]]
                flt = "?mid=eq.%s&direction=eq.out" % _q(str(wamid))
                flt += ("&status=in.(%s)" % ",".join(lower)) if lower else "&status=is.null"
                try:
                    _supa("PATCH", "wa_inbox", flt, {"status": status})
                except Exception:
                    pass
            for msg in (val.get("messages") or []):
                sender = str(msg.get("from") or "")            # клиент
                if not sender or _wa_seen_mid(msg.get("id", "")):
                    continue
                mtype = msg.get("type") or "text"
                text = ""
                if mtype == "text":
                    text = (msg.get("text") or {}).get("body", "")
                elif mtype == "button":
                    text = (msg.get("button") or {}).get("text", "")
                elif mtype == "interactive":
                    inter = msg.get("interactive") or {}
                    text = (inter.get("button_reply") or inter.get("list_reply") or {}).get("title", "")
                row = {"company_id": COMPANY_ID, "sender_id": sender, "recipient_id": phone_id,
                       "mid": msg.get("id", ""), "text": text,
                       "ts": int(str(msg.get("timestamp") or "0") or 0) * 1000,
                       "direction": "in", "raw": msg}
                _inbox_save("wa_inbox", row)
                # ИИ-бот «Акылай»: отвечает в фоне с паузой ~5 сек (вдруг клиент допишет ещё)
                if text and phone_id:
                    _wabot_schedule(sender, phone_id)

def _wa_pair(r):
    """(наш номер, клиент) из строки wa_inbox независимо от направления."""
    if r.get("direction") == "out":
        return str(r.get("sender_id")), str(r.get("recipient_id"))
    return str(r.get("recipient_id")), str(r.get("sender_id"))

def wa_rows(limit=1000):
    if not supa_on():
        return []
    try:
        return _supa("GET", "wa_inbox",
                     "?company_id=eq.%s&select=*&order=ts.desc,id.desc&limit=%d" % (_q(COMPANY_ID), limit)) or []
    except Exception:
        return []

def _wa_disp_text(r):
    t = r.get("text") or ""
    if t:
        return t
    typ = (r.get("raw") or {}).get("type") or ""
    return _WA_TYPE_LABEL.get(typ, "📎 вложение") if typ and typ != "text" else ""

def _build_wa_conversations():
    amap = wa_account_map()
    rows = wa_rows()
    convos = {}
    for r in rows:                            # rows уже по убыванию времени — первый = последний
        acc, cust = _wa_pair(r)
        key = acc + "|" + cust
        if key not in convos:
            convos[key] = {"account_id": acc, "customer_id": cust,
                           "account": (amap.get(acc) or {}).get("display", acc),
                           "channel": "wa",
                           "last_text": _wa_disp_text(r), "last_ts": int(r.get("ts") or 0),
                           "last_dir": r.get("direction", "in"), "count": 0}
        convos[key]["count"] += 1
    out = sorted(convos.values(), key=lambda c: c["last_ts"], reverse=True)
    for c in out:
        c["customer"] = wa_customer_name(c["customer_id"]) or ("+" + c["customer_id"])
    return out

def wa_conversations():
    """Список диалогов WhatsApp (кэш 5с, имена — из БД, без живых запросов → быстро)."""
    return cached("wa_convos", 5, _build_wa_conversations)

def wa_clients_base():
    """База клиентов WhatsApp: номер телефона (wa_id) и имя (если известно)."""
    out = []
    for c in wa_conversations():
        nm = c.get("customer") or ""
        if nm.startswith("+"):          # это просто номер, имени нет
            nm = ""
        out.append({"phone": c.get("customer_id") or "", "name": nm,
                    "last_ts": c.get("last_ts") or 0, "count": c.get("count") or 0})
    return out

def wa_thread(account_id, customer_id):
    """Сообщения одного диалога по возрастанию времени."""
    msgs = []; pair_rows = []
    for r in wa_rows():
        acc, cust = _wa_pair(r)
        if acc == str(account_id) and cust == str(customer_id):
            pair_rows.append(r)
            ts = int(r.get("ts") or 0)
            raw = r.get("raw") or {}
            typ = raw.get("type") or ""
            med = ""; mime = ""; fname = ""; cap = ""
            if typ in ("image", "audio", "video", "document", "sticker"):
                sub = raw.get(typ) or {}
                med = sub.get("id") or raw.get("media_id") or ""
                mime = sub.get("mime_type") or raw.get("mime") or ""
                fname = sub.get("filename") or raw.get("filename") or ""
                cap = sub.get("caption") or raw.get("caption") or ""
            if med:
                x = cap                                # реальная подпись, без авто-ярлыка
            else:
                x = r.get("text", "") or (_WA_TYPE_LABEL.get(typ, "") if typ and typ != "text" else "")
            msgs.append({"t": ("out" if r.get("direction") == "out" else "in"),
                         "x": x, "ts": ts, "mtype": typ, "med": med, "mime": mime, "fname": fname,
                         "st": (r.get("status") or ""),
                         "tm": time.strftime("%d.%m %H:%M", time.localtime(ts / 1000)) if ts > 1e12
                               else (time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "")})
    msgs.sort(key=lambda m: m["ts"])
    return {"msgs": msgs, "customer": wa_customer_name(customer_id) or ("+" + str(customer_id)),
            "source": wa_referral_info(pair_rows)}

def _wamid_of(resp):
    try:
        return ((resp or {}).get("messages") or [{}])[0].get("id", "") or ""
    except Exception:
        return ""

def wa_mark_read(phone_id, wamid):
    """Отметить входящее сообщение прочитанным — клиент увидит, что мы прочитали (синие галочки)."""
    phone_id = str(phone_id or CFG.get("WA_PHONE_ID", "") or "")
    token = wa_token_for(phone_id) if phone_id else CFG.get("WA_TOKEN", "")
    if not (token and phone_id and wamid):
        return
    url = WA_GRAPH + "/" + _q(phone_id) + "/messages"
    body = json.dumps({"messaging_product": "whatsapp", "status": "read",
                       "message_id": str(wamid)}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token,
                                          "User-Agent": "Shturval/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:
        return None

def wa_reply(account_id, customer_id, text, by="human"):
    """Ответить клиенту с нужного номера и сохранить в историю.
    by='human' (ручной ответ оператора) → ИИ-бот делает паузу в этом диалоге."""
    resp = wa_send(customer_id, text, account_id)
    try:
        _supa("POST", "wa_inbox", "",
              {"company_id": COMPANY_ID, "sender_id": str(account_id),
               "recipient_id": str(customer_id), "text": text, "mid": _wamid_of(resp),
               "ts": int(time.time() * 1000), "direction": "out", "status": "sent",
               "raw": {"by": by, "type": "text"}})
    except Exception:
        pass

def wa_send_template(to, template_name, from_phone_id=None, lang="ru"):
    """Отправить ШАБЛОННОЕ сообщение — можно писать клиенту ВНЕ окна 24 часов (после его
    ответа снова открывается обычное окно). Шаблон должен быть одобрен Meta."""
    phone_id = str(from_phone_id or CFG.get("WA_PHONE_ID", "") or "")
    token = wa_token_for(phone_id) if phone_id else CFG.get("WA_TOKEN", "")
    if not (token and phone_id):
        raise RuntimeError("WhatsApp не подключён (нет токена или номера).")
    url = WA_GRAPH + "/" + _q(phone_id) + "/messages"
    body = json.dumps({"messaging_product": "whatsapp", "to": str(to), "type": "template",
                       "template": {"name": template_name, "language": {"code": lang}}}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token, "User-Agent": "Shturval/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")

def wa_reply_template(account_id, customer_id, template_name, lang="ru", by="human", preview=""):
    """Отправить клиенту шаблон и сохранить в историю (чтобы показать в чате)."""
    resp = wa_send_template(customer_id, template_name, account_id, lang)
    try:
        _supa("POST", "wa_inbox", "",
              {"company_id": COMPANY_ID, "sender_id": str(account_id),
               "recipient_id": str(customer_id), "text": preview or ("📋 шаблон: " + template_name),
               "mid": _wamid_of(resp), "ts": int(time.time() * 1000), "direction": "out",
               "status": "sent", "raw": {"by": by, "type": "template"}})
    except Exception:
        pass
    return resp

# ====== ИИ-БОТ WhatsApp — тот же «Акылай», что и в Instagram (общие настройки и правила) ======
def _wabot_history(phone_id, customer, n=40):
    """Последние сообщения диалога с клиентом (по возрастанию времени)."""
    if not supa_on():
        return []
    try:
        rows = _supa("GET", "wa_inbox",
                     "?company_id=eq.%s&or=(sender_id.eq.%s,recipient_id.eq.%s)&order=ts.desc&limit=%d&select=text,direction,raw,ts"
                     % (_q(COMPANY_ID), _q(str(customer)), _q(str(customer)), int(n))) or []
        return list(reversed(rows))
    except Exception:
        return []

# Пауза перед ответом бота: ждём WABOT_DELAY сек — вдруг клиент допишет ещё. Каждое новое
# сообщение сбрасывает таймер; по истечении тишины бот отвечает ОДИН раз (читает всю историю).
WABOT_DELAY = 5.0
_wabot_timers = {}
_wabot_lock = threading.Lock()
def _wabot_schedule(sender, phone_id):
    key = (str(phone_id), str(sender))
    with _wabot_lock:
        t = _wabot_timers.get(key)
        if t:
            t.cancel()
        nt = threading.Timer(WABOT_DELAY, _wabot_fire, args=(sender, phone_id, key))
        nt.daemon = True
        _wabot_timers[key] = nt
        nt.start()
def _wabot_fire(sender, phone_id, key):
    with _wabot_lock:
        _wabot_timers.pop(key, None)
    try:
        wabot_handle(sender, phone_id, None)
    except Exception:
        pass

def wabot_handle(sender, phone_id, text=None, fallback=False):
    """ИИ-бот отвечает клиенту в WhatsApp. sender=клиент, phone_id=наш номер.
    Те же настройки/правила, что у Instagram-бота.
    Два режима:
    • Мгновенный (fallback=False) — бот отвечает через ~5 сек. Если оператор ответил
      в диалоге хоть раз — бот больше не вмешивается.
    • Запасной/«через час» (fallback=True, по умолчанию для WhatsApp) — мгновенно НЕ
      отвечаем; через периодический обход бот пишет сам, только если последнее сообщение
      клиента провисело без ответа дольше wa_fallback_min минут (см. _wa_fallback_sweep)."""
    try:
        s = _igbot_get()
        if not s["enabled"] or not (CFG.get("ANTHROPIC_API_KEY") and supa_on()):
            return
        # В режиме «через час» мгновенный ответ не шлём — пусть сначала ответит человек.
        if s.get("wa_fallback") and not fallback:
            return
        hist = _wabot_history(phone_id, sender, 40)
        if fallback:
            # Подстраховка от гонки: к моменту обхета человек мог уже ответить.
            if not hist or hist[-1].get("direction") == "out":
                return
        else:
            # Мгновенный режим: бот молчит, только если ЖИВОЙ менеджер отвечал НЕДАВНО
            # (в пределах handoff_hours). Если человек давно не отвечал — бот снова ведёт диалог.
            hh = int(_num(s.get("handoff_hours")) or 6)
            cutoff = (time.time() - hh * 3600) * 1000
            for r in hist:
                if (r.get("direction") == "out" and (r.get("raw") or {}).get("by") == "human"
                        and int(_num(r.get("ts")) or 0) >= cutoff):
                    _igbot_bump("handoffs")
                    return
        reply = _igbot_generate(hist, s)
        if not reply:
            return
        try:
            resp = wa_send(sender, reply, phone_id)
        except Exception as e:
            _igbot_log_error("Отправка в WhatsApp", str(e))
            return
        _igbot_bump("replies")
        try:
            _supa("POST", "wa_inbox", "",
                  {"company_id": COMPANY_ID, "sender_id": str(phone_id), "recipient_id": str(sender),
                   "text": reply, "mid": _wamid_of(resp), "ts": int(time.time() * 1000),
                   "direction": "out", "status": "sent", "raw": {"by": "bot", "type": "text"}})
        except Exception:
            pass
    except Exception as e:
        _igbot_log_error("WhatsApp: обработка", str(e))

def _wa_fallback_sweep():
    """Запасной ответ бота в WhatsApp: если последнее сообщение в диалоге — от КЛИЕНТА
    и провисело без ответа дольше wa_fallback_min минут (по умолчанию 60), «Акылай»
    отвечает сам. Верхняя граница — 24 часа: вне 24-часового окна WhatsApp свободным
    текстом писать нельзя, да и поздно реагировать. Запускается из планировщика."""
    try:
        s = _igbot_get()
        if not s.get("enabled") or not s.get("wa_fallback"):
            return
        if not (CFG.get("ANTHROPIC_API_KEY") and supa_on()):
            return
        mins = max(5, int(s.get("wa_fallback_min") or 60))
        now = time.time()
        lo = mins * 60          # минимум висит без ответа
        hi = 24 * 3600          # окно WhatsApp — позже писать нельзя
        done = 0
        for c in wa_conversations():
            if c.get("last_dir") != "in":         # последним писал не клиент — отвечать некому
                continue
            ts = c.get("last_ts") or 0
            ts_sec = ts / 1000.0 if ts > 1e12 else ts
            age = now - ts_sec
            if age < lo or age > hi:
                continue
            try:
                wabot_handle(c.get("customer_id"), c.get("account_id"), None, fallback=True)
                done += 1
                time.sleep(1.0)                   # бережно к лимитам WhatsApp
            except Exception:
                pass
            if done >= 30:                        # за один проход — не больше 30 ответов
                break
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
_goods = {"t": 0.0, "goods": None, "cats": None, "cats_t": 0.0, "refreshing": False,
          "next_try": 0.0, "fail_n": 0}
_goods_lock = threading.Lock()
CATALOG_TTL = 1800          # каталог меняется редко — обновляем не чаще раза в 30 мин (бережём 1С)

def _fetch_goods():
    return yaros_get("/goods").get("goods", [])

def _fetch_cats():
    data = yaros_get("/categories")
    return {c.get("ID"): (c.get("TITLE") or "").strip() for c in data.get("categories", [])}

def _refresh_catalog(force=False):
    """Качает свежий каталог и АТОМАРНО заменяет кэш. НЕ держит общую блокировку —
    поэтому пользовательские запросы во время обновления отвечают мгновенно из старого кэша.
    САМ СЕБЯ ТРОТТЛИТ: успешную загрузку повторяет не чаще раза в 30 мин, а при сбоях
    1С отступает всё дольше (5→30 мин). Это резко снижает нагрузку на хрупкую 1С —
    раньше тянули 20 МБ каждые ~2 мин и сами её роняли по памяти."""
    if _goods["refreshing"]:
        return
    now = time.time()
    if not force and now < _goods.get("next_try", 0):
        return                           # ещё рано — не долбим 1С
    _goods["refreshing"] = True
    try:
        goods = _fetch_goods()           # ~50 сек, но в фоне
        _goods["goods"] = goods          # подменяем разом
        _goods["t"] = now
        _goods["from_backup"] = False    # данные снова живые из 1С
        _goods["fail_n"] = 0
        _goods["next_try"] = now + CATALOG_TTL
        try:
            _goods["cats"] = _fetch_cats()
            _goods["cats_t"] = now
        except Exception:
            pass
        _save_catalog_backup(_goods["goods"], _goods["cats"])   # резервная копия в базу
    except Exception:
        # 1С не отдала каталог (обычно нехватка памяти на /goods) — отступаем всё дальше,
        # чтобы не добивать сервер: 5, 10, 20, 30 мин (максимум).
        _goods["fail_n"] = _goods.get("fail_n", 0) + 1
        _goods["next_try"] = now + min(CATALOG_TTL, 300 * (2 ** min(_goods["fail_n"] - 1, 3)))
    finally:
        _goods["refreshing"] = False

def get_goods():
    """Мгновенно отдаёт каталог из памяти. Если устарел — обновляет ФОНОМ, не задерживая ответ.
    Ждать (~50 сек) приходится только при самой первой загрузке, когда кэша ещё нет."""
    if _goods["goods"] is not None:
        if time.time() >= _goods.get("next_try", 0) and not _goods["refreshing"]:
            threading.Thread(target=_refresh_catalog, daemon=True).start()
        return _goods["goods"]
    with _goods_lock:                    # самый первый раз — приходится дождаться
        if _goods["goods"] is not None:
            return _goods["goods"]
        try:
            _goods["goods"] = _fetch_goods()
            _goods["t"] = time.time()
        except Exception:
            b = _load_catalog_backup()   # 1С недоступна на холодном старте — отдаём копию из базы
            if b and b.get("goods"):
                _goods["goods"] = b["goods"]
                _goods["t"] = b.get("t", 0)        # старая метка → фоновый рефреш обновит, когда 1С ответит
                _goods["from_backup"] = True       # пометка «данные из резервной копии»
                if _goods["cats"] is None and b.get("cats"):
                    _goods["cats"] = b["cats"]
                threading.Thread(target=_refresh_catalog, daemon=True).start()
                return _goods["goods"]
            raise
        return _goods["goods"]

def get_categories():
    if _goods["cats"] is None:
        try:
            _goods["cats"] = _fetch_cats()
            _goods["cats_t"] = time.time()
        except Exception:
            b = _load_catalog_backup()        # 1С молчит — берём категории из резервной копии
            _goods["cats"] = (b or {}).get("cats") or {}
    return _goods["cats"]

# ---- Резервная копия каталога в базе (переживает сон сервера и сбой 1С) ----
# Полный каталог (20 МБ) хранится только в памяти и теряется при перезапуске Render.
# Поэтому после каждой удачной загрузки сохраняем СЖАТУЮ копию нужных полей в Supabase
# (kv_cache, ключ catalog_v1). При холодном старте/сбое 1С отдаём её — без «1С недоступна».
_CAT_FIELDS = ("TITLE", "CATEGORY_ID", "PRICE", "QUANTITY", "PRICES")
_catalog_backup_t = {"t": 0.0}

def _save_catalog_backup(goods, cats):
    """Сжать каталог (только нужные поля) и положить в базу. Не чаще раза в 30 мин —
    чтобы не гонять мегабайты при каждом фоновом обновлении (каждые ~2 мин)."""
    if not supa_on() or not goods:
        return
    if time.time() - _catalog_backup_t["t"] < 1800:
        return
    try:
        slim = [{k: g.get(k) for k in _CAT_FIELDS} for g in goods]
        payload = json.dumps({"goods": slim, "cats": cats or {}}, ensure_ascii=False).encode("utf-8")
        packed = base64.b64encode(gzip.compress(payload, 6)).decode("ascii")
        kv_save("catalog_v1", {"z": packed, "n": len(slim), "t": int(time.time())})
        _catalog_backup_t["t"] = time.time()
    except Exception:
        pass

def _load_catalog_backup():
    """Достать каталог из резервной копии в базе → {'goods':[...], 'cats':{...}, 't':unix}.
    None, если копии нет."""
    try:
        v = kv_load("catalog_v1")
        if not v or not v.get("z"):
            return None
        raw = gzip.decompress(base64.b64decode(v["z"])).decode("utf-8")
        d = json.loads(raw)
        d["t"] = v.get("t", 0)
        return d
    except Exception:
        return None

_finalize_state = {"date": ""}

def _finalize_recent_days():
    """Раз в день дотягивает прошлые дни ПОЛНОСТЬЮ из 1С (фильтр from/to) и
    перезаписывает их. Чинит занижение: дневной снимок сохранялся ВО ВРЕМЯ дня
    (частичный), а потом день «застывал» неполным. Теперь каждый завершённый день
    добирается начисто. Срабатывает один раз в сутки и только при удачной выгрузке."""
    today = _today_str()
    if _finalize_state["date"] == today:
        return
    try:
        res = backfill_sales(8, overwrite=True)   # последние 8 завершённых дней — начисто
        if not res.get("failed"):                 # дотянули без ошибок — на сегодня готово
            _finalize_state["date"] = today
    except Exception:
        pass

def _warm_sales():
    try:
        if CFG.get("YAROS_URL"):
            s = cached("sales", 30, build_sales)
            _save_sales_daily(s)
            _finalize_recent_days()               # раз в день дочинить прошлые дни начисто
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
            jobs.append(_assort_refresh)      # снимок анализа ассортимента (сам троттлит, раз в 6 ч)
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
    if _goods.get("from_backup"):      # каталог взят из резервной копии (1С недоступна)
        _inv_res["stale"] = True
        if _goods.get("t"):
            _inv_res["updated"] = time.strftime("%d.%m %H:%M", time.localtime(_goods["t"]))
    else:
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
    res = {"total": total, "page": page, "per": per, "items": out}
    if _goods.get("from_backup"):      # каталог из резервной копии — честно помечаем
        res["stale"] = True
        if _goods.get("t"):
            res["updated"] = time.strftime("%d.%m %H:%M", time.localtime(_goods["t"]))
    return res


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

def build_sales(from_ts=None, to_ts=None, date_label=None):
    """Сводка продаж из чеков 1С. По умолчанию — за сегодня. Если переданы from_ts/to_ts
    (Unix-секунды) — за период: 1С поддерживает фильтр дат с 24.06.2026 (ТОЛЬКО unix-секунды,
    параметры from/to). date_label — какой датой пометить результат (для докачки истории).
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
        "date": date_label or _today_str(),
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

def _day_bounds(date_str):
    """Unix-границы бишкекского дня 'ГГГГ-ММ-ДД' (TZ сервера = Asia/Bishkek)."""
    t0 = int(time.mktime(time.strptime(date_str + " 00:00:00", "%Y-%m-%d %H:%M:%S")))
    return t0, t0 + 86400 - 1

def backfill_sales(days=30, overwrite=False):
    """Докачать историю продаж за прошлые дни из 1С (фильтр from/to, unix-секунды) в
    Supabase sales_daily. Уже сохранённые дни пропускает (если overwrite=False).
    Сегодняшний день не трогает (он копится сам). Возвращает отчёт."""
    if not supa_on():
        return {"error": "База не настроена"}
    have = set()
    if not overwrite:
        try:
            rows = _supa("GET", "sales_daily",
                         "?company_id=eq.%s&select=date&order=date.desc&limit=400" % _q(COMPANY_ID))
            have = {r.get("date") for r in (rows or [])}
        except Exception:
            pass
    saved = []; skipped = []; failed = []
    today = _today_str()
    for i in range(1, int(days) + 1):
        d = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        if d >= today:
            continue
        if d in have and not overwrite:
            skipped.append(d); continue
        try:
            f, t = _day_bounds(d)
            s = build_sales(f, t, date_label=d)
            if s.get("total_receipts", 0) > 0:
                _save_sales_daily(s)
                saved.append(d)
            else:
                skipped.append(d)        # день без чеков — не сохраняем
        except Exception as e:
            failed.append({"date": d, "error": str(e)[:120]})
    return {"saved_count": len(saved), "saved": saved,
            "skipped": skipped, "failed": failed}


# ====== АНАЛИЗ АССОРТИМЕНТА ПО ЧИСТОЙ ПРИБЫЛИ (ABC по группам) ======
# По запросу владельца: какие позиции оставить в каждой группе. Считаем ТОЛЬКО по
# чистой прибыли и марже продаж — склад/остатки НЕ учитываем. Группа = верхняя
# категория 1С (Носочные, Детская одежда, Обувь...). Тяжёлый расчёт (≈30 запросов
# чеков по дням) — держим снимок в памяти и Supabase (kv_cache), обновляем не чаще
# раза в 6 часов; запрос отвечает мгновенно из снимка.
ASSORT_TTL = 6 * 3600
ASSORT_DAYS = 30
ASSORT_TOPN = 10
_assort = {"data": None, "t": 0.0, "next": 0.0, "refreshing": False}

def _cat_top_resolver():
    """Карта category_id → ВЕРХНЯЯ группа (прямой ребёнок корня «Товары»).
    /categories отдаёт PARENT_ID — поднимаемся по дереву до верхней группы.
    Возвращает функцию top(category_id)->название группы (или None)."""
    data = yaros_get("/categories")
    nodes = {}
    for c in data.get("categories", []):
        nodes[c.get("ID")] = {"title": (c.get("TITLE") or "").strip(),
                              "parent": c.get("PARENT_ID") or ""}
    def top(cid):
        seen = set()
        while cid and cid in nodes and cid not in seen:
            seen.add(cid)
            n = nodes[cid]; p = n["parent"]
            # дошли до верхушки: родителя нет, родитель неизвестен, или родитель = корень «Товары»
            if not p or p not in nodes or nodes.get(p, {}).get("title") == "Товары":
                return n["title"] or None
            cid = p
        return None
    return top

def _cat_floor_sub_resolver():
    """Возвращает функцию cid -> (этаж, подкатегория): этаж = верхняя группа (ребёнок
    корня «Товары»), подкатегория = прямой ребёнок этажа на пути от товара к корню.
    Для отчёта по продажам «этаж → категория»."""
    data = yaros_get("/categories")
    nodes = {}
    for c in data.get("categories", []):
        nodes[c.get("ID")] = {"title": (c.get("TITLE") or "").strip(),
                              "parent": c.get("PARENT_ID") or ""}
    def resolve(cid):
        seen = set(); path = []
        while cid and cid in nodes and cid not in seen:
            seen.add(cid); path.append(cid)
            p = nodes[cid]["parent"]
            if not p or p not in nodes or nodes.get(p, {}).get("title") == "Товары":
                floor = nodes[cid]["title"] or None
                # подкатегория = узел на уровень ниже этажа (предыдущий в пути); если товар
                # сам прямой ребёнок этажа — подкатегория = сам этаж
                sub = nodes[path[-2]]["title"] if len(path) >= 2 else floor
                return (floor, sub or floor)
            cid = p
        return (None, None)
    return resolve

def build_assortment(days=ASSORT_DAYS, top_n=ASSORT_TOPN):
    """Аггрегирует прибыль по каждому товару из чеков за последние `days` дней,
    раскладывает по верхним группам 1С и берёт топ-`top_n` по чистой прибыли в каждой.
    Возвраты учтены с минусом. Склад не используется."""
    goods = get_goods()
    top = _cat_top_resolver()
    # карта название товара → category_id (предпочитаем запись с заполненной категорией)
    name2cat = {}
    for g in goods:
        t = (g.get("TITLE") or "").strip()
        if not t:
            continue
        cid = g.get("CATEGORY_ID") or ""
        if t not in name2cat or (cid and not name2cat[t]):
            name2cat[t] = cid
    # аггрегируем чеки по дням (сегодня + предыдущие days-1 дней)
    today = _today_str()
    dates = [today] + [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
                       for i in range(1, days)]
    agg = {}
    days_ok = 0; days_fail = 0
    for d in dates:
        f, t0 = _day_bounds(d)
        try:
            data = yaros_get("/receipts/v2?from=%d&to=%d" % (f, t0))
        except Exception:
            days_fail += 1
            continue
        days_ok += 1
        for x in data.get("receipts", []):
            sign = -1 if x.get("operationType") == "Возврат" else 1
            for it in x.get("items", []):
                nm = (it.get("name") or "").strip()
                if not nm:
                    continue
                a = agg.setdefault(nm, {"qty": 0.0, "rev": 0.0, "profit": 0.0})
                a["qty"] += sign * _num(it.get("qty"))
                a["rev"] += sign * _num(it.get("saleAmount"))
                a["profit"] += sign * _num(it.get("profit"))
    # раскладка по группам
    groups = {}
    for nm, a in agg.items():
        cid = name2cat.get(nm, "")
        grp = (top(cid) if cid else None) or "— без категории —"
        groups.setdefault(grp, []).append({"name": nm, **a})
    tot_profit = sum(a["profit"] for a in agg.values())
    tot_rev = sum(a["rev"] for a in agg.values())
    out_groups = []
    for grp, items in groups.items():
        gp_profit = sum(i["profit"] for i in items)
        gp_rev = sum(i["rev"] for i in items)
        if gp_profit <= 0:
            continue
        items.sort(key=lambda i: -i["profit"])
        top_items = items[:top_n]
        keep_profit = sum(i["profit"] for i in top_items)
        rows = []
        for rank, i in enumerate(top_items, 1):
            ppu = (i["profit"] / i["qty"]) if i["qty"] else 0
            rows.append({
                "rank": rank, "name": i["name"], "qty": round(i["qty"]),
                "revenue": round(i["rev"]), "profit": round(i["profit"]),
                "group_share": round(i["profit"] / gp_profit * 100, 1) if gp_profit else 0,
                "margin": round(i["rev"] and i["profit"] / i["rev"] * 100),
                "profit_per_unit": round(ppu),
            })
        out_groups.append({
            "group": grp, "profit": round(gp_profit), "revenue": round(gp_rev),
            "share": round(gp_profit / tot_profit * 100, 1) if tot_profit else 0,
            "count": len(items), "keep_n": len(top_items),
            "keep_profit_share": round(keep_profit / gp_profit * 100) if gp_profit else 0,
            "top": rows,
        })
    out_groups.sort(key=lambda g: -g["profit"])
    # ПОЛНЫЙ плоский список всех проданных товаров (для поиска по всему ассортименту)
    all_products = []
    for grp, items in groups.items():
        for i in items:
            rev = i["rev"]; pr = i["profit"]
            all_products.append({
                "name": i["name"], "group": grp, "qty": round(i["qty"]),
                "revenue": round(rev), "profit": round(pr),
                "margin": round(rev and pr / rev * 100),
                "profit_per_unit": round(pr / i["qty"]) if i["qty"] else 0,
            })
    all_products.sort(key=lambda i: -i["profit"])
    # ОТЧЁТ ПО ПРОДАЖАМ: этаж → категория → выручка + прибыль + маржа
    fsub = _cat_floor_sub_resolver()
    fmap = {}
    for nm, a in agg.items():
        cid = name2cat.get(nm, "")
        floor, sub = fsub(cid) if cid else (None, None)
        floor = floor or "— без категории —"; sub = sub or floor
        fm = fmap.setdefault(floor, {"revenue": 0.0, "profit": 0.0, "qty": 0.0, "cats": {}})
        fm["revenue"] += a["rev"]; fm["profit"] += a["profit"]; fm["qty"] += a["qty"]
        cm = fm["cats"].setdefault(sub, {"revenue": 0.0, "profit": 0.0, "qty": 0.0})
        cm["revenue"] += a["rev"]; cm["profit"] += a["profit"]; cm["qty"] += a["qty"]
    floors = []
    for fl, fm in fmap.items():
        cats = [{"name": k, "revenue": round(v["revenue"]), "profit": round(v["profit"]),
                 "qty": round(v["qty"]),
                 "margin": round(v["revenue"] and v["profit"] / v["revenue"] * 100)}
                for k, v in fm["cats"].items()]
        cats.sort(key=lambda c: -c["revenue"])
        floors.append({"floor": fl, "revenue": round(fm["revenue"]),
                       "profit": round(fm["profit"]), "qty": round(fm["qty"]),
                       "margin": round(fm["revenue"] and fm["profit"] / fm["revenue"] * 100),
                       "cats": cats})
    floors.sort(key=lambda f: -f["revenue"])
    res = {
        "period_days": days, "top_n": top_n,
        "from": dates[-1], "to": today,
        "total_revenue": round(tot_rev), "total_profit": round(tot_profit),
        "avg_margin": round(tot_profit / tot_rev * 100, 1) if tot_rev else 0,
        "total_products": len(agg), "days_ok": days_ok, "days_fail": days_fail,
        "groups": out_groups,
        "all_products": all_products,
        "floors": floors,
        "generated_ts": int(time.time()),
        "updated": time.strftime("%d.%m.%Y %H:%M"),
    }
    return res

def _assort_refresh(days=ASSORT_DAYS, force=False):
    """Пересчитывает снимок ассортимента (тяжело — ≈30 запросов к 1С). Сам себя
    троттлит: не чаще раза в ASSORT_TTL. При сбое — повтор через 30 мин."""
    if _assort["refreshing"]:
        return
    if not force and time.time() < _assort.get("next", 0):
        return
    _assort["refreshing"] = True
    try:
        r = build_assortment(days)
        _assort["data"] = r
        _assort["t"] = time.time()
        _assort["next"] = time.time() + ASSORT_TTL
        kv_save("assortment_v1", r)
    except Exception:
        _assort["next"] = time.time() + 1800
    finally:
        _assort["refreshing"] = False

def get_assortment():
    """Мгновенно отдаёт снимок анализа ассортимента из памяти/базы. Если устарел —
    пересчитывает ФОНОМ, не задерживая ответ. Самый первый раз (снимка нигде нет) —
    запускает расчёт в фоне и просит обновить через минуту."""
    d = _assort["data"]
    if d is None:
        d = kv_load("assortment_v1")
        if d:
            _assort["data"] = d
            _assort["t"] = d.get("generated_ts", 0)
            # снимок из базы — фоновый рефреш сам решит по next (сейчас 0 → обновит)
    if d is None:
        # снимка нет нигде — считаем в фоне, чтобы не держать запрос ~40 сек
        if not _assort["refreshing"]:
            threading.Thread(target=_assort_refresh, kwargs={"force": True}, daemon=True).start()
        return {"computing": True, "groups": [],
                "error": "Идёт первичный расчёт анализа — обновите страницу через минуту."}
    if time.time() >= _assort.get("next", 0) and not _assort["refreshing"]:
        threading.Thread(target=_assort_refresh, daemon=True).start()
    out = dict(d)
    age = time.time() - (d.get("generated_ts") or 0)
    if age > ASSORT_TTL * 2:      # снимок заметно устарел (1С долго не отвечала)
        out["stale"] = True
    return out


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

AI_SYSTEM_RENT = (
    "Ты — деловой ИИ-помощник компании по АРЕНДЕ АВТО (прокат). "
    "Отвечай ТОЛЬКО про этот прокат: машины и парк, аренды, выручка, прибыль, долги клиентов, "
    "расходы, сдано руководителю, загрузка машин, какая машина выгоднее, клиенты, аналитика. "
    "Если вопрос не про прокат (погода, политика, общее) — вежливо откажись одной фразой и предложи спросить про аренду. "
    "Валюта — сом (KGS). Отвечай по-русски, кратко и по делу, с конкретными цифрами из данных ниже. "
    "Если данных не хватает — честно скажи. Когда уместно — давай практичные советы владельцу проката."
)

def _ai_context_rent(company):
    """Сводка по прокату для ИИ (из rent_build данной компании)."""
    try:
        b = rent_build(None, company)
    except Exception:
        return "Нет данных по аренде."
    s = b.get("summary", {}); d = b.get("data", {})
    parts = ["ИТОГО: выручка(касса) {} сом, прибыль {} сом, расходы {} сом, долги клиентов {} сом, "
             "сдано руководителю {} сом, остаток прибыли {} сом.".format(
        s.get("revenue"), s.get("profit"), s.get("expenses"), s.get("debts"), s.get("handed"), s.get("balance")),
        "Парк: всего {}, свободно {}, в аренде {}, на ремонте {}. Аренд {}, активных {}. "
        "Средний чек {} сом, средняя длительность {} дн, маржа {}.".format(
        s.get("cars_total"), s.get("cars_free"), s.get("cars_rented"), s.get("cars_repair"),
        s.get("rentals_total"), s.get("rentals_active"), s.get("avg_check"), s.get("avg_days"), s.get("margin"))]
    ca = d.get("caranalysis") or []
    if ca:
        parts.append("По машинам (прибыль): " + "; ".join("%s — %s сом" % (c["model"], c["profit"]) for c in ca[:8]))
    months = d.get("months") or []
    if months:
        parts.append("По месяцам: " + "; ".join("%s: выр %s, приб %s" % (m["month"], m["revenue"], m["profit"]) for m in months[:6]))
    cl = d.get("clients") or []
    if cl:
        parts.append("Топ клиентов: " + "; ".join("%s (%s аренд, %s сом, долг %s)" % (c["name"], c["count"], c["revenue"], c["debt"]) for c in cl[:6]))
    return "\n".join(parts)

def ai_local_rent(question, company):
    """Бесплатный режим для проката: ответы по данным аренды без ИИ-движка (без токенов)."""
    q = (question or "").lower()
    try:
        b = rent_build(None, company)
    except Exception:
        return "Пока не могу получить данные по аренде — попробуй позже."
    s = b.get("summary", {}); d = b.get("data", {})
    has = lambda *ws: any(w in q for w in ws)
    ca = d.get("caranalysis") or []
    cl = d.get("clients") or []
    months = d.get("months") or []
    cur_m = months[0] if months else None
    if has("выгодн", "больше всего", "лучш", "топ", "прибыльн", "зарабат") and ca:
        return "\n".join(["🏆 Самые прибыльные машины:"] +
            ["• %s — прибыль %s сом (выручка %s, маржа %s)" % (c["model"], c["profit"], c["revenue"], c["margin"]) for c in ca[:5]])
    if has("долг", "долж", "задолж", "не заплат", "недоплат", "кто не "):
        deb = [c for c in cl if c.get("_debt", 0) > 0]
        if not deb:
            return "Долгов клиентов нет 👍 (итого долгов: %s сом)." % s.get("debts", "0")
        return "\n".join(["📕 Должники (итого %s сом):" % s.get("debts", "0")] +
            ["• %s%s — %s сом" % (c["name"], (" " + c["phone"]) if c["phone"] else "", c["debt"]) for c in deb[:15]])
    if has("свобод", "простаив", "простой", "доступн"):
        free = [c for c in (d.get("cars") or []) if "свобод" in (c.get("status") or "").lower()]
        head = "🅿️ Свободно: %s из %s машин." % (s.get("cars_free"), s.get("cars_total"))
        return "\n".join([head] + ["• %s" % c.get("model") for c in free[:20]]) if free else head
    if has("заработал", "выручк", "прибыл", "доход", "касса", "оборот", "сколько денег"):
        if has("месяц") and cur_m:
            return "За %s: выручка %s сом, расходы %s, прибыль %s (маржа %s)." % (cur_m["month"], cur_m["revenue"], cur_m["expenses"], cur_m["profit"], cur_m["margin"])
        return "ИТОГО: выручка (касса) %s сом, прибыль %s, расходы %s, остаток %s. Долги клиентов %s." % (s.get("revenue"), s.get("profit"), s.get("expenses"), s.get("balance"), s.get("debts"))
    if has("клиент", "арендатор", "кто брал"):
        if not cl:
            return "Пока нет клиентов с именем/телефоном."
        return "\n".join(["🧑 Топ клиентов:"] +
            ["• %s — %s аренд, %s сом%s" % (c["name"], c["count"], c["revenue"], (", долг " + c["debt"]) if c.get("_debt", 0) > 0 else "") for c in cl[:8]])
    if has("динамик", "рост", "по месяц", "помесяч"):
        return "\n".join(["📅 По месяцам:"] + ["• %s — выручка %s, прибыль %s" % (m["month"], m["revenue"], m["profit"]) for m in months[:8]])
    if has("машин", "парк", "авто"):
        return "🚗 Парк: всего %s, в аренде %s, свободно %s, на ремонте %s. Аренд %s, активных %s." % (s.get("cars_total"), s.get("cars_rented"), s.get("cars_free"), s.get("cars_repair"), s.get("rentals_total"), s.get("rentals_active"))
    return ("📊 Кратко по прокату: выручка %s сом, прибыль %s, машин %s (в аренде %s), долги клиентов %s.\n"
            "Спроси: «какая машина выгоднее?», «кто должен денег?», «сколько заработали в этом месяце?», «какие машины простаивают?»."
            ) % (s.get("revenue"), s.get("profit"), s.get("cars_total"), s.get("cars_rented"), s.get("debts"))

def ai_answer(question, crm, history, user=None):
    # БЕСПЛАТНЫЙ режим: отвечаем локально по данным компании, БЕЗ обращения к платному ИИ (токены не тратятся).
    co = (user or {}).get("company") or COMPANY_ID
    if co != COMPANY_ID:
        return {"free_mode": True, "answer": ai_local_rent(question, co)}
    return {"free_mode": True, "answer": ai_local_answer(question, crm)}


# ====== БАЗА ДАННЫХ (Supabase / Postgres, REST API) ======
from urllib.parse import quote as _q, unquote as _unq, parse_qs as _parse_qs

# COMPANY_ID определён выше (перед load_users) — компания по умолчанию для bizmart

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

def _inbox_save(table, row, retries=2):
    """Сохранить сообщение (ig_inbox / wa_inbox) в БД с повторами. Если после повторов
    не удалось — кладём в постоянный журнал inbox_errors и печатаем предупреждение,
    чтобы сообщение не пропало БЕЗ СЛЕДА при сбое Supabase. Возвращает True при успехе."""
    last = ""
    for i in range(max(1, retries)):
        try:
            _supa("POST", table, "", row)
            return True
        except Exception as e:
            last = str(e)
            if i + 1 < retries:
                time.sleep(0.5)
    try:
        buf = kv_load("inbox_errors") or []
        if not isinstance(buf, list):
            buf = []
        buf.insert(0, {"t": int(time.time()), "table": table, "from": row.get("sender_id"),
                       "mid": row.get("mid", ""), "text": (row.get("text") or "")[:200],
                       "err": last[:200], "row": row})
        kv_save("inbox_errors", buf[:100])
    except Exception:
        pass
    print("[inbox] WARN: не удалось сохранить в %s после %d попыток: %s" % (table, retries, last))
    return False

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

def payroll_rec(name, company=None):
    """Запись зарплаты сотрудника за текущий месяц (помесячно). Источник — Supabase."""
    m = _cur_month(); co = company or COMPANY_ID
    if supa_on():
        try:
            rows = _supa("GET", "payroll",
                         "?company_id=eq.%s&name=eq.%s&month=eq.%s&select=*"
                         % (_q(co), _q(name), _q(m)))
            if rows:
                return rows[0]
            rec = {"company_id": co, "name": name, "month": m,
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

def _emp_row(login, company=None, **f):
    """Полная строка для таблицы employees (берём текущие поля юзера + изменения)."""
    cur = USERS_BY_LOGIN.get(login.lower()) or {}
    secs = f.get("sections", cur.get("sections") or ["myday"])
    if isinstance(secs, (list, tuple)):
        secs = ",".join(secs)
    return {"company_id": (company or cur.get("company") or COMPANY_ID), "login": login,
            "pw": f.get("pw", cur.get("pw", "") or ""),
            "name": f.get("name", cur.get("name", "")),
            "role": f.get("role", cur.get("role", "") or ""),
            "department": f.get("department", cur.get("department", "") or ""),
            "phone": f.get("phone", cur.get("phone", "") or ""),
            "sections": secs or "myday",
            "salary_month": f.get("salary_month", cur.get("salary_month", 0) or 0),
            "daily_rate": f.get("daily_rate", cur.get("daily_rate", 0) or 0),
            "bonus_month": f.get("bonus_month", cur.get("bonus_month", 0) or 0),
            "plan_day": f.get("plan_day", cur.get("plan_day", 0) or 0),
            "video_rate": f.get("video_rate", cur.get("video_rate", 0) or 0),
            "active": f.get("active", True)}

def team_save(row):
    """Записать/обновить сотрудника в Supabase и перечитать в память."""
    _supa("POST", "employees", "?on_conflict=company_id,login", row)
    reload_users()

def _present_in_month(days, m):
    return sum(1 for d, st in (days or {}).items() if isinstance(d, str) and d[:7] == m and st == "p")

SHIFT_HOURS = 10.5                          # часов в смене (цена часа = ставка/день ÷ это)

def rent_commissions(company=None, month=None):
    """Комиссия менеджеров проката: % от выручки (got) аренд, которые ОНИ оформили
    (поле `by`), за месяц. Процент может быть СВОЙ у каждого (d['commissions'][login])
    либо общий по умолчанию (d['commission_pct'], дефолт 10%).
    Возвращает (amount_map, pct_for, default_pct):
      amount_map[by]  — сумма комиссии (ключ = имя/логин, как в поле by),
      pct_for[ключ]   — применённый % (по имени и по логину),
      default_pct     — общий % по умолчанию."""
    co = company or COMPANY_ID
    try:
        d = rent_doc(company)
    except Exception:
        return {}, {}, 10
    default_pct = d.get("commission_pct")
    default_pct = int(float(default_pct)) if (default_pct not in (None, "")) else 10
    per_login = {str(k).lower(): _rnum(v) for k, v in (d.get("commissions") or {}).items()}
    # карты имя→логин и имя/логин→%
    pct_for = {}
    for v in company_users(co):
        lg = (v.get("login") or "").strip().lower()
        nm = (v.get("name") or "").strip()
        pct = per_login.get(lg, default_pct)
        if lg:
            pct_for[lg] = pct
        if nm:
            pct_for[nm] = pct
    m = month or _cur_month()
    got_map = {}
    for r in d.get("rentals", []):
        who = (r.get("by") or "").strip()
        if not who:
            continue
        sd = _rdate(r.get("start"))
        mk = ("%04d-%02d" % (sd.year, sd.month)) if sd else None
        if mk != m:
            continue
        got_map[who] = got_map.get(who, 0) + _rnum(r.get("got"))
    amount_map = {}
    for who, got in got_map.items():
        pct = pct_for.get(who, pct_for.get(who.lower(), default_pct))
        amount_map[who] = int(round(got * pct / 100.0))
    return amount_map, pct_for, default_pct

def _is_video_dept(dep):
    return "мобилограф" in (dep or "").lower()

def rent_fx(company=None):
    """Курс сом за 1$ — для перевода оклада (хранится в сомах) в валюту кабинета проката ($)."""
    try:
        d = rent_doc(company)
        v = d.get("som_per_usd")
        return float(v) if (v not in (None, "") and float(v) > 0) else 87.0
    except Exception:
        return 87.0

def payroll_view(user, rec=None, commission=0, commission_pct=10, fx=0):
    name = user.get("name", "")
    key = _pkey(user)                       # зарплата ведётся по логину (уникален)
    r = rec if rec is not None else payroll_rec(key, user.get("company"))
    days = r.get("days") or {}
    hrs = r.get("hours") or {}
    vids = r.get("videos") or {}
    m = _cur_month(); today = _today_str()
    sal = float(user.get("salary_month") or 0)
    dep = user.get("department", "")
    is_video = _is_video_dept(dep)
    # --- ПРОКАТ (другая компания, не магазин): оклад фиксированный + комиссия с аренд ---
    co_u = user.get("company") or COMPANY_ID
    if co_u != COMPANY_ID:
        # Оклад вводится/хранится в СОМАХ, а кабинет проката ведёт учёт в $ →
        # переводим оклад по курсу. Премия/аванс/комиссия уже в $ (как ввёл владелец).
        _fx = float(fx) if (fx and float(fx) > 0) else 87.0
        accrued = round(sal / _fx)                  # оклад: сом → $
        bonus = float(r.get("bonus") or 0); adv = float(r.get("advance") or 0)
        return {"name": name, "login": user.get("login", ""),
                "role": user.get("role", ""), "department": dep,
                "phone": user.get("phone", "") or "", "sections": user.get("sections", []),
                "salary_month": round(sal / _fx), "salary_som": round(sal), "fx": _fx,
                "daily_rate": float(user.get("daily_rate") or 0),
                "bonus_month": float(user.get("bonus_month") or 0),
                "present_days": _present_in_month(days, m), "partial_days": 0,
                "hours_month": 0, "hours_today": 0, "hours": {}, "hourly_rate": 0, "shift_hours": SHIFT_HOURS,
                "is_video": False, "video_rate": 0, "videos_month": 0, "videos_today": 0,
                "accrued": accrued, "bonus": bonus, "advance": adv,
                "commission": commission, "commission_pct": commission_pct,
                "to_receive": round(accrued + bonus + commission - adv), "days": days,
                "marked_today": days.get(today) == "p", "marked_absent_today": days.get(today) == "a"}
    # --- МОБИЛОГРАФЫ: зарплата по числу отснятых видео ---
    if is_video:
        vrate = float(user.get("video_rate") or 0)
        if vrate <= 0 and sal > 0:
            vrate = round(sal / 30.0)       # по умолчанию 30 видео = полный оклад
        videos_month = sum(int(c or 0) for d, c in vids.items() if isinstance(d, str) and d[:7] == m)
        accrued = round(videos_month * vrate)
        bonus = float(r.get("bonus") or 0); adv = float(r.get("advance") or 0)
        return {"name": name, "login": user.get("login", ""),
                "role": user.get("role", ""), "department": dep,
                "phone": user.get("phone", "") or "",
                "sections": user.get("sections", []),
                "salary_month": sal, "bonus_month": float(user.get("bonus_month") or 0),
                "is_video": True, "video_rate": round(vrate),
                "videos_month": videos_month, "videos_today": int(vids.get(today) or 0),
                "videos": vids,
                "present_days": 0, "partial_days": 0, "hours_month": 0, "hours_today": 0, "hours": {},
                "accrued": accrued, "bonus": bonus, "advance": adv,
                "commission": commission, "commission_pct": commission_pct,
                "to_receive": round(accrued + bonus + commission - adv), "days": days,
                "marked_today": False, "marked_absent_today": False}
    rate = float(user.get("daily_rate") or 0)
    if rate <= 0 and sal > 0:
        rate = round(sal / 30.0)            # оклад ÷ 30 (магазин работает каждый день)
    hr_precise = (rate / SHIFT_HOURS) if rate > 0 else 0.0
    hourly_rate = round(hr_precise)         # цена часа (для показа)
    # За месяц: «пришёл» (полный день), частичные дни (по часам — пришёл не весь день) и «не пришёл» (пропуск).
    present_days = 0; partial_days = 0; hours_month = 0.0; absent_days = 0
    partial_deduct = 0.0                    # недоработка в частичные дни (пришёл, но меньше смены)
    seen = set()
    for d, hv in hrs.items():
        if not (isinstance(d, str) and d[:7] == m):
            continue
        h = float(hv or 0)
        if h > 0:
            partial_days += 1; hours_month += h; seen.add(d)
            partial_deduct += max(0.0, (SHIFT_HOURS - min(h, SHIFT_HOURS)) / SHIFT_HOURS) * rate
    for d, st in days.items():
        if not (isinstance(d, str) and d[:7] == m) or d in seen:
            continue
        if st == "p":
            present_days += 1
        elif st == "a":
            absent_days += 1
    hours_month = round(hours_month, 1)
    # ЗАРПЛАТА = ПОЛНЫЙ ОКЛАД − пропуски сверх нормы выходных − недоработка в частичные дни.
    # Каждому положено `weekends` выходных/мес (по умолч. 4): первые N пропусков НЕ вычитаются.
    # С (N+1)-го пропуска — минус дневная ставка за каждый лишний день. Неотмеченные дни ОПЛАЧИВАЮТСЯ.
    weekends = int(float(user.get("weekend_days"))) if user.get("weekend_days") not in (None, "") else 4
    free_used = min(absent_days, weekends)           # использованные выходные (не вычитаются)
    deduct_days = max(0, absent_days - weekends)      # пропуски сверх нормы — вычитаются по ставке
    accrued = round(max(0.0, sal - deduct_days * rate - partial_deduct))
    bonus = float(r.get("bonus") or 0); adv = float(r.get("advance") or 0)
    return {"name": name, "login": user.get("login", ""),
            "role": user.get("role", ""), "department": user.get("department", ""),
            "phone": user.get("phone", "") or "",
            "sections": user.get("sections", []),
            "salary_month": sal, "daily_rate": rate, "bonus_month": float(user.get("bonus_month") or 0),
            "present_days": present_days, "partial_days": partial_days,
            "absent_days": absent_days, "deduct_days": deduct_days,
            "paid_weekends": free_used, "weekend_days": weekends,
            "accrued": accrued, "bonus": bonus,
            "hourly_rate": hourly_rate, "shift_hours": SHIFT_HOURS,
            "hours_month": hours_month, "hours_today": float(hrs.get(today) or 0),
            "hours": hrs,
            "is_video": False, "video_rate": 0, "videos_month": 0, "videos_today": 0,
            "commission": commission, "commission_pct": commission_pct,
            "advance": adv, "to_receive": round(accrued + bonus + commission - adv), "days": days,
            "marked_today": days.get(today) == "p", "marked_absent_today": days.get(today) == "a"}

def payroll_all(company=None, month=None):
    co = company or COMPANY_ID
    m = month or _cur_month()
    # ВСЕ записи зарплаты за месяц — ОДНИМ запросом (а не по запросу на каждого: было N+1, ~13с)
    recs = None
    if supa_on():
        recs = {}
        try:
            rows = _supa("GET", "payroll",
                         "?company_id=eq.%s&month=eq.%s&select=*" % (_q(co), _q(m)))
            for row in (rows or []):
                recs[row.get("name")] = row
        except Exception:
            recs = None                     # не вышло — откат на поштучный режим
    comm_map, pct_for, default_pct = rent_commissions(co)   # комиссия проката за месяц (по полю `by`)
    _fx_co = rent_fx(co)                                     # курс сом→$ для оклада (один на компанию)
    def _comm(v):
        return comm_map.get((v.get("name") or "").strip(), 0) or comm_map.get((v.get("login") or "").strip(), 0)
    def _cpct(v):
        return pct_for.get((v.get("name") or "").strip(), pct_for.get((v.get("login") or "").strip(), default_pct))
    seen = set(); out = []
    for v in company_users(co):
        if "all" in v.get("sections", []) or v.get("owner"):  # владельца не показываем
            continue
        key = _pkey(v)
        if not key or key in seen:
            continue
        seen.add(key)
        if recs is not None:
            out.append(payroll_view(v, recs.get(key) or {}, _comm(v), _cpct(v), _fx_co))   # без обращения к базе
        else:
            out.append(payroll_view(v, None, _comm(v), _cpct(v), _fx_co))
    return out

# --- Планы по отделам (продажи/день) ---
DEPTPLAN = {}                # резерв в памяти/файле
DEFAULT_DEPT_PLAN = 250000   # план по умолчанию на отдел в день

def _dept_list(company=None):
    out = []
    for v in company_users(company):
        if "all" in v.get("sections", []) or v.get("owner"):
            continue
        d = (v.get("department") or "").strip()
        if d and d not in out:
            out.append(d)
    return out

def _dept_count(dep, company=None):
    return sum(1 for v in company_users(company)
               if (v.get("department") or "").strip() == dep
               and "all" not in v.get("sections", []) and not v.get("owner"))

def dept_plan_rec(dep, company=None):
    co = company or COMPANY_ID
    if supa_on():
        try:
            rows = _supa("GET", "dept_plan",
                         "?company_id=eq.%s&department=eq.%s&select=*" % (_q(co), _q(dep)))
            if rows:
                return rows[0]
            rec = {"company_id": co, "department": dep,
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

def dept_plan_all(company=None):
    co = company or COMPANY_ID
    today = _today_str(); out = []
    recs = None
    if supa_on():
        recs = {}
        try:
            rows = _supa("GET", "dept_plan", "?company_id=eq.%s&select=*" % _q(co))
            for row in (rows or []):
                recs[row.get("department")] = row
        except Exception:
            recs = None
    for dep in _dept_list(co):
        if dep == "Администраторы" or _is_video_dept(dep):   # админы и мобилографы не продают — плана нет
            continue
        if recs is not None:
            r = recs.get(dep) or {"plan_day": DEFAULT_DEPT_PLAN, "facts": {}}
        else:
            r = dept_plan_rec(dep, co)
        plan = float(r.get("plan_day") or 0)
        facts = r.get("facts") or {}
        fact = float(facts.get(today) or 0)
        pct = round(fact / plan * 100) if plan > 0 else 0
        out.append({"department": dep, "plan_day": plan, "fact": fact,
                    "pct": pct, "staff": _dept_count(dep, co)})
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


# ====== РЕКЛАМА (Meta Marketing API — таргет прямо из «Штурвала») ======
ADS_GRAPH = "https://graph.facebook.com/v21.0"
ADS_CUR_MULT = 100   # Meta принимает бюджет в «копейках» (minor units). Для сома/тенге/рубля = 100.

# Цель «по-человечески» → настройки кампании/оптимизации Meta (новый формат ODAX).
# opts — допустимые цели оптимизации группы (первая = по умолчанию). dest=True → выбор Direct/WA/Messenger.
ADS_OBJECTIVES = {
    "awareness":  {"objective": "OUTCOME_AWARENESS", "label": "Узнаваемость / охват", "cta": "LEARN_MORE",
                   "opts": ["REACH", "IMPRESSIONS", "THRUPLAY"]},
    "traffic":    {"objective": "OUTCOME_TRAFFIC", "label": "Трафик / переходы", "cta": "LEARN_MORE",
                   "opts": ["LINK_CLICKS", "LANDING_PAGE_VIEWS", "REACH", "IMPRESSIONS"]},
    "engagement": {"objective": "OUTCOME_ENGAGEMENT", "label": "Вовлечённость", "cta": "LEARN_MORE",
                   "opts": ["POST_ENGAGEMENT", "REACH", "IMPRESSIONS", "THRUPLAY"]},
    "messages":   {"objective": "OUTCOME_ENGAGEMENT", "label": "Сообщения", "cta": "MESSAGE_PAGE", "dest": True,
                   "opts": ["CONVERSATIONS", "LINK_CLICKS"]},
    "leads":      {"objective": "OUTCOME_LEADS", "label": "Лиды / заявки", "cta": "SIGN_UP",
                   "opts": ["CONVERSATIONS", "LINK_CLICKS", "LEAD_GENERATION"]},
    "sales":      {"objective": "OUTCOME_SALES", "label": "Продажи", "cta": "SHOP_NOW",
                   "opts": ["LINK_CLICKS", "CONVERSATIONS", "OFFSITE_CONVERSIONS"]},
}
OPT_LABELS = {"REACH": "Охват", "IMPRESSIONS": "Показы", "LINK_CLICKS": "Клики по ссылке",
    "LANDING_PAGE_VIEWS": "Просмотры страницы", "POST_ENGAGEMENT": "Вовлечённость",
    "THRUPLAY": "Просмотры видео", "CONVERSATIONS": "Переписки", "LEAD_GENERATION": "Заявки (форма)",
    "OFFSITE_CONVERSIONS": "Конверсии (нужен пиксель)"}
CTA_TYPES = [("LEARN_MORE", "Подробнее"), ("SHOP_NOW", "В магазин"), ("ORDER_NOW", "Заказать"),
    ("SIGN_UP", "Регистрация"), ("MESSAGE_PAGE", "Написать"), ("WHATSAPP_MESSAGE", "Написать в WhatsApp"),
    ("CONTACT_US", "Связаться"), ("SUBSCRIBE", "Подписаться"), ("GET_OFFER", "Получить предложение"),
    ("BOOK_TRAVEL", "Забронировать"), ("CALL_NOW", "Позвонить"), ("DOWNLOAD", "Скачать"),
    ("NO_BUTTON", "Без кнопки")]
BID_STRATEGIES = [("LOWEST_COST_WITHOUT_CAP", "Минимальная цена (авто, рекомендуется)"),
    ("LOWEST_COST_WITH_BID_CAP", "С предельной ставкой"), ("COST_CAP", "Контроль цены за результат")]
# Плейсменты: где показывать (ручной режим). key → (платформа, позиция, подпись)
PLACEMENTS = [
    ("ig_stream", "instagram", "stream", "Instagram — лента"),
    ("ig_story", "instagram", "story", "Instagram — сторис"),
    ("ig_reels", "instagram", "reels", "Instagram — Reels"),
    ("ig_explore", "instagram", "explore", "Instagram — интересное"),
    ("fb_feed", "facebook", "feed", "Facebook — лента"),
    ("fb_story", "facebook", "story", "Facebook — сторис"),
    ("fb_reels", "facebook", "facebook_reels", "Facebook — Reels"),
    ("fb_marketplace", "facebook", "marketplace", "Facebook — Маркетплейс"),
    ("fb_video", "facebook", "video_feeds", "Facebook — видеолента"),
]

def _ads_cfg():
    return {"token": CFG.get("META_ADS_TOKEN", ""), "act": CFG.get("META_AD_ACCOUNT", ""),
            "page": CFG.get("META_PAGE_ID", ""),
            "ig": CFG.get("META_IG_ID", "") or CFG.get("IG_ACCOUNT_ID", "")}

def ads_on():
    c = _ads_cfg(); return bool(c["token"] and c["act"])

def _act(c=None):
    c = c or _ads_cfg(); a = (c["act"] or "").strip()
    return a if a.startswith("act_") else ("act_" + a if a else "")

def _ads_req(method, path, params=None, timeout=45):
    """Запрос к Meta Graph API. GET — параметры в URL, POST — в теле. Ошибки Meta
    разворачиваем в понятное сообщение (error_user_msg)."""
    c = _ads_cfg()
    if not c["token"]:
        raise RuntimeError("Не задан токен Meta (META_ADS_TOKEN) — добавьте его в настройки сервера.")
    url = ADS_GRAPH + "/" + str(path).lstrip("/")
    p = dict(params or {}); p["access_token"] = c["token"]
    if method == "GET":
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(p),
                                     headers={"User-Agent": "Shturval/1.0"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(p).encode("utf-8"),
                                     headers={"User-Agent": "Shturval/1.0"})
        req.get_method = lambda: method
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            eo = (json.loads(e.read().decode()) or {}).get("error") or {}
            msg = eo.get("error_user_msg") or eo.get("message") or str(e)
        except Exception:
            msg = str(e)
        raise RuntimeError("Meta: " + msg)

def _ads_iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(int(ts)))

def ads_status():
    """Готовность рекламы: доступы + данные аккаунта + СПРАВОЧНИКИ для мастера (цели,
    оптимизации, CTA, ставки, плейсменты) — чтобы фронт отрисовал полный Ads Manager."""
    c = _ads_cfg()
    st = {"configured": ads_on(), "has_token": bool(c["token"]), "has_account": bool(c["act"]),
          "has_page": bool(c["page"]), "has_ig": bool(c["ig"]),
          "objectives": [{"key": k, "label": v["label"], "dest": bool(v.get("dest")),
                          "opts": [{"key": o, "label": OPT_LABELS.get(o, o)} for o in v["opts"]]}
                         for k, v in ADS_OBJECTIVES.items()],
          "cta_types": [{"key": k, "label": l} for k, l in CTA_TYPES],
          "bid_strategies": [{"key": k, "label": l} for k, l in BID_STRATEGIES],
          "placements": [{"key": k, "label": lbl} for k, _pf, _pos, lbl in PLACEMENTS]}
    if not ads_on():
        return st
    try:
        acc = _ads_req("GET", _act(c), {"fields": "name,currency,account_status,amount_spent"})
        st["account_name"] = acc.get("name"); st["currency"] = acc.get("currency")
        st["account_status"] = acc.get("account_status")   # 1 = активен
        st["spent"] = round(_num(acc.get("amount_spent", 0)) / ADS_CUR_MULT)
    except Exception as e:
        st["error"] = str(e)
    return st

_ig_id_cache = {"id": None}
def ads_ig_id():
    """ID инстаграм-аккаунта для рекламы: из конфига, иначе вычисляем по Странице."""
    c = _ads_cfg()
    if c["ig"]:
        return c["ig"]
    if _ig_id_cache["id"]:
        return _ig_id_cache["id"]
    if c["page"]:
        try:
            d = _ads_req("GET", str(c["page"]), {"fields": "instagram_business_account"})
            iid = (d.get("instagram_business_account") or {}).get("id")
            if iid:
                _ig_id_cache["id"] = iid
                return iid
        except Exception:
            pass
    return ""

def ads_list_posts(limit=30):
    """Список постов инстаграма (для продвижения готового поста). Возвращает превью."""
    if not ads_on():
        return {"posts": [], "configured": False}
    iid = ads_ig_id()
    if not iid:
        return {"posts": [], "error": "Не найден инстаграм-аккаунт. Привяжите Instagram к Странице/кабинету."}
    try:
        d = _ads_req("GET", str(iid) + "/media",
                     {"fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count",
                      "limit": int(limit)})
    except Exception as e:
        return {"posts": [], "error": str(e)}
    posts = []
    for m in (d.get("data") or []):
        posts.append({"id": m.get("id"),
                      "caption": (m.get("caption") or "")[:140],
                      "type": m.get("media_type"),
                      "thumb": m.get("thumbnail_url") or m.get("media_url"),
                      "permalink": m.get("permalink"),
                      "likes": int(_num(m.get("like_count"))),
                      "comments": int(_num(m.get("comments_count"))),
                      "time": m.get("timestamp")})
    return {"posts": posts, "configured": True}

def ads_search_interests(q):
    if not (q and ads_on()):
        return {"items": []}
    d = _ads_req("GET", "search", {"type": "adinterest", "q": q, "limit": 25})
    return {"items": [{"id": x.get("id"), "name": x.get("name"),
                       "audience": x.get("audience_size_upper_bound") or x.get("audience_size")}
                      for x in (d.get("data") or [])]}

def ads_suggest_interests(names):
    """Умные рекомендации интересов: по уже выбранным Meta предлагает похожие
    (как «прощупывание аудитории» в Ads Manager). names — список названий интересов."""
    names = [n for n in (names or []) if n]
    if not (names and ads_on()):
        return {"items": []}
    d = _ads_req("GET", "search", {"type": "adinterestsuggestion",
                 "interest_list": json.dumps(names), "limit": 30})
    return {"items": [{"id": x.get("id"), "name": x.get("name"),
                       "audience": x.get("audience_size_upper_bound") or x.get("audience_size")}
                      for x in (d.get("data") or [])]}

def ads_search_geo(q):
    if not (q and ads_on()):
        return {"items": []}
    d = _ads_req("GET", "search", {"type": "adgeolocation",
                 "location_types": '["city","region","country"]', "q": q, "limit": 20})
    out = []
    for x in (d.get("data") or []):
        out.append({"key": x.get("key"), "name": x.get("name"), "type": x.get("type"),
                    "region": x.get("region"), "country": x.get("country_name")})
    return {"items": out}

def ads_search_locales(q):
    """Поиск языков (locale) для таргетинга по языку аудитории."""
    if not (q and ads_on()):
        return {"items": []}
    d = _ads_req("GET", "search", {"type": "adlocale", "q": q, "limit": 20})
    return {"items": [{"key": x.get("key"), "name": x.get("name")} for x in (d.get("data") or [])]}

def ads_search_behaviors(q):
    """Поиск поведенческих характеристик аудитории."""
    if not (q and ads_on()):
        return {"items": []}
    d = _ads_req("GET", "search", {"type": "adTargetingCategory", "class": "behaviors", "q": q, "limit": 20})
    return {"items": [{"id": x.get("id"), "name": x.get("name"),
                       "audience": x.get("audience_size_upper_bound") or x.get("audience_size")}
                      for x in (d.get("data") or [])]}

def ads_campaigns():
    if not ads_on():
        return {"campaigns": [], "configured": False}
    d = _ads_req("GET", _act() + "/campaigns",
                 {"fields": "name,status,effective_status,objective,daily_budget,created_time", "limit": 50})
    ins = {}
    try:
        di = _ads_req("GET", _act() + "/insights",
                      {"level": "campaign", "fields": "campaign_id,spend,reach,impressions,clicks,actions",
                       "date_preset": "maximum", "limit": 200})
        for r in (di.get("data") or []):
            ins[r.get("campaign_id")] = r
    except Exception:
        pass
    # Расход за СЕГОДНЯ по кампаниям — реальный признак «крутится сейчас»
    # (поле status у кампании остаётся ACTIVE, даже когда группы объявлений на паузе).
    today = {}
    try:
        dt = _ads_req("GET", _act() + "/insights",
                      {"level": "campaign", "fields": "campaign_id,spend",
                       "date_preset": "today", "limit": 200})
        for r in (dt.get("data") or []):
            today[r.get("campaign_id")] = _num(r.get("spend", 0))
    except Exception:
        pass
    # Активно откручивающиеся группы объявлений: даёт (а) какие кампании реально
    # доставляются и (б) бюджет с уровня групп, если на кампании он нулевой (CBO выкл.).
    live_camps = set()
    adset_budget = {}
    try:
        asd = _ads_req("GET", _act() + "/adsets",
                       {"fields": "campaign_id,daily_budget,effective_status",
                        "filtering": json.dumps([{"field": "effective_status", "operator": "IN",
                                                  "value": ["ACTIVE"]}]),
                        "limit": 200})
        for a in (asd.get("data") or []):
            cid = a.get("campaign_id")
            if cid:
                live_camps.add(cid)
                adset_budget[cid] = adset_budget.get(cid, 0) + _num(a.get("daily_budget", 0))
    except Exception:
        pass
    def _action(i, *types):
        """Сумма по нужным типам действий (напр. начатые переписки)."""
        tot = 0
        for a in (i.get("actions") or []):
            if a.get("action_type") in types:
                tot += int(_num(a.get("value")))
        return tot
    out = []
    for c in (d.get("data") or []):
        cid = c.get("id")
        i = ins.get(cid, {})
        eff = c.get("effective_status") or c.get("status")
        sp_today = today.get(cid, 0)
        cbudget = _num(c.get("daily_budget", 0))
        budget = cbudget if cbudget > 0 else adset_budget.get(cid, 0)
        # Три честных состояния, а не один «активна»:
        #  spending   — реально тратит деньги СЕГОДНЯ (это и есть «крутится»);
        #  armed      — включена и есть активная группа, но пока $0 за сегодня (готова/раскачивается);
        #  eff ACTIVE без активных групп — тумблер включён, но группы на паузе (деньги не идут).
        spending = sp_today > 0
        armed = (eff == "ACTIVE") and (cid in live_camps)
        delivering = spending or armed   # совместимость: «работает вообще»
        out.append({"id": cid, "name": c.get("name"), "status": c.get("status"),
                    "effective_status": eff, "delivering": delivering,
                    "spending": spending, "armed": armed,
                    "objective": c.get("objective"),
                    "budget": round(budget / ADS_CUR_MULT),
                    "spend_today": round(sp_today, 2),
                    "spend": round(_num(i.get("spend", 0))), "reach": int(_num(i.get("reach", 0))),
                    "impressions": int(_num(i.get("impressions", 0))),
                    "clicks": int(_num(i.get("clicks", 0))),
                    "conversations": _action(i, "onsite_conversion.messaging_conversation_started_7d",
                                             "onsite_conversion.total_messaging_connection")})
    # Сначала реально тратящие, затем готовые, затем просто включённые, затем пауза.
    out.sort(key=lambda x: (x["spending"], x["armed"], x["status"] == "ACTIVE", x["spend_today"]), reverse=True)
    return {"campaigns": out, "configured": True}

def ads_set_status(cid, active):
    _ads_req("POST", str(cid), {"status": "ACTIVE" if active else "PAUSED"})
    return {"ok": True}

def ads_upload_image(b64):
    """Загрузить картинку в рекламный аккаунт, вернуть image_hash для креатива."""
    d = _ads_req("POST", _act() + "/adimages", {"bytes": b64})
    for k, v in (d.get("images") or {}).items():
        if v.get("hash"):
            return v["hash"]
    raise RuntimeError("Не удалось загрузить изображение в Meta")

_CONV_ACTIONS = ("onsite_conversion.messaging_conversation_started_7d",
                 "onsite_conversion.total_messaging_connection")
def _ins_conv(i):
    return sum(int(_num(a.get("value"))) for a in (i.get("actions") or [])
               if a.get("action_type") in _CONV_ACTIONS)

# Периоды для отчётов/анализа. Ключ Meta → человеческая подпись (используется и на фронте).
ADS_PERIODS = [
    ("today", "Сегодня"), ("yesterday", "Вчера"),
    ("last_7d", "7 дней"), ("last_14d", "14 дней"), ("last_30d", "30 дней"),
    ("last_90d", "90 дней"),
    ("this_month", "Этот месяц"), ("last_month", "Прошлый месяц"),
    ("maximum", "Всё время"),
]
_ADS_PERIOD_KEYS = {k for k, _ in ADS_PERIODS}

def _ads_date_params(period=None, since=None, until=None):
    """Параметры диапазона дат для Meta insights: свой период (time_range) или
    пресет (date_preset). По умолчанию — всё время."""
    since = (since or "").strip(); until = (until or "").strip()
    if since and until:
        return {"time_range": json.dumps({"since": since, "until": until})}
    period = (period or "").strip()
    if period in _ADS_PERIOD_KEYS:
        return {"date_preset": period}
    return {"date_preset": "maximum"}

def _ads_campaign_index():
    """id → {name, created}. Для сортировки отчёта по новизне и списка выбора кампаний."""
    idx = {}
    try:
        cc = _ads_req("GET", _act() + "/campaigns",
                      {"fields": "id,name,created_time", "limit": 200})
        for c in (cc.get("data") or []):
            idx[c.get("id")] = {"name": c.get("name") or "", "created": c.get("created_time") or ""}
    except Exception:
        pass
    return idx

def ads_report(period=None, since=None, until=None):
    """Подробный отчёт по рекламе: метрики по каждой кампании + итог + динамика по дням.
    period/since/until — выбранный диапазон (по умолчанию всё время)."""
    if not ads_on():
        return {"configured": False, "rows": [], "totals": {}, "days": []}
    dr = _ads_date_params(period, since, until)
    cur = ""
    try:
        cur = _ads_req("GET", _act(), {"fields": "currency"}).get("currency", "")
    except Exception:
        pass
    cidx = _ads_campaign_index()  # для сортировки от новых к старым
    flds = "campaign_id,campaign_name,spend,impressions,reach,clicks,ctr,cpc,cpm,frequency,actions"
    rows, tot = [], {"spend": 0.0, "impr": 0, "reach": 0, "clicks": 0, "conv": 0}
    try:
        pr = {"level": "campaign", "fields": flds, "limit": 200}; pr.update(dr)
        di = _ads_req("GET", _act() + "/insights", pr)
        for i in (di.get("data") or []):
            spend = _num(i.get("spend")); conv = _ins_conv(i)
            clicks = int(_num(i.get("clicks")))
            rows.append({"name": i.get("campaign_name"), "spend": round(spend), "impr": int(_num(i.get("impressions"))),
                         "reach": int(_num(i.get("reach"))), "clicks": clicks,
                         "ctr": round(_num(i.get("ctr")), 2), "cpc": round(_num(i.get("cpc")), 2),
                         "cpm": round(_num(i.get("cpm")), 2), "freq": round(_num(i.get("frequency")), 1),
                         "conv": conv, "cpconv": round(spend / conv, 2) if conv else 0,
                         "created": cidx.get(i.get("campaign_id"), {}).get("created", "")})
            tot["spend"] += spend; tot["impr"] += _num(i.get("impressions"))
            tot["reach"] += _num(i.get("reach")); tot["clicks"] += clicks; tot["conv"] += conv
    except Exception as e:
        return {"configured": True, "rows": [], "totals": {}, "days": [], "error": str(e), "currency": cur}
    # От новых к старым (по дате создания кампании); кампании без даты — в конец.
    rows.sort(key=lambda r: r.get("created") or "", reverse=True)
    days = []
    try:
        dp = {"fields": "spend,impressions,clicks", "time_increment": "1", "limit": 180}; dp.update(dr)
        dd = _ads_req("GET", _act() + "/insights", dp)
        days = [{"date": x.get("date_start"), "spend": round(_num(x.get("spend"))),
                 "clicks": int(_num(x.get("clicks")))} for x in (dd.get("data") or [])]
    except Exception:
        pass
    t = {"spend": round(tot["spend"]), "impr": int(tot["impr"]), "reach": int(tot["reach"]),
         "clicks": tot["clicks"], "conv": tot["conv"],
         "ctr": round(tot["clicks"] / tot["impr"] * 100, 2) if tot["impr"] else 0,
         "cpc": round(tot["spend"] / tot["clicks"], 2) if tot["clicks"] else 0,
         "cpconv": round(tot["spend"] / tot["conv"], 2) if tot["conv"] else 0}
    return {"configured": True, "rows": rows, "totals": t, "days": days, "currency": cur,
            "periods": ADS_PERIODS, "period": (period or "maximum")}

def ads_analysis(campaign=None, period=None, since=None, until=None):
    """Анализ аудитории: разбивки по полу/возрасту, платформе, плейсменту + авто-выводы.
    campaign — id конкретной кампании (пусто = весь аккаунт); period/since/until — диапазон."""
    if not ads_on():
        return {"configured": False}
    dr = _ads_date_params(period, since, until)
    campaign = (str(campaign).strip() if campaign else "")
    base = (campaign + "/insights") if campaign else (_act() + "/insights")
    def bd(breakdowns):
        try:
            pr = {"fields": "spend,impressions,clicks,reach", "breakdowns": breakdowns, "limit": 100}
            pr.update(dr)
            d = _ads_req("GET", base, pr)
            return d.get("data") or []
        except Exception:
            return []
    def pack(rows, keyf):
        out = []
        for r in rows:
            out.append({"seg": keyf(r), "spend": round(_num(r.get("spend"))),
                        "impr": int(_num(r.get("impressions"))), "clicks": int(_num(r.get("clicks"))),
                        "reach": int(_num(r.get("reach")))})
        out.sort(key=lambda x: -x["clicks"])
        return out
    gen = {"male": "Мужчины", "female": "Женщины", "unknown": "Не указан"}
    ag = pack(bd("age,gender"), lambda r: (r.get("age", "?") + " · " + gen.get(r.get("gender"), r.get("gender", "?"))))
    plat = pack(bd("publisher_platform"), lambda r: r.get("publisher_platform", "?"))
    place = pack(bd("publisher_platform,platform_position"),
                 lambda r: (r.get("publisher_platform", "?") + " · " + r.get("platform_position", "?")))
    tips = []
    if ag and ag[0]["clicks"]:
        tips.append("👥 Больше всего откликов от: <b>%s</b> (%d кликов)." % (ag[0]["seg"], ag[0]["clicks"]))
    if plat and plat[0]["clicks"]:
        tips.append("📱 Лучшая площадка: <b>%s</b>." % plat[0]["seg"])
    if place and place[0]["clicks"]:
        tips.append("📍 Лучший плейсмент: <b>%s</b>." % place[0]["seg"])
    if not tips:
        tips.append("Пока мало данных для выводов — запусти кампанию, и здесь появится анализ аудитории.")
    # Список кампаний для выбора (от новых к старым).
    cidx = _ads_campaign_index()
    camps = [{"id": cid, "name": v["name"]} for cid, v in
             sorted(cidx.items(), key=lambda kv: kv[1].get("created") or "", reverse=True)]
    return {"configured": True, "age_gender": ag[:12], "platform": plat, "placement": place[:12],
            "tips": tips, "campaigns": camps, "campaign": campaign,
            "periods": ADS_PERIODS, "period": (period or "maximum")}

def ads_create(p):
    """Полный мастер Ads Manager: Кампания → Группа объявлений → Креатив → Объявление.
    Поддержка: цель+оптимизация, бюджет (дневной/на срок, на уровне кампании/группы), ставки,
    категория рекламы, расписание, аудитория (гео/возраст/пол/языки/интересы/поведение/исключения),
    плейсменты (авто/вручную), назначение лидов, креатив (готовый пост / фото+текст+заголовок+CTA).
    Всё создаётся в статусе ПАУЗА — деньги не тратятся, пока владелец не нажмёт «Включить»."""
    if not ads_on():
        raise RuntimeError("Реклама не настроена: нужны токен Meta, рекламный аккаунт и Страница.")
    c = _ads_cfg()
    spec = ADS_OBJECTIVES.get(p.get("goal"), ADS_OBJECTIVES["traffic"])
    name = (p.get("name") or "Кампания Штурвал").strip()
    if not c["page"]:
        raise RuntimeError("Не задан ID Страницы Facebook (META_PAGE_ID).")
    # бюджет
    amount = int(round(_num(p.get("budget") or p.get("daily_budget") or 0) * ADS_CUR_MULT))
    if amount < ADS_CUR_MULT:
        raise RuntimeError("Укажите бюджет (минимум 1).")
    btype = p.get("budget_type") or "daily"            # daily | lifetime
    blevel = p.get("budget_level") or "adset"          # adset | campaign
    bfield = "daily_budget" if btype == "daily" else "lifetime_budget"
    bid_strategy = p.get("bid_strategy") or "LOWEST_COST_WITHOUT_CAP"
    cat = (p.get("special_ad_category") or "NONE").upper()
    # 1) Кампания
    camp_p = {"name": name, "objective": spec["objective"], "status": "PAUSED",
              "special_ad_categories": json.dumps([] if cat in ("", "NONE") else [cat])}
    if blevel == "campaign":
        camp_p[bfield] = amount
        camp_p["bid_strategy"] = bid_strategy
    else:
        camp_p["is_adset_budget_sharing_enabled"] = "false"
    camp_id = _ads_req("POST", _act() + "/campaigns", camp_p).get("id")
    # 2) Таргетинг
    targeting = {"age_min": int(_num(p.get("age_min")) or 18),
                 "age_max": int(_num(p.get("age_max")) or 65),
                 "targeting_automation": {"advantage_audience": 0}}
    if cat in ("", "NONE"):       # для спец-категорий пол/возраст ограничены — не трогаем
        g = p.get("gender")
        if g == "male":   targeting["genders"] = [1]
        elif g == "female": targeting["genders"] = [2]
    geo = {}
    cities = p.get("cities") or []
    countries = p.get("countries") or []
    if cities:
        geo["cities"] = [{"key": x.get("key"),
                          "radius": max(17, min(80, int(_num(x.get("radius")) or 25))),
                          "distance_unit": "kilometer"} for x in cities if x.get("key")]
    if countries:
        geo["countries"] = countries
    if not geo:
        geo = {"countries": ["KG"]}
    targeting["geo_locations"] = geo
    locales = [int(_num(x.get("key"))) for x in (p.get("locales") or []) if x.get("key")]
    if locales:
        targeting["locales"] = locales
    inc = {}
    interests = [{"id": x["id"], "name": x.get("name")} for x in (p.get("interests") or []) if x.get("id")]
    behaviors = [{"id": x["id"], "name": x.get("name")} for x in (p.get("behaviors") or []) if x.get("id")]
    if interests: inc["interests"] = interests
    if behaviors: inc["behaviors"] = behaviors
    if inc:
        targeting["flexible_spec"] = [inc]
    exclusions = [{"id": x["id"], "name": x.get("name")} for x in (p.get("exclusions") or []) if x.get("id")]
    if exclusions:
        targeting["exclusions"] = {"interests": exclusions}
    # плейсменты
    pl_keys = p.get("placements") or []
    if p.get("auto_placements", True) or not pl_keys:
        targeting["publisher_platforms"] = ["instagram", "facebook"]
    else:
        plat, fbpos, igpos = set(), [], []
        for k, pf, pos, _lbl in PLACEMENTS:
            if k in pl_keys:
                plat.add(pf)
                (fbpos if pf == "facebook" else igpos).append(pos)
        targeting["publisher_platforms"] = sorted(plat) or ["instagram", "facebook"]
        if fbpos: targeting["facebook_positions"] = fbpos
        if igpos: targeting["instagram_positions"] = igpos
    # 3) Группа объявлений
    opt = p.get("optimization_goal")
    if opt not in spec["opts"]:
        opt = spec["opts"][0]
    adset = {"name": name + " — группа", "campaign_id": camp_id, "billing_event": "IMPRESSIONS",
             "optimization_goal": opt, "targeting": json.dumps(targeting), "status": "PAUSED"}
    if blevel == "adset":
        adset[bfield] = amount
        adset["bid_strategy"] = bid_strategy
        if bid_strategy in ("LOWEST_COST_WITH_BID_CAP", "COST_CAP"):
            adset["bid_amount"] = int(round(_num(p.get("bid_amount")) * ADS_CUR_MULT)) or 100
    if p.get("start_date"):
        try: adset["start_time"] = _ads_iso(_day_bounds(p["start_date"])[0])
        except Exception: pass
    if p.get("end_date"):
        try: adset["end_time"] = _ads_iso(_day_bounds(p["end_date"])[1])
        except Exception: pass
    if btype == "lifetime" and "end_time" not in adset:
        raise RuntimeError("Для бюджета «на весь срок» укажите дату окончания.")
    if p.get("goal") == "messages":
        dest_map = {"ig_direct": "INSTAGRAM_DIRECT", "messenger": "MESSENGER", "whatsapp": "WHATSAPP"}
        adset["destination_type"] = dest_map.get(p.get("dest"), "INSTAGRAM_DIRECT")
        adset["promoted_object"] = json.dumps({"page_id": c["page"]})
    adset_id = _ads_req("POST", _act() + "/adsets", adset).get("id")
    # 4) Креатив
    ig_id = ads_ig_id()
    if p.get("post_id"):
        # Продвижение ГОТОВОГО поста инстаграма: instagram_user_id + source_instagram_media_id напрямую.
        if not ig_id:
            raise RuntimeError("Не найден инстаграм-аккаунт для продвижения поста.")
        creative_params = {"name": name + " — креатив", "instagram_user_id": str(ig_id),
                           "source_instagram_media_id": str(p["post_id"])}
    else:
        msg = (p.get("text") or "").strip()
        headline = (p.get("headline") or "").strip()
        desc = (p.get("description") or "").strip()
        cta = p.get("cta_type") or spec["cta"]
        link = (p.get("link") or "").strip() or ("https://instagram.com/" + CFG.get("IG_USERNAME") if CFG.get("IG_USERNAME") else "https://facebook.com/" + str(c["page"]))
        story = {"page_id": c["page"]}
        if ig_id:
            story["instagram_user_id"] = str(ig_id)
        link_data = {"message": msg, "link": link}
        if p.get("image_hash"): link_data["image_hash"] = p["image_hash"]
        if headline: link_data["name"] = headline
        if desc: link_data["description"] = desc
        if cta and cta != "NO_BUTTON":
            link_data["call_to_action"] = ({"type": cta} if p.get("goal") == "messages"
                                           else {"type": cta, "value": {"link": link}})
        story["link_data"] = link_data
        creative_params = {"name": name + " — креатив", "object_story_spec": json.dumps(story)}
    creative_id = _ads_req("POST", _act() + "/adcreatives", creative_params).get("id")
    # 5) Объявление
    ad_id = _ads_req("POST", _act() + "/ads",
                     {"name": name + " — объявление", "adset_id": adset_id,
                      "creative": json.dumps({"creative_id": creative_id}),
                      "status": "PAUSED"}).get("id")
    return {"ok": True, "campaign_id": camp_id, "adset_id": adset_id, "ad_id": ad_id,
            "note": "Реклама создана в статусе ПАУЗА. Проверьте и нажмите «Включить», чтобы запустить."}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        # сжатие: крупные JSON (дашборд аренды ~750 КБ) ужимаются в ~10 раз → ответ резче
        gz = False
        try:
            ae = self.headers.get("Accept-Encoding") or ""
        except Exception:
            ae = ""
        if "gzip" in ae and len(body) > 1024:
            body = gzip.compress(body, 5)
            gz = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Cache-Control", "no-store")
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(200, {})

    def _user(self):
        # БЕЗОПАСНОСТЬ: если пользователи не заданы вовсе — НИКОГО не пускаем (fail-closed).
        if not USERS and not USERS_BY_LOGIN:
            return None
        pw = (self.headers.get("X-Auth") or "").strip()
        login = (self.headers.get("X-Login") or "").strip().lower()
        if login:                                  # вход логин+пароль
            u = USERS_BY_LOGIN.get(login)
            if u and _verify_pw(u.get("pw"), pw):  # хеш или старый открытый текст, в пост. время
                return u
            return None
        if pw:                                      # обратная совместимость: только пароль
            for cand in USERS.values():
                if _verify_pw(cand.get("pw"), pw):
                    return cand
        return None

    def _authed(self):
        return self._user() is not None

    def do_POST(self):
        # Instagram webhook — ПУБЛИЧНЫЙ (Meta шлёт события без нашего логина). Отвечаем быстро 200.
        if self.path.startswith("/api/ig/webhook"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
            except Exception:
                raw = b""
            # БЕЗОПАСНОСТЬ: проверяем подпись Meta (HMAC-SHA256 от сырого тела с IG_APP_SECRET).
            # fail-closed: если секрет НЕ задан — отклоняем (иначе любой мог бы слать боту фейки).
            secret = CFG.get("IG_APP_SECRET", "")
            if not secret:
                print("[ig/webhook] WARN: IG_APP_SECRET не задан — событие отклонено (нельзя проверить подпись)")
                self.send_response(403); self.end_headers(); return
            sig = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            if not (sig and hmac.compare_digest(expected, sig)):
                self.send_response(403); self.end_headers(); return   # поддельный запрос
            try:
                evt = json.loads(raw or b"{}")
            except Exception:
                evt = {}
            try:
                ig_store_event(evt)
            except Exception:
                pass
            self.send_response(200); self.send_header("Content-Type", "text/plain")
            self.end_headers(); self.wfile.write(b"EVENT_RECEIVED"); return
        # WhatsApp webhook — ПУБЛИЧНЫЙ (Meta шлёт события без нашего логина). Отвечаем быстро 200.
        if self.path.startswith("/api/wa/webhook"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
            except Exception:
                raw = b""
            # БЕЗОПАСНОСТЬ: проверяем подпись Meta (HMAC-SHA256 от сырого тела).
            # WA_APP_SECRET, а если не задан — IG_APP_SECRET (обычно то же приложение Meta).
            # fail-closed: без секрета отклоняем событие.
            secret = CFG.get("WA_APP_SECRET", "") or CFG.get("IG_APP_SECRET", "")
            if not secret:
                print("[wa/webhook] WARN: WA_APP_SECRET/IG_APP_SECRET не заданы — событие отклонено")
                self.send_response(403); self.end_headers(); return
            sig = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            if not (sig and hmac.compare_digest(expected, sig)):
                self.send_response(403); self.end_headers(); return   # поддельный запрос
            try:
                evt = json.loads(raw or b"{}")
            except Exception:
                evt = {}
            try:
                wa_store_event(evt)
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
        if self.path.startswith("/api/rent"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            try:
                _u = self._user() or {}
                _co = _u.get("company") or COMPANY_ID
                res = rent_apply(body.get("action"), body, _co, _u)
            except Exception as e:
                return self._send(500, {"error": "Не удалось сохранить: %s" % e})
            if isinstance(res, dict) and res.get("error"):
                return self._send(400, res)
            return self._send(200, res)
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
        if self.path.startswith("/api/ig/media"):
            # Оператор отправляет клиенту в Instagram голосовое/фото/файл (data_b64 — содержимое).
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            acc = str(body.get("account_id") or ""); cust = str(body.get("customer_id") or "")
            mime = (body.get("mime") or "").strip()
            mtype = (body.get("type") or ig_media_type(mime)).strip()
            b64 = body.get("data_b64") or ""
            if not acc or not cust or not b64:
                return self._send(400, {"error": "нужны account_id, customer_id и data_b64"})
            try:
                data = base64.b64decode(b64)
            except Exception:
                return self._send(400, {"error": "плохие данные файла"})
            if len(data) > 25 * 1024 * 1024:
                return self._send(200, {"ok": False, "error": "Файл больше 25 МБ."})
            try:
                tok = _ig_outmedia_put(data, mime or "application/octet-stream")
                proto = (self.headers.get("X-Forwarded-Proto") or "https").split(",")[0].strip()
                host = self.headers.get("Host") or ""
                media_url = proto + "://" + host + "/api/ig/outmedia?k=" + tok
                ig_reply_media(acc, cust, media_url, mtype, mime=mime)
            except urllib.error.HTTPError as e:
                try:
                    det = json.loads(e.read().decode() or "{}").get("error", {}).get("message", "")
                except Exception:
                    det = str(e)
                return self._send(200, {"ok": False, "error": "Instagram: " + (det or str(e))})
            except Exception as e:
                return self._send(200, {"ok": False, "error": "Instagram: " + str(e)})
            return self._send(200, {"ok": True})
        if self.path.startswith("/api/wa/botreply"):
            # Форсировать ответ ИИ-бота этому клиенту WhatsApp сейчас (даже если раньше отвечал человек).
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            acc = str(body.get("account_id") or ""); cust = str(body.get("customer_id") or "")
            if not acc or not cust:
                return self._send(400, {"error": "нужны account_id и customer_id"})
            try:
                wabot_handle(cust, acc, fallback=True)   # fallback=True: отвечает, если последнее — от клиента, минуя хендофф
            except Exception as e:
                return self._send(200, {"ok": False, "error": "Бот: " + str(e)})
            return self._send(200, {"ok": True})
        if self.path.startswith("/api/wa/reply"):
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
                wa_reply(acc, cust, text)
            except Exception as e:
                return self._send(200, {"ok": False, "error": "WhatsApp: " + str(e)})
            return self._send(200, {"ok": True})
        if self.path.startswith("/api/wa/template"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            acc = str(body.get("account_id") or ""); cust = str(body.get("customer_id") or "")
            tpl = (body.get("template") or "bizmart_reengage_ru").strip()
            lang = (body.get("lang") or "ru").strip()
            preview = (body.get("preview") or "").strip()
            if not acc or not cust:
                return self._send(400, {"error": "нужны account_id и customer_id"})
            try:
                wa_reply_template(acc, cust, tpl, lang, preview=preview)
            except Exception as e:
                return self._send(200, {"ok": False, "error": "WhatsApp: " + str(e)})
            return self._send(200, {"ok": True})
        if self.path.startswith("/api/wa/read"):
            # Отметить последнее входящее сообщение клиента прочитанным
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            acc = str(body.get("account_id") or ""); cust = str(body.get("customer_id") or "")
            if not acc or not cust:
                return self._send(400, {"error": "нужны account_id и customer_id"})
            try:
                rows = _supa("GET", "wa_inbox",
                             "?company_id=eq.%s&sender_id=eq.%s&recipient_id=eq.%s&direction=eq.in"
                             "&order=ts.desc&limit=1&select=mid"
                             % (_q(COMPANY_ID), _q(cust), _q(acc)))
                mid = (rows[0].get("mid") if rows else "") or ""
                if mid:
                    wa_mark_read(acc, mid)
            except Exception:
                pass
            return self._send(200, {"ok": True})
        if self.path.startswith("/api/wa/media"):
            # Оператор отправляет клиенту файл/фото/видео/голосовое (data_b64 — содержимое файла).
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            acc = str(body.get("account_id") or ""); cust = str(body.get("customer_id") or "")
            mime = (body.get("mime") or "").strip()
            mtype = (body.get("type") or wa_media_type(mime)).strip()
            fname = (body.get("filename") or "file").strip()
            cap = (body.get("caption") or "").strip()
            b64 = body.get("data_b64") or ""
            if not acc or not cust or not b64:
                return self._send(400, {"error": "нужны account_id, customer_id и data_b64"})
            # голосовые/аудио: WhatsApp принимает только ogg/mp3/mp4/aac/amr (НЕ webm от Chrome)
            if mtype == "audio" and not any(mime.startswith(x) for x in WA_AUDIO_OK):
                return self._send(200, {"ok": False, "error":
                    "Этот браузер записал голосовое в формате, который WhatsApp не принимает (нужен OGG/Opus). "
                    "Запишите в Safari или Firefox, либо прикрепите готовый аудиофайл."})
            try:
                data = base64.b64decode(b64)
            except Exception:
                return self._send(400, {"error": "плохие данные файла"})
            if len(data) > 16 * 1024 * 1024:
                return self._send(200, {"ok": False, "error": "Файл больше 16 МБ — WhatsApp не примет."})
            try:
                media_id = wa_upload_media(acc, data, mime, fname)
                if not media_id:
                    return self._send(200, {"ok": False, "error": "Meta не приняла файл (нет media_id)."})
                wa_reply_media(acc, cust, mtype, media_id, caption=cap, filename=fname, mime=mime)
            except urllib.error.HTTPError as e:
                try:
                    det = json.loads(e.read().decode() or "{}").get("error", {}).get("message", "")
                except Exception:
                    det = str(e)
                return self._send(200, {"ok": False, "error": "WhatsApp: " + (det or str(e))})
            except Exception as e:
                return self._send(200, {"ok": False, "error": "WhatsApp: " + str(e)})
            return self._send(200, {"ok": True})
        if self.path.startswith("/api/ig/bot"):
            u = self._user(); secs = u.get("sections", []) if u else []
            if not (u and ("all" in secs or "chats" in secs)):
                return self._send(403, {"error": "Только владелец или менеджер чатов"})
            if self.path.startswith("/api/ig/bot/learn"):
                try:
                    return self._send(200, igbot_learn())
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            if self.path.startswith("/api/ig/bot/source/del"):
                return self._send(200, igbot_source_del(body.get("url")))
            if self.path.startswith("/api/ig/bot/source"):
                try:
                    return self._send(200, igbot_fetch_source(body.get("url")))
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            if self.path.startswith("/api/ig/bot/errors/clear"):
                kv_save("igbot_errors", [])
                return self._send(200, {"ok": True})
            return self._send(200, {"ok": True, "settings": _igbot_set(body)})
        if self.path.startswith("/api/ig/broadcast"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            text = (body.get("text") or "").strip()
            account = (body.get("account") or "all")
            try:
                limit = int(body.get("limit") or 500)
            except Exception:
                limit = 500
            limit = max(1, min(limit, 500))
            if not text:
                return self._send(400, {"error": "Введите текст рассылки"})
            try:
                return self._send(200, ig_broadcast(text, account, limit=limit))
            except Exception as e:
                return self._send(500, {"error": "Рассылка не удалась: %s" % e})
        if self.path.startswith("/api/ads/"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            try:
                if self.path.startswith("/api/ads/create"):
                    return self._send(200, ads_create(body))
                if self.path.startswith("/api/ads/pause"):
                    return self._send(200, ads_set_status(body.get("id"), bool(body.get("active"))))
                if self.path.startswith("/api/ads/upload"):
                    b64 = body.get("image_b64") or ""
                    if "," in b64[:64] and b64[:5] == "data:":
                        b64 = b64.split(",", 1)[1]
                    if not b64:
                        return self._send(400, {"error": "Нет изображения"})
                    return self._send(200, {"ok": True, "image_hash": ads_upload_image(b64)})
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
            return self._send(404, {"error": "неизвестный метод рекламы"})
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
            # ГЛАВНОЕ: складываем правки в «указания владельца» — бот будет реально им следовать
            # (хранится отдельно от авто-базы знаний, поэтому авто-обучение их не сотрёт).
            added = 0
            try:
                blocks = []
                for f in fixes:
                    w = (f.get("wrong") or "").strip()
                    rgt = (f.get("right") or "").strip()
                    note = (f.get("note") or "").strip()
                    if not rgt:
                        continue
                    b = ""
                    if w:
                        b += "❌ Так бот отвечал НЕВЕРНО: " + w + "\n"
                    b += "✅ Отвечай так: " + rgt + "\n"
                    if note:
                        b += "📝 " + note + "\n"
                    blocks.append(b)
                if blocks:
                    s = _igbot_get()
                    teach = ((s.get("teach") or "") + "\n" + "\n".join(blocks)).strip()
                    if len(teach) > 15000:
                        teach = teach[-15000:]
                    _igbot_set({"teach": teach})
                    added = len(blocks)
            except Exception as e:
                _igbot_log_error("Обучение (правки владельца)", str(e))
            return self._send(200, {"ok": True, "saved": len(fixes), "applied": added, "total": len(BOT_FEEDBACK)})
        if self.path.startswith("/api/assistant"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            q = (body.get("question") or "").strip()
            if not q:
                return self._send(400, {"error": "пустой вопрос"})
            return self._send(200, ai_answer(q, body.get("crm"), body.get("history"), self._user()))
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
            co = u.get("company") or COMPANY_ID
            if (tu.get("company") or COMPANY_ID) != co:   # чужую компанию не трогаем
                return self._send(403, {"error": "Это сотрудник другой компании"})
            r = payroll_rec(_pkey(tu), co)
            days = r.get("days") or {}
            hrs = r.get("hours") or {}
            vids = r.get("videos") or {}
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
            elif action == "set_days":         # массово сохранить календарь (правки копятся на фронте, шлются разом)
                nd = body.get("days")
                if not isinstance(nd, dict):
                    return self._send(400, {"error": "нужен список дней"})
                days = {k: ("p" if vv == "p" else "a")
                        for k, vv in nd.items()
                        if isinstance(k, str) and len(k) == 10 and k[4] == "-" and vv in ("p", "a")}
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
            elif action == "set_videos":       # ЗАДАТЬ число видео за день (мобилографы)
                n = int(float(body.get("videos") or 0))
                if n <= 0:
                    vids.pop(day, None)
                else:
                    vids[day] = n
                r["videos"] = vids
            else:
                return self._send(400, {"error": "неизвестное действие"})
            r["days"] = days
            r["hours"] = hrs
            r["videos"] = vids
            r["present_days"] = _present_in_month(days, _cur_month())
            r["last_present"] = today if days.get(today) == "p" else r.get("last_present", "")
            payroll_upsert(r)
            _cm, _pf, _dp = rent_commissions(tu.get("company"))
            _c = _cm.get((tu.get("name") or "").strip(), 0) or _cm.get((tu.get("login") or "").strip(), 0)
            _cp = _pf.get((tu.get("name") or "").strip(), _pf.get((tu.get("login") or "").strip(), _dp))
            return self._send(200, {"ok": True, "view": payroll_view(tu, r, _c, _cp, rent_fx(tu.get("company")))})
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
            co = u.get("company") or COMPANY_ID
            r = dept_plan_rec(dep, co)
            if action == "setplan":
                r["plan_day"] = round(float(body.get("plan_day") or 0))
            elif action == "setfact":
                facts = r.get("facts") or {}
                facts[_today_str()] = round(float(body.get("fact") or 0))
                r["facts"] = facts
            else:
                return self._send(400, {"error": "неизвестное действие"})
            dept_plan_upsert(r)
            return self._send(200, {"ok": True, "departments": dept_plan_all(co)})
        if self.path.startswith("/api/team"):
            u = self._user(); secs = u.get("sections", []) if u else []
            _isown = bool(u and ("all" in secs or u.get("owner")))
            _co_t = (u.get("company") if u else None) or COMPANY_ID
            # Команду РЕДАКТИРУЕТ владелец; HR-менеджер магазина — тоже. В кабинете проката
            # (другая компания) изменять сотрудников может ТОЛЬКО владелец (админ — нет).
            if not (u and (_isown or (_co_t == COMPANY_ID and "hr" in secs))):
                return self._send(403, {"error": "Изменять сотрудников может только владелец"})
            if not supa_on():
                return self._send(503, {"error": "База не настроена — добавление недоступно"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            action = body.get("action")
            co = u.get("company") or COMPANY_ID
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
                    _newpw = (body.get("pw") or "").strip()
                    row = _emp_row(login, company=co, name=name,
                                   role=(body.get("role") or "").strip(),
                                   department=(body.get("department") or "").strip(),
                                   phone=(body.get("phone") or "").strip(),
                                   pw=(_hash_pw(_newpw) if _newpw else ""),
                                   sections=secs_in,
                                   video_rate=round(float(body.get("video_rate") or 0)),
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
                if (tu.get("company") or COMPANY_ID) != co:   # чужую компанию не трогаем
                    return self._send(403, {"error": "Это сотрудник другой компании"})
                if "all" in tu.get("sections", []):
                    return self._send(400, {"error": "Владельца нельзя менять отсюда"})
                if action == "set_salary":
                    team_save(_emp_row(login, company=co,
                                       salary_month=round(float(body.get("salary_month") or 0))))
                    return self._send(200, {"ok": True})
                elif action == "update":
                    over = {}
                    for k in ("name", "role", "department", "phone"):
                        if k in body:
                            over[k] = (body.get(k) or "").strip()
                    if "salary_month" in body:
                        over["salary_month"] = round(float(body.get("salary_month") or 0))
                    if "video_rate" in body:
                        over["video_rate"] = round(float(body.get("video_rate") or 0))
                    if "sections" in body and body.get("sections"):
                        over["sections"] = body.get("sections")
                    if "pw" in body and (body.get("pw") or "").strip():
                        _np = (body.get("pw") or "").strip()
                        if len(_np) < 4:
                            return self._send(400, {"error": "Пароль минимум 4 символа"})
                        over["pw"] = _hash_pw(_np)
                    # смена логина сотрудника владельцем (необязательно)
                    new_login = (body.get("new_login") or "").strip()
                    if new_login and new_login.lower() != login.lower():
                        if not _re.match(r"^[A-Za-z0-9_]{2,}$", new_login):
                            return self._send(400, {"error": "Логин: латиница, цифры, _ (от 2 символов)"})
                        if new_login.lower() in USERS_BY_LOGIN:
                            return self._send(400, {"error": "Логин «%s» уже занят" % new_login})
                        row = _emp_row(login, company=co, **over)
                        row["login"] = new_login
                        team_save(row)                                      # запись под новым логином (поля скопированы)
                        team_save(_emp_row(login, company=co, active=False))  # старый логин спрятать
                        return self._send(200, {"ok": True, "login": new_login})
                    team_save(_emp_row(login, company=co, **over))
                    return self._send(200, {"ok": True})
                elif action == "remove":
                    team_save(_emp_row(login, active=False))
                    return self._send(200, {"ok": True})
                else:
                    return self._send(400, {"error": "неизвестное действие"})
            except Exception as e:
                return self._send(500, {"error": "Не удалось сохранить: %s" % e})
        if self.path.startswith("/api/crm-data"):
            u = self._user()
            if not u:
                return self._send(401, {"error": "вход"})
            secs = u.get("sections", [])
            if not ("all" in secs or "crm" in secs or "deals" in secs or "chats" in secs or "dash" in secs):
                return self._send(403, {"error": "Нет доступа к CRM"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            data = body.get("data")
            if data is None:
                return self._send(400, {"error": "нет данных"})
            kv_save("crm_" + COMPANY_ID, data)
            return self._send(200, {"ok": True})
        if self.path.startswith("/api/account"):
            u = self._user()
            if not u:
                return self._send(401, {"error": "вход"})
            if not supa_on():
                return self._send(503, {"error": "База не настроена"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "плохой запрос"})
            login = (u.get("login") or "").strip()
            if not login:
                return self._send(400, {"error": "У вашего аккаунта нет логина — обратитесь к владельцу"})
            action = body.get("action")
            try:
                if action == "set_password":
                    newpw = (body.get("new_pw") or "").strip()
                    if len(newpw) < 4:
                        return self._send(400, {"error": "Пароль минимум 4 символа"})
                    team_save(_emp_row(login, pw=_hash_pw(newpw)))   # храним хешем
                    return self._send(200, {"ok": True})
                elif action == "set_login":
                    newlogin = (body.get("new_login") or "").strip()
                    if not _re.match(r"^[A-Za-z0-9_]{2,}$", newlogin):
                        return self._send(400, {"error": "Логин: латиница, цифры, _ (от 2 символов)"})
                    if newlogin.lower() != login.lower() and newlogin.lower() in USERS_BY_LOGIN:
                        return self._send(400, {"error": "Логин «%s» уже занят" % newlogin})
                    row = _emp_row(login); row["login"] = newlogin
                    team_save(row)                          # новая запись с новым логином (поля скопированы)
                    if newlogin.lower() != login.lower():
                        team_save(_emp_row(login, active=False))   # спрятать старый логин
                    return self._send(200, {"ok": True, "login": newlogin})
                elif action == "set_profile":
                    over = {}
                    if "name" in body:
                        nm = (body.get("name") or "").strip()
                        if not nm:
                            return self._send(400, {"error": "Имя не может быть пустым"})
                        over["name"] = nm
                    if "role" in body:
                        over["role"] = (body.get("role") or "").strip()
                    if "phone" in body:
                        over["phone"] = (body.get("phone") or "").strip()
                    if not over:
                        return self._send(400, {"error": "нечего сохранять"})
                    team_save(_emp_row(login, **over))
                    return self._send(200, {"ok": True, "name": over.get("name", "")})
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
                    _ins = _supa("POST", "supplier_txns", "",
                          {"company_id": COMPANY_ID, "supplier_id": sid, "type": typ,
                           "amount": round(_num(body.get("amount"))), "qty": _num(body.get("qty")),
                           "note": (body.get("note") or "").strip(), "date": day,
                           "receipt_url": receipt_url, "created_by": who})
                    # ВЫПЛАТА долга поставщику → сразу расход «Погашение долгов поставщикам»
                    if typ == "payment":
                        try:
                            _tid = _ins[0].get("id") if isinstance(_ins, list) and _ins else None
                            _snm = ""
                            _sr = _supa("GET", "suppliers",
                                        "?id=eq.%s&company_id=eq.%s&select=name" % (sid, _q(COMPANY_ID)))
                            if _sr:
                                _snm = _sr[0].get("name", "")
                            _supa("POST", "expenses", "",
                                  {"company_id": COMPANY_ID,
                                   "amount": round(_num(body.get("amount"))),
                                   "category": "Погашение долгов поставщикам",
                                   "note": _snm or (body.get("note") or "").strip(),
                                   "date": day,
                                   "src_txn": (str(_tid) if _tid is not None else None)})
                        except Exception:
                            pass
                elif action == "edit_supplier":      # исправить имя/телефон поставщика
                    sid = body.get("id")
                    name = (body.get("name") or "").strip()
                    if not sid or not name:
                        return self._send(400, {"error": "нужны поставщик и название"})
                    _supa("PATCH", "suppliers",
                          "?id=eq.%s&company_id=eq.%s" % (sid, _q(COMPANY_ID)),
                          {"name": name, "note": (body.get("note") or "").strip()})
                elif action == "edit_txn":           # исправить сумму/комментарий операции (опечатка в долге)
                    tid = body.get("id")
                    if not tid:
                        return self._send(400, {"error": "нужна операция"})
                    _eamt = round(_num(body.get("amount")))
                    _supa("PATCH", "supplier_txns",
                          "?id=eq.%s&company_id=eq.%s" % (tid, _q(COMPANY_ID)),
                          {"amount": _eamt,
                           "note": (body.get("note") or "").strip()})
                    # если это была выплата — синхронизируем связанный расход
                    try:
                        _supa("PATCH", "expenses",
                              "?company_id=eq.%s&src_txn=eq.%s" % (_q(COMPANY_ID), _q(str(tid))),
                              {"amount": _eamt})
                    except Exception:
                        pass
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
        # WhatsApp webhook — ПУБЛИЧНЫЙ (Meta проверяет адрес). Должно отвечать сырым challenge.
        if self.path.startswith("/api/wa/webhook"):
            p = _parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            mode = (p.get("hub.mode") or [""])[0]
            token = (p.get("hub.verify_token") or [""])[0]
            challenge = (p.get("hub.challenge") or [""])[0]
            if mode == "subscribe" and token == CFG.get("WA_VERIFY_TOKEN", ""):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain"); self.end_headers()
                self.wfile.write(challenge.encode("utf-8")); return
            self.send_response(403); self.end_headers(); return
        # Исходящее медиа Instagram — ПУБЛИЧНОЕ (Meta скачивает голосовое/файл по этой ссылке
        # без нашего логина). Доступ ограничен случайным одноразовым токеном.
        if self.path.startswith("/api/ig/outmedia"):
            p = _parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            tok = (p.get("k") or [""])[0]
            item = _ig_outmedia.get(tok)
            if not item:
                self.send_response(404); self.end_headers(); return
            data, mime, _ts = item
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass
            return
        if self.path.startswith("/api/auth"):
            u = self._user()
            return self._send(200 if u else 401, {"ok": bool(u),
                "name": (u or {}).get("name", ""), "sections": (u or {}).get("sections", []),
                "role": (u or {}).get("role", ""), "department": (u or {}).get("department", ""),
                "owner": bool((u or {}).get("owner")), "plan_day": (u or {}).get("plan_day", 0)})
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
            co = u.get("company") or COMPANY_ID
            if "all" in secs or "hr" in secs:      # владелец и HR-менеджер видят всех СВОЕЙ компании
                mon = None
                try:
                    q = urllib.parse.urlparse(self.path).query
                    mv = urllib.parse.parse_qs(q).get("month", [""])[0]
                    if re.match(r"^\d{4}-\d{2}$", mv or ""):
                        mon = mv
                except Exception:
                    mon = None
                return self._send(200, {"all": payroll_all(co, mon), "month": mon or _cur_month()})
            _cm, _pf, _dp = rent_commissions(co)
            _c = _cm.get((u.get("name") or "").strip(), 0) or _cm.get((u.get("login") or "").strip(), 0)
            _cp = _pf.get((u.get("name") or "").strip(), _pf.get((u.get("login") or "").strip(), _dp))
            return self._send(200, {"me": payroll_view(u, None, _c, _cp, rent_fx(co))})
        if self.path.startswith("/api/deptplan"):
            u = self._user(); secs = u.get("sections", []) if u else []
            if not (u and ("all" in secs or "hr" in secs)):   # планы отделов — владелец и HR
                return self._send(403, {"error": "Только владелец или HR"})
            return self._send(200, {"departments": dept_plan_all(u.get("company") or COMPANY_ID)})
        if self.path.startswith("/api/crm-data"):
            u = self._user()
            if not u:
                return self._send(401, {"error": "вход"})
            secs = u.get("sections", [])
            if not ("all" in secs or "crm" in secs or "deals" in secs or "chats" in secs or "dash" in secs):
                return self._send(403, {"error": "Нет доступа к CRM"})
            return self._send(200, {"data": kv_load("crm_" + COMPANY_ID)})
        if self.path.startswith("/api/amo-import"):
            u = self._user(); secs = u.get("sections", []) if u else []
            if not (u and ("all" in secs or "crm" in secs or "deals" in secs)):
                return self._send(403, {"error": "Импорт может делать владелец или менеджер CRM"})
            try:
                return self._send(200, amo_import())
            except Exception as e:
                return self._send(500, {"error": "Импорт не удался: %s" % e})
        if self.path.startswith("/api/staff"):
            u = self._user()
            if not (u and "all" in u.get("sections", [])):
                return self._send(403, {"error": "Только владелец видит персонал"})
            staff = [{"name": v.get("name", ""), "login": v.get("login", ""),
                      "sections": v.get("sections", []),
                      "role": v.get("role", ""), "department": v.get("department", ""),
                      "plan_day": v.get("plan_day", 0)}
                     for v in company_users(u.get("company") or COMPANY_ID) if "all" not in v.get("sections", []) and not v.get("owner")]
            return self._send(200, {"staff": staff, "total": len(staff)})
        if self.path.startswith("/api/rent"):
            # доступ уже проверен общим шлюзом выше (раздел "rent")
            from urllib.parse import urlparse, parse_qs
            per = (parse_qs(urlparse(self.path).query).get("period", [""])[0] or "").strip()
            _co = (self._user() or {}).get("company") or COMPANY_ID
            return self._send(200, rent_build(per or None, _co))
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
        if self.path.startswith("/api/assortment"):
            try:
                return self._send(200, get_assortment())
            except Exception as e:
                return self._send(200, {"error": "Нет связи с 1С: " + str(e), "groups": []})
        if self.path.startswith("/api/sales-history"):
            from urllib.parse import urlparse, parse_qs
            try:
                days = int(parse_qs(urlparse(self.path).query).get("days", ["14"])[0])
            except ValueError:
                days = 14
            return self._send(200, sales_history(max(1, min(days, 400))))
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
        if self.path.startswith("/api/ads/"):
            from urllib.parse import urlparse, parse_qs
            _qp = parse_qs(urlparse(self.path).query)
            q = (_qp.get("q", [""])[0] or "").strip()
            _per = (_qp.get("period", [""])[0] or "").strip()
            _since = (_qp.get("since", [""])[0] or "").strip()
            _until = (_qp.get("until", [""])[0] or "").strip()
            _camp = (_qp.get("campaign", [""])[0] or "").strip()
            try:
                if self.path.startswith("/api/ads/status"):
                    return self._send(200, ads_status())
                if self.path.startswith("/api/ads/campaigns"):
                    return self._send(200, ads_campaigns())
                if self.path.startswith("/api/ads/report"):
                    return self._send(200, ads_report(_per, _since, _until))
                if self.path.startswith("/api/ads/analysis"):
                    return self._send(200, ads_analysis(_camp, _per, _since, _until))
                if self.path.startswith("/api/ads/interests"):
                    return self._send(200, ads_search_interests(q))
                if self.path.startswith("/api/ads/geo"):
                    return self._send(200, ads_search_geo(q))
                if self.path.startswith("/api/ads/locales"):
                    return self._send(200, ads_search_locales(q))
                if self.path.startswith("/api/ads/behaviors"):
                    return self._send(200, ads_search_behaviors(q))
                if self.path.startswith("/api/ads/suggest"):
                    return self._send(200, ads_suggest_interests([n for n in q.split("||") if n.strip()]))
                if self.path.startswith("/api/ads/posts"):
                    return self._send(200, ads_list_posts())
            except Exception as e:
                return self._send(200, {"error": str(e)})
            return self._send(404, {"error": "неизвестный метод рекламы"})
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
        if self.path.startswith("/api/ig/clients"):
            try:
                return self._send(200, {"clients": ig_clients_base()})
            except Exception as e:
                return self._send(200, {"clients": [], "error": str(e)})
        if self.path.startswith("/api/wa/clients"):
            try:
                return self._send(200, {"clients": wa_clients_base()})
            except Exception as e:
                return self._send(200, {"clients": [], "error": str(e)})
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
        if self.path.startswith("/api/ig/broadcast"):
            p = _parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            account = (p.get("account") or ["all"])[0]
            try:
                return self._send(200, ig_broadcast_count(account))
            except Exception as e:
                return self._send(200, {"eligible": 0, "error": str(e)})
        if self.path.startswith("/api/ig/accounts"):
            return self._send(200, {"accounts": [{"username": a.get("username"), "ig_id": a.get("ig_id")}
                                                 for a in ig_accounts()]})
        if self.path.startswith("/api/wa/conversations"):
            try:
                return self._send(200, {"conversations": wa_conversations(),
                                        "accounts": [{"display": a.get("display"), "phone_id": a.get("phone_id")}
                                                     for a in wa_accounts()]})
            except Exception as e:
                return self._send(200, {"conversations": [], "error": str(e)})
        if self.path.startswith("/api/wa/thread"):
            p = _parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            acc = (p.get("account") or [""])[0]; cust = (p.get("customer") or [""])[0]
            if not acc or not cust:
                return self._send(400, {"error": "нужны account и customer"})
            try:
                return self._send(200, wa_thread(acc, cust))
            except Exception as e:
                return self._send(200, {"msgs": [], "error": str(e)})
        if self.path.startswith("/api/wa/media"):
            # Прокси: скачиваем медиа из Meta (нужен токен) и отдаём браузеру для показа/прослушивания.
            p = _parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            mid = (p.get("id") or [""])[0]; phone = (p.get("phone") or [""])[0]
            if not mid:
                return self._send(400, {"error": "нужен id медиа"})
            try:
                data, mime = wa_media_download(mid, phone)
            except Exception as e:
                return self._send(200, {"error": "медиа недоступно: " + str(e)})
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            # CORS — фронт на другом домене (Vercel) тянет медиа через fetch; без этого браузер блокирует ответ.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass
            return
        if self.path.startswith("/api/ig/media"):
            # Прокси входящих вложений Instagram: сервер скачивает CDN-ссылку и отдаёт браузеру
            # (у клиента прямая ссылка не открывается — CORS/срок). u = сама CDN-ссылка.
            p = _parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            u = (p.get("u") or [""])[0]; acc = (p.get("account") or [""])[0]
            if not u:
                return self._send(400, {"error": "нужна ссылка медиа"})
            try:
                data, mime = ig_media_download(u, acc)
            except Exception as e:
                return self._send(200, {"error": "медиа недоступно: " + str(e)})
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass
            return
        if self.path.startswith("/api/wa/profile"):
            p = _parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            phone = (p.get("phone") or [""])[0]
            try:
                return self._send(200, wa_business_profile(phone))
            except Exception as e:
                return self._send(200, {"error": str(e)})
        if self.path.startswith("/api/wa/accounts"):
            return self._send(200, {"accounts": [{"display": a.get("display"), "phone_id": a.get("phone_id")}
                                                 for a in wa_accounts()]})
        if self.path.startswith("/api/ig/bot"):
            return self._send(200, {"settings": _igbot_get(),
                                    "has_key": bool(CFG.get("ANTHROPIC_API_KEY")),
                                    "default_prompt": IGBOT_DEFAULT_PROMPT,
                                    "stats": kv_load("igbot_stats") or {},
                                    "errors": kv_load("igbot_errors") or []})
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
    # авто-обучение ИИ-бота по расписанию
    if CFG.get("ANTHROPIC_API_KEY"):
        threading.Thread(target=_igbot_scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
