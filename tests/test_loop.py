"""Orchestrator loop tests with a scripted fake Anthropic client."""

import pytest

import src.orchestrator.loop as loop_mod
from src.adapters.base import Tool, ToolRegistry
from src.orchestrator.loop import (Completed, NeedsConfirmation, NeedsContinuation,
                                   build_followup_state, run_task)


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, **kw):
        return dict(self.__dict__)


def text_block(text):
    return Block(type="text", text=text)


def tool_block(id, name, input):
    return Block(type="tool_use", id=id, name=name, input=input)


class FakeAnthropic:
    """Yields pre-scripted responses in order."""
    script = []

    def __init__(self, *a, **kw):
        self.messages = self

    async def create(self, **kw):
        return Block(content=FakeAnthropic.script.pop(0))


@pytest.fixture
def registry():
    calls = []

    async def read_handler(args):
        calls.append(("read_tool", args))
        return {"ok": True}

    async def write_handler(args):
        calls.append(("write_tool", args))
        return {"created": "rec123"}

    reg = ToolRegistry([
        Tool("read_tool", "reads", {"type": "object", "properties": {}}, "read", read_handler),
        Tool("write_tool", "writes", {"type": "object", "properties": {}}, "write", write_handler),
    ], capabilities=["airtable"])
    reg.calls = calls
    return reg


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    monkeypatch.setattr(loop_mod, "AsyncAnthropic", FakeAnthropic)
    monkeypatch.setenv("MAX_TOOL_ITERATIONS", "15")
    FakeAnthropic.script = []


@pytest.mark.asyncio
async def test_direct_answer_no_tools(registry):
    FakeAnthropic.script = [[text_block("Hello!")]]
    result = await run_task("say hello", registry)
    assert isinstance(result, Completed)
    assert result.reply == "Hello!" and result.tools_called == []


@pytest.mark.asyncio
async def test_read_tool_runs_without_confirmation(registry):
    FakeAnthropic.script = [
        [tool_block("t1", "read_tool", {"q": "x"})],
        [text_block("Found it.")],
    ]
    result = await run_task("look something up", registry)
    assert isinstance(result, Completed)
    assert registry.calls == [("read_tool", {"q": "x"})]
    assert result.tools_called == [{"tool": "read_tool", "input": {"q": "x"}}]


@pytest.mark.asyncio
async def test_write_tool_pauses_then_resumes(registry):
    FakeAnthropic.script = [[tool_block("t1", "write_tool", {"name": "Jane"})]]
    result = await run_task("create jane", registry)
    assert isinstance(result, NeedsConfirmation)
    assert result.pending_tool_call == {"id": "t1", "name": "write_tool", "input": {"name": "Jane"}}
    assert registry.calls == []  # nothing executed before confirmation

    FakeAnthropic.script = [[text_block("Created Jane.")]]
    resumed = await run_task("create jane", registry, state=result.state,
                             approved_call=result.pending_tool_call)
    assert isinstance(resumed, Completed)
    assert registry.calls == [("write_tool", {"name": "Jane"})]
    assert resumed.reply == "Created Jane."


@pytest.mark.asyncio
async def test_iteration_cap_pauses_for_continuation(registry):
    FakeAnthropic.script = [[tool_block(f"t{i}", "read_tool", {})] for i in range(20)]
    result = await run_task("loop forever", registry)
    assert isinstance(result, NeedsContinuation)
    assert "read_tool" in result.progress
    assert result.state["iterations"] == 15

    # Founder taps Continue: iterations reset, loop resumes and finishes.
    result.state["iterations"] = 0
    FakeAnthropic.script = [[text_block("All done.")]]
    resumed = await run_task("loop forever", registry, state=result.state)
    assert isinstance(resumed, Completed)
    assert resumed.reply == "All done."


@pytest.mark.asyncio
async def test_followup_inherits_context(registry):
    FakeAnthropic.script = [[text_block("Which Friday, this week or next?")]]
    first = await run_task("remind me to call Paul on Friday", registry)
    assert isinstance(first, Completed) and len(first.messages) == 2

    state = build_followup_state(first.messages, "this week")
    assert state["messages"][:2] == first.messages  # history preserved
    assert state["messages"][-1] == {"role": "user", "content": "this week"}
    FakeAnthropic.script = [[text_block("Got it, this Friday.")]]
    second = await run_task("this week", registry, state=state)
    assert isinstance(second, Completed)
    assert second.reply == "Got it, this Friday."


def test_followup_trim_never_splits_tool_pairs():
    big = "x" * 30000
    prev = [
        {"role": "user", "content": big},  # over budget, must be dropped
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "a", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}]},
        {"role": "user", "content": "small question"},
        {"role": "assistant", "content": [{"type": "text", "text": "small answer"}]},
    ]
    state = build_followup_state(prev, "next")
    # Dropping the oversized first message orphans the tool pair, so trimming
    # continues until the history starts with a plain user text message.
    assert state["messages"][0] == {"role": "user", "content": "small question"}
    assert state["messages"][-1] == {"role": "user", "content": "next"}
