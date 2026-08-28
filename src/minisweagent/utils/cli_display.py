"""Small terminal renderer for readable agent traces."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

MAX_PREVIEW_CHARS = 1000
RESET = "\033[0m"
DIM = "\033[2m"
COLORS = {
    "思考": "\033[36m",
    "回复": "\033[32m",
    "工具": "\033[35m",
    "工具结果": "\033[33m",
    "错误": "\033[31m",
}
_recent_full_blocks: list[tuple[str, str]] = []


def _color_enabled(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and not os.getenv("NO_COLOR")


def _paint(stream: TextIO, text: str, color: str = "") -> str:
    if not color or not _color_enabled(stream):
        return text
    return f"{color}{text}{RESET}"


def _category(label: str) -> str:
    if label.startswith("工具结果"):
        return "工具结果"
    if label.startswith("工具"):
        return "工具"
    return label if label in COLORS else "回复"


def _separator(label: str, stream: TextIO) -> None:
    title = f"── {label} "
    line = f"\n{title}{'─' * max(3, 72 - len(title))}"
    stream.write(_paint(stream, line, COLORS.get(_category(label), "")) + "\n")


def render_block(
    label: str,
    text: str,
    *,
    stream: TextIO | None = None,
    max_chars: int = MAX_PREVIEW_CHARS,
) -> bool:
    """Render a bounded block and remember the full text for an explicit later request."""
    stream = stream or sys.stdout
    text = text or ""
    _separator(label, stream)
    truncated = len(text) > max_chars
    stream.write(text[:max_chars] if truncated else text)
    if text and not text.endswith("\n"):
        stream.write("\n")
    if not truncated:
        stream.flush()
        return False
    stream.write(_paint(stream, f"… 已截断，原文 {len(text)} 字符", DIM) + "\n")
    _remember_full(label, text)
    stream.flush()
    return True


def render_status(status: str | None, returncode: int | None, *, stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    label = "工具结果"
    color = COLORS["工具结果"] if status == "success" else COLORS["错误"]
    _separator(label, stream)
    stream.write(_paint(stream, f"status={status or 'unknown'}  returncode={returncode}", color) + "\n")
    stream.flush()


def render_tool_actions(actions: list[dict[str, Any]], *, stream: TextIO | None = None) -> None:
    """Render parsed Bash calls separately instead of printing raw argument JSON."""
    stream = stream or sys.stdout
    for index, action in enumerate(actions, start=1):
        tool_name = action.get("tool", "bash")
        _separator(f"工具调用 {index} · {tool_name}", stream)
        description = action.get("description")
        if description:
            stream.write(_paint(stream, "描述  ", DIM) + f"{description}\n")
        workdir = action.get("workdir")
        if workdir:
            stream.write(_paint(stream, "目录  ", DIM) + f"{workdir}\n")
        timeout = action.get("timeout")
        if timeout is not None:
            stream.write(_paint(stream, "超时  ", DIM) + f"{timeout} 秒\n")
        if tool_name == "str_replace_editor":
            stream.write(_paint(stream, f"操作  {action.get('command', '')}\n", COLORS["工具"]))
            stream.write(_paint(stream, f"路径  {action.get('path', '')}\n", DIM))
            if action.get("expected_hash"):
                stream.write(_paint(stream, f"版本  {action['expected_hash']}\n", DIM))
            if action.get("file_text") is not None:
                _render_preview(str(action["file_text"]), label=f"工具调用 {index} 的文件内容", stream=stream)
            elif action.get("old_str") is not None:
                _render_preview(str(action["old_str"]), label=f"工具调用 {index} 的替换原文", stream=stream)
            continue
        if tool_name == "web_search":
            stream.write(_paint(stream, "查询\n", COLORS["工具"]))
            _render_preview(
                "\n".join(str(query) for query in action.get("queries", [])),
                label=f"工具调用 {index} 的完整查询",
                stream=stream,
            )
            continue
        stream.write(_paint(stream, "命令\n", COLORS["工具"]))
        _render_preview(
            str(action.get("command", "")),
            label=f"工具调用 {index} 的完整命令",
            stream=stream,
        )
    stream.flush()


def _render_preview(text: str, *, label: str, stream: TextIO, max_chars: int = MAX_PREVIEW_CHARS) -> bool:
    truncated = len(text) > max_chars
    stream.write(text[:max_chars] if truncated else text)
    if text and not text.endswith("\n"):
        stream.write("\n")
    if not truncated:
        return False
    stream.write(_paint(stream, f"… 已截断，原文 {len(text)} 字符", DIM) + "\n")
    _remember_full(label, text)
    return True


def _remember_full(label: str, text: str) -> None:
    _recent_full_blocks.append((label, text))


def clear_recent_full_blocks() -> None:
    _recent_full_blocks.clear()


def render_recent_full_blocks(*, stream: TextIO | None = None) -> bool:
    """Render content hidden by the CLI preview during the most recent turn."""
    stream = stream or sys.stdout
    if not _recent_full_blocks:
        stream.write(_paint(stream, "当前轮次没有被截断的内容。", DIM) + "\n")
        stream.flush()
        return False
    for label, text in _recent_full_blocks:
        _separator(f"{label}（完整）", stream)
        stream.write(text)
        if text and not text.endswith("\n"):
            stream.write("\n")
    stream.flush()
    return True


@dataclass
class StreamRenderer:
    """Stream bounded previews while retaining full sections for an explicit later request."""

    stream: TextIO = field(default_factory=lambda: sys.stdout)
    max_chars: int = MAX_PREVIEW_CHARS
    current_label: str | None = None
    sections: dict[str, str] = field(default_factory=dict)
    rendered_chars: dict[str, int] = field(default_factory=dict)
    warned: set[str] = field(default_factory=set)

    def write(self, label: str, text: str) -> None:
        if not text:
            return
        if self.current_label != label:
            if self.current_label is not None:
                self.stream.write("\n")
            _separator(label, self.stream)
            self.current_label = label
        self.sections[label] = self.sections.get(label, "") + text
        rendered = self.rendered_chars.get(label, 0)
        remaining = max(0, self.max_chars - rendered)
        if remaining:
            self.stream.write(text[:remaining])
            self.rendered_chars[label] = rendered + min(len(text), remaining)
        if len(self.sections[label]) > self.max_chars and label not in self.warned:
            self.stream.write("\n" + _paint(self.stream, "… 已截断，任务结束后输入 /open 展开", DIM) + "\n")
            self.warned.add(label)
        self.stream.flush()

    def finish(self) -> None:
        if self.current_label is not None:
            self.stream.write("\n")
        for label, text in self.sections.items():
            if label in self.warned:
                _remember_full(label, text)
        self.stream.flush()
