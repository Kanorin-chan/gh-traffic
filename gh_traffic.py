#!/usr/bin/env python3
"""GitHub traffic checker + tracker for every repo you own.

GitHub only keeps traffic for 14 days. Each run saves it to traffic.db next to
this script, so the history outlives that window. Run at least every 14 days.

  python gh_traffic.py                  fetch, store, print the summary
  python gh_traffic.py --repo gallery   one repo: daily, referrers, paths, assets
  python gh_traffic.py --days 60        how far back the --repo daily table goes
  python gh_traffic.py --public         public repos only
  python gh_traffic.py --offline        print from traffic.db, no network

Auth comes from the gh CLI ("gh auth login"). No token is stored here.
Exit code: 0 ok, 1 some repos failed, 2 could not start.
"""
import argparse
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

# A compiled exe unpacks to a temp folder, so anchor on the exe itself.
BASE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).parent
DB_PATH = BASE_DIR / "traffic.db"
OWNED_REPOS = "user/repos?affiliation=owner&per_page=100"
RETENTION_DAYS = 14
WORKERS = 6
TOP_N = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY,          -- GitHub's id, so a rename keeps its history
    name TEXT NOT NULL,
    private INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS daily (   -- GitHub's own per-day buckets, UTC
    repo_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,              -- views | clones
    count INTEGER NOT NULL,
    uniques INTEGER NOT NULL,
    PRIMARY KEY (repo_id, day, kind)
);
CREATE TABLE IF NOT EXISTS snapshots (  -- counters that only exist as "right now"
    repo_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    stars INTEGER NOT NULL,
    forks INTEGER NOT NULL,
    issues INTEGER NOT NULL,         -- GitHub counts open PRs in this too
    downloads INTEGER NOT NULL,
    views14 INTEGER NOT NULL,
    views14_uniq INTEGER NOT NULL,
    clones14 INTEGER NOT NULL,
    clones14_uniq INTEGER NOT NULL,
    stars_others INTEGER NOT NULL,   -- stars minus the owner's own
    PRIMARY KEY (repo_id, day)
);
CREATE TABLE IF NOT EXISTS ranked (  -- 14-day top lists and per-asset downloads
    repo_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,              -- referrer | path | asset
    key TEXT NOT NULL,
    count INTEGER NOT NULL,
    uniques INTEGER NOT NULL,
    PRIMARY KEY (repo_id, day, kind, key)
);
CREATE TABLE IF NOT EXISTS windows (  -- GitHub's exact uniques for one finished 14-day span
    repo_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- views | clones
    first_day TEXT NOT NULL,
    last_day TEXT NOT NULL,
    uniques INTEGER NOT NULL,
    PRIMARY KEY (repo_id, kind, first_day, last_day)
);
"""


def open_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    columns = [c["name"] for c in db.execute("PRAGMA table_info(snapshots)")]
    if "stars_others" not in columns:
        db.execute("ALTER TABLE snapshots ADD COLUMN stars_others INTEGER")
        db.execute("UPDATE snapshots SET stars_others = stars")
        db.commit()
    return db


def utc_today():
    return datetime.now(timezone.utc).date().isoformat()


def gh(path, paginate=False):
    """Call the GitHub API through the gh CLI and return parsed JSON."""
    cmd = ["gh", "api", path] + (["--paginate", "--slurp"] if paginate else [])
    # No stdin and no console window: the exe has neither to hand down.
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"gh exited {result.returncode}")
    data = json.loads(result.stdout)
    return [item for page in data for item in page] if paginate else data


def fetch_repo(repo):
    base = f"repos/{repo['full_name']}"
    return {
        "views": gh(f"{base}/traffic/views"),
        "clones": gh(f"{base}/traffic/clones"),
        "referrers": gh(f"{base}/traffic/popular/referrers"),
        "paths": gh(f"{base}/traffic/popular/paths"),
        "releases": gh(f"{base}/releases?per_page=100", paginate=True),
        "stargazers": (gh(f"{base}/stargazers?per_page=100", paginate=True)
                       if repo["stargazers_count"] else []),
    }


def store(db, day, repo, data):
    """Write one repo's fetch. Safe to run any number of times a day."""
    rid = repo["id"]
    db.execute(
        "INSERT INTO repos VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "name=excluded.name, private=excluded.private",
        (rid, repo["full_name"], int(repo["private"])),
    )
    # Keep the larger value: a partial edge-of-window day can't shrink a saved one.
    for kind in ("views", "clones"):
        for bucket in data[kind][kind]:
            db.execute(
                "INSERT INTO daily VALUES (?,?,?,?,?) "
                "ON CONFLICT(repo_id, day, kind) DO UPDATE SET "
                "count=MAX(count, excluded.count), "
                "uniques=MAX(uniques, excluded.uniques)",
                (rid, bucket["timestamp"][:10], kind, bucket["count"], bucket["uniques"]),
            )
        # Only finished spans: a span ending today would miss today's later visitors.
        days = [bucket["timestamp"][:10] for bucket in data[kind][kind]]
        if days and max(days) < day:
            db.execute(
                "INSERT INTO windows VALUES (?,?,?,?,?) "
                "ON CONFLICT(repo_id, kind, first_day, last_day) DO UPDATE SET "
                "uniques=MAX(uniques, excluded.uniques)",
                (rid, kind, min(days), max(days), data[kind]["uniques"]),
            )

    assets = [
        (f"{release['tag_name']}/{asset['name']}", asset["download_count"], 0)
        for release in data["releases"]
        for asset in release["assets"]
    ]
    ranked = {
        "referrer": [(r["referrer"], r["count"], r["uniques"]) for r in data["referrers"]],
        "path": [(p["path"], p["count"], p["uniques"]) for p in data["paths"]],
        "asset": assets,
    }
    # Replace, not merge: something that fell off a top list today must go.
    db.execute("DELETE FROM ranked WHERE repo_id=? AND day=?", (rid, day))
    for kind, rows in ranked.items():
        db.executemany(
            "INSERT INTO ranked VALUES (?,?,?,?,?,?)",
            [(rid, day, kind, key, count, uniques) for key, count, uniques in rows],
        )

    owner = repo["owner"]["login"]
    db.execute(
        "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            rid, day,
            repo["stargazers_count"], repo["forks_count"], repo["open_issues_count"],
            sum(count for _, count, _ in assets),
            data["views"]["count"], data["views"]["uniques"],
            data["clones"]["count"], data["clones"]["uniques"],
            sum(1 for star in data["stargazers"] if star["login"] != owner),
        ),
    )


