import os, json, requests, datetime, time
import yfinance as yf

# === CONFIG ===
PRICE_MIN = 5.0
PRICE_MAX = 50.0
DAYS_MIN = 30
DAYS_MAX = 90
MAX_RESULTS = 20
MAX_PER_DATE = 3
MAX_CANDIDATES = 200
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")

# === RATE-LIMITED FINNHUB CALL ===
_last_call = [0.0]
def finnhub_get(url):
    elapsed = time.time() - _last_call[0]
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    try:
        r = requests.get(url, timeout=15)
        _last_call[0] = time.time()
        return r.json()
    except Exception:
        return {}

# === FETCH EARNINGS DATA ===
today = datetime.date.today()
start = (today + datetime.timedelta(days=DAYS_MIN)).isoformat()
end = (today + datetime.timedelta(days=DAYS_MAX)).isoformat()
url = f"https://finnhub.io/api/v1/calendar/earnings?from={start}&to={end}&token={FINNHUB_KEY}"
data = finnhub_get(url)
events = data.get("earningsCalendar", [])
print(f"Total earnings events: {len(events)}")

# === FILTER: date, EPS estimate present, meaningful magnitude ===
filtered = []
for e in events:
    d_str = e.get("date")
    if not d_str:
        continue
    eps = e.get("epsEstimate")
    if eps is None or abs(eps) < 0.20:
        continue
    try:
        d = datetime.date.fromisoformat(d_str)
    except Exception:
        continue
    days_out = (d - today).days
    if DAYS_MIN <= days_out <= DAYS_MAX:
        filtered.append({**e, "days_out": days_out})
print(f"After date + EPS magnitude filter: {len(filtered)}")

filtered = filtered[:MAX_CANDIDATES]
print(f"Processing first {len(filtered)} candidates")

# === FETCH PRICE via FINNHUB QUOTE ===
for e in filtered:
    q = finnhub_get(f"https://finnhub.io/api/v1/quote?symbol={e['symbol']}&token={FINNHUB_KEY}")
    e["price"] = q.get("c")

priced = [e for e in filtered if e.get("price") and PRICE_MIN <= e["price"] <= PRICE_MAX]
print(f"After price filter (${PRICE_MIN}-${PRICE_MAX}): {len(priced)}")

# === FETCH METRICS via FINNHUB ===
for e in priced:
    m = finnhub_get(f"https://finnhub.io/api/v1/stock/metric?symbol={e['symbol']}&metric=all&token={FINNHUB_KEY}").get("metric", {})
    e["52w_high"] = m.get("52WeekHigh")
    e["52w_low"] = m.get("52WeekLow")
    e["pe"] = m.get("peTTM")
    e["pb"] = m.get("pb")
    e["rev_growth"] = m.get("revenueGrowthTTMYoy")
    e["eps_growth"] = m.get("epsGrowthTTMYoy")

# === FETCH VOLUME TREND via yfinance ===
for e in priced:
    try:
        df = yf.Ticker(e["symbol"]).history(period="3mo", interval="1d")
        if df is not None and len(df) >= 25:
            v20 = float(df["Volume"].tail(20).mean())
            v90 = float(df["Volume"].mean())
            if v90 > 0:
                e["vol_ratio"] = round(v20 / v90, 2)
    except Exception:
        pass

# === COMPUTE 52W POSITION ===
for e in priced:
    high, low, price = e.get("52w_high"), e.get("52w_low"), e.get("price")
    if high and low and price and high > low:
        e["52w_pos"] = round((price - low) / (high - low) * 100, 1)
        e["pct_below_high"] = round((high - price) / high * 100, 1)
    else:
        e["52w_pos"] = None
        e["pct_below_high"] = None

# === SCORING (0-100) ===
def score(e):
    s = 0
    pos = e.get("52w_pos")
    if pos is not None:
        if pos <= 15: s += 35
        elif pos <= 30: s += 28
        elif pos <= 50: s += 20
        elif pos <= 70: s += 10
    rg = e.get("rev_growth")
    if rg is not None:
        if rg >= 20: s += 20
        elif rg >= 10: s += 15
        elif rg >= 0: s += 10
    eg = e.get("eps_growth")
    if eg is not None:
        if eg >= 20: s += 20
        elif eg >= 10: s += 15
        elif eg >= 0: s += 10
    pe = e.get("pe")
    if pe is not None and pe > 0:
        if pe <= 15: s += 15
        elif pe <= 25: s += 10
        elif pe <= 40: s += 5
    vr = e.get("vol_ratio")
    if vr is not None:
        s += 5
        if vr >= 1.2: s += 5
        elif vr <= 0.7: s += 3
    return s

for e in priced:
    e["score"] = score(e)

# === SORT, CAP PER DATE ===
priced.sort(key=lambda x: x["score"], reverse=True)
final = []
date_counts = {}
for e in priced:
    d = e["date"]
    if date_counts.get(d, 0) >= MAX_PER_DATE:
        continue
    final.append(e)
    date_counts[d] = date_counts.get(d, 0) + 1
    if len(final) >= MAX_RESULTS:
        break

# === FORMAT MESSAGE ===
if not final:
    msg = f"Earnings Scanner ({today}): 0 candidates"
else:
    lines = [f"Earnings Scanner ({today})", f"{len(final)} candidates (${PRICE_MIN}-${PRICE_MAX})", ""]
    for e in final:
        pe_str = f"PE:{e['pe']:.0f}" if e.get("pe") else "PE:-"
        pos_str = f"52W:{e['52w_pos']:.0f}%" if e.get("52w_pos") is not None else "52W:-"
        vr_str = f"V:{e['vol_ratio']:.1f}x" if e.get("vol_ratio") else "V:-"
        rg = e.get("rev_growth")
        eg = e.get("eps_growth")
        rg_str = f"Rev:{rg:+.0f}%" if rg is not None else "Rev:-"
        eg_str = f"EPS:{eg:+.0f}%" if eg is not None else "EPS:-"
        lines.append(f"{e['symbol']} ${e['price']:.2f} {e['date']}({e['days_out']}d) S{e['score']}")
        lines.append(f"  {pos_str} {pe_str} {rg_str} {eg_str} {vr_str}")
    msg = "\n".join(lines)

print(msg)

# === SEND TO TELEGRAM ===
token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat = os.environ.get("TELEGRAM_CHAT_ID")
tg = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": msg})
print(f"Telegram status: {tg.status_code}")
