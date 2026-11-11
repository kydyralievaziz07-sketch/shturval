"""Сравнить текст вкладок двух обходов: python3 cmp.py <папка-до> <папка-после> [сколько строк различий показать]"""
import json,sys,difflib
a=json.load(open(sys.argv[1]+'/result.json'))['tabs']; b=json.load(open(sys.argv[2]+'/result.json'))['tabs']
same=0
for k in a:
    if k not in b: print('нет в новом:',k); continue
    ta,tb=(a[k]['text'] or '').lower(),(b[k]['text'] or '').lower()   # заглавные буквы в подписях — это оформление, не данные
    if ta==tb: same+=1; continue
    d=[l for l in difflib.unified_diff(ta.split('\n'),tb.split('\n'),lineterm='',n=0) if not l.startswith(('---','+++','@@'))]
    print(f'РАЗЛИЧИЕ {k}: {len(d)} строк; кнопок {a[k]["buttons"]}→{b[k]["buttons"]}')
    for l in d[:int(sys.argv[3]) if len(sys.argv)>3 else 6]: print('   ',l[:160])
print(f'одинаковый текст: {same} из {len(a)}')
bt=[k for k in a if k in b and a[k]['buttons']!=b[k]['buttons']]
print('разное число кнопок:',[(k,a[k]['buttons'],b[k]['buttons']) for k in bt])
