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
MINIQMT_SECTORS_TOOL = {
    "type": "function",
    "function": {
        "name": "miniqmt_sectors",
        "description": (
            "查询 MiniQMT 板块：不传 sector_name 返回板块名列表（板块总数上千，必须用 name_filter 关键字过滤，"
            "返回带 total、matched 和 truncated 说明截断情况），传板块名返回成分股代码。用于确定候选股票池。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sector_name": {"type": "string", "maxLength": 30},
                "name_filter": {"type": "string", "maxLength": 30},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}
MINIQMT_SCREEN_TOOL = {
    "type": "function",
    "function": {
        "name": "miniqmt_screen",
        "description": (
            "对一个板块或指定代码列表批量取实时行情，排序后只返回前 limit 条紧凑行。板块可以到全市场规模"
            "（沪深A股 5000 余只），自动分批取行情；no_tick_count 和 unquotable_count 表示无行情或停牌被排除的数量。"
            "sort_by 里 close_position_desc 按收盘价在当日振幅中的位置排序，用来区分收在最高价的强势票和冲高回落；"
            "涨幅榜只能告诉你今天谁已经涨完了，挑趋势跟随候选必须配合 enrich_trend。"
            "enrich_trend=true 时额外读日线补确定性趋势字段（limit 最多 20）：ma5/ma10/ma20、ma_stack、ma20_gap_pct、"
            "vol_ratio（当日量 / 前 5 日均量）、pivot（20 日最高，突破参考）、high_20d_gap_pct、swing_low_10d、"
            "stop_ref（止损参考）、breakout_entry（可直接用于 account_monitor price_range 的下界和追高上限）"
            "以及 trend_gate（breakout / pullback / holding / extended / broken / insufficient_data）。"
            "这些字段是工具算出的事实，只能引用不得重判。"
            "每行的 lot_cost 是一手（100 股）成本，buyable 表示宿主账户能否买入，买不了的行带 unbuyable 原因；"
            "顶层 buy_limits 给出单笔买入金额上限、可买最高股价和被禁板块。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sector_name": {"type": "string", "maxLength": 30},
                "stock_codes": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^[036][0-9]{5}\\.(SH|SZ)$"},
                    "minItems": 1,
                    "maxItems": 300,
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["change_pct_desc", "change_pct_asc", "amount_desc", "close_position_desc"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "enrich_trend": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}
MINIQMT_SECTOR_RANK_TOOL = {
    "type": "function",
    "function": {
        "name": "miniqmt_sector_rank",
        "description": (
            "按板块聚合当日全市场行情，返回板块热度榜。发现候选的入口就是这里，不是个股涨幅榜——"
            "涨幅榜前排要么涨停封死买不进，要么一手成本就超过单笔上限。"
            "family：TGN 是概念题材（短线资金炒的就是概念），THY 是行业，SW1/SW2 是申万一级/二级行业，"
            "用 TGN 定当日主线、再用 SW2 交叉验证这个主线背后有没有行业级资金。"
            "每个板块返回成分股数、上涨家数与占比、中位涨幅、总成交额，以及这个账户真正关心的三个字段："
            "buyable_count（一手成本在单笔上限内且非科创板的家数）、buyable_median_change_pct（只统计可买票的中位涨幅，"
            "榜单按它排序）和 top_buyable（板块内可买且最强的三只，作为下钻起点）。"
            "只有龙头在涨、可买小票不动的板块 buyable_median_change_pct 会很低，对这个账户没有意义。"
            "min_buyable 过滤掉可买家数不足的板块。板块成分股按交易日缓存，当天第一次调用较慢。"
            "拿到热板块后用 miniqmt_screen 传 sector_name 加 enrich_trend 复查个股结构，只买 trend_gate=breakout 的。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "family": {"type": "string", "enum": ["TGN", "THY", "SW1", "SW2"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                "min_buyable": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}
MINIQMT_HISTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "miniqmt_history",
        "description": (
            "查询最多 20 只 A 股的历史 K 线（自动先补下载再读本地），返回按日期排序的 open/high/low/close/volume/amount。"
            "用于动量、相对强弱和量能对比；无数据的代码会列在 empty_codes 里，不会伪装成 0。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stock_codes": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^[036][0-9]{5}\\.(SH|SZ)$"},
                    "minItems": 1,
                    "maxItems": 20,
                },
                "period": {"type": "string", "enum": ["1d", "5m", "1m"]},
                "start_time": {"type": "string", "pattern": "^[0-9]{8}$"},
                "end_time": {"type": "string", "pattern": "^[0-9]{8}$"},
            },
            "required": ["stock_codes", "start_time", "end_time"],
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
ACCOUNT_MONITOR_TOOL = {
    "type": "function",
    "function": {
        "name": "account_monitor",
        "description": (
            "读取或全量替换宿主持久化的股票行情监控计划。只保存显式触发条件，不会自行下单；清空必须显式提交空 plans 数组。"
            "SELL 用 price_lte（破位止损）、price_gte（止盈）或 immediate（时间止损到期，无条件在第一次轮询触发，不接受 value）。"
            "BUY 只能用 price_range：value 是区间下界，upper 是区间上界，只有价格落在 [value, upper] 内才触发，"
            "order 只给 volume——限价由交易工具在提交那一刻按最新价推导，upper 就是追高上限，写 order.price 会被拒。"
            "突破腿把区间挂在现价上方（下界取 pivot，上界取追高天花板，"
            "可直接用 miniqmt_screen 的 breakout_entry），回踩腿把区间挂在现价下方（下界是不能破的结构位）。"
            "单点买入触发已被禁止：它的真实语义是越跌越买，跳空砸穿也会成交。"
            "换仓请在 BUY 计划里写 rotate_from=卖出股票代码，同批必须存在该股票的 SELL 计划，否则整批被拒。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["read", "replace"]},
                "plans": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "plan_id": {"type": "string"},
                            "stock_code": {"type": "string", "pattern": "^[036][0-9]{5}\\.(SH|SZ)$"},
                            "side": {"type": "string", "enum": ["BUY", "SELL"]},
                            "trigger": {
                                "type": "object",
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "enum": ["price_lte", "price_gte", "immediate", "price_range"],
                                    },
                                    "value": {"type": "number"},
                                    "upper": {"type": "number"},
                                    "baseline": {"type": "number"},
                                },
                                "required": ["type"],
                                "additionalProperties": False,
                            },
                            "order": {"type": "object", "additionalProperties": True},
                            "rotate_from": {"type": "string", "pattern": "^[036][0-9]{5}\\.(SH|SZ)$"},
                            "note": {"type": "string"},
                        },
                        "required": ["plan_id", "stock_code", "side", "trigger"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["operation"],
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
    MINIQMT_SECTORS_TOOL,
    MINIQMT_SCREEN_TOOL,
    MINIQMT_SECTOR_RANK_TOOL,
    MINIQMT_HISTORY_TOOL,
    MINIQMT_ACCOUNT_TOOL,
    MINIQMT_TRADE_TOOL,
    ACCOUNT_JOURNAL_TOOL,
    ACCOUNT_MONITOR_TOOL,
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
        elif tool_name == "miniqmt_screen":
            error_msg += _validate_miniqmt_screen_args(args)
        elif tool_name == "miniqmt_history":
            error_msg += _validate_miniqmt_history_args(args)
        elif tool_name == "miniqmt_account":
            error_msg += _validate_miniqmt_account_args(args)
        elif tool_name == "miniqmt_trade":
            error_msg += _validate_miniqmt_trade_args(args)
        elif tool_name == "account_journal":
            error_msg += _validate_account_journal_args(args)
        elif tool_name == "account_monitor":
            error_msg += _validate_account_monitor_args(args)
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
            elif tool_name == "miniqmt_sectors":
                allowed = {"sector_name", "name_filter", "limit"}
            elif tool_name == "miniqmt_screen":
                allowed = {"sector_name", "stock_codes", "sort_by", "limit", "enrich_trend"}
            elif tool_name == "miniqmt_sector_rank":
                allowed = {"family", "limit", "min_buyable"}
            elif tool_name == "miniqmt_history":
                allowed = {"stock_codes", "period", "start_time", "end_time"}
            elif tool_name == "miniqmt_account":
                allowed = {"view"}
            elif tool_name == "miniqmt_trade":
                allowed = {"operation", "inputs"}
            elif tool_name == "account_journal":
                allowed = {"operation", "record"}
            elif tool_name == "account_monitor":
                allowed = {"operation", "plans"}
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
        if tool_name == "account_journal":
            action["operation"] = args["operation"]
            if "record" in args:
                action["record"] = args["record"]
            actions.append(action)
            continue
        if tool_name == "account_monitor":
            action["operation"] = args["operation"]
            if "plans" in args:
                action["plans"] = args["plans"]
            actions.append(action)
            continue
        if tool_name == "agent_call":
            action["role"] = args["role"]
            action["task"] = args["task"]
            actions.append(action)
            continue
        if tool_name in {"miniqmt_sectors", "miniqmt_screen", "miniqmt_sector_rank", "miniqmt_history"}:
            # 这几个行情发现工具没有必填的 command，参数按 schema 原样透传给宿主。
            for key in (
                "sector_name",
                "name_filter",
                "stock_codes",
                "sort_by",
                "limit",
                "enrich_trend",
                "family",
                "min_buyable",
                "period",
                "start_time",
                "end_time",
            ):
                if key in args:
                    action[key] = args[key]
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


def _validate_miniqmt_screen_args(args: dict) -> str:
    sector_name = args.get("sector_name")
    stock_codes = args.get("stock_codes")
    if bool(isinstance(sector_name, str) and sector_name.strip()) == bool(isinstance(stock_codes, list) and stock_codes):
        return "miniqmt_screen 必须且只能提供 sector_name 或 stock_codes 之一。"
    if isinstance(stock_codes, list) and not 1 <= len(stock_codes) <= 300:
        return "miniqmt_screen 的 stock_codes 最多 300 项。"
    if "sort_by" in args and args["sort_by"] not in {
        "change_pct_desc",
        "change_pct_asc",
        "amount_desc",
        "close_position_desc",
    }:
        return "miniqmt_screen 的 sort_by 不受支持。"
    limit = args.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        return "miniqmt_screen 的 limit 必须是 1 到 50 的整数。"
    if args.get("enrich_trend") and limit > 20:
        return "miniqmt_screen 带 enrich_trend 时 limit 最多 20（趋势字段需要逐只读日线）。"
    return ""


def _validate_miniqmt_history_args(args: dict) -> str:
    stock_codes = args.get("stock_codes")
    if not isinstance(stock_codes, list) or not 1 <= len(stock_codes) <= 20:
        return "miniqmt_history 的 stock_codes 必须包含 1 到 20 项。"
    if any(not isinstance(code, str) or not code.strip() for code in stock_codes):
        return "miniqmt_history 的股票代码必须是非空字符串。"
    for name in ("start_time", "end_time"):
        value = args.get(name)
        if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
            return f"miniqmt_history 的 {name} 必须是 YYYYMMDD。"
    if args["start_time"] > args["end_time"]:
        return "miniqmt_history 的 start_time 不能晚于 end_time。"
    if "period" in args and args["period"] not in {"1d", "5m", "1m"}:
        return "miniqmt_history 的 period 只能是 1d、5m 或 1m。"
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


def _validate_account_monitor_args(args: dict) -> str:
    operation = args.get("operation")
    if operation not in {"read", "replace"}:
        return "account_monitor 的 operation 必须是 read 或 replace。"
    if operation == "replace" and not isinstance(args.get("plans"), list):
        return "account_monitor replace 必须包含 plans 数组，清空时提交空数组。"
    if operation != "replace" and "plans" in args:
        return f"account_monitor {operation} 不能包含 plans。"
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
