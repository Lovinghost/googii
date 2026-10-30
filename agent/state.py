import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list[Any], add_messages]
    task: str
    goal: str
    acceptance_criteria: list[str]
    files_changed: Annotated[list[str], operator.add]
    tool_result_refs: Annotated[list[str], operator.add]
    command_results: Annotated[list[dict[str, Any]], operator.add]  # Compatibility preview.
    iterations: int
    tool_call_count: int
    compact_count: int
    no_progress_count: int
    active_summary: dict[str, Any]
    context_manifest: dict[str, Any]
    last_progress_fingerprint: str | None
    status: Literal[
        "queued",
        "running",
        "waiting_tool",
        "waiting_user",
        "compacting",
        "verifying",
        "completed",
        "failed",
        "cancelled",
    ]
    error: dict[str, Any] | None
