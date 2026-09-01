"""
BIST Magic Formula Screener
----------------------------
Screens Borsa Istanbul (BIST) stocks using Joel Greenblatt's Magic Formula:
  - Earnings Yield    = TTM EBIT / Enterprise Value
  - Return on Capital = TTM EBIT / (Net Working Capital + Net Fixed Assets)

Two data sources, because neither one has everything:
  - getmidas.com      -> TTM EBIT, balance sheet, market cap (more precise
                         financials, via Midas's public JSON API)
  - isyatirim.com.tr  -> reporting period, net debt, average volume, company
                         name (scraped from the "sirket-karti" HTML page)

Run `--midas-only --input <raw csv>` to refresh just the Midas figures when
isyatirim can't be reached (VPN / geo-blocking).
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

ISYATIRIM_CARD_URL       = "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/sirket-karti.aspx?hisse={ticker}"
ISYATIRIM_COMPARISON_URL = "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/Temel-Degerler-Ve-Oranlar.aspx"
MIDAS_QUOTE_URL          = "https://www.getmidas.com/canli-borsa/{ticker}-hisse/"
MIDAS_API_URL            = "https://www.getmidas.com/wp-json/midas-api/v1/midas_bilnaco_date"

# Purely a prefilter to skip the expensive per-ticker fetch_stock() pipeline
# for names that almost certainly won't clear apply_magic_formula_to_all.py's
# real 25,000 mn TL floor. Kept well below that real threshold as a safety
# margin because this number comes from isyatirim's bulk comparison table,
# while the actual market cap that ends up in the CSV and gets filtered on
# later still comes from fetch_market_cap() (Midas) — a different source
# that can disagree slightly with isyatirim near the boundary.
PREFILTER_MARKET_CAP_FLOOR_MN_TL = 20_000

# (connect, read). A tight connect timeout fails fast on an unreachable host;
# the longer read timeout still allows for a slow response body.
REQUEST_TIMEOUT = (5, 15)
MAX_RETRIES     = 5   # total attempts, not extra attempts
RETRY_BACKOFF_S = 2   # doubles each retry: 2s, 4s, 8s, 16s...

# Midas line-item labels, keyed by the section they live in.
BALANCE_SHEET  = "bilanco"
INCOME_STMT    = "gelir-table"

CURR_ASSETS_LINE  = "DÖNEN VARLIKLAR"
CURR_LIAB_LINE    = "KISA VADELİ YÜKÜMLÜLÜKLER"
TOTAL_ASSETS_LINE = "TOPLAM VARLIKLAR"
INTANGIBLES_LINE  = "Maddi Olmayan Duran Varlıklar"
EBIT_LINE         = "ESAS FAALİYET KARI/ZARARI"
FIN_EXPENSE_LINE  = "Finansman Giderleri (-)"

MIN_VOLUME_MN_USD = 5    # Minimum avg volume filter (mn USD); None = no filter
MAX_WORKERS       = 5    # Concurrent threads

# Column order of the raw CSV (kept stable — downstream scripts read it).
RAW_COLUMNS = [
    "Ticker", "Name", "Period", "Group", "EBIT_TTM", "EBIT_Estimated",
    "EnterpriseValue", "EarningsYield", "RoC", "MarketCap_mnTL",
    "NetDebt_mnTL", "FinansmanGideri", "Volume_mnUSD",
]


# ── Networking ─────────────────────────────────────────────────────────────────

def request_with_retry(url: str) -> requests.Response | None:
    """
    GET `url`, retrying transient failures (timeouts, connection errors, 5xx)
    with exponential backoff. 4xx responses are not retried — a bad ticker
    won't get better on the second try. Returns None once attempts run out.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, verify=False)
            if not 500 <= resp.status_code < 600:
                resp.raise_for_status()  # raises on 4xx
                return resp
        except requests.exceptions.HTTPError:
            return None  # 4xx
        except Exception:
            pass

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_S * 2 ** (attempt - 1))

    return None


# ── Midas API ──────────────────────────────────────────────────────────────────

