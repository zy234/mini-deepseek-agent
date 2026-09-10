"""Parse actions & format observations with toolcalls"""

import json
import time
from typing import Any

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
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "使用仓库自带的零 Key 多引擎能力搜索网络信息。返回去重后的 URL、标题、摘要、来源、抓取时间和可识别的发布时间；模拟模式会隐藏截止时间之后的结果，时间不明候选只返回脱敏 URL，必须再用 web_fetch 核验。搜索摘要不能作为最终事实。",
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 4,
                    "description": "1 到 4 个非空搜索查询；单个查询也必须放在数组中",
                }
            },
            "required": ["queries"],
            "additionalProperties": False,
        },
    },
}
WEB_FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "打开 web_search 返回的一个 URL，先用 HTTP 提取正文，质量不足时可由宿主降级到浏览器渲染；返回来源、标题、发布时间、正文、抓取引擎和质量诊断。模拟模式会阻断晚于截止时间或发布时间不明的正文。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "完整的 http 或 https 网页地址，通常来自 web_search 的结果",
                }
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}
MINIQMT_ACCOUNT_TOOL = {
    "type": "function",
    "function": {
        "name": "miniqmt_account",
        "description": "查询宿主绑定的个人账户快照、委托或成交。账户标识由宿主注入，返回结果会脱敏。",
        "parameters": {
            "type": "object",
            "properties": {"view": {"type": "string", "enum": ["snapshot", "orders", "trades"]}},
            "required": ["view"],
            "additionalProperties": False,
        },
    },
}
MINIQMT_TRADE_TOOL = {
    "type": "function",
    "function": {
        "name": "miniqmt_trade",
        "description": (
            "向宿主绑定的个人账户提交或撤销委托。observe 阻断，execute 人工审批，auto_execute 通过宿主安全规则后自动执行。"
            "买入必须固定限价、单笔金额不超过宿主上限、买入后保留现金下限，且禁止科创板 688/689；卖出只校验可卖数量和上限。"
            "买入用 price_cap（追高上限）而不是 price：宿主在提交那一刻按最新价推导出既能成交又不超偏离上限的限价，"
            "自己算 price 会在价格漂移后被偏离上限拒掉。最新价已高过 price_cap 时宿主直接拒单，不追高。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["submit", "cancel"]},
                "inputs": {
                    "type": "object",
                    "description": (
                        "submit: client_intent_id/stock_code/side/volume，加 price_cap（买入，追高上限）"
                        "或 price（卖出，固定限价）；两者互斥。cancel: client_intent_id/order_id"
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["operation", "inputs"],
            "additionalProperties": False,
        },
    },
}
ACCOUNT_JOURNAL_TOOL = {
    "type": "function",
    "function": {
        "name": "account_journal",
        "description": "读取账户管理每日记录，或追加本轮结构化观点、决策、操作、后续观察和踩坑。路径和时间由宿主决定。",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["read", "append"]},
                "record": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["BUY", "SELL", "CANCEL", "HOLD", "REVIEW"]},
                        "market_view": {"type": "string"},
                        "account_risk": {"type": "string"},
                        "decision": {"type": "string"},
                        "follow_up": {"type": "string"},
                        "orders": {"type": "array", "items": {"type": "string"}},
                        "pitfalls": {"type": "array", "items": {"type": "string"}},
                        "tool_errors": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "action",
                        "market_view",
                        "account_risk",
                        "decision",
                        "follow_up",
                        "orders",
                        "pitfalls",
                        "tool_errors",
                    ],
                    "additionalProperties": False,
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}
TOOL_DEFINITIONS = [
    BASH_TOOL,
    EDITOR_TOOL,
    WEB_SEARCH_TOOL,
    WEB_FETCH_TOOL,
    MINIQMT_ACCOUNT_TOOL,
    MINIQMT_TRADE_TOOL,
    ACCOUNT_JOURNAL_TOOL,
]
TOOL_DEFINITIONS_BY_NAME = {tool["function"]["name"]: tool for tool in TOOL_DEFINITIONS}
DEFAULT_TOOL_NAMES = ["bash", "str_replace_editor", "web_search", "web_fetch"]


def get_tool_definitions(names: list[str] | None) -> list[dict]:
    """按角色配置缩小工具集合；未配置时只保留通用基础工具。"""
    if names is None:
        names = DEFAULT_TOOL_NAMES
    unknown = set(names) - set(TOOL_DEFINITIONS_BY_NAME)
    if unknown:
        raise ValueError(f"未知工具：{', '.join(sorted(unknown))}")
    return [TOOL_DEFINITIONS_BY_NAME[name] for name in names]


