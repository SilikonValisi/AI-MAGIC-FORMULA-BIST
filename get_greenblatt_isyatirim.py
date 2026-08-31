"""
BIST Magic Formula Screener
----------------------------
Screens Borsa Istanbul (BIST) stocks using Joel Greenblatt's Magic Formula:
  - Earnings Yield  = TTM EBIT / Enterprise Value
  - Return on Capital = TTM EBIT / (Net Working Capital + Net Fixed Assets)

Data sources (hybrid):
  - isyatirim.com.tr  -> reporting period, market cap, net debt, average
                         volume, company name (scraped from the "sirket-karti"
                         HTML page — unchanged from the original version)
  - getmidas.com      -> TTM EBIT and balance-sheet items (current assets,
                         current liabilities, total assets, intangibles,
                         financing expense), via Midas's public JSON API
"""

import re
import sys
import time
import json
import warnings
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import pandas as pd
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

# ── Constants ──────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

ISYATIRIM_BASE_URL = "https://www.isyatirim.com.tr"
MIDAS_BASE_URL = "https://www.getmidas.com/wp-json/midas-api/v1/midas_bilnaco_date"

# Request timeout as (connect_timeout, read_timeout). A tight connect timeout
# means a slow-to-reach host fails fast instead of eating a full flat timeout;
# the read timeout still allows for a slower response body.
REQUEST_TIMEOUT = (5, 15)

# Retry behavior for transient failures (timeouts, connection resets, 5xx).
MAX_RETRIES     = 3     # total attempts = 1 initial + (MAX_RETRIES - 1) retries
RETRY_BACKOFF_S = 2     # base backoff in seconds, doubles each retry (2s, 4s, 8s...)

# Midas lineCodeId references
# -- "bilanco" section (balance sheet) --
MIDAS_CURR_ASSETS_CODE  = 3    # DÖNEN VARLIKLAR
MIDAS_CURR_LIAB_CODE    = 49   # KISA VADELİ YÜKÜMLÜLÜKLER
MIDAS_TOTAL_ASSETS_CODE = 46   # TOPLAM VARLIKLAR
MIDAS_INTANGIBLES_CODE  = 40   # Maddi Olmayan Duran Varlıklar
# -- "gelir-table" section (income statement) --
MIDAS_EBIT_CODE         = 17   # ESAS FAALİYET KARI/ZARARI (EBIT)
MIDAS_FIN_EXPENSE_CODE  = 22   # Finansman Giderleri (-)

MIN_MARKET_CAP_MN_TL = 1_000   # Minimum market cap filter (mn TL)
MIN_VOLUME_MN_USD    = 5       # Minimum avg volume filter (mn USD), None = skip filter
MAX_WORKERS          = 5      # Concurrent threads


# ── Networking Helper ──────────────────────────────────────────────────────────

def request_with_retry(url: str, max_retries: int = MAX_RETRIES) -> requests.Response | None:
    """
    GET `url` with retry + exponential backoff on transient failures
    (timeouts, connection errors, 5xx server errors).

    Does NOT retry on 4xx client errors (bad ticker, bad params, etc.) since
    retrying those just wastes time hitting the same wall again.

    Returns the Response on success, or None if all attempts are exhausted.
    """
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, verify=False)
            if 500 <= resp.status_code < 600:
                # Server-side error — worth retrying
                last_exc = f"HTTP {resp.status_code}"
            else:
                resp.raise_for_status()  # raises on 4xx, no retry for those
                return resp
        except requests.exceptions.HTTPError:
            return None  # 4xx — don't retry, it won't get better
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
        except Exception as e:
            last_exc = e

        if attempt < max_retries:
            wait = RETRY_BACKOFF_S * (2 ** (attempt - 1))
            time.sleep(wait)

    return None


# ── Ticker Loading ─────────────────────────────────────────────────────────────

