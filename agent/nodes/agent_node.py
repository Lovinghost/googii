from typing import Any

from agent.context import ContextAssembler
from agent.events import emit_event
from agent.state import AgentState

SYSTEM_PROMPT = """You are a code agent working in /workspace.

Use only the provided MCP tools to inspect files, edit files, and execute commands.
Prefer apply_patch over replacing a whole existing file. Inspect relevant code before editing it.
After making changes, run the smallest relevant test or validation command. Never claim a command
passed unless its tool result shows exit_code 0. Finish with a concise summary of changes and tests.
"""


def make_agent_node(model_with_tools: Any, context_assembler: ContextAssembler):
    async def agent_node(state: AgentState) -> dict[str, Any]:
        context = context_assembler.build(state)
        await emit_event("context.built", context.manifest)
        if context.compacted:
            await emit_event("compact.completed", {"summary": context.summary})
        await emit_event("llm.started", {"iteration": state.get("iterations", 0) + 1})
        response = await model_with_tools.ainvoke(context.messages)
        await emit_event(
            "llm.completed",
            {
                "iteration": state.get("iterations", 0) + 1,
                "toolCallCount": len(getattr(response, "tool_calls", []) or []),
            },
        )
        result = {
            "messages": [response],
            "iterations": state.get("iterations", 0) + 1,
            "context_manifest": context.manifest,
            "active_summary": context.summary,
            "compact_count": state.get("compact_count", 0) + int(context.compacted),
            "status": "waiting_tool" if getattr(response, "tool_calls", None) else "verifying",
        }
        return result

    return agent_node
