"""Thin async wrapper over the Telegram Bot API. sendMessage, buttons, edits."""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_bot_username: str | None = None


async def get_bot_username() -> str:
    global _bot_username
    if _bot_username is None:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_api("getMe"))
            _bot_username = resp.json()["result"]["username"]
    return _bot_username


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}"


async def _post(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(_api(method), json=payload)
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram API %s failed: %s", method, data.get("description"))
        return data


async def send_message(chat_id: str, text: str) -> dict:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    data = await _post("sendMessage", payload)
    if not data.get("ok"):
        # Markdown parse errors are common with model output; retry as plain text.
        data = await _post("sendMessage", {"chat_id": chat_id, "text": text})
    return data


async def send_confirmation_buttons(chat_id: str, text: str, task_id: int,
                                    confirm_label: str = "Confirm",
                                    cancel_label: str = "Cancel") -> dict:
    keyboard = {
        "inline_keyboard": [[
            {"text": confirm_label, "callback_data": f"confirm:{task_id}"},
            {"text": cancel_label, "callback_data": f"cancel:{task_id}"},
        ]]
    }
    return await _post("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": keyboard})


async def edit_message(chat_id: str, message_id: int, text: str) -> dict:
    return await _post("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})


async def answer_callback_query(callback_query_id: str, text: str = "") -> dict:
    return await _post("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


async def delete_message(chat_id: str, message_id: int) -> dict:
    return await _post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


async def send_chat_action(chat_id: str, action: str = "typing") -> dict:
    """Shows 'typing…' in the chat header. Telegram expires it after ~5s,
    so callers re-send it periodically while work is in progress."""
    return await _post("sendChatAction", {"chat_id": chat_id, "action": action})
