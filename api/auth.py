# -*- coding: utf-8 -*-
"""Облачный эндпоинт /api/auth для Vercel — проверка пароля формы входа.
Возвращает 200, если защита выключена или прислан верный пароль (заголовок X-Auth),
иначе 401. Пароль берётся из переменной окружения Vercel SITE_PASSWORD."""
import os, json
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        site_pw = os.environ.get("SITE_PASSWORD", "").strip()
        given = (self.headers.get("X-Auth") or "").strip()
        ok = (not site_pw) or (given == site_pw)
        self._send(200 if ok else 401, {"ok": ok})

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
