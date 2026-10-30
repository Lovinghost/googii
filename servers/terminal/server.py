import asyncio
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from sandbox.docker_manager import DockerManager


def _manager_from_env() -> tuple[DockerManager, int]:
    max_timeout = int(os.environ.get("COMMAND_TIMEOUT_MAX", "300"))
    manager = DockerManager(
        session_id=os.environ.get("AGENT_SESSION_ID", "default"),
        workspace=Path(os.environ.get("WORKSPACE_ROOT", "workspace")),
        image=os.environ.get("SANDBOX_IMAGE", "python:3.12-slim"),
        cpus=float(os.environ.get("SANDBOX_CPUS", "1.0")),
        memory=os.environ.get("SANDBOX_MEMORY", "512m"),
        pids_limit=int(os.environ.get("SANDBOX_PIDS_LIMIT", "256")),
    )
    return manager, max_timeout


manager, command_timeout_max = _manager_from_env()
mcp = FastMCP("terminal")


@mcp.tool()
async def execute_command(command: str, timeout: int = 60) -> dict[str, Any]:
    """Execute a shell command inside the session's isolated Docker workspace."""
    if timeout < 1 or timeout > command_timeout_max:
        raise ValueError(f"timeout must be between 1 and {command_timeout_max} seconds")
    return (await manager.execute(command, timeout)).as_dict()


def main() -> None:
    try:
        mcp.run(transport="stdio")
    finally:
        asyncio.run(manager.remove())


if __name__ == "__main__":
    main()
