import os, json, requests, datetime
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# === CONFIG ===
PRICE_MIN = 5.0
PRICE_MAX = 50.0
ADV_MIN = 2_000_000
DAYS_MIN = 25
DAYS_MAX = 90
MAX_RESULTS = 15
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")

# === FETCH EARNINGS DATA ===
today = datetime.date.today()
start = (today + datetime.timedelta(days=DAYS_MIN)).isoformat()
end = (today + datetime.timedelta(days=DAYS_MAX)).isoformat()
url = f"https://finnhub.io/api/v1/calendar/earnings?from={start}&to={end}&token={FINNHUB_KEY}"
r = requests.get(url, timeout=30)
data = r.json()
events = data.get("earningsCalendar", [])
print(f"Total earnings events: {len(events)}")

# === FILTER BY DATE AND EPS ESTIMATE ===
filtered = []
for e in events:
    d_str = e.get("date")
    if not d_str:
        continue
    if e.get("epsEstimate") is None:
        continue
    try:
        d = datetime.date.fromisoformat(d_str)
    except Exception:
        continue
    days_out = (d - today).days
    if DAYS_MIN <= days_out <= DAYS_MAX:
        filtered.append({**e, "days_out": days_out})
print(f"After date + EPS filter ({DAYS_MIN}-{DAYS_MAX} days): {len(filtered)}")

# === FETCH PRICES AND VOLUME FROM ALPACA ===
cfg = json.load(open("/opt/render/project/src/alpaca_config.json"))
client = StockHistoricalDataClient(cfg["api_key"], cfg["secret_key"])

priced = []
for e in filtered:
    ticker = e.get("symbol")
    if not ticker:
        continue
    try:
        tr = client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
        price = float(tr[ticker].price)
        if price < PRICE_MIN or price > PRICE_MAX:
            continue
        end_dt = datetime.datetime.now(datetime.timezone.utc)
        start_dt = end_dt - datetime.timedelta(days=30)
        req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=start_dt, end=end_dt)
        bars = client.get_stock_bars(req)
        df = bars.df
        if df is None or df.empty:
            continue
        if ticker in df.index.get_level_values(0):
            sub = df.xs(ticker, level=0)
        else:
            sub = df
        if len(sub) < 10:
            continue
        adv = (sub["close"] * sub["volume"]).mean()
        if adv < ADV_MIN:
            continue
        priced.append({**e, "price": price, "adv_m": adv / 1e6})
    except Exception:
        continue
print(f"After quality filter (${PRICE_MIN}-${PRICE_MAX}, ADV>${ADV_MIN/1e6:.0f}M): {len(priced)}")

# === SORT AND TRIM ===
priced.sort(key=lambda x: x["days_out"])
final = priced[:MAX_RESULTS]

# === FORMAT MESSAGE ===
if not final:
    msg = f"Earnings Scanner ({today}): 0 candidates\n\nNo quality earnings in {DAYS_MIN}-{DAYS_MAX} day window"
else:
    lines = [f"Earnings Scanner ({today})", f"{len(final)} candidates (${PRICE_MIN}-${PRICE_MAX}, ADV>$2M, EPS est)", ""]
    for e in final:
        hour = e.get("hour", "") or "-"
        eps = e.get("epsEstimate")
        eps_str = f"EPS est ${eps:.2f}" if eps is not None else "no est"
        lines.append(f"{e['symbol']} | ${e['price']:.2f} | {e['date']} ({e['days_out']}d, {hour}) | {eps_str} | ADV ${e.get('adv_m', 0):.1f}M")
    msg = "\n".join(lines)

print(msg)

# === SEND TO TELEGRAM ===
token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat = os.environ.get("TELEGRAM_CHAT_ID")
tg = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": msg})
print(f"Telegram status: {tg.status_code}")
