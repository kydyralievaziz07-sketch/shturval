// Полигон для аудита кнопок Штурвала: отдаёт локальный index.html и подменяет боевое API
// фейковыми ответами. Все POST только логируются — ничего никуда не пишется.
import http from 'node:http';
import fs from 'node:fs';

import path from 'node:path';
import { fileURLToPath } from 'node:url';

// index.html из корня репозитория (его же отдают Vercel/Pages и Render для Бизмарта)
const HTML = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'index.html');
const LOG = [];
const j = (o) => JSON.stringify(o);

const money = (n) => n;
const emp = (login, name, role, dep) => ({
  login, name, role, department: dep, phone: '+996700000000', sections: ['dash'],
  salary_month: 30000, salary_som: 30000, daily_rate: 1050, bonus_month: 6000,
  present_days: 12, partial_days: 0, absent_days: 2, weekend_days: 4, deduct_days: 0,
  hours_month: 96, hours_today: 8, hours: {}, hourly_rate: 130, shift_hours: 12,
  is_video: false, video_rate: 0, videos_month: 0, videos_today: 0, videos: {},
  accrued: 21000, bonus: 3000, advance: 5000, commission: 0, commission_pct: 10,
  to_receive: 19000, days: {}, marked_today: false, marked_absent_today: false,
});
const STAFF = [
  emp('bek', 'Бексултан', 'Продавец', 'Носочные'),
  emp('aida', 'Айда', 'Кассир', 'Посуда'),
];

// --- фейковые данные по форме боевых ответов, чтобы таблицы и кнопки в них отрисовались
const MONTHS = ['2026-04', '2026-05', '2026-06', '2026-07', '2026-08'];
const byMonth = MONTHS.map((m, i) => ({ month: m, sales: 4000000 + i * 300000, profit: 900000 + i * 50000, expense: 600000 + i * 20000, net: 300000 + i * 30000 }));
const expRows = MONTHS.flatMap((m, i) => ([
  { id: 'e' + i + 'a', date: m + '-05', category: 'Аренда', amount: 350000, note: 'аренда помещения', manual: true },
  { id: 'e' + i + 'b', date: m + '-12', category: 'Зарплата', amount: 220000, note: 'выплата', manual: true },
  { id: 'e' + i + 'c', date: m + '-20', category: 'Реклама', amount: 45000, note: 'таргет', manual: true },
]));
const salesByDay = Array.from({ length: 60 }, (_, i) => {
  const d = new Date(2026, 5, 18 + i);
  return { date: d.toISOString().slice(0, 10), sales: 120000 + (i % 7) * 9000, profit: 28000 + (i % 5) * 2000 };
});
const PRODUCTS = Array.from({ length: 24 }, (_, i) => ({
  id: 'p' + i, code: String(1000 + i), title: 'Товар ' + (i + 1), category: ['Чемоданы', 'Посуда', 'Носочные'][i % 3],
  price: 1500 + i * 120, cost: 900 + i * 70, qty: (i % 6) === 0 ? 0 : (i % 9), stock: i % 9,
}));
const GROUPS = ['Чемоданы', 'Посуда', 'Носочные'].map((g, i) => ({
  group: g, count: 8, sales: 1200000 - i * 200000, profit: 300000 - i * 40000, qty: 400 - i * 50,
  items: PRODUCTS.filter(p => p.category === g).map(p => ({ ...p, name: p.title, sales: p.price * 20, profit: (p.price - p.cost) * 20, qty: 20 })),
}));
const CARS = ['Camry 2018', 'Alphard', 'Lexus RX'].map((model, i) => ({
  id: 'c' + i, model, plate: '01KG' + (100 + i), status: i === 0 ? 'В аренде' : 'Свободна',
  day_price: 45 + i * 15, photo: '', year: 2018 + i, vin: 'VIN' + i,
}));
const RENTALS = CARS.map((c, i) => ({
  id: 'r' + i, car_id: c.id, model: c.model, renter: 'Клиент ' + (i + 1), phone: '+996700' + (100000 + i),
  start: '2026-08-0' + (i + 1), end: '2026-08-2' + (i + 1), days: 20, price: c.day_price, got: c.day_price * 20,
  paid: c.day_price * 18, debt: c.day_price * 2, status: 'active', return_time: '18:00', manager: 'bek',
}));

