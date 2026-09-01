"""
Daily orchestrator for the BIST Magic Formula pipeline.
--------------------------------------------------------
Runs the full fetch → filter/rank pipeline once, archives the raw + ranked
CSVs it produces into archive/ (flat, dated filenames — matching the manual
archive/magic_formula_all_<date>.csv convention already in use), then diffs
today's ranking against the most recent prior archived day and reports what
changed (new/dropped top-20 names, big rank movers) — as a macOS banner when
run locally, or a GitHub issue comment when run on a GitHub Actions runner
(detected via the GITHUB_ACTIONS env var), since there's no desktop to show a
banner on there.

Runs either via GitHub Actions (see .github/workflows/daily.yml — the
intended setup, since isyatirim.com.tr may be geo-blocked from wherever the
local machine happens to be) or triggered locally by launchd (see
setup_launchd.sh). Safe to run by hand at any time either way — it will
simply add one more dated snapshot.
"""

import json
import os
import re
import subprocess
import sys
import shutil
import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO_ROOT   = Path(__file__).resolve().parent
ARCHIVE_DIR = REPO_ROOT / "archive"
LOG_DIR     = REPO_ROOT / "logs"

TOP_N          = 20   # "top of the list" size for new-entrant/dropped tracking
BIG_MOVE_RANKS = 10    # minimum |rank change| to call out as a "big mover"

NOTIFY_ISSUE_TITLE = "Daily Magic Formula Updates"

RANKED_RE = re.compile(r"magic_formula_all_(\d{8})\.csv$")


