#!/usr/bin/env python3
"""
Qullamaggie Stage-1 Scanner
===========================
A nightly/daily universe screener for the three "Qullamaggie" setups:
  1. Breakout          - big prior move + tight consolidation + volume breakout
  2. Episodic Pivot     - gap up 10%+ on a volume surge, not already extended
  3. Parabolic Short    - extreme short-term move + consecutive up days + stretch from MA
     (Parabolic Long is the mirror: a recent "crash" followed by a green reversal bar)

Data source: yfinance (free, no API key). Universe: NASDAQ + NYSE + AMEX common
stocks, pulled fresh from Nasdaq Trader's public symbol directory.

This is Stage 1 of a two-stage workflow:
  Stage 1 (this script): scan the whole market, produce a short watchlist.
  Stage 2 (TradingView):  load that watchlist and apply the companion Pine
                           script for chart-level confirmation + alerts.

USAGE
-----
    pip install yfinance pandas numpy requests --break-system-packages
    python qullamaggie_scanner.py
    python qullamaggie_scanner.py --limit 800 --workers 12 --out ./scan_results
    python qullamaggie_scanner.py --tickers-file my_universe.txt   # use your own list

See README_scanner.md for full details and customization options.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import logging
import random
import re
import sys
import time
import traceback
import webbrowser
from pathlib import Path

import numpy as np
import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    print("Missing dependency 'yfinance'. Attempting to install it automatically...")
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", "yfinance"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "--disable-pip-version-check", "--break-system-packages", "yfinance"],
                capture_output=True, text=True, timeout=120,
            )
        if result.returncode == 0:
            import yfinance as yf
            print("Installed successfully - continuing.")
        else:
            raise RuntimeError(result.stderr.strip()[-300:])
    except Exception as e:
        print(f"Auto-install failed ({e}).")
        print("Run this yourself once: pip install yfinance pandas numpy requests --break-system-packages")
        sys.exit(1)

# yfinance logs a "possibly delisted; no price data found" line per failed
# ticker straight to the console. Across a 500+ ticker universe that's a wall
# of noise for something already handled gracefully in download_batch() below
# (missing tickers are just skipped). Quiet it down to errors only.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# =============================================================================
# CONFIG / DEFAULTS  (mirrors the Pine indicator's inputs so results line up)
# =============================================================================
NASDAQ_LISTED_URL = "https://ftp.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://ftp.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Cached, liquidity-pre-filtered universe (built by refresh_universe.py / the
# --refresh-universe flag). Lives next to this script so it's shared across
# whatever --out folder you're writing results to, and persists between runs.
UNIVERSE_CACHE_PATH = Path(__file__).resolve().parent / "universe_cache.json"

# Optional Alpaca API credentials. If this file exists and is valid, Alpaca is
# used automatically instead of yfinance (see fetch_universe/download_universe
# below) - officially documented rate limits and real bulk multi-symbol
# endpoints, versus yfinance's unofficial/undocumented throttling. Falls back
# to yfinance transparently if this isn't set up or alpaca-py isn't installed.
ALPACA_CONFIG_PATH = Path(__file__).resolve().parent / "alpaca_config.json"

DEFAULTS = dict(
    # --- universe / liquidity ---
    history_period="1y",
    min_price=2.0,
    min_avg_dollar_vol=5_000_000,
    min_adr_pct=3.0,
    adr_len=20,
    vol_avg_len=20,
    # --- breakout ---
    bo_move_lookback=60,
    bo_min_move_pct=30.0,
    bo_consol_len=15,
    bo_max_range_pct=15.0,   # was 35 - a dedicated Qullamaggie-methodology script uses <10% VCP
                              # tightness (over a 5-day window); adjusted to 15% since our window
                              # is 15 days (longer windows accumulate more range even in a genuine
                              # orderly base, so a direct 10% copy would likely be too strict)
    bo_vol_expansion=1.5,
    bo_require_above_sma50=True,  # Kris's own stated hard rule: "never buys below 50MA"
    bo_trend_slope_lookback=5,  # bars used to confirm EMA10 is rising (not just "above"),
                                  # for the Trend Position scoring bucket - matches Minervini's
                                  # slope-confirmation approach, which Kris's own methodology draws on
    # --- episodic pivot ---
    ep_min_gap_pct=10.0,
    ep_min_vol_mult=3.0,
    ep_no_run_lookback=120,
    ep_max_prior_run_pct=20.0,  # was 40 - sources describe the ideal pre-EP base as "flat for
                                  # 3-6 months prior"; no exact % was ever cited, but 40% clearly
                                  # doesn't qualify as "flat" by any reasonable reading
    # --- parabolic short/long ---
    pb_lookback=10,
    pb_min_move_pct=50.0,
    pb_min_streak=4,
    pb_ma_len=20,
    pb_min_stretch_pct=30.0,
    pb_long_reversal_window=5,
    # --- signal recency ---
    signal_lookback_days=1,   # flag as a hit if the setup fired in the last N bars
    # --- score-based candidate threshold ---
    min_score_to_report=60,   # "WATCH" tier and above make it into the CSV/dashboard (raised from
                               # 40/Monitor - that tier let too many generically-healthy trending
                               # stocks through without requiring real breakout-specific strength)
    # --- universe cache (see --refresh-universe) ---
    universe_pool_size=0,   # 0 = no cap, rank the ENTIRE available universe (see build_liquid_universe)
    universe_keep_size=1200,   # how many of the most liquid ones to keep in the cache
    universe_cache_warn_days=30,  # nag (not block) if the cache is older than this
    # --- dashboard "Recently Triggered" panel ---
    recent_triggers_lookback_days=5,   # how many days a hit stays on the dashboard's "Recently
                                        # Triggered" table after its score decays back down (e.g.
                                        # an Episodic Pivot the day after its gap). Purely a
                                        # display/logging window - does not affect scoring.
    recent_triggers_min_score=80,      # only SETUP!-caliber hits (>=80, matching the badge tier
                                        # boundary) get tracked here - WATCH/Monitor tier is
                                        # deliberately excluded.
)

# Badge tiers for the 0-100 scoring system (same for all three setups)
BADGE_TIERS = [
    (80, "SETUP!"),
    (60, "WATCH"),
    (40, "Monitor"),
    (0, "-"),
]


def score_to_badge(score: float | None) -> str:
    if score is None:
        return "-"
    for threshold, badge in BADGE_TIERS:
        if score >= threshold:
            return badge
    return "-"


def tier_score_ge(value, tiers: list[tuple[float, int]]) -> int:
    """Highest-tier points for value >= threshold (thresholds sorted descending)."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return 0
    for threshold, points in tiers:
        if value >= threshold:
            return points
    return 0


