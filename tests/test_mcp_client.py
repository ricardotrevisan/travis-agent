import unittest
from unittest.mock import patch

from utils.mcp_client import MCPClient, MCPTool


class _FakeStreamableHTTPClient:
    def __init__(self, streams):
        self._streams = streams

    async def __aenter__(self):
        return self._streams

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, read_stream, write_stream):
        self.read_stream = read_stream
        self.write_stream = write_stream
        self.init_params = None
        self._capabilities = {"tools": True}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def initialize(self, params):
        self.init_params = params
        return {"capabilities": self._capabilities}

    def get_server_capabilities(self):
        return self._capabilities

    async def list_tools(self):
        class _ToolObj:
            name = "search"
            description = "Search tool"
            input_schema = {"type": "object"}

        return {"tools": [{"name": "calc", "description": None, "inputSchema": {}} , _ToolObj()]}

    async def call_tool(self, name, arguments):
        return {"name": name, "arguments": arguments}

    def _received_notification(self, notification):
        self.last_notification = notification


def _fake_sdk_loader():
    def _fake_streamable_http_client(url, **kwargs):
        return _FakeStreamableHTTPClient(("read", "write"))

    return _FakeSession, _fake_streamable_http_client


class TestMCPClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = MCPClient("http://localhost:8000")

    async def asyncTearDown(self):
        await self.client.close()

    async def test_initialize_stores_capabilities(self):
        with patch("utils.mcp_client._load_mcp_sdk", _fake_sdk_loader):
            await self.client.connect()
            await self.client.initialize()
            self.assertEqual(self.client.server_capabilities, {"tools": True})
            self.assertEqual(
                self.client._session.init_params,
                {"clientInfo": {"name": "generic-mcp-client", "version": "0.1"}},
            )

    async def test_tools_list_normalizes(self):
        with patch("utils.mcp_client._load_mcp_sdk", _fake_sdk_loader):
            await self.client.connect()
            tools = await self.client.tools_list()
            self.assertIsInstance(tools[0], MCPTool)
            self.assertEqual(tools[0].name, "calc")
            self.assertEqual(tools[1].name, "search")

    async def test_tools_call_delegates(self):
        with patch("utils.mcp_client._load_mcp_sdk", _fake_sdk_loader):
            await self.client.connect()
            result = await self.client.tools_call("echo", {"msg": "hi"})
            self.assertEqual(result, {"name": "echo", "arguments": {"msg": "hi"}})

    async def test_notification_callback(self):
        got = []

        def _cb(notification):
            got.append(notification)

        with patch("utils.mcp_client._load_mcp_sdk", _fake_sdk_loader):
            self.client.on_notification(_cb)
            await self.client.connect()
            self.client._session._received_notification({"event": "tools/updated"})
            self.assertEqual(got, [{"event": "tools/updated"}])


if __name__ == "__main__":
    unittest.main()
