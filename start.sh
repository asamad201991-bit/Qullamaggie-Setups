#!/bin/sh
while true; do
  # --- DAILY 12:30 AM Oman: Qullamaggie scan ---
  python -c "import time,datetime,zoneinfo; tz=zoneinfo.ZoneInfo('Asia/Muscat'); n=datetime.datetime.now(tz); t=n.replace(hour=0,minute=30,second=0,microsecond=0); t=t+datetime.timedelta(days=1) if n>=t else t; print(f'QG sleep {(t-n).total_seconds():.0f}s'); time.sleep((t-n).total_seconds())"
  python qullamaggie_scanner.py --universe-source sec

  # --- DAILY 6:00 AM Oman: FDA scan ---
  python -c "import time,datetime,zoneinfo; tz=zoneinfo.ZoneInfo('Asia/Muscat'); n=datetime.datetime.now(tz); t=n.replace(hour=6,minute=0,second=0,microsecond=0); t=t+datetime.timedelta(days=1) if n>=t else t; print(f'FDA sleep {(t-n).total_seconds():.0f}s'); time.sleep((t-n).total_seconds())"
  python fda_scanner.py

  # --- DAILY 6:30 AM Oman: Earnings scan ---
  python -c "import time,datetime,zoneinfo; tz=zoneinfo.ZoneInfo('Asia/Muscat'); n=datetime.datetime.now(tz); t=n.replace(hour=6,minute=30,second=0,microsecond=0); t=t+datetime.timedelta(days=1) if n>=t else t; print(f'Earnings sleep {(t-n).total_seconds():.0f}s'); time.sleep((t-n).total_seconds())"
  python earnings_scanner.py
done
