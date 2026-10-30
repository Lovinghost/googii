from agent.loop.policy import requires_approval


def test_dangerous_commands_require_approval() -> None:
    assert requires_approval("execute_command", {"command": "git reset --hard HEAD"})
    assert requires_approval("execute_command", {"command": "rm -rf build"})
    assert not requires_approval("execute_command", {"command": "go test ./..."})
    assert not requires_approval("read_file", {"path": "README.md"})