def tier_score_le(value, tiers: list[tuple[float, int]]) -> int:
    """Highest-tier points for value <= threshold (thresholds sorted ascending)."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return 0
    for threshold, points in tiers:
        if value <= threshold:
            return points
    return 0


# =============================================================================
# UNIVERSE
# =============================================================================
def _fetch_url_with_retries(url: str, attempts: int = 3, timeout: int = 30,
                             headers: dict | None = None) -> str | None:
    """Try a URL a few times, then fall back to plain HTTP in case something
    (corporate firewall, antivirus TLS inspection) is specifically blocking
    the HTTPS connection to this host but not HTTP.
    """
    last_err = None
    for scheme_url in [url, url.replace("https://", "http://")]:
        for attempt in range(attempts):
            try:
                resp = requests.get(scheme_url, timeout=timeout, headers=headers)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
    print(f"[WARN] All attempts to reach {url} failed. Last error: {last_err}")
    return None


def _fetch_nasdaq_trader() -> list[str] | None:
    """Primary source: Nasdaq Trader's symbol directory. Cleanly flags ETFs
    and test issues, but the host is sometimes blocked by ISPs/firewalls/AV.
    """
    tickers: list[str] = []
    for url, is_nasdaq in [(NASDAQ_LISTED_URL, True), (OTHER_LISTED_URL, False)]:
        text = _fetch_url_with_retries(url, attempts=2, timeout=15)
        if text is None:
            return None
        lines = text.strip().split("\n")
        header = lines[0].split("|")
        rows = [l.split("|") for l in lines[1:-1]]  # last line is a file-creation footer
        df = pd.DataFrame(rows, columns=header)

        if is_nasdaq:
            df = df[df["Test Issue"] == "N"]
            df = df[df["ETF"] == "N"]
            sym_col = "Symbol"
        else:
            df = df[df["Test Issue"] == "N"]
            df = df[df["ETF"] == "N"]
            sym_col = "ACT Symbol"

        syms = df[sym_col].astype(str)
        syms = syms[~syms.str.contains(r"[\.\$\+=]", regex=True)]
        syms = syms[syms.str.len() <= 5]
        tickers.extend(syms.tolist())
    return tickers


def _fetch_sec_tickers() -> list[str] | None:
    """Fallback source: SEC's official ticker list (sec.gov is rarely blocked,
    even on networks/ISPs that filter ftp.nasdaqtrader.com). Doesn't flag ETFs
    separately, so this universe is noisier than Nasdaq Trader's, but we clean
    up the most obvious junk here (warrants/units/rights/bankruptcy suffixes,
    per Nasdaq's documented 5th-character symbol convention) since a lot of it
    is OTC/foreign-listed and Yahoo has no data for it anyway.
    """
    headers = {"User-Agent": "QullamaggieScanner research-tool contact@example.com"}
    text = _fetch_url_with_retries(SEC_TICKERS_URL, attempts=2, timeout=15, headers=headers)
    if text is None:
        return None
    try:
        import json
        data = json.loads(text)
        raw = [row["ticker"] for row in data.values() if "ticker" in row]
        # Nasdaq 5th-character suffix meanings that reliably aren't a normal
        # common-stock listing: Q=bankruptcy, R=rights, U=units, W=warrants,
        # V=when-issued, X=mutual fund. Safe to drop; keeps common stock clean.
        junk_suffixes = {"Q", "R", "U", "W", "V", "X"}
        cleaned = [
            t for t in raw
            if not (len(t) == 5 and t[-1] in junk_suffixes)
        ]
        return cleaned
    except Exception as e:
        print(f"[WARN] SEC ticker list came back but couldn't be parsed: {e}")
        return None


# =============================================================================
# DATA PROVIDER: ALPACA (optional, preferred if configured)
# =============================================================================
# NOTE ON TESTING: this integration is written against alpaca-py's documented
# request/response shapes but has NOT been exercised against Alpaca's live API
# (no network access in the environment this was built in). Treat the first
# real run the way we treated yfinance early on - likely to need a small
# correction or two once actual API responses are in hand. If something
# breaks, share the traceback and it'll get fixed fast, same as before.

_alpaca_clients_cache = None
_alpaca_unavailable = False


def load_alpaca_config() -> dict | None:
    """Reads alpaca_config.json next to this script. Expected shape:
    {"api_key": "...", "secret_key": "..."}
    Get free keys at https://alpaca.markets (paper/data-only account, no
    funding required) - see README for the exact signup steps.
    """
    if not ALPACA_CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(ALPACA_CONFIG_PATH.read_text(encoding="utf-8"))
        if cfg.get("api_key") and cfg.get("secret_key"):
            return cfg
        print(f"[WARN] {ALPACA_CONFIG_PATH.name} exists but is missing api_key/secret_key.")
    except Exception as e:
        print(f"[WARN] {ALPACA_CONFIG_PATH.name} exists but couldn't be read ({e}).")
    return None


def get_alpaca_clients():
    """Returns (trading_client, data_client) if Alpaca is configured and the
    alpaca-py package is installed, else None. Cached after the first call
    (including a cached "unavailable" result) so we don't re-check every batch.
    """
    global _alpaca_clients_cache, _alpaca_unavailable
    if _alpaca_unavailable:
        return None
    if _alpaca_clients_cache is not None:
        return _alpaca_clients_cache

    cfg = load_alpaca_config()
    if not cfg:
        _alpaca_unavailable = True
        return None
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
    except ImportError:
        # The double-click workflow never runs the .bat/.command launcher's
        # pip-install step, so a missing package just silently falls back to
        # yfinance until someone notices and installs it manually. Try to
        # install it ourselves instead - same self-healing behavior a
        # launcher would give, without requiring one.
        print(f"[WARN] alpaca_config.json found but the 'alpaca-py' package isn't installed.")
        print("       Attempting to install it automatically...")
        try:
            import subprocess
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "--disable-pip-version-check", "alpaca-py"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                # externally-managed-environment systems need this flag; retry once with it
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--quiet",
                     "--disable-pip-version-check", "--break-system-packages", "alpaca-py"],
                    capture_output=True, text=True, timeout=120,
                )
            if result.returncode == 0:
                from alpaca.trading.client import TradingClient
                from alpaca.data.historical import StockHistoricalDataClient
                print("       Installed successfully - continuing with Alpaca.")
            else:
                raise RuntimeError(result.stderr.strip()[-300:])
        except Exception as install_err:
            print(f"       Auto-install failed ({install_err}).")
            print("       Run this yourself once: pip install alpaca-py --break-system-packages")
            print("       Falling back to yfinance for now.")
            _alpaca_unavailable = True
            return None
    try:
        trading_client = TradingClient(cfg["api_key"], cfg["secret_key"], paper=True)
        data_client = StockHistoricalDataClient(cfg["api_key"], cfg["secret_key"])
        _alpaca_clients_cache = (trading_client, data_client)
        print("Using Alpaca for data (found valid alpaca_config.json).")
        return _alpaca_clients_cache
    except Exception as e:
        print(f"[WARN] Could not initialize Alpaca clients ({e}). Falling back to yfinance.")
        _alpaca_unavailable = True
        return None


def fetch_universe_alpaca(trading_client, limit: int | None = None) -> list[str]:
    """Alpaca's asset list is broker-curated (things you can actually trade),
    which is a cleaner starting point than SEC's filing-based list (which
    includes tons of OTC/inactive/foreign entities Yahoo has no data for).
    """
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.trading.enums import AssetClass, AssetStatus

    req = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets = trading_client.get_all_assets(req)
    tickers = [a.symbol for a in assets if getattr(a, "tradable", True)]

    # Same suffix cleanup as the SEC path, for consistency (drop obvious
    # warrant/unit/rights/bankruptcy-suffix symbols).
    junk_suffixes = {"Q", "R", "U", "W", "V", "X"}
    tickers = [t for t in tickers if not (len(t) == 5 and t[-1] in junk_suffixes)]
    # NOTE: deliberately NOT doing the Yahoo-style .replace(".", "-") here that
    # the SEC path does - these symbols come straight from Alpaca's own asset
    # list, already in Alpaca's expected format. Converting them to Yahoo's
    # convention (e.g. turning a correctly-formatted class-share symbol into
    # one with a hyphen) actively breaks Alpaca's own bars API for exactly
    # those symbols - this was a real bug (see the invalid-symbol handling in
    # download_universe_alpaca for the belt-and-suspenders fix on top of this).
    tickers = sorted(set(t for t in tickers if t and t.isascii()))

    if limit and limit < len(tickers):
        rng = random.Random(int(dt.date.today().strftime("%Y%m%d")))
        tickers = rng.sample(tickers, limit)
        tickers = sorted(tickers)
    return tickers


def download_universe_alpaca(data_client, tickers: list[str],
                              chunk_size: int = 200) -> dict[str, pd.DataFrame]:
    """Fetches ~1y of daily bars for many tickers via Alpaca's real bulk
    multi-symbol endpoint (a handful of chunked calls instead of yfinance's
    many small batches), converting the result to the same per-ticker
    Open/High/Low/Close/Volume DataFrame shape the rest of the pipeline
    already expects - a drop-in replacement, nothing downstream needs to change.

    IMPORTANT: explicitly requests feed=SIP (consolidated volume across ALL
    exchanges), not the default IEX-only feed. IEX represents only ~2% of
    total US market volume, so IEX-only data would badly understate real
    dollar volume for every ticker - which would break our liquidity filter
    outright (everything would look far less liquid than it really is). SIP
    is free on the Basic plan for delayed (15-min) data, which costs us
    nothing here since this is EOD/daily-bar analysis, not live trading.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment

    # NOT passing an explicit `end` date: confirmed via direct testing that
    # Alpaca's API returns data through TODAY when end is omitted, but
    # passing a bare date() object as `end` was truncating ~3 days of
    # legitimately-available data (likely interpreted as "up to the START
    # of that day" rather than through its close). Omitting it entirely
    # also does NOT trigger the earlier "recent SIP data" rejection - that
    # was specific to explicitly requesting a too-recent end date, not to
    # requesting current data in general.
    start = dt.date.today() - dt.timedelta(days=400)  # buffer for a full year + weekends/holidays
    print(f"  Alpaca request range: {start} to most-recent-available (today is {dt.date.today()})")

    def _extract_df(bars) -> dict[str, pd.DataFrame]:
        out = {}
        df = bars.df
        if df is None or df.empty:
            return out
        for symbol in df.index.get_level_values(0).unique():
            sub = df.xs(symbol, level=0).copy()
            sub = sub.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                       "close": "Close", "volume": "Volume"})
            if len(sub) > 30 and {"Open", "High", "Low", "Close", "Volume"}.issubset(sub.columns):
                out[symbol] = sub[["Open", "High", "Low", "Close", "Volume"]]
        return out

    def _fetch_chunk(symbols: list[str], feed, max_exclusions: int = 20) -> dict[str, pd.DataFrame]:
        """Fetch a chunk, and if Alpaca rejects it over one or two bad
        symbols, strip exactly those out and retry - rather than losing the
        entire chunk (which is what silently ate ~400 tickers before this fix:
        one bad symbol like a hyphenated share class was tanking all 200
        tickers in its chunk).
        """
        remaining = list(symbols)
        excluded = []
        for _ in range(max_exclusions):
            if not remaining:
                return {}
            try:
                req = StockBarsRequest(symbol_or_symbols=remaining, timeframe=TimeFrame.Day,
                                        start=start, feed=feed, adjustment=Adjustment.ALL)
                bars = data_client.get_stock_bars(req)
                if excluded:
                    print(f"[INFO] Excluded {len(excluded)} invalid symbol(s) from this chunk: "
                          f"{', '.join(excluded)}")
                return _extract_df(bars)
            except Exception as e:
                msg = str(e)
                m = re.search(r"invalid symbol:\s*([A-Za-z0-9.\-]+)", msg, re.IGNORECASE)
                if m and m.group(1) in remaining:
                    bad = m.group(1)
                    remaining.remove(bad)
                    excluded.append(bad)
                    continue  # retry without the bad symbol
                raise  # some other error - let the caller's except handle it
        print(f"[WARN] Gave up after excluding {max_exclusions} invalid symbols from one chunk.")
        return {}

    results: dict[str, pd.DataFrame] = {}
    chunks = [tickers[i:i + chunk_size] for i in range(0, len(tickers), chunk_size)]
    for i, chunk in enumerate(chunks):
        try:
            results.update(_fetch_chunk(chunk, DataFeed.SIP))
        except Exception as e:
            if "feed" in str(e).lower() or "subscription" in str(e).lower():
                print(f"[WARN] Alpaca chunk {i + 1}/{len(chunks)}: SIP feed request failed "
                      f"({e}). Your plan may not include SIP access - check your Alpaca "
                      f"dashboard. Falling back to IEX for this chunk (less accurate volume).")
                try:
                    results.update(_fetch_chunk(chunk, DataFeed.IEX))
                except Exception as e2:
                    print(f"[WARN] IEX fallback also failed for chunk {i + 1}: {e2}")
            else:
                print(f"[WARN] Alpaca bars chunk {i + 1}/{len(chunks)} failed: {e}")
        print(f"\r  Alpaca: downloaded {i + 1}/{len(chunks)} chunks "
              f"({len(results)} tickers with data)", end="", flush=True)
    print()
    if results:
        sample_ticker = next(iter(results))
        actual_last_date = results[sample_ticker].index[-1]
        print(f"  Alpaca actually returned data through: {actual_last_date} (sample: {sample_ticker})")
        if hasattr(actual_last_date, "date") and actual_last_date.date() < dt.date.today() - dt.timedelta(days=3):
            print(f"  [WARN] Most recent data is more than 3 days old - worth checking your")
            print(f"  Alpaca account/data subscription status if this persists.")
    return results


def fetch_universe(limit: int | None = None, source_pref: str = "auto") -> list[str]:
    """Download a current US common-stock universe. Tries, in order:
      0. Alpaca (if alpaca_config.json is set up - broker-curated, cleanest)
      1. Nasdaq Trader (cleanest data, flags ETFs)
      2. SEC company_tickers.json (very reliably reachable, less clean)
      3. Built-in static list of ~150 liquid names (always works, offline)

    source_pref: "auto" (default, try in order), "sec" (skip straight to SEC -
    useful once you know Nasdaq Trader is blocked on your network and don't
    want to wait through retries every run), or "fallback" (skip straight to
    the built-in list, e.g. if you're offline). Alpaca (if configured) is
    always tried first regardless of source_pref, since it's not part of the
    Nasdaq/SEC chain source_pref controls.
    """
    alpaca = get_alpaca_clients()
    if alpaca:
        trading_client, _ = alpaca
        try:
            tickers = fetch_universe_alpaca(trading_client, limit=limit)
            print(f"Universe source: Alpaca ({len(tickers)} tradable US equities)")
            return tickers
        except Exception as e:
            print(f"[WARN] Alpaca universe fetch failed ({e}). Falling back to Nasdaq/SEC...")

    tickers = None
    source = None

    if source_pref == "auto":
        tickers = _fetch_nasdaq_trader()
        source = "Nasdaq Trader"
        if tickers is None:
            print("[WARN] Nasdaq Trader unreachable (likely blocked by your ISP/firewall/AV "
                  "for this specific host). Trying SEC's ticker list instead...")
            tickers = _fetch_sec_tickers()
            source = "SEC"
    elif source_pref == "sec":
        tickers = _fetch_sec_tickers()
        source = "SEC"
    elif source_pref == "fallback":
        tickers = None  # falls through to the built-in list below
        source = None

    if tickers is None:
        if source_pref != "fallback":
            print("[WARN] Live universe sources unreachable. Your network appears to be blocking")
            print("       or heavily filtering outbound connections to financial-data hosts.")
        print("       Using the built-in list of ~{} liquid names.".format(len(FALLBACK_UNIVERSE)))
        print("       You can also scan your own list any time with:")
        print("       --tickers-file your_list.txt (one ticker per line).")
        tickers = FALLBACK_UNIVERSE.copy()
        source = "built-in fallback"

    # BUGFIX: list(set(...)) has a non-obvious problem - Python randomizes
    # string hash seeds per-process, so set() iteration order for the same
    # tickers differs on every single run of the script. That broke the
    # "date-seeded random sample = reproducible same-day results" logic below:
    # random.sample()'s output depends on both the seed AND the input order,
    # so two runs minutes apart could draw a completely different sample even
    # with an identical seed, just because set() scrambled the order
    # differently each time. Sorting first makes the order (and therefore the
    # sample) fully deterministic across runs on the same day.
    tickers = sorted(set(t.replace(".", "-") for t in tickers if t and t.isascii()))
    if limit and limit < len(tickers):
        # IMPORTANT: sample randomly, don't just sort-then-slice - alphabetical
        # truncation would only ever scan tickers starting with A/B/C... which
        # is a real bug this had before (a `--limit` below the full universe
        # size silently meant "only stocks near the start of the alphabet").
        # Seed by today's date so re-running the same day is reproducible, but
        # different days sample a different slice, gradually covering more of
        # the market over repeated runs rather than always seeing the same names.
        rng = random.Random(int(dt.date.today().strftime("%Y%m%d")))
        tickers = rng.sample(tickers, limit)
    tickers = sorted(tickers)
    print(f"Universe source: {source} ({len(tickers)} tickers before per-ticker liquidity filtering)")
    return tickers


