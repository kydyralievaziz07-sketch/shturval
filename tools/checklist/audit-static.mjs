// Аудит кликабельности: сопоставляем каждый onclick="..." с объявлением функции
// и проверяем, что она объявлена в глобальной области (иначе клик = ReferenceError).
import fs from 'node:fs';

const file = process.argv[2];
const src = fs.readFileSync(file, 'utf8');
const lines = src.split('\n');

// --- 1. Собираем объявления функций и их глубину вложенности по фигурным скобкам.
// Глубину считаем грубо: игнорируем содержимое строковых литералов и комментариев.
function depthMap(text) {
  const depths = new Array(text.length).fill(0);
  let d = 0, i = 0;
  let inS = null, inTpl = 0, inLine = false, inBlock = false, inRe = false;
  let prevSig = ''; // предыдущий значимый символ — для распознавания regex-литерала
  while (i < text.length) {
    const c = text[i], n = text[i + 1];
    depths[i] = d;
    if (inLine) { if (c === '\n') inLine = false; i++; continue; }
    if (inBlock) { if (c === '*' && n === '/') { inBlock = false; i += 2; continue; } i++; continue; }
    if (inRe) { if (c === '\\') { i += 2; continue; } if (c === '/') inRe = false; if (c === '\n') inRe = false; i++; continue; }
    if (inS) { if (c === '\\') { i += 2; continue; } if (c === inS) inS = null; i++; continue; }
    if (inTpl) {
      if (c === '\\') { i += 2; continue; }
      if (c === '`') { inTpl--; i++; continue; }
      i++; continue;
    }
    if (c === '/' && n === '/') { inLine = true; i += 2; continue; }
    if (c === '/' && n === '*') { inBlock = true; i += 2; continue; }
    if (c === '"' || c === "'") { inS = c; i++; continue; }
    if (c === '`') { inTpl++; i++; continue; }
    if (c === '/' && /[=(,:[!&|?{;+\-*%~^]/.test(prevSig)) { inRe = true; i++; continue; }
    if (c === '{') { d++; i++; if (!/\s/.test(c)) prevSig = c; continue; }
    if (c === '}') { d--; depths[i] = d; i++; prevSig = c; continue; }
    if (!/\s/.test(c)) prevSig = c;
    i++;
  }
  return depths;
}

// Работаем только по содержимому <script>…</script>
const scripts = [];
const reScript = /<script\b[^>]*>([\s\S]*?)<\/script>/gi;
let m;
while ((m = reScript.exec(src))) scripts.push({ start: m.index + m[0].indexOf(m[1]), body: m[1] });

const decls = new Map(); // имя -> [{depth, line}]
function addDecl(name, depth, line) {
  if (!decls.has(name)) decls.set(name, []);
  decls.get(name).push({ depth, line });
}
const lineOfOffset = (off) => src.slice(0, off).split('\n').length;

for (const sc of scripts) {
  const d = depthMap(sc.body);
  const patterns = [
    [/\bfunction\s+([A-Za-z_$][\w$]*)\s*\(/g, 1],
    [/\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)/g, 1],
    [/\bwindow\.([A-Za-z_$][\w$]*)\s*=/g, 1],
  ];
  for (const [re, gi] of patterns) {
    let mm;
    while ((mm = re.exec(sc.body))) {
      const off = mm.index;
      const isWindow = /^\s*window\./.test(mm[0]);
      addDecl(mm[gi], isWindow ? 0 : d[off], lineOfOffset(sc.start + off));
    }
  }
}

// --- 2. Собираем все onclick-выражения (в т.ч. внутри JS-строк, где верстка клеится строками)
const calls = [];
const reOn = /on(click|change|input|submit|keydown|keyup|keypress|mouseenter|mouseleave|focus|blur|dblclick)\s*=\s*(\\?["'])([\s\S]*?)\2/g;
while ((m = reOn.exec(src))) {
  calls.push({ ev: m[1], code: m[3], line: lineOfOffset(m.index) });
}

// имена, вызываемые внутри выражения обработчика
const BUILTIN = new Set(['if','else','return','typeof','new','this','event','delete','void','in','of','for','while','do','switch','case','try','catch','throw','var','let','const','function','true','false','null','undefined','alert','confirm','prompt','parseInt','parseFloat','Number','String','Boolean','Array','Object','JSON','Math','Date','console','document','window','setTimeout','setInterval','localStorage','sessionStorage','encodeURIComponent','decodeURIComponent','Promise','fetch','navigator','location','history','Set','Map','isNaN','RegExp','Error']);

const missing = new Map();  // имя -> [{line, ev, code}]
const nested = new Map();   // имя -> {depth, declLine, uses:[...]}

for (const c of calls) {
  const re = /([A-Za-z_$][\w$.]*)\s*\(/g;
  let mm;
  while ((mm = re.exec(c.code))) {
    const full = mm[1];
    if (full.includes('.')) continue;      // методы объектов — отдельная история
    if (BUILTIN.has(full)) continue;
    // пропускаем то, что стоит после точки (метод)
    const before = c.code.slice(0, mm.index).trimEnd();
    if (before.endsWith('.')) continue;
    const d = decls.get(full);
    if (!d) {
      if (!missing.has(full)) missing.set(full, []);
      missing.get(full).push(c);
    } else if (!d.some(x => x.depth === 0)) {
      const best = d.reduce((a, b) => (a.depth <= b.depth ? a : b));
      if (!nested.has(full)) nested.set(full, { depth: best.depth, declLine: best.line, uses: [] });
      nested.get(full).uses.push(c);
    }
  }
}

console.log('=== ВСЕГО обработчиков в разметке:', calls.length, '| объявлений функций найдено:', decls.size);
console.log('\n=== A. ФУНКЦИЯ НЕ НАЙДЕНА ВООБЩЕ (клик = ошибка) ===');
if (!missing.size) console.log('  нет');
for (const [name, uses] of [...missing].sort((a, b) => b[1].length - a[1].length)) {
  console.log(`  ${name}()  — ${uses.length} использ., строки: ${[...new Set(uses.map(u => u.line))].slice(0, 8).join(', ')}`);
}
console.log('\n=== B. ОБЪЯВЛЕНА НЕ ГЛОБАЛЬНО (возможен ReferenceError) ===');
if (!nested.size) console.log('  нет');
for (const [name, info] of [...nested].sort((a, b) => b[1].uses.length - a[1].uses.length)) {
  console.log(`  ${name}()  — объявлена на глубине ${info.depth} (строка ${info.declLine}); использ. ${info.uses.length}, строки: ${[...new Set(info.uses.map(u => u.line))].slice(0, 8).join(', ')}`);
}
