import os, json, requests, datetime
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# === CONFIG ===
PRICE_MAX = 50.0
DAYS_MIN = 25
DAYS_MAX = 90
MAX_RESULTS = 15
PIPEWORX_URL = "https://gateway.pipeworx.io/regulatory-catalysts/mcp"

# === FETCH PDUFA DATA ===
payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "pdufa_catalysts", "arguments": {}}, "id": 1}
r = requests.post(PIPEWORX_URL, json=payload, timeout=30)
data = json.loads(r.json()["result"]["content"][0]["text"])
events = data.get("catalysts", [])
print(f"Total PDUFA events: {len(events)}")

# === FILTER BY DATE ===
today = datetime.date.today()
filtered = []
for e in events:
    d_str = e.get("pdufa_date")
    if not d_str:
        continue
    try:
        d = datetime.date.fromisoformat(d_str)
    except Exception:
        continue
    days_out = (d - today).days
    if DAYS_MIN <= days_out <= DAYS_MAX:
        filtered.append({**e, "days_out": days_out, "pdufa_date_obj": d})
print(f"After date filter ({DAYS_MIN}-{DAYS_MAX} days): {len(filtered)}")

# === FETCH PRICES FROM ALPACA ===
cfg = json.load(open("/opt/render/project/src/alpaca_config.json"))
client = StockHistoricalDataClient(cfg["api_key"], cfg["secret_key"])

priced = []
for e in filtered:
    ticker = e.get("ticker")
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
    msg = f"FDA Scanner ({today}): 0 candidates\n\nNo PDUFA events in {DAYS_MIN}-{DAYS_MAX} day window under ${PRICE_MAX}"
else:
    lines = [f"FDA Scanner ({today})", f"{len(final)} candidates (PDUFA in {DAYS_MIN}-{DAYS_MAX} days, <= ${PRICE_MAX})", ""]
    for e in final:
        lines.append(f"{e['ticker']} | ${e['price']:.2f} | {e['pdufa_date']} ({e['days_out']}d) | {e.get('company','')[:40]}")
    msg = "\n".join(lines)

print(msg)

# === SEND TO TELEGRAM ===
token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat = os.environ.get("TELEGRAM_CHAT_ID")
tg = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": msg})
print(f"Telegram status: {tg.status_code}")
