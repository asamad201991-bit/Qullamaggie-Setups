import os, json, requests, datetime
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# === CONFIG ===
PRICE_MIN = 5.0
PRICE_MAX = 50.0
DAYS_MIN = 25
DAYS_MAX = 90
MAX_RESULTS = 15
MAX_PER_DATE = 3
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

# === FILTER BY DATE + BASIC QUALITY ===
filtered = []
for e in events:
    d_str = e.get("date")
    if not d_str:
        continue
    # Require an EPS estimate (analyst coverage exists)
    if e.get("epsEstimate") is None:
        continue
    try:
        d = datetime.date.fromisoformat(d_str)
    except Exception:
        continue
    days_out = (d - today).days
    if DAYS_MIN <= days_out <= DAYS_MAX:
        filtered.append({**e, "days_out": days_out})
print(f"After date + coverage filter ({DAYS_MIN}-{DAYS_MAX} days): {len(filtered)}")

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
        if price < PRICE_MIN or price > PRICE_MAX:
            continue
        priced.append({**e, "price": price})
    except Exception:
        continue
print(f"After price filter (${PRICE_MIN}-${PRICE_MAX}): {len(priced)}")

# === SCORE EACH CANDIDATE (0-100) ===
def score_candidate(c):
    # Revenue component (0-40)
    rev = c.get("revenueEstimate")
    if rev is None or rev <= 0:
        rev_score = 0
    elif rev >= 500_000_000:
        rev_score = 40
    elif rev >= 100_000_000:
        rev_score = 30
    elif rev >= 50_000_000:
        rev_score = 20
    elif rev >= 10_000_000:
        rev_score = 10
    else:
        rev_score = 5
    
    # EPS component (0-30) — only positive EPS earns points
    eps = c.get("epsEstimate")
    if eps is None or eps <= 0:
        eps_score = 0
    elif eps >= 2.0:
        eps_score = 30
    elif eps >= 1.0:
        eps_score = 25
    elif eps >= 0.5:
        eps_score = 20
    elif eps >= 0.2:
        eps_score = 15
    elif eps >= 0.1:
        eps_score = 10
    else:
        eps_score = 5
    
    # Days-out component (0-30) — sooner = higher
    days = c["days_out"]
    if days <= 30:
        days_score = 30
    elif days <= 45:
        days_score = 25
    elif days <= 60:
        days_score = 20
    elif days <= 75:
        days_score = 15
    else:
        days_score = 10
    
    return rev_score + eps_score + days_score

for c in priced:
    c["score"] = score_candidate(c)

# === SORT BY SCORE, SOFT CAP 3 PER DATE ===
priced.sort(key=lambda x: x["score"], reverse=True)
final = []
date_counts = {}
for c in priced:
    d = c["date"]
    if date_counts.get(d, 0) >= MAX_PER_DATE:
        continue
    final.append(c)
    date_counts[d] = date_counts.get(d, 0) + 1
    if len(final) >= MAX_RESULTS:
        break

# === FORMAT MESSAGE ===
if not final:
    msg = f"Earnings Scanner ({today}): 0 candidates\n\nNo quality earnings in {DAYS_MIN}-{DAYS_MAX} day window"
else:
    lines = [f"Earnings Scanner ({today})", f"{len(final)} candidates (${PRICE_MIN}-${PRICE_MAX}, ranked by quality score)", ""]
    for e in final:
        hour = e.get("hour", "") or "-"
        eps = e.get("epsEstimate")
        eps_str = f"EPS ${eps:.2f}" if eps is not None else "EPS -"
        rev = e.get("revenueEstimate")
        if rev and rev > 0:
            rev_str = f"Rev ${rev/1e6:.0f}M"
        else:
            rev_str = "Rev -"
        lines.append(f"{e['symbol']} | ${e['price']:.2f} | {e['date']} ({e['days_out']}d) | score {e['score']} | {eps_str} | {rev_str}")
    msg = "\n".join(lines)

print(msg)

# === SEND TO TELEGRAM ===
token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat = os.environ.get("TELEGRAM_CHAT_ID")
tg = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": msg})
print(f"Telegram status: {tg.status_code}")
