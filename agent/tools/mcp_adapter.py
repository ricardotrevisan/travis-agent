import asyncio
import json
import os
import threading
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from utils.mcp_client import MCPClient, MCPClientError, MCPTool


_MCP_LOCK = threading.Lock()
_MCP_CLIENT: Optional[MCPClient] = None
_MCP_TOOLS: Optional[list[MCPTool]] = None
_MCP_TOOL_CALLS: Optional[Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = None
_MCP_TOOL_SCHEMAS: Optional[list[dict]] = None


def load_mcp_tools() -> Tuple[Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]], list[dict]]:
    global _MCP_TOOL_CALLS, _MCP_TOOL_SCHEMAS
    if not _get_mcp_url():
        return {}, []

    with _MCP_LOCK:
        if _MCP_TOOL_CALLS is not None and _MCP_TOOL_SCHEMAS is not None:
            return _MCP_TOOL_CALLS, _MCP_TOOL_SCHEMAS

        tools = _run_async(_ensure_tools_loaded())
        tool_calls: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}
        tool_schemas: list[dict] = []

        for tool in tools:
            if not tool.name:
                continue
            tool_calls[tool.name] = _make_tool_caller(tool.name)
            tool_schemas.append(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema or {"type": "object"},
                }
            )

        _MCP_TOOL_CALLS = tool_calls
        _MCP_TOOL_SCHEMAS = tool_schemas

    return _MCP_TOOL_CALLS, _MCP_TOOL_SCHEMAS


async def _ensure_tools_loaded() -> Iterable[MCPTool]:
    client = _get_client()
    if client is None:
        return []
    await _ensure_connected(client)
    tools = await client.tools_list()
    global _MCP_TOOLS
    _MCP_TOOLS = tools
    return tools


def _make_tool_caller(name: str) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def _call(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = _run_async(_call_async(name, args))
            return {"ok": True, "data": result}
        except Exception as exc:
            return {"ok": False, "error": f"mcp tool {name} failed: {exc}"}

    return _call


async def _call_async(name: str, args: Dict[str, Any]) -> Any:
    client = _get_client()
    if client is None:
        raise MCPClientError("MCP_URL is not configured")
    await _ensure_connected(client)
    return await client.tools_call(name, args)


async def _ensure_connected(client: MCPClient) -> None:
    if getattr(client, "_session", None) is None:
        await client.connect()
        await client.initialize()


def _get_client() -> Optional[MCPClient]:
    global _MCP_CLIENT
    if _MCP_CLIENT is not None:
        return _MCP_CLIENT

    url = _get_mcp_url()
    if not url:
        return None

    headers = _parse_json_env("MCP_HEADERS_JSON") or None
    client_info = _parse_json_env("MCP_CLIENT_INFO_JSON") or None

    timeout = os.getenv("MCP_TIMEOUT")
    timeout_value = float(timeout) if timeout else None
    enable_stream = _parse_bool_env("MCP_ENABLE_STREAM", True)

    _MCP_CLIENT = MCPClient(
        url,
        headers=headers,
        timeout=timeout_value,
        client_info=client_info,
        enable_stream=enable_stream,
    )
    return _MCP_CLIENT


def _get_mcp_url() -> str:
    return (os.getenv("MCP_URL") or "").strip()


def _parse_json_env(var_name: str) -> Optional[dict]:
    raw = (os.getenv(var_name) or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _parse_bool_env(var_name: str, default: bool) -> bool:
    raw = (os.getenv(var_name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict = {}
    error: dict = {}

    def _runner():
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:  # pragma: no cover - defensive
            error["exc"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if "exc" in error:
        raise error["exc"]
    return result.get("value")
