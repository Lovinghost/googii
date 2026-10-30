from dataclasses import dataclass
from typing import Any


def message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return str(content)


def estimate_tokens(value: str) -> int:
    # Deterministic lightweight estimate; provider usage metadata remains authoritative.
    return max(1, (len(value) + 3) // 4)


@dataclass(frozen=True, slots=True)
class TokenBudget:
    context_limit: int
    output_reserve_ratio: float
    safety_buffer_ratio: float

    @property
    def input_limit(self) -> int:
        reserved = self.context_limit * (
            self.output_reserve_ratio + self.safety_buffer_ratio
        )
        return max(1, int(self.context_limit - reserved))
