// Полигон «только чтение»: отдаёт старую и новую версии index.html и проксирует
// ТОЛЬКО GET-запросы /api/* на боевой сервер (ответы кэшируются на диск, чтобы обе
// версии видели одинаковые данные). Любая запись (POST/PUT/DELETE) НЕ уходит на сервер:
// она только логируется, в ответ — заглушка {ok:true}.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import os from 'node:os';

const UP = 'https://shturval-backend.onrender.com';
const DIR = path.dirname(new URL(import.meta.url).pathname);
const CACHE = process.env.POLYGON_DIR ? path.join(process.env.POLYGON_DIR, 'cache') : path.join(os.tmpdir(), 'shturval-polygon', 'cache');
fs.mkdirSync(CACHE, { recursive: true });
const FILES = {
  '/old.html': process.env.OLD_HTML,                                              // версия «до» (например, git show HEAD:index.html > /tmp/old.html)
  '/new.html': process.env.NEW_HTML || path.join(DIR, '..', '..', 'index.html'),   // рабочий index.html
};
const WRITES = [];
const MISSES = [];
// параметры-«антикэш», которые не влияют на данные
const STRIP = new Set(['_', 't', 'ts', 'nocache', 'cb', 'r']);
function keyOf(method, url, login) {
  const u = new URL(url, 'http://x');
  const q = [...u.searchParams.entries()].filter(([k]) => !STRIP.has(k)).sort((a, b) => a[0].localeCompare(b[0]));
  const norm = method + ' ' + u.pathname + '?' + q.map(([k, v]) => k + '=' + v).join('&') + ' @' + (login || '');
  return { norm, file: path.join(CACHE, crypto.createHash('sha1').update(norm).digest('hex') + '.json') };
}
const cors = (req) => ({
  'Access-Control-Allow-Origin': req.headers.origin || '*',
  'Access-Control-Allow-Headers': 'Content-Type, X-Auth, X-Login, Authorization',
  'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
});
http.createServer(async (req, res) => {
  try {
    const u = new URL(req.url, 'http://localhost');
    if (req.method === 'OPTIONS') { res.writeHead(204, cors(req)); return res.end(); }
    if (FILES[u.pathname]) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      return res.end(fs.readFileSync(FILES[u.pathname]));
    }
    if (u.pathname === '/__writes') { res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end(JSON.stringify(WRITES)); }
    if (u.pathname === '/__misses') { res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end(JSON.stringify(MISSES)); }
    if (u.pathname === '/__reset') { WRITES.length = 0; MISSES.length = 0; res.writeHead(200); return res.end('ok'); }
    if (!u.pathname.startsWith('/api/')) { res.writeHead(404, cors(req)); return res.end('nf'); }
    if (req.method !== 'GET') {
      let body = ''; for await (const c of req) body += c;
      WRITES.push({ method: req.method, url: req.url, body: body.slice(0, 400) });
      res.writeHead(200, { ...cors(req), 'Content-Type': 'application/json' });
      return res.end('{"ok":true,"_blocked":true}');
    }
    const login = req.headers['x-login'] || '';
    const { norm, file } = keyOf('GET', req.url, login);
    if (process.env.NO_CACHE !== '1' && fs.existsSync(file)) {   // NO_CACHE=1 — «примерочная»: всегда свежие данные с сервера, но по-прежнему только чтение
      const c = JSON.parse(fs.readFileSync(file, 'utf8'));
      res.writeHead(c.status, { ...cors(req), 'Content-Type': c.type });
      return res.end(Buffer.from(c.body, 'base64'));
    }
    if (process.env.REPLAY_ONLY === '1') {
      MISSES.push(norm);
      res.writeHead(200, { ...cors(req), 'Content-Type': 'application/json' });
      return res.end('{"error":"нет в записи полигона"}');
    }
    const h = {}; for (const k of ['x-auth', 'x-login']) if (req.headers[k]) h[k] = req.headers[k];
    const r = await fetch(UP + req.url, { headers: h, signal: AbortSignal.timeout(90000) });
    const buf = Buffer.from(await r.arrayBuffer());
    const type = r.headers.get('content-type') || 'application/json';
    if (r.status < 500 && process.env.NO_CACHE !== '1') fs.writeFileSync(file, JSON.stringify({ norm, status: r.status, type, body: buf.toString('base64') }));
    res.writeHead(r.status, { ...cors(req), 'Content-Type': type });
    res.end(buf);
  } catch (e) {
    res.writeHead(502, cors(req)); res.end(JSON.stringify({ error: 'proxy: ' + e.message }));
  }
}).listen(8787, '127.0.0.1', () => console.log('ro-proxy :8787 →', UP));