def is_empty_midas(data: dict | None) -> bool:
    """
    True when Midas returned nothing for the primary requested period (date1
    / period index 0) in any section — the signal that this ticker doesn't
    file under the requested consolidation flag (consolidated vs. solo) and
    the caller should retry with the other one.

    Checking only index 0 (not "every requested date slot", the original
    check) matters because a ticker that files solo-only can still have a
    leftover non-empty slot elsewhere in the same response — e.g. AVTUR
    returned real consolidated data for one of the three distinct dates
    requested but nothing for the current period (date1) itself, so the old
    "all slots empty" check saw a non-empty slot and never fell back to solo,
    leaving the actually-needed period silently empty (mis-logged as "missing
    EBIT data" further downstream, rather than as a consolidation mismatch).
    """
    if not data:
        return True
    return any(
        isinstance(section, list) and (not section or not section[0])
        for section in data.values()
    )


def fetch_midas(ticker: str, dates: list[tuple[int, int]], section: str) -> dict | None:
    """
    Fetch one Midas statement section for up to 4 (year, month) periods.

    `section` picks the endpoint variant: BALANCE_SHEET adds `bilanco=true`,
    INCOME_STMT omits it. The API always wants 4 date slots, so a shorter
    list is padded by repeating the last one.

    Consolidated statements are tried first and, if the company doesn't file
    them, the request is repeated for solo statements.
    """
    def url_for(consolidated: int) -> str:
        padded = list(dates) + [dates[-1]] * (4 - len(dates))
        params = [f"code={ticker}"]
        for i, (year, month) in enumerate(padded, start=1):
            # "consildated" is Midas's own spelling — not a typo on our side.
            params += [f"date{i}={year}-{month}", f"consildated{i}={consolidated}"]
        if section == BALANCE_SHEET:
            params.append("bilanco=true")
        return f"{MIDAS_API_URL}?{'&'.join(params)}"

    def get(consolidated: int) -> dict | None:
        resp = request_with_retry(url_for(consolidated))
        if resp is None:
            return None
        try:
            data = resp.json()
            return json.loads(data) if isinstance(data, str) else data
        except (ValueError, TypeError):
            return None

    data = get(1)
    return (get(0) or data) if is_empty_midas(data) else data


def midas_value(data: dict | None, section: str, line: str, period_index: int = 0) -> float | None:
    """
    Read one line item out of a Midas response.

    `period_index` selects which of the requested date slots to read (0 is
    date1). Returns None when the line is missing, zero, or the period has no
    data — e.g. a recently listed company with no history that far back.
    """
    if not data or section not in data:
        return None
    try:
        period = data[section][period_index]
    except (IndexError, TypeError):
        return None

    for item in period:
        if item.get("description") == line:
            value = item.get("value")
            if value in (None, 0):
                return None
            try:
                return float(value)
            except (ValueError, TypeError):
                return None
    return None


def ttm_from_ytd(data: dict | None, section: str, line: str, month: int) -> tuple[float | None, bool]:
    """
    Convert one YTD-cumulative income-statement line into a trailing-twelve-
    month figure. Midas reports every income-statement line cumulatively
    within the fiscal year — not just EBIT — so:
        TTM = YTD(year, month) + YTD(prior year, 12) - YTD(prior year, month)
    December reporters' YTD figure already covers the full year, so it's used
    as-is. `data` must already have been fetched with those three date slots
    (or just the December one) via `fetch_midas`.

    Returns (value, is_estimated). `is_estimated` is True when the prior-year
    figures needed for a real TTM weren't available (a recent listing, say)
    and the YTD figure was annualized instead — a rough approximation, worth
    treating with suspicion.
    """
    if month == 12:
        return midas_value(data, section, line, 0), False

    current    = midas_value(data, section, line, 0)
    prior_full = midas_value(data, section, line, 1)
    prior_ytd  = midas_value(data, section, line, 2)

    if current is None:
        return None, False                    # nothing usable

    if prior_full is None or prior_ytd is None:
        return current / month * 12, True

    return current + prior_full - prior_ytd, False


def fetch_ttm_ebit(ticker: str, year: int, month: int) -> tuple[float | None, bool, dict | None]:
    """
    TTM EBIT from Midas's income statement (see `ttm_from_ytd`).

    Returns (ebit, is_estimated, income_data). `income_data` is handed back so
    the caller can convert other income-statement lines (financing expense)
    from the same response instead of paying for another request.
    """
    if month == 12:
        data = fetch_midas(ticker, [(year, 12)], INCOME_STMT)
    else:
        prior = year - 1
        data = fetch_midas(ticker, [(year, month), (prior, 12), (prior, month)], INCOME_STMT)

    ebit, is_estimated = ttm_from_ytd(data, INCOME_STMT, EBIT_LINE, month)

    if ebit is None:
        print(f"[WARN] {ticker}: missing EBIT data for {year}/{month}")

    return ebit, is_estimated, data


