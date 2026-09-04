"""账户管理 Agent 的每日追加式账本。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAX_JOURNAL_READ_CHARS = 40_000
MAX_FIELD_CHARS = 6_000
ALLOWED_ACTIONS = {"BUY", "SELL", "CANCEL", "HOLD", "REVIEW"}
TRADING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
RECORD_FIELDS = {
    "action",
    "market_view",
    "account_risk",
    "decision",
    "follow_up",
    "orders",
    "pitfalls",
    "tool_errors",
}


def read_account_journal(directory: str | Path) -> dict[str, Any]:
    """读取账本快照；待观测清单保留给宿主展示，不属于 Agent 指令。"""
    journal_dir = Path(directory).expanduser().resolve() / "journals"
    todo_path = journal_dir.parent / "observation-todo.md"
    today = datetime.now(TRADING_TZ).date().isoformat()
    today_path = journal_dir / f"{today}.md"
    previous_paths = sorted(path for path in journal_dir.glob("*.md") if path.name < today_path.name)
    previous_path = previous_paths[-1] if previous_paths else None
    return _success(
        "journal_read",
        {
            "date": today,
            "today": _read_tail(today_path, MAX_JOURNAL_READ_CHARS),
            "previous": _read_tail(previous_path, 12_000) if previous_path else "",
            "observation_todo": _read_tail(todo_path, 12_000),
        },
    )


def read_observation_todo(directory: str | Path) -> str:
    """读取给用户查看的待观测清单，不作为 Agent 工具结果返回。"""
    journal_dir = Path(directory).expanduser().resolve() / "journals"
    return _read_tail(journal_dir.parent / "observation-todo.md", 12_000)


def append_account_cycle(
    directory: str | Path,
    cycle_id: str,
    record: Any,
) -> dict[str, Any]:
    """校验结构化周期记录并渲染为 Markdown；模型不能指定写入路径。"""
    if not isinstance(record, dict):
        return _error("invalid_argument", "record 必须是对象")
    unknown = set(record) - RECORD_FIELDS
    if unknown:
        return _error("invalid_argument", f"record 包含未知字段：{', '.join(sorted(unknown))}")
    missing = RECORD_FIELDS - set(record)
    if missing:
        return _error("invalid_argument", f"record 缺少字段：{', '.join(sorted(missing))}")
    action = record.get("action")
    if action not in ALLOWED_ACTIONS:
        return _error("invalid_argument", "action 必须是 BUY、SELL、CANCEL、HOLD 或 REVIEW")
    text_fields = ("market_view", "account_risk", "decision", "follow_up")
    if any(not isinstance(record.get(name), str) for name in text_fields):
        return _error("invalid_argument", "market_view、account_risk、decision、follow_up 必须是字符串")
    list_fields = ("orders", "pitfalls", "tool_errors")
    if any(
        not isinstance(record.get(name), list)
        or any(not isinstance(item, str) for item in record[name])
        for name in list_fields
    ):
        return _error("invalid_argument", "orders、pitfalls、tool_errors 必须是字符串数组")

    now = datetime.now(TRADING_TZ)
    path = _journal_path(directory, now)
    marker = f"<!-- cycle:{cycle_id} -->"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if marker in existing:
        return _error("duplicate_cycle", "本轮记录已经写入，禁止重复追加")
    sections = [
        marker,
        f"## {now.strftime('%H:%M:%S')} · {action}",
        "",
        f"- 周期：`{_clean(cycle_id, 200)}`",
        f"- 行情观点：{_clean(record['market_view'])}",
        f"- 账户风险：{_clean(record['account_risk'])}",
        f"- 决策：{_clean(record['decision'])}",
        f"- 后续观察：{_clean(record['follow_up'])}",
        f"- 操作：{_list_text(record['orders'])}",
        f"- 踩坑：{_list_text(record['pitfalls'])}",
        f"- 工具错误：{_list_text(record['tool_errors'])}",
        "",
    ]
    _append(path, "\n".join(sections), now)
    return _success("journal_append", {"date": now.date().isoformat(), "cycle_id": cycle_id})


def append_trade_audit(
    directory: str | Path,
    cycle_id: str,
    operation: str,
    inputs: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """交易工具结果由宿主直接落账，避免依赖模型复述。"""
    now = datetime.now(TRADING_TZ)
    path = _journal_path(directory, now)
    intent_id = str(inputs.get("client_intent_id") or "missing")
    safe_inputs = {key: value for key, value in inputs.items() if key != "account_id"}
    block = [
        f"<!-- trade:{cycle_id}:{intent_id}:{now.strftime('%H%M%S%f')} -->",
        f"### {now.strftime('%H:%M:%S')} · 交易工具审计",
        "",
        f"- 周期：`{_clean(cycle_id, 200)}`",
        f"- 操作：`{_clean(operation, 40)}`",
        f"- 意图：`{_clean(intent_id, 200)}`",
        f"- 请求：`{_clean(json.dumps(safe_inputs, ensure_ascii=False, sort_keys=True))}`",
        f"- 结果：`{_clean(json.dumps(result, ensure_ascii=False, sort_keys=True))}`",
        "",
    ]
    _append(path, "\n".join(block), now)


def has_cycle_record(directory: str | Path, cycle_id: str) -> bool:
    path = _journal_path(directory, datetime.now(TRADING_TZ))
    return path.is_file() and f"<!-- cycle:{cycle_id} -->" in path.read_text(encoding="utf-8")


def append_cycle_fallback(directory: str | Path, cycle_id: str, submission: str, status: str) -> None:
    if has_cycle_record(directory, cycle_id):
        return
    append_account_cycle(
        directory,
        cycle_id,
        {
            "action": "HOLD",
            "market_view": "Agent 未提交结构化行情观点。",
            "account_risk": f"周期结束状态：{status}",
            "decision": submission or "本轮没有有效最终答复。",
            "follow_up": "下一轮先重新查询账户、委托、成交和行情。",
            "orders": [],
            "pitfalls": ["Agent 未调用 account_journal append，记录由宿主补写。"],
            "tool_errors": [] if status == "Submitted" else [status],
        },
    )


def _journal_path(directory: str | Path, now: datetime) -> Path:
    return Path(directory).expanduser().resolve() / "journals" / f"{now.date().isoformat()}.md"


def _append(path: Path, content: str, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# 账户管理记录 · {now.date().isoformat()}\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)


def _read_tail(path: Path | None, limit: int) -> str:
    if path is None or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")[-limit:]


def _clean(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    return str(value).replace("\x00", "").replace("`", "'").strip()[:limit]


def _list_text(values: list[str]) -> str:
    return "；".join(_clean(value, 1_000) for value in values) if values else "无"


def _success(operation: str, data: Any) -> dict[str, Any]:
    return {"ok": True, "status": "success", "operation": operation, "data": data, "error": None}


def _error(code: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "blocked" if code == "duplicate_cycle" else "error",
        "operation": None,
        "data": None,
        "error": {"code": code, "detail": detail},
    }
