# agent/dispatcher.py
import json

class ToolDispatcher:
    def __init__(self, tools):
        self.tools = {tool.name: tool for tool in tools}

    def schemas(self):
        return [tool.as_schema() for tool in self.tools.values()]

    async def execute(self, call):
        fn_name = call.get("function", {}).get("name") or call.get("name")
        args_raw = call.get("function", {}).get("arguments") or call.get("arguments")
        args = json.loads(args_raw or "{}")
        tool = self.tools.get(fn_name)
        if not tool:
            return {"error": f"Unknown tool {fn_name}"}
        return await tool.run(args)