# Small fallback list used only if the live universe download fails (e.g. no network).
FALLBACK_UNIVERSE = [
    # mega/large-cap tech & growth
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "CRM", "ADBE", "COST", "PEP", "LIN", "TMO", "ABBV", "MRK", "ORCL", "CSCO",
    "SHOP", "PLTR", "SMCI", "MSTR", "COIN", "ARM", "DELL", "NOW", "PANW", "ANET",
    "UBER", "ABNB", "SNOW", "DDOG", "NET", "CRWD", "ZS", "MDB", "TEAM", "WDAY",
    "INTU", "ADSK", "ADP", "PYPL", "SQ", "SOFI", "AFRM", "UPST", "HOOD", "RBLX",
    # semis & hardware
    "QCOM", "TXN", "MU", "ON", "MRVL", "LRCX", "AMAT", "KLAC", "ASML", "TSM",
    # EV / auto / industrials momentum names
    "RIVN", "LCID", "NIO", "XPEV", "LI", "F", "GM", "CAT", "DE", "BA",
    # biotech / healthcare momentum
    "MRNA", "BNTX", "VRTX", "REGN", "ISRG", "DXCM", "ALGN", "EXAS", "CRSP", "NVAX",
    # financials
    "JPM", "GS", "MS", "BAC", "WFC", "SCHW", "AXP", "V", "MA", "BLK",
    # consumer / retail
    "WMT", "TGT", "HD", "LOW", "NKE", "SBUX", "MCD", "CMG", "LULU", "DECK",
    # media / communications
    "DIS", "CMCSA", "T", "VZ", "TMUS", "SPOT", "ROKU", "WBD", "PARA", "SNAP",
    # energy
    "XOM", "CVX", "OXY", "SLB", "COP", "DVN", "FANG", "MPC", "PSX", "VLO",
    # crypto/fintech adjacent momentum
    "MARA", "RIOT", "CLSK", "CLF", "BITF", "IREN", "APLD", "CIFR", "WULF", "BTBT",
    # small/mid-cap momentum staples that show up often in breakout/EP screens
    "CVNA", "DKNG", "CELH", "AXON", "TTD", "APP", "DUOL", "TOST", "IOT", "GTLB",
    "FTNT", "ALAB", "VRT", "NBIS", "ASTS", "RKLB", "IONQ", "QUBT", "SOUN", "BBAI",
]


# =============================================================================
# DATA DOWNLOAD
# =============================================================================
def download_batch(tickers: list[str], period: str, retries: int = 3) -> dict[str, pd.DataFrame]:
    """Download OHLCV history for a batch of tickers in one yfinance call."""
    RATE_LIMIT_MARKERS = ("429", "too many requests", "rate limit", "rate-limit")

    for attempt in range(retries + 1):
        try:
            raw = yf.download(
                tickers=tickers,
                period=period,
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
            )
            break
        except Exception as e:
            if attempt == retries:
                print(f"[WARN] batch download failed permanently after {retries + 1} attempts: {e}")
                return {}
            is_rate_limited = any(marker in str(e).lower() for marker in RATE_LIMIT_MARKERS)
            if is_rate_limited:
                # Yahoo's throttling needs real cooldown time - a few seconds does
                # nothing. This is the fix for large scans (e.g. --refresh-universe
                # against thousands of tickers) silently losing most of their data:
                # the old short backoff gave up long before Yahoo's limit reset.
                wait = 30 * (attempt + 1)
                print(f"[WARN] Rate-limited by Yahoo, waiting {wait}s before retry "
                      f"{attempt + 1}/{retries}...")
            else:
                wait = 2 * (attempt + 1)
            time.sleep(wait)

    out: dict[str, pd.DataFrame] = {}
    if len(tickers) == 1:
        t = tickers[0]
        df = raw.dropna(how="all")
        if isinstance(df.columns, pd.MultiIndex):
            # some yfinance versions still return (ticker, field) MultiIndex
            # even for a single-ticker batch when group_by='ticker' is set
            try:
                df = df[t]
            except KeyError:
                df.columns = df.columns.get_level_values(-1)
        if not df.empty:
            out[t] = df
        return out

    for t in tickers:
        try:
            df = raw[t].dropna(how="all")
            if not df.empty and len(df) > 30:
                out[t] = df
        except (KeyError, TypeError):
            continue
    return out


def download_universe(tickers: list[str], period: str, batch_size: int = 50,
                       workers: int = 8, pause: float = 0.5) -> dict[str, pd.DataFrame]:
    alpaca = get_alpaca_clients()
    if alpaca:
        _, data_client = alpaca
        try:
            return download_universe_alpaca(data_client, tickers)
        except Exception as e:
            print(f"[WARN] Alpaca bars fetch failed ({e}). Falling back to yfinance...")

    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]
    results: dict[str, pd.DataFrame] = {}

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(download_batch, b, period): b for b in batches}
        done = 0
        for fut in cf.as_completed(futures):
            done += 1
            try:
                results.update(fut.result())
            except Exception:
                traceback.print_exc()
            print(f"\r  downloaded {done}/{len(batches)} batches "
                  f"({len(results)} tickers with data)", end="", flush=True)
            time.sleep(pause)
    print()
    return results


# =============================================================================
# LIQUID UNIVERSE CACHE
# A persistent, liquidity-pre-filtered ticker list, built occasionally (see
# --refresh-universe / refresh_universe.py) rather than re-sampled randomly
# every run. This mirrors how real screeners actually work - Kris himself
# scans his full TC2000 universe every day, and the most detailed public
# DIY replication of this method pre-filters to ~1,500 liquid names once a
# month and scans that same full set daily, rather than randomly resampling.
# =============================================================================
def save_universe_cache(tickers: list[str], pool_scanned: int) -> None:
    payload = {
        "generated": dt.date.today().isoformat(),
        "pool_scanned": pool_scanned,
        "kept": len(tickers),
        "tickers": sorted(tickers),
    }
    UNIVERSE_CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_universe_cache() -> dict | None:
    if not UNIVERSE_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(UNIVERSE_CACHE_PATH.read_text(encoding="utf-8"))
        data["age_days"] = (dt.date.today() - dt.date.fromisoformat(data["generated"])).days
        return data
    except Exception as e:
        print(f"[WARN] Universe cache exists but couldn't be read ({e}). Treating as missing.")
        return None


def build_liquid_universe(cfg: dict, source_pref: str, pool_size: int, keep_size: int,
                           workers: int = 8, batch_size: int = 50,
                           trace_tickers: tuple[str, ...] = ()) -> tuple[list[str], int]:
    """The slow part, meant to be run occasionally (not every day): pull a
    raw pool of tickers, download price history for all of them, rank by
    real liquidity (ADR% + true daily-dollar-volume, not a shortcut), and
    keep only the most liquid `keep_size`. This is what gets cached.

    IMPORTANT: pool_size <= 0 means "no cap - rank the ENTIRE available
    universe." This should be the default. Randomly sampling a capped pool
    before ranking is the wrong approach here: the full universe is dominated
    by thousands of illiquid/OTC/shell tickers, so a random sub-sample can
    easily miss genuinely liquid megacaps (even something like LLY) purely by
    chance, which defeats the point of a "most liquid" list. Since this is a
    slow, infrequent, one-time refresh (not a daily operation), there's no
    good reason to cap it - completeness matters more than speed here.

    trace_tickers: symbols to print stage-by-stage diagnostics for (in the
    pool? did the download return data? what were its actual computed
    numbers vs. the thresholds?) - for pinning down exactly where/why a
    specific ticker drops out instead of guessing.
    """
    if pool_size and pool_size > 0:
        print(f"Pulling a capped pool of up to {pool_size} tickers to rank by liquidity...")
        print(f"[WARN] A capped pool can miss genuinely liquid names by chance if the full")
        print(f"       universe is much larger than {pool_size} - use --universe-pool-size 0")
        print(f"       (the default) to rank the complete universe instead, for reliability.")
        pool = fetch_universe(limit=pool_size, source_pref=source_pref)
    else:
        print("Pulling the FULL available ticker universe to rank by liquidity (no cap)...")
        pool = fetch_universe(limit=None, source_pref=source_pref)

    for t in trace_tickers:
        status = "IN the raw pool" if t in pool else "NOT in the raw pool (dropped by fetch/cleanup filtering, before download even happens)"
        print(f"[TRACE] {t}: {status}")

    print(f"Downloading price history for {len(pool)} tickers to compute liquidity "
          f"(this is the slow part - grab a coffee)...")
    # Auto-throttle for large pools: the double-click workflow can't easily pass
    # --workers/--batch-size, and a full-universe refresh (thousands of tickers)
    # hitting yfinance with the same concurrency as a ~700-ticker daily scan is
    # what triggers Yahoo rate-limiting in the first place (better to prevent it
    # than only rely on the retry backoff in download_batch to recover from it).
    #
    # This throttling only actually matters for the yfinance fallback path -
    # download_universe() tries Alpaca first and, when it succeeds, never
    # touches the throttled batch/pause logic below at all. Checking here (not
    # just inside download_universe) so the console message reflects which
    # path is actually about to be used, instead of always warning about
    # Yahoo even on a run that's going to use Alpaca the whole way through.
    effective_workers = workers
    effective_pause = 0.5
    if len(pool) > 2000:
        if get_alpaca_clients():
            print(f"Large pool ({len(pool)} tickers) - using Alpaca. Still expect this to "
                  f"take a while for a full-universe refresh.")
        else:
            effective_workers = min(workers, 4)
            effective_pause = 1.5
            print(f"Large pool ({len(pool)} tickers), no Alpaca configured - throttling to "
                  f"{effective_workers} workers and a longer pause between batches to avoid "
                  f"Yahoo rate limits. This will take a while; that's expected for an "
                  f"infrequent full-universe refresh.")
    data = download_universe(pool, cfg["history_period"], batch_size=batch_size,
                              workers=effective_workers, pause=effective_pause)
    print(f"Got usable data for {len(data)}/{len(pool)} tickers. Ranking by liquidity...")

    for t in trace_tickers:
        status = "got price data back" if t in data else "NO price data returned (dropped during download - a data gap for this exact symbol from whichever source was used, or it landed in a failed batch)"
        print(f"[TRACE] {t}: {status}")

    scored = []
    for ticker, df in data.items():
        is_traced = ticker in trace_tickers
        try:
            df2 = df.rename(columns=str.title)
            if not {"High", "Low", "Close", "Volume"}.issubset(df2.columns) or len(df2) < cfg["adr_len"] + 5:
                if is_traced:
                    print(f"[TRACE] {ticker}: FAILED - missing expected columns or not enough history "
                          f"({len(df2)} rows, need {cfg['adr_len'] + 5})")
                continue
            h, l, c, v = df2["High"], df2["Low"], df2["Close"], df2["Volume"]
            adr_pct = ((h / l).rolling(cfg["adr_len"]).mean() * 100 - 100).iloc[-1]
            daily_dollar_vol = v * c
            dollar_vol = daily_dollar_vol.rolling(cfg["vol_avg_len"]).mean().iloc[-1]
            price = c.iloc[-1]
            if pd.isna(adr_pct) or pd.isna(dollar_vol) or pd.isna(price):
                if is_traced:
                    print(f"[TRACE] {ticker}: FAILED - NaN in computed metrics "
                          f"(adr_pct={adr_pct}, dollar_vol={dollar_vol}, price={price})")
                continue
            passes = adr_pct >= cfg["min_adr_pct"] and dollar_vol >= cfg["min_avg_dollar_vol"] and price >= cfg["min_price"]
            if is_traced:
                print(f"[TRACE] {ticker}: adr_pct={adr_pct:.2f}% (need >={cfg['min_adr_pct']}), "
                      f"dollar_vol=${dollar_vol/1e6:.1f}M (need >=${cfg['min_avg_dollar_vol']/1e6:.0f}M), "
                      f"price=${price:.2f} (need >=${cfg['min_price']}) -> {'PASSES' if passes else 'FAILS'} liquidity bar")
            if passes:
                scored.append((ticker, float(dollar_vol)))
        except Exception as e:
            if is_traced:
                print(f"[TRACE] {ticker}: FAILED - exception during liquidity calc: {e}")
            continue

    scored.sort(key=lambda x: x[1], reverse=True)
    kept = [t for t, _ in scored[:keep_size]]
    print(f"{len(scored)} tickers cleared the liquidity bar; keeping the top {len(kept)} by dollar volume.")
    return kept, len(pool)


