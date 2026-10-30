import asyncio
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agent.config import Settings
from agent.context import ContextAssembler
from agent.context.budget import TokenBudget
from agent.events import EventJournal, bind_event_sink, reset_event_sink
from agent.graph import build_graph
from agent.loop.policy import bind_approval_requester, reset_approval_requester
from agent.nodes.agent_node import SYSTEM_PROMPT
from mcp_client.client import MCPClient

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "waiting_user"}


def create_model(settings: Settings) -> Any:
    if settings.model_provider == "anthropic":
        return ChatAnthropic(model=settings.model_name, temperature=0)
    return ChatOpenAI(model=settings.model_name, temperature=0)


def _safe_workspace(root: Path, requested: str) -> Path:
    root = root.resolve()
    candidate = Path(requested).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Workspace must be inside configured root: {root}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _runtime_session_id(user_name: str, session_id: str) -> str:
    safe_user = re.sub(r"[^a-zA-Z0-9_.-]", "-", user_name)[:48] or "anonymous"
    return f"{safe_user}:{session_id}"


class AgentSession:
    def __init__(
        self,
        session_id: str,
        runtime_session_id: str,
        workspace: Path,
        settings: Settings,
        checkpointer: Any,
    ) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.client = MCPClient(
            project_root=project_root,
            workspace=workspace,
            session_id=runtime_session_id,
            settings=settings,
        )
        self.session_id = session_id
        self.runtime_session_id = runtime_session_id
        self.workspace = workspace
        self.settings = settings
        self.checkpointer = checkpointer
        self.graph: Any | None = None
        self.lock = asyncio.Lock()
        self.last_state: dict[str, Any] | None = None

    async def start(self) -> None:
        if self.graph is not None:
            return
        await self.client.start()
        assembler = ContextAssembler(
            workspace=self.workspace,
            system_prompt=SYSTEM_PROMPT,
            budget=TokenBudget(
                context_limit=self.settings.model_context_limit,
                output_reserve_ratio=self.settings.output_token_reserve_ratio,
                safety_buffer_ratio=self.settings.context_safety_buffer_ratio,
            ),
            recent_message_limit=self.settings.recent_message_limit,
        )
        self.graph = build_graph(
            model=create_model(self.settings),
            client=self.client,
            checkpointer=self.checkpointer,
            max_iterations=self.settings.agent_max_iterations,
            max_tool_calls=self.settings.max_tool_calls,
            max_no_progress_steps=self.settings.max_no_progress_steps,
            context_assembler=assembler,
            tool_inline_max_bytes=self.settings.tool_inline_max_bytes,
        )

    async def run(self, task: str) -> dict[str, Any]:
        async with self.lock:
            await self.start()
            assert self.graph is not None
            state = await self.graph.ainvoke(
                {
                    "messages": [HumanMessage(content=task)],
                    "task": task,
                    "goal": task,
                    "acceptance_criteria": [],
                    "files_changed": [],
                    "tool_result_refs": [],
                    "command_results": [],
                    "iterations": 0,
                    "tool_call_count": 0,
                    "compact_count": 0,
                    "no_progress_count": 0,
                    "active_summary": {},
                    "context_manifest": {},
                    "last_progress_fingerprint": None,
                    "status": "running",
                    "error": None,
                },
                config={
                    "configurable": {"thread_id": self.runtime_session_id},
                    "recursion_limit": self.settings.agent_max_iterations * 4 + 10,
                },
            )
            self.last_state = state
            response = next(
                (
                    message.content
                    for message in reversed(state["messages"])
                    if isinstance(message, AIMessage) and not message.tool_calls
                ),
                "",
            )
            return {
                "session_id": self.session_id,
                "response": response,
                "files_changed": list(dict.fromkeys(state.get("files_changed", []))),
                "command_results": state.get("command_results", []),
                "status": state.get("status", "completed"),
                "context_manifest": state.get("context_manifest", {}),
                "active_summary": state.get("active_summary", {}),
            }

    async def close(self) -> None:
        await self.client.close()
        self.graph = None


