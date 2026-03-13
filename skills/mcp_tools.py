import asyncio
import json
import os
import threading
import time
from typing import Any, Dict

from runtime.models import RequestContext, SkillResult
from skills.base import BaseSkill
from utils.mcp_client import MCPClient, MCPClientError, MCPTool


_MCP_LOCK = threading.RLock()
_MCP_CLIENT: MCPClient | None = None
_MCP_TOOLS: list[MCPTool] | None = None
_MCP_TOOLS_LOADED_AT: float | None = None
_MCP_LOOP = None
_MCP_LOOP_THREAD: threading.Thread | None = None
_MCP_LOAD_TIMEOUT_SECONDS = float(os.getenv("MCP_LOAD_TIMEOUT", "5"))
_MCP_TOOLS_CACHE_TTL_SECONDS = float(os.getenv("MCP_TOOLS_CACHE_TTL", "300"))
_MCP_GMAIL_USER_EMAIL = (os.getenv("MCP_GMAIL_USER_EMAIL") or "").strip()
_GMAIL_TOOL_NAMES = {
    "search_gmail_messages",
    "get_gmail_message_content",
    "get_gmail_messages_content_batch",
    "get_gmail_attachment_content",
    "send_gmail_message",
    "draft_gmail_message",
    "get_gmail_thread_content",
    "get_gmail_threads_content_batch",
    "list_gmail_labels",
    "manage_gmail_label",
    "list_gmail_filters",
    "manage_gmail_filter",
    "modify_gmail_message_labels",
    "batch_modify_gmail_message_labels",
}

# These are wrapped by gmail_list and should not appear directly in the planner.
_GMAIL_PLANNER_HIDDEN = {
    "search_gmail_messages",
    "get_gmail_message_content",
    "get_gmail_messages_content_batch",
    "get_gmail_thread_content",
    "get_gmail_threads_content_batch",
}


class MCPDynamicToolSkill(BaseSkill):
    planner_visible = True

    def __init__(self, name: str, description: str, tool_name: str) -> None:
        self.name = name
        self.description = description
        self._tool_name = tool_name

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        try:
            final_args = dict(args or {})
            if self._tool_name in _GMAIL_TOOL_NAMES:
                if final_args.get("user_google_email") != _MCP_GMAIL_USER_EMAIL:
                    print(
                        "[mcp] forcing gmail user_google_email "
                        f"tool={self._tool_name} value={_MCP_GMAIL_USER_EMAIL}"
                    )
                final_args["user_google_email"] = _MCP_GMAIL_USER_EMAIL
            result = _run_async(_call_tool(self._tool_name, final_args))
            user_text = _format_user_text(result)
            return SkillResult(ok=True, output={"result": result}, user_visible_text=user_text)
        except Exception as exc:
            return SkillResult(ok=False, error=f"mcp tool {self._tool_name} failed: {exc}")


def load_mcp_skills(existing_names: set[str]) -> dict[str, BaseSkill]:
    url = _get_mcp_url()
    if not url:
        return {}

    with _MCP_LOCK:
        tools = _run_async(_ensure_tools_loaded(), timeout=_MCP_LOAD_TIMEOUT_SECONDS)
        skills: dict[str, BaseSkill] = {}
        for tool in tools:
            base_name = (tool.name or "").strip()
            if not base_name:
                continue
            skill_name = base_name
            if skill_name in existing_names or skill_name in skills:
                skill_name = f"mcp_{base_name}"
            description = tool.description or ""
            if skill_name != base_name:
                description = f"{description} (MCP tool: {base_name})".strip()
            skill = MCPDynamicToolSkill(
                name=skill_name,
                description=description or f"MCP tool: {base_name}",
                tool_name=base_name,
            )
            # Hide raw Gmail tools from the planner — gmail_list wraps them
            # into a single structured workflow and should be used instead.
            if base_name in _GMAIL_PLANNER_HIDDEN:
                skill.planner_visible = False
            skills[skill_name] = skill
        return skills


def invalidate_mcp_tools_cache() -> None:
    global _MCP_TOOLS, _MCP_TOOLS_LOADED_AT
    with _MCP_LOCK:
        _MCP_TOOLS = None
        _MCP_TOOLS_LOADED_AT = None


async def _ensure_tools_loaded() -> list[MCPTool]:
    global _MCP_TOOLS
    if _MCP_TOOLS is not None and _cache_is_fresh():
        return _MCP_TOOLS
    try:
        client = _get_client()
        await _ensure_connected(client)
        _MCP_TOOLS = await client.tools_list()
        _mark_cache_loaded()
        return _MCP_TOOLS
    except Exception:
        _reset_client()
        raise


async def _call_tool(name: str, args: Dict[str, Any]) -> Any:
    try:
        client = _get_client()
        await _ensure_connected(client)
        return await client.tools_call(name, args or {})
    except Exception:
        _reset_client()
        raise


def _reset_client() -> None:
    global _MCP_CLIENT, _MCP_TOOLS, _MCP_TOOLS_LOADED_AT
    _MCP_CLIENT = None
    _MCP_TOOLS = None
    _MCP_TOOLS_LOADED_AT = None


async def _ensure_connected(client: MCPClient) -> None:
    if getattr(client, "_session", None) is None:
        await client.connect()
        await client.initialize()


def _get_client() -> MCPClient:
    global _MCP_CLIENT
    if _MCP_CLIENT is not None:
        return _MCP_CLIENT
    url = _get_mcp_url()
    if not url:
        raise MCPClientError("MCP_URL is not configured")

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


def _parse_json_env(var_name: str) -> dict | None:
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


def _run_async(coro: Any, timeout: float | None = None) -> Any:
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def _ensure_loop():
    global _MCP_LOOP, _MCP_LOOP_THREAD
    if _MCP_LOOP is not None:
        return _MCP_LOOP

    with _MCP_LOCK:
        if _MCP_LOOP is not None:
            return _MCP_LOOP

        loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        _MCP_LOOP = loop
        _MCP_LOOP_THREAD = thread
        return _MCP_LOOP


def _cache_is_fresh() -> bool:
    if _MCP_TOOLS_LOADED_AT is None:
        return False
    return (time.time() - _MCP_TOOLS_LOADED_AT) < _MCP_TOOLS_CACHE_TTL_SECONDS


def _mark_cache_loaded() -> None:
    global _MCP_TOOLS_LOADED_AT
    _MCP_TOOLS_LOADED_AT = time.time()


def _format_user_text(result: Any) -> str:
    text = _extract_text_result(result)
    if isinstance(text, str) and text.strip():
        cleaned = text.strip()
        usage_marker = "\n\n💡 USAGE:"
        if usage_marker in cleaned:
            cleaned = cleaned.split(usage_marker, 1)[0].strip()
        return cleaned
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)


def _extract_text_result(result: Any) -> str | None:
    if isinstance(result, str):
        return result

    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            structured_text = structured.get("result")
            if isinstance(structured_text, str):
                return structured_text
        direct_result = result.get("result")
        if isinstance(direct_result, str):
            return direct_result
        content = result.get("content")
        text = _join_text_content(content)
        if text:
            return text
        return None

    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        structured_text = structured.get("result")
        if isinstance(structured_text, str):
            return structured_text
    direct_result = getattr(result, "result", None)
    if isinstance(direct_result, str):
        return direct_result
    content = getattr(result, "content", None)
    text = _join_text_content(content)
    if text:
        return text
    return None


def _join_text_content(content: Any) -> str | None:
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            value = item.get("text")
        else:
            value = getattr(item, "text", None)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n\n".join(parts) if parts else None