def fetch_financials(ticker: str, year: int, month: int) -> dict:
    """TTM EBIT, Return on Capital, and financing expense for one period."""
    ebit, is_estimated, income_data = fetch_ttm_ebit(ticker, year, month)
    balance = fetch_midas(ticker, [(year, month)], BALANCE_SHEET)

    current_assets = midas_value(balance, BALANCE_SHEET, CURR_ASSETS_LINE)
    current_liab   = midas_value(balance, BALANCE_SHEET, CURR_LIAB_LINE)
    total_assets   = midas_value(balance, BALANCE_SHEET, TOTAL_ASSETS_LINE)
    intangibles    = midas_value(balance, BALANCE_SHEET, INTANGIBLES_LINE) or 0.0

    roc = None
    if ebit and current_assets is not None and current_liab is not None and total_assets:
        # Greenblatt's capital base is net working capital plus net fixed
        # assets, and the current-assets term cancels out of the sum:
        #   (CA - CL) + (TA - CA - intangibles)  ==  TA - CL - intangibles
        # Current assets are still required above, as a completeness check on
        # the balance sheet — a company missing that line is missing others.
        capital = total_assets - current_liab - intangibles
        # Capital employed must be positive for the ratio to mean anything —
        # a negative EBIT over negative capital would otherwise divide out to
        # a spuriously high positive RoC (seen live on CRFSA).
        if capital > 0:
            roc = ebit / capital

    fin_expense, _ = ttm_from_ytd(income_data, INCOME_STMT, FIN_EXPENSE_LINE, month)

    return {
        "EBIT_TTM":        ebit,
        "EBIT_Estimated":  is_estimated,
        "RoC":             roc,
        "FinansmanGideri": abs(fin_expense) if fin_expense else None,
    }


def fetch_market_cap(ticker: str) -> float | None:
    """Market cap in millions of TL, scraped from the Midas quote page."""
    resp = request_with_retry(MIDAS_QUOTE_URL.format(ticker=ticker.lower()))
    if resp is None:
        return None

    text = BeautifulSoup(resp.text, "lxml").get_text()
    value = parse_turkish_number(extract_after_keyword(text, "Piyasa Değeri", 50))
    return value / 1_000_000 if value is not None else None


def fetch_market_cap_lookup() -> dict[str, float]:
    """
    One request to isyatirim's fundamentals comparison page, which renders a
    table of every listed stock's ticker and market cap (mn TL) in a single
    page load — used to cheaply prefilter which tickers are even worth
    running the full per-ticker fetch_stock() pipeline on, instead of paying
    for isyatirim + Midas financials calls on names that will just get
    dropped later by the market-cap floor anyway.

    Returns {} on any failure (page unreachable, layout changed) rather than
    raising — callers should treat an empty dict as "couldn't prefilter, fall
    back to fetching everything" rather than "nothing has a big enough
    market cap".
    """
    resp = request_with_retry(ISYATIRIM_COMPARISON_URL)
    if resp is None:
        return {}

    soup = BeautifulSoup(resp.text, "lxml")
    table = None
    header = []
    for candidate in soup.find_all("table"):
        rows = candidate.find_all("tr")
        if not rows:
            continue
        candidate_header = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if "Kod" in candidate_header and any(h.startswith("Piyasa Değeri(mn TL)") for h in candidate_header):
            table, header = candidate, candidate_header
            break
    if table is None:
        return {}

    ticker_idx = header.index("Kod")
    cap_idx = next(i for i, h in enumerate(header) if h.startswith("Piyasa Değeri(mn TL)"))

    caps: dict[str, float] = {}
    for row in table.find_all("tr")[1:]:
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) <= max(ticker_idx, cap_idx):
            continue
        ticker = cells[ticker_idx].strip().upper()
        cap = parse_turkish_number(cells[cap_idx])
        if ticker and cap is not None:
            caps[ticker] = cap
    return caps


# ── Text Parsing (isyatirim page) ──────────────────────────────────────────────

