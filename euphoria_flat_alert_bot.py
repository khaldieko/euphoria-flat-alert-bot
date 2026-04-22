import asyncio
import logging
import math
import os
import time
from datetime import datetime

import httpx

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "ETHUSDT"
INTERVAL = "1m"
WINDOW = 15
FLAT_THRESHOLD = float(os.getenv("FLAT_THRESHOLD", "0.0005"))

POLL_SECONDS = 60
ALERT_COOLDOWN_SECONDS = 30 * 60

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/ETH-USD/candles"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("❌ Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables!")

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("tap-alert")

# ================== HELPERS ==================
async def fetch_closes(client: httpx.AsyncClient, limit: int = 16) -> list[float]:
    try:
        r = await client.get(
            BINANCE_KLINES_URL,
            params={"symbol": SYMBOL, "interval": INTERVAL, "limit": limit},
            timeout=10.0,
        )
        r.raise_for_status()
        return [float(k[4]) for k in r.json()]
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 451:
            raise
        log.warning("Binance blocked (451) — using Coinbase fallback")

    r = await client.get(
        COINBASE_CANDLES_URL,
        params={"granularity": 60},
        timeout=10.0,
    )
    r.raise_for_status()
    candles = list(reversed(r.json()[:limit]))
    return [float(c[4]) for c in candles]


def stdev_of_log_returns(closes: list[float]) -> float:
    if len(closes) < 2:
        return 0.0
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var)


async def send_telegram(client: httpx.AsyncClient, text: str, chat_id=None):
    chat_id = chat_id or TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        await client.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=10)
        log.info("✅ Message sent")
    except Exception as e:
        log.error(f"Telegram error: {e}")


async def get_status(client: httpx.AsyncClient) -> str:
    closes = await fetch_closes(client, WINDOW + 1)
    vol = stdev_of_log_returns(closes)
    price = closes[-1]
    is_low = vol < FLAT_THRESHOLD

    status_emoji = "🟢 GOOD TO TAP" if is_low else "🔴 WAIT"

    return (
        f"📊 *ETH Volatility Check*\n\n"
        f"Price: `${price:,.2f}`\n"
        f"15m Volatility: `{vol:.4%}`\n"
        f"Threshold: `{FLAT_THRESHOLD:.4%}`\n"
        f"Status: {status_emoji}\n\n"
        f"Tip: Tap when volatility is low (🟢)"
    )


# ================== MAIN ==================
async def main():
    log.info(f"🚀 Euphoria Low-Vol Tap Bot started | threshold={FLAT_THRESHOLD:.4%}")

    last_alert_ts = 0.0
    offset = 0

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Startup
        try:
            closes = await fetch_closes(client)
            log.info(f"✅ Connected | Current price: {closes[-1]:.2f}")
        except Exception as e:
            log.error(f"Startup failed: {e}")

        while True:
            try:
                # 1. Check market & send auto alert if low vol
                closes = await fetch_closes(client, WINDOW + 1)
                vol = stdev_of_log_returns(closes)
                price = closes[-1]
                now = time.time()

                is_low_vol = vol < FLAT_THRESHOLD
                cooled = (now - last_alert_ts) > ALERT_COOLDOWN_SECONDS

                log.info(f"price={price:.2f} | vol={vol:.4%} | {'🟢 LOW VOL - GOOD TO TAP' if is_low_vol else '🔴 NORMAL VOL'}")

                if is_low_vol and cooled:
                    msg = (
                        f"🔥 *GOOD TIME TO TAP!*\n\n"
                        f"Price: `${price:,.2f}`\n"
                        f"15m Volatility: `{vol:.4%}`\n"
                        f"Threshold: `{FLAT_THRESHOLD:.4%}`\n\n"
                        f"😴 Tap line is sleeping — perfect time to start tapping!"
                    )
                    await send_telegram(client, msg)
                    last_alert_ts = now

                # 2. Check for Telegram commands (/vol)
                try:
                    resp = await client.get(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                        params={"offset": offset, "timeout": 10, "allowed_updates": ["message"]}
                    )
                    data = resp.json()

                    if data.get("result"):
                        for update in data["result"]:
                            offset = update["update_id"] + 1
                            msg = update.get("message")
                            if msg and msg.get("text") == "/vol":
                                status_text = await get_status(client)
                                await send_telegram(client, status_text, chat_id=msg["chat"]["id"])
                except Exception as e:
                    log.debug(f"getUpdates error: {e}")

            except Exception as e:
                log.error(f"Main loop error: {e}")

            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())