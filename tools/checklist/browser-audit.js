// Аудит кнопок в живой странице. Вставляется в консоль браузера, открытого на полигоне
// (http://localhost:8787), ПОСЛЕ входа. Возвращает отчёт объектом.
//
// Безопасность: confirm() всегда отвечает «нет», alert/prompt заглушены, кнопки с опасными
// словами (удалить, сбросить, отправить, опубликовать…) не нажимаются вообще.
(() => {
  const A = { errors: [], dialogs: [], reqs: [], dead: [], fails: [], clicks: 0, noEffect: [], currency: [] };
  window.__audit = A;
  window.addEventListener('error', e => A.errors.push(String(e.message)));
  window.addEventListener('unhandledrejection', e => A.errors.push('promise: ' + String(e.reason && e.reason.message || e.reason).slice(0, 120)));
  window.alert = m => A.dialogs.push('alert: ' + String(m).slice(0, 90));
  window.confirm = m => { A.dialogs.push('confirm(ОТМЕНЁН): ' + String(m).slice(0, 90)); return false; };
  window.prompt = m => { A.dialogs.push('prompt(отменён)'); return null; };
  const of = window.fetch;
  window.fetch = function (u, o) { A.reqs.push(((o && o.method) || 'GET') + ' ' + String(u)); return of.apply(this, arguments); };

  const SKIP = /удал|сброс|очист|выйти|выход|опубликов|отправ|разослать|рассылк|запустить|остановить|импорт|синхрон|сохранить|списать|оплат|провести|назначить|позвонить|архив|уволить|платёж|включить бот|выключить/i;
  const BUILTIN = new Set(['if','else','return','typeof','new','this','event','delete','void','alert','confirm','prompt','parseInt','parseFloat','Number','String','Boolean','Array','Object','JSON','Math','Date','console','document','window','setTimeout','setInterval','localStorage','encodeURIComponent','decodeURIComponent','Promise','fetch','navigator','location','history','Set','Map','isNaN','RegExp','Error','true','false','null','undefined','open']);

  let handlers = 0;
  const scan = (root, where) => {
    for (const n of root.querySelectorAll('[onclick],[onchange],[oninput],[onsubmit],[onkeydown]'))
      for (const attr of ['onclick', 'onchange', 'oninput', 'onsubmit', 'onkeydown']) {
        const code = n.getAttribute(attr); if (!code) continue; handlers++;
        for (const mm of code.matchAll(/([A-Za-z_$][\w$]*)\s*\(/g)) {
          const name = mm[1];
          if (BUILTIN.has(name) || code.slice(0, mm.index).trimEnd().endsWith('.')) continue;
          if (typeof window[name] !== 'function')
            A.dead.push({ где: where, функция: name, подпись: (n.textContent || '').trim().slice(0, 30) });
        }
      }
  };

  // синхронный проход: таймеры в фоновой панели тротлятся, поэтому без await
  const tabs = MODULES.flatMap(m => m.tabs);
  for (const tab of tabs) {
    try { openTab(tab); } catch (e) { A.fails.push(tab + ': ' + e.message); continue; }
    const sec = [...document.querySelectorAll('main section')].find(s => !s.classList.contains('hide'));
    if (!sec) continue;
    scan(sec, tab);
    if (/сом\s+сом|\$[\d\s]+сом/.test(sec.innerText || '')) A.currency.push(tab);   // двойная валюта
    const btns = [...sec.querySelectorAll('button, .btn, [role="button"]')]
      .filter(b => b.offsetParent !== null)
      .filter(b => !SKIP.test((b.textContent || '') + ' ' + (b.getAttribute('onclick') || '')));
    for (const b of btns.slice(0, 45)) {
      if (!document.body.contains(b)) continue;
      const h0 = document.body.innerHTML.length, r0 = A.reqs.length, d0 = A.dialogs.length;
      const m0 = document.querySelectorAll('.modal-bg.open').length;
      try { b.click(); A.clicks++; } catch (e) { A.fails.push(tab + ' / ' + (b.textContent || '').trim().slice(0, 20) + ': ' + e.message); continue; }
      const без = document.body.innerHTML.length === h0 && A.reqs.length === r0
        && A.dialogs.length === d0 && document.querySelectorAll('.modal-bg.open').length === m0;
      if (без) A.noEffect.push(tab + ' / ' + ((b.textContent || '').trim().slice(0, 24) || b.getAttribute('onclick')));
      document.querySelectorAll('.modal-bg.open').forEach(m => m.classList.remove('open'));
      if (typeof curTab !== 'undefined' && curTab !== tab) { try { openTab(tab); } catch (_) {} }
    }
  }
  for (const m of document.querySelectorAll('.modal-bg')) scan(m, 'модалка:' + m.id);

  // физическая кликабельность: не перекрыта ли кнопка чем-то сверху
  const перекрытые = [];
  for (const tab of tabs) {
    try { openTab(tab); } catch (_) { continue; }
    const sec = [...document.querySelectorAll('main section')].find(s => !s.classList.contains('hide'));
    if (!sec) continue;
    for (const b of sec.querySelectorAll('button, .btn, [role="button"]')) {
      if (b.offsetParent === null) continue;
      const r = b.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) { перекрытые.push({ tab, подпись: (b.textContent || '').trim().slice(0, 24), причина: 'нулевой размер' }); continue; }
      if (getComputedStyle(b).pointerEvents === 'none') { перекрытые.push({ tab, подпись: (b.textContent || '').trim().slice(0, 24), причина: 'pointer-events:none' }); continue; }
      if (r.top > innerHeight || r.bottom < 0) continue;
      const el = document.elementFromPoint(Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1),
                                           Math.min(Math.max(r.top + r.height / 2, 1), innerHeight - 1));
      if (el && el !== b && !b.contains(el) && !el.contains(b))
        перекрытые.push({ tab, подпись: (b.textContent || '').trim().slice(0, 24), перекрытоЧем: el.tagName });
    }
  }

  return {
    вкладок: tabs.length, сверокОбработчиков: handlers,
    мёртвыхКнопок: A.dead.length, мёртвые: A.dead,
    кликов: A.clicks, безЭффекта: A.noEffect,
    паденийВкладок: A.fails, jsОшибок: A.errors,
    двойнаяВалюта: A.currency, перекрытые,
  };
})()