def detect_period(text: str) -> tuple[int, int] | None:
    """Latest YYYY/Q reporting period on the page, as a (year, month) pair."""
    # The digit-boundary lookarounds stop us grabbing "020/4" out of a longer
    # run of digits like 8020/4.
    matches = re.findall(r"(?<!\d)(\d{4})/(\d{1,2})(?!\d)", text)
    periods = [
        (int(year), int(month))
        for year, month in matches
        if 2020 < int(year) < 2100 and int(month) % 3 == 0
    ]
    return max(periods) if periods else None


def extract_after_keyword(text: str, keyword: str, chars: int = 100) -> str | None:
    """Return the `chars` characters that follow `keyword` in `text`."""
    idx = text.find(keyword)
    if idx == -1:
        return None
    start = idx + len(keyword)
    return text[start:start + chars]


def parse_turkish_number(text: str | None) -> float | None:
    """Parse a Turkish-formatted number (1.234,56 or 1.234) out of a snippet."""
    if not text:
        return None

    # Decimal form first: 1.234,56
    match = re.search(r"-?\d{1,3}(?:\.\d{3})*,\d+", text)
    if match:
        raw = match.group().replace(".", "").replace(",", ".")
    else:
        # Fallback: integer with thousands separators, 1.234
        matches = re.findall(r"-?(?:\d{1,3}\.)+\d{3}", text)
        if not matches:
            return None
        raw = matches[0].replace(".", "")

    try:
        return float(raw)
    except ValueError:
        return None


def month_to_group(month: int) -> str:
    """Name of the reporting-quarter cohort a month belongs to."""
    return {3: "March", 6: "June", 9: "September", 12: "December"}.get(month, f"Month_{month}")


def to_float(value) -> float | None:
    """Coerce a CSV cell (which may be NaN, "", or a string) to a float."""
    try:
        return None if value is None or pd.isna(value) else float(value)
    except (TypeError, ValueError):
        return None


# ── Row Building ───────────────────────────────────────────────────────────────

def build_row(ticker: str, name: str, year: int, month: int,
              net_debt: float | None, volume: float | None) -> dict:
    """
    Assemble one screener row from Midas data plus the four isyatirim-sourced
    fields, whichever way the caller obtained them (live scrape or a stored
    CSV row). Market cap and net debt are both in millions of TL.
    """
    market_cap = fetch_market_cap(ticker)

    ev = None
    if market_cap:
        ev = (market_cap + (net_debt or 0)) * 1_000_000

    financials = fetch_financials(ticker, year, month)
    ebit = financials["EBIT_TTM"]

    return {
        "Ticker":          ticker,
        "Name":            name,
        "Period":          f"{year}/{month}",
        "Group":           month_to_group(month),
        "EnterpriseValue": ev,
        "EarningsYield":   ebit / ev if ebit and ev and ev > 0 else None,
        "MarketCap_mnTL":  market_cap,
        "NetDebt_mnTL":    net_debt,
        "Volume_mnUSD":    volume,
        **financials,
    }


def fetch_stock(ticker: str) -> dict | None:
    """Full fetch for one ticker: isyatirim page + Midas financials."""
    resp = request_with_retry(ISYATIRIM_CARD_URL.format(ticker=ticker))
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text()

    period = detect_period(text)
    if period is None:
        return None
    year, month = period

    title = soup.find("title")
    return build_row(
        ticker   = ticker,
        name     = title.text.split("|")[0].strip() if title else ticker,
        year     = year,
        month    = month,
        net_debt = parse_turkish_number(extract_after_keyword(text, "Net Borç", 30)),
        volume   = parse_turkish_number(extract_after_keyword(text, "Ort Hacim (mn$) 3A/12A", 30)),
    )


def refresh_stock(row: dict) -> dict | None:
    """
    Re-fetch only the Midas side for one ticker, reusing the period, name, net
    debt, and volume already stored in a raw CSV row. Returns None if that
    row's period can't be parsed.
    """
    try:
        year, month = (int(part) for part in str(row.get("Period", "")).split("/"))
    except (ValueError, TypeError):
        return None

    return build_row(
        ticker   = row["Ticker"],
        name     = row.get("Name", row["Ticker"]),
        year     = year,
        month    = month,
        net_debt = to_float(row.get("NetDebt_mnTL")),
        volume   = to_float(row.get("Volume_mnUSD")),
    )


# ── Concurrent Runner ──────────────────────────────────────────────────────────

