"""Parse actions & format observations with toolcalls"""

import json
import time

from jinja2 import StrictUndefined, Template

from minisweagent.exceptions import FormatError

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "在当前工作目录执行一条 Bash 命令。仅用于必要的检查、编辑和验证；禁止提权、删除根目录或工作区、磁盘写入、远程脚本管道和泄露凭据。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 Bash 命令；应保持短小、可审查，并避免访问敏感信息或执行破坏性操作",
                },
                "workdir": {
                    "type": "string",
                    "description": "可选的工作目录；必须是本地已有目录",
                },
                "timeout": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "可选的单次命令超时秒数，不能超过环境全局限制",
                },
                "description": {
                    "type": "string",
                    "description": "可选的简短命令意图，便于审批和记录",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

EDITOR_TOOL = {
    "type": "function",
    "function": {
        "name": "str_replace_editor",
        "description": "在当前工作区查看和修改 UTF-8 文本文件；修改操作会请求用户批准。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert"]},
                "path": {"type": "string", "description": "工作区内的文件或目录路径"},
                "file_text": {"type": "string", "description": "create 使用的完整文件内容"},
                "old_str": {"type": "string", "description": "str_replace 要替换的原文"},
                "new_str": {"type": "string", "description": "替换后的文本；str_replace 可为空，insert 必填"},
                "insert_line": {"type": "integer", "description": "insert 插入到该行之后，行号从 1 开始；0 表示文件开头"},
                "view_range": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
                "expected_hash": {"type": "string", "description": "可选的 view 返回 hash，用于检测文件被外部修改"},
            },
            "required": ["command", "path"],
            "additionalProperties": False,
        },
    },
}
TOOL_DEFINITIONS = [BASH_TOOL, EDITOR_TOOL]

def parse_toolcall_actions(
    tool_calls: list, *, format_error_template: str, template_kwargs: dict | None = None
) -> list[dict]:
    """Parse tool calls from the response. Raises FormatError if unknown tool or invalid args.

    ``template_kwargs`` are extra variables exposed to ``format_error_template`` (e.g.
    ``{"finish_reason": ...}`` so a template can distinguish a real format mistake from a
    ``max_tokens`` truncation).
    """
    template_kwargs = template_kwargs or {}
    if not tool_calls:
        raise FormatError(
            {
                "role": "user",
                "content": Template(format_error_template, undefined=StrictUndefined).render(
                    error="响应中没有可执行的工具调用。",
                    actions=[],
                    has_tool_calls=False,
                    **template_kwargs,
                ),
                "extra": {"interrupt_type": "FormatError"},
            }
        )
    actions = []
    for tool_call in tool_calls:
        error_msg = ""
        args = {}
        try:
            args = json.loads(tool_call.function.arguments)
        except Exception as e:
            error_msg = f"无法解析工具参数：{e}。"
        tool_name = tool_call.function.name
        if tool_name not in {"bash", "str_replace_editor"}:
            error_msg += f"未知工具：{tool_name}。"
        if not isinstance(args, dict):
            error_msg += f"{tool_name} 工具参数必须是对象。"
        elif tool_name == "bash":
            if "command" not in args or not isinstance(args["command"], str) or not args["command"].strip():
                error_msg += "bash 工具的 command 必须是非空字符串。"
        elif tool_name == "str_replace_editor":
            error_msg += _validate_editor_args(args)
        if isinstance(args, dict):
            allowed = {"command", "workdir", "timeout", "description"} if tool_name == "bash" else {
                "command", "path", "file_text", "old_str", "new_str", "insert_line", "view_range", "expected_hash"
            }
            unknown_keys = set(args) - allowed
            if unknown_keys:
                error_msg += f"{tool_name} 工具包含未知参数：{', '.join(sorted(unknown_keys))}。"
            if tool_name == "bash" and "workdir" in args and not isinstance(args["workdir"], str):
                error_msg += "bash 工具的 workdir 必须是字符串。"
            timeout = args.get("timeout") if tool_name == "bash" else None
            if timeout is not None and (
                isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
            ):
                error_msg += "bash 工具的 timeout 必须是正数。"
            if tool_name == "bash" and "description" in args and not isinstance(args["description"], str):
                error_msg += "bash 工具的 description 必须是字符串。"
        if error_msg:
            raise FormatError(
                {
                    "role": "user",
                    "content": Template(format_error_template, undefined=StrictUndefined).render(
                        actions=[], error=error_msg.strip(), has_tool_calls=True, **template_kwargs
                    ),
                    "extra": {"interrupt_type": "FormatError"},
                }
            )
        action = {"tool": tool_name, "command": args["command"], "tool_call_id": tool_call.id}
        keys = ("workdir", "timeout", "description") if tool_name == "bash" else (
            "path", "file_text", "old_str", "new_str", "insert_line", "view_range", "expected_hash"
        )
        for key in keys:
            if key in args:
                action[key] = args[key]
        actions.append(action)
    return actions


