import inspect
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Iterable, Optional


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPTool:
    name: str
    description: Optional[str]
    input_schema: dict


class MCPClientError(Exception):
    pass


class MCPClient:
    def __init__(
        self,
        url: str,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
        client_info: Optional[dict] = None,
        enable_stream: bool = True,
    ) -> None:
        self._url = url
        self._headers = headers
        self._timeout = timeout
        self._client_info = client_info or {
            "name": "generic-mcp-client",
            "version": "0.1",
        }
        self._enable_stream = enable_stream
        self._notification_callbacks: list[Callable[[Any], None]] = []
        self._transport_cm = None
        self._session = None
        self.server_capabilities = None

    def on_notification(self, callback: Callable[[Any], None]) -> None:
        self._notification_callbacks.append(callback)

    async def connect(self) -> None:
        ClientSession, streamable_http_client = _load_mcp_sdk()

        http_client = _build_http_client(self._headers, self._timeout)
        streamable_kwargs = _filter_kwargs(
            streamable_http_client,
            {"http_client": http_client},
        )

        self._transport_cm = streamable_http_client(self._url, **streamable_kwargs)
        streams = await self._transport_cm.__aenter__()
        if _looks_like_session(streams):
            self._session = streams
        else:
            read_stream, write_stream = _coerce_streams(streams)
            self._session = ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=_timeout_to_timedelta(self._timeout),
                client_info=_build_client_info(self._client_info),
            )
            if hasattr(self._session, "__aenter__"):
                self._session = await self._session.__aenter__()

        _install_notification_hook(self._session, self._notification_callbacks)

    async def close(self) -> None:
        if self._session is not None:
            if hasattr(self._session, "__aexit__"):
                await self._session.__aexit__(None, None, None)
            elif hasattr(self._session, "aclose"):
                await self._session.aclose()
            elif hasattr(self._session, "close"):
                self._session.close()
        self._session = None

        if self._transport_cm is not None:
            await self._transport_cm.__aexit__(None, None, None)
        self._transport_cm = None

    async def initialize(self, params: Optional[dict] = None) -> Any:
        session = _require_session(self._session)
        result = await session.initialize()
        self.server_capabilities = _get_server_capabilities(session, result)
        return result

    async def tools_list(self) -> list[MCPTool]:
        session = _require_session(self._session)
        result = await session.list_tools()
        tools = _extract_tools(result)
        return [_normalize_tool(tool) for tool in tools]

    async def tools_call(self, name: str, arguments: dict) -> Any:
        session = _require_session(self._session)
        return await session.call_tool(name, arguments)


def _load_mcp_sdk():
    try:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except Exception as exc:
        raise MCPClientError(
            "Failed to import MCP SDK. Ensure `mcp` is installed."
        ) from exc
    return ClientSession, streamable_http_client


def _filter_kwargs(func: Callable[..., Any], kwargs: dict) -> dict:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in sig.parameters}


def _coerce_streams(streams: Any) -> tuple[Any, Any]:
    if isinstance(streams, tuple) and len(streams) >= 2:
        return streams[0], streams[1]
    read_stream = getattr(streams, "read_stream", None)
    write_stream = getattr(streams, "write_stream", None)
    if read_stream is None or write_stream is None:
        read_stream = getattr(streams, "read", None)
        write_stream = getattr(streams, "write", None)
    if read_stream is None or write_stream is None:
        raise MCPClientError("Streamable HTTP client did not return read/write streams.")
    return read_stream, write_stream


def _require_session(session: Any) -> Any:
    if session is None:
        raise MCPClientError("Client is not connected. Call connect() first.")
    return session


def _looks_like_session(obj: Any) -> bool:
    if obj is None:
        return False
    return all(hasattr(obj, attr) for attr in ("initialize", "list_tools", "call_tool"))


def _install_notification_hook(session: Any, callbacks: list[Callable[[Any], None]]) -> None:
    if not session or not callbacks:
        return
    handler = getattr(session, "_received_notification", None)
    if not callable(handler):
        return

    def _wrapped(notification: Any) -> None:
        for cb in list(callbacks):
            try:
                cb(notification)
            except Exception as exc:
                log.exception("notification callback failed: %s", exc)
        handler(notification)

    setattr(session, "_received_notification", _wrapped)


def _timeout_to_timedelta(timeout: Optional[float]) -> Optional[timedelta]:
    if timeout is None:
        return None
    try:
        return timedelta(seconds=float(timeout))
    except Exception:
        return None


def _build_client_info(client_info: dict) -> Any:
    if not client_info:
        return None
    try:
        from mcp.types import Implementation
    except Exception:
        return None
    if isinstance(client_info, Implementation):
        return client_info
    name = str(client_info.get("name") or "generic-mcp-client")
    version = str(client_info.get("version") or "0.1")
    website = client_info.get("websiteUrl")
    icons = client_info.get("icons")
    return Implementation(name=name, version=version, websiteUrl=website, icons=icons)


def _build_http_client(headers: Optional[dict], timeout: Optional[float]) -> Any:
    if not headers and not timeout:
        return None
    try:
        import httpx
    except Exception:
        return None
    kwargs: dict[str, Any] = {}
    if headers:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = httpx.Timeout(timeout)
    return httpx.AsyncClient(**kwargs)


def _get_server_capabilities(session: Any, result: Any) -> Any:
    getter = getattr(session, "get_server_capabilities", None)
    if callable(getter):
        return getter()
    if isinstance(result, dict):
        return result.get("capabilities")
    return None


def _extract_tools(result: Any) -> Iterable[Any]:
    if hasattr(result, "tools"):
        return result.tools
    if isinstance(result, dict):
        return result.get("tools", [])
    return []


def _normalize_tool(tool: Any) -> MCPTool:
    if isinstance(tool, dict):
        return MCPTool(
            name=tool.get("name"),
            description=tool.get("description"),
            input_schema=tool.get("inputSchema") or tool.get("input_schema") or {},
        )
    return MCPTool(
        name=getattr(tool, "name", None),
        description=getattr(tool, "description", None),
        input_schema=getattr(tool, "inputSchema", None)
        or getattr(tool, "input_schema", None)
        or {},
    )