@dataclass(slots=True)
class RunRecord:
    run_id: str
    session_id: str
    user_name: str
    workspace: str
    prompt: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    journal: EventJournal | None = None
    task: asyncio.Task[None] | None = None
    decisions: dict[str, str] = field(default_factory=dict)
    approval_waiters: dict[str, asyncio.Future[bool]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "sessionId": self.session_id,
            "userName": self.user_name,
            "workspace": self.workspace,
            "status": self.status,
            "createdAt": self.created_at,
            "finishedAt": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class CodeAgentService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sessions: dict[str, AgentSession] = {}
        self.runs: dict[str, RunRecord] = {}
        self._stack = AsyncExitStack()
        self._checkpointer: Any | None = None
        self._sessions_lock = asyncio.Lock()
        self._runs_lock = asyncio.Lock()

    async def start(self) -> None:
        self.settings.workspace_root.mkdir(parents=True, exist_ok=True)
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        await self._stack.__aenter__()
        self._checkpointer = await self._stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(self.settings.database_path))
        )

    async def get_session(
        self, session_id: str, user_name: str = "local", workspace: str = "."
    ) -> AgentSession:
        runtime_id = _runtime_session_id(user_name, session_id)
        resolved_workspace = _safe_workspace(self.settings.workspace_root, workspace)
        async with self._sessions_lock:
            session = self.sessions.get(runtime_id)
            if session is None:
                if self._checkpointer is None:
                    raise RuntimeError("CodeAgentService has not been started")
                session = AgentSession(
                    session_id,
                    runtime_id,
                    resolved_workspace,
                    self.settings,
                    self._checkpointer,
                )
                self.sessions[runtime_id] = session
            elif session.workspace != resolved_workspace:
                raise ValueError("A session cannot switch workspace")
            return session

    async def run(
        self,
        session_id: str,
        task: str,
        user_name: str = "local",
        workspace: str = ".",
    ) -> dict[str, Any]:
        return await (await self.get_session(session_id, user_name, workspace)).run(task)

    async def start_run(
        self,
        prompt: str,
        workspace: str,
        user_name: str,
        session_id: str | None = None,
    ) -> RunRecord:
        # Validate before returning the streaming response so invalid paths become HTTP 400.
        _safe_workspace(self.settings.workspace_root, workspace)
        session_id = session_id or str(uuid4())
        run_id = str(uuid4())
        journal = EventJournal(run_id, session_id, self.settings.event_history_limit)
        record = RunRecord(
            run_id=run_id,
            session_id=session_id,
            user_name=user_name,
            workspace=workspace,
            prompt=prompt,
            journal=journal,
        )
        async with self._runs_lock:
            self.runs[run_id] = record
        record.task = asyncio.create_task(self._execute_run(record), name=f"agent-run-{run_id}")
        return record

    async def _execute_run(self, record: RunRecord) -> None:
        assert record.journal is not None
        event_token = bind_event_sink(record.journal.publish)

        async def request_approval(tool_name: str, arguments: dict[str, Any]) -> bool:
            request_id = str(uuid4())
            future = asyncio.get_running_loop().create_future()
            record.approval_waiters[request_id] = future
            record.status = "waiting_user"
            await record.journal.publish(
                "approval.required",
                {
                    "requestId": request_id,
                    "toolName": tool_name,
                    "arguments": arguments,
                    "message": "This operation requires user approval.",
                },
            )
            try:
                approved = await future
                record.status = "running"
                return approved
            finally:
                record.approval_waiters.pop(request_id, None)

        approval_token = bind_approval_requester(request_approval)
        try:
            record.status = "running"
            await record.journal.publish("run.started", {"workspace": record.workspace})
            result = await asyncio.wait_for(
                self.run(
                    record.session_id,
                    record.prompt,
                    record.user_name,
                    record.workspace,
                ),
                timeout=self.settings.max_wall_time_seconds,
            )
            record.result = result
            record.status = result.get("status", "completed")
            if record.status not in TERMINAL_STATUSES:
                record.status = "completed"
            await record.journal.publish(
                "run.completed" if record.status == "completed" else f"run.{record.status}",
                {
                    "content": result.get("response", ""),
                    "filesChanged": result.get("files_changed", []),
                    "contextManifest": result.get("context_manifest", {}),
                },
            )
        except asyncio.CancelledError:
            record.status = "cancelled"
            await record.journal.publish("run.cancelled", {})
        except TimeoutError:
            record.status = "failed"
            record.error = "Agent wall-time budget exceeded"
            await record.journal.publish("run.failed", {"message": record.error})
        except Exception as exc:
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            await record.journal.publish("run.failed", {"message": record.error})
        finally:
            record.finished_at = datetime.now(UTC).isoformat()
            reset_approval_requester(approval_token)
            reset_event_sink(event_token)
            await record.journal.close()

    async def get_run(self, run_id: str, user_name: str | None = None) -> RunRecord | None:
        record = self.runs.get(run_id)
        if record is not None and user_name is not None and record.user_name != user_name:
            return None
        return record

    async def cancel_run(self, run_id: str, user_name: str) -> bool:
        record = await self.get_run(run_id, user_name)
        if record is None or record.task is None or record.status in TERMINAL_STATUSES:
            return False
        record.task.cancel()
        return True

    async def decide(self, run_id: str, user_name: str, request_id: str, decision: str) -> bool:
        record = await self.get_run(run_id, user_name)
        if record is None or request_id not in record.approval_waiters:
            return False
        record.decisions[request_id] = decision
        waiter = record.approval_waiters.get(request_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(decision == "approved")
        if record.journal is not None:
            await record.journal.publish(
                "approval.decided", {"requestId": request_id, "decision": decision}
            )
        return True

    async def session_context(
        self, session_id: str, user_name: str, workspace: str = "."
    ) -> dict[str, Any]:
        session = await self.get_session(session_id, user_name, workspace)
        state = session.last_state or {}
        return {
            "sessionId": session_id,
            "workspace": str(session.workspace),
            "status": state.get("status", "idle"),
            "summary": state.get("active_summary", {}),
            "manifest": state.get("context_manifest", {}),
            "filesChanged": state.get("files_changed", []),
        }

    async def close_session(self, session_id: str, user_name: str = "local") -> bool:
        runtime_id = _runtime_session_id(user_name, session_id)
        async with self._sessions_lock:
            session = self.sessions.pop(runtime_id, None)
        if session is None:
            return False
        await session.close()
        return True

    async def close(self) -> None:
        running = [
            record.task
            for record in self.runs.values()
            if record.task and not record.task.done()
        ]
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        sessions = list(self.sessions.values())
        self.sessions.clear()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
        await self._stack.aclose()
        self._checkpointer = None
