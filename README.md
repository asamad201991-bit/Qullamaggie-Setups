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

### 3. Set up a free Alpaca account for data (strongly recommended)

The scanner has an automatic fallback to [yfinance](https://pypi.org/project/yfinance/)
if Alpaca isn't configured, but **you should set up Alpaca** — yfinance isn't
a real API (Yahoo shut theirs down in 2017; it's scraping a website endpoint),
has undocumented rate limits that can lock you out for hours, and has known
data-quality issues (corporate-action/adjustment errors, occasional missing
days). It's there purely as a last-resort fallback so the scanner doesn't
hard-fail if you skip this step — not something to actually rely on.

Alpaca is free (a data-only account, no funding required) and gives you full
market-coverage historical data for this use case — the scanner only ever
requests daily bars *after market close*, and Alpaca's free tier serves full
SIP data (100% market coverage, same as the paid tier) for any historical
request where the timestamp is at least 15 minutes old, which every
post-close run satisfies:

1. Sign up at [alpaca.markets](https://alpaca.markets)
2. Grab your API key + secret from the dashboard
3. Copy `alpaca_config.example.json` to `alpaca_config.json` and fill in your keys
4. `alpaca_config.json` is gitignored — it will never be committed

### 4. Run it

- **Windows, no command line needed:** double-click `qullamaggie_scanner.py`
  for a daily scan, or `refresh_universe.py` once a month to rebuild the
  liquid-ticker cache (`universe_cache.json`) that the daily scan uses.
- **Command line:** `python qullamaggie_scanner.py`

Output lands in `scan_results/` next to the script: a dated CSV and an HTML
dashboard you can open in any browser. See `guide.html` for a full breakdown
of every score, badge, and table column.

### 5. Add the Pine Script indicator

1. Open the [`Qullamaggie_Setups.pine`](Qullamaggie_Setups.pine) file on GitHub and copy its full contents
2. In TradingView, open **Pine Editor** (bottom toolbar of any chart)
3. Paste the code in, then click **Add to Chart**

That's it — it computes the same 0–100 scores as the Python scanner directly
on the chart, with visual setup boxes and a status table. No TradingView
publish/subscription needed; this works on a free TradingView account.

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

## Data sources & limitations

Worth understanding what each stage actually does before relying on this:

- **Stage 1 (the Python scanner) is an end-of-day batch scan, not a live
  system.** It runs after market close and asks "what set up over the last
  few days across ~1,200 tickers" — a daily-close question, not a live one.
  By the time it's scoring yesterday's daily bar, that bar is already final;
  there's no real-time step anywhere in that pipeline. This is what makes
  Alpaca's free tier a good fit here (see below) and is also why this stage
  should never be treated as a live trading signal — it's a candidate
  finder, nothing more.
- **Stage 2 (the TradingView Pine indicator) is where live data actually
  matters**, and it's running on TradingView's own live feed the whole
  time — that's where you're watching a candidate confirm (or not) in real
  time before acting. Stage 1 tells you where to look; Stage 2 is where you
  actually watch it happen.
- **Alpaca (free tier) is the recommended data source for Stage 1.**
  Alpaca's free-tier restriction only applies to live/real-time streaming
  (IEX-only, ~2% of market volume) — for historical requests where the
  timestamp is at least 15 minutes old, which every post-close scan
  satisfies by definition, Alpaca serves full SIP data (100% market
  coverage, same as its paid tier). So the free tier is genuinely
  sufficient for what this tool does.
- **yfinance is a last-resort fallback only, not a real option.** It isn't
  an official API — Yahoo shut theirs down in 2017, this is scraping a
  website endpoint — and it comes with undocumented, IP-based rate limits
  that can lock you out for hours (sometimes longer), plus known
  data-quality issues (corporate-action/adjustment errors, occasional
  missing days). It exists so the scanner doesn't hard-fail if you skip
  Alpaca setup, not as something to actually depend on. Set up Alpaca.

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
