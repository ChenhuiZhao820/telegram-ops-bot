"""Background worker: polls the tasks table, runs the orchestrator, replies.

One task at a time (v1). The same loop sweeps expired confirmations.
Run with: python -m src.queue.worker
"""

import asyncio
import logging
import os
from datetime import timedelta

from dotenv import load_dotenv
from sqlalchemy import select

load_dotenv()  # no-op in production; picks up .env for local dev

from src.adapters.airtable import log_execution
from src.adapters.base import build_registry, current_user_id
from src.orchestrator.loop import (Completed, NeedsConfirmation, NeedsContinuation,
                                   build_followup_state, run_task)
from src.queue.models import Task, as_utc, db_session, init_db, utcnow
from src.telegram import client as tg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIRMATION_TTL = timedelta(minutes=15)
POLL_INTERVAL_S = 1
# Chat context persists until the founder agrees to close the session (via the
# close_session tool). The TTL is only a safety net against very stale context.
CONTEXT_TTL = timedelta(hours=float(os.environ.get("CONTEXT_TTL_HOURS", "48")))


def _inherited_state(db, task: Task) -> dict | None:
    prev = db.scalars(select(Task).where(
        Task.telegram_chat_id == task.telegram_chat_id,
        Task.status == "done", Task.id != task.id,
        Task.updated_at >= utcnow() - CONTEXT_TTL)
        .order_by(Task.updated_at.desc()).limit(1)).first()
    prev_messages = (prev.conversation_state or {}).get("messages") if prev else None
    if not prev_messages:
        return None
    logger.info("Task %s inherits context from task %s", task.id, prev.id)
    return build_followup_state(prev_messages, task.instruction)


def _final_messages(outcome: Completed) -> list:
    """The session boundary: a task that closed the session stores an empty
    conversation, so the next message starts fresh instead of inheriting."""
    if any(t["tool"] == "close_session" for t in outcome.tools_called):
        return []
    return outcome.messages


async def _keep_typing(chat_id: str) -> None:
    """Re-sends Telegram's 'typing…' chat action (it expires after ~5s) until
    cancelled, so users see the bot is thinking/executing the whole time."""
    while True:
        try:
            await tg.send_chat_action(chat_id)
        except Exception:
            pass  # a failed indicator must never affect the task
        await asyncio.sleep(4)


async def process_task(task_id: int, registry) -> None:
    with db_session() as db:
        task = db.get(Task, task_id)
        task.status = "running"
        chat_id, user_id = task.telegram_chat_id, task.telegram_user_id
        instruction = task.instruction
        state = task.conversation_state
        pending = task.pending_tool_call
        created_at = task.created_at
        ack_id = task.ack_message_id
        if state is None and pending is None:
            state = _inherited_state(db, task)
        db.commit()

    async def clear_ack() -> None:
        nonlocal ack_id
        if ack_id:
            await tg.delete_message(chat_id, ack_id)
            ack_id = None
            with db_session() as db:
                db.get(Task, task_id).ack_message_id = None
                db.commit()

    approved_call = None
    if (pending or {}).get("approved"):
        if pending.get("continue"):
            state["iterations"] = 0  # founder tapped Continue; grant a fresh budget
        else:
            approved_call = pending
    current_user_id.set(user_id)
    logger.info("Running task %s for user %s", task_id, user_id)

    # Show "typing…" in the chat for as long as the task is actually running.
    typing = asyncio.create_task(_keep_typing(chat_id))
    try:
        outcome = await run_task(instruction, registry, state=state, approved_call=approved_call)
    except Exception:
        logger.exception("Task %s failed", task_id)
        reply = "Couldn't reach the AI service, try again in a minute."
        await clear_ack()
        await tg.send_message(chat_id, reply)
        duration = (utcnow() - as_utc(created_at)).total_seconds()
        with db_session() as db:
            task = db.get(Task, task_id)
            task.status, task.reply, task.duration_s = "failed", reply, duration
            db.commit()
        await log_execution(user_id, instruction, (state or {}).get("tools_called", []),
                            "failed", reply, duration)
        return
    finally:
        typing.cancel()

    if isinstance(outcome, NeedsConfirmation):
        with db_session() as db:
            task = db.get(Task, task_id)
            task.status = "awaiting_confirmation"
            task.conversation_state = outcome.state
            task.pending_tool_call = outcome.pending_tool_call
            task.expires_at = utcnow() + CONFIRMATION_TTL
            db.commit()
        await clear_ack()
        await tg.send_confirmation_buttons(chat_id, outcome.description, task_id)
        return

    if isinstance(outcome, NeedsContinuation):
        with db_session() as db:
            task = db.get(Task, task_id)
            task.status = "awaiting_confirmation"
            task.conversation_state = outcome.state
            task.pending_tool_call = {"continue": True}
            task.expires_at = utcnow() + CONFIRMATION_TTL
            db.commit()
        await clear_ack()
        await tg.send_confirmation_buttons(
            chat_id,
            f"This is taking many steps (done so far: {outcome.progress}). Continue?",
            task_id, confirm_label="Continue", cancel_label="Stop")
        return

    assert isinstance(outcome, Completed)
    duration = (utcnow() - as_utc(created_at)).total_seconds()
    with db_session() as db:
        task = db.get(Task, task_id)
        task.status = "done"
        task.reply = outcome.reply
        task.tools_called = outcome.tools_called
        task.duration_s = duration
        task.conversation_state = {"messages": _final_messages(outcome)}
        db.commit()
    await clear_ack()
    await tg.send_message(chat_id, outcome.reply)
    await log_execution(user_id, instruction, outcome.tools_called, "success",
                        outcome.reply, duration)


async def sweep_expired() -> None:
    with db_session() as db:
        expired = db.scalars(select(Task).where(
            Task.status == "awaiting_confirmation", Task.expires_at < utcnow())).all()
        details = [(t.id, t.telegram_chat_id, t.telegram_user_id, t.instruction,
                    (t.conversation_state or {}).get("tools_called", []),
                    t.created_at) for t in expired]
        for t in expired:
            t.status = "expired"
        db.commit()
    for task_id, chat_id, user_id, instruction, tools_called, created_at in details:
        logger.info("Task %s confirmation expired", task_id)
        await tg.send_message(chat_id, "Cancelled.")
        duration = (utcnow() - as_utc(created_at)).total_seconds()
        await log_execution(user_id, instruction, tools_called, "expired", "Cancelled.", duration)


async def main() -> None:
    init_db()
    registry = await build_registry()
    logger.info("Worker started with capabilities: %s", registry.capabilities)
    while True:
        await sweep_expired()
        with db_session() as db:
            task = db.scalars(select(Task).where(Task.status == "queued")
                              .order_by(Task.created_at).limit(1)).first()
            task_id = task.id if task else None
        if task_id is not None:
            await process_task(task_id, registry)
        else:
            await asyncio.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(main())
