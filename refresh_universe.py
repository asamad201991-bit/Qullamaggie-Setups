#!/usr/bin/env python3
"""
Refresh the cached liquid-ticker universe.
===========================================
Double-click this file occasionally (e.g. once a month) to rebuild the list
of liquid tickers that qullamaggie_scanner.py scans every day.

This is the SLOW one - it pulls a large raw pool of tickers (4,000 by
default) and downloads price history for all of them just to rank by
liquidity, so it can take a while. That's expected; you're not meant to run
this daily. Your normal day-to-day scans should keep using
qullamaggie_scanner.py as usual, which will automatically pick up whatever
this produces.

Why this exists: without a cache, each daily scan would only look at a
random ~600-1200 ticker slice of the market, which could differ from run to
run (a stock present in one scan might just not have been drawn in the next
one - not because it stopped qualifying, but because of the random sample).
This mirrors how real screening tools actually work: Kris scans his full
TC2000 universe every day; the most detailed public DIY replication of his
method pre-filters to ~1,500 liquid names once a month and scans that same
full set daily. This script builds that same kind of stable, pre-filtered
universe rather than resampling randomly every run.
"""
import sys
import traceback

sys.argv = [sys.argv[0], "--refresh-universe"]

try:
    import qullamaggie_scanner
    qullamaggie_scanner.main()
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
