import json
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage

from agent.events import emit_event
from agent.loop.policy import request_approval, requires_approval
from agent.state import AgentState
from mcp_client.client import MCPClient


def _tool_payload(result: dict[str, Any]) -> Any:
    return result.get("data") if result.get("data") is not None else result


def _slim_tool_result(
    payload: Any, tool_call_id: str, workspace: Path | None, inline_max_bytes: int
) -> tuple[Any, str | None]:
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    encoded = serialized.encode("utf-8")
    if len(encoded) <= inline_max_bytes or workspace is None:
        return payload, None
    artifact = workspace / ".code_agent" / "artifacts" / f"{tool_call_id}.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(serialized, encoding="utf-8")
    preview_size = min(2048, inline_max_bytes // 2)
    slim = {
        "truncated": True,
        "preview": f"{serialized[:preview_size]}\n...\n{serialized[-preview_size:]}",
        "artifact_ref": artifact.relative_to(workspace).as_posix(),
        "size": len(encoded),
    }
    return slim, slim["artifact_ref"]


def make_mcp_tool_node(client: MCPClient, inline_max_bytes: int = 8 * 1024):
    async def mcp_tool_node(state: AgentState) -> dict[str, Any]:
        last_message = state["messages"][-1]
        tool_messages: list[ToolMessage] = []
        files_changed: list[str] = []
        command_results: list[dict[str, Any]] = []
        tool_result_refs: list[str] = []

        for call in getattr(last_message, "tool_calls", []):
            name = call["name"]
            arguments = call.get("args", {})
            await emit_event(
                "tool.started",
                {"toolCallId": call["id"], "toolName": name, "arguments": arguments},
            )
            if requires_approval(name, arguments) and not await request_approval(name, arguments):
                result = {"is_error": True, "error": "User denied this operation"}
            else:
                try:
                    result = await client.call_tool(name, arguments)
                except Exception as exc:  # Tool failures must be returned to the model loop.
                    result = {"is_error": True, "error": f"{type(exc).__name__}: {exc}"}

            payload = _tool_payload(result)
            payload, artifact_ref = _slim_tool_result(
                payload,
                call["id"],
                getattr(client, "workspace", None),
                inline_max_bytes,
            )
            if artifact_ref:
                tool_result_refs.append(artifact_ref)
            tool_messages.append(
                ToolMessage(
                    content=json.dumps(payload, ensure_ascii=False, default=str),
                    tool_call_id=call["id"],
                    name=name,
                    status="error" if result.get("is_error") else "success",
                )
            )
            if not result.get("is_error") and name in {"write_file", "apply_patch"}:
                changed_path = arguments.get("path")
                if changed_path:
                    files_changed.append(str(changed_path))
                    await emit_event("file.changed", {"path": str(changed_path)})
            if name == "execute_command":
                command_results.append(
                    {
                        "command": arguments.get("command", ""),
                        "timeout": arguments.get("timeout", 60),
                        "result": payload,
                    }
                )
            await emit_event(
                "tool.failed" if result.get("is_error") else "tool.completed",
                {
                    "toolCallId": call["id"],
                    "toolName": name,
                    "result": payload,
                    "artifactRef": artifact_ref,
                },
            )

        return {
            "messages": tool_messages,
            "files_changed": files_changed,
            "command_results": command_results,
            "tool_result_refs": tool_result_refs,
            "tool_call_count": state.get("tool_call_count", 0)
            + len(getattr(last_message, "tool_calls", [])),
            "status": "running",
        }

    return mcp_tool_node
