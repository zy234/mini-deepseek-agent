"""基于宿主行情的轻量股票监控器。

监控器只负责读取行情和判断显式触发条件，不负责选股、改价或下单。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .miniqmt import MiniQMTClient, _extract_quote

TRADING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
STOCK_CODE_PATTERN = re.compile(r"^(?:[036]\d{5})\.(?:SH|SZ)$")
TRIGGER_TYPES = {"price_lte", "price_gte", "change_pct_lte", "change_pct_gte"}
MAX_PLANS = 20


class MarketMonitor:
    """持久化监控计划并将已触发计划标记为一次性事件。"""

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir).expanduser().resolve() / "market-monitor.json"

    def replace(self, plans: Any) -> dict[str, Any]:
        if not isinstance(plans, list) or not 1 <= len(plans) <= MAX_PLANS:
            return _error("invalid_argument", f"plans 必须是 1 到 {MAX_PLANS} 项数组")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for plan in plans:
            result = _normalize_plan(plan)
            if isinstance(result, str):
                return _error("invalid_argument", result)
            key = result["plan_id"]
            if key in seen:
                return _error("invalid_argument", "plan_id 不能重复")
            seen.add(key)
            normalized.append(result)
        self._write({"updated_at": _now(), "plans": normalized})
        return _success("monitor_replace", {"path": str(self.path), "plans": normalized})

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return _success("monitor_read", {"updated_at": None, "plans": []})
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _error("state_error", f"监控计划读取失败：{type(exc).__name__}")
        if not isinstance(data, dict) or not isinstance(data.get("plans"), list):
            return _error("state_error", "监控计划文件格式无效")
        return _success("monitor_read", data)

    def clear(self) -> dict[str, Any]:
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError as exc:
            return _error("state_error", f"监控计划清理失败：{type(exc).__name__}")
        return _success("monitor_clear", {"plans": []})

    def poll(self, client: MiniQMTClient) -> dict[str, Any]:
        current = self.read()
        if not current["ok"]:
            return current
        plans = current["data"].get("plans", [])
        active = [plan for plan in plans if not plan.get("fired")]
        if not active:
            return _success("monitor_poll", {"events": [], "plans": plans})
        codes = [plan["stock_code"] for plan in active]
        quote_result = client.quotes(codes)
        if not quote_result["ok"]:
            return _error("quote_error", "监控轮询无法取得最新行情")
        events: list[dict[str, Any]] = []
        changed = False
        for plan in plans:
            if plan.get("fired"):
                continue
            quote = _extract_quote(quote_result.get("data"), plan["stock_code"])
            if quote is None:
                continue
            price, quote_at = quote
            if _triggered(plan["trigger"], price):
                plan["fired"] = True
                plan["fired_at"] = _now()
                changed = True
                events.append(
                    {
                        "plan": plan,
                        "stock_code": plan["stock_code"],
                        "price": price,
                        "quote_at": quote_at.isoformat(),
                    }
                )
        if changed:
            self._write({"updated_at": _now(), "plans": plans})
        return _success("monitor_poll", {"events": events, "plans": plans})

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def _normalize_plan(value: Any) -> dict[str, Any] | str:
    if not isinstance(value, dict):
        return "每个监控计划必须是对象"
    allowed = {"plan_id", "stock_code", "side", "trigger", "order", "note"}
    unknown = set(value) - allowed
    if unknown:
        return f"监控计划包含未知字段：{', '.join(sorted(unknown))}"
    plan_id = value.get("plan_id")
    if not isinstance(plan_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{3,127}", plan_id):
        return "plan_id 必须是 4 到 128 位稳定标识"
    stock_code = value.get("stock_code")
    if not isinstance(stock_code, str) or not STOCK_CODE_PATTERN.fullmatch(stock_code):
        return "stock_code 必须是 6 位 A 股代码并带 SH/SZ 后缀"
    side = value.get("side")
    if side not in {"BUY", "SELL"}:
        return "side 只能是 BUY 或 SELL"
    trigger = value.get("trigger")
    if not isinstance(trigger, dict) or set(trigger) - {"type", "value", "baseline"}:
        return "trigger 必须包含 type、value，可选 baseline"
    trigger_type = trigger.get("type")
    threshold = trigger.get("value")
    if trigger_type not in TRIGGER_TYPES or isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return "trigger.type 或 trigger.value 无效"
    if not math.isfinite(float(threshold)):
        return "trigger.value 必须是有限数字"
    if trigger_type.startswith("change_pct"):
        baseline = trigger.get("baseline")
        if isinstance(baseline, bool) or not isinstance(baseline, (int, float)) or not math.isfinite(float(baseline)) or float(baseline) <= 0:
            return "change_pct 触发必须提供正数 baseline"
    order = value.get("order", {})
    if not isinstance(order, dict):
        return "order 必须是对象"
    volume = order.get("volume")
    if isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0:
        return "order.volume 必须是正整数"
    price = order.get("price")
    if price is not None and (
        isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(float(price)) or float(price) <= 0
    ):
        return "order.price 必须是正数或省略"
    # 监控器只保存交易意图，实际账户、价格新鲜度和数量限制由交易工具重检。
    safe_order = {key: order[key] for key in ("volume", "price") if key in order}
    return {
        "plan_id": plan_id,
        "stock_code": stock_code,
        "side": side,
        "trigger": dict(trigger),
        "order": safe_order,
        "note": str(value.get("note", ""))[:1000],
        "fired": False,
    }


def _triggered(trigger: dict[str, Any], price: float) -> bool:
    threshold = float(trigger["value"])
    trigger_type = trigger["type"]
    if trigger_type == "price_lte":
        return price <= threshold
    if trigger_type == "price_gte":
        return price >= threshold
    baseline = float(trigger["baseline"])
    change_pct = (price - baseline) / baseline * 100
    return change_pct <= threshold if trigger_type == "change_pct_lte" else change_pct >= threshold


def _now() -> str:
    return datetime.now(TRADING_TZ).isoformat()


def _success(operation: str, data: Any) -> dict[str, Any]:
    return {"ok": True, "status": "success", "operation": operation, "data": data, "error": None}


def _error(code: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "status": "error", "operation": None, "data": None, "error": {"code": code, "detail": detail}}
