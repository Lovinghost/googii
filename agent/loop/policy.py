import contextvars
import re
from collections.abc import Awaitable, Callable
from typing import Any

ApprovalRequester = Callable[[str, dict[str, Any]], Awaitable[bool]]
_approval_requester: contextvars.ContextVar[ApprovalRequester | None] = contextvars.ContextVar(
    "agent_approval_requester", default=None
)

DANGEROUS_COMMANDS = re.compile(
    r"(^|\s)(rm\s+-rf|git\s+reset\s+--hard|git\s+clean\s+-fd|format\s+|mkfs\b|shutdown\b|reboot\b)",
    re.IGNORECASE,
)


def bind_approval_requester(requester: ApprovalRequester):
    return _approval_requester.set(requester)


def reset_approval_requester(token: contextvars.Token) -> None:
    _approval_requester.reset(token)


def requires_approval(tool_name: str, arguments: dict[str, Any]) -> bool:
    if tool_name != "execute_command":
        return False
    return bool(DANGEROUS_COMMANDS.search(str(arguments.get("command", ""))))


async def request_approval(tool_name: str, arguments: dict[str, Any]) -> bool:
    requester = _approval_requester.get()
    if requester is None:
        # Synchronous/local compatibility API has no interactive approval channel.
        return True
    return await requester(tool_name, arguments)
