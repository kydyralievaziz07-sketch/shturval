// Аудит-3: кнопки без обработчика вообще + onclick, дергающие несуществующие id.
import fs from 'node:fs';
const src = fs.readFileSync(process.argv[2], 'utf8');
const lineOf = (off) => src.slice(0, off).split('\n').length;

// --- D. <button …> без onclick / type=submit / id / class-хука
const noHandler = [];
for (const m of src.matchAll(/<button\b([^>]*)>/gi)) {
  const attrs = m[1];
  if (/\bon[a-z]+\s*=/i.test(attrs)) continue;
  if (/type\s*=\s*\\?["']?submit/i.test(attrs)) continue;
  const id = (attrs.match(/\bid\s*=\s*\\?["']([^"'\\]+)/i) || [])[1];
  noHandler.push({ line: lineOf(m.index), id: id || null, attrs: attrs.trim().slice(0, 110) });
}
// какие id реально подписаны через addEventListener / .onclick=
const wired = new Set();
for (const m of src.matchAll(/getElementById\(\s*['"]([^'"]+)['"]\s*\)\s*(?:\?\.)?\s*(?:\.addEventListener|\.onclick\s*=)/g)) wired.add(m[1]);
for (const m of src.matchAll(/\$\(\s*['"]#([^'"]+)['"]\s*\)\s*(?:\?\.)?\s*(?:\.addEventListener|\.onclick\s*=)/g)) wired.add(m[1]);
// подписка через querySelectorAll по классам/атрибутам — считаем «возможно подписана»
const delegated = [...src.matchAll(/querySelectorAll\(\s*['"]([^'"]+)['"]/g)].map(m => m[1]);

console.log('=== D. <button> без inline-обработчика ===');
const suspicious = [];
for (const b of noHandler) {
  if (b.id && wired.has(b.id)) continue;                 // подписан по id — ок
  suspicious.push(b);
}
if (!suspicious.length) console.log('  нет');
for (const b of suspicious) console.log(`  строка ${b.line}: id=${b.id || '—'} | ${b.attrs}`);
console.log('\n  (селекторы querySelectorAll, через которые вешаются обработчики):');
console.log('  ', [...new Set(delegated)].slice(0, 25).join(' | '));

// --- E. getElementById в коде -> есть ли такой id в разметке или в строковых шаблонах
const idsInMarkup = new Set([...src.matchAll(/\bid\s*=\s*\\?["']([^"'\\]+)/g)].map(m => m[1]));
const missingIds = new Map();
for (const m of src.matchAll(/getElementById\(\s*['"]([^'"]+)['"]\s*\)/g)) {
  const id = m[1];
  if (idsInMarkup.has(id)) continue;
  if (!missingIds.has(id)) missingIds.set(id, []);
  missingIds.get(id).push(lineOf(m.index));
}
console.log('\n=== E. getElementById по id, которого нигде нет в разметке ===');
if (!missingIds.size) console.log('  нет');
for (const [id, lines] of missingIds) console.log(`  #${id} — строки ${[...new Set(lines)].slice(0, 8).join(', ')}`);
