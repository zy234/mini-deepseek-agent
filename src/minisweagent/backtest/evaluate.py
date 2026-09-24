"""结论质量评估：T 时刻的判断，用之后的真实走势打分。

这是回测里唯一"事后"的部分——forward 收益、MFE/MAE 都来自模型根本没看到的未来 bar，
它们回答的是"结论准不准"；最终资金回答的是"赚没赚"。两个都要看：结论准但规则差，
和结论不准，赔钱的方式不一样。

前向评估走的是 T+1 之后的**日线**，不是当日 intraday：A 股 T+1，day-1 买入当天根本卖不掉，
拿当日收盘价算 BUY 的收益是在评估一个卖不出去的仓位。所以买在决策价，最早 day+1 才能卖，
看之后若干个交易日的日线走势——这才是这条策略真实的收益兑现窗口。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def forward_view_daily(forward_bars: list[dict], entry_price: float) -> dict[str, Any] | None:
    """决策日之后 N 个交易日的日线走势：买在决策价，T+1 下最早 day+1 才能卖。

    forward_bars 是 trade_date 之后、按日期升序的日线（调用方已截到 N 根）。第一根就是 day+1，
    T+1 下最早的可卖日；t1_* 是这一天的兑现，mfe/mae 是整个窗口里能摸到的最好/最坏离场点，
    fwd_close 是持满窗口到最后一天收盘的结果。缺 open/high/low/close 的残缺 bar 整根丢掉。
    """
    usable = [
        bar
        for bar in forward_bars
        if all(isinstance(bar.get(field), (int, float)) and bar.get(field) for field in ("open", "high", "low", "close"))
    ]
    if not usable or not entry_price:
        return None
    first = usable[0]  # day+1：T+1 下最早可卖的交易日
    high = max(float(bar["high"]) for bar in usable)
    low = min(float(bar["low"]) for bar in usable)
    return {
        "fwd_days": len(usable),
        "t1_open_pct": round((float(first["open"]) / entry_price - 1) * 100, 2),
        "t1_close_pct": round((float(first["close"]) / entry_price - 1) * 100, 2),
        "mfe_pct": round((high / entry_price - 1) * 100, 2),
        "mae_pct": round((low / entry_price - 1) * 100, 2),
        "fwd_close_pct": round((float(usable[-1]["close"]) / entry_price - 1) * 100, 2),
    }


def action_stats(records: list[dict]) -> dict[str, dict]:
    """按动作聚合前向收益。BUY 命中 = day+1 收盘上涨，SELL 命中 = day+1 收盘下跌；HOLD 不定义命中率。

    命中口径统一用 t1_close_pct（day+1 收盘）——这是 T+1 下最早能兑现的那天，BUY 该赚、SELL 该躲的
    就是这一天。mfe/mae 给出整个前向窗口的收益边界，fwd_close 给持满窗口的结果。
    """
    hit: dict[str, Callable[[dict], bool] | None] = {
        "BUY": lambda record: record["t1_close_pct"] > 0,
        "SELL": lambda record: record["t1_close_pct"] < 0,
        "HOLD": None,
    }
    stats: dict[str, dict] = {}
    for action, judge in hit.items():
        rows = [
            record for record in records if record.get("action") == action and record.get("t1_close_pct") is not None
        ]
        if not rows:
            continue
        forward = sorted(record["t1_close_pct"] for record in rows)
        middle = len(forward) // 2
        median = forward[middle] if len(forward) % 2 else (forward[middle - 1] + forward[middle]) / 2
        stats[action] = {
            "count": len(rows),
            "mean_t1_close_pct": round(sum(forward) / len(forward), 2),
            "median_t1_close_pct": round(median, 2),
            "hit_rate": round(sum(1 for record in rows if judge(record)) / len(rows), 2) if judge else None,
            "mean_t1_open_pct": round(sum(record["t1_open_pct"] for record in rows) / len(rows), 2),
            "mean_mfe_pct": round(sum(record["mfe_pct"] for record in rows) / len(rows), 2),
            "mean_mae_pct": round(sum(record["mae_pct"] for record in rows) / len(rows), 2),
            "mean_fwd_close_pct": round(sum(record["fwd_close_pct"] for record in rows) / len(rows), 2),
        }
    return stats


def print_summary(report: dict, echo: Callable[[str], None]) -> None:
    """终端摘要：先看 T+1 之后的结论质量，再看当日纸面成交，错误只报数量、细节在报告文件里。"""
    fwd_days = report.get("forward_days")
    trades = sum(len(slot.get("orders") or []) for slot in report["slots"])
    echo(
        f"回测 {report['trade_date']}：{len(report['slots'])} 个槽位、{len(report['verdicts'])} 条结论、"
        f"当日纸面成交 {trades} 笔，前向窗口 {fwd_days} 个交易日"
    )
    for action in ("BUY", "SELL", "HOLD"):
        if action not in report["stats"]:
            continue
        item = report["stats"][action]
        hit = f"，命中率 {item['hit_rate']:.0%}" if item["hit_rate"] is not None else ""
        echo(
            f"{action} {item['count']} 条：T+1(day+1) 开盘 {item['mean_t1_open_pct']:+.2f}%、"
            f"收盘平均 {item['mean_t1_close_pct']:+.2f}%（中位 {item['median_t1_close_pct']:+.2f}%{hit}）；"
            f"窗口内最大有利 {item['mean_mfe_pct']:+.2f}%、最大不利 {item['mean_mae_pct']:+.2f}%、"
            f"持满收盘 {item['mean_fwd_close_pct']:+.2f}%"
        )
    final = report.get("final")
    if final:
        echo(
            f"（当日纸面：初始 {final['initial_cash']:.2f} → 收盘估值 {final['total']:.2f}，"
            f"{final['return_pct']:+.2f}%；T+1 下买入当日不可卖，此数仅供 SELL 侧参考）"
        )
    if report["errors"]:
        echo(f"取数与读图错误 {len(report['errors'])} 条，详见报告文件。")
