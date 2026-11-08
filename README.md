# gh-traffic

GitHub keeps repo traffic (views, clones, unique visitors) for 14 days, then deletes it.
gh-traffic saves it to a local SQLite file every time it runs, so you keep the whole
history and get all-time totals for every repo you own.

- **One-tap window**: each repo's views, unique visitors, clones and unique cloners, for
  the last 14 days and for all time, plus stars (your own star left out).
- **Command line**: the same numbers in a terminal, plus per-repo daily traffic,
  referrers, popular pages and release downloads.
- **Daily recording**: a scheduled run keeps the history complete without opening anything.

It works for whichever account `gh` is logged in as, and talks only to GitHub's API.

## Requirements

- Python 3.12, standard library only. Tested on Windows 11.
- [GitHub CLI](https://cli.github.com/), logged in with `gh auth login`.

## Window

```
python gh_traffic_app.py
```

It shows the saved numbers straight away, then refreshes from GitHub and saves (about
15 seconds for 16 repos). Closing it early still finishes the save.

### One-tap exe (Windows)

```
pip install pyinstaller
python -m PyInstaller --onefile --noconsole --name GitHubTraffic gh_traffic_app.py
```

Move `dist\GitHubTraffic.exe` into this folder, then pin it or put a shortcut on your
desktop. It reads and writes the `traffic.db` beside it.

## Command line

```
python gh_traffic.py                 fetch, save, print the summary
python gh_traffic.py --repo NAME     one repo: daily, referrers, pages, assets
python gh_traffic.py --days 60       how far back the --repo daily table goes
python gh_traffic.py --public        public repos only
python gh_traffic.py --offline       print from traffic.db, no network
```

Exit code: 0 ok, 1 some repos failed, 2 could not start.

## Record daily

A gap of more than 14 days between runs is lost for good, so let Windows run it once a
day. From this folder, in PowerShell:

```powershell
$exe = (Resolve-Path .\GitHubTraffic.exe).Path
Register-ScheduledTask -TaskName 'GitHub Traffic record' `
  -Action (New-ScheduledTaskAction -Execute $exe -Argument '--record' -WorkingDirectory (Split-Path $exe)) `
  -Trigger (New-ScheduledTaskTrigger -Daily -At '09:00') `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10))
```

`--record` saves without opening a window and exits 1 if anything failed. A missed run
catches up when the PC is next on. Without the exe, use `pythonw.exe` with
`gh_traffic_app.py --record` as the arguments. Remove the task with
`Unregister-ScheduledTask 'GitHub Traffic record'`.

The window's status line says whether the record has any missing days.

## What's stored (`traffic.db`, SQLite)

| Table | Holds |
|---|---|
| `repos` | GitHub id, name, private flag. Keyed on id, so a rename keeps its history. |
| `daily` | Views and clones per UTC day. Reruns keep the larger value. |
| `snapshots` | One row per repo per run day: stars, forks, issues, downloads, 14-day totals. |
| `ranked` | Referrers, popular pages, per-asset downloads, as of each run day. |
| `windows` | GitHub's exact unique count for each finished 14-day span it returned. |

Deltas like `3 (+1)` compare with the latest earlier day, so a same-day rerun keeps them.

## Reading the numbers

- Stars leave out your own (`stars_others`); the raw count stays in `stars`.
- All-time unique visitors and cloners are a ceiling. GitHub gives exact uniques per day
  and per 14-day span, never who came back. So spans that don't overlap count each person
  once, and days outside them add their own daily uniques (`unique_people`).
- Uniques can't be added across repos either: one person can visit several. The CLI's
  TOTAL leaves them blank; the window shows "up to N".
- Your own visits and clones count. GitHub doesn't say who they were, so they can't be removed.
- Clones include bots and mirrors, so clones far above views usually aren't people.
- `issues/PRs` is GitHub's `open_issues_count`, which includes open pull requests.
- Referrers and pages exist only as a rolling top list, never per day.
- Only repos you own: no org repos, and none you only collaborate on.

## Tests

```
python tests/check_gh_traffic.py
```

## License

MIT
