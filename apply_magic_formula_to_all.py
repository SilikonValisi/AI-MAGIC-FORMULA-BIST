import glob
import os
import pandas as pd
from datetime import datetime

# ── Load raw data ──────────────────────────────────────────────────────────────
# Don't assume today's date — pick the most recently *modified* raw file on
# disk. Hardcoding today's date meant this script only worked if run the same
# calendar day as bist.py's fetch; any later re-run (e.g. to try a different
# filter) would crash with FileNotFoundError since no file matches today.
#raw_files = glob.glob("bist_greenblatt_raw_*.csv")
#if not raw_files:
#    raise FileNotFoundError(
#        "No bist_greenblatt_raw_*.csv found in this directory. "
#        "Run bist.py first, or place a raw file here."
#    )
#raw_path = max(raw_files, key=os.path.getmtime)
raw_path = "bist_greenblatt_raw_20260831_midasonly.csv"
df = pd.read_csv(raw_path)
print(f"Loaded {raw_path}")
print(f"Total stocks in raw file: {len(df)}")
print(f"Groups: {df['Group'].value_counts().to_dict()}")

# ── Filters ────────────────────────────────────────────────────────────────────
df = df.dropna(subset=["EarningsYield", "RoC"])
# RoC = EBIT / capital employed is only meaningful when capital employed is
# positive — a negative EBIT over negative capital (e.g. CRFSA) divides out
# to a spuriously high positive RoC otherwise. The raw CSV doesn't carry
# capital employed directly, so back it out via EBIT_TTM / RoC and drop rows
# where it's non-positive. (Fixed at the source in bist_magic_formula_midas.py
# too, but existing raw CSVs were fetched before that fix.)
df = df[(df["EBIT_TTM"] / df["RoC"]) > 0]
#df = df[df["EarningsYield"] > 0]                                          # EBIT pozitif
#df = df[df["RoC"] > 0]                                                    # RoC pozitif
df = df[df["MarketCap_mnTL"] >= 25000]                                     # Market cap > 30 milyar TL
#df = df[df["Volume_mnUSD"].isna() | (df["Volume_mnUSD"] >= 2)]       # Volume > 2 mn$/gün
#df = df[df["FinansmanGideri"].isna() | (df["FinansmanGideri"] / df["EBIT_TTM"] < 0.80)]  # Faiz < %80 EBIT
#df = df[df["MarketCap_mnTL"] >= df["NetDebt_mnTL"]]

print(f"\nAfter filters: {len(df)} stocks")

# ── Rank ───────────────────────────────────────────────────────────────────────
df["EY_Rank"]    = df["EarningsYield"].rank(ascending=False, method="min")
df["RoC_Rank"]   = df["RoC"].rank(ascending=False, method="min")
df["Magic_Score"] = df["EY_Rank"] + df["RoC_Rank"]
df = df.sort_values("Magic_Score").reset_index(drop=True)
df.index += 1
#df = df.head(25)  # ← add this line

df["EarningsYield"] = (df["EarningsYield"]) # * 100).round(2)
df["RoC"]           = (df["RoC"] ) # * 100).round(2)

# How much of EBIT gets eaten by financing expense (interest + FX on debt) —
# not part of the Magic Formula itself (which ranks off pre-interest EBIT),
# just a supplementary leverage-risk figure to eyeball per stock.
df["FinansmanGideri_EBIT_%"] = (df["FinansmanGideri"] / df["EBIT_TTM"] * 100).round(1)

# RoC = EBIT / capital employed doesn't care whether that capital was funded
# by debt or equity, so a heavily-levered company can post the same RoC as an
# unleveraged one generating identical EBIT off a much smaller equity base —
# a good RoC rank that's really a leverage illusion rather than genuine
# capital efficiency. Capital employed isn't stored directly, so it's backed
# out the same way as the CRFSA sign-guard above (EBIT_TTM / RoC — guaranteed
# positive here since that guard already dropped non-positive cases), then
# compared against net debt. High % = the RoC is substantially debt-funded;
# negative % = the company is sitting on net cash, not net debt.
df["DebtShareOfCapital_%"] = (
    df["NetDebt_mnTL"] * 1_000_000 / (df["EBIT_TTM"] / df["RoC"]) * 100
).round(1)


def tr_number(x, decimals=0):
    """Format a number Turkish-style: '.' for thousands, ',' for decimals.
    e.g. 5319469000.0 -> '5.319.469.000'  |  12.5 -> '12,50'
    """
    if pd.isna(x):
        return ""
    s = f"{x:,.{decimals}f}"          # US style first: 5,319,469,000.00
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")  # swap , and .
    return s


# Turkish-format the big money columns so they're readable straight in the CSV
# (this turns them into text, so they'll no longer sort/filter as numbers in
# Excel/pandas — that's the tradeoff for the readable "." separators)
big_number_cols = ["EBIT_TTM", "EnterpriseValue", "MarketCap_mnTL", "NetDebt_mnTL", "FinansmanGideri"]
for col in big_number_cols:
    if col in df.columns:
        df[col] = df[col].apply(lambda x: tr_number(x, 0))

# ── Save ───────────────────────────────────────────────────────────────────────
date_str = datetime.now().strftime('%Y%m%d')
filename = f"magic_formula_all_{date_str}.csv"
df.to_csv(filename, index=True, index_label="Rank")

print(f"\nSaved → {filename}")
print(f"\nTop 30:")
display_cols = ["Ticker", "Name", "Period", "Group", "EarningsYield", "RoC", "FinansmanGideri_EBIT_%", "DebtShareOfCapital_%", "EY_Rank", "RoC_Rank", "Magic_Score"]
if "EBIT_Estimated" in df.columns:
    display_cols.append("EBIT_Estimated")
print(df[display_cols].head(30).to_string())

if "EBIT_Estimated" in df.columns:
    n_estimated = int(df["EBIT_Estimated"].head(30).sum())
    if n_estimated:
        print(
            f"\nNote: {n_estimated} of the top 30 use an *annualized estimate* of "
            f"TTM EBIT (true prior-year data was unavailable). Check the "
            f"EBIT_Estimated column before trusting their rank."
        )