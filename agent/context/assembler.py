from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage

from agent.context.budget import TokenBudget, estimate_tokens, message_text


@dataclass(slots=True)
class ContextBuild:
    messages: list[BaseMessage]
    manifest: dict[str, int]
    summary: dict[str, Any]
    compacted: bool


class ContextAssembler:
    def __init__(
        self,
        workspace: Path,
        system_prompt: str,
        budget: TokenBudget,
        recent_message_limit: int,
    ) -> None:
        self.workspace = workspace
        self.system_prompt = system_prompt
        self.budget = budget
        self.recent_message_limit = recent_message_limit

    def _project_rules(self) -> str:
        rules: list[str] = []
        current = self.workspace.resolve()
        for directory in [current, *current.parents]:
            candidate = directory / "AGENTS.md"
            if candidate.is_file():
                rules.append(candidate.read_text(encoding="utf-8", errors="replace")[:16_000])
            if directory == self.workspace.resolve():
                break
        return "\n\n".join(rules)

    @staticmethod
    def _summarize(messages: list[BaseMessage], previous: dict[str, Any]) -> dict[str, Any]:
        excerpts = []
        for message in messages[-12:]:
            text = message_text(message).strip().replace("\n", " ")
            if text:
                excerpts.append(text[:240])
        return {
            "objective": previous.get("objective", ""),
            "constraints": previous.get("constraints", []),
            "completed": previous.get("completed", []),
            "pending": previous.get("pending", []),
            "history_excerpts": excerpts,
            "next_action": previous.get("next_action", "Continue the current task."),
        }

    def build(self, state: dict[str, Any]) -> ContextBuild:
        rules = self._project_rules()
        task = state.get("task", "")
        summary = state.get("active_summary") or {}
        fixed_parts = [self.system_prompt, f"Current task:\n{task}"]
        if rules:
            fixed_parts.append(f"Project rules (authoritative):\n{rules}")
        if summary:
            fixed_parts.append(f"Session summary:\n{summary}")

        system_text = "\n\n".join(fixed_parts)
        system_tokens = estimate_tokens(system_text)
        remaining = max(0, self.budget.input_limit - system_tokens)
        selected: list[BaseMessage] = []
        all_messages = list(state.get("messages", []))
        candidates = all_messages[-self.recent_message_limit :]
        dropped: list[BaseMessage] = all_messages[: -self.recent_message_limit]
        for message in reversed(candidates):
            cost = estimate_tokens(message_text(message))
            if cost <= remaining:
                selected.append(message)
                remaining -= cost
            else:
                dropped.append(message)
        selected.reverse()
        compacted = bool(dropped)
        if compacted:
            summary = self._summarize(dropped, summary)
            summary["objective"] = summary.get("objective") or task
            system_text += f"\n\nCompacted session summary:\n{summary}"
            while selected and (
                estimate_tokens(system_text)
                + sum(estimate_tokens(message_text(message)) for message in selected)
                > self.budget.input_limit
            ):
                dropped.append(selected.pop(0))
                summary = self._summarize(dropped, summary)
                system_text = "\n\n".join(fixed_parts)
                system_text += f"\n\nCompacted session summary:\n{summary}"

        recent_tokens = sum(estimate_tokens(message_text(message)) for message in selected)
        manifest = {
            "input_limit": self.budget.input_limit,
            "system_tokens": estimate_tokens(self.system_prompt),
            "rules_tokens": estimate_tokens(rules) if rules else 0,
            "summary_tokens": estimate_tokens(str(summary)) if summary else 0,
            "recent_message_tokens": recent_tokens,
            "total_estimated_tokens": estimate_tokens(system_text) + recent_tokens,
        }
        return ContextBuild(
            messages=[SystemMessage(content=system_text), *selected],
            manifest=manifest,
            summary=summary,
            compacted=compacted,
        )
