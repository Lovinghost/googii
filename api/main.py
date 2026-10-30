import json
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from agent.config import get_settings
from agent.service import CodeAgentService


class TaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    workspace: str = "."
    user_name: str = "local"


class TaskResponse(BaseModel):
    session_id: str
    response: str | list[Any]
    files_changed: list[str]
    command_results: list[dict[str, Any]]
    status: str = "completed"
    context_manifest: dict[str, Any] = Field(default_factory=dict)
    active_summary: dict[str, Any] = Field(default_factory=dict)


class GatewayRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    prompt: str = Field(min_length=1, max_length=20_000)
    workspace: str = Field(min_length=1, max_length=2048)
    session_id: str | None = Field(default=None, alias="sessionId", max_length=128)
    user_name: str = Field(alias="userName", min_length=1, max_length=128)
    environment: dict[str, str] = Field(default_factory=dict)


class CancelRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    user_name: str = Field(alias="userName", min_length=1, max_length=128)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    request_id: str = Field(alias="requestId", min_length=1, max_length=128)
    decision: Literal["approved", "denied"]
    user_name: str = Field(alias="userName", min_length=1, max_length=128)


class HealthResponse(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = CodeAgentService(get_settings())
    await service.start()
    app.state.code_agent = service
    try:
        yield
    finally:
        await service.close()


app = FastAPI(title="Code Agent", version="0.2.0", lifespan=lifespan)


def service_from(request: Request) -> CodeAgentService:
    return request.app.state.code_agent


def sse_payload(event: dict[str, Any]) -> str:
    return f"event: agent.event\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/tasks", response_model=TaskResponse)
async def run_task(payload: TaskRequest, request: Request) -> TaskResponse:
    """Backward-compatible synchronous MVP endpoint."""
    session_id = payload.session_id or str(uuid4())
    try:
        result = await service_from(request).run(
            session_id, payload.task, payload.user_name, payload.workspace
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent execution failed: {type(exc).__name__}: {exc}",
        ) from exc
    return TaskResponse.model_validate(result)


@app.post("/api/v1/runs/stream")
async def stream_run(payload: GatewayRunRequest, request: Request) -> StreamingResponse:
    try:
        record = await service_from(request).start_run(
            prompt=payload.prompt,
            workspace=payload.workspace,
            user_name=payload.user_name,
            session_id=payload.session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    assert record.journal is not None

    async def generate():
        async for event in record.journal.stream():
            yield sse_payload(event.as_api_dict())

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/runs/{run_id}")
async def get_run(
    run_id: str, request: Request, user_name: str | None = Query(default=None, alias="userName")
) -> dict[str, Any]:
    record = await service_from(request).get_run(run_id, user_name)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return record.as_dict()


@app.get("/api/v1/runs/{run_id}/events")
async def stream_existing_run(
    run_id: str,
    request: Request,
    user_name: str | None = Query(default=None, alias="userName"),
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    record = await service_from(request).get_run(run_id, user_name)
    if record is None or record.journal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    async def generate():
        async for event in record.journal.stream(after):
            yield sse_payload(event.as_api_dict())

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str, payload: CancelRequest, request: Request) -> dict[str, Any]:
    cancelled = await service_from(request).cancel_run(run_id, payload.user_name)
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found or already finished",
        )
    return {"runId": run_id, "status": "cancelling"}


@app.post("/api/v1/runs/{run_id}/decisions")
async def decide(run_id: str, payload: DecisionRequest, request: Request) -> dict[str, Any]:
    accepted = await service_from(request).decide(
        run_id, payload.user_name, payload.request_id, payload.decision
    )
    if not accepted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return {"runId": run_id, "requestId": payload.request_id, "decision": payload.decision}


@app.get("/api/v1/sessions/{session_id}/context")
async def session_context(
    session_id: str,
    request: Request,
    user_name: str = Query(alias="userName"),
    workspace: str = Query(default="."),
) -> dict[str, Any]:
    try:
        return await service_from(request).session_context(session_id, user_name, workspace)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@app.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    session_id: str,
    request: Request,
    user_name: str = Query(default="local", alias="userName"),
) -> None:
    closed = await service_from(request).close_session(session_id, user_name)
    if not closed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
