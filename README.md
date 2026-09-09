# Weekly Heikin-Ashi Close > SuperTrend(1,1) — Upstox + Telegram

This is an **alert-only** Python worker for the Chartink condition:

`Weekly HA-Close Crossed above Weekly SuperTrend - SuperTrend Line (1,1)`

It uses:

- Upstox Historical Candle Data V3 for weekly history.
- Upstox Market Data Feed V3 (`ltpc`) for live LTP/LTT.
- Upstox OHLC V3 once at startup to seed the current day's open/high/low.
- Local tick updates to keep the current week's OHLC live.
- Python calculation of Heikin-Ashi close and SuperTrend(1,1).
- Telegram Bot API for the alert.
- Render Background Worker for 24/7 process hosting.

## Important implementation assumption

The scanner interprets "crossed above" as:

`previous weekly HA close <= previous weekly SuperTrend` AND `current live weekly HA close > current live weekly SuperTrend`.

The signal is evaluated on every live tick, so the Telegram message reports the LTP and the exchange-provided LTT timestamp when available.

Chartink's internal SuperTrend calculation can differ from another implementation. Validate a few historical examples against Chartink before relying on alerts.

## Setup

1. Create an Upstox Developer App and generate an access token. Do **not** paste your token into chat or source code.
2. Create a Telegram bot and obtain its bot token and chat ID.
3. Install Python 3.10+.
4. Install packages:

   `pip install -r requirements.txt`

5. Copy `.env.example` to `.env` and fill the secrets.
6. Build the NSE symbol list:

   `python build_symbols.py`

7. For first testing, set `MAX_SYMBOLS=50` or `100`.
8. Run:

   `python scanner.py`

## Render

Use a **Background Worker**, not a Cron Job, because the market feed is a persistent WebSocket connection.

- Push this folder to a Git repository.
- Create a Render Background Worker from the repository.
- Build command: `pip install -r requirements.txt`
- Start command: `python scanner.py`
- Add the environment variables from `.env.example` in Render.

## Universe size

Upstox documents a 5,000-instrument individual limit for LTPC subscriptions. This project therefore supports large NSE equity universes, but the first run can take time because historical weekly data is requested once per symbol and cached locally.

For a practical first deployment, use 100–500 symbols. After the cache is built and the implementation is validated against Chartink, increase the universe if needed.

## Token note

Upstox access tokens expire at 3:30 AM the following day, so a daily token refresh/approval process is needed for an always-on deployment. Keep the token in Render environment variables, never in Git.

## Telegram example

`WEEKLY HA CLOSE CROSSOVER`

`Symbol: ABC`

`Entry price (LTP): 512.35`

`Signal time: 2026-09-09 10:14:22 IST`

`HA Close: 510.87`

`SuperTrend(1,1): 509.92`

## What this project does NOT do

It does not place orders. It only calculates the condition and sends alerts.
