"""Claude tool-use loop. No business logic, no if/else on user intent.

run_task drives the Anthropic Messages API against the tool registry. When the
model requests a write tool, the loop returns NeedsConfirmation with the full
serialisable state; the worker persists it and resumes the loop after the
founder presses Confirm.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field

from anthropic import AsyncAnthropic

from src.adapters.base import ToolRegistry
from src.orchestrator.confirm import describe_pending_action
from src.orchestrator.prompts import build_system_prompt

logger = logging.getLogger(__name__)


@dataclass
class Completed:
    reply: str
    tools_called: list = field(default_factory=list)
    # Final messages array, persisted so a quick follow-up message can inherit
    # the conversation (see build_followup_state).
    messages: list = field(default_factory=list)


@dataclass
class NeedsConfirmation:
    description: str
    pending_tool_call: dict  # {"id", "name", "input"}
    state: dict  # serialisable loop state to persist on the task row


@dataclass
class NeedsContinuation:
    """Iteration cap reached; the task can resume if the founder taps Continue."""
    progress: str  # human-readable summary of tools run so far
    state: dict


async def _call_anthropic(client: AsyncAnthropic, **kwargs):
    """Retry twice with exponential backoff, then raise."""
    for attempt in range(3):
        try:
            return await client.messages.create(**kwargs)
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)


async def _execute_tool(registry: ToolRegistry, name: str, tool_input: dict):
    tool = registry.get(name)
    if tool is None:
        return {"error": f"Unknown tool {name!r}"}
    try:
        return await tool.handler(tool_input)
    except Exception as exc:
        logger.warning("Tool %s raised", name, exc_info=True)
        return {"error": f"Tool {name} failed: {exc}"}


def _tool_result_block(tool_use_id: str, result) -> dict:
    content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


async def run_task(instruction: str, registry: ToolRegistry,
                   state: dict | None = None,
                   approved_call: dict | None = None) -> Completed | NeedsConfirmation:
    """Run (or resume) one task. `state`/`approved_call` are set when resuming
    after a confirmation."""
    client = AsyncAnthropic()
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    max_iterations = int(os.environ.get("MAX_TOOL_ITERATIONS", "15"))

    if state:
        messages = state["messages"]
        pending_results = state["pending_results"]
        call_queue = state["remaining_calls"]
        iterations = state["iterations"]
        tools_called = state["tools_called"]
    else:
        messages = [{"role": "user", "content": instruction}]
        pending_results, call_queue, tools_called = [], [], []
        iterations = 0

    if approved_call:
        result = await _execute_tool(registry, approved_call["name"], approved_call["input"])
        tools_called.append({"tool": approved_call["name"], "input": approved_call["input"]})
        pending_results.append(_tool_result_block(approved_call["id"], result))

    while True:
        # Drain tool calls requested by the last assistant turn.
        while call_queue:
            call = call_queue[0]
            tool = registry.get(call["name"])
            if tool is not None and tool.risk == "write":
                return NeedsConfirmation(
                    description=describe_pending_action(call["name"], call["input"]),
                    pending_tool_call=call,
                    state={"messages": messages, "pending_results": pending_results,
                           "remaining_calls": call_queue[1:], "iterations": iterations,
                           "tools_called": tools_called},
                )
            result = await _execute_tool(registry, call["name"], call["input"])
            tools_called.append({"tool": call["name"], "input": call["input"]})
            pending_results.append(_tool_result_block(call["id"], result))
            call_queue = call_queue[1:]

        if pending_results:
            messages.append({"role": "user", "content": pending_results})
            pending_results = []

        if iterations >= max_iterations:
            done = ", ".join(dict.fromkeys(t["tool"] for t in tools_called)) or "no tools yet"
            return NeedsContinuation(
                progress=done,
                state={"messages": messages, "pending_results": pending_results,
                       "remaining_calls": call_queue, "iterations": iterations,
                       "tools_called": tools_called})

        response = await _call_anthropic(
            client, model=model, max_tokens=2048,
            system=build_system_prompt(registry.capabilities),
            messages=messages, tools=registry.anthropic_schemas())
        iterations += 1
        messages.append({"role": "assistant",
                         "content": [b.model_dump(exclude_none=True) for b in response.content]})
        call_queue = [{"id": b.id, "name": b.name, "input": b.input}
                      for b in response.content if b.type == "tool_use"]
        if not call_queue:
            text = "".join(b.text for b in response.content if b.type == "text")
            return Completed(reply=text or "(no reply)", tools_called=tools_called,
                             messages=messages)


CONTEXT_CHAR_BUDGET = 24000


def build_followup_state(prev_messages: list, instruction: str) -> dict:
    """Seed a new task with the previous task's conversation so follow-ups
    ("yes, Friday", "add another one like that") keep their context. Trims the
    inherited history to a character budget without ever splitting a
    tool_use/tool_result pair (the first kept message must be plain user text)."""
    msgs = list(prev_messages)

    def oversized() -> bool:
        return sum(len(json.dumps(m, default=str)) for m in msgs) > CONTEXT_CHAR_BUDGET

    def dirty_start() -> bool:
        return bool(msgs) and not (msgs[0]["role"] == "user" and isinstance(msgs[0]["content"], str))

    while msgs and (oversized() or dirty_start()):
        msgs.pop(0)
    return {"messages": msgs + [{"role": "user", "content": instruction}],
            "pending_results": [], "remaining_calls": [], "iterations": 0,
            "tools_called": []}
