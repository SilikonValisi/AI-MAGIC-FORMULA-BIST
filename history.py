"""
Look back through archive/, the dated snapshots produced by run_daily.py
(and, before that, manually — archive/magic_formula_all_<YYYYMMDD>.csv).

Usage:
    python history.py AKBNK              # one ticker's rank/score over time
    python history.py --changes          # show the most recent day's diff
    python history.py --changes 5        # show diffs from the last 5 archived days
    python history.py --list             # list all archived dates
"""

import argparse
import re
from pathlib import Path

import pandas as pd

REPO_ROOT   = Path(__file__).resolve().parent
ARCHIVE_DIR = REPO_ROOT / "archive"

RANKED_RE  = re.compile(r"magic_formula_all_(\d{8})\.csv$")
CHANGES_RE = re.compile(r"changes_(\d{8})\.txt$")

DISPLAY_COLS = ["Rank", "Name", "EarningsYield", "RoC", "Magic_Score",
                 "FinansmanGideri_EBIT_%", "DebtShareOfCapital_%"]


def archived_ranked_files() -> list[tuple[str, Path]]:
    """(date_str, path) for every archived magic_formula_all_<date>.csv, sorted by date."""
    found = []
    for f in ARCHIVE_DIR.glob("magic_formula_all_*.csv"):
        m = RANKED_RE.match(f.name)
        if m:
            found.append((m.group(1), f))
    return sorted(found, key=lambda t: t[0])


def ticker_history(ticker: str) -> None:
    ticker = ticker.upper()
    rows = []
    for date_str, path in archived_ranked_files():
        df = pd.read_csv(path)
        match = df[df["Ticker"] == ticker]
        if match.empty:
            continue
        row = match.iloc[0]
        rows.append({"Date": date_str, **{c: row.get(c) for c in DISPLAY_COLS if c in row}})

    if not rows:
        print(f"No archived history found for {ticker}. Has it ranked in any daily run yet?")
        return

    out = pd.DataFrame(rows).set_index("Date")
    print(f"History for {ticker}:\n")
    print(out.to_string())


def show_changes(n: int) -> None:
    found = []
    for f in ARCHIVE_DIR.glob("changes_*.txt"):
        m = CHANGES_RE.match(f.name)
        if m:
            found.append((m.group(1), f))
    found.sort(key=lambda t: t[0])

    if not found:
        print("No archived diffs yet (changes_<date>.txt appears starting from the second daily run).")
        return

    for date_str, path in found[-n:]:
        print(f"\n{'=' * 60}\n{date_str}\n{'=' * 60}")
        print(path.read_text())


def list_days() -> None:
    files = archived_ranked_files()
    if not files:
        print("No archived runs yet — run run_daily.py first.")
        return
    for date_str, _ in files:
        print(date_str)


def main():
    parser = argparse.ArgumentParser(description="Browse the BIST Magic Formula archive")
    parser.add_argument("ticker", nargs="?", help="Ticker symbol to show rank/score history for")
    parser.add_argument("--changes", nargs="?", const=1, type=int, metavar="N",
                         help="Show the diff summary from the last N archived days (default 1)")
    parser.add_argument("--list", action="store_true", help="List all archived dates")
    args = parser.parse_args()

    if args.list:
        list_days()
    elif args.changes is not None:
        show_changes(args.changes)
    elif args.ticker:
        ticker_history(args.ticker)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
