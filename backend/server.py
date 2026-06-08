#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Штурвал — бэкенд-«посредник» между amoCRM и сайтом.
Токен хранится здесь, на сервере, и НЕ попадает в код сайта.
Запуск: python3 server.py   (или двойной клик по «Запустить-сервер.command»)
"""
import os, json, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8787
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
    cfg["AMO_TOKEN"] = os.environ.get("AMO_TOKEN", cfg.get("AMO_TOKEN", ""))
    cfg["AMO_SUBDOMAIN"] = os.environ.get("AMO_SUBDOMAIN", cfg.get("AMO_SUBDOMAIN", ""))
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

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        if self.path.startswith("/api/health"):
            ok = bool(CFG.get("AMO_TOKEN")) and bool(CFG.get("AMO_SUBDOMAIN"))
            return self._send(200, {"ok": ok, "account": CFG.get("AMO_SUBDOMAIN")})
        if self.path.startswith("/api/overview"):
            data = cached("overview", 60, build_overview)
            return self._send(200, data)
        if self.path.startswith("/api/chats"):
            p = os.path.join(HERE, "chats-data.json")
            if os.path.exists(p):
                data = json.load(open(p, encoding="utf-8"))
            else:
                data = {"chats": []}
            return self._send(200, data)
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
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
