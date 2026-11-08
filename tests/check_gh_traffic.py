"""Checks for the logic a live run can't exercise. Run: python tests/check_gh_traffic.py"""
import io
import sqlite3
import sys
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gh_traffic as t


def fresh():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(t.SCHEMA)
    return db


def repo(name="me/a", rid=1, stars=1, forks=0, private=False):
    return {"id": rid, "full_name": name, "private": private, "stargazers_count": stars,
            "forks_count": forks, "open_issues_count": 0, "owner": {"login": "me"}}


def data(views=(("2026-11-01", 5, 2),), referrers=(("x.org", 3, 1),), dl=4, total=None, fans=()):
    buckets = [{"timestamp": f"{d}T00:00:00Z", "count": c, "uniques": u} for d, c, u in views]
    return {
        "views": {"count": total if total is not None else sum(b["count"] for b in buckets),
                  "uniques": 2, "views": buckets},
        "clones": {"count": 0, "uniques": 0, "clones": []},
        "referrers": [{"referrer": k, "count": c, "uniques": u} for k, c, u in referrers],
        "paths": [],
        "releases": [{"tag_name": "v1", "assets": [{"name": "a.zip", "download_count": dl}]}],
        "stargazers": [{"login": f} for f in fans],
    }


def daily(db, day="2026-11-01"):
    return tuple(db.execute("SELECT count, uniques FROM daily WHERE day=?", (day,)).fetchone())


# 1. Rerun on the same data must not double anything.
db = fresh()
t.store(db, "2026-11-07", repo(), data())
t.store(db, "2026-11-07", repo(), data())
assert daily(db) == (5, 2)
assert db.execute("SELECT SUM(count) FROM daily").fetchone()[0] == 5
assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1

# 2. A smaller value for a saved day (window-edge partial) must not shrink it.
t.store(db, "2026-11-08", repo(), data(views=(("2026-11-01", 1, 1),)))
assert daily(db) == (5, 2), "partial day overwrote the full one"

# 3. A larger value (today still filling up) must win.
t.store(db, "2026-11-08", repo(), data(views=(("2026-11-01", 9, 3),)))
assert daily(db) == (9, 3)

# 4. Same-day rerun: a referrer that fell off the list must disappear.
db = fresh()
t.store(db, "2026-11-07", repo(), data(referrers=(("old.org", 3, 1), ("keep.org", 2, 1))))
t.store(db, "2026-11-07", repo(), data(referrers=(("keep.org", 2, 1),)))
keys = [r["key"] for r in db.execute("SELECT key FROM ranked WHERE kind='referrer'")]
assert keys == ["keep.org"], keys

# 5. Rename keeps history: same id, new name, one repo.
db = fresh()
t.store(db, "2026-11-07", repo("me/old"), data())
t.store(db, "2026-11-08", repo("me/new"), data(views=(("2026-11-02", 7, 1),)))
assert [r["name"] for r in db.execute("SELECT name FROM repos")] == ["me/new"]
assert db.execute("SELECT SUM(count) FROM daily WHERE repo_id=1").fetchone()[0] == 12

# 6. Deltas compare with the previous day, and survive a same-day rerun.
db = fresh()
t.store(db, "2026-11-06", repo(stars=3), data(dl=10, fans=("a", "b", "c")))
t.store(db, "2026-11-07", repo(stars=4), data(dl=12, fans=("a", "b", "c", "d")))
t.store(db, "2026-11-07", repo(stars=4), data(dl=12, fans=("a", "b", "c", "d")))
out = io.StringIO()
with redirect_stdout(out):
    assert t.summary(db, [1])
assert "4 (+1)" in out.getvalue() and "12 (+2)" in out.getvalue(), out.getvalue()

# 7. A repo that failed today is flagged, not shown as if current.
t.store(db, "2026-11-08", repo("me/b", rid=2), data())
out = io.StringIO()
with redirect_stdout(out):
    t.summary(db, [1, 2])
assert "[stale: 2026-11-07]" in out.getvalue(), out.getvalue()

# 8. collect(): one bad repo warns, the good one still saves, nothing half-written.
real_fetch = t.fetch_repo


def fake_fetch(r):
    if r["id"] == 2:
        raise RuntimeError("HTTP 403")
    return data()


t.fetch_repo = fake_fetch
db = fresh()
err = io.StringIO()
with redirect_stderr(err):
    failed = t.collect(db, [repo(), repo("me/b", rid=2)], "2026-11-07")
t.fetch_repo = real_fetch
assert failed == ["me/b"] and "HTTP 403" in err.getvalue()
assert [r["repo_id"] for r in db.execute("SELECT repo_id FROM snapshots")] == [1]
assert db.execute("SELECT COUNT(*) FROM daily WHERE repo_id=2").fetchone()[0] == 0

