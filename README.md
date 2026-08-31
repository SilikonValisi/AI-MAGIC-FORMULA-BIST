# BIST Magic Formula Screener

A Joel Greenblatt "Magic Formula" screener for Borsa Istanbul (BIST), adapted for
a market with structurally higher interest rates and FX-driven debt costs than
the US market the formula was originally designed for.

Ranks stocks by combined rank of:
- **Earnings Yield** = TTM EBIT ÷ Enterprise Value
- **Return on Capital** = TTM EBIT ÷ (Net Working Capital + Net Fixed Assets)

...then layers two supplementary leverage lenses on top (see below), because the
vanilla formula is blind to capital structure in a way that matters a lot more
in Turkey than it does in the US.

## Why this exists

Greenblatt's formula intentionally ignores how a company is financed — it
compares operating profitability (EBIT, before interest) against valuation and
capital efficiency. That's a reasonable simplification in a low-rate market.
It's a much bigger blind spot in Turkey, where policy rates and FX volatility
mean a company can rank as "cheap and efficient" while its financing costs are
quietly eating most or all of its operating profit. This project keeps the
original formula's ranking intact, but adds two extra columns so that risk is
visible rather than hidden.

## Data pipeline

```
bist_tickers.txt
       │
       ▼
bist_magic_formula_midas.py   ── fetches TTM EBIT, balance sheet, market cap
       │                          from Midas, reporting period / net debt /
       │                          volume from isyatirim, ranks by combined
       │                          Earnings-Yield + RoC rank
       ▼
bist_greenblatt_raw_*.csv     ── raw per-ticker data, one row per stock
       │
       ▼
apply_magic_formula_to_all.py ── applies custom filters (market cap floor,
       │                          capital-employed sign guard) and adds the
       │                          two leverage columns described below
       ▼
magic_formula_all_*.csv       ── final ranked table
       │
       ▼
magic_formula_heatmap.html    ── self-contained, no-build browser page: sortable
                                  table with color-coded leverage columns; drag
                                  a new CSV onto it each month to refresh
```

`get_greenblatt_isyatirim.py` is an earlier, İş Yatırım-only version of the
fetcher, kept for reference — the Midas-based pipeline above supersedes it and
is what the rest of this README describes. `get_stocks.py` builds the ticker
universe (BIST-listed companies, excluding banks/insurance/brokerages/factoring
— financial-sector balance sheets don't fit this screen's capital-employed
concept). `pegy_enricher.py` is a supplementary enrichment step for
growth/valuation metrics on top of the ranked output.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Full fetch (isyatirim + Midas) for every ticker in bist_tickers.txt
python bist_magic_formula_midas.py

# Refresh only the Midas-derived fields in an existing raw CSV
# (use when isyatirim is unreachable — VPN/geo-blocking)
python bist_magic_formula_midas.py --midas-only --input bist_greenblatt_raw_<date>_midasonly.csv

# Apply custom filters + leverage columns, produce the final ranked CSV
# (edit raw_path at the top of the script to point at your latest raw file)
python apply_magic_formula_to_all.py
```

Then open `magic_formula_heatmap.html` in a browser (or publish it anywhere
static) and drag the resulting `magic_formula_all_*.csv` onto it. It's a plain
HTML/CSS/JS file with no server or build step — the file you get is the file
you can host.

## The leverage columns

Two ratios beyond the core formula, both computed in `apply_magic_formula_to_all.py`:

**`FinansmanGideri_EBIT_%`** — financing expense (interest + FX cost on debt)
as a percentage of TTM EBIT. Both sides are now on the same trailing-twelve-month
basis (`ttm_from_ytd()` in `bist_magic_formula_midas.py` — Turkish interim
statements report cumulative year-to-date, so this reconstructs a true TTM the
same way EBIT itself is reconstructed). A high value means a cheap-looking
Earnings Yield is misleading once financing costs are accounted for — the
company may be keeping little to none of its operating profit. Meaningless for
negative-EBIT rows, which show `EBIT neg.` instead of a ratio.

**`DebtShareOfCapital_%`** — net debt as a percentage of capital employed
(the same capital base RoC is computed against, backed out as `EBIT_TTM / RoC`
since it isn't stored directly). RoC itself doesn't care whether that capital
was funded by debt or equity, so a company can post an excellent RoC while
most of the capital behind it is borrowed — the ratio itself is real, but the
business is far more fragile than the number alone suggests. Negative values
mean net *cash*, not net debt — the healthy end of the scale. This one stays
meaningful even when EBIT is currently negative, unlike the ratio above.

## Known data-quality guards already in place

- **Capital-employed sign guard**: RoC = EBIT ÷ capital employed only means
  something when capital employed is positive. A negative EBIT over negative
  capital divides out to a spuriously high *positive* RoC otherwise (caught
  live on CRFSA). Guarded both at the source (`bist_magic_formula_midas.py`)
  and, for already-fetched raw CSVs, by re-deriving capital employed in
  `apply_magic_formula_to_all.py`.
- **TTM, not YTD**: Turkish interim financials are cumulative within the
  fiscal year, not per-quarter — both EBIT and financing expense are
  reconstructed to true trailing-twelve-month figures rather than compared
  YTD-to-YTD, which would understate non-December reporters' numbers.

## Things worth knowing before trusting a rank

- **Liquidity isn't filtered by default** in `apply_magic_formula_to_all.py` —
  check `Volume_mnUSD` before assuming a top-ranked name is actually tradeable
  in size.
- **Holding/investment companies** (anything ending in *Holding*, *Yatırım*,
  etc.) don't fit the capital-employed concept well — their "EBIT" often
  reflects equity-method income from subsidiaries rather than one comparable
  operating business. Consider excluding them, or at least discounting an
  extreme rank driven by one.
- Both leverage columns are diagnostic overlays, not filters — nothing is
  excluded from the ranking based on them by default. That's deliberate: a
  hard cutoff on a single trailing ratio risks throwing out good businesses
  caught in a temporary trough (see: Şişecam's EBIT swinging from a 2.6bn TL
  loss in 2024 to a 4.7bn TL profit in 2025 to near-zero in H1 2026 — genuine
  cyclicality, not smooth decline).
