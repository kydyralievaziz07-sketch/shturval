// node probe.mjs /old.html mod tab "<js-выражение>"  — открыть вкладку и вычислить выражение
import puppeteer from 'puppeteer-core'; import fs from 'node:fs'; import os from 'node:os'; import path from 'node:path';
const [, , PAGE, mod, tab, expr, ...flags] = process.argv;
const env = {}; for (const l of fs.readFileSync(path.join(path.dirname(new URL(import.meta.url).pathname), '..', '..', 'backend', 'secret.env'), 'utf8').split('\n')) { const m = l.match(/^([A-Z0-9_]+)=(.*)$/); if (m) env[m[1]] = m[2].trim().replace(/^['"]|['"]$/g, ''); }
const browser = await puppeteer.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: 'new', timeout: 180000, protocolTimeout: 240000, userDataDir: fs.mkdtempSync(path.join(os.tmpdir(), 'shturval-test-')), args: ['--no-first-run', '--disable-extensions', '--mute-audio'] });
const page = await browser.newPage();
const mobile = flags.includes('--mobile');
await page.setViewport(mobile ? { width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true } : { width: 1440, height: 900 });
let pending = 0, last = Date.now();
page.on('request', r => { if (r.url().includes('/api/')) { pending++; last = Date.now(); } });
const dn = r => { if (r.url().includes('/api/')) { pending = Math.max(0, pending - 1); last = Date.now(); } };
page.on('requestfinished', dn); page.on('requestfailed', dn);
page.on('pageerror', e => console.log('PAGEERROR', e.message));
page.on('dialog', d => d.dismiss().catch(() => { }));
await page.evaluateOnNewDocument((pw, lg) => { try { localStorage.setItem('shturval_pw', pw); localStorage.setItem('shturval_login', lg); } catch (e) { } window.confirm = () => false; window.alert = () => { }; window.prompt = () => null; }, env.SITE_PASSWORD, env.OWNER_LOGIN || '');
const settle = async (max = 40000) => { const t0 = Date.now(); await new Promise(r => setTimeout(r, 500)); while (Date.now() - t0 < max) { if (pending === 0 && Date.now() - last > 1200) return; await new Promise(r => setTimeout(r, 200)); } };
await page.goto('http://localhost:8787' + PAGE, { waitUntil: 'domcontentloaded' }); await settle(60000);
if (tab) { await page.evaluate((m, t) => { if (m && m !== '-') selectModule(m); openTab(t); }, mod, tab); await settle(); }
const shot = flags.find(f => f.startsWith('--shot=')); const full = flags.includes('--full');
if (expr && expr !== '-') console.log(JSON.stringify(await page.evaluate(expr), null, 1));
if (shot) { await new Promise(r => setTimeout(r, 900)); await page.screenshot({ path: shot.slice(7), fullPage: full }); }
await browser.close();
