# Qullamaggie Setups Scanner

A two-stage momentum screening system implementing setups described by Kristjan
"Qullamaggie" Kullamägi's publicly shared trading methodology — **Breakout**,
**Episodic Pivot**, **Parabolic Short**, and **Parabolic Long** — built for
personal swing-trading research.

- **Stage 1 — Python scanner** (`qullamaggie_scanner.py`): scans a
  liquidity-filtered universe of ~1,200 US equities, scores every candidate
  0–100 per setup, and outputs a CSV plus a self-contained HTML dashboard
  (with a built-in position-sizing calculator and a "Recently Triggered"
  table that keeps SETUP!-tier hits visible for a few days after they fire).
- **Stage 2 — TradingView Pine Script indicator** (`Qullamaggie_Setups.pine`):
  chart-level confirmation with visual boxes (consolidation tightness,
  60-bar linearity, parabolic extension), a per-setup breakdown table, and
  the same 0–100 scoring as Stage 1 — so a candidate the scanner finds can be
  visually confirmed on the actual chart.

## Quick start

*No command line, no Git knowledge needed — everything below is clicking buttons.*

### 1. Download the project

1. On this GitHub page, click the green **Code** button (top right of the file list) → **Download ZIP**
2. Find the downloaded ZIP file (usually in your Downloads folder), right-click it → **Extract All**
3. Open the extracted folder — you should see `qullamaggie_scanner.py`, `Qullamaggie_Setups.pine`, `setup_windows.bat`, and the rest of the files

*(Comfortable with Git already? `git clone https://github.com/axidzz/Qullamaggie-Setups.git` works exactly the same way — this is just the version that doesn't require it.)*

### 2. Install the required Python packages

Double-click **`setup_windows.bat`** inside that folder. It installs everything the scanner needs automatically — a black window will open, show some installation text, then say "All set!" when done. If you don't already have Python installed, get it free from [python.org/downloads](https://www.python.org/downloads/) first (the installer has a checkbox for "Add python.exe to PATH" — make sure that's ticked).

### 3. (Recommended) Set up a free Alpaca account for data

The scanner works out of the box with [yfinance](https://pypi.org/project/yfinance/)
(no signup needed), but [Alpaca](https://alpaca.markets)'s data is faster and
more reliable for scanning a 1,200-ticker universe every day. It's free — a
data-only account, no funding required:

1. Sign up at [alpaca.markets](https://alpaca.markets)
2. Grab your API key + secret from the dashboard
3. Copy `alpaca_config.example.json` to `alpaca_config.json` and fill in your keys
4. `alpaca_config.json` is gitignored — it will never be committed, and the
   scanner automatically falls back to yfinance if it's missing

### 4. Run it

- **Windows, no command line needed:** double-click `qullamaggie_scanner.py`
  for a daily scan, or `refresh_universe.py` once a month to rebuild the
  liquid-ticker cache (`universe_cache.json`) that the daily scan uses.
- **Command line:** `python qullamaggie_scanner.py`

Output lands in `scan_results/` next to the script: a dated CSV and an HTML
dashboard you can open in any browser. See `guide.html` for a full breakdown
of every score, badge, and table column.

### 5. Add the Pine Script indicator

Open `Qullamaggie_Setups.pine` in TradingView's Pine Editor (or use the
published version — link once live: `<your published script URL>`), add it
to any chart, and it computes the same 0–100 scores directly on the chart
with visual setup boxes and a status table.

## Repo contents

| File | What it is |
|---|---|
| `qullamaggie_scanner.py` | Stage 1 — the daily scanner |
| `setup_windows.bat` | Double-click to install Python dependencies — no command line needed |
| `refresh_universe.py` | Rebuilds the liquid-ticker cache (run monthly) |
| `Qullamaggie_Setups.pine` | Stage 2 — the TradingView indicator |
| `guide.html` | Full reference guide — every score, badge, and column explained |
| `alpaca_config.example.json` | Template for your own `alpaca_config.json` |
| `requirements.txt` | Python dependencies |

## Methodology

This tool is a personal implementation of setups and rules Kristjan
"Qullamaggie" Kullamägi has described in his publicly available blog posts
and interviews. **It is not affiliated with or endorsed by him.** Scoring
weights and thresholds are the author's own interpretation and adaptation of
his stated rules, cross-checked against his own words where possible — not
an official or guaranteed reproduction of his process.

## Disclaimer

This is a screening/research tool, not financial advice. It does not
execute trades, does not guarantee any result, and past patterns are not
predictive of future performance. Use at your own risk and do your own due
diligence before trading anything it surfaces.

## Support this project

If this tool is useful to you, consider
[buying me a coffee on Ko-fi](https://ko-fi.com/axidzz) —
totally optional, but appreciated. (Also available as the "Sponsor" button
at the top of this repo.)

## License

[MIT](LICENSE) — free to use, modify, and redistribute.
