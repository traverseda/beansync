"""Two MCP servers exposing bean-sync over Streamable HTTP.

  /mcp/agent  — the finance assistant as a sub-agent. One tool, ask_beansync,
                which runs a full agent turn (ledger queries, tool calls, the
                lot) and returns its prose answer. Use this when you want
                bean-sync to think.

  /mcp/tools  — bean-sync's individual tools, for driving the ledger directly
                from another agent. Which tools are visible is chosen per
                connection with a query string; see _resolve_filter below.

Auth is Home Assistant's: both are mounted on the same ASGI app as the UI and
inherit whatever the ingress in front of it enforces. Nothing here authenticates.

For the same reason we leave the SDK's DNS-rebinding protection off (its default
when transport_security is unset). It works off a Host/Origin allowlist, and the
hostname bean-sync is reached on belongs to the user's HA install — we can't know
it here, and guessing wrong would reject every request. The ingress is the thing
deciding who gets to talk to this app.
"""

from __future__ import annotations

import contextlib
import inspect
from contextvars import ContextVar
from typing import Any

from loguru import logger
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.types import Tool as MCPTool
from starlette.types import ASGIApp, Receive, Scope, Send

from beansync.agent_tools import (
    TOOL_NAMES,
    TOOL_SPECS,
    WRITE_TOOLS,
    build_handlers,
    run_agent,
)

# Set per-request by _FilterMiddleware from the query string, read by
# _FilteredFastMCP. A single FastMCP instance serves every connection, so the
# visible tool set has to be request-scoped rather than baked into the server.
# None means unfiltered.
_visible_tools: ContextVar[frozenset[str] | None] = ContextVar("_visible_tools", default=None)

_GROUPS = {
    "@all": frozenset(TOOL_NAMES),
    "@write": WRITE_TOOLS,
    "@read": frozenset(TOOL_NAMES) - WRITE_TOOLS,
}


def _expand(csv: str) -> tuple[frozenset[str], list[str]]:
    """Expand a comma-separated tool/group list into concrete names + unknown entries."""
    names: set[str] = set()
    unknown: list[str] = []
    for raw in csv.split(","):
        token = raw.strip()
        if not token:
            continue
        if token in _GROUPS:
            names |= _GROUPS[token]
        elif token in TOOL_NAMES:
            names.add(token)
        else:
            unknown.append(token)
    return frozenset(names), unknown


def _resolve_filter(query: str) -> frozenset[str]:
    """Work out which tools a connection may see from its query string.

    Supported (both optional, combinable):
      ?include=query_ledger,list_accounts   whitelist — only these
      ?exclude=save_config,run_ingest       blacklist — everything but these

    Names may also be groups: @all, @read, @write. include is applied first,
    then exclude is subtracted, so ?include=@read&exclude=tavily_search works.
    With neither parameter every tool is exposed.
    """
    from urllib.parse import parse_qs

    params = parse_qs(query)
    allowed = frozenset(TOOL_NAMES)

    if raw := params.get("include"):
        included, unknown = _expand(",".join(raw))
        if unknown:
            logger.warning("MCP include= names no such tool: {}", ", ".join(unknown))
        allowed = included

    if raw := params.get("exclude"):
        excluded, unknown = _expand(",".join(raw))
        if unknown:
            logger.warning("MCP exclude= names no such tool: {}", ", ".join(unknown))
        allowed -= excluded

    return allowed


_JSON_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _tool_from_spec(spec: dict, impl: Any) -> Tool:
    """Adapt one OpenAI-style tool spec into an MCP Tool.

    The specs in agent_tools carry hand-written JSON Schema with per-argument
    descriptions the LLM relies on. FastMCP would rather derive a schema from a
    function signature, which loses all of that, so we synthesise a signature
    with matching argument names (enough for FastMCP to validate and dispatch)
    and then advertise the original schema verbatim.
    """
    fn_spec = spec["function"]
    name = fn_spec["name"]
    schema = fn_spec.get("parameters") or {"type": "object", "properties": {}}
    props: dict[str, dict] = schema.get("properties", {})
    required = set(schema.get("required", []))

    params = [
        inspect.Parameter(
            arg,
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if arg in required else None,
            annotation=(
                _JSON_TYPES.get(str(defn.get("type", "")), Any)
                if arg in required
                else _JSON_TYPES.get(str(defn.get("type", "")), Any) | None
            ),
        )
        # required args first so the signature is legal
        for arg, defn in sorted(props.items(), key=lambda kv: kv[0] not in required)
    ]

    def call(**kwargs: Any) -> str:
        # Drop unsupplied optionals so each handler's own defaults apply.
        return str(impl(**{k: v for k, v in kwargs.items() if v is not None}))

    call.__name__ = name
    call.__signature__ = inspect.Signature(params, return_annotation=str)  # type: ignore[attr-defined]
    call.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}

    tool = Tool.from_function(call, name=name, description=fn_spec.get("description", ""))
    tool.parameters = schema
    return tool