const ROUTES = {
  '/api/auth': { ok: true, name: 'Владелец', login: 'owner', owner: true, sections: ['all'], role: 'all', company: 'bizmart' },
  '/api/account': { ok: true, name: 'Владелец', login: 'owner', owner: true, sections: ['all'], phone: '', avatar: '' },
  '/api/payroll': { all: STAFF, month: '2026-08' },
  '/api/staff': { staff: STAFF.map(s => ({ ...s, plan_day: 15000 })) },
  '/api/team': { team: STAFF },
  '/api/deptplan': { departments: [{ department: 'Носочные', plan_month: 9000000, done: 4000000, steps: [] }] },
  '/api/crm-data': { leads: [], deals: [], stages: [], pipelines: [], contacts: [] },
  '/api/overview': { periods: {}, all: {} },
  '/api/products': { items: PRODUCTS, products: PRODUCTS, total: PRODUCTS.length, page: 1, pages: 2 },
  '/api/categories': { categories: [{ name: 'Чемоданы', count: 8 }, { name: 'Посуда', count: 8 }, { name: 'Носочные', count: 8 }] },
  '/api/inventory': { items: PRODUCTS },
  '/api/sales': { sales: salesByDay, total: 7200000, by_dept: GROUPS.map(g => ({ department: g.group, sales: g.sales, profit: g.profit })) },
  '/api/sales-history': { days: salesByDay },
  '/api/assortment': {
    groups: GROUPS, items: GROUPS.flatMap(g => g.items), by_month: byMonth,
    floors: [{ floor: '1 этаж', groups: GROUPS }],
    tree: [{ floor: '1 этаж', sales: 2400000, profit: 500000, categories: GROUPS.map(g => ({ category: g.group, sales: g.sales, profit: g.profit, items: g.items })) }],
  },
  '/api/expenses': {
    expenses: expRows, by_category: [{ category: 'Аренда', amount: 1750000 }, { category: 'Зарплата', amount: 1100000 }, { category: 'Реклама', amount: 225000 }],
    by_month: byMonth, sales_by_day: salesByDay, total_count: expRows.length,
    periods: { today: { sales: 120000, profit: 30000, expense: 12000, net: 18000 }, week: { sales: 840000, profit: 200000, expense: 90000, net: 110000 }, month: { sales: 3600000, profit: 860000, expense: 615000, net: 245000 } },
  },
  '/api/suppliers': { suppliers: [{ id: 's1', name: 'Поставщик А', phone: '+996700111222', debt: 120000, note: '' }] },
  '/api/dailyrep': { reports: [{ id: 'd1', date: '2026-08-15', cashier: 'Айда', cash: 120000, terminal: 40000, total: 160000 }], today: null },
  '/api/rent': {
    summary: { commission_pct: 10, total: 100000 },
    data: { cars: CARS, rentals: RENTALS, expenses: [{ id: 'x1', date: '2026-08-03', car_id: 'c0', category: 'Мойка', amount: 500, note: '' }],
            handed: [], months: MONTHS, staff: STAFF, rtasks: [{ id: 't1', title: 'Помыть Camry', assignee: 'bek', done: false, due: '2026-08-17' }],
            notes: [{ id: 'n1', text: 'Заметка', date: '2026-08-10' }] },
  },
  '/api/chats': { chats: [] },
  '/api/ig/conversations': { items: [], conversations: [] },
  '/api/ig/thread': { messages: [] },
  '/api/ig/bot': { enabled: false, wa_fallback: false, sources: [], errors: [] },
  '/api/wa/conversations': { items: [], conversations: [] },
  '/api/wa/thread': { messages: [] },
  '/api/wa/profile': { ok: true },
  '/api/wa/template': { templates: [] },
  '/api/tg/conversations': { items: [] },
  '/api/ads/status': { ok: true, campaigns: [], account: 'Bizmart_women', currency: 'USD' },
  '/api/ads/report': { rows: [], totals: {} },
  '/api/ads/posts': { posts: [] },
  '/api/ads/suggest': { suggestions: [] },
  '/api/wbads': { campaigns: [] },
  '/api/wborders': { orders: [] },
  '/api/wbfinance': { rows: [] },
  '/api/bot-feedback': { items: [] },
  '/api/assistant': { answer: 'фейковый ответ полигона' },
  '/api/chat': { answer: 'фейковый ответ полигона' },
};

http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const p = url.pathname;
  const cors = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': '*',
    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
  };
  if (req.method === 'OPTIONS') { res.writeHead(204, cors); return res.end(); }

  if (p === '/' || p === '/index.html') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', ...cors });
    return res.end(fs.readFileSync(HTML));
  }
  if (p === '/__log') {
    res.writeHead(200, { 'Content-Type': 'application/json', ...cors });
    return res.end(j(LOG));
  }
  let body = '';
  req.on('data', c => (body += c));
  req.on('end', () => {
    LOG.push({ m: req.method, p, q: url.search, body: body.slice(0, 300) });
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', ...cors });
    if (req.method !== 'GET') return res.end(j({ ok: true, view: STAFF[0] }));
    const data = ROUTES[p];
    res.end(j(data !== undefined ? data : { ok: true, items: [], rows: [] }));
  });
}).listen(8787, () => console.log('полигон на http://localhost:8787'));