def _validate_editor_args(args: dict) -> str:
    operation = args.get("command")
    if operation not in {"view", "create", "str_replace", "insert"}:
        return "编辑器 command 必须是 view、create、str_replace 或 insert。"
    if not isinstance(args.get("path"), str) or not args["path"].strip():
        return "编辑器 path 必须是非空字符串。"
    if operation == "create" and not isinstance(args.get("file_text"), str):
        return "编辑器 create 必须提供 file_text。"
    if operation == "str_replace" and not isinstance(args.get("old_str"), str):
        return "编辑器 str_replace 必须提供 old_str。"
    if operation == "insert":
        if not isinstance(args.get("insert_line"), int) or isinstance(args.get("insert_line"), bool):
            return "编辑器 insert 必须提供整数 insert_line。"
        if not isinstance(args.get("new_str"), str):
            return "编辑器 insert 必须提供 new_str。"
    view_range = args.get("view_range")
    if view_range is not None and (
        not isinstance(view_range, list)
        or len(view_range) != 2
        or any(not isinstance(value, int) or isinstance(value, bool) for value in view_range)
    ):
        return "编辑器 view_range 必须是两个整数。"
    if "expected_hash" in args and not isinstance(args["expected_hash"], str):
        return "编辑器 expected_hash 必须是字符串。"
    return ""


def format_toolcall_observation_messages(
    *,
    actions: list[dict],
    outputs: list[dict],
    observation_template: str,
    template_vars: dict | None = None,
) -> list[dict]:
    """Format execution outputs into tool result messages."""
    not_executed = {
        "stdout": "",
        "stderr": "",
        "returncode": -1,
        "exit_code": None,
        "status": "not_executed",
        "timed_out": False,
        "signal": None,
        "termination": None,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "stdout_spill_path": None,
        "stderr_spill_path": None,
        "path": None,
        "operation": None,
        "content_hash": None,
        "exception_info": "操作未执行",
    }
    padded_outputs = outputs + [not_executed] * (len(actions) - len(outputs))
    results = []
    for action, output in zip(actions, padded_outputs, strict=True):
        content = Template(observation_template, undefined=StrictUndefined).render(
            output=output, **(template_vars or {})
        )
        msg = {
            "content": content,
            "extra": {
                "stdout": output.get("stdout", ""),
                "stderr": output.get("stderr", ""),
                "returncode": output.get("returncode"),
                "exit_code": output.get("exit_code"),
                "status": output.get("status"),
                "timed_out": output.get("timed_out", False),
                "signal": output.get("signal"),
                "termination": output.get("termination"),
                "stdout_truncated": output.get("stdout_truncated", False),
                "stderr_truncated": output.get("stderr_truncated", False),
                "stdout_spill_path": output.get("stdout_spill_path"),
                "stderr_spill_path": output.get("stderr_spill_path"),
                "path": output.get("path"),
                "operation": output.get("operation"),
                "content_hash": output.get("content_hash"),
                "error_code": output.get("extra", {}).get("error_code"),
                "timestamp": time.time(),
                "exception_info": output.get("exception_info"),
                **output.get("extra", {}),
            },
        }
        if "tool_call_id" in action:
            msg["tool_call_id"] = action["tool_call_id"]
            msg["role"] = "tool"
        else:
            msg["role"] = "user"  # human issued commands
        results.append(msg)
    return results
