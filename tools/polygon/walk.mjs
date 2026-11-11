// Обход вкладок кабинета в безголовом Chrome (отдельный временный профиль, окно не появляется).
// node walk.mjs /old.html out-old [--mobile] [--shots] [--tabs=shop:dash,rent:rentdash]
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const [, , PAGE = '/old.html', OUT = 'out', ...flags] = process.argv;
const MOBILE = flags.includes('--mobile');
const SHOTS = flags.includes('--shots');
const tabsArg = (flags.find(f => f.startsWith('--tabs=')) || '').slice(7);
const DIR = path.dirname(new URL(import.meta.url).pathname);
const outDir = path.join(process.env.POLYGON_DIR || path.join(os.tmpdir(), 'shturval-polygon'), OUT);   // вне репозитория: внутри боевые данные
fs.mkdirSync(outDir, { recursive: true });

const env = {};
for (const l of fs.readFileSync(path.join(DIR, '..', '..', 'backend', 'secret.env'), 'utf8').split('\n')) {
  const m = l.match(/^([A-Z0-9_]+)=(.*)$/); if (m) env[m[1]] = m[2].trim().replace(/^['"]|['"]$/g, '');
}
const RENT = ['rentdash', 'renttasks', 'rentnotes', 'rentcars', 'rentlist', 'rentalert', 'rentexp', 'renthanded', 'rentanalysis', 'rentplan', 'rentclients', 'rentcalendar', 'rentgps', 'rentaudit'];
let PLAN = [
  ...['dash', 'prod', 'sales', 'assort', 'report', 'analytics', 'supl', 'fin', 'exp'].map(t => ['shop', t]),
  ...['ads', 'adsreport', 'adsanalysis'].map(t => ['mkt', t]),
  ...RENT.map(t => ['rent', t]),
  ...RENT.map(t => ['rent2', t]),
  ['dailyrep', 'reportdash'],
  ['', 'set'],
];
if (tabsArg) PLAN = tabsArg.split(',').map(s => s.split(':'));

const browser = await puppeteer.launch({
  executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  headless: 'new', timeout: 180000, protocolTimeout: 240000,
  userDataDir: fs.mkdtempSync(path.join(os.tmpdir(), 'shturval-test-')),
  args: ['--no-first-run', '--no-default-browser-check', '--disable-extensions', '--mute-audio', '--hide-scrollbars'],
});
const page = await browser.newPage();
await page.setViewport(MOBILE ? { width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true } : { width: 1440, height: 900, deviceScaleFactor: 1 });
const errors = [];
let pending = 0, lastNet = Date.now();
page.on('pageerror', e => errors.push({ type: 'pageerror', tab: cur, msg: String(e && e.message || e).slice(0, 400) }));
page.on('console', m => { if (m.type() === 'error') { const t = m.text(); if (!/Failed to load resource|net::ERR|favicon/.test(t)) errors.push({ type: 'console', tab: cur, msg: t.slice(0, 400) }); } });
const apiSeen = new Set();
page.on('request', r => { if (r.url().includes('/api/')) { pending++; lastNet = Date.now(); apiSeen.add(r.method() + ' ' + new URL(r.url()).pathname); } });
const done = r => { if (r.url().includes('/api/')) { pending = Math.max(0, pending - 1); lastNet = Date.now(); } };
page.on('requestfinished', done); page.on('requestfailed', done);
page.on('dialog', d => d.dismiss().catch(() => { }));
let cur = 'boot';
await page.evaluateOnNewDocument((pw, lg) => {
  try { localStorage.setItem('shturval_pw', pw); localStorage.setItem('shturval_login', lg); } catch (e) { }
  window.confirm = () => false; window.alert = () => { }; window.prompt = () => null;
}, env.SITE_PASSWORD, env.OWNER_LOGIN || '');

async function settle(max = 45000, quiet = 1200) {
  const t0 = Date.now();
  await new Promise(r => setTimeout(r, 400));
  while (Date.now() - t0 < max) {
    if (pending === 0 && Date.now() - lastNet > quiet) return true;
    await new Promise(r => setTimeout(r, 200));
  }
  return false;
}
await page.goto('http://localhost:8787' + PAGE, { waitUntil: 'domcontentloaded', timeout: 60000 });
await settle(90000, 2500);
const boot = await page.evaluate(() => ({
  loginHidden: (() => { const l = document.getElementById('login'); return !l || getComputedStyle(l).display === 'none'; })(),
  user: (typeof USER !== 'undefined') ? { name: USER.name, sections: USER.sections, owner: USER.owner, company: USER.company } : null,
  mods: [...document.querySelectorAll('#topmods a')].map(a => a.dataset.mod + ':' + a.textContent.trim()),
  curModule: typeof curModule !== 'undefined' ? curModule : null, curTab: typeof curTab !== 'undefined' ? curTab : null,
  version: (document.querySelector('.side-foot') || {}).textContent ? document.querySelector('.side-foot').textContent.trim().slice(0, 40) : '',
}));
console.log('boot', JSON.stringify(boot));
const result = { boot, tabs: {}, slow: [] };
for (const [mod, tab] of PLAN) {
  cur = (mod || '-') + ':' + tab;
  const ok = await page.evaluate((mod, tab) => {
    try {
      if (mod) { if (typeof MODULES !== 'undefined' && !MODULES.find(m => m.key === mod)) return 'no-module'; selectModule(mod); }
      openTab(tab); return 'ok';
    } catch (e) { return 'ERR ' + e.message; }
  }, mod, tab);
  const settled = await settle();
  await new Promise(r => setTimeout(r, 1200));   // дать закончиться анимации цифр
  if (!settled) result.slow.push(cur);
  const info = await page.evaluate((tab) => {
    const s = document.getElementById('tab-' + tab);
    const vis = s && !s.classList.contains('hide');
    return {
      visible: !!vis,
      text: s ? s.innerText.replace(/[ \t]+\n/g, '\n').replace(/\n{2,}/g, '\n').trim() : null,
      buttons: s ? s.querySelectorAll('button,[onclick]').length : 0,
      submenu: [...document.querySelectorAll('#submenu a')].map(a => a.dataset.tab).join(','),
      title: (document.getElementById('page-title') || {}).textContent,
      overflowX: document.documentElement.scrollWidth > window.innerWidth + 2,
    };
  }, tab);
  info.open = ok;
  result.tabs[cur] = info;
  if (SHOTS) await page.screenshot({ path: path.join(outDir, cur.replace(':', '_') + '.png'), fullPage: false });
  console.log(cur.padEnd(22), ok, 'vis=' + info.visible, 'len=' + (info.text || '').length, 'btn=' + info.buttons, settled ? '' : 'SLOW');
}
result.errors = errors; result.api = [...apiSeen].sort();
console.log('api:', result.api.join(' · '));
fs.writeFileSync(path.join(outDir, 'result.json'), JSON.stringify(result, null, 1));
console.log('errors:', errors.length); errors.slice(0, 30).forEach(e => console.log(' ', e.type, e.tab, e.msg));
await browser.close();
