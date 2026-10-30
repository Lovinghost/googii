from langchain_core.messages import AIMessage

from agent.nodes.tool_node import make_mcp_tool_node


class FakeClient:
    async def call_tool(self, name, arguments):
        if name == "execute_command":
            return {
                "is_error": False,
                "data": {"stdout": "ok\n", "stderr": "", "exit_code": 0},
            }
        return {"is_error": False, "data": {"path": arguments["path"]}}


async def test_tool_node_tracks_file_and_command_results() -> None:
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "hello.py", "content": "print('hello')"},
                "id": "call-1",
                "type": "tool_call",
            },
            {
                "name": "execute_command",
                "args": {"command": "python hello.py", "timeout": 10},
                "id": "call-2",
                "type": "tool_call",
            },
        ],
    )
    node = make_mcp_tool_node(FakeClient())
    result = await node(
        {
            "messages": [message],
            "task": "demo",
            "files_changed": [],
            "command_results": [],
            "iterations": 1,
        }
    )
    assert result["files_changed"] == ["hello.py"]
    assert result["command_results"][0]["result"]["exit_code"] == 0
    assert len(result["messages"]) == 2