def get_market_regime(period: str = "6mo") -> dict:
    """Simple market-health gate: 10-day SMA vs 20-day SMA on a broad index.
    Kris has referenced stepping aside on breakouts/EPs when this flips bearish.

    Tries SPY first, falls back to QQQ if SPY's data is unavailable/throttled,
    so a single symbol's API hiccup doesn't take out the whole regime check.
    Deliberately keeps the fast 10-vs-20-day definition (not a slower 50/200
    "golden cross" style filter) since that's what actually matches how Kris
    describes gauging near-term breakout/EP conditions - a 50/200 regime
    measures something meaningfully different (long-term trend, not swing-
    trading timing) and swapping it in would be a strategy change, not a
    robustness fix.
    """
    for symbol in ("SPY", "QQQ"):
        try:
            idx = yf.download(symbol, period=period, interval="1d", auto_adjust=True, progress=False)
            if idx.empty or len(idx) < 20:
                continue
            # Newer yfinance versions sometimes return MultiIndex columns (e.g.
            # ('Close','SPY')) even for a single ticker, which turns idx["Close"]
            # into a DataFrame instead of a Series and breaks scalar comparisons
            # below ("truth value of a Series is ambiguous"). Flatten defensively.
            if isinstance(idx.columns, pd.MultiIndex):
                idx.columns = idx.columns.get_level_values(0)
            idx = idx.loc[:, ~idx.columns.duplicated()]  # just in case of dup columns too

            idx["sma10"] = idx["Close"].rolling(10).mean()
            idx["sma20"] = idx["Close"].rolling(20).mean()
            last = idx.iloc[-1]
            sma10_val = float(last["sma10"])
            sma20_val = float(last["sma20"])
            bullish = bool(sma10_val > sma20_val)
            return {
                "symbol": symbol,
                "bullish": bullish,
                "spy_close": round(float(last["Close"]), 2),
                "spy_sma10": round(sma10_val, 2),
                "spy_sma20": round(sma20_val, 2),
                "as_of": idx.index[-1].strftime("%Y-%m-%d"),
            }
        except Exception as e:
            print(f"[WARN] Could not compute market regime from {symbol}: {e}"
                  + (" - trying QQQ instead..." if symbol == "SPY" else ""))
            continue

    print("[WARN] Market regime unavailable from both SPY and QQQ.")
    return {"symbol": None, "bullish": None}


# =============================================================================
# 0-100 SCORING SYSTEM
# Momentum/trend/volume point breakdowns per setup, badged SETUP!/WATCH/Monitor/-.
# Unlike the pass/fail boolean setups above, this surfaces "getting there"
# candidates that are missing 1-2 criteria, not just fully-confirmed ones.
# =============================================================================
def score_htf_breakout(ret_1m, ret_3m, ema10_rising, ma_stacked, dist_10ma_pct,
                        dist_20ma_pct, vol_mult, pct_of_52w_high) -> tuple[int, dict]:
    momentum = (
        tier_score_ge(ret_1m, [(40, 20), (25, 15), (15, 10), (5, 5)])
        + tier_score_ge(ret_3m, [(80, 15), (50, 10), (30, 5)])
    )
    # Redesigned 2026-07-30: previously flat "close > EMA10" / "close > EMA20"
    # points regardless of trend strength - rewarded a stock barely poking above
    # a flat MA the same as one in a strong confirmed uptrend. Now requires
    # EMA10 to actually be RISING (not just "above", mirrors Minervini's
    # slope-confirmation requirement) and the full MA stack in bullish order
    # (EMA10 > EMA20 > SMA50, mirrors Minervini's 50>150>200 stack requirement
    # and the common "EMA ribbon" trend-strength convention). Same 20-point
    # weight as before, just requiring real trend confirmation to earn it.
    trend_position = (10 if ema10_rising else 0) + (10 if ma_stacked else 0)
    ma_surfing = (
        tier_score_le(dist_10ma_pct, [(2, 20), (4, 15), (7, 10)])
        + (5 if dist_20ma_pct is not None and dist_20ma_pct <= 5 else 0)
    )
    vol_ext = (
        tier_score_ge(vol_mult, [(2, 10), (1.3, 5)])
        + tier_score_ge(pct_of_52w_high, [(90, 10), (75, 5)])
    )
    total = momentum + trend_position + ma_surfing + vol_ext
    return total, {"momentum": momentum, "trend_position": trend_position,
                    "ma_surfing": ma_surfing, "volume_extension": vol_ext}


def score_episodic_pivot(gap_pct, vol_mult, ret_3m, above_20d_high, pct_of_20d_high) -> tuple[int, dict]:
    gap_move = tier_score_ge(gap_pct, [(20, 40), (10, 30), (5, 15)])
    vol_surge = tier_score_ge(vol_mult, [(3, 25), (2, 20), (1.5, 10)])
    # Bug fix (2026-07-30): was tier_score_le(ret_3m, ...) unsigned - a deep prior
    # CRASH (e.g. ret_3m = -73%) satisfied "ret_3m <= 10" just as well as genuine
    # flatness (ret_3m = +5%), incorrectly awarding full "prior base" points to
    # already-collapsed stocks. "Flat for 3-6 months" means low MAGNITUDE of
    # movement either direction, so this now checks abs(ret_3m).
    prior_base = tier_score_le(abs(ret_3m) if ret_3m is not None else None, [(10, 20), (25, 15), (40, 10)])
    breakout_pts = 15 if above_20d_high else (10 if pct_of_20d_high is not None and pct_of_20d_high >= 95 else 0)
    total = gap_move + vol_surge + prior_base + breakout_pts
    return total, {"gap_move": gap_move, "volume_surge": vol_surge,
                    "prior_base": prior_base, "breakout": breakout_pts}


def score_parabolic_short(ret_1m, up_streak, dist_10ma_pct, adr_pct) -> tuple[int, dict]:
    massive_gain = tier_score_ge(ret_1m, [(200, 35), (100, 25), (60, 15), (40, 10)])
    consec_up = tier_score_ge(up_streak, [(7, 25), (5, 20), (3, 10)])
    # only credit extension when actually above the MA (positive distance)
    ext_val = dist_10ma_pct if (dist_10ma_pct is not None and dist_10ma_pct > 0) else 0
    extension = tier_score_ge(ext_val, [(40, 25), (25, 20), (15, 10)])
    volatility = tier_score_ge(adr_pct, [(10, 15), (7, 10), (5, 5)])
    total = massive_gain + consec_up + extension + volatility
    return total, {"massive_gain": massive_gain, "consecutive_up_days": consec_up,
                    "extension_from_ma": extension, "volatility": volatility}


# =============================================================================
# 2LYNCH CONSOLIDATION QUALITY CHECKS
# Pradeep Bonde (Stockbee)'s breakout-quality checklist, adapted:
#   2 = not up two days in a row already, going into today
#   L = linearity of the prior move (efficiency ratio, not just a jagged spike)
#   Y = young trend (breakout is early in the base's life, not aged/extended)
#   N = narrow-range or down day right before the trigger (coiling, not thrusting)
#   C = consolidation quality (shallow, orderly, <=1 sharp down day, volume dry-up)
#   H = close near the high of the trigger bar
# Returns a dict of 6 booleans plus a 0-6 pass count.
# =============================================================================
def check_2lynch(o: pd.Series, h: pd.Series, l: pd.Series, c: pd.Series, v: pd.Series, cfg: dict) -> dict:
    def last(x, default=False):
        try:
            val = x.iloc[-1]
            return bool(val) if pd.notna(val) else default
        except Exception:
            return default

    # 2 - not already up two days in a row before today
    prev1_up = c.shift(1) > c.shift(2)
    prev2_up = c.shift(2) > c.shift(3)
    lynch_2 = not last(prev1_up & prev2_up)

    # L - linearity via Kaufman efficiency ratio over the breakout lookback window
    win = cfg["bo_move_lookback"]
    if len(c) > win:
        net = abs(c.iloc[-1] - c.iloc[-win])
        path = c.diff().abs().iloc[-win:].sum()
        efficiency = net / path if path else 0
    else:
        efficiency = 0
    lynch_L = efficiency >= 0.35

    # Y - young trend: days since price last closed below its 50-day SMA
    sma50 = c.rolling(50).mean()
    below50 = c < sma50
    # count consecutive False (i.e. bars since last True) from the end
    rev = below50[::-1]
    bars_since_below = 0
    for val in rev:
        if bool(val):
            break
        bars_since_below += 1
    lynch_Y = bars_since_below <= 90

    # N - narrow range or negative day immediately before the trigger bar
    bar_range = h - l
    avg_range = bar_range.rolling(20).mean()
    narrow_prev = last(bar_range.shift(1) < avg_range.shift(1))
    neg_prev = last(c.shift(1) < c.shift(2))
    lynch_N = narrow_prev or neg_prev

    # C - consolidation quality: <=1 sharp (>=4%) down day in the base, volume dry-up
    pct_chg = c.pct_change() * 100
    consol_len = cfg["bo_consol_len"]
    down4_count = pct_chg.shift(1).rolling(consol_len).apply(
        lambda s: float((s <= -4).sum()), raw=True
    )
    vol_consol = v.shift(1).rolling(consol_len).mean()
    vol_prior_move = v.shift(1).rolling(cfg["bo_move_lookback"]).mean()
    shallow = last(down4_count <= 1)
    dry_up = last(vol_consol < vol_prior_move)
    lynch_C = shallow and dry_up

    # H - close near the high of the current bar
    today_range = h.iloc[-1] - l.iloc[-1] if len(h) else 0
    close_pos = (c.iloc[-1] - l.iloc[-1]) / today_range if today_range else 0
    lynch_H = close_pos >= 0.7

    flags = {"2": lynch_2, "L": lynch_L, "Y": lynch_Y, "N": lynch_N, "C": lynch_C, "H": lynch_H}
    flags["lynch_count"] = sum(1 for k, val in flags.items() if k != "lynch_count" and val)
    return flags


