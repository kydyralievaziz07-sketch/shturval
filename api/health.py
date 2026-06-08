# -*- coding: utf-8 -*-
"""Облачный эндпоинт /api/health — проверка, что сервер жив и настроен."""
import os, json
from http.server import BaseHTTPRequestHandler

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = bool(os.environ.get("AMO_SUBDOMAIN")) and bool(os.environ.get("AMO_TOKEN"))
        body = json.dumps({"ok": ok, "account": os.environ.get("AMO_SUBDOMAIN", "")}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
