"""Confirmation-gate logic: plain-English descriptions of pending write actions,
plus natural-language confirm/cancel classification for text replies."""

import json
import logging
import os

from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)


def _fields_summary(fields: dict, limit: int = 6) -> str:
    parts = [f"{v}" if k.lower() in ("name",) else f"{k}: {v}"
             for k, v in list(fields.items())[:limit]]
    if len(fields) > limit:
        parts.append("…")
    return ", ".join(str(p) for p in parts)


def describe_pending_action(tool_name: str, tool_input: dict) -> str:
    if tool_name == "create_record":
        return (f"Create record in {tool_input.get('base')} → {tool_input.get('table')}: "
                f"{_fields_summary(tool_input.get('fields', {}))}. Confirm?")
    if tool_name == "update_record":
        return (f"Update record {tool_input.get('record_id')} in {tool_input.get('base')} → "
                f"{tool_input.get('table')}: {_fields_summary(tool_input.get('fields', {}))}. Confirm?")
    if tool_name == "create_devin_session":
        return (f"Start a Devin session: '{tool_input.get('task_description')}' "
                f"This spends Devin credits. Confirm?")
    if tool_name == "archive_devin_session":
        return f"Close (archive) Devin session {tool_input.get('session_id')}. Confirm?"
    if tool_name == "send_message_to_devin":
        return (f"Send to Devin session {tool_input.get('session_id')}: "
                f"'{tool_input.get('message')}'. Confirm?")
    return f"Run {tool_name} with {json.dumps(tool_input, ensure_ascii=False)}. Confirm?"


def describe_pending(pending: dict) -> str:
    if pending.get("continue"):
        return "Continue the paused task?"
    return describe_pending_action(pending.get("name", "action"), pending.get("input", {}))


_CLASSIFY_PROMPT = """\
A user was shown this pending action and asked to confirm or cancel it:
"{description}"

They replied with this message:
"{message}"

Classify the reply. Answer with exactly one word:
- confirm — they approve the pending action
- cancel — they reject or stop the pending action
- unrelated — the message is a new request or does not answer the question"""


async def classify_confirmation_reply(description: str, message: str) -> str:
    """Returns 'confirm', 'cancel', or 'unrelated'. Defaults to 'unrelated' on
    any error so an ambiguous reply becomes a normal new task, never a write."""
    try:
        client = AsyncAnthropic()
        response = await client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=5,
            messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(
                description=description, message=message)}])
        verdict = "".join(b.text for b in response.content if b.type == "text").strip().lower()
        return verdict if verdict in ("confirm", "cancel") else "unrelated"
    except Exception:
        logger.warning("Confirmation classification failed", exc_info=True)
        return "unrelated"