# =============================================================================
# SETUP LOGIC  (same math as the Pine indicator, in pandas form)
# =============================================================================
def compute_signals(ticker: str, df: pd.DataFrame, cfg: dict) -> dict | None:
    df = df.rename(columns=str.title)  # Open/High/Low/Close/Volume
    needed = {"Open", "High", "Low", "Close", "Volume"}
    if not needed.issubset(df.columns) or len(df) < max(
        cfg["bo_move_lookback"], cfg["ep_no_run_lookback"]
    ) + 5:
        return None

    o, h, l, c, v = df["Open"], df["High"], df["Low"], df["Close"], df["Volume"]

    # --- liquidity / tradeability ---
    adr_pct = ((h / l).rolling(cfg["adr_len"]).mean() * 100 - 100)
    avg_vol = v.rolling(cfg["vol_avg_len"]).mean()
    # BUGFIX: dollar volume must be the average of (daily volume * that day's
    # price), NOT (avg volume * today's price). The latter mixes an older,
    # possibly much-lower price regime's volume with today's price - for a
    # stock that just spiked (exactly what Breakout/EP candidates look like),
    # this can make an actually-thin stock look like it clears the $5M bar
    # when its real day-to-day dollar volume never has.
    daily_dollar_vol = v * c
    dollar_vol = daily_dollar_vol.rolling(cfg["vol_avg_len"]).mean()
    liquidity_ok = (adr_pct >= cfg["min_adr_pct"]) & (dollar_vol >= cfg["min_avg_dollar_vol"]) \
        & (c >= cfg["min_price"])

    ema10 = c.ewm(span=10, adjust=False).mean()
    ema20 = c.ewm(span=20, adjust=False).mean()
    sma50 = c.rolling(50).mean()
    sma200 = c.rolling(200).mean()

    # --- metrics used by the 0-100 scoring system ---
    # NOTE: uses ema10/ema20 (not sma10/sma20) - Kris has stated directly that
    # price should "smoothly surf the 10 and 20 ema". Previously this used
    # SMA10/SMA20, which was inconsistent with the Pine indicator (which always
    # used EMA10/EMA20) and with Kris's own stated methodology. Standardized
    # 2026-07-30 to EMA10/EMA20 across both Stage 1 and Stage 2.
    ret_1m = (c - c.shift(21)) / c.shift(21) * 100
    ret_3m = (c - c.shift(63)) / c.shift(63) * 100
    dist_10ma_pct = (c - ema10) / ema10 * 100        # signed: + above, - below
    dist_20ma_pct = (c - ema20).abs() / ema20 * 100  # unsigned distance for "surfing"
    high_20d = h.shift(1).rolling(20).max()
    lookback_52w = min(len(c), 252)
    high_52w = h.rolling(lookback_52w, min_periods=20).max()
    pct_of_52w_high = c / high_52w * 100
    pct_of_20d_high = c / high_20d * 100

    # --- breakout ---
    prior_high = h.rolling(cfg["bo_move_lookback"]).max()
    prior_low = l.rolling(cfg["bo_move_lookback"]).min()
    prior_move_pct = (prior_high - prior_low) / prior_low * 100
    made_big_move = prior_move_pct >= cfg["bo_min_move_pct"]

    consol_high = h.shift(1).rolling(cfg["bo_consol_len"]).max()
    consol_low = l.shift(1).rolling(cfg["bo_consol_len"]).min()
    consol_range_pct = (consol_high - consol_low) / consol_low * 100
    tight = consol_range_pct <= cfg["bo_max_range_pct"]

    vol_expansion = v >= avg_vol * cfg["bo_vol_expansion"]
    breaks_high = (c > consol_high) & (c > c.shift(1))
    above_sma50 = (c > sma50) if cfg["bo_require_above_sma50"] else True
    breakout_setup = liquidity_ok & made_big_move & tight & vol_expansion & breaks_high & (c > ema20) & above_sma50

    # --- episodic pivot ---
    gap_pct = (o - c.shift(1)) / c.shift(1) * 100
    prior_low_6mo = l.shift(1).rolling(cfg["ep_no_run_lookback"]).min()
    prior_run_pct = (c.shift(1) - prior_low_6mo) / prior_low_6mo * 100
    not_extended = prior_run_pct <= cfg["ep_max_prior_run_pct"]
    ep_setup = liquidity_ok & (gap_pct >= cfg["ep_min_gap_pct"]) \
        & (v >= avg_vol * cfg["ep_min_vol_mult"]) & not_extended

    # --- parabolic short / long ---
    up_streak = (c > c.shift(1)).astype(int)
    up_streak = up_streak.groupby((up_streak != up_streak.shift()).cumsum()).cumsum() * up_streak
    down_streak = (c < c.shift(1)).astype(int)
    down_streak = down_streak.groupby((down_streak != down_streak.shift()).cumsum()).cumsum() * down_streak

    move_up_pct = (c - c.shift(cfg["pb_lookback"])) / c.shift(cfg["pb_lookback"]) * 100
    move_down_pct = (c.shift(cfg["pb_lookback"]) - c) / c.shift(cfg["pb_lookback"]) * 100
    pb_ma = c.rolling(cfg["pb_ma_len"]).mean()
    stretch_above = (c - pb_ma) / pb_ma * 100
    stretch_below = (pb_ma - c) / pb_ma * 100

    para_short = liquidity_ok & (move_up_pct >= cfg["pb_min_move_pct"]) \
        & (up_streak >= cfg["pb_min_streak"]) & (stretch_above >= cfg["pb_min_stretch_pct"])

    crash = liquidity_ok & (move_down_pct >= cfg["pb_min_move_pct"]) \
        & (down_streak >= cfg["pb_min_streak"]) & (stretch_below >= cfg["pb_min_stretch_pct"])
    bar_idx = pd.Series(np.arange(len(crash)), index=crash.index)
    last_crash_idx = pd.Series(np.where(crash, bar_idx, np.nan), index=crash.index).ffill()
    bars_since_crash = (bar_idx - last_crash_idx).fillna(10_000)
    recent_crash = bars_since_crash <= cfg["pb_long_reversal_window"]
    para_long = recent_crash & (c > o) & (c > c.shift(1)) & liquidity_ok

    # --- collapse to "did it fire in the last N bars" ---
    n = cfg["signal_lookback_days"]

    def recent(series: pd.Series) -> bool:
        return bool(series.tail(n).fillna(False).any())

    def scalar(series: pd.Series, ndigits=2):
        val = series.iloc[-1]
        return round(float(val), ndigits) if pd.notna(val) else None

    last = df.iloc[-1]
    vol_mult_val = scalar(v / avg_vol, 2)
    adr_val = scalar(adr_pct, 2)
    ret1m_val = scalar(ret_1m, 1)
    ret3m_val = scalar(ret_3m, 1)
    gap_val = scalar(gap_pct, 1)
    dist10_val = scalar(dist_10ma_pct, 2)
    dist20_val = scalar(dist_20ma_pct, 2)
    pct52w_val = scalar(pct_of_52w_high, 1)
    pct20d_val = scalar(pct_of_20d_high, 1)
    up_streak_val = int(up_streak.iloc[-1]) if pd.notna(up_streak.iloc[-1]) else 0

    slope_lb = cfg["bo_trend_slope_lookback"]
    ema10_now = ema10.iloc[-1]
    ema10_prev = ema10.iloc[-1 - slope_lb] if len(ema10) > slope_lb else None
    ema10_rising = bool(pd.notna(ema10_now) and ema10_prev is not None and pd.notna(ema10_prev)
                         and ema10_now > ema10_prev)
    ema20_now = ema20.iloc[-1]
    sma50_now = sma50.iloc[-1]
    ma_stacked = bool(pd.notna(ema10_now) and pd.notna(ema20_now) and pd.notna(sma50_now)
                       and ema10_now > ema20_now > sma50_now)

    htf_score, htf_breakdown = score_htf_breakout(
        ret1m_val, ret3m_val, ema10_rising, ma_stacked,
        abs(dist10_val) if dist10_val is not None else None, dist20_val, vol_mult_val, pct52w_val,
    )
    ep_score, ep_breakdown = score_episodic_pivot(
        gap_val, vol_mult_val, ret3m_val,
        bool(last["Close"] > high_20d.iloc[-1]) if pd.notna(high_20d.iloc[-1]) else False, pct20d_val,
    )
    pshort_score, pshort_breakdown = score_parabolic_short(ret1m_val, up_streak_val, dist10_val, adr_val)

    # BUGFIX: the liquidity gate (min ADR%, min dollar volume, min price) was
    # only ever applied to the boolean pass/fail signals below, never to the
    # 0-100 scores - meaning a sub-$2 or thin-volume ticker could still score
    # into WATCH/Monitor tiers and show up in results despite failing the
    # liquidity filter entirely. Zero the score out (and badge falls to "-")
    # for anything that doesn't clear the liquidity bar.
    liq_ok_last = bool(liquidity_ok.iloc[-1]) if pd.notna(liquidity_ok.iloc[-1]) else False
    if not liq_ok_last:
        htf_score, ep_score, pshort_score = 0, 0, 0

    # Kris's stated hard rule "never buys below 50MA" applies specifically to
    # Breakouts (not EP - a fresh gap-driven catalyst can legitimately start
    # below the 50MA; not Parabolic Short/Long - different dynamics entirely).
    if cfg["bo_require_above_sma50"]:
        above_sma50_last = bool(c.iloc[-1] > sma50.iloc[-1]) if pd.notna(sma50.iloc[-1]) else False
        if not above_sma50_last:
            htf_score = 0

    lynch = check_2lynch(o, h, l, c, v, cfg)

    result = {
        "ticker": ticker,
        "close": round(float(last["Close"]), 2),
        "adr_pct": adr_val,
        "dollar_vol_m": round(float(dollar_vol.iloc[-1]) / 1e6, 1) if pd.notna(dollar_vol.iloc[-1]) else None,
        "prior_move_pct": round(float(prior_move_pct.iloc[-1]), 1) if pd.notna(prior_move_pct.iloc[-1]) else None,
        "gap_pct": gap_val,
        "vol_mult": vol_mult_val,
        "ret_1m_pct": ret1m_val,
        "ret_3m_pct": ret3m_val,
        "breakout": recent(breakout_setup),
        "episodic_pivot": recent(ep_setup),
        "parabolic_short": recent(para_short),
        "parabolic_long": recent(para_long),
        "htf_score": htf_score,
        "htf_badge": score_to_badge(htf_score),
        "ep_score": ep_score,
        "ep_badge": score_to_badge(ep_score),
        "pshort_score": pshort_score,
        "pshort_badge": score_to_badge(pshort_score),
        "lynch_count": lynch["lynch_count"],
        "lynch_flags": "".join(k for k in ["2", "L", "Y", "N", "C", "H"] if lynch[k]),
        "above_sma200": bool(c.iloc[-1] > sma200.iloc[-1]) if pd.notna(sma200.iloc[-1]) else None,
        "as_of": df.index[-1].strftime("%Y-%m-%d"),
    }
    return result


# =============================================================================
# HTML DASHBOARD
# =============================================================================
RECENT_TRIGGERS_FILENAME = "recent_triggers.json"


def load_recent_triggers(out_dir: Path) -> dict:
    path = out_dir / RECENT_TRIGGERS_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_recent_triggers(out_dir: Path, history: dict) -> None:
    (out_dir / RECENT_TRIGGERS_FILENAME).write_text(
        json.dumps(history, indent=2, sort_keys=True), encoding="utf-8"
    )