def run_all(items: list, worker, workers: int) -> list[dict]:
    """
    Run `worker` over `items` in a thread pool, printing a progress line per
    ticker. `items` are either ticker strings or raw-CSV row dicts; a worker
    returning None means the ticker was skipped.
    """
    def ticker_of(item):
        return item if isinstance(item, str) else item["Ticker"]

    results = []
    total = len(items)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(worker, item): ticker_of(item) for item in items}

        for done, future in enumerate(as_completed(futures), start=1):
            ticker = futures[future]
            prefix = f"[{done:>4}/{total}]"

            try:
                row = future.result()
            except Exception as e:
                # An unhandled error on one ticker (malformed page, odd
                # response shape) shouldn't take the whole run down with it.
                print(f"{prefix}   {ticker:<6}  — error: {e}")
                continue

            if row is None:
                print(f"{prefix}   {ticker:<6}  — skipped: no data")
                continue

            results.append(row)

            ey, roc = row["EarningsYield"], row["RoC"]
            if ey is None or roc is None:
                print(f"{prefix}   {ticker:<6}  {row['Period']:<7} — missing EY/RoC")
            else:
                flag = " [EST]" if row["EBIT_Estimated"] else ""
                print(
                    f"{prefix} ✓ {ticker:<6}  {row['Period']:<7} "
                    f"({row['Group']:<9})  EY: {ey:.4f}  RoC: {roc:.4f}{flag}"
                )

            time.sleep(0.1)  # be polite to the servers

    return results


# ── Ranking ────────────────────────────────────────────────────────────────────

def rank_and_save(df: pd.DataFrame, date_str: str, group_name: str | None = None) -> pd.DataFrame | None:
    """
    Rank stocks by combined Earnings Yield + RoC rank and save the table.

    By default every eligible stock is ranked together, which is the right way
    to run the screen: TTM figures already normalize each company to its own
    trailing twelve months, so a December reporter and a June reporter are
    directly comparable. Passing `group_name` restricts the table to one
    reporting-quarter cohort instead — only useful for comparing reporting
    freshness, not for the screen itself.
    """
    g = df if group_name is None else df[df["Group"] == group_name]
    g = g.copy().dropna(subset=["EarningsYield", "RoC"])

    if MIN_VOLUME_MN_USD is not None:
        g = g[g["Volume_mnUSD"].isna() | (g["Volume_mnUSD"] >= MIN_VOLUME_MN_USD)]

    if g.empty:
        print(f"  No valid stocks in {group_name or 'all stocks (combined)'}")
        return None

    g["EY_Rank"]     = g["EarningsYield"].rank(ascending=False, method="min")
    g["RoC_Rank"]    = g["RoC"].rank(ascending=False, method="min")
    g["Magic_Score"] = g["EY_Rank"] + g["RoC_Rank"]
    g = g.sort_values("Magic_Score").reset_index(drop=True)
    g.index += 1

    g["EarningsYield_%"] = (g["EarningsYield"] * 100).round(2)
    g["RoC_%"]           = (g["RoC"] * 100).round(2)

    filename = f"magic_formula_{(group_name or 'combined').lower()}_{date_str}.csv"
    g.to_csv(filename, index=True, index_label="Rank")
    print(f"  Saved {len(g)} stocks → {filename}")

    print(g[["Ticker", "Name", "Period", "EarningsYield_%", "RoC_%",
             "EY_Rank", "RoC_Rank", "Magic_Score", "EBIT_Estimated"]].head(20).to_string())

    estimated = int(g["EBIT_Estimated"].sum())
    if estimated:
        print(
            f"  Note: {estimated} stock(s) above use an *annualized estimate* of "
            f"TTM EBIT (real prior-year data was unavailable). Check the "
            f"EBIT_Estimated column before trusting their rank."
        )
    return g


def save_and_rank(results: list[dict], suffix: str, by_group: bool = False) -> None:
    """Write the raw CSV, then print and save the rankings."""
    if not results:
        print("\nNo results collected. Check your internet connection and ticker file.")
        sys.exit(1)

    df = pd.DataFrame(results)[RAW_COLUMNS]

    raw_file = f"bist_greenblatt_raw_{suffix}.csv"
    df.to_csv(raw_file, index=False)
    print(f"\nRaw data saved → {raw_file}")
    print(f"Total stocks fetched: {len(df)}")
    print("\nPeriod breakdown:")
    print(df["Group"].value_counts().to_string())

    print("\n" + "=" * 60)
    print("MAGIC FORMULA RANKING (combined — all eligible stocks)")
    print("=" * 60)
    rank_and_save(df, suffix)

    if by_group:
        print("\n" + "=" * 60)
        print("RANKING BY PERIOD GROUP (quarterly cohorts, optional)")
        print("=" * 60)
        for group in ["December", "September", "June", "March"]:
            print(f"\n--- {group} ---")
            rank_and_save(df, suffix, group_name=group)

    print("\nDone.")


