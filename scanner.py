import os
import json
import time
import gzip
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo
from urllib.parse import quote

import requests
import pandas as pd
import upstox_client
from dotenv import load_dotenv

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
HISTORY_URL = "https://api.upstox.com/v3/historical-candle/{instrument_key}/weeks/1/{to_date}/{from_date}"
OHLC_URL = "https://api.upstox.com/v3/market-quote/ohlc"
TELEGRAM_URL = "https://api.telegram.org/bot{}/sendMessage"

TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "500"))
HISTORY_WEEKS = int(os.getenv("HISTORY_WEEKS", "120"))
ST_PERIOD = int(os.getenv("ST_PERIOD", "1"))
ST_FACTOR = float(os.getenv("ST_FACTOR", "1"))
PRICE_MIN = float(os.getenv("PRICE_MIN", "0"))
PRICE_MAX = float(os.getenv("PRICE_MAX", "999999"))
SYMBOLS_FILE = os.getenv("SYMBOLS_FILE", "symbols.csv")
CACHE_DIR = os.getenv("CACHE_DIR", "cache")
SIGNAL_MODE = os.getenv("SIGNAL_MODE", "INTRAWEEK").upper()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("upstox-ha-st")


@dataclass
class SymbolState:
    symbol: str
    key: str
    weekly: pd.DataFrame
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    day_date: date | None = None
    week_ohlc: dict = field(default_factory=dict)
    last_ltp: float | None = None
    last_ltt: int | None = None
    alerted_week: str | None = None


def auth_headers():
    if not TOKEN:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN is missing")
    return {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"}


def download_instruments():
    r = requests.get(INSTRUMENT_URL, timeout=60)
    r.raise_for_status()
    raw = gzip.decompress(r.content)
    data = json.loads(raw.decode("utf-8"))
    rows = []
    for x in data:
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ":
            rows.append({
                "trading_symbol": x.get("trading_symbol"),
                "instrument_key": x.get("instrument_key"),
                "isin": x.get("isin", ""),
                "security_type": x.get("security_type", ""),
            })
    return rows


def load_symbols():
    if os.path.exists(SYMBOLS_FILE):
        df = pd.read_csv(SYMBOLS_FILE)
        required = {"trading_symbol", "instrument_key"}
        if not required.issubset(df.columns):
            raise ValueError(f"{SYMBOLS_FILE} must contain trading_symbol,instrument_key")
        rows = df[["trading_symbol", "instrument_key"]].dropna().to_dict("records")
    else:
        rows = download_instruments()
        pd.DataFrame(rows).to_csv(SYMBOLS_FILE, index=False)
        log.info("Downloaded NSE instrument master: %s equities", len(rows))

    # Optional price-universe filter is applied after the live seed, so keep all symbols here.
    if MAX_SYMBOLS > 0:
        rows = rows[:MAX_SYMBOLS]
    return rows


def history_for(key: str):
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = key.replace("|", "_")
    path = os.path.join(CACHE_DIR, safe + ".json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if obj.get("asof") == date.today().isoformat() and len(obj.get("candles", [])) >= 20:
                return obj["candles"]
        except Exception:
            pass

    to_date = date.today()
    from_date = to_date - timedelta(days=max(365, HISTORY_WEEKS * 8))
    url = HISTORY_URL.format(
        instrument_key=quote(key, safe=""),
        to_date=to_date.isoformat(),
        from_date=from_date.isoformat(),
    )
    r = requests.get(url, headers=auth_headers(), timeout=30)
    r.raise_for_status()
    candles = r.json().get("data", {}).get("candles", [])
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"asof": date.today().isoformat(), "candles": candles}, f)
    return candles


