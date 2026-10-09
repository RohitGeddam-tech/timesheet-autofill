"""One small check of the row-building logic. Run: python test_timesheet.py"""
import datetime as dt

from timesheet import build_rows, split_blocks, subtract

DAY = dt.date(2026, 10, 6)
START, LUNCH_END = dt.timedelta(hours=9, minutes=30), dt.timedelta(hours=14)


def t(hhmm: str) -> dt.datetime:
    return dt.datetime.combine(DAY, dt.datetime.strptime(hhmm, "%H:%M").time())


def ev(hhmm: str, kind: str, repo: str = "console") -> dict:
    return {"t": t(hhmm), "kind": kind, "repo": repo}


def spans(events: list, busy=()) -> list:
    return [(f"{s:%H:%M}", f"{e:%H:%M:%S}"[:5 if e.second == 0 else 8], len(g))
            for s, e, g in build_rows(events, list(busy), DAY, START, LUNCH_END)]


# Grouping: consecutive events of one repo and kind share a row; each row runs from the
# previous row's end (day start for the first) to its last event.
assert spans([ev("11:00", "review"), ev("11:30", "review"), ev("11:45", "closed"), ev("12:10", "review", "portal")]) == [
    ("09:30", "11:30", 2), ("11:30", "11:45", 1), ("11:45", "12:10", 1)]

# Lunch: work that begins after lunch starts at lunch end, and lunch splits a group.
assert spans([ev("12:45", "review"), ev("15:00", "review")]) == [("09:30", "12:45", 1), ("14:00", "15:00", 1)]

# After-midnight work starts at 00:00, and the working day still starts at day start.
assert spans([ev("00:20", "own"), ev("10:00", "own")]) == [("00:00", "00:20", 1), ("09:30", "10:00", 1)]

# Trimming around a fixed row: the row keeps the free piece that ends at its event...
note = (t("10:00"), t("11:00"))
assert spans([ev("11:40", "own")], [note]) == [("11:00", "11:40", 1)]
# ...a fixed row splits a group, and an event inside a fixed row with no free time before it
# lands straight after that row.
assert spans([ev("09:50", "own"), ev("10:30", "own")], [note]) == [("09:30", "09:50", 1), ("09:50", "10:00", 1)]
assert spans([ev("10:10", "own"), ev("10:30", "closed")], [note]) == [("09:30", "10:00", 1), ("11:00", "11:05", 1)]

# Midnight cut: nothing leaves the date, and events at the same minute still get a short row each.
assert spans([ev("23:58", "own"), ev("23:58", "closed")]) == [("14:00", "23:58", 1), ("23:58", "23:59:59", 1)]

assert subtract(t("09:00"), t("12:00"), [note]) == [(t("09:00"), t("10:00")), (t("11:00"), t("12:00"))]
assert split_blocks([t("09:00"), t("09:10"), t("09:40"), t("09:45")], dt.timedelta(minutes=15)) == [
    (t("09:00"), t("09:10")), (t("09:40"), t("09:45"))]

print("ok")
