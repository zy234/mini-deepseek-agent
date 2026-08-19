"""Workspace-bounded text editor used by the host-owned editor tool."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class EditorError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def execute_editor(action: dict, workspace: str) -> dict:
    operation = action["command"]
    path = _resolve_workspace_path(action["path"], workspace)
    if operation == "view":
        return _view(path, action.get("view_range"))
    if operation == "create":
        if path.exists():
            raise EditorError("already_exists", f"文件已存在，拒绝覆盖：{path}")
        content = action["file_text"]
        _write_atomic(path, content, expected_hash=None)
        return _result(path, "created", content)
    if operation == "str_replace":
        content = _read_text(path)
        expected = action.get("expected_hash")
        _check_hash(path, content, expected)
        old = action["old_str"]
        count = content.count(old)
        if count == 0:
            raise EditorError("not_found", "old_str 在文件中没有匹配项")
        if count != 1:
            raise EditorError("ambiguous_edit", f"old_str 匹配了 {count} 处，拒绝批量替换")
        new_content = content.replace(old, action.get("new_str", ""), 1)
        _write_atomic(path, new_content, expected_hash=expected)
        return _result(path, "replaced", new_content)
    if operation == "insert":
        content = _read_text(path)
        expected = action.get("expected_hash")
        _check_hash(path, content, expected)
        lines = content.splitlines(keepends=True)
        line = action["insert_line"]
        if line < 0 or line > len(lines):
            raise EditorError("invalid_line", f"insert_line 超出范围：{line}")
        insertion = action["new_str"]
        if insertion and not insertion.endswith("\n"):
            insertion += "\n"
        new_content = "".join(lines[:line]) + insertion + "".join(lines[line:])
        _write_atomic(path, new_content, expected_hash=expected)
        return _result(path, "inserted", new_content)
    raise EditorError("invalid_command", f"未知编辑操作：{operation}")


def _resolve_workspace_path(raw_path: str, workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise EditorError("outside_workspace", f"路径不在工作区内：{raw_path}") from error
    return resolved


def _read_text(path: Path) -> str:
    if not path.exists():
        raise EditorError("not_found", f"文件不存在：{path}")
    if path.is_dir():
        raise EditorError("not_text", f"路径是目录，不是文本文件：{path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise EditorError("not_text", f"文件不是 UTF-8 文本：{path}") from error


def _view(path: Path, view_range: list[int] | None) -> dict:
    if path.is_dir():
        entries = sorted(item.name + ("/" if item.is_dir() else "") for item in path.iterdir())
        text = "\n".join(entries) + ("\n" if entries else "")
        return {
            "stdout": text,
            "stderr": "",
            "status": "success",
            "path": str(path),
            "operation": "viewed",
            "content_hash": None,
            "returncode": 0,
            "exit_code": 0,
            "timed_out": False,
            "signal": None,
            "termination": None,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout_spill_path": None,
            "stderr_spill_path": None,
        }
    content = _read_text(path)
    content_hash = _hash(content)
    lines = content.splitlines(keepends=True)
    if view_range is not None:
        start, end = view_range
        if start < 1 or end == 0 or (end != -1 and end < start):
            raise EditorError("invalid_range", "view_range 行号无效")
        stop = len(lines) if end == -1 else min(end, len(lines))
        content = "".join(f"{index:>6}\t{lines[index - 1]}" for index in range(start, stop + 1))
    result = _result(path, "viewed", content)
    result["content_hash"] = content_hash
    return result


def _result(path: Path, operation: str, content: str) -> dict:
    return {
        "stdout": content,
        "stderr": "",
        "status": "success",
        "path": str(path),
        "operation": operation,
        "content_hash": _hash(content),
        "returncode": 0,
        "exit_code": 0,
        "timed_out": False,
        "signal": None,
        "termination": None,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "stdout_spill_path": None,
        "stderr_spill_path": None,
    }


def _check_hash(path: Path, content: str, expected: str | None) -> None:
    if expected is not None and expected != _hash(content):
        raise EditorError("stale_file", f"文件在读取后已被修改：{path}")


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _write_atomic(path: Path, content: str, expected_hash: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and expected_hash is not None:
        _check_hash(path, _read_text(path), expected_hash)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() and expected_hash is not None:
            _check_hash(path, _read_text(path), expected_hash)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
