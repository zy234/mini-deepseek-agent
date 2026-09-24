"""回测结果评估：多日闭环里，买卖信号真金白银赚没赚。

回测跑的是实盘那条链的真实盈亏：day-1 建仓、之后每天管持仓找安全卖点、跨日结转。所以"赚没赚"
直接看纸面账户从初始资金到末日估值的变化（final.return_pct），以及每只票的买入→卖出兑现。不再
用"决策后 N 天固定价格"那种事后窗口——那评估的是一个没有卖出纪律的假策略。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def trade_summary(trades: list[dict]) -> dict[str, dict[str, Any]]:
    """按标的聚合买卖：买入量/均价、卖出量/均价、已实现毛收益（卖出回款 - 对应买入成本，含费）。

    卖出量可能小于买入量（还没卖完，剩的算未实现，在 final 的持仓市值里）；只对已卖出的部分算
    已实现收益，口径粗但透明：卖出回款净额 - 同量买入成本（按买入均价）。
    """
    summary: dict[str, dict[str, Any]] = {}
    for order in trades:
        code = order["stock_code"]
        item = summary.setdefault(
            code,
            {"buy_vol": 0, "buy_cost": 0.0, "sell_vol": 0, "sell_proceeds": 0.0, "first_buy": None, "last_sell": None},
        )
        gross = order["price"] * order["volume"]
        if order["action"] == "BUY":
            item["buy_vol"] += order["volume"]
            item["buy_cost"] += gross + order["fee"]
            item["first_buy"] = item["first_buy"] or f"{order.get('date', '')} {order['slot']}@{order['price']}"
        else:
            item["sell_vol"] += order["volume"]
            item["sell_proceeds"] += gross - order["fee"]
            item["last_sell"] = f"{order.get('date', '')} {order['slot']}@{order['price']}"
    for item in summary.values():
        avg_buy = item["buy_cost"] / item["buy_vol"] if item["buy_vol"] else 0.0
        # 已实现：卖出回款 - 卖出这部分量对应的买入成本（按买入均价折算）
        matched_cost = avg_buy * item["sell_vol"]
        item["realized_pnl"] = round(item["sell_proceeds"] - matched_cost, 2) if item["sell_vol"] else 0.0
        item["realized_pct"] = round((item["sell_proceeds"] / matched_cost - 1) * 100, 2) if matched_cost else None
        item["open_vol"] = item["buy_vol"] - item["sell_vol"]
    return summary


def print_summary(report: dict, echo: Callable[[str], None]) -> None:
    """终端摘要：先看账户到末日赚没赚，再逐票看买卖兑现，错误只报数量、细节在报告文件里。"""
    final = report["final"]
    trades = report["trades"]
    buys = sum(1 for order in trades if order["action"] == "BUY")
    sells = sum(1 for order in trades if order["action"] == "SELL")
    echo(
        f"回测 {report['trade_date']}→{report['through']}：{len(report['trading_days'])} 个交易日、"
        f"每 {report['interval_minutes']} 分钟一槽，成交 买{buys} 卖{sells} 笔"
    )
    echo(
        f"初始 {final['initial_cash']:.2f} → 末日估值 {final['total']:.2f}"
        f"（{final['return_pct']:+.2f}%；现金 {final['cash']:.2f}、持仓市值 {final['market_value']:.2f}）"
    )
    for code, item in trade_summary(trades).items():
        if item["sell_vol"]:
            realized = f"已实现 {item['realized_pnl']:+.2f} 元（{item['realized_pct']:+.2f}%）"
            tail = f"，卖出 {item['last_sell']}" + (f"，仍持 {item['open_vol']} 股" if item["open_vol"] else "，已清仓")
        else:
            realized = f"仍全持 {item['open_vol']} 股（未卖出，收益在期末估值里）"
            tail = ""
        echo(f"  {code}: 买入 {item['first_buy']} → {realized}{tail}")
    if report["errors"]:
        echo(f"取数与读图错误 {len(report['errors'])} 条，详见报告文件。")
