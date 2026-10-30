import asyncio
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False

    def as_dict(self) -> dict[str, str | int | bool]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
        }


class DockerError(RuntimeError):
    pass


class DockerManager:
    """Owns the single network-isolated container for one Agent session."""

    def __init__(
        self,
        session_id: str,
        workspace: Path,
        image: str,
        cpus: float,
        memory: str,
        pids_limit: int,
    ) -> None:
        self.workspace = workspace.resolve()
        self.image = image
        self.cpus = cpus
        self.memory = memory
        self.pids_limit = pids_limit
        slug = re.sub(r"[^a-z0-9_.-]", "-", session_id.lower())[:24].strip("-.") or "session"
        digest = hashlib.sha256(session_id.encode()).hexdigest()[:10]
        self.container_name = f"code-agent-{slug}-{digest}"
        self._started = False
        self._engine_checked = False
        self._lock = asyncio.Lock()

    async def _run_docker(self, args: Sequence[str], timeout: float = 120) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise DockerError("Docker CLI is not installed or not on PATH") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise DockerError(f"Docker operation timed out after {timeout:g}s") from None
        return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def ensure_started(self) -> None:
        if self._started:
            return
        async with self._lock:
            if self._started:
                return
            if not self._engine_checked:
                try:
                    code, stdout, stderr = await self._run_docker(
                        ["version", "--format", "{{.Server.Version}}"], timeout=5
                    )
                except DockerError as exc:
                    raise DockerError(f"Docker Engine is unavailable: {exc}") from exc
                if code != 0 or not stdout.strip():
                    detail = stderr.strip() or "Docker server did not return a version"
                    raise DockerError(f"Docker Engine is unavailable: {detail}")
                self._engine_checked = True
            self.workspace.mkdir(parents=True, exist_ok=True)
            code, stdout, _ = await self._run_docker(
                ["inspect", "-f", "{{.State.Running}}", self.container_name], timeout=15
            )
            if code == 0:
                if stdout.strip() != "true":
                    code, _, stderr = await self._run_docker(
                        ["start", self.container_name], timeout=30
                    )
                    if code != 0:
                        raise DockerError(f"Unable to start sandbox: {stderr.strip()}")
                self._started = True
                return

            args = [
                "run",
                "--detach",
                "--name",
                self.container_name,
                "--label",
                "code-agent.managed=true",
                "--network",
                "none",
                "--cpus",
                str(self.cpus),
                "--memory",
                self.memory,
                "--pids-limit",
                str(self.pids_limit),
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--mount",
                f"type=bind,source={self.workspace},target=/workspace",
                "--workdir",
                "/workspace",
                self.image,
                "sleep",
                "infinity",
            ]
            code, _, stderr = await self._run_docker(args, timeout=180)
            if code != 0:
                raise DockerError(f"Unable to create sandbox: {stderr.strip()}")
            self._started = True

    async def execute(self, command: str, timeout: int) -> CommandResult:
        if not command.strip():
            raise ValueError("command must not be empty")
        await self.ensure_started()
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            self.container_name,
            "timeout",
            "--signal=KILL",
            f"{timeout}s",
            "sh",
            "-lc",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout + 10)
        except TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            return CommandResult(
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace") + "\nHost-side timeout exceeded.",
                124,
                True,
            )
        return CommandResult(
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
            process.returncode,
            process.returncode == 124,
        )

    async def remove(self) -> None:
        if not self._started:
            return
        code, _, _ = await self._run_docker(["inspect", self.container_name], timeout=15)
        if code == 0:
            await self._run_docker(["rm", "--force", self.container_name], timeout=30)
        self._started = False
        self._engine_checked = False
