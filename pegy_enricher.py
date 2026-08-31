import requests
from bs4 import BeautifulSoup
import pandas as pd
import re
import time
import warnings
warnings.filterwarnings('ignore')
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

TTM_CUTOFF = datetime.now() - timedelta(days=365)


date_str = datetime.now().strftime('%Y%m%d')
filename = f"magic_formula_all_{date_str}.csv"
INPUT_CSV  = filename
OUTPUT_CSV = filename

# ── USD/TRY rate cache to avoid redundant API calls ───────────────────────────

def fetch_financial_data(ticker, current_period_str, prior_period_str):
    """Fetch financial data via isyatirim API for current and prior year periods."""
    try:
        cur_year, cur_month = current_period_str.split("/")
        pri_year, pri_month = prior_period_str.split("/")
        url = (
            f"https://www.isyatirim.com.tr/_layouts/15/IsYatirim.Website/Common/Data.aspx/MaliTablo"
            f"?companyCode={ticker}&exchange=USD&financialGroup=XI_29"
            f"&year1={cur_year}&period1={cur_month}"
            f"&year2={pri_year}&period2={pri_month}"
            f"&year3={pri_year}&period3={pri_month}"
            f"&year4={pri_year}&period4={pri_month}"
        )
        response = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        if response.status_code != 200:
            return None
        return response.json().get("value", [])
    except:
        return None

def get_item_value(data, item_code, col="value1"):
    """Extract a value from API response by item code."""
    try:
        for item in data:
            if item.get("itemCode") == item_code:
                val = item.get(col)
                return float(val) if val not in (None, "", "0") else None
        return None
    except:
        return None

def parse_number(raw):
    if not raw:
        return None
    raw = raw.strip().replace('\xa0', '').replace(' ', '')
    raw = re.sub(r'mn\s*TL', '', raw, flags=re.IGNORECASE).strip()
    try:
        if ',' in raw:
            raw = raw.replace('.', '').replace(',', '.')
        else:
            raw = raw.replace('.', '')
        return float(raw)
    except:
        return None

def extract_after_keyword(text, keyword, chars=100):
    idx = text.find(keyword)
    if idx == -1:
        return None
    return text[idx + len(keyword): idx + len(keyword) + chars]

def get_first_number(text):
    if not text:
        return None
    match = re.search(r'-?[\d]{1,3}(?:\.\d{3})*,\d+', text)
    if match:
        return parse_number(match.group())
    matches = re.findall(r'-?(?:\d{1,3}\.)+\d{3}', text)
    if matches:
        return parse_number(matches[0])
    return None

def get_ttm_dividend_yield(text, ticker):
    pattern = re.compile(
        rf'{re.escape(ticker)}(\d{{2}}\.\d{{2}}\.\d{{4}})(\d{{1,2}},\d+)'
    )
    seen_dates = set()
    ttm_total = 0.0
    for match in pattern.finditer(text):
        date_str  = match.group(1)
        verim_str = match.group(2)
        if date_str in seen_dates:
            continue
        seen_dates.add(date_str)
        try:
            row_date = datetime.strptime(date_str, "%d.%m.%Y")
            verim = float(verim_str.replace(',', '.'))
            if row_date >= TTM_CUTOFF:
                ttm_total += verim
        except:
            continue
    return ttm_total if ttm_total > 0 else None

def fetch_pegy(ticker):
    url = f"https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/sirket-karti.aspx?hisse={ticker}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        if response.status_code != 200:
            return ticker, None, None, None, None

        soup = BeautifulSoup(response.text, "lxml")
        text = soup.get_text()

        # ── P/E ───────────────────────────────────────────────────────────
        fk_text = extract_after_keyword(text, "F/K", 50)
        pe = get_first_number(fk_text)

        # ── Detect current and prior periods (always 1 year apart) ────────
        period_matches = re.findall(r'(\d{4}/\d{1,2})', text)
        current_period_str = period_matches[0] if period_matches else "2025/12"
        year, month = current_period_str.split("/")
        prior_period_str = f"{int(year) - 1}/{month}"

        # ── Fetch EBIT via API (value1=current, value2=prior year) ─────────
        api_data   = fetch_financial_data(ticker, current_period_str, prior_period_str)
        ebit       = get_item_value(api_data, "3DF", "value1") if api_data else None
        ebit_prior = get_item_value(api_data, "3DF", "value2") if api_data else None
        print(f"ebit: {ebit} | ebit_prior: {ebit_prior} ticker {ticker}")
        # ── USD-adjusted EBIT growth ───────────────────────────────────────
        ebit_growth = None
        if ebit is not None and ebit_prior is not None and ebit_prior != 0:
            ebit_growth = ((ebit - ebit_prior) / abs(ebit_prior)) * 100
            #ebit_growth = min(ebit_growth, 200)

        # ── TTM dividend yield ─────────────────────────────────────────────
        div_yield = get_ttm_dividend_yield(text, ticker)

        # ── PEGY ──────────────────────────────────────────────────────────
        pegy = None
        if pe and pe > 0 and ebit_growth and ebit_growth > 0:
            denominator = ebit_growth + (div_yield or 0)
            if denominator > 0:
                pegy = round((pe / denominator) * 100, 2)

        return ticker, pe, ebit_growth, div_yield, pegy

    except Exception as e:
        return ticker, None, None, None, None


# ── Main ───────────────────────────────────────────────────────────────────────

df = pd.read_csv(INPUT_CSV)
tickers = df["Ticker"].tolist()
total = len(tickers)
print(f"Loaded {total} rows from {INPUT_CSV}")
print(f"Fetching PEGY data for each ticker...\n")

results = {}
completed = 0

with ThreadPoolExecutor(max_workers=5) as executor:
    futures = {executor.submit(fetch_pegy, t): t for t in tickers}
    for future in as_completed(futures):
        ticker, pe, ebit_growth, div_yield, pegy = future.result()
        completed += 1
        results[ticker] = {
            "PE":              pe,
            "EBIT_Growth_pct": round(ebit_growth, 2) if ebit_growth is not None else None,
            "DivYield_pct":    div_yield,
            "PEGY":            pegy,
        }
        pegy_str = f"{pegy:.4f}" if pegy else "N/A"
        print(f"[{completed}/{total}] {ticker} | PE: {pe} | Growth: "
              f"{ebit_growth:.1f}%" if ebit_growth else f"[{completed}/{total}] {ticker} | PE: {pe} | Growth: N/A",
              f"| DivYield: {div_yield}% | PEGY: {pegy_str}")
        time.sleep(0.1)

# ── Append columns to dataframe ───────────────────────────────────────────────
df["PE"]              = df["Ticker"].map(lambda t: results.get(t, {}).get("PE"))
df["EBIT_Growth_pct"] = df["Ticker"].map(lambda t: results.get(t, {}).get("EBIT_Growth_pct"))
df["DivYield_pct"]    = df["Ticker"].map(lambda t: results.get(t, {}).get("DivYield_pct"))
df["PEGY"]            = df["Ticker"].map(lambda t: results.get(t, {}).get("PEGY"))

df.to_csv(OUTPUT_CSV, index=False)
print(f"\nSaved {len(df)} rows → {OUTPUT_CSV}")
print(f"PEGY populated for {df['PEGY'].notna().sum()} / {len(df)} stocks")