class _FilteredFastMCP(FastMCP):
    """FastMCP that honours the per-request tool filter in _visible_tools."""

    async def list_tools(self) -> list[MCPTool]:
        allowed = _visible_tools.get()
        tools = await super().list_tools()
        if allowed is None:
            return tools
        return [t for t in tools if t.name in allowed]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        allowed = _visible_tools.get()
        if allowed is not None and name not in allowed:
            # Hidden tools must be uncallable, not merely unlisted — otherwise
            # the filter is decorative and a client that guesses a name wins.
            raise ValueError(f"Tool {name!r} is not available on this connection")
        return await super().call_tool(name, arguments)


class _FilterMiddleware:
    """Read the tool filter off the query string and publish it for this request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        query = scope.get("query_string", b"").decode()
        token = _visible_tools.set(_resolve_filter(query) if query else None)
        try:
            await self.app(scope, receive, send)
        finally:
            _visible_tools.reset(token)


def _build_agent_server() -> FastMCP:
    server = FastMCP(
        "beansync-agent",
        instructions=(
            "bean-sync's finance assistant, as a delegate. Ask it questions about the "
            "beancount ledger, or tell it to make a change; it will run its own tools and "
            "report back in prose. Each call is independent — it has no memory of previous "
            "calls, so include any context the request needs."
        ),
        stateless_http=True,
    )

    async def ask_beansync(prompt: str) -> str:
        """Ask bean-sync's finance assistant to answer a question or perform a task.

        It can query the beancount ledger, create and edit transactions, inspect and
        reconfigure ingestion sources, and search the web for merchant information.
        State the request in full: the assistant does not see this conversation and
        retains nothing between calls.
        """
        logger.info("MCP sub-agent request: {}", prompt[:200])
        return await run_agent(prompt)

    server.add_tool(ask_beansync)
    return server


def _build_tools_server() -> FastMCP:
    server = _FilteredFastMCP(
        "beansync-tools",
        instructions=(
            "bean-sync's ledger tools, exposed directly. Query and edit a beancount "
            "ledger, manage ingestion sources, and read or write merchant notes."
        ),
        stateless_http=True,
    )
    handlers = build_handlers([])  # UI side effects have nowhere to go here; discard them
    for spec in TOOL_SPECS:
        name = spec["function"]["name"]
        impl = handlers.get(name)
        if impl is None:
            logger.warning("No handler for tool spec {!r}; not exposing it over MCP", name)
            continue
        server._tool_manager._tools[name] = _tool_from_spec(spec, impl)
    return server


agent_server = _build_agent_server()
tools_server = _build_tools_server()


def mount(app: Any) -> None:
    """Mount both MCP servers on a FastAPI/Starlette app and run their lifespans.

    Starlette does not propagate lifespan events into mounted sub-applications,
    so the session managers each server needs are started from the parent app's
    own startup hook rather than from the mounted app.
    """
    app.mount("/mcp/agent", agent_server.streamable_http_app())
    app.mount("/mcp/tools", _FilterMiddleware(tools_server.streamable_http_app()))

    stack = contextlib.AsyncExitStack()

    async def _start() -> None:
        await stack.__aenter__()
        await stack.enter_async_context(agent_server.session_manager.run())
        await stack.enter_async_context(tools_server.session_manager.run())
        logger.info("MCP servers mounted at /mcp/agent and /mcp/tools")

    async def _stop() -> None:
        await stack.aclose()

    # NiceGUI's app wraps the FastAPI lifespan with its own hooks; a plain
    # Starlette/FastAPI app only has the router's lists.
    if hasattr(app, "on_startup") and hasattr(app, "on_shutdown"):
        app.on_startup(_start)
        app.on_shutdown(_stop)
    else:
        app.router.on_startup.append(_start)
        app.router.on_shutdown.append(_stop)