def update_recent_triggers(results_df: pd.DataFrame, out_dir: Path,
                            lookback_days: int, min_score: int = 80) -> pd.DataFrame:
    """
    Maintains a small JSON sidecar (recent_triggers.json, next to the dashboard) tracking
    every ticker/setup pair that hits >= min_score (SETUP!-caliber, not WATCH/Monitor) on
    any of the three scored setups. This is pure persistence/logging on top of scores that
    are already computed - it does NOT change how anything is scored.

    Each scan run is a separate process with no memory of prior runs, so without this file
    a signal that fired yesterday (e.g. an EP gap) simply vanishes from the dashboard the
    next day once its score naturally decays - even though the trade opportunity it
    flagged may still be very much alive. This keeps it visible for `lookback_days` days
    after the last day it actually qualified, then drops it.
    """
    today_str = dt.date.today().isoformat()
    history = load_recent_triggers(out_dir)

    setup_cols = {
        "Breakout": ("htf_score", "htf_badge"),
        "Episodic Pivot": ("ep_score", "ep_badge"),
        "Parabolic Short": ("pshort_score", "pshort_badge"),
    }

    for setup_label, (score_col, badge_col) in setup_cols.items():
        hits = results_df[results_df[score_col] >= min_score]
        for _, row in hits.iterrows():
            key = f"{row['ticker']}|{setup_label}"
            existing = history.get(key, {})
            history[key] = {
                "ticker": row["ticker"],
                # named "type" (not "setup") so it doesn't collide visually with the
                # "Setup" header table_html() generates from the badge column below
                "type": setup_label,
                "close": float(row["close"]) if pd.notna(row.get("close")) else None,
                "score": int(row[score_col]),
                "badge": row[badge_col],
                "first_triggered": existing.get("first_triggered", today_str),
                "last_triggered": today_str,
            }

    # Drop anything that hasn't re-qualified within the lookback window. Using
    # last_triggered (not first_triggered) means a setup that keeps re-firing
    # (e.g. SETUP! several days running) keeps resetting its own clock, which is
    # the intended behavior - it's "still active", not "recently gone stale".
    #
    # Also re-check each stored entry's score against the CURRENT min_score, not
    # just the date cutoff. Without this, entries written by an earlier run under
    # a looser threshold (e.g. this file previously used 60, then 75, before
    # settling on 80) would keep showing up for the rest of their lookback window
    # even though they'd never have been added under today's threshold.
    cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
    history = {
        k: v for k, v in history.items()
        if dt.date.fromisoformat(v["last_triggered"]) >= cutoff and v.get("score", 0) >= min_score
    }
    save_recent_triggers(out_dir, history)

    cols = ["ticker", "type", "close", "score", "badge", "first_triggered", "last_triggered", "days_since"]
    if not history:
        return pd.DataFrame(columns=cols)

    recent_df = pd.DataFrame(list(history.values()))
    recent_df["days_since"] = recent_df["last_triggered"].apply(
        lambda d: (dt.date.today() - dt.date.fromisoformat(d)).days
    )
    recent_df = recent_df.sort_values(["days_since", "score"], ascending=[True, False])
    return recent_df[cols]


def build_html_dashboard(results_df: pd.DataFrame, regime: dict, out_path: Path,
                          min_score: int = 40, recent_df: pd.DataFrame | None = None) -> None:
    regime_badge = (
        '<span class="badge bull">BULLISH REGIME</span>' if regime.get("bullish")
        else '<span class="badge bear">BEARISH / CAUTION</span>' if regime.get("bullish") is False
        else '<span class="badge unknown">REGIME UNKNOWN</span>'
    )
    regime_symbol = regime.get("symbol", "SPY")
    # Same daily-chart TradingView link format as ticker_link() below - inlined here
    # since ticker_link isn't defined yet at this point in the function.
    regime_symbol_link = (
        f'<a href="https://www.tradingview.com/chart/?symbol={regime_symbol}&interval=D" '
        f'target="_blank" rel="noopener" class="ticker-link">{regime_symbol}</a>'
    )
    regime_detail = (
        f'{regime_symbol_link} {regime.get("spy_close", "-")} | '
        f'10SMA {regime.get("spy_sma10", "-")} vs 20SMA {regime.get("spy_sma20", "-")} '
        f'(as of {regime.get("as_of", "-")})' if regime.get("bullish") is not None else ""
    )

    def badge_html(badge: str) -> str:
        cls = {"SETUP!": "b-setup", "WATCH": "b-watch", "Monitor": "b-monitor", "-": "b-none"}.get(badge, "b-none")
        return f'<span class="sbadge {cls}">{badge}</span>'

    def ticker_link(t: str) -> str:
        return (f'<a href="https://www.tradingview.com/chart/?symbol={t}&interval=D" '
                f'target="_blank" rel="noopener" class="ticker-link">{t}</a>')

    def table_html(df: pd.DataFrame, cols: list[str], score_col: str | None, badge_col: str | None,
                    table_id: str = "") -> str:
        if df.empty:
            return "<p class='empty'>No candidates today.</p>"
        d = df[cols].copy()
        if "ticker" in d.columns:
            d["ticker"] = d["ticker"].apply(ticker_link)
        if badge_col:
            d[badge_col] = df[badge_col].apply(badge_html)
            d = d.rename(columns={score_col: "Score", badge_col: "Setup"})
        html_table = d.to_html(index=False, classes="data-table", border=0, escape=False)
        if table_id:
            html_table = html_table.replace('class="dataframe data-table"',
                                             f'class="dataframe data-table" id="{table_id}"', 1)
        return html_table

    if recent_df is None:
        recent_df = pd.DataFrame(columns=["ticker", "type", "close", "score", "badge",
                                           "first_triggered", "last_triggered", "days_since"])
    recent_cols = ["ticker", "type", "close", "score", "badge", "first_triggered",
                   "last_triggered", "days_since"]

    bo_cols = ["ticker", "close", "htf_score", "htf_badge", "adr_pct", "prior_move_pct",
               "vol_mult", "lynch_count", "lynch_flags", "as_of"]
    ep_cols = ["ticker", "close", "ep_score", "ep_badge", "adr_pct", "gap_pct", "vol_mult",
               "ret_3m_pct", "dollar_vol_m", "as_of"]
    ps_cols = ["ticker", "close", "pshort_score", "pshort_badge", "ret_1m_pct",
               "adr_pct", "vol_mult", "as_of"]
    pl_cols = ["ticker", "close", "adr_pct", "dollar_vol_m", "vol_mult", "as_of"]

    bo_df = results_df[results_df["htf_score"] >= min_score].sort_values("htf_score", ascending=False)
    ep_df = results_df[results_df["ep_score"] >= min_score].sort_values("ep_score", ascending=False)
    ps_df = results_df[results_df["pshort_score"] >= min_score].sort_values("pshort_score", ascending=False)
    pl_df = results_df[results_df["parabolic_long"]].sort_values("adr_pct", ascending=False)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Qullamaggie Stage-1 Scan — {dt.date.today().isoformat()}</title>
<style>
  :root {{ --bg:#0f1115; --panel:#171a21; --line:#262b36; --text:#e6e8ec; --muted:#9aa2b1;
           --green:#3ecf8e; --red:#ff5c5c; --amber:#f5b942; --accent:#6ea8fe; }}
  * {{ box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,Roboto,sans-serif;
          margin:0; padding:32px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); margin-bottom:20px; font-size:13px; }}
  .badge {{ display:inline-block; padding:4px 10px; border-radius:20px; font-size:12px; font-weight:600;
            letter-spacing:.03em; margin-right:10px; }}
  .badge.bull {{ background:rgba(62,207,142,.15); color:var(--green); }}
  .badge.bear {{ background:rgba(255,92,92,.15); color:var(--red); }}
  .badge.unknown {{ background:rgba(154,162,177,.15); color:var(--muted); }}
  .sbadge {{ display:inline-block; padding:3px 9px; border-radius:6px; font-size:11.5px; font-weight:700; }}
  .sbadge.b-setup {{ background:rgba(62,207,142,.18); color:var(--green); }}
  .sbadge.b-watch {{ background:rgba(245,185,66,.18); color:var(--amber); }}
  .sbadge.b-monitor {{ background:rgba(110,168,254,.15); color:var(--accent); }}
  .sbadge.b-none {{ background:rgba(154,162,177,.1); color:var(--muted); }}
  .regime {{ margin-bottom:14px; font-size:13px; color:var(--muted); }}
  .legend {{ margin-bottom:28px; font-size:12.5px; color:var(--muted); }}
  .muted-note {{ color:var(--muted); font-size:11.5px; }}
  section {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
             padding:18px 20px; margin-bottom:22px; }}
  section h2 {{ margin:0 0 4px; font-size:15px; }}
  section p.desc {{ margin:0 0 14px; color:var(--muted); font-size:12.5px; }}
  table.data-table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  table.data-table th {{ text-align:left; color:var(--muted); font-weight:600; padding:8px 10px;
                          border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--panel);
                          cursor:pointer; user-select:none; white-space:nowrap; }}
  table.data-table th:hover {{ color:var(--text); }}
  table.data-table th.sorted-asc::after {{ content:" \25B2"; color:var(--accent); font-size:10px; }}
  table.data-table th.sorted-desc::after {{ content:" \25BC"; color:var(--accent); font-size:10px; }}
  table.data-table tr.row-hidden {{ display:none; }}
  table.data-table td {{ padding:7px 10px; border-bottom:1px solid rgba(255,255,255,.04); }}
  table.data-table tr:hover td {{ background:rgba(255,255,255,.03); }}
  .empty {{ color:var(--muted); font-size:13px; font-style:italic; }}
  .count {{ color:var(--accent); font-weight:600; }}
  .search {{ margin-bottom:12px; background:var(--bg); border:1px solid var(--line); color:var(--text);
             padding:6px 10px; border-radius:6px; font-size:13px; width:200px; }}
  footer {{ color:var(--muted); font-size:11px; margin-top:30px; }}
  .guide-link {{ color:var(--accent); text-decoration:none; }}
  .guide-link:hover {{ text-decoration:underline; }}
  .ticker-link {{ color:var(--text); font-weight:600; text-decoration:none; border-bottom:1px dotted var(--muted); }}
  .ticker-link:hover {{ color:var(--accent); border-bottom-color:var(--accent); }}
  .calc-inputs {{ display:flex; gap:20px; flex-wrap:wrap; margin-bottom:10px; }}
  .calc-inputs label {{ display:flex; flex-direction:column; gap:4px; font-size:12.5px; color:var(--muted); }}
  .calc-inputs input {{ background:var(--bg); border:1px solid var(--line); color:var(--text);
                         padding:6px 10px; border-radius:6px; font-size:13.5px; width:150px; }}
  td.calc-warn {{ color:var(--red); font-weight:600; }}
