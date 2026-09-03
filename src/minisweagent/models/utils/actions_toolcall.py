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
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "使用仓库自带的零 Key 多引擎能力搜索当前网络信息。返回去重后的来源 URL、标题、抓取时间和摘要；最终答复应引用相关 URL。",
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
        "description": "打开 web_search 返回的一个 URL，提取页面标题、可识别的发布时间和正文文本。仅用于查看具体来源，不要批量抓取。",
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
FINANCIAL_CALC_TOOL = {
    "type": "function",
    "function": {
        "name": "financial_calc",
        "description": "执行无网络、无账户访问的确定性金融计算。缺少必要数据时返回结构化错误，不补默认财务数字。",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["returns", "max_drawdown", "risk_metrics", "dcf", "portfolio_risk"],
                },
                "inputs": {
                    "type": "object",
                    "description": "计算所需的完整输入；字段随 operation 变化",
                    "additionalProperties": True,
                },
            },
            "required": ["operation", "inputs"],
            "additionalProperties": False,
        },
    },
}
MINIQMT_QUOTES_TOOL = {
    "type": "function",
    "function": {
        "name": "miniqmt_quotes",
        "description": "通过宿主绑定的 MiniQMT 查询最多 20 只 A 股的实时行情。不能指定服务地址、账户或凭据。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_codes": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^[036][0-9]{5}\\.(SH|SZ)$"},
                    "minItems": 1,
                    "maxItems": 20,
                }
            },
            "required": ["stock_codes"],
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
        "description": "向宿主绑定的个人账户提交或撤销委托。默认 observe 模式会阻断；execute 模式仍需终端人工审批。",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["submit", "cancel"]},
                "inputs": {
                    "type": "object",
                    "description": "submit: client_intent_id/stock_code/side/volume/price；cancel: client_intent_id/order_id",
                    "additionalProperties": True,
                },
            },
            "required": ["operation", "inputs"],
            "additionalProperties": False,
        },
    },
}
AGENT_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "agent_call",
        "description": "调用一个固定的金融子 Agent 获取研究、账户组合分析或受控交易结果。子 Agent 使用独立上下文，不能继续委派其他 Agent。",
        "parameters": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": ["financial_research", "portfolio_manager", "account_trader"],
                },
                "task": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 12000,
                    "description": "交给子 Agent 的明确任务；不要包含凭据或账户标识",
                },
            },
            "required": ["role", "task"],
            "additionalProperties": False,
        },
    },
}

