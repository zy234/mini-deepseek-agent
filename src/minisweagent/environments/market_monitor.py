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
# 买入只允许区间触发：单点 price_lte 的真实语义是"越跌越买"，跳空砸穿也会成交，
# 把趋势跟随写成了接刀；区间的下界挡住破位，上界挡住跳空追高，两个数字都是硬约束，
# 不再依赖 note 里的自然语言去指望执行端二次确认（执行端既无权也无数据做这件事）。
SELL_TRIGGER_TYPES = {"price_lte", "price_gte", "immediate"}
BUY_TRIGGER_TYPES = {"price_range"}
TRIGGER_TYPES = SELL_TRIGGER_TYPES | BUY_TRIGGER_TYPES
MAX_PLANS = 20


class MarketMonitor:
    """持久化监控计划并将已触发计划标记为一次性事件。"""

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir).expanduser().resolve() / "market-monitor.json"

    def replace(self, plans: Any) -> dict[str, Any]:
        if not isinstance(plans, list) or len(plans) > MAX_PLANS:
            return _error("invalid_argument", f"plans 必须是最多 {MAX_PLANS} 项数组")
        previous = self.read()
        if not previous["ok"]:
            return previous
        # 已触发的事实不能被重算抹掉：一天里盘前、午盘、盘中扫描都会 replace 整张表，
        # 如果 fired 每次归零，上午已经成交的卖出下午会再触发一次。想重新布防就必须换一个 plan_id，
        # 那是一笔新意图，也会拿到新的 client_intent_id。
        fired_before = {
            plan["plan_id"]: plan
            for plan in previous["data"].get("plans", [])
            if isinstance(plan, dict) and plan.get("fired") and isinstance(plan.get("plan_id"), str)
        }
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
            if key in fired_before:
                result["fired"] = True
                result["fired_at"] = fired_before[key].get("fired_at")
            normalized.append(result)
        # 换仓是一个决策，不是两张互不相干的单：买入声明了资金来源，就必须同批存在那只票的卖出计划，
        # 否则盘中可能只买不卖，现金和集中度双双失控。
        sell_codes = {plan["stock_code"] for plan in normalized if plan["side"] == "SELL"}
        for plan in normalized:
            source = plan.get("rotate_from")
            if source and source not in sell_codes:
                return _error("invalid_argument", f"{plan['plan_id']} 声明 rotate_from={source}，但同批没有该股票的 SELL 计划")
        self._write({"updated_at": _now(), "plans": normalized})
        return _success(
            "monitor_replace",
            {"path": str(self.path), "kept_fired": sorted(seen & set(fired_before)), "plans": normalized},
        )

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
            # 行情失败的真正原因在 client 的 error.detail 里，吞掉它等于盘中轮询无法排查。
            detail = (quote_result.get("error") or {}).get("detail") or "未知原因"
            return _error("quote_error", f"监控轮询无法取得最新行情：{detail}")
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
    allowed = {"plan_id", "stock_code", "side", "trigger", "order", "note", "rotate_from"}
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
    if not isinstance(trigger, dict) or set(trigger) - {"type", "value", "upper", "baseline"}:
        return "trigger 必须包含 type、value，price_range 另需 upper，baseline 可选且只作记录"
    trigger_type = trigger.get("type")
    threshold = trigger.get("value")
    if trigger_type not in TRIGGER_TYPES:
        return f"trigger.type 只能是 {'、'.join(sorted(TRIGGER_TYPES))}"
    expected = BUY_TRIGGER_TYPES if side == "BUY" else SELL_TRIGGER_TYPES
    if trigger_type not in expected:
        return f"{side} 的 trigger.type 只能是 {'、'.join(sorted(expected))}"
    # immediate 是时间止损专用：到期退出与价格无关，用 price_gte 0 假装无条件只会让人读不懂计划。
    if trigger_type == "immediate":
        if threshold is not None:
            return "immediate 触发不接受 value：它按时间止损无条件触发，与价格无关"
    elif not _finite_positive(threshold):
        return "trigger.value 必须是正数"
    order = value.get("order", {})
    if not isinstance(order, dict):
        return "order 必须是对象"
    volume = order.get("volume")
    if isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0:
        return "order.volume 必须是正整数"
    price = order.get("price")
    if price is not None and not _finite_positive(price):
        return "order.price 必须是正数或省略"
    if trigger_type == "price_range":
        upper = trigger.get("upper")
        if not _finite_positive(upper):
            return "price_range 必须提供正数 upper 作为追高上限"
        if float(upper) <= float(threshold):
            return "price_range 的 upper 必须高于 value（value 是下界，upper 是上界）"
        # 买入不再提前钉死限价：区间宽度通常远大于宿主的限价偏离上限，价格从区间下沿触发时
        # 一个固定的 upper 限价必然偏离现价太多而被交易工具拒掉，等于这条计划永远无法成交。
        # 限价改由交易工具在提交那一刻按最新价推导，计划只需要给出 upper 作为追高上限。
        if price is not None:
            return "price_range 触发的买入不接受 order.price：限价由交易工具按触发时最新价推导，上限是 trigger.upper"
    baseline = trigger.get("baseline")
    if baseline is not None and not _finite_positive(baseline):
        return "trigger.baseline 必须是正数或省略"
    rotate_from = value.get("rotate_from")
    if rotate_from is not None:
        if side != "BUY":
            return "只有 BUY 计划可以声明 rotate_from"
        if not isinstance(rotate_from, str) or not STOCK_CODE_PATTERN.fullmatch(rotate_from):
            return "rotate_from 必须是 6 位 A 股代码并带 SH/SZ 后缀"
        if rotate_from == stock_code:
            return "rotate_from 不能是自己"
    # 监控器只保存交易意图，实际账户、价格新鲜度和数量限制由交易工具重检。
    safe_order = {key: order[key] for key in ("volume", "price") if key in order}
    normalized = {
        "plan_id": plan_id,
        "stock_code": stock_code,
        "side": side,
        "trigger": dict(trigger),
        "order": safe_order,
        "note": str(value.get("note", ""))[:1000],
        "fired": False,
    }
    if rotate_from:
        normalized["rotate_from"] = rotate_from
    return normalized


def _finite_positive(value: Any) -> bool:
    return (
        not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0
    )


def _triggered(trigger: dict[str, Any], price: float) -> bool:
    trigger_type = trigger["type"]
    if trigger_type == "immediate":
        return True
    threshold = float(trigger["value"])
    if trigger_type == "price_lte":
        return price <= threshold
    if trigger_type == "price_gte":
        return price >= threshold
    # price_range：突破腿把区间挂在现价上方（下界=pivot，上界=追高上限），
    # 回踩腿把区间挂在现价下方（下界=不破的结构位，上界=回踩起点）。判定完全相同。
    return threshold <= price <= float(trigger["upper"])


def _now() -> str:
    return datetime.now(TRADING_TZ).isoformat()


def _success(operation: str, data: Any) -> dict[str, Any]:
    return {"ok": True, "status": "success", "operation": operation, "data": data, "error": None}


def _error(code: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "status": "error", "operation": None, "data": None, "error": {"code": code, "detail": detail}}
