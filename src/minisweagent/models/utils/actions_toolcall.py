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
        if tool_call.function.name != "bash":
            error_msg += f"未知工具：{tool_call.function.name}。"
        if not isinstance(args, dict) or "command" not in args:
            error_msg += "bash 工具调用缺少 command 参数。"
        elif not isinstance(args["command"], str) or not args["command"].strip():
            error_msg += "bash 工具的 command 必须是非空字符串。"
        if isinstance(args, dict):
            unknown_keys = set(args) - {"command", "workdir", "timeout", "description"}
            if unknown_keys:
                error_msg += f"bash 工具包含未知参数：{', '.join(sorted(unknown_keys))}。"
            if "workdir" in args and not isinstance(args["workdir"], str):
                error_msg += "bash 工具的 workdir 必须是字符串。"
            timeout = args.get("timeout")
            if timeout is not None and (
                isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
            ):
                error_msg += "bash 工具的 timeout 必须是正数。"
            if "description" in args and not isinstance(args["description"], str):
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
        action = {"command": args["command"], "tool_call_id": tool_call.id}
        for key in ("workdir", "timeout", "description"):
            if key in args:
                action[key] = args[key]
        actions.append(action)
    return actions


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
