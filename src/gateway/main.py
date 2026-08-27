"""FastAPI gateway: Telegram webhook in, replies out. No business logic."""

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response

load_dotenv()  # no-op in production; picks up .env for local dev

from sqlalchemy import select

from src.adapters.airtable import log_execution
from src.gateway.auth import is_allowed
from src.orchestrator.confirm import classify_confirmation_reply, describe_pending
from src.queue.models import Task, as_utc, db_session, init_db, utcnow
from src.telegram import client as tg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> Response:
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != os.environ.get("TELEGRAM_WEBHOOK_SECRET"):
        return Response(status_code=403)

    update = await request.json()

    if "callback_query" in update:
        await _handle_callback(update["callback_query"])
        return Response(status_code=200)

    message = update.get("message")
    if not message:
        return Response(status_code=200)

    user_id = (message.get("from") or {}).get("id")
    chat_id = message["chat"]["id"]
    if not is_allowed(user_id):
        logger.info("Rejected message from non-whitelisted user %s", user_id)
        await tg.send_message(chat_id, "This is a private bot.")
        return Response(status_code=200)

    if "voice" in message:
        # TODO(lynn): voice transcription is a v2 feature.
        await tg.send_message(chat_id, "Voice not supported yet.")
        return Response(status_code=200)

    text = message.get("text")
    if not text:
        # TODO(lynn): only text, voice, and button presses are specified; ignore the rest.
        return Response(status_code=200)

    if await _try_resolve_pending_by_text(str(chat_id), text):
        return Response(status_code=200)

    with db_session() as db:
        task = Task(telegram_chat_id=str(chat_id), telegram_user_id=str(user_id), instruction=text)
        db.add(task)
        db.commit()
        task_id = task.id
    logger.info("Enqueued task %s for user %s", task_id, user_id)
    await tg.send_message(chat_id, "Got it, working on it.")
    return Response(status_code=200)


async def _handle_callback(callback: dict) -> None:
    user_id = (callback.get("from") or {}).get("id")
    callback_id = callback["id"]
    if not is_allowed(user_id):
        logger.info("Rejected callback from non-whitelisted user %s", user_id)
        await tg.answer_callback_query(callback_id, "This is a private bot.")
        return

    data = callback.get("data", "")
    action, _, task_id_str = data.partition(":")
    if action not in ("confirm", "cancel") or not task_id_str.isdigit():
        await tg.answer_callback_query(callback_id)
        return

    msg = callback.get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    message_id = msg.get("message_id")

    with db_session() as db:
        task = db.get(Task, int(task_id_str))
        if task is None or task.status != "awaiting_confirmation":
            await tg.answer_callback_query(callback_id, "This action is no longer pending.")
            return
        if task.expires_at and utcnow() > as_utc(task.expires_at):
            await tg.answer_callback_query(callback_id, "This confirmation expired.")
            return

        if action == "confirm":
            _apply_confirm(db, task)
            await tg.answer_callback_query(callback_id, "Confirmed")
            if message_id:
                await tg.edit_message(chat_id, message_id, f"{msg.get('text', '')}\n\nConfirmed.")
        else:
            log_args = _apply_cancel(db, task)
            await tg.answer_callback_query(callback_id, "Cancelled")
            if message_id:
                await tg.edit_message(chat_id, message_id, f"{msg.get('text', '')}\n\nCancelled.")
            await tg.send_message(chat_id, "Cancelled.")
            await log_execution(*log_args)


def _apply_confirm(db, task: Task) -> None:
    """Mark the pending action approved and requeue; the worker resumes the loop."""
    task.pending_tool_call = {**(task.pending_tool_call or {}), "approved": True}
    task.status = "queued"
    db.commit()


def _apply_cancel(db, task: Task) -> tuple:
    """Cancel a pending task; returns args for log_execution."""
    task.status = "cancelled"
    state = task.conversation_state or {}
    duration = (utcnow() - as_utc(task.created_at)).total_seconds()
    log_args = (task.telegram_user_id, task.instruction,
                state.get("tools_called", []), "cancelled", "Cancelled.", duration)
    db.commit()
    return log_args


async def _try_resolve_pending_by_text(chat_id: str, text: str) -> bool:
    """Natural-language confirmation: if this chat has a pending confirmation and
    the message clearly confirms or cancels it, act accordingly. Returns True if
    the message was consumed; False means it should become a normal new task."""
    with db_session() as db:
        pending_task = db.scalars(
            select(Task).where(Task.telegram_chat_id == chat_id,
                               Task.status == "awaiting_confirmation")
            .order_by(Task.created_at.desc()).limit(1)).first()
        if pending_task is None or (
                pending_task.expires_at and utcnow() > as_utc(pending_task.expires_at)):
            return False
        description = describe_pending(pending_task.pending_tool_call or {})
        verdict = await classify_confirmation_reply(description, text)
        logger.info("NL confirmation verdict for task %s: %s", pending_task.id, verdict)
        if verdict == "confirm":
            _apply_confirm(db, pending_task)
            await tg.send_message(chat_id, "Confirmed.")
            return True
        if verdict == "cancel":
            log_args = _apply_cancel(db, pending_task)
            await tg.send_message(chat_id, "Cancelled.")
            await log_execution(*log_args)
            return True
        return False