def collect(db, repos, day):
    """Fetch in parallel, write on this thread. Returns names that failed."""
    failed = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [(repo, pool.submit(fetch_repo, repo)) for repo in repos]
        for repo, future in futures:
            try:
                data = future.result()
            except (RuntimeError, ValueError) as err:
                print(f"warning: {repo['full_name']}: {err}", file=sys.stderr)
                failed.append(repo["full_name"])
                continue
            store(db, day, repo, data)
    db.commit()
    return failed


def gap_warning(db, today):
    """Say so if runs were too far apart for GitHub's window to cover. Else None."""
    last = db.execute("SELECT MAX(day) FROM snapshots").fetchone()[0]
    if not last:
        return None
    gap = (date.fromisoformat(today) - date.fromisoformat(last)).days
    if gap <= RETENTION_DAYS:
        return None
    return (f"Last run was {gap} days ago and GitHub keeps {RETENTION_DAYS}, "
            f"so about {gap - RETENTION_DAYS} days of traffic are gone.")


def latest_snapshots(db, ids):
    """Each repo's newest snapshot, busiest first."""
    return db.execute(
        f"""SELECT r.id, r.name, r.private, s.*
            FROM repos r JOIN snapshots s ON s.repo_id = r.id
            WHERE r.id IN ({placeholders(ids)})
              AND s.day = (SELECT MAX(day) FROM snapshots WHERE repo_id = r.id)
            ORDER BY s.views14 DESC, s.clones14 DESC, s.stars DESC, r.name""",
        ids,
    ).fetchall()


