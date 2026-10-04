import os, json, requests, datetime

# === CONFIG ===
PRICE_MIN = 1.0
PRICE_MAX = 50.0
DAYS_MIN = 25
DAYS_MAX = 90
MAX_RESULTS = 15
PIPEWORX_URL = "https://gateway.pipeworx.io/regulatory-catalysts/mcp"
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")

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
        filtered.append({**e, "days_out": days_out})
print(f"After date filter ({DAYS_MIN}-{DAYS_MAX} days): {len(filtered)}")

# === FETCH PRICES FROM FINNHUB ===
def finnhub_quote(ticker):
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=15)
        return r.json().get("c")
    except Exception:
        return None

priced = []
for e in filtered:
    ticker = e.get("ticker")
    if not ticker:
        continue
    price = finnhub_quote(ticker)
    if price and PRICE_MIN <= price <= PRICE_MAX:
        priced.append({**e, "price": price})
print(f"After price filter (${PRICE_MIN}-${PRICE_MAX}): {len(priced)}")

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
