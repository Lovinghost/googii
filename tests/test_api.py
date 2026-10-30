from fastapi.testclient import TestClient

from api.main import app


def test_health_endpoint_starts_with_sqlite_checkpointer(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "checkpoints.sqlite"))
    from agent.config import get_settings

    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert (tmp_path / "checkpoints.sqlite").exists()
    finally:
        get_settings.cache_clear()


def test_stream_endpoint_rejects_workspace_escape(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "checkpoints-escape.sqlite"))
    from agent.config import get_settings

    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/runs/stream",
                json={
                    "prompt": "inspect project",
                    "workspace": "../outside",
                    "sessionId": "session-1",
                    "userName": "alice",
                },
            )
        assert response.status_code == 400
        assert "Workspace must be inside configured root" in response.json()["detail"]
    finally:
        get_settings.cache_clear()