def unique_people(db, repo_id, kind):
    """All-time distinct visitors (views) or cloners (clones), as a ceiling.

    Non-overlapping finished spans count each person once; other days count per day.
    """
    daily = dict(db.execute(
        "SELECT day, uniques FROM daily WHERE repo_id=? AND kind=?", (repo_id, kind)
    ).fetchall())
    total, last_end, covered = 0, "", set()
    for first, last, uniques in db.execute(
        "SELECT first_day, last_day, uniques FROM windows WHERE repo_id=? AND kind=? "
        "ORDER BY first_day, last_day DESC",
        (repo_id, kind),
    ):
        if first > last_end:
            total += uniques
            last_end = last
            covered.update(d for d in daily if first <= d <= last)
    return total + sum(u for d, u in daily.items() if d not in covered)


def repo_stats(db, ids):
    """Each repo's newest snapshot plus all-time totals since tracking began."""
    stats = []
    for snap in latest_snapshots(db, ids):
        row = dict(snap)
        sums = dict(db.execute(
            "SELECT kind, SUM(count) FROM daily WHERE repo_id=? GROUP BY kind", (row["id"],)
        ).fetchall())
        row.update(
            views_all=sums.get("views", 0),
            clones_all=sums.get("clones", 0),
            visitors_all=unique_people(db, row["id"], "views"),
            cloners_all=unique_people(db, row["id"], "clones"),
        )
        stats.append(row)
    return stats


def record_health(db, ids):
    """First tracked day, and the most days any repo is missing since then."""
    spans = db.execute(
        f"""SELECT MIN(day), MAX(day), COUNT(DISTINCT day) FROM daily
            WHERE kind='views' AND repo_id IN ({placeholders(ids)}) GROUP BY repo_id""",
        ids,
    ).fetchall()
    if not spans:
        return None, 0
    missing = max(
        (date.fromisoformat(last) - date.fromisoformat(first)).days + 1 - count
        for first, last, count in spans
    )
    return min(first for first, _, _ in spans), missing


def with_delta(now, before):
    if before is None or now == before:
        return str(now)
    return f"{now} ({now - before:+d})"


def table(headers, rows, left_cols=1):
    rows = [[str(cell) for cell in row] for row in rows]
    widths = [max(len(cell) for cell in col) for col in zip(headers, *rows)]

    def line(cells):
        return "  ".join(
            cell.ljust(width) if i < left_cols else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(cells, widths))
        ).rstrip()

    print(line(headers))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(line(row))


def placeholders(ids):
    return ",".join("?" * len(ids))


def summary(db, ids):
    """Print the all-repos table. Returns False when there is nothing stored."""
    stats = repo_stats(db, ids)
    if not stats:
        return False

    newest = max(s["day"] for s in stats)
    since, missing = record_health(db, ids)
    print(f"{stats[0]['name'].split('/')[0]}: last 14 days as of {newest} UTC. "
          f"All time since {since}, {missing} days missing. "
          f"All-time visitors/cloners are ceilings.\n")

    keys = ("views14", "views14_uniq", "clones14", "clones14_uniq",
            "views_all", "visitors_all", "clones_all", "cloners_all")
    rows, totals = [], dict.fromkeys(keys + ("stars_others", "forks", "downloads"), 0)
    for s in stats:
        before = db.execute(
            "SELECT stars_others, forks, downloads FROM snapshots "
            "WHERE repo_id=? AND day<? ORDER BY day DESC LIMIT 1",
            (s["id"], s["day"]),
        ).fetchone()
        name = s["name"].split("/")[1] + (" [private]" if s["private"] else "")
        if s["day"] != newest:
            name += f" [stale: {s['day']}]"
        rows.append([name] + [s[k] for k in keys] + [
            with_delta(s["stars_others"], before and before["stars_others"]),
            with_delta(s["forks"], before and before["forks"]),
            s["issues"],
            with_delta(s["downloads"], before and before["downloads"]),
        ])
        for key in totals:
            totals[key] += s[key]

    # Uniques stay blank: one visitor to two repos is one person, not two.
    rows.append(["TOTAL"] + [
        "" if "uniq" in k or k in ("visitors_all", "cloners_all") else totals[k] for k in keys
    ] + [totals["stars_others"], totals["forks"], "", totals["downloads"]])
    table(["repo", "views", "visitors", "clones", "cloners",
           "all views", "all visitors", "all clones", "all cloners",
           "stars", "forks", "issues/PRs", "downloads"], rows)

    referrers = db.execute(
        f"""SELECT k.key, SUM(k.count) AS views, COUNT(*) AS repos
            FROM ranked k
            WHERE k.kind='referrer' AND k.repo_id IN ({placeholders(ids)})
              AND k.day = (SELECT MAX(day) FROM snapshots WHERE repo_id = k.repo_id)
            GROUP BY k.key ORDER BY views DESC, k.key LIMIT {TOP_N}""",
        ids,
    ).fetchall()
    if referrers:
        print("\nTop referrers")
        table(["from", "views", "repos"], [tuple(r) for r in referrers])
    return True


