from pathlib import Path

from sandbox.docker_manager import DockerManager


def test_container_name_is_stable_and_safe(tmp_path: Path) -> None:
    first = DockerManager("My Session/一", tmp_path, "python:3.12-slim", 1, "512m", 128)
    second = DockerManager("My Session/一", tmp_path, "python:3.12-slim", 1, "512m", 128)
    assert first.container_name == second.container_name
    assert first.container_name.startswith("code-agent-my-session")
    assert len(first.container_name) <= 64
