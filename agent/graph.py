from typing import Any, Literal

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from agent.context import ContextAssembler
from agent.events import emit_event
from agent.loop import progress_fingerprint
from agent.nodes.agent_node import make_agent_node
from agent.nodes.tool_node import make_mcp_tool_node
from agent.state import AgentState
from mcp_client.client import MCPClient


def _route_after_agent(
    state: AgentState, max_iterations: int, max_tool_calls: int
) -> Literal["mcp_tools", "iteration_limit", "__end__"]:
    message = state["messages"][-1]
    if not getattr(message, "tool_calls", None):
        return END
    if (
        state.get("iterations", 0) >= max_iterations
        or state.get("tool_call_count", 0) >= max_tool_calls
    ):
        return "iteration_limit"
    return "mcp_tools"


async def iteration_limit_node(state: AgentState) -> dict[str, Any]:
    await emit_event("turn.failed", {"reason": "budget_exhausted"})
    return {
        "messages": [
            AIMessage(
                content=(
                    "The agent stopped because it reached the configured iteration limit. "
                    "Review the latest tool results or continue the same session."
                )
            )
        ],
        "status": "failed",
        "error": {"type": "budget_exhausted"},
    }


async def progress_guard_node(state: AgentState) -> dict[str, Any]:
    fingerprint = progress_fingerprint(state)
    previous = state.get("last_progress_fingerprint")
    no_progress = state.get("no_progress_count", 0) + 1 if previous == fingerprint else 0
    await emit_event(
        "progress.checked",
        {"noProgressSteps": no_progress, "fingerprint": fingerprint[:12]},
    )
    return {"last_progress_fingerprint": fingerprint, "no_progress_count": no_progress}


async def stalled_node(state: AgentState) -> dict[str, Any]:
    await emit_event("turn.waiting_user", {"reason": "no_progress"})
    return {
        "messages": [
            AIMessage(
                content="The agent stopped after repeated steps without measurable progress."
            )
        ],
        "status": "waiting_user",
        "error": {"type": "no_progress"},
    }


async def verify_node(state: AgentState) -> dict[str, Any]:
    files_changed = list(dict.fromkeys(state.get("files_changed", [])))
    commands = state.get("command_results", [])
    verified = not files_changed or any(
        command.get("result", {}).get("exit_code") == 0 for command in commands
    )
    await emit_event(
        "verification.completed",
        {
            "filesChanged": files_changed,
            "commands": len(commands),
            "verified": verified,
        },
    )
    if verified:
        return {"status": "completed"}
    await emit_event("turn.waiting_user", {"reason": "changes_not_verified"})
    return {
        "messages": [
            AIMessage(
                content=(
                    "The code changes were made, but no successful verification command was "
                    "recorded. Run the relevant tests before treating this task as complete."
                )
            )
        ],
        "status": "waiting_user",
        "error": {"type": "changes_not_verified"},
    }


def build_graph(
    model: Any,
    client: MCPClient,
    checkpointer: Any,
    max_iterations: int,
    max_tool_calls: int,
    max_no_progress_steps: int,
    context_assembler: ContextAssembler,
    tool_inline_max_bytes: int,
) -> Any:
    model_with_tools = model.bind_tools(client.model_tools)
    builder = StateGraph(AgentState)
    builder.add_node("agent", make_agent_node(model_with_tools, context_assembler))
    builder.add_node("mcp_tools", make_mcp_tool_node(client, tool_inline_max_bytes))
    builder.add_node("progress_guard", progress_guard_node)
    builder.add_node("stalled", stalled_node)
    builder.add_node("verify", verify_node)
    builder.add_node("iteration_limit", iteration_limit_node)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent",
        lambda state: _route_after_agent(state, max_iterations, max_tool_calls),
        {
            "mcp_tools": "mcp_tools",
            "iteration_limit": "iteration_limit",
            END: "verify",
        },
    )
    builder.add_edge("mcp_tools", "progress_guard")
    builder.add_conditional_edges(
        "progress_guard",
        lambda state: "stalled"
        if state.get("no_progress_count", 0) >= max_no_progress_steps
        else "agent",
        {"stalled": "stalled", "agent": "agent"},
    )
    builder.add_edge("stalled", END)
    builder.add_edge("verify", END)
    builder.add_edge("iteration_limit", END)
    return builder.compile(checkpointer=checkpointer)