</style>
</head>
<body>
  <h1>Qullamaggie Stage-1 Scan</h1>
  <div class="sub">Generated {dt.datetime.now().strftime("%Y-%m-%d %H:%M")} &middot;
     {len(results_df)} tickers scanned &middot; candidate generator only, confirm on chart before trading
     &middot; <a href="guide.html" class="guide-link">📖 What do these scores/flags mean?</a></div>
  <div class="regime">{regime_badge} {regime_detail}</div>

  <section id="calc-panel">
    <h2>Position Sizing Calculator</h2>
    <p class="desc">ADR-based stop and share count for every row below, computed live in your browser as you
      change these settings — no re-scan needed. Stop = close &times; (1 &minus; ADR% &times; multiplier).
      Shares = (account &times; risk%) &divide; (close &minus; stop). Kris's own stated ranges: risk 0.25-1% per
      trade (0.5-1.5% on smaller accounts), never more than 30% of account in one position overnight.</p>
    <div class="calc-inputs">
      <label>Account Size ($)<input type="number" id="calcAccount" value="100000" min="0" step="1000"></label>
      <label>Risk % per Trade<input type="number" id="calcRisk" value="0.5" min="0.05" max="5" step="0.05"></label>
      <label>Stop Distance (&times; ADR%)<input type="number" id="calcAdrMult" value="1.0" min="0.25" max="3" step="0.25"></label>
    </div>
    <p class="muted-note">Position value shown in red if it would exceed 30% of account size (Kris's overnight cap).
      This is a mechanical calculation from ADR%, not a substitute for judgment — a stock's actual consolidation
      low or the prior day's low may call for a wider or tighter stop than a flat ADR multiple. Not financial advice.</p>
  </section>
  <div class="legend">
    {badge_html("SETUP!")} 80-100, chart it now &nbsp;&nbsp;
    {badge_html("WATCH")} 60-79, add to watchlist &nbsp;&nbsp;
    {badge_html("Monitor")} 40-59, worth tracking &nbsp;&nbsp;
    <span class="muted-note">Lynch = 2LYNCH consolidation-quality checks passed, out of 6 (Breakout table only)</span>
  </div>

  <section id="recent-list">
    <h2>Recently Triggered (score &ge; 80) &nbsp;<span class="count">({len(recent_df)})</span></h2>
    <p class="desc">Tickers that scored 80 or higher (SETUP!-caliber) on any setup within the last few days,
      even if today's score has since decayed back down — e.g. an Episodic Pivot the day after its gap,
      once the volume/gap-of-the-day buckets naturally reset. "Days Since" counts back to the most recent
      day it actually qualified. This list does not affect scoring; it's a memory layer on top of it.
      Click any column header to sort.</p>
    <input class="search" data-table="recent-table" placeholder="Filter ticker...">
    <div class="list">{table_html(recent_df, recent_cols, "score", "badge", "recent-table")}</div>
  </section>

  <section id="breakout-list">
    <h2>Breakout &nbsp;<span class="count">({len(bo_df)})</span></h2>
    <p class="desc">Scored 0-100 on momentum, trend position, MA-surfing tightness, and volume/extension.
      Score &ge; {min_score} shown below; not all of these are confirmed breakouts yet — check the badge.
      Click any column header to sort.</p>
    <input class="search" data-table="breakout-table" placeholder="Filter ticker...">
    <div class="list">{table_html(bo_df, bo_cols, "htf_score", "htf_badge", "breakout-table")}</div>
  </section>

  <section id="ep-list">
    <h2>Episodic Pivot &nbsp;<span class="count">({len(ep_df)})</span></h2>
    <p class="desc">Scored 0-100 on gap size, volume surge, prior-base flatness, and 20-day-high breakout.
      Click any column header to sort.</p>
    <input class="search" data-table="ep-table" placeholder="Filter ticker...">
    {table_html(ep_df, ep_cols, "ep_score", "ep_badge", "ep-table")}
  </section>

  <section id="pshort-list">
    <h2>Parabolic Short &nbsp;<span class="count">({len(ps_df)})</span></h2>
    <p class="desc">Scored 0-100 on 1-month gain size, consecutive up days, extension above the 10-day MA, and ADR.
      Click any column header to sort.</p>
    <input class="search" data-table="pshort-table" placeholder="Filter ticker...">
    {table_html(ps_df, ps_cols, "pshort_score", "pshort_badge", "pshort-table")}
  </section>

  <section id="plong-list">
    <h2>Parabolic Long (post-crash reversal) &nbsp;<span class="count">({len(pl_df)})</span></h2>
    <p class="desc">A recent parabolic-down "crash" followed by a green reversal bar. (Boolean signal only — no 0-100 score defined for this one yet.)
      Click any column header to sort.</p>
    <input class="search" data-table="plong-table" placeholder="Filter ticker...">
    {table_html(pl_df, pl_cols, None, None, "plong-table")}
  </section>

  <footer>Data via Alpaca/yfinance, delayed/EOD. Not financial advice.
    Feed the tickers above into TradingView + the companion Pine indicator for Stage-2 chart confirmation.</footer>

  <script>
  (function() {{
    // Vanilla-JS sort + filter for every .data-table - no external library needed.
    function getCellValue(row, idx) {{
      return row.children[idx].innerText.trim();
    }}
    function compareValues(a, b, idx) {{
      const av = getCellValue(a, idx), bv = getCellValue(b, idx);
      const an = parseFloat(av.replace(/[^0-9.-]/g, ''));
      const bn = parseFloat(bv.replace(/[^0-9.-]/g, ''));
      const bothNumeric = !isNaN(an) && !isNaN(bn) && av.match(/[0-9]/) && bv.match(/[0-9]/);
      if (bothNumeric) return an - bn;
      return av.localeCompare(bv);
    }}
    document.querySelectorAll('table.data-table').forEach(function(table) {{
      const headers = table.querySelectorAll('th');
      headers.forEach(function(th, idx) {{
        th.addEventListener('click', function() {{
          const tbody = table.querySelector('tbody');
          const rows = Array.from(tbody.querySelectorAll('tr'));
          const wasAsc = th.classList.contains('sorted-asc');
          headers.forEach(function(h) {{ h.classList.remove('sorted-asc', 'sorted-desc'); }});
          const dir = wasAsc ? -1 : 1;
          th.classList.add(wasAsc ? 'sorted-desc' : 'sorted-asc');
          rows.sort(function(a, b) {{ return dir * compareValues(a, b, idx); }});
          rows.forEach(function(row) {{ tbody.appendChild(row); }});
        }});
      }});
    }});
    document.querySelectorAll('input.search').forEach(function(input) {{
      const targetId = input.getAttribute('data-table');
      const table = document.getElementById(targetId);
      if (!table) return;
      input.addEventListener('input', function() {{
        const q = input.value.trim().toLowerCase();
        const tbody = table.querySelector('tbody');
        Array.from(tbody.querySelectorAll('tr')).forEach(function(row) {{
          const tickerCell = row.children[0];
          const match = !q || (tickerCell && tickerCell.innerText.toLowerCase().includes(q));
          row.classList.toggle('row-hidden', !match);
        }});
      }});
    }});

    // --- Position Sizing Calculator ---
    // Finds the 'close' and 'adr_pct' columns in every .data-table and appends
    // live-computed Stop/Shares columns, driven by the Account/Risk/ADR-multiplier
    // inputs above. This is a plain local HTML file opened directly in your
    // browser (not a sandboxed Claude artifact), so localStorage works normally
    // here and is used to remember your settings across scans.
    var calcAccountEl = document.getElementById('calcAccount');
    var calcRiskEl = document.getElementById('calcRisk');
    var calcAdrMultEl = document.getElementById('calcAdrMult');

    function restoreCalcSettings() {{
      try {{
        var saved = JSON.parse(localStorage.getItem('qm_calc_settings') || '{{}}');
        if (saved.account) calcAccountEl.value = saved.account;
        if (saved.risk) calcRiskEl.value = saved.risk;
        if (saved.adrMult) calcAdrMultEl.value = saved.adrMult;
      }} catch (e) {{}}
    }}
    function saveCalcSettings() {{
      try {{
        localStorage.setItem('qm_calc_settings', JSON.stringify({{
          account: calcAccountEl.value, risk: calcRiskEl.value, adrMult: calcAdrMultEl.value
        }}));
      }} catch (e) {{}}
    }}
    function findColIndex(table, headerText) {{
      var headers = table.querySelectorAll('th');
      for (var i = 0; i < headers.length; i++) {{
        if (headers[i].innerText.trim().toLowerCase() === headerText) return i;
      }}
      return -1;
    }}
    function addCalcColumns() {{
      var account = parseFloat(calcAccountEl.value) || 0;
      var riskPct = parseFloat(calcRiskEl.value) || 0;
      var adrMult = parseFloat(calcAdrMultEl.value) || 0;
      var riskDollars = account * (riskPct / 100);

      document.querySelectorAll('table.data-table').forEach(function(table) {{
        var closeIdx = findColIndex(table, 'close');
        var adrIdx = findColIndex(table, 'adr_pct');
        if (closeIdx === -1 || adrIdx === -1) return;

        var thead = table.querySelector('thead tr');
        if (!table.querySelector('th.calc-stop-header')) {{
          var stopTh = document.createElement('th');
          stopTh.innerText = 'Stop';
          stopTh.className = 'calc-stop-header';
          var sharesTh = document.createElement('th');
          sharesTh.innerText = 'Shares';
          sharesTh.className = 'calc-shares-header';
          thead.appendChild(stopTh);
          thead.appendChild(sharesTh);
        }}

        table.querySelectorAll('tbody tr').forEach(function(row) {{
          var close = parseFloat(row.children[closeIdx].innerText.replace(/[^0-9.\\-]/g, ''));
          var adr = parseFloat(row.children[adrIdx].innerText.replace(/[^0-9.\\-]/g, ''));
          var stopCell = row.querySelector('td.calc-stop-cell');
          var sharesCell = row.querySelector('td.calc-shares-cell');
          if (!stopCell) {{
            stopCell = document.createElement('td');
            stopCell.className = 'calc-stop-cell';
            sharesCell = document.createElement('td');
            sharesCell.className = 'calc-shares-cell';
            row.appendChild(stopCell);
            row.appendChild(sharesCell);
          }}
          if (isNaN(close) || isNaN(adr) || close <= 0 || adr <= 0 || riskDollars <= 0) {{
            stopCell.innerText = '-';
            sharesCell.innerText = '-';
            sharesCell.classList.remove('calc-warn');
            sharesCell.title = '';
            return;
          }}
          var stop = close * (1 - (adr * adrMult) / 100);
          var riskPerShare = close - stop;
          var shares = riskPerShare > 0 ? Math.floor(riskDollars / riskPerShare) : 0;
          var positionValue = shares * close;
          stopCell.innerText = '$' + stop.toFixed(2);
          sharesCell.innerText = shares.toLocaleString();
          var isTooBig = account > 0 && positionValue > account * 0.3;
          sharesCell.classList.toggle('calc-warn', isTooBig);
          sharesCell.title = 'Position value ~$' + Math.round(positionValue).toLocaleString() +
            (isTooBig ? " - exceeds 30% of account (Kris's overnight cap)" : '');
        }});
      }});
    }}
    if (calcAccountEl) {{
      restoreCalcSettings();
      addCalcColumns();
      [calcAccountEl, calcRiskEl, calcAdrMultEl].forEach(function(el) {{
        el.addEventListener('input', function() {{ saveCalcSettings(); addCalcColumns(); }});
      }});
    }}
  }})();
  </script>
</body>
</html>"""
    out_path.write_text(html, encoding="utf-8")


# =============================================================================
# MAIN
# =============================================================================
def main():
    ap = argparse.ArgumentParser(description="Qullamaggie Stage-1 daily scanner")
    ap.add_argument("--limit", type=int, default=600,
                     help="Max number of tickers to scan (start small, yfinance rate-limits).")
    ap.add_argument("--workers", type=int, default=8, help="Parallel download threads.")
    ap.add_argument("--batch-size", type=int, default=50, help="Tickers per yfinance batch call.")
    ap.add_argument("--out", type=str,
                     default=str(Path(__file__).resolve().parent / "scan_results"),
                     help="Output directory. Defaults to a 'scan_results' folder next to this "
                          "script (not the current working directory, which can be ambiguous "
                          "when Python is launched via double-click).")
    ap.add_argument("--tickers-file", type=str, default=None,
                     help="Optional: path to a text file of tickers (one per line) instead of the full universe.")
    ap.add_argument("--min-score", type=int, default=DEFAULTS["min_score_to_report"],
                     help="Minimum 0-100 score (any setup) to include in CSV/watchlist/dashboard tables. "
                          "40=Monitor, 60=WATCH, 80=SETUP!.")
    ap.add_argument("--strict", action="store_true",
                     help="For the Breakout setup only: additionally require 2LYNCH quality "
                          "count >= 4/6 before counting a signal as confirmed.")
    ap.add_argument("--no-browser", action="store_true",
                     help="Don't automatically open the dashboard in your browser when done.")
    ap.add_argument("--no-alpaca", action="store_true",
                     help="Force yfinance even if alpaca_config.json is set up - useful for "
                          "comparing the two data sources or debugging.")
    ap.add_argument("--universe-source", choices=["auto", "sec", "fallback"], default="sec",
                     help="Which ticker-universe source to use. 'sec' (now the default here since "
                          "Nasdaq Trader is confirmed reliably blocked on this network) skips "
                          "straight to SEC's ticker list. 'auto' would try Nasdaq Trader first, "
                          "wasting 1-2 minutes of retries every run before falling through anyway.")
    ap.add_argument("--refresh-universe", action="store_true",
                     help="Rebuild the cached liquid-ticker universe instead of running a scan. "
                          "Slower (downloads price history for a large raw pool to rank by "
                          "liquidity), meant to be run occasionally (e.g. monthly), not daily. "
                          "Normal scans will then use this cached list every day - consistent "
                          "coverage instead of a different random sample each run.")
    ap.add_argument("--universe-pool-size", type=int, default=DEFAULTS["universe_pool_size"],
                     help="During --refresh-universe: how many raw tickers to pull and rank. "
                          "0 (the default) means no cap - rank the ENTIRE available universe. "
                          "This is deliberately the default: capping this can miss genuinely "
                          "liquid names (even megacaps) by chance, since a random sample skews "
                          "toward the thousands of illiquid tickers that dominate by sheer count. "
                          "Only set this if you want a faster (less complete) refresh.")
    ap.add_argument("--universe-keep-size", type=int, default=DEFAULTS["universe_keep_size"],
                     help="During --refresh-universe: how many of the most liquid tickers to keep.")
    ap.add_argument("--trace", type=str, default="",
                     help="During --refresh-universe: comma-separated tickers to print stage-by-"
                          "stage diagnostics for (e.g. --trace LLY,TNET), to see exactly where/why "
                          "a specific ticker drops out of the pipeline instead of guessing.")
    args = ap.parse_args()

    if args.no_alpaca:
        global _alpaca_unavailable
        _alpaca_unavailable = True
        print("--no-alpaca set: forcing yfinance even if alpaca_config.json is present.")

    if args.refresh_universe:
        pool_desc = "the full available universe (no cap)" if args.universe_pool_size <= 0 else f"up to {args.universe_pool_size} tickers"
        print("=== Refreshing the liquid ticker universe cache ===")
        print(f"(Pool: {pool_desc}, keeping top {args.universe_keep_size} by liquidity)\n")
        trace = tuple(t.strip().upper() for t in args.trace.split(",") if t.strip()) if args.trace else ()
        kept, pool_scanned = build_liquid_universe(
            DEFAULTS, args.universe_source, args.universe_pool_size, args.universe_keep_size,
            workers=args.workers, batch_size=args.batch_size, trace_tickers=trace,
        )
        save_universe_cache(kept, pool_scanned=pool_scanned)
        print(f"\nSaved {len(kept)} liquid tickers (out of {pool_scanned} scanned) to: {UNIVERSE_CACHE_PATH}")
        print("Normal scans (qullamaggie_scanner.py) will now use this cached universe automatically.")
        print("Re-run this refresh occasionally (e.g. monthly) to keep it current.")
        return

    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        # some permission issues (notably Windows "Controlled folder access" on
        # Desktop/Documents/OneDrive) let mkdir succeed but block writes, so
        # verify we can actually write here before trusting this location.
        probe = out_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except (PermissionError, OSError) as e:
        fallback_dir = Path.home() / "QullamaggieScans"
        print(f"[WARN] Couldn't write to '{out_dir}' ({e}).")
        print(f"       This is a Windows permissions issue - possible causes include:")
        print(f"       - Windows 'Controlled folder access' (check: Windows Security > Virus &")
        print(f"         threat protection > Manage ransomware protection)")
        print(f"       - Third-party antivirus with its own folder/ransomware protection")
        print(f"       - A stray file (not folder) already named '{out_dir.name}' in this location")
        print(f"       - NTFS permissions on this specific folder")
        print(f"       Falling back to: {fallback_dir}")
        print(f"       Or just keep using --out '{fallback_dir}' going forward.")
        out_dir = fallback_dir
        out_dir.mkdir(parents=True, exist_ok=True)

    if args.tickers_file:
        tickers = [t.strip().upper() for t in Path(args.tickers_file).read_text().splitlines() if t.strip()]
    else:
        cache = load_universe_cache()
        if cache:
            tickers = cache["tickers"]
            print(f"Using cached liquid universe: {len(tickers)} tickers "
                  f"(refreshed {cache['generated']}, {cache['age_days']} days ago)")
            if cache["age_days"] > DEFAULTS["universe_cache_warn_days"]:
                print(f"[WARN] Cache is over {DEFAULTS['universe_cache_warn_days']} days old. Consider running "
                      f"refresh_universe.py (or 'python qullamaggie_scanner.py --refresh-universe') to update it.")
        else:
            print("No universe cache found. Doing a live (randomly-sampled) fetch instead - results")
            print("may vary run to run. For consistent daily coverage, run refresh_universe.py once")
            print("(or 'python qullamaggie_scanner.py --refresh-universe') to build a cached, liquidity-")
            print("filtered universe that every future scan will reuse.")
            tickers = fetch_universe(limit=args.limit, source_pref=args.universe_source)
    print(f"Universe size: {len(tickers)}")

    print("Checking market regime (SPY)...")
    regime = get_market_regime()
    print(f"  Regime: {'BULLISH' if regime.get('bullish') else 'BEARISH/UNKNOWN'}")

    print(f"Downloading price history for {len(tickers)} tickers "
          f"(batch_size={args.batch_size}, workers={args.workers})...")
    data = download_universe(tickers, DEFAULTS["history_period"],
                              batch_size=args.batch_size, workers=args.workers)
    print(f"Got usable data for {len(data)}/{len(tickers)} tickers.")

    print("Computing setup signals...")
    rows = []
    for t, df in data.items():
        try:
            r = compute_signals(t, df, DEFAULTS)
            if r:
                rows.append(r)
        except Exception as e:
            print(f"[WARN] {t}: {e}")

    results_df = pd.DataFrame(rows)
    if results_df.empty:
        print("No results computed. Check your data source / network connection.")
        return

    if args.strict:
        # gate the boolean breakout signal on 2LYNCH quality (>=4/6) in addition to the raw criteria
        results_df["breakout"] = results_df["breakout"] & (results_df["lynch_count"] >= 4)

    # candidate = scored >=min_score on ANY setup, OR a confirmed boolean signal on any setup
    scored_hit = (
        (results_df["htf_score"] >= args.min_score)
        | (results_df["ep_score"] >= args.min_score)
        | (results_df["pshort_score"] >= args.min_score)
    )
    boolean_hit = results_df[["breakout", "episodic_pivot", "parabolic_short", "parabolic_long"]].any(axis=1)
    hits_df = results_df[scored_hit | boolean_hit].copy()
    hits_df["best_score"] = hits_df[["htf_score", "ep_score", "pshort_score"]].max(axis=1)
    hits_df = hits_df.sort_values("best_score", ascending=False)

    csv_path = out_dir / f"qullamaggie_scan_{dt.date.today().isoformat()}.csv"
    hits_df.to_csv(csv_path, index=False)

    watchlist_path = out_dir / f"tradingview_watchlist_{dt.date.today().isoformat()}.txt"
    # Bare tickers, not exchange-prefixed - we were previously prefixing every
    # ticker with "NASDAQ:" regardless of its actual exchange, which is wrong
    # for NYSE-listed names (e.g. LLY). TradingView resolves bare tickers fine
    # across NYSE/NASDAQ/AMEX, so this is both more correct and simpler.
    watchlist_path.write_text(",".join(hits_df["ticker"]), encoding="utf-8")

    recent_df = update_recent_triggers(
        results_df, out_dir,
        lookback_days=DEFAULTS["recent_triggers_lookback_days"],
        min_score=DEFAULTS["recent_triggers_min_score"],
    )

    html_path = out_dir / f"dashboard_{dt.date.today().isoformat()}.html"
    build_html_dashboard(results_df, regime, html_path, min_score=args.min_score, recent_df=recent_df)
    # also keep a stable-named copy so guide.html (and shortcuts) can always
    # link to "the latest scan" without needing to know today's date
    latest_path = out_dir / "dashboard_latest.html"
    latest_path.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")

    # Keep guide.html alongside the dashboard so their relative links to each
    # other work. Source lives next to this script; harmless no-op if absent.
    guide_src = Path(__file__).resolve().parent / "guide.html"
    if guide_src.exists():
        (out_dir / "guide.html").write_text(guide_src.read_text(encoding="utf-8"), encoding="utf-8")

    def tier_counts(score_col):
        s = results_df[score_col]
        return {
            "SETUP!": int((s >= 80).sum()),
            "WATCH": int(((s >= 60) & (s < 80)).sum()),
            "Monitor": int(((s >= 40) & (s < 60)).sum()),
        }

    bo_tiers = tier_counts("htf_score")
    ep_tiers = tier_counts("ep_score")
    ps_tiers = tier_counts("pshort_score")

    print("\n=== DONE ===")
    print(f"  Tickers scanned:            {len(results_df)}")
    print(f"  Total candidates (score>={args.min_score} or confirmed): {len(hits_df)}")
    print()
    print(f"  Breakout   -> SETUP!:{bo_tiers['SETUP!']:>3}  WATCH:{bo_tiers['WATCH']:>3}  "
          f"Monitor:{bo_tiers['Monitor']:>3}  (confirmed boolean: {int(results_df['breakout'].sum())})")
    print(f"  Episodic P -> SETUP!:{ep_tiers['SETUP!']:>3}  WATCH:{ep_tiers['WATCH']:>3}  "
          f"Monitor:{ep_tiers['Monitor']:>3}  (confirmed boolean: {int(results_df['episodic_pivot'].sum())})")
    print(f"  Parabolic S-> SETUP!:{ps_tiers['SETUP!']:>3}  WATCH:{ps_tiers['WATCH']:>3}  "
          f"Monitor:{ps_tiers['Monitor']:>3}  (confirmed boolean: {int(results_df['parabolic_short'].sum())})")
    print(f"  Parabolic Long candidates (boolean only): {int(results_df['parabolic_long'].sum())}")
    print(f"  Recently Triggered (score>={DEFAULTS['recent_triggers_min_score']}, last {DEFAULTS['recent_triggers_lookback_days']}d): {len(recent_df)}")
    print(f"\n  CSV:       {csv_path}")
    print(f"  Watchlist: {watchlist_path}  (import this into TradingView)")
    print(f"  Dashboard: {html_path}  (open in a browser)")

    if not args.no_browser:
        try:
            webbrowser.open(latest_path.resolve().as_uri())
        except Exception:
            pass  # non-fatal; the path was already printed above


if __name__ == "__main__":
    # Keep the console window open on Windows when the script is double-clicked
    # directly (no wrapper), so errors/output are visible instead of flashing shut.
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception:
        traceback.print_exc()
    finally:
        if sys.stdout.isatty() and sys.platform.startswith("win"):
            try:
                input("\nPress Enter to close this window...")
            except Exception:
                pass

# === TELEGRAM AUTO-SEND ===
import os as _os, glob as _glob, csv as _csv, requests as _requests
try:
    _files = sorted(_glob.glob('/opt/render/project/src/scan_results/qullamaggie_scan_*.csv'))
    if _files:
        with open(_files[-1]) as _f:
            _rows = list(_csv.DictReader(_f))
        _hits = [r for r in _rows if float(r.get('best_score') or 0) >= 75]
        _msg = f"Qullamaggie Scan: {len(_hits)} hits >= 75\n\n" + "\n".join(f"{r['ticker']} | score {r['best_score']} | ${r['close']}" for r in _hits[:20])
        _r = _requests.post(f"https://api.telegram.org/bot{_os.environ.get('TELEGRAM_BOT_TOKEN')}/sendMessage", data={'chat_id': _os.environ.get('TELEGRAM_CHAT_ID'), 'text': _msg})
        print(f"Telegram sent: {_r.status_code}")
except Exception as _e:
    print(f"Telegram error: {_e}")
