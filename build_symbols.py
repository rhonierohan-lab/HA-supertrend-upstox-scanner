import gzip, json, requests, pandas as pd
URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
r = requests.get(URL, timeout=60); r.raise_for_status()
data = json.loads(gzip.decompress(r.content).decode())
rows = [
    {"trading_symbol": x.get("trading_symbol"), "instrument_key": x.get("instrument_key")}
    for x in data
    if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
]
pd.DataFrame(rows).drop_duplicates().sort_values("trading_symbol").to_csv("symbols.csv", index=False)
print(f"Wrote {len(rows)} NSE EQ symbols to symbols.csv")
