#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка GPS-пульта без сети: команды трекерам запрещены, права и личная ссылка работают,
трек и стоянки считаются верно. Запуск: cd backend && SUPABASE_URL= SUPABASE_KEY= python3 check_gps.py"""
import re, sys, urllib.request


def _boom(*a, **k):
    raise AssertionError("сеть")


urllib.request.urlopen = _boom            # ДО импорта server: любое обращение к сети сразу заметно
import server as s

fails = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


# --- только просмотр: всё, кроме читающих вызовов, отсекается до обращения к сети ---
for svc in ("unit/send_cmd", "unit/exec_cmd", "core/batch", "token/update", "token/delete",
            "core/update_data_flags", "unit/update_sensor", "report/exec_report", "avl_evts", "", None):
    try:
        s._wia_raw(svc, {})
        check("запрещён %r" % (svc,), False)
    except s.WiaReadOnly:
        check("запрещён %r" % (svc,), True)
    except AssertionError:
        check("запрещён %r (дошёл до сети!)" % (svc,), False)
for svc in sorted(s.WIA_READ_SVC):
    s.CFG.setdefault("WIALON_HOST", "https://example.invalid")
    s.CFG["WIALON_HOST"] = s.CFG["WIALON_HOST"] or "https://example.invalid"
    try:
        s._wia_raw(svc, {})
        check("читающий %s" % svc, False)
    except AssertionError:
        check("читающий %s доходит до сети" % svc, True)
src = open(s.__file__, encoding="utf-8").read()
lits = set(re.findall(r'_wia_raw\(\s*"([^"]+)"', src)) | set(re.findall(r'_gps_call\(\s*"[a-z]+",\s*"([^"]+)"', src))
check("в коде только читающие вызовы: %s" % sorted(lits), lits <= s.WIA_READ_SVC and len(lits) >= 5)
check("нет вызова с вычисляемым именем", not re.search(r'_wia_raw\(\s*[^"\s]', src.replace("def _wia_raw(svc", "")
                                                       .replace("_wia_raw(svc, params, sid, timeout)", "")))
check("нет следов команд", not re.search(r"send_cmd|exec_cmd|commandType|core/batch", src))

# --- права ---
owner, flag, emp, gpsu = {"sections": ["all"]}, {"sections": [], "owner": True}, {"sections": ["rent"]}, {"sections": ["rent_gps"]}
check("владелец видит пульт", s._allowed(owner, "/api/gps/live") and s._allowed(flag, "/api/gps/live"))
check("право rent_gps открывает пульт", s._allowed(gpsu, "/api/gps/live"))
check("сотрудник проката пульт не видит", not s._allowed(emp, "/api/gps/live"))
check("сотрудник проката работает как раньше", s._allowed(emp, "/api/rent") and s._allowed(emp, "/api/track"))

# --- личная ссылка ---
s.CFG["GPS_VIEW_KEY"] = ""
check("нет ключа на сервере — ссылка не работает", s._gps_view_user("что-угодно") is None and s._gps_view_user("") is None)
s.CFG["GPS_VIEW_KEY"] = "короткий"
check("короткий ключ не принимается", s._gps_view_user("короткий") is None)
s.CFG["GPS_VIEW_KEY"] = "k" * 43
check("верный ключ принят", (s._gps_view_user("k" * 43) or {}).get("gps_view") is True)
check("неверный ключ отклонён", s._gps_view_user("k" * 42 + "x") is None and s._gps_view_user(None) is None)
check("ссылка не владелец", not s._GPS_VIEW_USER["owner"] and "all" not in s._GPS_VIEW_USER["sections"])
for path in ("/api/expenses", "/api/sales", "/api/chats", "/api/assistant", "/api/ads"):
    check("по ссылке закрыт %s" % path, not s._allowed(s._GPS_VIEW_USER, path))


# --- состояние машины ---
def st(**k):
    r = {"lat": 42.8, "age_s": 10, "speed": 0, "ignition": False}
    r.update(k)
    return s._gps_state(r)


check("едет", st(speed=40, ignition=True) == "moving")
check("стоит заведённая", st(ignition=True) == "idle")
check("на парковке", st() == "parked")
check("молчит час — нет сигнала", st(age_s=3601, speed=40) == "offline")
check("давний сигнал со скоростью — не «едет»", st(age_s=900, speed=40) == "parked")
check("нет координат — нет сигнала", st(lat=None) == "offline")
check("связь есть, а GPS потерян — не «едет»", st(speed=60, ignition=False, pos_age_s=900) == "parked")
check("свежие координаты — едет", st(speed=60, pos_age_s=20) == "moving")

# --- разбор объекта Wialon: датчики, сырые параметры не выходят наружу ---
unit = {"id": 7, "nm": "X", "netconn": 1, "cnm": 100, "cneh": 5.5,
        "pos": {"y": 42.1, "x": 74.2, "s": 12, "c": 90, "sc": 9, "t": 1000},
        "lmsg": {"t": 1010, "p": {"io_1": 1, "io_179": 0, "pwr_ext": 13.8, "iccid": "secret"}},
        "sens": {"1": {"t": "engine operation", "n": "Зажигание", "p": "io_1"},
                 "2": {"t": "digital", "n": "Глушение", "p": "io_179"},
                 "3": {"t": "voltage", "n": "Напряжение", "p": "pwr_ext"}}}
row = s._gps_parse_unit(unit, 1020)
check("разбор полей", (row["lat"], row["lon"], row["course"], row["sats"], row["last_t"], row["age_s"]) == (42.1, 74.2, 90, 9, 1010, 10))
check("датчики", row["ignition"] is True and row["blocked"] is False and row["volt"] == 13.8)
check("состояние из данных", row["state"] == "moving")
check("сырых параметров нет", "p" not in row and "lmsg" not in row and "iccid" not in str(row))


# --- трек: стоянки, пробег, прореживание ---
def msg(t, lat, lon, sp, odo=None, sc=9):
    m = {"t": t, "pos": {"t": t, "y": lat, "x": lon, "s": sp, "sc": sc}, "p": {}}
    if odo is not None:
        m["p"]["io_16"] = odo
    return m


base = 1000000
msgs = [msg(base, 42.0, 74.0, 0, 10000)]                                   # стоит с начала дня
msgs += [msg(base + 60 * i, 42.0, 74.0, 0) for i in range(1, 11)]           # 10 минут
msgs += [msg(base + 660 + 60 * i, 42.0 + 0.005 * i, 74.0, 40) for i in range(1, 11)]   # едет на север
msgs += [msg(base + 1260 + 3600, 42.05, 74.0, 35)]                          # молчал час на месте, поехал дальше
msgs += [msg(base + 1260 + 3660, 42.055, 74.0, 35, 16000)]
msgs += [msg(base + 1260 + 3720, 42.055, 74.0, 0, 16000)]                   # приехал, день закончился стоянкой
msgs += [msg(base + 1260 + 3720 + 400, 42.055, 74.0, 0, 16000)]
out = s._gps_track_reduce(msgs, 300)
check("пробег по одометру", out["km"] == 6.0 and out["km_src"] == "odometer")
check("пробег по координатам близок", 5.5 < out["km_geo"] < 6.8)
check("три стоянки: %s" % [(x["from"] - base, x["dur_s"]) for x in out["stops"]], len(out["stops"]) == 3)
check("первая стоянка началась до начала дня", out["stops"][0].get("open_start") is True)
check("разрыв связи на месте считается стоянкой", out["stops"][1]["dur_s"] == 3600)
check("последняя стоянка не закрыта", out["stops"][2].get("open_end") is True and out["stops"][2]["dur_s"] == 400)
check("макс. скорость", out["max_speed"] == 40)
check("первая и последняя точки на месте", out["points"][0][2] == base and out["points"][-1][2] == base + 1260 + 3720 + 400)
check("в стоянке точки не копятся", len(out["points"]) < len(msgs))
check("пустой день", s._gps_track_reduce([], 300)["km"] is None and s._gps_track_reduce([], 300)["points"] == [])
bad = s._gps_track_reduce([msg(base, 0, 0, 0), msg(base + 60, 42.0, 74.0, 0, sc=0)], 300)
check("нули и плохой приём отброшены", bad["points"] == [] and bad["first_t"] is None)
many = [msg(base + 30 * i, 42.0 + 0.001 * i, 74.0, 50) for i in range(3000)]
big = s._gps_track_reduce(many, 300)
check("потолок точек: %d" % len(big["points"]), len(big["points"]) <= s.GPS_PTS_MAX + 1 and big["points"][-1][2] == many[-1]["t"])
jump = s._gps_track_reduce([msg(base, 42.0, 74.0, 50), msg(base + 10, 43.0, 75.0, 50), msg(base + 20, 42.0, 74.0, 50)], 300)
check("прыжки GPS не идут в пробег", jump["km_geo"] == 0)

moved = s._gps_track_reduce([msg(base, 42.0, 74.0, 0), msg(base + 900, 42.0, 74.0, 0),
                             msg(base + 11700, 42.9, 74.0, 0), msg(base + 12600, 42.9, 74.0, 0)], 300)
check("переезд за время молчания — две стоянки, а не одна посередине: %s" % [x["lat"] for x in moved["stops"]],
      len(moved["stops"]) == 2 and moved["stops"][0]["lat"] == 42.0 and moved["stops"][1]["lat"] == 42.9)

# --- даты истории ---
import time
today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + s.WIA_TZ))
check("сегодня принято", s._gps_day_bounds(today) is not None)
for wrong in ("", "2026-13-01", "2026-02-30", "abc", "2020-01-01", "2999-01-01", "../x"):
    check("дата отклонена: %r" % wrong, s._gps_day_bounds(wrong) is None)
b = s._gps_day_bounds(today)
check("сутки начинаются в полночь по Бишкеку", (b[0] + s.WIA_TZ) % 86400 == 0 and b[1] <= time.time() + 1)

print()
if fails:
    sys.exit("НЕ ПРОШЛО: %d" % len(fails))
print("OK: GPS-пульт — только просмотр, проверки пройдены")
