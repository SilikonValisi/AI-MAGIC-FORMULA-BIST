import requests
from bs4 import BeautifulSoup
import re
import time
import warnings
warnings.filterwarnings('ignore')

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
BASE_URL = "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/Temel-Degerler-Ve-Oranlar.aspx"

# 0001 = Bankacılık | 0051 = Aracı Kurumlar | 0040 = Sigorta | 0014 = Faktoring
FINANCIAL_SECTOR_CODES = {
    "0001": "Bankacılık (Banks)",
    "0015": "gyo ",
    "0051": "Aracı Kurumlar (Brokerages)",
    "0040": "Sigorta (Insurance)",
    "0014": "Faktoring (Factoring)",
    "0049": "GYO gayri menkül ortaklıgı",
    "0055": "Varlık yönetimi",
    "0047": "Yatırım ortaklığı",
}

def fetch_tickers_via_hrefs(sector_code, sector_name):
    """Fetch tickers by finding sirket-karti links with hisse= param."""
    url = f"{BASE_URL}?sektor={sector_code}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=25, verify=False)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        tickers = []
        seen = set()
        for a in soup.find_all("a", href=True):
            m = re.search(r'hisse=([A-Z0-9]{2,6})', a["href"])
            if m:
                t = m.group(1)
                if t not in seen:
                    seen.add(t)
                    tickers.append(t)

        # Fallback: <td> cells
        if not tickers:
            for td in soup.find_all("td"):
                text = td.get_text(strip=True)
                if re.fullmatch(r'[A-Z]{2,6}[0-9]?', text) and text not in seen:
                    seen.add(text)
                    tickers.append(text)

        print(f"  [{sector_code}] {sector_name:<35} {len(tickers)} tickers")
        return set(tickers)
    except Exception as e:
        print(f"  [{sector_code}] WARNING: {e}")
        return set()

# ── Step 1: Get ALL tickers (original approach via <option> tags) ──────────
response = requests.get(BASE_URL, headers=HEADERS, timeout=15, verify=False)
soup = BeautifulSoup(response.text, "lxml")

all_tickers = set()
for option in soup.find_all("option"):
    val = option.get("value", "").strip()
    if val and len(val) <= 6 and val.isupper():
        all_tickers.add(val)

print(f"All tickers found: {len(all_tickers)}\n")

# ── Step 2: Get financial tickers using href method ────────────────────────
print("Fetching financial sector tickers to exclude...")
financial_tickers = set()
for code, name in FINANCIAL_SECTOR_CODES.items():
    financial_tickers.update(fetch_tickers_via_hrefs(code, name))
    time.sleep(0.5)

print(f"\nFinancial tickers to remove: {len(financial_tickers)}")

# ── Step 3: Subtract and save ──────────────────────────────────────────────
final_tickers = sorted(all_tickers - financial_tickers)
print(f"Final ticker count: {len(final_tickers)}")

with open("bist_tickers.txt", "w") as f:
    for t in final_tickers:
        f.write(t + "\n")

print("Saved: bist_tickers.txt")