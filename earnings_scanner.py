import os, json, requests, datetime
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# === CONFIG ===
PRICE_MAX = 50.0
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

# === FILTER BY DATE ===
filtered = []
for e in events:
    d_str = e.get("date")
    if not d_str:
        continue
    try:
        d = datetime.date.fromisoformat(d_str)
    except Exception:
        continue
    days_out = (d - today).days
    if DAYS_MIN <= days_out <= DAYS_MAX:
        filtered.append({**e, "days_out": days_out})
print(f"After date filter ({DAYS_MIN}-{DAYS_MAX} days): {len(filtered)}")

# === FETCH PRICES FROM ALPACA ===
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
        if price <= PRICE_MAX:
            priced.append({**e, "price": price})
    except Exception:
        continue
print(f"After price filter (<= ${PRICE_MAX}): {len(priced)}")

# === SORT AND TRIM ===
priced.sort(key=lambda x: x["days_out"])
final = priced[:MAX_RESULTS]

# === FORMAT MESSAGE ===
if not final:
    msg = f"Earnings Scanner ({today}): 0 candidates\n\nNo earnings in {DAYS_MIN}-{DAYS_MAX} day window under ${PRICE_MAX}"
else:
    lines = [f"Earnings Scanner ({today})", f"{len(final)} candidates (earnings in {DAYS_MIN}-{DAYS_MAX} days, <= ${PRICE_MAX})", ""]
    for e in final:
        hour = e.get("hour", "") or "-"
        eps = e.get("epsEstimate")
        eps_str = f"EPS est ${eps:.2f}" if eps is not None else "no est"
        lines.append(f"{e['symbol']} | ${e['price']:.2f} | {e['date']} ({e['days_out']}d, {hour}) | {eps_str}")
    msg = "\n".join(lines)

print(msg)

# === SEND TO TELEGRAM ===
token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat = os.environ.get("TELEGRAM_CHAT_ID")
tg = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": msg})
print(f"Telegram status: {tg.status_code}")
