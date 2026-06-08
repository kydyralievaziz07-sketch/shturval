# -*- coding: utf-8 -*-
"""Облачный эндпоинт POST /api/send для Vercel — отправка ответа клиенту через ChatPlace.
Берёт диалог на себя (chats_open, пауза ИИ) и шлёт сообщение. Требует env: SITE_PASSWORD, CHATPLACE_KEY."""
import os, json, urllib.request
from http.server import BaseHTTPRequestHandler


def chatplace_call(name, arguments):
    key = os.environ.get("CHATPLACE_KEY", "").strip()
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


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        site_pw = os.environ.get("SITE_PASSWORD", "").strip()
        if site_pw and (self.headers.get("X-Auth") or "").strip() != site_pw:
            return self._send(401, {"error": "Требуется вход"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "плохой запрос"})
        cid = body.get("chatId"); text = (body.get("text") or "").strip()
        if not cid or not text:
            return self._send(400, {"error": "нужны chatId и text"})
        try:
            chatplace_call("chats_open", {"chatId": cid})
            chatplace_call("chats_send_message", {"chatId": cid, "text": text})
            self._send(200, {"ok": True})
        except Exception as e:
            self._send(200, {"ok": False, "error": "ChatPlace: " + str(e)})

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