TOOL_DEFINITIONS = [
    BASH_TOOL,
    EDITOR_TOOL,
    WEB_SEARCH_TOOL,
    WEB_FETCH_TOOL,
    FINANCIAL_CALC_TOOL,
    MINIQMT_QUOTES_TOOL,
    MINIQMT_ACCOUNT_TOOL,
    MINIQMT_TRADE_TOOL,
    AGENT_CALL_TOOL,
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
        if tool_name not in TOOL_DEFINITIONS_BY_NAME:
            error_msg += f"未知工具：{tool_name}。"
        elif allowed_tools is not None and tool_name not in allowed_tools:
            error_msg += f"当前 Agent 不允许使用工具：{tool_name}。"
        if not isinstance(args, dict):
            error_msg += f"{tool_name} 工具参数必须是对象。"
        elif tool_name == "bash":
            if "command" not in args or not isinstance(args["command"], str) or not args["command"].strip():
                error_msg += "bash 工具的 command 必须是非空字符串。"
        elif tool_name == "str_replace_editor":
            error_msg += _validate_editor_args(args)
        elif tool_name == "web_search":
            error_msg += _validate_web_search_args(args)
        elif tool_name == "web_fetch":
            error_msg += _validate_web_fetch_args(args)
        elif tool_name == "financial_calc":
            error_msg += _validate_financial_calc_args(args)
        elif tool_name == "miniqmt_quotes":
            error_msg += _validate_miniqmt_quotes_args(args)
        elif tool_name == "miniqmt_account":
            error_msg += _validate_miniqmt_account_args(args)
        elif tool_name == "miniqmt_trade":
            error_msg += _validate_miniqmt_trade_args(args)
        elif tool_name == "agent_call":
            error_msg += _validate_agent_call_args(args)
        if isinstance(args, dict):
            if tool_name == "bash":
                allowed = {"command", "workdir", "timeout", "description"}
            elif tool_name == "web_search":
                allowed = {"queries"}
            elif tool_name == "web_fetch":
                allowed = {"url"}
            elif tool_name == "financial_calc":
                allowed = {"operation", "inputs"}
            elif tool_name == "miniqmt_quotes":
                allowed = {"stock_codes"}
            elif tool_name == "miniqmt_account":
                allowed = {"view"}
            elif tool_name == "miniqmt_trade":
                allowed = {"operation", "inputs"}
            elif tool_name == "agent_call":
                allowed = {"role", "task"}
            else:
                allowed = {
                    "command",
                    "path",
                    "file_text",
                    "old_str",
                    "new_str",
                    "insert_line",
                    "view_range",
                    "expected_hash",
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
        action = {"tool": tool_name, "tool_call_id": tool_call.id}
        if tool_name == "web_search":
            action["queries"] = args["queries"]
            actions.append(action)
            continue
        if tool_name == "web_fetch":
            action["url"] = args["url"]
            actions.append(action)
            continue
        if tool_name == "financial_calc":
            action["operation"] = args["operation"]
            action["inputs"] = args["inputs"]
            actions.append(action)
            continue
        if tool_name == "miniqmt_quotes":
            action["stock_codes"] = args["stock_codes"]
            actions.append(action)
            continue
        if tool_name == "miniqmt_account":
            action["view"] = args["view"]
            actions.append(action)
            continue
        if tool_name == "miniqmt_trade":
            action["operation"] = args["operation"]
            action["inputs"] = args["inputs"]
            actions.append(action)
            continue
        if tool_name == "agent_call":
            action["role"] = args["role"]
            action["task"] = args["task"]
            actions.append(action)
            continue
        action["command"] = args["command"]
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


def _validate_web_search_args(args: dict) -> str:
    queries = args.get("queries")
    if not isinstance(queries, list) or not 1 <= len(queries) <= 4:
        return "web_search 的 queries 必须是包含 1 到 4 项的数组。"
    if any(not isinstance(query, str) or not query.strip() for query in queries):
        return "web_search 的 queries 每一项都必须是非空字符串。"
    return ""


def _validate_web_fetch_args(args: dict) -> str:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return "web_fetch 的 url 必须是非空字符串。"
    return ""


def _validate_agent_call_args(args: dict) -> str:
    role = args.get("role")
    if role not in {"financial_research", "portfolio_manager", "account_trader"}:
        return "agent_call 的 role 必须是 financial_research、portfolio_manager 或 account_trader。"
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        return "agent_call 的 task 必须是非空字符串。"
    if len(task) > 12000:
        return "agent_call 的 task 不能超过 12000 个字符。"
    return ""


def _validate_financial_calc_args(args: dict) -> str:
    operation = args.get("operation")
    if operation not in {"returns", "max_drawdown", "risk_metrics", "dcf", "portfolio_risk"}:
        return "financial_calc 的 operation 不受支持。"
    if not isinstance(args.get("inputs"), dict):
        return "financial_calc 的 inputs 必须是对象。"
    return ""


def _validate_miniqmt_quotes_args(args: dict) -> str:
    stock_codes = args.get("stock_codes")
    if not isinstance(stock_codes, list) or not 1 <= len(stock_codes) <= 20:
        return "miniqmt_quotes 的 stock_codes 必须包含 1 到 20 项。"
    if any(not isinstance(code, str) or not code.strip() for code in stock_codes):
        return "miniqmt_quotes 的股票代码必须是非空字符串。"
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