def parse_toolcall_actions(
    tool_calls: list,
    *,
    format_error_template: str,
    template_kwargs: dict | None = None,
    allowed_tools: set[str] | None = None,
) -> list[dict]:
    """Parse tool calls from the response. Raises FormatError if unknown tool or invalid args.

    ``template_kwargs`` are extra variables exposed to ``format_error_template`` (e.g.
    ``{"finish_reason": ...}`` so a template can distinguish a real format mistake from a
    ``max_tokens`` truncation).
    """
    template_kwargs = template_kwargs or {}
    if not tool_calls:
        raise _format_error(format_error_template, "响应中没有可执行的工具调用。", False, template_kwargs)
    actions = []
    for tool_call in tool_calls:
        name = tool_call.function.name
        spec = TOOL_SPECS.get(name)
        error = ""
        args: Any = {}
        try:
            args = json.loads(tool_call.function.arguments)
        except ValueError as exc:
            error = f"无法解析工具参数：{exc}。"
        if spec is None:
            error += f"未知工具：{name}。"
        elif allowed_tools is not None and name not in allowed_tools:
            error += f"当前 Agent 不允许使用工具：{name}。"
        if not isinstance(args, dict):
            error += f"{name} 工具参数必须是对象。"
        elif spec is not None:
            unknown_keys = set(args) - spec["keys"]
            if unknown_keys:
                error += f"{name} 工具包含未知参数：{', '.join(sorted(unknown_keys))}。"
            error += spec["validate"](args)
        if error:
            raise _format_error(format_error_template, error.strip(), True, template_kwargs)
        # 参数按每个工具声明的键原样透传：宿主自己会再校验一次业务约束，
        # 这里逐工具重写一遍 action 只会让两处约束慢慢漂移。
        actions.append(
            {"tool": name, "tool_call_id": tool_call.id, **{key: args[key] for key in spec["keys"] & set(args)}}
        )
    return actions


def _format_error(template: str, error: str, has_tool_calls: bool, template_kwargs: dict) -> FormatError:
    return FormatError(
        {
            "role": "user",
            "content": Template(template, undefined=StrictUndefined).render(
                error=error, actions=[], has_tool_calls=has_tool_calls, **template_kwargs
            ),
            "extra": {"interrupt_type": "FormatError"},
        }
    )


def _validate_bash_args(args: dict) -> str:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return "bash 工具的 command 必须是非空字符串。"
    if "workdir" in args and not isinstance(args["workdir"], str):
        return "bash 工具的 workdir 必须是字符串。"
    if "description" in args and not isinstance(args["description"], str):
        return "bash 工具的 description 必须是字符串。"
    timeout = args.get("timeout")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
        return "bash 工具的 timeout 必须是正数。"
    return ""


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


def _validate_web_search_args(args: dict) -> str:
    queries = args.get("queries")
    if not isinstance(queries, list) or not 1 <= len(queries) <= 4:
        return "web_search 的 queries 必须是包含 1 到 4 项的数组。"
    if any(not isinstance(query, str) or not query.strip() for query in queries):
        return "web_search 的 queries 每一项都必须是非空字符串。"
    return ""


def _validate_web_fetch_args(args: dict) -> str:
    if not isinstance(args.get("url"), str) or not args["url"].strip():
        return "web_fetch 的 url 必须是非空字符串。"
    return ""


def _validate_miniqmt_account_args(args: dict) -> str:
    if args.get("view") not in {"snapshot", "orders", "trades"}:
        return "miniqmt_account 的 view 不受支持。"
    return ""


def _validate_miniqmt_trade_args(args: dict) -> str:
    if args.get("operation") not in {"submit", "cancel"}:
        return "miniqmt_trade 的 operation 不受支持。"
    if not isinstance(args.get("inputs"), dict):
        return "miniqmt_trade 的 inputs 必须是对象。"
    return ""


def _validate_account_journal_args(args: dict) -> str:
    operation = args.get("operation")
    if operation not in {"read", "append"}:
        return "account_journal 的 operation 必须是 read 或 append。"
    if operation == "read" and "record" in args:
        return "account_journal read 不能包含 record。"
    if operation == "append" and not isinstance(args.get("record"), dict):
        return "account_journal append 必须包含 record 对象。"
    return ""


# 每个工具一行：模型能传哪些键、怎么校验。新增工具只在这里加一行，不用再改解析流程。
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "bash": {"keys": {"command", "workdir", "timeout", "description"}, "validate": _validate_bash_args},
    "str_replace_editor": {
        "keys": {"command", "path", "file_text", "old_str", "new_str", "insert_line", "view_range", "expected_hash"},
        "validate": _validate_editor_args,
    },
    "web_search": {"keys": {"queries"}, "validate": _validate_web_search_args},
    "web_fetch": {"keys": {"url"}, "validate": _validate_web_fetch_args},
    "miniqmt_account": {"keys": {"view"}, "validate": _validate_miniqmt_account_args},
    "miniqmt_trade": {"keys": {"operation", "inputs"}, "validate": _validate_miniqmt_trade_args},
    "account_journal": {"keys": {"operation", "record"}, "validate": _validate_account_journal_args},
}
if set(TOOL_SPECS) != set(TOOL_DEFINITIONS_BY_NAME):
    # 模型看到的工具和宿主能解析的工具必须完全一致，缺一边都是启动即错的配置故障。
    raise RuntimeError(f"工具定义与解析规则不一致：{set(TOOL_DEFINITIONS_BY_NAME) ^ set(TOOL_SPECS)}")


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
        "started_at": None,
        "ended_at": None,
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
                # 观测消息自带工具名和起止时间：否则回溯执行路径必须拿 tool_call_id 反查上一条 assistant。
                "tool": action.get("tool", ""),
                "started_at": output.get("started_at"),
                "ended_at": output.get("ended_at"),
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