# 9. Gap warning fires past 14 days only.
db = fresh()
t.store(db, "2026-10-01", repo(), data())
assert t.gap_warning(db, "2026-10-15") is None
msg = t.gap_warning(db, "2026-10-20")
assert "19 days ago" in msg and "about 5 days" in msg, msg

# 10. App table: most visitors first, uniques total is a ceiling, TOTAL last.
sys.argv = ["x"]
import gh_traffic_app as app
db = fresh()
d1 = data(views=(("2026-11-01", 5, 2),), fans=("me",))
d2 = data(views=(("2026-11-01", 9, 7),), fans=("me", "x", "y"))
d2["views"]["uniques"] = 7
t.store(db, "2026-11-07", repo("me/low"), d1)
t.store(db, "2026-11-07", repo("me/high", rid=2, private=True), d2)
rows = app.table_rows(db, [1, 2])
assert rows[0] == ("high  (private)", 9, 7, 0, 0, 9, 7, 0, 0, 2), rows
assert rows[1] == ("low", 5, 2, 0, 0, 5, 2, 0, 0, 0), rows
assert rows[2] == ("TOTAL", 14, "up to 9", 0, "up to 0", 14, "up to 9", 0, "up to 0", 2), rows
assert app.table_rows(db, []) == []
assert app.owner_name(db, [1, 2]) == "me" and app.owner_name(db, []) is None

# 11. Owner's star is left out, raw count kept, and the CLI prints the left-out number.
db = fresh()
t.store(db, "2026-11-07", repo(stars=2), data(fans=("me", "x")))
snap = db.execute("SELECT stars, stars_others FROM snapshots").fetchone()
assert tuple(snap) == (2, 1), tuple(snap)

# 12. A database from before stars_others upgrades, keeps its rows, and accepts new ones.
import os, tempfile
old_schema = t.SCHEMA.replace("    stars_others INTEGER NOT NULL,   -- stars minus the owner's own" + chr(10), "")
assert "stars_others" not in old_schema
path = os.path.join(tempfile.mkdtemp(), "old.db")
raw = sqlite3.connect(path)
raw.executescript(old_schema)
raw.execute("INSERT INTO snapshots VALUES (1,'2026-11-06',3,0,0,0,0,0,0,0)")
raw.commit(); raw.close()
real_path, t.DB_PATH = t.DB_PATH, path
db = t.open_db()
db2 = t.open_db()  # second open must not re-migrate
db2.close()
t.store(db, "2026-11-07", repo(stars=2), data(fans=("me", "x")))
rows = [tuple(r) for r in db.execute("SELECT day, stars, stars_others FROM snapshots ORDER BY day")]
assert rows == [("2026-11-06", 3, 3), ("2026-11-07", 2, 1)], rows
db.close()
t.DB_PATH = real_path

# 13. All-time uniques: finished spans that don't overlap replace their days' sums.
db = fresh()
t.store(db, "2026-11-01", repo(), data(views=()))
db.executemany("INSERT INTO daily VALUES (1,?,'views',?,3)",
               [("2026-11-0%d" % d, 5) for d in (1, 2, 3, 4)])
assert t.unique_people(db, 1, "views") == 12            # no spans: 3+3+3+3
db.execute("INSERT INTO windows VALUES (1,'views','2026-11-01','2026-11-02',4)")
assert t.unique_people(db, 1, "views") == 10            # 4 + 3 + 3
db.execute("INSERT INTO windows VALUES (1,'views','2026-11-02','2026-11-03',4)")
assert t.unique_people(db, 1, "views") == 10            # overlaps the first: ignored
db.execute("INSERT INTO windows VALUES (1,'views','2026-11-03','2026-11-04',5)")
assert t.unique_people(db, 1, "views") == 9             # 4 + 5
assert t.unique_people(db, 1, "clones") == 0

# 14. A span is only kept once all its days are finished.
db = fresh()
t.store(db, "2026-11-01", repo(), data(views=(("2026-11-01", 5, 2),)))
assert db.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 0
t.store(db, "2026-11-02", repo(), data(views=(("2026-11-01", 5, 2),)))
assert [tuple(r) for r in db.execute("SELECT first_day, last_day, uniques FROM windows")] \
    == [("2026-11-01", "2026-11-01", 2)]

# 15. Record health: first day and the days nobody saved.
db = fresh()
t.store(db, "2026-11-09", repo(), data(views=(("2026-11-01", 1, 1), ("2026-11-02", 0, 0),
                                              ("2026-11-05", 1, 1))))
assert t.record_health(db, [1]) == ("2026-11-01", 2)
assert t.record_health(db, [99]) == (None, 0)

# 16. CLI summary prints the all-time columns and the record line.
out = io.StringIO()
with redirect_stdout(out):
    assert t.summary(db, [1])
assert "all visitors" in out.getvalue() and "2 days missing" in out.getvalue(), out.getvalue()

print("all 16 checks passed")
