#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка без сети: каталог 1С читается потоком, оборванный ответ не портит склад.
Запуск: cd backend && SUPABASE_URL= SUPABASE_KEY= python3 check_memory.py"""
import io, json, sys, urllib.request


def _boom(*a, **k):
    raise AssertionError("сеть")


urllib.request.urlopen = _boom
import server as s

fails = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


items = [{"ID": "x%d" % i, "TITLE": "Товар «ё» %d ]}\"{[" % i, "CATEGORY_ID": "cat-%d" % (i % 3), "PRICE": "12.5", "QUANTITY": str(i),
          "PRICES": [{"TYPE": "Розничная", "PRICE": "12.5"}, {"TYPE": "Закупочная", "PRICE": "7"}, {"TYPE": "Закупочная", "PRICE": "9"}],
          "DESCRIPTION": "длинное описание " * 20} for i in range(200)]
good = json.dumps({"status": "success", "goods": items, "tail": 1}, ensure_ascii=False, indent=0).replace("\n", "\r\n").encode("utf-8")

for chunk in (1, 2, 3, 7, 64, 1000, 1 << 18):
    got = list(s._iter_goods(io.BytesIO(good), chunk))
    check("все товары на любых границах кусков: %d" % chunk, got == items)
for bad, name in ((good[:len(good) // 2], "оборван посередине"), (good[:good.rindex(b"]")], "оборван перед концом списка"),
                  (b'{"status":"error","message":"out of memory"}', "ошибка 1С без списка"), (b"", "пустой ответ"),
                  (b'<html>502</html>', "не JSON")):
    try:
        list(s._iter_goods(io.BytesIO(bad)))
        check("%s: должно быть исключение" % name, False)
    except ValueError:
        check("%s: исключение" % name, True)
check("пустой список товаров допустим при разборе", list(s._iter_goods(io.BytesIO(b'{"goods":[]}'))) == [])

ids = {}
a, b = s._slim_good(items[1], ids), s._slim_good(items[4], ids)
check("нужные поля и первая закупочная цена", a == {"TITLE": items[1]["TITLE"], "CATEGORY_ID": "cat-1", "PRICE": 12.5, "QUANTITY": 1.0, "COST": 7.0})
check("id категории хранится одной копией", a["CATEGORY_ID"] is b["CATEGORY_ID"])
check("повторное облегчение не меняет товар", s._slim_good(a) == a)
check("старая копия без цен", s._slim_good({"TITLE": "x", "PRICES": None})["COST"] == 0.0)


# обновление: сбои оставляют старый каталог
class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serve(body):
    def f(req, timeout=None):
        if str(getattr(req, "full_url", req)).endswith("/goods"):
            return FakeResp(body)
        return FakeResp('{"categories":[{"ID":"cat-1","TITLE":"Кат 1"}]}'.encode("utf-8"))
    return f


s.CFG["YAROS_URL"] = "https://example.invalid/x"
s._save_catalog_backup = lambda *a: None
s._COMPANY_CFG_CACHE.clear() if hasattr(s, "_COMPANY_CFG_CACHE") else None
urllib.request.urlopen = serve(good)
s._refresh_catalog(force=True)
check("каталог обновился: %d" % len(s._goods["goods"] or []), len(s._goods["goods"] or []) == 200 and s._goods["next_try"] > 0)
keep = s._goods["goods"]
for body, name in ((good[:5000], "обрыв"), (b'{"status":"success","goods":[]}', "пустой каталог"), (b'{"status":"error"}', "ошибка 1С")):
    urllib.request.urlopen = serve(body)
    s._refresh_catalog(force=True)
    check("%s: склад остался прежним, отступаем" % name, s._goods["goods"] is keep and s._goods["fail_n"] >= 1)
urllib.request.urlopen = _boom

print()
if fails:
    sys.exit("НЕ ПРОШЛО: %d" % len(fails))
print("OK: каталог читается потоком, сбои 1С не портят склад")