def run_step(name: str, args: list[str], log_file) -> bool:
    """Run one pipeline step as a subprocess, streaming output line-by-line to
    both the console (when attached to one) and the log file as it's produced
    — a full fetch takes minutes, and buffering everything until the process
    exits (subprocess.run's default) makes it look hung. Returns True on
    success."""
    print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
    log_file.write(f"\n{'=' * 60}\n{name}\n{'=' * 60}\n")
    log_file.flush()

    proc = subprocess.Popen(
        # -u: unbuffered stdout in the child. Without it, Python fully
        # block-buffers stdout when it isn't a terminal (i.e. always, once
        # piped here), so output would still arrive in stalled chunks instead
        # of per-ticker as bist_magic_formula_midas.py actually prints it.
        [sys.executable, "-u", *args],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        print(line, end="")
        log_file.write(line)
        log_file.flush()
    proc.wait()

    if proc.returncode != 0:
        print(f"[ERROR] {name} exited with code {proc.returncode}")
        log_file.write(f"[ERROR] {name} exited with code {proc.returncode}\n")
        return False
    return True


def notify(title: str, message: str, body: str | None = None) -> None:
    """Notify about today's run: a macOS banner when run locally, or a
    comment on a persistent GitHub issue when run unattended on a GitHub
    Actions runner (no desktop to show a banner on, but `gh` is preinstalled
    there and repo watchers get notified on new issue comments). `body`, if
    given, is the fuller write-up (e.g. the full diff) used for the issue
    comment; `message` alone is used for the terser macOS banner."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        _notify_github_issue(title, body or message)
    else:
        _notify_macos(title, message)


def _notify_macos(title: str, message: str) -> None:
    """Fire a macOS banner notification. Best-effort — never raises."""
    try:
        # AppleScript string literals only need their own quotes/backslashes
        # escaped; anything else in message/title is passed through as-is.
        safe_msg   = message.replace("\\", "\\\\").replace('"', '\\"')
        safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe_msg}" with title "{safe_title}"'],
            check=False,
        )
    except FileNotFoundError:
        pass  # not on macOS / osascript unavailable — notification is best-effort


def _get_or_create_notify_issue() -> str | None:
    """Find the persistent "Daily Magic Formula Updates" issue (creating it
    on the first run) that each day's update gets posted to as a comment,
    rather than opening a new issue every day."""
    listing = subprocess.run(
        ["gh", "issue", "list", "--state", "all",
         "--search", f'"{NOTIFY_ISSUE_TITLE}" in:title',
         "--json", "number,title", "--limit", "10"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if listing.returncode == 0:
        try:
            for issue in json.loads(listing.stdout):
                if issue["title"] == NOTIFY_ISSUE_TITLE:
                    return str(issue["number"])
        except (ValueError, KeyError):
            pass

    created = subprocess.run(
        ["gh", "issue", "create", "--title", NOTIFY_ISSUE_TITLE,
         "--body", "Running log of daily BIST Magic Formula runs — one comment posted per day."],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if created.returncode != 0:
        print(f"[WARN] Couldn't create notification issue:\n{created.stdout}{created.stderr}")
        return None
    match = re.search(r"/issues/(\d+)", created.stdout)
    return match.group(1) if match else None


def _notify_github_issue(title: str, body: str) -> None:
    issue_number = _get_or_create_notify_issue()
    if issue_number is None:
        return
    comment = subprocess.run(
        ["gh", "issue", "comment", issue_number, "--body", f"**{title}**\n\n{body}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if comment.returncode != 0:
        print(f"[WARN] Couldn't post notification comment:\n{comment.stdout}{comment.stderr}")


def archived_ranked_files() -> list[tuple[str, Path]]:
    """(date_str, path) for every archived magic_formula_all_<date>.csv, sorted by date."""
    found = []
    for f in ARCHIVE_DIR.glob("magic_formula_all_*.csv"):
        m = RANKED_RE.match(f.name)
        if m:
            found.append((m.group(1), f))
    return sorted(found, key=lambda t: t[0])


def most_recent_prior(date_str: str) -> Path | None:
    prior = [f for d, f in archived_ranked_files() if d < date_str]
    return prior[-1] if prior else None


def write_manifest() -> Path:
    """archive/manifest.json — the sorted list of archived dates that
    magic_formula_heatmap.html fetches to populate its history picker and to
    find the latest day to auto-load. Newest first."""
    dates = sorted((d for d, _ in archived_ranked_files()), reverse=True)
    manifest_path = ARCHIVE_DIR / "manifest.json"
    manifest_path.write_text(json.dumps({"dates": dates}, indent=2) + "\n")
    return manifest_path


def publish_to_github(date_str: str, summary_line: str, log_file) -> None:
    """Commit today's archived CSV + manifest and push, so GitHub Pages
    (serving magic_formula_heatmap.html + archive/) picks it up. Best-effort:
    logs and returns on failure rather than raising, since a push failure
    (offline, auth expired, merge conflict) shouldn't be treated the same as
    a fetch/ranking failure — today's local archive is already saved either way.
    """
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)

    # A fresh GitHub Actions checkout has no commit identity configured at
    # all (unlike a local clone, where it's inherited from the user's own git
    # config) — set one, scoped to this repo only, only if none exists yet.
    if not git("config", "user.email").stdout.strip():
        git("config", "user.name", "github-actions[bot]")
        git("config", "user.email", "github-actions[bot]@users.noreply.github.com")

    add = git("add", f"archive/magic_formula_all_{date_str}.csv", "archive/manifest.json")
    log_file.write(f"git add: {add.returncode}\n{add.stdout}{add.stderr}\n")

    status = git("status", "--porcelain", "--", "archive/")
    if not status.stdout.strip():
        print("[INFO] Nothing new to publish (archive already matches git).")
        return

    commit = git("commit", "-m", f"Daily update: {date_str} — {summary_line}")
    log_file.write(f"git commit: {commit.returncode}\n{commit.stdout}{commit.stderr}\n")
    if commit.returncode != 0:
        print(f"[WARN] git commit failed:\n{commit.stdout}{commit.stderr}")
        return

    # Explicit "HEAD:main" rather than bare "HEAD": actions/checkout leaves a
    # GitHub Actions runner in detached-HEAD state (checked out to a specific
    # SHA, not a tracked branch), where "git push origin HEAD" fails outright
    # ("You are not currently on a branch") since there's no current branch
    # name to infer a destination from. Naming the branch explicitly works in
    # both that case and a normal local clone.
    push = git("push", "origin", "HEAD:main")
    log_file.write(f"git push: {push.returncode}\n{push.stdout}{push.stderr}\n")
    if push.returncode != 0:
        print(f"[WARN] git push failed — today's data is committed locally but not published:\n{push.stdout}{push.stderr}")
    else:
        print("[INFO] Published today's ranking to GitHub.")


def build_diff(today_df: pd.DataFrame, prior_df: pd.DataFrame, prior_label: str) -> tuple[str, int, int, int]:
    today_top = set(today_df[today_df["Rank"] <= TOP_N].index)
    prior_top = set(prior_df[prior_df["Rank"] <= TOP_N].index)

    new_entrants = today_top - prior_top
    dropped      = prior_top - today_top

    common = today_df.index.intersection(prior_df.index)
    rank_delta = (prior_df.loc[common, "Rank"] - today_df.loc[common, "Rank"]).sort_values(ascending=False)
    big_movers = rank_delta[rank_delta.abs() >= BIG_MOVE_RANKS]

    lines = [f"Changes vs. {prior_label}", ""]

    lines.append(f"New in top {TOP_N} ({len(new_entrants)}):")
    for t in sorted(new_entrants, key=lambda t: today_df.loc[t, "Rank"]):
        lines.append(f"  + {t:<6} → rank {int(today_df.loc[t, 'Rank'])}")

    lines.append(f"\nDropped out of top {TOP_N} ({len(dropped)}):")
    for t in sorted(dropped, key=lambda t: prior_df.loc[t, "Rank"]):
        lines.append(f"  - {t:<6} (was rank {int(prior_df.loc[t, 'Rank'])})")

    lines.append(f"\nBig rank movers (|Δ| ≥ {BIG_MOVE_RANKS}, {len(big_movers)}):")
    for t, delta in big_movers.items():
        direction = "↑" if delta > 0 else "↓"
        lines.append(
            f"  {direction} {t:<6} rank {int(prior_df.loc[t, 'Rank'])} → "
            f"{int(today_df.loc[t, 'Rank'])} ({delta:+.0f})"
        )

    return "\n".join(lines), len(new_entrants), len(dropped), len(big_movers)


def main():
    parser = argparse.ArgumentParser(description="Run the daily BIST Magic Formula pipeline and archive results")
    parser.add_argument("--pegy", action="store_true", help="Also run pegy_enricher.py on today's ranked output")
    parser.add_argument("--workers", type=int, default=5, help="Concurrent fetch threads (passed through)")
    parser.add_argument("--no-push", action="store_true", help="Archive locally only — skip committing/pushing today's ranking to GitHub")
    args = parser.parse_args()

    ARCHIVE_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    today = datetime.now()
    date_str = today.strftime("%Y%m%d")

    log_path = LOG_DIR / f"run_{date_str}.log"
    with open(log_path, "a") as log_file:
        log_file.write(f"\n\n##### Run started {today.isoformat()} #####\n")

        ok = run_step(
            "1/2 Fetching data (bist_magic_formula_midas.py)",
            ["bist_magic_formula_midas.py", "--workers", str(args.workers)],
            log_file,
        )
        if not ok:
            notify("BIST Magic Formula — FAILED", "Fetch step failed. Check logs/run_%s.log" % date_str)
            sys.exit(1)

        raw_file = REPO_ROOT / f"bist_greenblatt_raw_{date_str}.csv"
        if not raw_file.exists():
            print(f"[ERROR] Expected raw file not found: {raw_file}")
            notify("BIST Magic Formula — FAILED", "Fetch completed but expected raw CSV was missing.")
            sys.exit(1)

        out_file = REPO_ROOT / f"magic_formula_all_{date_str}.csv"
        ok = run_step(
            "2/2 Applying filters + leverage columns (apply_magic_formula_to_all.py)",
            ["apply_magic_formula_to_all.py", "--raw", str(raw_file), "--out", str(out_file)],
            log_file,
        )
        if not ok or not out_file.exists():
            notify("BIST Magic Formula — FAILED", "Ranking step failed. Check logs/run_%s.log" % date_str)
            sys.exit(1)

        if args.pegy:
            ok = run_step("Enriching with PEGY (pegy_enricher.py)", ["pegy_enricher.py"], log_file)
            if not ok:
                print("[WARN] PEGY enrichment failed — continuing with un-enriched ranking.")

        # ── Diff against the most recent prior archived day (before moving
        #    today's files in, so "prior" can't accidentally match today) ───
        prior_file = most_recent_prior(date_str)
        summary_line = "No prior archive to compare against — first run."
        diff_text = None

        if prior_file is not None:
            today_df = pd.read_csv(out_file).set_index("Ticker")
            prior_df = pd.read_csv(prior_file).set_index("Ticker")
            diff_text, n_new, n_dropped, n_moved = build_diff(today_df, prior_df, prior_file.stem)
            summary_line = (
                f"{n_new} new in top {TOP_N}, {n_dropped} dropped, "
                f"{n_moved} big movers (vs {prior_file.stem})"
            )
            print("\n" + diff_text)
            log_file.write("\n" + diff_text + "\n")

        # ── Archive everything this run produced (flat, dated filenames) ───
        combined_file = REPO_ROOT / f"magic_formula_combined_{date_str}.csv"
        for f in [raw_file, combined_file, out_file]:
            if f.exists():
                shutil.move(str(f), str(ARCHIVE_DIR / f.name))
                log_file.write(f"Archived {f.name} → {ARCHIVE_DIR}\n")

        if diff_text is not None:
            (ARCHIVE_DIR / f"changes_{date_str}.txt").write_text(diff_text + "\n")

        write_manifest()
        print(f"\nArchived today's run → {ARCHIVE_DIR}")

        if args.no_push:
            print("[INFO] --no-push set, skipping GitHub publish.")
        else:
            publish_to_github(date_str, summary_line, log_file)

        log_file.write(f"\n##### Run finished {datetime.now().isoformat()} — {summary_line} #####\n")

    notify("BIST Magic Formula", summary_line, body=diff_text or summary_line)
    print(f"\nDone. {summary_line}")


if __name__ == "__main__":
    main()