def candles_to_df(candles):
    rows = []
    for c in candles:
        rows.append({"ts": pd.to_datetime(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("ts").drop_duplicates("ts").set_index("ts")
    return df[["open", "high", "low", "close"]]


def heikin_ashi(df):
    ha = pd.DataFrame(index=df.index)
    ha["ha_close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open = []
    for i, row in enumerate(df.itertuples()):
        if i == 0:
            ha_open.append((row.open + row.close) / 2.0)
        else:
            ha_open.append((ha_open[-1] + ha["ha_close"].iloc[i-1]) / 2.0)
    ha["ha_open"] = ha_open
    ha["ha_high"] = pd.concat([df["high"], ha["ha_open"], ha["ha_close"]], axis=1).max(axis=1)
    ha["ha_low"] = pd.concat([df["low"], ha["ha_open"], ha["ha_close"]], axis=1).min(axis=1)
    return ha


def supertrend(df, period=1, factor=1.0):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high-low), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period, min_periods=period).mean()
    hl2 = (high + low) / 2.0
    upper_basic = hl2 + factor * atr
    lower_basic = hl2 - factor * atr
    upper = upper_basic.copy()
    lower = lower_basic.copy()
    direction = pd.Series(index=df.index, dtype="int64")
    st = pd.Series(index=df.index, dtype="float64")

    for i in range(len(df)):
        if pd.isna(atr.iloc[i]):
            continue
        if i == 0 or pd.isna(st.iloc[i-1]):
            upper.iloc[i] = upper_basic.iloc[i]
            lower.iloc[i] = lower_basic.iloc[i]
            direction.iloc[i] = 1
            st.iloc[i] = lower.iloc[i]
            continue
        if upper_basic.iloc[i] < upper.iloc[i-1] or close.iloc[i-1] > upper.iloc[i-1]:
            upper.iloc[i] = upper_basic.iloc[i]
        else:
            upper.iloc[i] = upper.iloc[i-1]
        if lower_basic.iloc[i] > lower.iloc[i-1] or close.iloc[i-1] < lower.iloc[i-1]:
            lower.iloc[i] = lower_basic.iloc[i]
        else:
            lower.iloc[i] = lower.iloc[i-1]
        if st.iloc[i-1] == upper.iloc[i-1]:
            direction.iloc[i] = -1 if close.iloc[i] > upper.iloc[i] else 1
        else:
            direction.iloc[i] = 1 if close.iloc[i] < lower.iloc[i] else -1
        st.iloc[i] = lower.iloc[i] if direction.iloc[i] == -1 else upper.iloc[i]

    return st


def signal_snapshot(weekly: pd.DataFrame):
    if len(weekly) < 3:
        return None
    ha = heikin_ashi(weekly)
    st = supertrend(weekly, ST_PERIOD, ST_FACTOR)
    out = pd.DataFrame({"ha_close": ha["ha_close"], "st": st}, index=weekly.index).dropna()
    if len(out) < 2:
        return None
    prev = out.iloc[-2]
    cur = out.iloc[-1]
    crossed = prev.ha_close <= prev.st and cur.ha_close > cur.st
    return crossed, float(cur.ha_close), float(cur.st), float(prev.ha_close), float(prev.st)


def telegram_send(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram credentials missing; signal: %s", text.replace("\n", " | "))
        return
    url = TELEGRAM_URL.format(TG_TOKEN)
    r = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=15)
    if not r.ok:
        log.error("Telegram error %s: %s", r.status_code, r.text[:500])


def extract_tick(message):
    """Best-effort parser for SDK-decoded V3 feed dictionaries.
    Returns (instrument_key, ltp, ltt_ms).
    """
    if not isinstance(message, dict):
        return None

    def walk(obj, key_hint=None):
        if isinstance(obj, dict):
            # Direct instrument-key container.
            for k, v in obj.items():
                if isinstance(k, str) and (k.startswith("NSE_EQ|") or k.startswith("NSE_EQ:")):
                    res = walk(v, k.replace(":", "|", 1))
                    if res:
                        return res
            ltp = None
            ltt = None
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in {"ltp", "last_price", "lastprice"} and isinstance(v, (int, float)):
                    ltp = float(v)
                elif kl in {"ltt", "last_trade_time", "lasttradetime"} and isinstance(v, (int, float)):
                    ltt = int(v)
                elif kl == "ltpc" and isinstance(v, dict):
                    rr = walk(v, key_hint)
                    if rr:
                        return rr
            if ltp is not None and key_hint:
                return key_hint, ltp, ltt
            for v in obj.values():
                res = walk(v, key_hint)
                if res:
                    return res
        elif isinstance(obj, list):
            for v in obj:
                res = walk(v, key_hint)
                if res:
                    return res
        return None

    return walk(message)


def seed_current_day(states):
    keys = list(states.keys())
    if not keys:
        return
    for start in range(0, len(keys), 500):
        batch = keys[start:start+500]
        params = {"instrument_key": ",".join(batch), "interval": "1d"}
        r = requests.get(OHLC_URL, headers=auth_headers(), params=params, timeout=30)
        r.raise_for_status()
        data = r.json().get("data", {})
        today = datetime.now(IST).date()
        for raw_key, obj in data.items():
            key = raw_key.replace(":", "|", 1)
            if key not in states:
                continue
            live = obj.get("live_ohlc") or {}
            if not live:
                continue
            states[key].day_date = today
            states[key].day_open = float(live.get("open")) if live.get("open") is not None else None
            states[key].day_high = float(live.get("high")) if live.get("high") is not None else None
            states[key].day_low = float(live.get("low")) if live.get("low") is not None else None
    log.info("Seeded current-day OHLC for %s symbols", sum(s.day_open is not None for s in states.values()))


def build_current_week(state: SymbolState, ltp: float):
    now = datetime.now(IST)
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    week_id = monday.isoformat()

    if state.week_ohlc.get("week_id") != week_id:
        # We need Monday's opening price. The current-day seed is used on Monday;
        # for a restart later in the week, historical weekly data supplies prior weeks
        # and the current week's OHLC is rebuilt from the day seed + tick updates.
        state.week_ohlc = {
            "week_id": week_id,
            "open": state.day_open if state.day_date == today else ltp,
            "high": state.day_high if state.day_date == today and state.day_high is not None else ltp,
            "low": state.day_low if state.day_date == today and state.day_low is not None else ltp,
            "close": ltp,
        }
    else:
        w = state.week_ohlc
        w["close"] = ltp
        w["high"] = max(float(w["high"]), ltp)
        w["low"] = min(float(w["low"]), ltp)

    return state.week_ohlc


def check_symbol(state: SymbolState, ltp: float, ltt_ms: int | None):
    if ltp < PRICE_MIN or ltp > PRICE_MAX:
        return
    w = build_current_week(state, ltp)
    if not w:
        return

    current = pd.DataFrame([{
        "open": w["open"], "high": w["high"], "low": w["low"], "close": w["close"]
    }], index=[pd.Timestamp(w["week_id"], tz=IST)])
    weekly = pd.concat([state.weekly, current])
    weekly = weekly[~weekly.index.duplicated(keep="last")].sort_index()

    snap = signal_snapshot(weekly)
    if not snap:
        return
    crossed, ha_close, st_line, prev_ha, prev_st = snap
    week_id = w["week_id"]
    if crossed and state.alerted_week != week_id:
        state.alerted_week = week_id
        if ltt_ms:
            dt = datetime.fromtimestamp(ltt_ms / 1000.0, tz=IST)
        else:
            dt = datetime.now(IST)
        text = (
            "🟢 WEEKLY HA CLOSE CROSSOVER\n"
            f"Symbol: {state.symbol}\n"
            f"Entry price (LTP): {ltp:.2f}\n"
            f"Signal time: {dt:%Y-%m-%d %H:%M:%S %Z}\n"
            f"HA Close: {ha_close:.2f}\n"
            f"SuperTrend(1,1): {st_line:.2f}\n"
            f"Previous HA Close: {prev_ha:.2f}\n"
            f"Previous SuperTrend: {prev_st:.2f}\n"
            "Mode: intrawweek live"
        )
        telegram_send(text)
        log.info("SIGNAL %s LTP=%s time=%s", state.symbol, ltp, dt.isoformat())


def main():
    if not TOKEN:
        raise SystemExit("UPSTOX_ACCESS_TOKEN is required")
    rows = load_symbols()
    log.info("Preparing %s symbols", len(rows))
    states = {}
    for i, row in enumerate(rows, 1):
        try:
            candles = history_for(row["instrument_key"])
            df = candles_to_df(candles)
            if len(df) < 20:
                log.warning("Skipping %s: insufficient weekly history", row["trading_symbol"])
                continue
            # Exclude today's incomplete week if it is present in the historical response.
            states[row["instrument_key"]] = SymbolState(row["trading_symbol"], row["instrument_key"], df)
            if i % 25 == 0:
                log.info("Loaded history %s/%s", i, len(rows))
        except Exception as e:
            log.warning("History failed for %s: %s", row.get("trading_symbol"), e)

    if not states:
        raise SystemExit("No symbols loaded. Check token/instrument data/history access.")

    # Current-day OHLC seeds weekly open/high/low. Live LTPC ticks then keep the week exact.
    try:
        seed_current_day(states)
    except Exception as e:
        log.warning("Current-day OHLC seed failed: %s. Worker will reconstruct from live ticks.", e)

    keys = list(states.keys())
    configuration = upstox_client.Configuration()
    configuration.access_token = TOKEN
    streamer = upstox_client.MarketDataStreamerV3(upstox_client.ApiClient(configuration))
    streamer.auto_reconnect(True, 5, 100)

    def on_open():
        log.info("Upstox WebSocket connected; subscribing to %s instruments", len(keys))
        # LTPC supports a much larger subscription than full mode.
        streamer.subscribe(keys, "ltpc")

    def on_message(message):
        try:
            tick = extract_tick(message)
            if not tick:
                return
            key, ltp, ltt = tick
            state = states.get(key)
            if not state:
                return
            state.last_ltp = ltp
            state.last_ltt = ltt
            check_symbol(state, ltp, ltt)
        except Exception:
            log.exception("Tick processing error")

    streamer.on("open", on_open)
    streamer.on("message", on_message)
    streamer.on("error", lambda e: log.error("WebSocket error: %s", e))
    streamer.on("close", lambda: log.warning("WebSocket closed"))
    streamer.connect()


if __name__ == "__main__":
    main()