def load_tickers(filepath: str) -> list[str]:
    """Load ticker symbols from a plain-text file (one per line)."""
    path = Path(filepath)
    if not path.exists():
        print(f"[ERROR] Ticker file not found: {filepath}")
        sys.exit(1)
    tickers = [line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not tickers:
        print(f"[ERROR] No tickers found in {filepath}")
        sys.exit(1)
    print(f"Loaded {len(tickers)} tickers from {filepath}")
    return tickers


# ── Midas API Helpers ───────────────────────────────────────────────────────────

def fetch_midas(ticker: str, dates: list[tuple[int, int]], bilanco: bool = True) -> dict | None:
    """
    Fetch financial data from the Midas midas_bilnaco_date API.

    `dates` is a list of up to 4 (year, month) tuples, corresponding to the
    date1..date4 query params. If fewer than 4 are given, the last one is
    repeated to pad the request (the API requires all 4 params).

    `bilanco=True` requests the balance sheet ({"bilanco": [...]});
    `bilanco=False` requests the income statement + cash flow
    ({"gelir-table": [...], "nakit": [...]}).

    IMPORTANT: Midas's response body is double-JSON-encoded — the raw text
    is a JSON string literal containing escaped JSON. `requests`' .json()
    only undoes the outer layer, so the result needs a second json.loads()
    if it comes back as a str instead of a dict.

    Returns the parsed dict, or None on failure.
    """
    padded = list(dates) + [dates[-1]] * (4 - len(dates))
    parts = [f"code={ticker}"]
    for i, (y, m) in enumerate(padded, start=1):
        parts.append(f"date{i}={y}-{m}")
        parts.append(f"consildated{i}=1")
    if bilanco:
        parts.append("bilanco=true")
    url = f"{MIDAS_BASE_URL}?{'&'.join(parts)}"

    r = request_with_retry(url)
    if r is None:
        return None
    try:
        data = r.json()
        if isinstance(data, str):
            data = json.loads(data)
        return data
    except Exception:
        return None


def get_midas_value(data: dict | None, section: str, line_code_id: int, period_index: int = 0) -> float | None:
    """
    Extract a numeric value from a Midas response by lineCodeId.

    `section` is "bilanco" or "gelir-table". `period_index` selects which
    of the (up to 4) requested date slots to read — 0 corresponds to date1.
    Returns None if the item is missing, zero, or the period is unavailable
    (e.g. a recently listed company with no data that far back).
    """
    if not data or section not in data:
        return None
    try:
        period_list = data[section][period_index]
    except (IndexError, TypeError):
        return None
    for item in period_list:
        if item.get("lineCodeId") == line_code_id:
            val = item.get("value")
            if val in (None, 0):
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None
    return None


# ── TTM EBIT Calculation (Midas) ────────────────────────────────────────────────

def fetch_ttm_ebit_midas(ticker: str, current_year: int, current_month: int) -> tuple[float | None, bool, dict | None]:
    """
    Calculate Trailing Twelve Months (TTM) EBIT from Midas's income statement.

    Midas (like isyatirim) stores YTD cumulative values, so TTM is:
        TTM = YTD(cur_year, cur_month)
            + YTD(prior_year, 12)
            - YTD(prior_year, cur_month)

    For December reporters (full-year), just return the annual value directly.

    Returns a (ttm_ebit, is_estimated, gelir_data) tuple. `is_estimated` is
    True when the prior-year figures needed for a true TTM calc weren't
    available (e.g. a recent listing) and the current year-to-date EBIT was
    annualized instead (YTD / month * 12) as an approximation. `gelir_data`
    is the raw fetched dict, returned so the caller can pull other line
    items (e.g. financing expense) from the same period without an extra
    API call.
    """
    if current_month == 12:
        data = fetch_midas(ticker, [(current_year, 12)], bilanco=False)
        return get_midas_value(data, "gelir-table", MIDAS_EBIT_CODE, 0), False, data

    prior_year = current_year - 1
    dates = [(current_year, current_month), (prior_year, 12), (prior_year, current_month)]
    data = fetch_midas(ticker, dates, bilanco=False)

    ebit_cur        = get_midas_value(data, "gelir-table", MIDAS_EBIT_CODE, 0)
    ebit_prior_full = get_midas_value(data, "gelir-table", MIDAS_EBIT_CODE, 1)
    ebit_prior_ytd  = get_midas_value(data, "gelir-table", MIDAS_EBIT_CODE, 2)

    if None in (ebit_prior_full, ebit_prior_ytd):
        if ebit_cur is None:
            return None, False, data   # Nothing usable at all — exclude the stock
        annualized = ebit_cur / current_month * 12
        return annualized, True, data  # Flagged estimate, not a true TTM figure

    return ebit_cur + ebit_prior_full - ebit_prior_ytd, False, data


# ── Page Scraping Helpers (isyatirim — period, market cap, net debt, volume) ───

def detect_period(text: str) -> str | None:
    """Extract the latest YYYY/Q quarterly period string from page text."""
    # (?<!\d) and (?!\d) act as digit-boundaries so we never grab a
    # substring out of a longer run of digits (fixes the 8020/4 bug)
    matches = re.findall(r"(?<!\d)(\d{4})/(\d{1,2})(?!\d)", text)

    valid = [
        (int(year), int(q))
        for year, q in matches
        if 2020 < int(year) < 2100 and int(q) % 3 == 0
    ]

    if not valid:
        return None

    latest = max(valid)  # sorts by year, then quarter
    return f"{latest[0]}/{latest[1]}"


def extract_after_keyword(text: str, keyword: str, chars: int = 100) -> str | None:
    """Return a substring starting right after `keyword` in `text`."""
    idx = text.find(keyword)
    if idx == -1:
        return None
    return text[idx + len(keyword): idx + len(keyword) + chars]


def parse_turkish_number(text: str | None) -> float | None:
    """
    Parse a Turkish-formatted number (1.234,56) from a string snippet.
    Returns a float or None.
    """
    if not text:
        return None
    # Try full Turkish decimal format first: 1.234,56
    match = re.search(r"-?[\d]{1,3}(?:\.\d{3})*,\d+", text)
    if match:
        raw = match.group().strip()
        raw = re.sub(r"mn\s*TL", "", raw, flags=re.IGNORECASE).strip()
        raw = raw.replace(".", "").replace(",", ".")
        try:
            return float(raw)
        except ValueError:
            return None
    # Fallback: integer with thousands separator (1.234)
    matches = re.findall(r"-?(?:\d{1,3}\.)+\d{3}", text)
    if matches:
        try:
            return float(matches[0].replace(".", ""))
        except ValueError:
            return None
    return None


def month_to_group(month: int) -> str:
    """Map a month number to the isyatirim period group name."""
    return {12: "December", 9: "September", 6: "June", 3: "March"}.get(month, f"Month_{month}")


# ── Main Fetch Function ────────────────────────────────────────────────────────

def fetch_stock(ticker: str) -> dict | None:
    """
    Fetch all required data for a single ticker.

    Reporting period, market cap, net debt, average volume, and company
    name come from isyatirim's "sirket-karti" page (unchanged approach).
    TTM EBIT and balance-sheet items (current assets/liabilities, total
    assets, intangibles, financing expense) come from Midas's JSON API.

    Returns a result dict or None if data is unavailable.
    """
    isy_url = f"{ISYATIRIM_BASE_URL}/tr-tr/analiz/hisse/Sayfalar/sirket-karti.aspx?hisse={ticker}"
    resp = request_with_retry(isy_url)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text()

    # ── Period detection (isyatirim) ───────────────────────────────────────
    period = detect_period(text)
    if not period:
        return None

    year  = int(period.split("/")[0])
    month = int(period.split("/")[1])
    group = month_to_group(month)

    # ── Market cap & net debt & volume (scraped from isyatirim page) ──────
    market_cap = parse_turkish_number(extract_after_keyword(text, "Piyasa Değeri", 30))
    net_debt   = parse_turkish_number(extract_after_keyword(text, "Net Borç", 30))
    volume     = parse_turkish_number(extract_after_keyword(text, "Ort Hacim (mn$) 3A/12A", 30))

    if market_cap and net_debt:
        ev = (market_cap + net_debt) * 1_000_000
    elif market_cap:
        ev = market_cap * 1_000_000
    else:
        ev = None

    # ── TTM EBIT (Midas) ────────────────────────────────────────────────────
    ebit, ebit_is_estimated, gelir_data = fetch_ttm_ebit_midas(ticker, year, month)

    # ── Balance sheet, latest period (Midas) ────────────────────────────────
    bilanco_data = fetch_midas(ticker, [(year, month)], bilanco=True)

    current_assets = get_midas_value(bilanco_data, "bilanco", MIDAS_CURR_ASSETS_CODE, 0)
    current_liab   = get_midas_value(bilanco_data, "bilanco", MIDAS_CURR_LIAB_CODE, 0)
    total_assets   = get_midas_value(bilanco_data, "bilanco", MIDAS_TOTAL_ASSETS_CODE, 0)
    intangibles    = get_midas_value(bilanco_data, "bilanco", MIDAS_INTANGIBLES_CODE, 0) or 0.0

    fin_expense_raw = get_midas_value(gelir_data, "gelir-table", MIDAS_FIN_EXPENSE_CODE, 0)
    fin_expense     = abs(fin_expense_raw) if fin_expense_raw else None

    # ── Magic Formula metrics ──────────────────────────────────────────────
    earnings_yield = None
    if ebit and ev and ev > 0:
        earnings_yield = ebit / ev

    roc = None
    if ebit and current_assets is not None and current_liab is not None and total_assets:
        nwc    = current_assets - current_liab
        nfa    = total_assets - current_assets - intangibles
        capital = nwc + nfa
        if capital != 0:
            roc = ebit / capital

    # ── Company name (isyatirim) ───────────────────────────────────────────
    title_tag = soup.find("title")
    name = title_tag.text.split("|")[0].strip() if title_tag else ticker

    return {
        "Ticker":          ticker,
        "Name":            name,
        "Period":          period,
        "Group":           group,
        "EBIT_TTM":        ebit,
        "EBIT_Estimated":  ebit_is_estimated,
        "EnterpriseValue": ev,
        "EarningsYield":   earnings_yield,
        "RoC":             roc,
        "MarketCap_mnTL":  market_cap,
        "NetDebt_mnTL":    net_debt,
        "FinansmanGideri": fin_expense,
        "Volume_mnUSD":    volume,
    }


# ── Midas-Only Refresh (updates an existing raw CSV, no isyatirim access) ─────

def refresh_stock_midas_only(row: dict) -> dict:
    """
    Re-fetch only the Midas-derived fields for a single ticker, reusing the
    reporting period and isyatirim-sourced fields (market cap, net debt,
    volume, company name) already present in an existing raw CSV row.

    Used with --midas-only when isyatirim can't be reached at all (e.g. VPN
    / geo-blocking) but the previously fetched period, market cap, net
    debt, and volume for that ticker are still considered valid/current
    enough to reuse. Only EBIT_TTM, EBIT_Estimated, EarningsYield, RoC, and
    FinansmanGideri are refreshed — everything else in the row (Ticker,
    Name, Period, Group, MarketCap_mnTL, NetDebt_mnTL, EnterpriseValue,
    Volume_mnUSD) passes through unchanged.

    `row` is a dict (e.g. from a DataFrame.itertuples()/to_dict() row) that
    must contain at least "Ticker", "Period" (as "YYYY/M"), and
    "EnterpriseValue". Returns an updated dict; on failure to parse the
    stored period, the row is returned unchanged with
    "_midas_refresh_failed": True so the caller can report it.
    """
    ticker = row["Ticker"]
    result = dict(row)
    result["_midas_refresh_failed"] = False

    period = str(row.get("Period", ""))
    try:
        year_str, month_str = period.split("/")
        year, month = int(year_str), int(month_str)
    except (ValueError, AttributeError):
        result["_midas_refresh_failed"] = True
        return result

    ebit, ebit_is_estimated, gelir_data = fetch_ttm_ebit_midas(ticker, year, month)
    bilanco_data = fetch_midas(ticker, [(year, month)], bilanco=True)

    current_assets = get_midas_value(bilanco_data, "bilanco", MIDAS_CURR_ASSETS_CODE, 0)
    current_liab   = get_midas_value(bilanco_data, "bilanco", MIDAS_CURR_LIAB_CODE, 0)
    total_assets   = get_midas_value(bilanco_data, "bilanco", MIDAS_TOTAL_ASSETS_CODE, 0)
    intangibles    = get_midas_value(bilanco_data, "bilanco", MIDAS_INTANGIBLES_CODE, 0) or 0.0

    fin_expense_raw = get_midas_value(gelir_data, "gelir-table", MIDAS_FIN_EXPENSE_CODE, 0)
    fin_expense     = abs(fin_expense_raw) if fin_expense_raw else None

    ev = row.get("EnterpriseValue")
    try:
        ev = None if ev is None or pd.isna(ev) else float(ev)
    except (TypeError, ValueError):
        ev = None

    earnings_yield = None
    if ebit and ev and ev > 0:
        earnings_yield = ebit / ev

    roc = None
    if ebit and current_assets is not None and current_liab is not None and total_assets:
        nwc     = current_assets - current_liab
        nfa     = total_assets - current_assets - intangibles
        capital = nwc + nfa
        if capital != 0:
            roc = ebit / capital

    result.update({
        "EBIT_TTM":        ebit,
        "EBIT_Estimated":  ebit_is_estimated,
        "EarningsYield":   earnings_yield,
        "RoC":             roc,
        "FinansmanGideri": fin_expense,
    })
    return result


def run_midas_only_refresh(input_path: str, workers: int) -> None:
    """
    Load an existing raw CSV (produced by a prior full run) and refresh
    only its Midas-derived columns, skipping isyatirim entirely.
    """
    path = Path(input_path)
    if not path.exists():
        print(f"[ERROR] Input file not found: {input_path}")
        sys.exit(1)

    df_old = pd.read_csv(path)
    required_cols = {"Ticker", "Period", "EnterpriseValue"}
    missing = required_cols - set(df_old.columns)
    if missing:
        print(f"[ERROR] Input CSV is missing required column(s): {sorted(missing)}")
        sys.exit(1)

    rows = df_old.to_dict(orient="records")
    total = len(rows)
    completed = 0
    results = []

    print(
        f"\n[Midas-only mode] Refreshing {total} tickers from {input_path} "
        f"(isyatirim skipped — reusing its stored Period/MarketCap/NetDebt/Volume)...\n"
    )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(refresh_stock_midas_only, r): r["Ticker"] for r in rows}

        for future in as_completed(futures):
            ticker = futures[future]
            completed += 1

            try:
                data = future.result()
            except Exception as e:
                print(f"[{completed:>4}/{total}]   {ticker:<6}  — error: {e}")
                time.sleep(0.1)
                continue

            if data.get("_midas_refresh_failed"):
                print(f"[{completed:>4}/{total}]   {ticker:<6}  — skipped: could not parse stored Period")
            else:
                est_flag = " [EST]" if data.get("EBIT_Estimated") else ""
                ey, roc = data.get("EarningsYield"), data.get("RoC")
                ey_str  = f"{ey:.4f}" if ey is not None else "None"
                roc_str = f"{roc:.4f}" if roc is not None else "None"
                print(f"[{completed:>4}/{total}] ✓ {ticker:<6}  EY: {ey_str}  RoC: {roc_str}{est_flag}")

            results.append(data)
            time.sleep(0.1)  # Be polite to Midas's server

    df_new = pd.DataFrame(results).drop(columns=["_midas_refresh_failed"], errors="ignore")

    date_str = datetime.now().strftime("%Y%m%d")
    raw_file = f"bist_greenblatt_raw_{date_str}_midasonly.csv"
    df_new.to_csv(raw_file, index=False)
    print(f"\nUpdated raw data saved → {raw_file}")
    print(f"Total stocks refreshed: {len(df_new)}")

    print("\n" + "=" * 60)
    print("MAGIC FORMULA RANKING (combined — Midas-only refresh)")
    print("=" * 60)
    rank_and_save(df_new, f"{date_str}_midasonly")

    print("\nDone.")


