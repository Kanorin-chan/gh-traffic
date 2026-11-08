"""One-tap window for gh_traffic: saved numbers at once, fresh ones seconds later.

Build: see README.md. Every open also records, same as a CLI run.
"""
import ctypes
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import font as tkfont
from tkinter import ttk
from types import SimpleNamespace

import gh_traffic as t

BG, ROW, ALT, LINE = "#14161b", "#1b1e25", "#20242c", "#3a3f4b"
TEXT, MUTED = "#e6e8ec", "#8f96a3"
METRICS = ("Views", "Unique\nvisitors", "Clones", "Unique\ncloners")
# Grid columns: repo | line | 14-day x4 | line | all-time x4 | line | stars.
GROUP_COLS = (2, 7)
LINE_COLS = (1, 6, 11)
STARS_COL = 12
VALUE_COLS = (0, 2, 3, 4, 5, 7, 8, 9, 10, STARS_COL)
UNIQUE_VALUES = (2, 4, 6, 8)  # positions in a row tuple that count people
MAX_SCREEN_SHARE = 0.85
SUBTITLE = ("Unique = different people. All-time unique is an upper limit: people who come "
            "back can be counted again. Stars leave out your own.")


def table_rows(db, ids):
    """Rows for the table, most recent visitors first, with a TOTAL row last."""
    stats = sorted(
        t.repo_stats(db, ids) if ids else [],
        key=lambda s: (-s["views14_uniq"], -s["visitors_all"], -s["clones14_uniq"],
                       s["name"].lower()),
    )
    rows = [
        (
            s["name"].split("/")[1] + ("  (private)" if s["private"] else ""),
            s["views14"], s["views14_uniq"], s["clones14"], s["clones14_uniq"],
            s["views_all"], s["visitors_all"], s["clones_all"], s["cloners_all"],
            s["stars_others"],
        )
        for s in stats
    ]
    if rows:
        total = ["TOTAL"] + [sum(row[i] for row in rows) for i in range(1, len(rows[0]))]
        # One person can hit several repos, so summed uniques are only a ceiling.
        for i in UNIQUE_VALUES:
            total[i] = f"up to {total[i]:,}"
        rows.append(tuple(total))
    return rows


def refresh(result):
    """Worker thread: fetch everything into traffic.db, report through result."""
    try:
        db = t.open_db()
        repos = t.gh(t.OWNED_REPOS, paginate=True)
        result["failed"] = t.collect(db, repos, t.utc_today())
        result["ids"] = [r["id"] for r in repos]
        db.close()
    except FileNotFoundError:
        result["error"] = "GitHub CLI not found. Install it, then run: gh auth login"
    except (RuntimeError, ValueError) as err:
        result["error"] = f"Could not reach GitHub: {err}"
    result["done"] = True


