// Чистка мёртвого кода в однофайловом index.html.
// Режимы:
//   node dce.mjs roots  <file>            — показать верхнеуровневые «корни» (не объявления) в скриптах
//   node dce.mjs report <file>            — что станет мёртвым (ничего не меняет)
//   node dce.mjs apply  <file> <outfile>  — вырезать мёртвые объявления
// Правило живости (осторожное): объявление живо, если его имя как слово встречается где-либо
// в живом тексте: HTML вне <script>, верхнеуровневые не-объявления, тела уже живых объявлений.
import fs from 'node:fs';
import * as acorn from 'acorn';

const [, , MODE, FILE, OUT] = process.argv;
const html = fs.readFileSync(FILE, 'utf8');
const scripts = [];
const re = /<script>([\s\S]*?)<\/script>/g; let m;
while ((m = re.exec(html))) scripts.push({ start: m.index + '<script>'.length, code: m[1] });
const lineOf = (pos) => html.slice(0, pos).split('\n').length;

// корни (не объявления), которые относятся к убранным разделам — считаем мёртвыми
const KILL = [
  /^let TEMPLATES=/, /^\(function\(\)\{ if\(Array\.isArray\(TEMPLATES\)/,
  /getElementById\('replyBtn'\)/,
  /^try\{ const s=JSON\.parse\(localStorage\.getItem\('shturval_crm'\)/,
  /^migrateStatuses\(\)/, /^purgeImported\(\)/, /^dedupeClients\(\)/, /^rebuildCrmIndex\(\)/,
  /^try\{ NOTIF_ON=/, /^try\{ AUTO_ON=/, /^try\{ CK_HIDDEN=/, /^try\{ BOTFIX=/,
  /^document\.addEventListener\('contextmenu'/, /^document\.addEventListener\('touchstart',function\(e\)\{ const b=e\.target\.closest&&e\.target\.closest\('\.bubble'\)/,
  /^document\.addEventListener\('touch(end|move|cancel)',_cancelPress\)/,
];
const units = [];   // {si, start, end, names[], decl:boolean, text}
scripts.forEach((s, si) => {
  const ast = acorn.parse(s.code, { ecmaVersion: 2022, sourceType: 'script' });
  for (const n of ast.body) {
    let names = [], decl = false;
    if (n.type === 'FunctionDeclaration') { names = [n.id.name]; decl = true; }
    else if (n.type === 'VariableDeclaration') {
      decl = true;
      for (const d of n.declarations) {
        if (d.id.type === 'Identifier') names.push(d.id.name); else decl = false;   // деструктуризацию не трогаем
        // объявление с побочным эффектом в инициализаторе (вызов) — считаем корнем
        if (d.init && /CallExpression|NewExpression|AwaitExpression/.test(d.init.type) ) {
          const src = s.code.slice(d.init.start, d.init.end);
          if (!/^(new (Map|Set|Date)\(|Object\.create\(null\)|\(function\s*\(\)\s*\{\s*try\s*\{\s*return (JSON\.parse|localStorage))/.test(src)) decl = false;
        }
      }
    }
    const text = s.code.slice(n.start, n.end);
    const killed = KILL.some(rx => rx.test(text));
    if (killed) { decl = true; }
    units.push({ si, start: s.start + n.start, end: s.start + n.end, names, decl, killed, type: n.type, text });
  }
});

if (MODE === 'roots') {
  for (const u of units) if (!u.decl) console.log(String(lineOf(u.start)).padStart(5), u.type.padEnd(22), u.text.replace(/\s+/g, ' ').slice(0, 170));
  console.log('units', units.length, 'decl', units.filter(u => u.decl).length);
  process.exit(0);
}

// живой текст: всё вне скриптов + корни
let outside = html;
for (const s of [...scripts].reverse()) outside = outside.slice(0, s.start) + outside.slice(s.start + s.code.length);
const live = new Set(units.filter(u => !u.decl));
let liveText = outside + '\n' + [...live].map(u => u.text).join('\n');
const wordRe = (name) => new RegExp('(^|[^A-Za-z0-9_$])' + name.replace(/\$/g, '\\$') + '([^A-Za-z0-9_$]|$)');
let changed = true;
const pendingUnits = units.filter(u => u.decl);
while (changed) {
  changed = false;
  for (const u of pendingUnits) {
    if (live.has(u)) continue;
    if (u.names.some(nm => wordRe(nm).test(liveText))) { live.add(u); liveText += '\n' + u.text; changed = true; }
  }
}
if (MODE === 'why') {
  const nm = OUT; const rx = wordRe(nm);
  if (rx.test(outside)) console.log('HTML вне скриптов');
  for (const u of live) if (rx.test(u.text) && !u.names.includes(nm)) console.log(String(lineOf(u.start)).padStart(5), u.decl ? u.names.join(',') : '[корень] ' + u.text.replace(/\s+/g, ' ').slice(0, 80));
  process.exit(0);
}
const dead = units.filter(u => !live.has(u));
const bytes = dead.reduce((a, u) => a + (u.end - u.start), 0);
console.log('объявлений всего:', pendingUnits.length, '· мёртвых:', dead.length, '· байт:', bytes);
if (MODE === 'report') {
  for (const u of dead) console.log(String(lineOf(u.start)).padStart(5), (u.end - u.start + '').padStart(6), u.names.join(','));
  process.exit(0);
}
if (MODE === 'apply') {
  let out = html;
  for (const u of [...dead].sort((a, b) => b.start - a.start)) {
    // вырезаем вместе с хвостом строки, если после объявления только пробелы
    let end = u.end; while (out[end] === ' ' || out[end] === '\t' || out[end] === ';') end++; if (out[end] === '\n') end++;
    let start = u.start; while (start > 0 && (out[start - 1] === ' ' || out[start - 1] === '\t')) start--;
    out = out.slice(0, start) + out.slice(end);
  }
  fs.writeFileSync(OUT, out);
  console.log('записано', OUT, out.length, 'байт (было', html.length + ')');
}
