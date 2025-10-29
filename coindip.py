import os, time, json, requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# === ENV CONFIG (edit via env, not code) ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))

COINS = [c.strip() for c in os.getenv(
    "COIN_LIST",
    "pepe,bonk,floki,ordi,shiba-inu"
).split(",") if c.strip()]

TRADE_SIZE_USD    = float(os.getenv("TRADE_SIZE_USD", "500"))     # nominal position size
DIP_FROM_HIGH_PCT = float(os.getenv("DIP_FROM_HIGH_PCT", "7.0"))  # buy when this % below 7d high
SELL_TARGET_PCT   = float(os.getenv("SELL_TARGET_PCT", "3.0"))    # take-profit %
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT", "3.0"))      # stop-loss %

BUY_TRIGGER_MULT  = 1.0 - (DIP_FROM_HIGH_PCT / 100.0)
SELL_TRIGGER_MULT = 1.0 + (SELL_TARGET_PCT / 100.0)
STOP_LOSS_MULT    = 1.0 - (STOP_LOSS_PCT / 100.0)

STATE_FILE = "positions.json"   # remembers open positions across restarts

# === helpers ===
def tg_send(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text}, timeout=15
        )
    except Exception as e:
        print("Telegram error:", e)

def gecko_simple(ids):
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": ",".join(ids), "vs_currencies": "usd"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

def gecko_7d_high(coin):
    r = requests.get(
        f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart",
        params={"vs_currency": "usd", "days": 7},
        timeout=25,
    )
    r.raise_for_status()
    prices = r.json().get("prices", [])
    return max(p[1] for p in prices) if prices else None

def fmt_price(x):
    if x >= 1: return f"${x:,.2f}"
    if x >= 0.01: return f"${x:.4f}"
    return f"${x:.8f}"

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            return json.load(open(STATE_FILE))
    except Exception:
        pass
    return {}  # { coin: {"buy": float, "t": epoch} }

def save_state(positions):
    try:
        json.dump(positions, open(STATE_FILE, "w"))
    except Exception as e:
        print("State save error:", e)

# === main ===
if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

positions = load_state()
high7_cache = {}  # coin -> (value, ts)

tg_send(
    "🟢 CoinDip started.\n"
    f"Watching: {', '.join(c.upper() for c in COINS)}\n"
    f"Buy: {DIP_FROM_HIGH_PCT:.1f}% below 7d high\n"
    f"TP: +{SELL_TARGET_PCT:.1f}% | SL: -{STOP_LOSS_PCT:.1f}%\n"
    f"Nominal size: ${TRADE_SIZE_USD:,.0f}"
)

while True:
    try:
        now = int(time.time())

        # 1) current prices
        simple = gecko_simple(COINS)

        for coin in COINS:
            price = simple.get(coin, {}).get("usd")
            if price is None:
                continue

            # 2) refresh 7d high hourly
            cached = high7_cache.get(coin)
            if not cached or now - cached[1] > 3600:
                try:
                    h7 = gecko_7d_high(coin)
                    if h7: high7_cache[coin] = (h7, now)
                except Exception as e:
                    print("7d high error:", coin, e)

            h7 = high7_cache.get(coin, (None, None))[0]
            if not h7:
                continue

            # 3) BUY signal
            if coin not in positions and price <= h7 * BUY_TRIGGER_MULT:
                qty = TRADE_SIZE_USD / price
                target = price * SELL_TRIGGER_MULT
                stop   = price * STOP_LOSS_MULT
                positions[coin] = {"buy": price, "t": now}
                save_state(positions)
                tg_send(
                    f"🚀 BUY {coin.upper()} at {fmt_price(price)} "
                    f"(dip {(1 - price/h7)*100:.1f}% vs 7d high {fmt_price(h7)})\n"
                    f"Qty ≈ {qty:,.0f} (for ${TRADE_SIZE_USD:,.0f})\n"
                    f"→ Target: {fmt_price(target)} (+{SELL_TARGET_PCT:.1f}%) | "
                    f"Stop: {fmt_price(stop)} (-{STOP_LOSS_PCT:.1f}%)"
                )
                continue

            # 4) manage open position
            if coin in positions:
                buy = positions[coin]["buy"]

                # take-profit
                if price >= buy * SELL_TRIGGER_MULT:
                    pnl = (price / buy - 1) * 100
                    tg_send(f"✅ SELL {coin.upper()} at {fmt_price(price)}  (+{pnl:.2f}% from {fmt_price(buy)})")
                    del positions[coin]
                    save_state(positions)
                    continue

                # stop-loss
                if price <= buy * STOP_LOSS_MULT:
                    pnl = (price / buy - 1) * 100
                    tg_send(f"⚠️ STOP {coin.upper()} at {fmt_price(price)}  ({pnl:.2f}% from {fmt_price(buy)})")
                    del positions[coin]
                    save_state(positions)
                    continue

        time.sleep(INTERVAL)

    except requests.HTTPError as e:
        print("HTTP error:", e)
        time.sleep(30)
    except Exception as e:
        print("Loop error:", e)
        time.sleep(10)
