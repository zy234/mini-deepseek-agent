"""金融计算工具。

计算在宿主侧完成，模型只负责解释结果。该模块不访问网络、账户或环境密钥，
所有输入都经过显式校验，并返回可重放的版本和输入摘要。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from typing import Any

CALC_VERSION = "financial-calc/v1"


def execute_financial_calc(operation: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """执行一个确定性计算并返回稳定 JSON 结构。"""
    try:
        if operation == "returns":
            data, warnings = _returns(inputs)
        elif operation == "max_drawdown":
            data, warnings = _max_drawdown(inputs)
        elif operation == "risk_metrics":
            data, warnings = _risk_metrics(inputs)
        elif operation == "dcf":
            data, warnings = _dcf(inputs)
        elif operation == "portfolio_risk":
            data, warnings = _portfolio_risk(inputs)
        else:
            return _error("invalid_argument", f"不支持的计算 operation：{operation}")
    except ValueError as exc:
        return _error("invalid_argument", str(exc))
    except (TypeError, KeyError) as exc:
        return _error("invalid_argument", f"输入结构无效：{exc}")
    return {
        "ok": True,
        "status": "success",
        "schema": CALC_VERSION,
        "operation": operation,
        "input_hash": _input_hash(inputs),
        "data": data,
        "warnings": warnings,
        "missing_data": [],
        "error": None,
    }


def _returns(inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    prices = _numbers(inputs, "prices", minimum=2, positive=True)
    result = [(prices[i] / prices[i - 1]) - 1.0 for i in range(1, len(prices))]
    return {"returns": _rounded(result)}, []


def _max_drawdown(inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    equity = _numbers(inputs, "equity", minimum=1, positive=True)
    peak = equity[0]
    peak_index = 0
    max_drawdown_peak_index = 0
    max_dd = 0.0
    trough_index = 0
    drawdown = []
    for index, value in enumerate(equity):
        if value > peak:
            peak, peak_index = value, index
        current = value / peak - 1.0
        drawdown.append(current)
        if current < max_dd:
            max_dd, trough_index = current, index
            max_drawdown_peak_index = peak_index
    return {
        "max_drawdown": round(max_dd, 10),
        "peak_index": max_drawdown_peak_index,
        "trough_index": trough_index,
        "drawdown_series": _rounded(drawdown),
    }, []


def _risk_metrics(inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    values = _numbers(inputs, "returns", minimum=2)
    annualization = _positive_number(inputs, "annualization", default=252.0)
    risk_free = _number(inputs, "risk_free", default=0.0)
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    volatility = math.sqrt(variance) * math.sqrt(annualization)
    annual_return = mean * annualization
    sharpe = (annual_return - risk_free) / volatility if volatility else None
    warnings = ["volatility_zero"] if volatility == 0 else []
    return {
        "observations": len(values),
        "mean_return": round(mean, 10),
        "annualized_return": round(annual_return, 10),
        "annualized_volatility": round(volatility, 10),
        "sharpe": None if sharpe is None else round(sharpe, 10),
        "annualization": annualization,
        "risk_free": risk_free,
    }, warnings


def _dcf(inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    cashflows = _numbers(inputs, "cashflows", minimum=1)
    discount_rate = _number(inputs, "discount_rate")
    terminal_growth = _number(inputs, "terminal_growth")
    if discount_rate <= terminal_growth:
        raise ValueError("discount_rate 必须大于 terminal_growth")
    if discount_rate <= -1 or terminal_growth <= -1:
        raise ValueError("折现率和终值增长率必须大于 -1")
    discounted = [value / ((1 + discount_rate) ** (index + 1)) for index, value in enumerate(cashflows)]
    terminal_value = cashflows[-1] * (1 + terminal_growth) / (discount_rate - terminal_growth)
    terminal_present_value = terminal_value / ((1 + discount_rate) ** len(cashflows))
    enterprise_value = sum(discounted) + terminal_present_value
    warnings = ["negative_cashflow"] if any(value < 0 for value in cashflows) else []
    return {
        "enterprise_value": round(enterprise_value, 10),
        "present_value_cashflows": round(sum(discounted), 10),
        "terminal_value": round(terminal_value, 10),
        "terminal_present_value": round(terminal_present_value, 10),
        "discounted_cashflows": _rounded(discounted),
        "discount_rate": discount_rate,
        "terminal_growth": terminal_growth,
    }, warnings


def _portfolio_risk(inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    positions = inputs.get("positions")
    if not isinstance(positions, list) or not positions:
        raise ValueError("positions 必须是非空数组")
    values: list[float] = []
    for index, position in enumerate(positions):
        if not isinstance(position, dict):
            raise ValueError(f"positions[{index}] 必须是对象")
        value = position.get("market_value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"positions[{index}].market_value 必须是有限数字")
        if value < 0:
            raise ValueError(f"positions[{index}].market_value 不能为负数")
        values.append(float(value))
    total = sum(values)
    if total <= 0:
        raise ValueError("market_value 总和必须大于 0")
    weights = [value / total for value in values]
    warnings = ["zero_value_position"] if any(value == 0 for value in values) else []
    return {
        "total_market_value": round(total, 10),
        "position_weights": _rounded(weights),
        "largest_position_weight": round(max(weights), 10),
        "herfindahl_index": round(sum(weight * weight for weight in weights), 10),
        "position_count": len(values),
    }, warnings


def _numbers(inputs: dict[str, Any], key: str, *, minimum: int = 1, positive: bool = False) -> list[float]:
    values = inputs.get(key)
    if not isinstance(values, list) or len(values) < minimum:
        raise ValueError(f"{key} 必须是至少包含 {minimum} 项的数组")
    result = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key}[{index}] 必须是有限数字")
        if positive and value <= 0:
            raise ValueError(f"{key}[{index}] 必须大于 0")
        result.append(float(value))
    return result


def _number(inputs: dict[str, Any], key: str, default: float | None = None) -> float:
    value = inputs.get(key, default)
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} 必须是有限数字")
    return float(value)


def _positive_number(inputs: dict[str, Any], key: str, default: float) -> float:
    value = _number(inputs, key, default)
    if value <= 0:
        raise ValueError(f"{key} 必须大于 0")
    return value


def _rounded(values: Iterable[float]) -> list[float]:
    return [round(value, 10) for value in values]


def _input_hash(inputs: dict[str, Any]) -> str:
    payload = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _error(code: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "schema": CALC_VERSION,
        "operation": None,
        "input_hash": None,
        "data": None,
        "warnings": [],
        "missing_data": [],
        "error": {"code": code, "detail": detail},
    }
