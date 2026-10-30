import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass(frozen=True)
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    server: str

    def model_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema,
        }


class MCPClient:
    """Starts both stdio MCP servers and presents one unified tool registry."""

    def __init__(self, project_root: Path, workspace: Path, session_id: str, settings: Any):
        self.project_root = project_root.resolve()
        self.workspace = workspace.resolve()
        self.session_id = session_id
        self.settings = settings
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, MCPTool] = {}

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in [str(self.project_root), python_path] if value
        )
        environment.update(
            {
                "WORKSPACE_ROOT": str(self.workspace),
                "AGENT_SESSION_ID": self.session_id,
                "SANDBOX_IMAGE": self.settings.sandbox_image,
                "SANDBOX_CPUS": str(self.settings.sandbox_cpus),
                "SANDBOX_MEMORY": self.settings.sandbox_memory,
                "SANDBOX_PIDS_LIMIT": str(self.settings.sandbox_pids_limit),
                "COMMAND_TIMEOUT_MAX": str(self.settings.command_timeout_max),
            }
        )
        return environment

    async def start(self) -> None:
        if self._stack is not None:
            return
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            for server, module in (
                ("filesystem", "servers.filesystem.server"),
                ("terminal", "servers.terminal.server"),
            ):
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", module],
                    env=self._environment(),
                )
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(parameters)
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                self._sessions[server] = session
                tools = await session.list_tools()
                for tool in tools.tools:
                    if tool.name in self._tools:
                        raise RuntimeError(f"Duplicate MCP tool name: {tool.name}")
                    schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
                    self._tools[tool.name] = MCPTool(
                        name=tool.name,
                        description=tool.description or "",
                        input_schema=schema,
                        server=server,
                    )
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack

    @property
    def model_tools(self) -> list[dict[str, Any]]:
        if self._stack is None:
            raise RuntimeError("MCP client has not been started")
        return [tool.model_schema() for tool in self._tools.values()]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"is_error": True, "error": f"Unknown MCP tool: {name}"}
        result = await self._sessions[tool.server].call_tool(name, arguments=arguments)
        structured = getattr(result, "structuredContent", None)
        if structured is None:
            structured = getattr(result, "structured_content", None)
        text_parts = [
            block.text for block in result.content if getattr(block, "type", None) == "text"
        ]
        if structured is None and len(text_parts) == 1:
            try:
                structured = json.loads(text_parts[0])
            except json.JSONDecodeError:
                pass
        return {
            "is_error": bool(getattr(result, "isError", False)),
            "data": structured,
            "text": "\n".join(text_parts),
        }

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._sessions.clear()
            self._tools.clear()
