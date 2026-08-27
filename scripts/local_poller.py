"""Local dev only: long-polls Telegram and forwards updates to the local gateway.

In production the webhook replaces this. Run: python scripts/local_poller.py
"""

import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
GATEWAY = os.environ.get("LOCAL_GATEWAY_URL", "http://localhost:8000/telegram/webhook")


def main() -> None:
    httpx.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook", timeout=30)
    print(f"Polling Telegram, forwarding to {GATEWAY} ...")
    offset = 0
    while True:
        try:
            resp = httpx.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                             params={"offset": offset, "timeout": 25}, timeout=35).json()
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                httpx.post(GATEWAY, json=update, timeout=60,
                           headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
        except Exception as exc:
            print(f"poller error: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    main()
