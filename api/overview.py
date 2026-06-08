# -*- coding: utf-8 -*-
"""Облачный эндпоинт /api/overview для Vercel. Самодостаточный, только стандартная библиотека.
Токен и поддомен берутся из переменных окружения Vercel (AMO_TOKEN, AMO_SUBDOMAIN)."""
import os, json, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler

MAX_PAGES = 4  # до 1000 сделок — чтобы укладываться в лимит времени облака

def amo_get(subdomain, token, path):
    url = "https://{}.amocrm.ru/api/v4{}".format(subdomain, path)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8").strip()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"_error": "HTTP {}".format(e.code)}
    except Exception as e:
        return {"_error": str(e)}

def build_overview(subdomain, token):
    pipes = amo_get(subdomain, token, "/leads/pipelines")
    status_name, pipelines = {}, []
    for p in pipes.get("_embedded", {}).get("pipelines", []):
        stages = []
        for s in p.get("_embedded", {}).get("statuses", []):
            status_name[s["id"]] = s["name"]; stages.append(s["name"])
        pipelines.append({"name": p["name"], "stages": stages})

    recent_raw = amo_get(subdomain, token, "/leads?limit=20")
    recent = [{
        "name": l.get("name") or "(без названия)",
        "price": l.get("price") or 0,
        "stage": status_name.get(l.get("status_id"), "—"),
    } for l in recent_raw.get("_embedded", {}).get("leads", [])]

    by_stage, total_count, total_sum, page = {}, 0, 0, 1
    while page <= MAX_PAGES:
        data = amo_get(subdomain, token, "/leads?limit=250&page={}".format(page))
        leads = data.get("_embedded", {}).get("leads", [])
        if not leads:
            break
        for l in leads:
            st = status_name.get(l.get("status_id"), "—"); pr = l.get("price") or 0
            agg = by_stage.setdefault(st, {"count": 0, "sum": 0})
            agg["count"] += 1; agg["sum"] += pr
            total_count += 1; total_sum += pr
        if len(leads) < 250:
            break
        page += 1
    sampled = (page > MAX_PAGES)

    stage_summary = [{"stage": k, "count": v["count"], "sum": v["sum"]}
                     for k, v in sorted(by_stage.items(), key=lambda x: -x[1]["sum"])]
    return {
        "account": subdomain, "currency": "сом",
        "pipelines": pipelines, "recent": recent,
        "stage_summary": stage_summary,
        "total_count": total_count, "total_sum": total_sum,
        "sampled": sampled, "updated": time.strftime("%H:%M:%S"),
    }

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # защита паролем: если задан SITE_PASSWORD — без верного заголовка данные не отдаём
        site_pw = os.environ.get("SITE_PASSWORD", "").strip()
        if site_pw:
            given = (self.headers.get("X-Auth") or "").strip()
            if given != site_pw:
                return self._send(401, {"error": "Требуется вход"})
        subdomain = os.environ.get("AMO_SUBDOMAIN", "").strip()
        token = os.environ.get("AMO_TOKEN", "").strip()
        if not subdomain or not token:
            return self._send(500, {"error": "Не заданы AMO_SUBDOMAIN / AMO_TOKEN в настройках Vercel"})
        self._send(200, build_overview(subdomain, token))

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
