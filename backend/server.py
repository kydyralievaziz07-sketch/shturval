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
    # пароль для входа на сайт (если пусто — защита выключена)
    cfg["SITE_PASSWORD"] = env("SITE_PASSWORD")
    return cfg

CFG = load_secret()
BASE = "https://{}.amocrm.ru/api/v4".format(CFG.get("AMO_SUBDOMAIN", ""))

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

    # 2) последние сделки
    recent_raw = amo_get("/leads?limit=20")
    recent = []
    for l in recent_raw.get("_embedded", {}).get("leads", []):
        recent.append({
            "name": l.get("name") or "(без названия)",
            "price": l.get("price") or 0,
            "stage": status_name.get(l.get("status_id"), "—"),
        })

    # 3) сводка по этапам — собираем до 1500 последних сделок
    by_stage = {}
    total_count = 0
    total_sum = 0
    page = 1
    while page <= 6:
        data = amo_get("/leads?limit=250&page={}".format(page))
        leads = data.get("_embedded", {}).get("leads", [])
        if not leads:
            break
        for l in leads:
            st = status_name.get(l.get("status_id"), "—")
            pr = l.get("price") or 0
            agg = by_stage.setdefault(st, {"count": 0, "sum": 0})
            agg["count"] += 1
            agg["sum"] += pr
            total_count += 1
            total_sum += pr
        if len(leads) < 250:
            break
        page += 1
    sampled = (page > 6)  # упёрлись в лимит выборки

    stage_summary = [{"stage": k, "count": v["count"], "sum": v["sum"]}
                     for k, v in sorted(by_stage.items(), key=lambda x: -x[1]["sum"])]

    return {
        "account": CFG.get("AMO_SUBDOMAIN"),
        "currency": "сом",
        "pipelines": pipelines,
        "recent": recent,
        "stage_summary": stage_summary,
        "total_count": total_count,
        "total_sum": total_sum,
        "sampled": sampled,
        "updated": time.strftime("%H:%M:%S"),
    }

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

def build_chats():
    res = chatplace_call("chats_list", {"limit": 15})
    items = res.get("items", []) if isinstance(res, dict) else []
    chats = []
    for it in items:
        ts = it.get("lastMessageAt")
        tm = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
        chats.append({"id": it.get("id"), "name": it.get("clientName", "клиент"),
                      "time": tm, "status": it.get("statusName", "")})
    return {"source": "ChatPlace · Instagram", "updated": time.strftime("%H:%M:%S"), "chats": chats}

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
_goods = {"t": 0.0, "goods": None, "cats": None}
_goods_lock = threading.Lock()

def get_categories():
    now = time.time()
    if _goods["cats"] is None or now - _goods["t"] > 600:
        try:
            data = yaros_get("/categories")
            _goods["cats"] = {c.get("ID"): (c.get("TITLE") or "").strip()
                              for c in data.get("categories", [])}
        except Exception:
            _goods["cats"] = _goods["cats"] or {}
    return _goods["cats"]

def get_goods():
    """Возвращает список товаров из 1С (кэш 2 минуты). Блокировка не даёт качать 20 МБ дважды разом.
    Если кэш есть, но устарел, а обновить не вышло — отдаём старый (лучше слегка несвежее, чем ничего)."""
    now = time.time()
    if _goods["goods"] is not None and now - _goods["t"] < 120:
        return _goods["goods"]
    with _goods_lock:
        # пока ждали блокировку, другой поток мог уже обновить
        if _goods["goods"] is not None and time.time() - _goods["t"] < 120:
            return _goods["goods"]
        try:
            data = yaros_get("/goods")
            _goods["goods"] = data.get("goods", [])
            _goods["t"] = time.time()
        except Exception:
            if _goods["goods"] is None:
                raise            # совсем нет данных — пробрасываем ошибку
        return _goods["goods"]

def _warmer():
    """Фоновый поток: держит каталог 1С тёплым, чтобы запросы отвечали мгновенно."""
    while True:
        try:
            if CFG.get("YAROS_URL"):
                _goods["t"] = 0.0          # принудительно обновить
                get_goods()
                get_categories()
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
    for x in goods:
        qty = _num(x.get("QUANTITY"))
        price = _num(x.get("PRICE"))
        cost = _price_by_type(x, "Закупочная")
        total_units += qty
        retail_value += qty * price
        cost_value += qty * cost
        if qty > 0:
            in_stock += 1
            cat = cats.get(x.get("CATEGORY_ID"), "Без категории") or "Без категории"
            agg = by_cat.setdefault(cat, {"value": 0.0, "units": 0.0, "count": 0})
            agg["value"] += qty * price
            agg["units"] += qty
            agg["count"] += 1
            if qty <= 3:
                low.append({"title": x.get("TITLE", ""), "qty": int(qty)})
    categories = [{"title": k, "value": round(v["value"]),
                   "units": int(v["units"]), "count": v["count"]}
                  for k, v in sorted(by_cat.items(), key=lambda i: -i[1]["value"])][:8]
    low.sort(key=lambda i: i["qty"])
    return {
        "total_sku": len(goods),
        "in_stock": in_stock,
        "total_units": int(total_units),
        "retail_value": round(retail_value),
        "cost_value": round(cost_value),
        "margin_value": round(retail_value - cost_value),
        "categories": categories,
        "low": low[:12],
        "updated": time.strftime("%H:%M:%S"),
    }

def search_products(q, page, per=50):
    goods = get_goods()
    cats = get_categories()
    q = (q or "").strip().lower()
    if q:
        items = [x for x in goods if q in (x.get("TITLE", "") or "").lower()]
    else:
        items = goods
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

    def _authed(self):
        """True, если защита выключена или прислан верный пароль в заголовке X-Auth."""
        pw = CFG.get("SITE_PASSWORD", "").strip()
        if not pw:
            return True
        given = (self.headers.get("X-Auth") or "").strip()
        return given == pw

    def do_POST(self):
        if self.path.startswith("/api/") and not self._authed():
            return self._send(401, {"error": "Требуется вход"})
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
        self._send(404, {"error": "не найдено"})

    def do_GET(self):
        if self.path.startswith("/api/health"):
            ok = bool(CFG.get("AMO_TOKEN")) and bool(CFG.get("AMO_SUBDOMAIN"))
            return self._send(200, {"ok": ok, "account": CFG.get("AMO_SUBDOMAIN")})
        if self.path.startswith("/api/auth"):
            # проверка пароля для формы входа
            return self._send(200 if self._authed() else 401, {"ok": self._authed()})
        # всё остальное под /api/ — только с верным паролем
        if self.path.startswith("/api/") and not self._authed():
            return self._send(401, {"error": "Требуется вход"})
        if self.path.startswith("/api/overview"):
            data = cached("overview", 60, build_overview)
            return self._send(200, data)
        if self.path.startswith("/api/inventory"):
            try:
                return self._send(200, cached("inventory", 120, build_inventory))
            except Exception as e:
                return self._send(200, {"error": "Нет связи с 1С: " + str(e)})
        if self.path.startswith("/api/products"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            q = qs.get("q", [""])[0]
            try:
                page = int(qs.get("page", ["1"])[0])
            except ValueError:
                page = 1
            try:
                return self._send(200, search_products(q, max(1, page)))
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
    # фоновый прогрев каталога 1С (чтобы запросы не ждали 50 сек)
    if CFG.get("YAROS_URL"):
        threading.Thread(target=_warmer, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
