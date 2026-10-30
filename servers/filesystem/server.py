import os
import re
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from mcp.server.fastmcp import FastMCP

ROOT = Path(os.environ.get("WORKSPACE_ROOT", "workspace")).resolve()
MAX_READ_BYTES = int(os.environ.get("FILESYSTEM_MAX_READ_BYTES", str(2 * 1024 * 1024)))
MAX_LIST_ENTRIES = int(os.environ.get("FILESYSTEM_MAX_LIST_ENTRIES", "1000"))
ROOT.mkdir(parents=True, exist_ok=True)
mcp = FastMCP("filesystem")


def safe_path(raw_path: str, *, must_exist: bool = False) -> Path:
    candidate = Path(raw_path)
    if PurePosixPath(raw_path).is_absolute() or PureWindowsPath(raw_path).is_absolute():
        raise ValueError("Absolute paths are not allowed; paths are relative to /workspace")
    resolved = (ROOT / candidate).resolve(strict=must_exist)
    if resolved != ROOT and ROOT not in resolved.parents:
        raise ValueError("Path escapes /workspace")
    return resolved


def relative(path: Path) -> str:
    value = path.relative_to(ROOT).as_posix()
    return value or "."


@mcp.tool()
def list_files(path: str = ".", recursive: bool = False) -> dict[str, Any]:
    """List files and directories below a workspace-relative directory."""
    directory = safe_path(path, must_exist=True)
    if not directory.is_dir():
        raise ValueError(f"Not a directory: {path}")
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    entries: list[dict[str, Any]] = []
    for child in sorted(iterator, key=lambda item: item.as_posix()):
        try:
            resolved = child.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        if resolved != ROOT and ROOT not in resolved.parents:
            continue
        entries.append(
            {
                "path": relative(child),
                "type": "directory" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else None,
            }
        )
        if len(entries) >= MAX_LIST_ENTRIES:
            return {"entries": entries, "truncated": True}
    return {"entries": entries, "truncated": False}


@mcp.tool()
def read_file(path: str) -> dict[str, Any]:
    """Read a UTF-8 text file from the workspace."""
    file_path = safe_path(path, must_exist=True)
    if not file_path.is_file():
        raise ValueError(f"Not a file: {path}")
    size = file_path.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"File is too large ({size} bytes; limit is {MAX_READ_BYTES})")
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Only UTF-8 text files are supported") from exc
    return {"path": relative(file_path), "content": content, "size": size}


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise ValueError("Target is not a regular file")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@mcp.tool()
def write_file(path: str, content: str) -> dict[str, Any]:
    """Create or replace a UTF-8 text file in the workspace."""
    file_path = safe_path(path)
    atomic_write(file_path, content)
    return {"path": relative(file_path), "bytes_written": len(content.encode("utf-8"))}


HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_unified_diff(original: str, patch: str) -> str:
    source = original.splitlines(keepends=True)
    patch_lines = patch.splitlines(keepends=True)
    output: list[str] = []
    source_index = 0
    index = 0
    saw_hunk = False

    while index < len(patch_lines):
        line = patch_lines[index]
        if line.startswith(("--- ", "+++ ")):
            index += 1
            continue
        match = HUNK_HEADER.match(line.rstrip("\r\n"))
        if not match:
            if line.strip():
                raise ValueError(f"Invalid unified diff line: {line.rstrip()}")
            index += 1
            continue
        saw_hunk = True
        old_start = int(match.group(1))
        target_index = max(old_start - 1, 0)
        if target_index < source_index or target_index > len(source):
            raise ValueError("Patch hunks are out of order or outside the file")
        output.extend(source[source_index:target_index])
        source_index = target_index
        index += 1

        while index < len(patch_lines) and not patch_lines[index].startswith("@@ "):
            hunk_line = patch_lines[index]
            if hunk_line.startswith(("--- ", "+++ ")):
                raise ValueError("Unexpected file header inside a hunk")
            if hunk_line.startswith("\\ No newline at end of file"):
                index += 1
                continue
            if not hunk_line:
                raise ValueError("Malformed hunk")
            marker, value = hunk_line[0], hunk_line[1:]
            if marker == " ":
                if source_index >= len(source) or source[source_index] != value:
                    raise ValueError("Patch context does not match the current file")
                output.append(source[source_index])
                source_index += 1
            elif marker == "-":
                if source_index >= len(source) or source[source_index] != value:
                    raise ValueError("Patch removal does not match the current file")
                source_index += 1
            elif marker == "+":
                output.append(value)
            else:
                raise ValueError(f"Invalid hunk marker: {marker!r}")
            index += 1

    if not saw_hunk:
        raise ValueError("Patch contains no hunks")
    output.extend(source[source_index:])
    return "".join(output)


@mcp.tool()
def apply_patch(path: str, patch: str) -> dict[str, Any]:
    """Apply a standard unified diff to one existing UTF-8 workspace file."""
    file_path = safe_path(path, must_exist=True)
    if not file_path.is_file():
        raise ValueError(f"Not a file: {path}")
    original = file_path.read_text(encoding="utf-8")
    updated = apply_unified_diff(original, patch)
    atomic_write(file_path, updated)
    return {
        "path": relative(file_path),
        "changed": updated != original,
        "bytes_written": len(updated.encode("utf-8")),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