# ── Entry Point ────────────────────────────────────────────────────────────────

def load_tickers(filepath: str) -> list[str]:
    """Load ticker symbols from a plain-text file, one per line."""
    path = Path(filepath)
    if not path.exists():
        sys.exit(f"[ERROR] Ticker file not found: {filepath}")

    tickers = [line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not tickers:
        sys.exit(f"[ERROR] No tickers found in {filepath}")

    print(f"Loaded {len(tickers)} tickers from {filepath}")
    return tickers


def load_raw_rows(filepath: str) -> list[dict]:
    """Load a previously saved raw CSV for a Midas-only refresh."""
    path = Path(filepath)
    if not path.exists():
        sys.exit(f"[ERROR] Input file not found: {filepath}")

    df = pd.read_csv(path)
    missing = {"Ticker", "Period"} - set(df.columns)
    if missing:
        sys.exit(f"[ERROR] Input CSV is missing required column(s): {sorted(missing)}")

    return df.to_dict(orient="records")


def main():
    parser = argparse.ArgumentParser(
        description="BIST Magic Formula Screener",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--tickers", default="bist_tickers.txt", help="Path to the ticker list file")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="Number of concurrent threads")
    parser.add_argument(
        "--by-group", action="store_true",
        help="Also produce separate rankings split by reporting-quarter cohort, "
             "in addition to the combined ranking that runs by default"
    )
    parser.add_argument(
        "--midas-only", action="store_true",
        help="Skip isyatirim and refresh only the Midas-derived fields (TTM EBIT, "
             "balance sheet, market cap, EarningsYield, RoC) in an existing raw CSV. "
             "Requires --input. Use when isyatirim isn't reachable — reporting "
             "period, net debt, and volume are reused from that CSV as-is."
    )
    parser.add_argument("--input", help="Existing raw CSV to refresh. Required with --midas-only.")
    parser.add_argument(
        "--no-prefilter", action="store_true",
        help="Skip the bulk market-cap prefilter and run the full per-ticker fetch on "
             "every ticker in --tickers, even ones that would almost certainly get "
             "dropped later by the market-cap floor. Use if isyatirim's comparison "
             "page is unreachable or its layout changed."
    )
    args, _ = parser.parse_known_args()

    date_str = datetime.now().strftime("%Y%m%d")

    if args.midas_only:
        if not args.input:
            sys.exit("[ERROR] --midas-only requires --input <path to existing raw CSV>")

        rows = load_raw_rows(args.input)
        print(f"\n[Midas-only mode] Refreshing {len(rows)} tickers from {args.input} "
              f"(reusing stored Period/NetDebt/Volume)...\n")
        results = run_all(rows, refresh_stock, args.workers)
        save_and_rank(results, f"{date_str}_midasonly", args.by_group)
        return

    tickers = load_tickers(args.tickers)

    if not args.no_prefilter:
        print("\n[Prefilter] Fetching market caps in bulk from isyatirim...")
        caps = fetch_market_cap_lookup()
        if caps:
            before = len(tickers)
            # Keep anything at/above the floor, AND anything the bulk table
            # didn't have an entry for at all — an unmatched ticker is a sign
            # of a naming mismatch, not evidence it's actually small, so it's
            # safer to still fetch it than to silently drop it.
            tickers = [t for t in tickers if t not in caps or caps[t] >= PREFILTER_MARKET_CAP_FLOOR_MN_TL]
            print(
                f"[Prefilter] {before} -> {len(tickers)} tickers "
                f"(kept >= {PREFILTER_MARKET_CAP_FLOOR_MN_TL:,} mn TL, or unmatched in the bulk table)\n"
            )
        else:
            print("[Prefilter] Couldn't fetch the bulk market cap table — proceeding with the full ticker list.\n")

    print(f"\nFetching data for {len(tickers)} tickers with {args.workers} threads...\n")
    results = run_all(tickers, fetch_stock, args.workers)
    save_and_rank(results, date_str, args.by_group)


if __name__ == "__main__":
    main()
