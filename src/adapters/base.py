"""Adapter contract and tool registry.

Each adapter module exposes `async def load_tools() -> list[Tool]`. The registry
collects tools from all adapters; an adapter that fails to initialise logs a
warning and contributes zero tools. Adding a new adapter requires only adding
it to ADAPTER_MODULES below — the orchestrator never changes.
"""

import importlib
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Telegram user ID of the founder whose task is currently executing.
# Set by the worker before run_task; read by adapters that record provenance.
current_user_id: ContextVar[str] = ContextVar("current_user_id", default="unknown")

ADAPTER_MODULES = ["src.adapters.airtable", "src.adapters.devin", "src.adapters.otter",
                   "src.adapters.sessions"]


@dataclass
class Tool:
    name: str  # snake_case, verb-first
    description: str  # written for the model
    input_schema: dict  # JSON Schema
    risk: str  # "read" | "write"
    handler: Callable[[dict], Awaitable[Any]]


class ToolRegistry:
    def __init__(self, tools: list[Tool], capabilities: list[str]):
        self._tools = {t.name: t for t in tools}
        # Adapter names that loaded successfully; used to build the system prompt.
        self.capabilities = capabilities

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def anthropic_schemas(self) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in self._tools.values()
        ]


async def build_registry() -> ToolRegistry:
    tools: list[Tool] = []
    capabilities: list[str] = []
    for module_name in ADAPTER_MODULES:
        adapter = module_name.rsplit(".", 1)[-1]
        try:
            module = importlib.import_module(module_name)
            adapter_tools = await module.load_tools()
            if adapter_tools:
                tools.extend(adapter_tools)
                capabilities.append(adapter)
            else:
                logger.warning("Adapter %s registered zero tools", adapter)
        except Exception:
            logger.warning("Adapter %s failed to initialise, skipping", adapter, exc_info=True)
    return ToolRegistry(tools, capabilities)
