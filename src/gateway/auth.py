"""Whitelist check — the only security boundary beyond the webhook secret."""

import os


def is_allowed(user_id: int | str | None) -> bool:
    if user_id is None:
        return False
    allowed = {u.strip() for u in os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").split(",") if u.strip()}
    return str(user_id) in allowed
