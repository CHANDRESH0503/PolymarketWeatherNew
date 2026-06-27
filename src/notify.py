"""Telegram notifications. Silently no-ops if credentials aren't set."""
from __future__ import annotations

import requests

from .config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

API = "https://api.telegram.org/bot{token}/sendMessage"


def enabled() -> bool:
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def send(text: str) -> bool:
    """Send a Markdown message. Returns True on success, never raises."""
    if not enabled():
        return False
    try:
        r = requests.post(
            API.format(token=TELEGRAM_TOKEN),
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=15,
        )
        return r.ok
    except Exception:  # noqa: BLE001
        return False


def notify_tick_error(error: Exception, consecutive: int) -> bool:
    """Alert when a daemon tick fails. Only fires on the 1st, 3rd, and every 5th failure
    after that — avoids a flood when the API is down for hours."""
    if consecutive not in (1, 3) and consecutive % 5 != 0:
        return False
    text = (
        f"⚠️ *Daemon tick error* (#{consecutive} in a row)\n"
        f"`{type(error).__name__}: {error}`\n"
        f"_Equity chart will freeze until the next successful tick._"
    )
    return send(text)


def notify_tick_recovered(consecutive_was: int) -> bool:
    """Alert when the daemon recovers after one or more failed ticks."""
    if consecutive_was < 1:
        return False
    text = (
        f"✅ *Daemon recovered* after {consecutive_was} failed tick(s)\n"
        f"_Equity chart is updating again._"
    )
    return send(text)


def notify_settlement(fill: dict, balance: dict) -> bool:
    """Format and send a closed-trade result.

    `fill` is the settled row; `balance` carries running portfolio totals.
    """
    won = fill["pnl"] > 0
    emoji = "🟢" if won else "🔴"
    verb = "WON" if won else "LOST"
    roi = fill["pnl"] / fill["cost"] * 100 if fill["cost"] else 0
    text = (
        f"{emoji} *Trade {verb}*  ({fill['pnl']:+.2f} USDC, {roi:+.0f}%)\n"
        f"_{fill['question']}_\n"
        f"`{fill['side']}` @ {fill['entry_price']:.3f} → resolved {fill['exit_price']:.0f}\n"
        f"stake ${fill['cost']:.2f} · model P(Yes) {fill['model_prob']*100:.0f}% · "
        f"edge {fill['edge']*100:+.1f}%\n"
        f"——\n"
        f"Equity *${balance['equity']:.2f}*  ·  "
        f"realized {balance['realized']:+.2f}  ·  "
        f"record {balance['wins']}/{balance['settled']}"
    )
    return send(text)


if __name__ == "__main__":
    ok = send("✅ PolymarketWeather: Telegram notifications are wired up.")
    print("sent" if ok else "failed (check TELEGRAM_TOKEN / TELEGRAM_CHAT_ID in .env)")
