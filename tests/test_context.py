from langchain_core.messages import HumanMessage

from agent.context.assembler import ContextAssembler
from agent.context.budget import TokenBudget


def test_context_compacts_old_messages_and_loads_project_rules(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("Always run tests.", encoding="utf-8")
    assembler = ContextAssembler(
        workspace=tmp_path,
        system_prompt="You are a code agent.",
        budget=TokenBudget(4096, 0.15, 0.10),
        recent_message_limit=2,
    )

    result = assembler.build(
        {
            "task": "fix it",
            "messages": [HumanMessage(content=f"message-{index}") for index in range(4)],
            "active_summary": {},
        }
    )

    assert result.compacted is True
    assert result.summary["history_excerpts"] == ["message-0", "message-1"]
    assert "Always run tests." in result.messages[0].content
    assert [message.content for message in result.messages[1:]] == ["message-2", "message-3"]
    assert result.manifest["total_estimated_tokens"] <= assembler.budget.input_limit