def detail(db, repo_id, today, days):
    daily = db.execute(
        """SELECT day,
                  SUM(CASE WHEN kind='views'  THEN count   ELSE 0 END) AS v,
                  SUM(CASE WHEN kind='views'  THEN uniques ELSE 0 END) AS vu,
                  SUM(CASE WHEN kind='clones' THEN count   ELSE 0 END) AS c,
                  SUM(CASE WHEN kind='clones' THEN uniques ELSE 0 END) AS cu
           FROM daily WHERE repo_id=? AND day >= date(?, ?)
           GROUP BY day HAVING v + c > 0 ORDER BY day""",
        (repo_id, today, f"-{days} days"),
    ).fetchall()
    print(f"\nDaily, last {days} days (quiet days hidden)")
    if daily:
        table(["day", "views", "uniq", "clones", "uniq"], [tuple(d) for d in daily])
    else:
        print("nothing")

    day = db.execute(
        "SELECT MAX(day) FROM snapshots WHERE repo_id=?", (repo_id,)
    ).fetchone()[0]
    for kind, title, headers in (
        ("referrer", "Referrers, last 14 days", ["from", "views", "uniq"]),
        ("path", "Popular pages, last 14 days", ["page", "views", "uniq"]),
        ("asset", "Release downloads, all time", ["asset", "downloads"]),
    ):
        rows = db.execute(
            "SELECT key, count, uniques FROM ranked WHERE repo_id=? AND day=? AND kind=? "
            "ORDER BY count DESC, key",
            (repo_id, day, kind),
        ).fetchall()
        if rows:
            print(f"\n{title}")
            table(headers, [tuple(r)[:len(headers)] for r in rows])


def select(repos, args):
    if args.public:
        repos = [r for r in repos if not r["private"]]
    if not args.repo:
        return repos
    want = args.repo.lower()
    hits = [r for r in repos
            if want in (r["full_name"].lower(), r["full_name"].split("/")[1].lower())]
    if not hits:
        names = ", ".join(sorted(r["full_name"].split("/")[1] for r in repos))
        print(f"no repo called {args.repo!r}. Have: {names}", file=sys.stderr)
        sys.exit(2)
    return hits


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="GitHub traffic checker + tracker.")
    parser.add_argument("--repo", help="one repo, by short or full name, with detail")
    parser.add_argument("--days", type=int, default=RETENTION_DAYS,
                        help="how far back the --repo daily table goes")
    parser.add_argument("--public", action="store_true", help="public repos only")
    parser.add_argument("--offline", action="store_true", help="no network, read traffic.db")
    args = parser.parse_args()

    db = open_db()
    today = utc_today()

    failed = []
    if args.offline:
        repos = [{"id": r["id"], "full_name": r["name"], "private": bool(r["private"])}
                 for r in db.execute("SELECT * FROM repos")]
        repos = select(repos, args)
    else:
        try:
            repos = gh(OWNED_REPOS, paginate=True)
        except FileNotFoundError:
            print("gh CLI not found. Install it, then run: gh auth login", file=sys.stderr)
            return 2
        except (RuntimeError, ValueError) as err:
            print(f"could not list repos: {err}", file=sys.stderr)
            return 2
        repos = select(repos, args)
        gap = gap_warning(db, today)
        if gap:
            print(f"warning: {gap}", file=sys.stderr)
        failed = collect(db, repos, today)

    ids = [r["id"] for r in repos]
    if not ids or not summary(db, ids):
        print("Nothing stored yet. Run it without --offline first.")
        return 1 if failed else 0
    if args.repo:
        detail(db, ids[0], today, args.days)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