def build_window():
    root = tk.Tk()
    root.title("GitHub Traffic")
    root.configure(bg=BG)
    body = tkfont.Font(root, family="Segoe UI", size=10)
    bold = tkfont.Font(root, family="Segoe UI Semibold", size=10)

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Vertical.TScrollbar", background=ALT, troughcolor=BG,
                    bordercolor=BG, arrowcolor=MUTED)

    heading = tk.Label(root, text="Your repos", bg=BG, fg=TEXT, anchor="w",
                       font=("Segoe UI Semibold", 15))
    heading.pack(fill="x", padx=16, pady=(14, 0))
    subtitle = tk.Label(root, text=SUBTITLE, bg=BG, fg=MUTED, anchor="w", justify="left",
                        font=body)
    subtitle.pack(fill="x", padx=16, pady=(2, 10))
    status = tk.Label(root, bg=BG, fg=MUTED, anchor="w", justify="left", font=body)
    status.pack(side="bottom", fill="x", padx=16, pady=10)

    holder = tk.Frame(root, bg=BG)
    holder.pack(fill="both", expand=True, padx=16)
    canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, borderwidth=0)
    scroll = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    grid = tk.Frame(canvas, bg=BG)
    canvas.create_window((0, 0), window=grid, anchor="nw")
    root.bind_all("<MouseWheel>",
                  lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
    return SimpleNamespace(root=root, heading=heading, subtitle=subtitle, status=status,
                           canvas=canvas, scroll=scroll, grid=grid, body=body, bold=bold)


def owner_name(db, ids):
    """GitHub account name from the saved repo names, or None before the first save."""
    if not ids:
        return None
    row = db.execute(
        f"SELECT name FROM repos WHERE id IN ({t.placeholders(ids)}) LIMIT 1", ids
    ).fetchone()
    return row["name"].split("/")[0] if row else None


def render(ui, rows, since):
    """Draw the table: grouped headers, ruled lines, striped rows, TOTAL last."""
    for child in ui.grid.winfo_children():
        child.destroy()
    pad_x, pad_y = ui.bold.measure("00"), ui.body.metrics("linespace") // 4

    def cell(row, col, text, font, fg, bg, anchor, span=1):
        tk.Label(ui.grid, text=text, font=font, fg=fg, bg=bg, anchor=anchor,
                 justify="left" if anchor.endswith("w") else "right",
                 padx=pad_x, pady=pad_y).grid(row=row, column=col, columnspan=span,
                                              sticky="nsew")

    def rule(row):
        tk.Frame(ui.grid, bg=LINE, height=1).grid(row=row, column=0,
                                                 columnspan=STARS_COL + 1, sticky="ew")

    titles = ("Last 14 days", f"All time, since {since}" if since else "All time")
    for col, title in zip(GROUP_COLS, titles):
        cell(0, col, title, ui.bold, TEXT, BG, "center", span=len(METRICS))
    cell(1, 0, "Repo", ui.bold, MUTED, BG, "sw")
    for start in GROUP_COLS:
        for offset, name in enumerate(METRICS):
            cell(1, start + offset, name, ui.bold, MUTED, BG, "se")
    cell(1, STARS_COL, "Stars", ui.bold, MUTED, BG, "se")
    rule(2)

    r = 3
    for i, values in enumerate(rows):
        is_total = values[0] == "TOTAL"
        if is_total:
            rule(r)
            r += 1
        bg = BG if is_total else (ALT if i % 2 else ROW)
        for value, col in zip(values, VALUE_COLS):
            text = f"{value:,}" if isinstance(value, int) else value
            cell(r, col, text, ui.bold if is_total else ui.body, TEXT, bg,
                 "w" if col == 0 else "e")
        r += 1

    for col in LINE_COLS:
        tk.Frame(ui.grid, bg=LINE, width=1).grid(row=0, column=col, rowspan=r, sticky="ns")
    for col in VALUE_COLS[1:-1]:
        ui.grid.columnconfigure(col, uniform="metric")


def fit(ui):
    """Size the window to the table, scrolling only past most of the screen."""
    ui.root.update_idletasks()
    width, height = ui.grid.winfo_reqwidth(), ui.grid.winfo_reqheight()
    chrome = ui.root.winfo_reqheight() - ui.canvas.winfo_reqheight()
    room = int(ui.root.winfo_screenheight() * MAX_SCREEN_SHARE) - chrome
    ui.canvas.configure(width=width, height=min(height, room),
                        scrollregion=(0, 0, width, height))
    if height > room:
        ui.scroll.pack(side="right", fill="y", before=ui.canvas)
    else:
        ui.scroll.pack_forget()
    ui.subtitle.configure(wraplength=width)
    ui.status.configure(wraplength=width)
    ui.root.geometry("")


def show(ui, db, ids):
    """Redraw from traffic.db. Returns (first tracked day, days missing)."""
    since, missing = t.record_health(db, ids) if ids else (None, 0)
    owner = owner_name(db, ids)
    ui.heading.configure(text=f"{owner}'s repos" if owner else "Your repos")
    ui.root.title(f"GitHub Traffic - {owner}" if owner else "GitHub Traffic")
    render(ui, table_rows(db, ids), since)
    fit(ui)
    return since, missing


def record_line(since, missing):
    if not since:
        return "Nothing saved yet."
    if missing:
        return (f"Record since {since} is missing {missing} days. "
                f"Open this at least every {t.RETENTION_DAYS} days to keep it whole.")
    return f"Record complete since {since}."


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled screens
    except (AttributeError, OSError):
        pass
    ui = build_window()

    db = t.open_db()
    show(ui, db, [r["id"] for r in db.execute("SELECT id FROM repos")])
    db.close()
    ui.status.configure(text="Checking GitHub...")

    result = {}
    # Not a daemon: closing the window early must still finish and save the fetch.
    threading.Thread(target=refresh, args=(result,)).start()

    def poll():
        if not result.get("done"):
            ui.root.after(150, poll)
            return
        if "error" in result:
            ui.status.configure(text=f"{result['error']}\nShowing the last saved numbers.")
            return
        db = t.open_db()
        since, missing = show(ui, db, result["ids"])
        db.close()
        lines = [f"Updated {datetime.now():%H:%M}, {len(result['ids'])} repos. "
                 + record_line(since, missing)]
        if result["failed"]:
            lines.append("Failed, showing older numbers: " + ", ".join(
                name.split("/")[1] for name in result["failed"]))
        ui.status.configure(text="\n".join(lines))

    poll()
    ui.root.mainloop()


def record_only():
    """Scheduled run: fetch and save, no window. Exit 1 if anything failed."""
    result = {}
    refresh(result)
    return 1 if "error" in result or result.get("failed") else 0


if __name__ == "__main__":
    if "--record" in sys.argv[1:]:
        sys.exit(record_only())
    main()
