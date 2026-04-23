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
WINDOW = 15
FLAT_THRESHOLD = float(os.getenv("FLAT_THRESHOLD", "0.0005"))

POLL_SECONDS = 60
ALERT_COOLDOWN_SECONDS = 30 * 60

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/ETH-USD/candles"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("❌ Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables!")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("tap-alert")

# Global variables for duration tracking
low_vol_start_time = None


async def fetch_closes(client: httpx.AsyncClient, limit: int = 16) -> list[float]:
    try:
        r = await client.get(BINANCE_KLINES_URL, params={"symbol": SYMBOL, "interval": "1m", "limit": limit}, timeout=10)
        r.raise_for_status()
        return [float(k[4]) for k in r.json()]
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 451:
            raise
        log.warning("Binance blocked (451) — using Coinbase fallback")

    r = await client.get(COINBASE_CANDLES_URL, params={"granularity": 60}, timeout=10)
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


async def send_telegram(client, text: str, chat_id=None):
    chat_id = chat_id or TELEGRAM_CHAT_ID
    try:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        log.info("✅ Message sent")
    except Exception as e:
        log.error(f"Telegram failed: {e}")


async def get_status(client, threshold) -> str:
    global low_vol_start_time
    closes = await fetch_closes(client, WINDOW + 1)
    vol = stdev_of_log_returns(closes)
    price = closes[-1]
    is_low = vol < threshold

    duration_str = ""
    if is_low and low_vol_start_time:
        minutes = int((time.time() - low_vol_start_time) / 60)
        duration_str = f"\nLow volatility for: `{minutes}` minutes"

    emoji = "🟢 GOOD TO TAP" if is_low else "🔴 WAIT"

    return f"""📊 *ETH Volatility Check*

Price: `${price:,.2f}`
15m Volatility: `{vol:.4%}`
Current Threshold: `{threshold:.4%}`
Status: {emoji}{duration_str}

💡 Tip: Tap when you see 🟢"""


# ================== MAIN ==================
async def main():
    global FLAT_THRESHOLD, low_vol_start_time
    log.info(f"🚀 Euphoria Low-Vol Tap Bot started | threshold={FLAT_THRESHOLD:.4%}")

    last_alert_ts = 0.0
    update_offset = 0

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            closes = await fetch_closes(client)
            log.info(f"✅ Connected | Current price ~ {closes[-1]:.2f}")
        except Exception as e:
            log.error(f"Startup failed: {e}")

        while True:
            try:
                # === Market check + auto alert ===
                closes = await fetch_closes(client, WINDOW + 1)
                vol = stdev_of_log_returns(closes)
                price = closes[-1]
                now = time.time()

                is_low_vol = vol < FLAT_THRESHOLD

                # Update duration tracking
                if is_low_vol:
                    if low_vol_start_time is None:
                        low_vol_start_time = now
                else:
                    low_vol_start_time = None

                log.info(f"price={price:.2f} | vol={vol:.4%} | {'🟢 LOW VOL' if is_low_vol else '🔴 NORMAL VOL'}")

                # Auto alert
                if is_low_vol and (now - last_alert_ts) > ALERT_COOLDOWN_SECONDS:
                    minutes = int((now - low_vol_start_time) / 60) if low_vol_start_time else 0
                    msg = f"""🔥 *GOOD TIME TO TAP!*

Price: `${price:,.2f}`
15m Volatility: `{vol:.4%}`
Threshold: `{FLAT_THRESHOLD:.4%}`
Low volatility for: `{minutes}` minutes

😴 Tap line is sleeping — perfect time to start tapping on Euphoria!"""
                    await send_telegram(client, msg)
                    last_alert_ts = now

                # === Check for Telegram commands ===
                try:
                    resp = await client.get(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                        params={"offset": update_offset, "timeout": 8, "allowed_updates": ["message"]}
                    )
                    data = resp.json()

                    if data.get("result"):
                        for update in data["result"]:
                            update_offset = update["update_id"] + 1
                            msg = update.get("message")
                            if not msg:
                                continue

                            text = msg.get("text", "").strip()
                            chat_id = msg["chat"]["id"]

                            if text == "/vol":
                                status = await get_status(client, FLAT_THRESHOLD)
                                await send_telegram(client, status, chat_id=chat_id)

                            elif text.startswith("/setthreshold"):
                                try:
                                    parts = text.split()
                                    if len(parts) > 1:
                                        new_threshold = float(parts[1])
                                        if 0.0001 <= new_threshold <= 0.005:
                                            old_threshold = FLAT_THRESHOLD
                                            FLAT_THRESHOLD = new_threshold
                                            await send_telegram(client, 
                                                f"✅ Threshold updated!\n\n"
                                                f"Old: `{old_threshold:.4%}`\n"
                                                f"New: `{FLAT_THRESHOLD:.4%}`\n\n"
                                                f"Bot will now alert when volatility drops below this value.", 
                                                chat_id=chat_id)
                                            log.info(f"Threshold changed from {old_threshold:.4%} to {FLAT_THRESHOLD:.4%}")
                                        else:
                                            await send_telegram(client, "❌ Invalid value. Use a number between 0.0001 and 0.005 (e.g. `/setthreshold 0.0004`)", chat_id=chat_id)
                                    else:
                                        await send_telegram(client, "Usage: `/setthreshold 0.0004`", chat_id=chat_id)
                                except ValueError:
                                    await send_telegram(client, "❌ Please provide a valid number. Example: `/setthreshold 0.0004`", chat_id=chat_id)

                except Exception as e:
                    log.debug(f"getUpdates: {e}")

            except Exception as e:
                log.error(f"Main loop error: {type(e).__name__} - {e}")

            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())