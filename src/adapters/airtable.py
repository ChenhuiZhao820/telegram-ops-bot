"""Airtable REST adapter. Two bases (Basanite XRM, Basanite Todos), five tools.

Also home of the execution-log writer (Section 7), which bypasses the
confirmation gate because it is a system operation, not a user-requested write.
"""

import json
import logging
import os
from datetime import datetime, timezone

import httpx

from src.adapters.base import Tool

logger = logging.getLogger(__name__)

API = "https://api.airtable.com/v0"
LOG_TABLE = "Intern Execution Log"
TODOS_BASE_NAME = "Basanite Todos"

# base name -> base id, resolved at startup via the metadata API
_bases: dict[str, str] = {}


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['AIRTABLE_API_KEY']}"}


async def _request(method: str, url: str, **kwargs):
    """Shared request helper. 4xx bodies go back to the model so it can
    self-correct; 5xx is retried once then raised."""
    async with httpx.AsyncClient(timeout=30, headers=_headers()) as client:
        resp = await client.request(method, url, **kwargs)
        if resp.status_code >= 500:
            resp = await client.request(method, url, **kwargs)
            resp.raise_for_status()
        if 400 <= resp.status_code < 500:
            return {"error": resp.status_code, "body": resp.text}
        return resp.json()


async def _resolve_bases() -> dict[str, str]:
    ids = [b.strip() for b in os.environ["AIRTABLE_BASE_IDS"].split(",") if b.strip()]
    if len(ids) != 2:
        raise ValueError("AIRTABLE_BASE_IDS must contain exactly two base IDs (XRM, Todos)")
    async with httpx.AsyncClient(timeout=30, headers=_headers()) as client:
        resp = await client.get(f"{API}/meta/bases")
        resp.raise_for_status()
        names = {b["id"]: b["name"] for b in resp.json().get("bases", [])}
    return {names.get(base_id, base_id): base_id for base_id in ids}


def _base_id(base: str) -> str:
    if base not in _bases:
        raise ValueError(f"Unknown base {base!r}; known bases: {list(_bases)}")
    return _bases[base]


async def search_records(args: dict):
    # TODO(lynn): Airtable has no native full-record text search via
    # filterByFormula, so we match against every text-like field ORed together.
    base_id = _base_id(args["base"])
    meta = await _request("GET", f"{API}/meta/bases/{base_id}/tables")
    if isinstance(meta, dict) and meta.get("error"):
        return meta
    table_meta = next((t for t in meta.get("tables", [])
                       if t["name"].lower() == args["table"].lower()), None)
    if table_meta is None:
        return {"error": 404, "body": f"Table {args['table']!r} not found in {args['base']}"}
    text_types = {"singleLineText", "multilineText", "richText", "email", "url",
                  "phoneNumber", "singleSelect"}
    field_names = [f["name"] for f in table_meta.get("fields", []) if f.get("type") in text_types]
    query = args["query"].replace('"', '\\"').lower()
    clauses = [f'SEARCH("{query}", LOWER({{{f}}} & ""))' for f in field_names] or ["FALSE()"]
    formula = f"OR({', '.join(clauses)})"
    url = f"{API}/{_base_id(args['base'])}/{args['table']}"
    result = await _request("GET", url, params={"filterByFormula": formula, "maxRecords": 10})
    if isinstance(result, dict) and result.get("error"):
        return result
    return result.get("records", [])


async def get_record(args: dict):
    return await _request("GET", f"{API}/{_base_id(args['base'])}/{args['table']}/{args['record_id']}")


async def create_record(args: dict):
    url = f"{API}/{_base_id(args['base'])}/{args['table']}"
    return await _request("POST", url, json={"fields": args["fields"]})


async def update_record(args: dict):
    url = f"{API}/{_base_id(args['base'])}/{args['table']}/{args['record_id']}"
    return await _request("PATCH", url, json={"fields": args["fields"]})


async def list_tables(args: dict):
    out = {}
    for name, base_id in _bases.items():
        result = await _request("GET", f"{API}/meta/bases/{base_id}/tables")
        if isinstance(result, dict) and result.get("error"):
            out[name] = result
            continue
        out[name] = [
            {"table": t["name"], "fields": [f["name"] for f in t.get("fields", [])]}
            for t in result.get("tables", [])
        ]
    return out


_BASE_PARAM = {"type": "string", "enum": ["Basanite XRM", "Basanite Todos"],
               "description": "Which Airtable base to operate on."}


async def load_tools() -> list[Tool]:
    global _bases
    _bases = await _resolve_bases()
    return [
        Tool(
            name="search_records",
            description=("Search records in an Airtable table by free-text query against the "
                         "primary Name field. Returns up to 10 matching records with their IDs "
                         "and fields. Use list_tables first if unsure of table names."),
            input_schema={"type": "object",
                          "properties": {"base": _BASE_PARAM,
                                         "table": {"type": "string"},
                                         "query": {"type": "string"}},
                          "required": ["base", "table", "query"]},
            risk="read", handler=search_records),
        Tool(
            name="get_record",
            description="Fetch a single Airtable record by its record ID (rec...). Returns all fields.",
            input_schema={"type": "object",
                          "properties": {"base": _BASE_PARAM,
                                         "table": {"type": "string"},
                                         "record_id": {"type": "string"}},
                          "required": ["base", "table", "record_id"]},
            risk="read", handler=get_record),
        Tool(
            name="create_record",
            description=("Create a new record in an Airtable table. `fields` maps exact field "
                         "names to values. Use list_tables to discover valid field names first."),
            input_schema={"type": "object",
                          "properties": {"base": _BASE_PARAM,
                                         "table": {"type": "string"},
                                         "fields": {"type": "object"}},
                          "required": ["base", "table", "fields"]},
            risk="write", handler=create_record),
        Tool(
            name="update_record",
            description="Update fields on an existing Airtable record. Only supplied fields change.",
            input_schema={"type": "object",
                          "properties": {"base": _BASE_PARAM,
                                         "table": {"type": "string"},
                                         "record_id": {"type": "string"},
                                         "fields": {"type": "object"}},
                          "required": ["base", "table", "record_id", "fields"]},
            risk="write", handler=update_record),
        Tool(
            name="list_tables",
            description="List all tables and their field names in both Basanite bases. Takes no arguments.",
            input_schema={"type": "object", "properties": {}},
            risk="read", handler=list_tables),
    ]


async def log_execution(user_id: str, instruction: str, tools_called: list,
                        outcome: str, reply: str, duration_s: float) -> None:
    """Append one row to the Intern Execution Log in Basanite Todos.
    Bypasses the confirmation gate. Must never crash the task."""
    try:
        base_id = _bases.get(TODOS_BASE_NAME)
        if base_id is None:
            # Adapter didn't initialise (or resolve wasn't run); resolve lazily.
            base_id = (await _resolve_bases()).get(TODOS_BASE_NAME)
        if base_id is None:
            raise ValueError("Basanite Todos base not found")
        fields = {
            "Timestamp": datetime.now(timezone.utc).isoformat(),
            "User": str(user_id),
            "Instruction": instruction,
            "Tools Called": json.dumps(tools_called or [], ensure_ascii=False),
            "Outcome": outcome,
            "Reply": reply or "",
            "Duration (s)": round(duration_s, 2),
        }
        await _request("POST", f"{API}/{base_id}/{LOG_TABLE}",
                       json={"fields": fields, "typecast": True})
    except Exception:
        logger.warning("Failed to write execution log row", exc_info=True)
