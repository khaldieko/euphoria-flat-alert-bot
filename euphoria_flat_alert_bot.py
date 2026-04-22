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
FLAT_THRESHOLD = float(os.getenv("FLAT_THRESHOLD", "0.0005"))  # Lower = more sensitive

POLL_SECONDS = 60
ALERT_COOLDOWN_SECONDS = 30 * 60  # 30 min between auto alerts

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

    # Coinbase fallback
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


async def send_telegram(client: httpx.AsyncClient, text: str, chat_id: str = None) -> None:
    """Send message to Telegram"""
    chat_id = chat_id or TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = await client.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10.0,
        )
        r.raise_for_status()
        log.info("✅ Message sent")
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


async def get_current_status(client: httpx.AsyncClient) -> str:
    """Get current price and volatility for /vol command"""
    closes = await fetch_closes(client, limit=WINDOW + 1)
    vol = stdev_of_log_returns(closes)
    price = closes[-1]
    
    status = "🟢 LOW VOL - GOOD TO TAP" if vol < FLAT_THRESHOLD else "🔴 NORMAL VOL"
    
    return (
        f"📊 *Current ETH Status*\n\n"
        f"Price: `${price:,.2f}`\n"
        f"15m Volatility: `{vol:.4%}`\n"
        f"Threshold: `{FLAT_THRESHOLD:.4%}`\n"
        f"Status: {status}\n\n"
        f"_Tap when volatility is low_"
    )


# ================== MAIN LOOP ==================
async def main() -> None:
    log.info(f"🚀 Euphoria Low-Vol Tap Alert Bot started | threshold={FLAT_THRESHOLD:.4%}")

    last_alert_ts: float = 0.0

    async with httpx.AsyncClient() as client:
        # Startup check
        try:
            closes = await fetch_closes(client, limit=WINDOW + 1)
            log.info(f"✅ Connected. Current price: {closes[-1]:.2f}")
        except Exception as e:
            log.error(f"Startup fetch failed: {e}")
            return

        # Set up bot commands (optional, shows /vol in menu)
        try:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands",
                json={"commands": [{"command": "vol", "description": "Check current volatility"}]}
            )
        except:
            pass

        while True:
            try:
                # === Automatic low-vol alert ===
                closes = await fetch_closes(client, limit=WINDOW + 1)
                vol = stdev_of_log_returns(closes)
                price = closes[-1]
                now = time.time()

                is_low_vol = vol < FLAT_THRESHOLD
                cooled = (now - last_alert_ts) > ALERT_COOLDOWN_SECONDS

                log.info(f"price={price:.2f} | vol={vol:.4%} | {'🟢 LOW VOL' if is_low_vol else '🔴 NORMAL'}")

                if is_low_vol and cooled:
                    msg = (
                        f"🔥 *GOOD TIME TO TAP!*\n\n"
                        f"Price: `${price:,.2f}`\n"
                        f"15m Volatility: `{vol:.4%}`\n"
                        f"Threshold: `{FLAT_THRESHOLD:.4%}`\n\n"
                        f"😴 Tap line is sleeping — perfect time to start tapping on Euphoria!"
                    )
                    await send_telegram(client, msg)
                    last_alert_ts = now

                # === Check for new messages (/vol command) ===
                updates = await client.get(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                    params={"offset": -1, "timeout": 5}
                )
                updates = updates.json()

                if updates.get("result"):
                    for update in updates["result"]:
                        message = update.get("message")
                        if message and message.get("text") == "/vol":
                            status_msg = await get_current_status(client)
                            await send_telegram(client, status_msg, chat_id=message["chat"]["id"])

            except Exception as e:
                log.error(f"Loop error: {e}")

            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Stopped by user")
    except Exception as e:
        log.error(f"Fatal error: {e}")