# ── Ranking ────────────────────────────────────────────────────────────────────

def rank_and_save(df: pd.DataFrame, date_str: str, group_name: str | None = None) -> pd.DataFrame | None:
    """
    Filter, rank, and save stocks by Magic Formula score.

    By default (group_name=None) ALL eligible stocks are ranked together in
    one combined table. This is the correct way to run the screen: TTM
    Earnings Yield and RoC already normalize each company to its own
    trailing twelve months, so a December reporter and a June reporter are
    perfectly comparable and belong in the same ranked list. Splitting them
    into separate December/September/June/March tables (the old default)
    just fragments one screen into four incomplete ones with no combined
    output anywhere.

    Pass group_name to instead restrict to stocks whose most recent filing
    falls in that reporting-quarter cohort (legacy/optional mode — useful
    only if you specifically want to compare "reporting freshness" groups
    against each other, not for the actual Magic Formula ranking).

    Returns the ranked DataFrame (or None if no valid stocks).
    """
    g = df if group_name is None else df[df["Group"] == group_name]
    g = g.copy()
    g = g.dropna(subset=["EarningsYield", "RoC"])
    g = g[g["EarningsYield"] > 0]
    g = g[g["RoC"] > 0]
    g = g[g["MarketCap_mnTL"] >= MIN_MARKET_CAP_MN_TL]

    if MIN_VOLUME_MN_USD is not None:
        g = g[g["Volume_mnUSD"].isna() | (g["Volume_mnUSD"] >= MIN_VOLUME_MN_USD)]

    label = group_name if group_name else "all stocks (combined)"
    if len(g) == 0:
        print(f"  No valid stocks in {label}")
        return None

    g["EY_Rank"]     = g["EarningsYield"].rank(ascending=False, method="min")
    g["RoC_Rank"]    = g["RoC"].rank(ascending=False, method="min")
    g["Magic_Score"] = g["EY_Rank"] + g["RoC_Rank"]
    g = g.sort_values("Magic_Score").reset_index(drop=True)
    g.index += 1

    g["EarningsYield_%"] = (g["EarningsYield"] * 100).round(2)
    g["RoC_%"]           = (g["RoC"] * 100).round(2)

    suffix = group_name.lower() if group_name else "combined"
    filename = f"magic_formula_{suffix}_{date_str}.csv"
    g.to_csv(filename, index=True, index_label="Rank")
    print(f"  Saved {len(g)} stocks → {filename}")

    display_cols = ["Ticker", "Name", "Period", "EarningsYield_%", "RoC_%",
                    "EY_Rank", "RoC_Rank", "Magic_Score", "EBIT_Estimated"]
    print(g[display_cols].head(20).to_string())

    n_estimated = int(g["EBIT_Estimated"].sum())
    if n_estimated:
        print(
            f"  Note: {n_estimated} stock(s) above use an *annualized estimate* "
            f"of TTM EBIT (true prior-year data was unavailable). Check the "
            f"EBIT_Estimated column before trusting their rank."
        )
    return g


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="BIST Magic Formula Screener",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--tickers", default="bist_tickers.txt",
        help="Path to the ticker list file"
    )
    parser.add_argument(
        "--workers", type=int, default=MAX_WORKERS,
        help="Number of concurrent threads"
    )
    parser.add_argument(
        "--by-group", action="store_true",
        help="Also produce separate rankings split by reporting-quarter "
             "cohort (December/September/June/March), in addition to the "
             "single combined ranking that runs by default"
    )
    parser.add_argument(
        "--midas-only", action="store_true",
        help="Skip isyatirim entirely and only refresh Midas-derived fields "
             "(TTM EBIT, balance-sheet items, EarningsYield, RoC) in an "
             "existing raw CSV. Requires --input. Use this when isyatirim "
             "isn't reachable (e.g. VPN/geo restrictions) — reporting "
             "period, market cap, net debt, and volume are reused as-is "
             "from that CSV rather than re-scraped."
    )
    parser.add_argument(
        "--input",
        help="Path to an existing raw CSV (e.g. bist_greenblatt_raw_YYYYMMDD.csv) "
             "to refresh. Required when --midas-only is set."
    )
    args, unknown = parser.parse_known_args()

    if args.midas_only:
        if not args.input:
            print("[ERROR] --midas-only requires --input <path to existing raw CSV>")
            sys.exit(1)
        run_midas_only_refresh(args.input, args.workers)
        return

    tickers = load_tickers(args.tickers)
    results = []
    total = len(tickers)
    completed = 0

    print(f"\nFetching data for {total} tickers with {args.workers} threads...\n")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(fetch_stock, t): t for t in tickers}

        for future in as_completed(futures):
            ticker = futures[future]
            completed += 1

            try:
                data = future.result()
            except Exception as e:
                # Any unhandled exception inside fetch_stock (e.g. a KeyError
                # or AttributeError from a malformed page/response) lands
                # here instead of killing the whole run.
                print(f"[{completed:>4}/{total}]   {ticker:<6}  — error: {e}")
                time.sleep(0.1)
                continue

            if data and data["EarningsYield"] is not None and data["RoC"] is not None:
                results.append(data)
                est_flag = " [EST]" if data["EBIT_Estimated"] else ""
                print(
                    f"[{completed:>4}/{total}] ✓ {ticker:<6}  "
                    f"{data['Period']} ({data['Group']:<10})  "
                    f"EY: {data['EarningsYield']:.4f}  RoC: {data['RoC']:.4f}{est_flag}"
                )
            else:
                reason = "no data returned (fetch failed / no period found)" if data is None else "missing EY/RoC"
                print(f"[{completed:>4}/{total}]   {ticker:<6}  — skipped: {reason}")

            time.sleep(0.1)  # Be polite to the servers

    if not results:
        print("\nNo results collected. Check your internet connection and ticker file.")
        sys.exit(1)

    df = pd.DataFrame(results)
    date_str = datetime.now().strftime("%Y%m%d")

    raw_file = f"bist_greenblatt_raw_{date_str}.csv"
    df.to_csv(raw_file, index=False)
    print(f"\nRaw data saved → {raw_file}")
    print(f"Total stocks fetched: {len(df)}")
    print("\nPeriod breakdown:")
    print(df["Group"].value_counts().to_string())

    print("\n" + "=" * 60)
    print("MAGIC FORMULA RANKING (combined — all eligible stocks)")
    print("=" * 60)
    rank_and_save(df, date_str)

    if args.by_group:
        print("\n" + "=" * 60)
        print("RANKING BY PERIOD GROUP (legacy quarterly cohorts, optional)")
        print("=" * 60)
        for group in ["December", "September", "June", "March"]:
            print(f"\n--- {group} ---")
            rank_and_save(df, date_str, group_name=group)

    print("\nDone.")


if __name__ == "__main__":
    main()
