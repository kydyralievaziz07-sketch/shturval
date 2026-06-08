# -*- coding: utf-8 -*-
"""Облачный эндпоинт /api/chat?id=... для Vercel — сообщения одного диалога из ChatPlace.
Самодостаточный, только стандартная библиотека. Требует env: SITE_PASSWORD, CHATPLACE_KEY."""
import os, json, urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


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


def build_chat_messages(cid):
    res = chatplace_call("chats_messages", {"chatId": cid, "limit": 40})
    arr = res if isinstance(res, list) else res.get("items", [])
    msgs = []
    for m in reversed(arr):
        x = (m.get("message") or "").strip()
        if not x or x.endswith("Label") or x.endswith("StatusLabel"):
            continue
        msgs.append({"t": ("in" if m.get("side") == "client" else "out"), "x": x})
    return {"msgs": msgs}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        site_pw = os.environ.get("SITE_PASSWORD", "").strip()
        if site_pw and (self.headers.get("X-Auth") or "").strip() != site_pw:
            return self._send(401, {"error": "Требуется вход"})
        cid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
        if not cid:
            return self._send(400, {"error": "нет id"})
        try:
            self._send(200, build_chat_messages(cid))
        except Exception as e:
            self._send(200, {"msgs": [], "error": "ChatPlace: " + str(e)})

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
