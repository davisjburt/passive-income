"""Optional Telegram notifications. No-ops if env vars are unset.

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable. Uses only the stdlib so
there's no extra dependency.

Helpers (run locally after putting the token in .env):
    python -m bot.notify chatid   # discover your chat id (message the bot first)
    python -m bot.notify test     # send a test message
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("bot")
_API = "https://api.telegram.org/bot{token}/{method}"

# Telegram enforces ~1 msg/sec per chat; a burst of alerts in one wheel cycle
# (morning briefing + several state transitions + order confirmations) can
# trigger a 429 if sent back-to-back. Space sends out and retry on failure.
_MIN_INTERVAL_S = 1.2
_MAX_RETRIES = 4
_last_sent = 0.0


def _token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN")


def _chat() -> str | None:
    return os.getenv("TELEGRAM_CHAT_ID")


def telegram_enabled() -> bool:
    return bool(_token() and _chat())


def _call(method: str, params: dict, token: str | None = None, timeout: int = 15) -> dict:
    token = token or _token()
    url = _API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def send_telegram(text: str) -> bool:
    """Send a Markdown message. Returns True on success, False if disabled/failed.

    Retries on transient failures (timeouts, connection errors, Telegram 429s)
    since a single dropped send otherwise looks like a message that just never
    arrived, with no visible error.
    """
    if not telegram_enabled():
        log.info("Telegram not configured; skipping notification.")
        return False

    global _last_sent
    for attempt in range(1, _MAX_RETRIES + 1):
        # Self-throttle to stay under Telegram's per-chat rate limit.
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_sent)
        if wait > 0:
            time.sleep(wait)
        try:
            _call("sendMessage", {
                "chat_id": _chat(),
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "true",
            })
            _last_sent = time.monotonic()
            return True
        except urllib.error.HTTPError as exc:
            _last_sent = time.monotonic()
            body = exc.read().decode(errors="replace")
            retry_after = _MIN_INTERVAL_S
            if exc.code == 429:
                try:
                    retry_after = json.loads(body).get("parameters", {}).get("retry_after", 1) + 0.5
                except (json.JSONDecodeError, AttributeError):
                    retry_after = 3.0
            log.warning("Telegram send failed (HTTP %s, attempt %d/%d): %s",
                        exc.code, attempt, _MAX_RETRIES, body)
            if attempt < _MAX_RETRIES:
                time.sleep(retry_after)
                continue
            return False
        except Exception as exc:  # noqa: BLE001 — network hiccups, timeouts, etc.
            _last_sent = time.monotonic()
            log.warning("Telegram send failed (attempt %d/%d): %s", attempt, _MAX_RETRIES, exc)
            if attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)  # 2s, 4s backoff
                continue
            return False
    return False


def notify_trades(actions: list[str], equity: float | None = None) -> bool:
    """Format and send a trade notification. Skips if there are no actions."""
    if not actions:
        return False
    lines = ["🤖 *Trading bot* — trades executed"]
    for a in actions:
        if a.startswith("BUY"):
            emoji = "🟢"
        elif a.startswith("SELL"):
            emoji = "🔴"
        else:
            emoji = "•"
        lines.append(f"{emoji} {a}")
    if equity is not None:
        lines.append(f"\nEquity: ${equity:,.2f}")
    return send_telegram("\n".join(lines))


def _main() -> int:
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"

    if cmd == "chatid":
        token = _token()
        if not token:
            print("Set TELEGRAM_BOT_TOKEN in .env first.")
            return 1
        res = _call("getUpdates", {}, token=token)
        seen: dict = {}
        for u in res.get("result", []):
            msg = u.get("message") or u.get("channel_post") or {}
            chat = msg.get("chat", {})
            if chat.get("id"):
                seen[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
        if not seen:
            print("No chats found. Send any message to your bot in Telegram, then re-run.")
            return 1
        for cid, name in seen.items():
            print(f"TELEGRAM_CHAT_ID={cid}   ({name})")
        return 0

    # default: test
    if not telegram_enabled():
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
        return 1
    ok = send_telegram("✅ Test message from your trading bot. Notifications are working.")
    print("Sent!" if ok else "Failed — check token/chat id.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
