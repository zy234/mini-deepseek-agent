"""结论质量评估：T 时刻的判断，用之后的真实走势打分。

这是回测里唯一"事后"的部分——forward 收益、MFE/MAE 都来自模型根本没看到的未来 bar，
它们回答的是"结论准不准"；最终资金回答的是"赚没赚"。两个都要看：结论准但规则差，
和结论不准，赔钱的方式不一样。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from minisweagent.backtest.replay import _minute_stamp
from minisweagent.trading import charts


def forward_view(intraday_bars: list[dict], cutoff: int, price: float) -> dict[str, Any] | None:
    """决策时刻之后到收盘的走势：收盘价、前向收益、最大有利/不利偏移。"""
    forward = [
        bar
        for bar in charts.usable_bars(intraday_bars)
        if (stamp := _minute_stamp(bar)) is not None and stamp > cutoff
    ]
    if not forward or not price:
        return None
    day_close = float(forward[-1]["close"])
    high = max(float(bar["high"]) for bar in forward)
    low = min(float(bar["low"]) for bar in forward)
    return {
        "day_close": day_close,
        "fwd_pct": round((day_close / price - 1) * 100, 2),
        "mfe_pct": round((high / price - 1) * 100, 2),
        "mae_pct": round((low / price - 1) * 100, 2),
    }


def action_stats(records: list[dict]) -> dict[str, dict]:
    """按动作聚合前向收益。BUY 命中 = 之后上涨，SELL 命中 = 之后下跌；HOLD 不定义命中率。"""
    hit: dict[str, Callable[[dict], bool] | None] = {
        "BUY": lambda record: record["fwd_pct"] > 0,
        "SELL": lambda record: record["fwd_pct"] < 0,
        "HOLD": None,
    }
    stats: dict[str, dict] = {}
    for action, judge in hit.items():
        rows = [record for record in records if record.get("action") == action and record.get("fwd_pct") is not None]
        if not rows:
            continue
        forward = sorted(record["fwd_pct"] for record in rows)
        middle = len(forward) // 2
        median = forward[middle] if len(forward) % 2 else (forward[middle - 1] + forward[middle]) / 2
        stats[action] = {
            "count": len(rows),
            "mean_fwd_pct": round(sum(forward) / len(forward), 2),
            "median_fwd_pct": round(median, 2),
            "hit_rate": round(sum(1 for record in rows if judge(record)) / len(rows), 2) if judge else None,
            "mean_mfe_pct": round(sum(record["mfe_pct"] for record in rows) / len(rows), 2),
            "mean_mae_pct": round(sum(record["mae_pct"] for record in rows) / len(rows), 2),
        }
    return stats


def print_summary(report: dict, echo: Callable[[str], None]) -> None:
    """终端摘要：先看钱，再看结论质量，错误只报数量、细节在报告文件里。"""
    final = report["final"]
    trades = sum(len(slot.get("orders") or []) for slot in report["slots"])
    echo(
        f"回测 {report['trade_date']}：{len(report['slots'])} 个槽位、{len(report['verdicts'])} 条结论、{trades} 笔成交"
    )
    echo(
        f"初始资金 {final['initial_cash']:.2f} → 最终 {final['total']:.2f}"
        f"（{final['return_pct']:+.2f}%，持仓市值 {final['market_value']:.2f}）"
    )
    for action in ("BUY", "SELL", "HOLD"):
        if action not in report["stats"]:
            continue
        item = report["stats"][action]
        hit = f"，命中率 {item['hit_rate']:.0%}" if item["hit_rate"] is not None else ""
        echo(
            f"{action} {item['count']} 条：到收盘平均 {item['mean_fwd_pct']:+.2f}%"
            f"（中位 {item['median_fwd_pct']:+.2f}%{hit}），最大有利均值 {item['mean_mfe_pct']:+.2f}%，"
            f"最大不利均值 {item['mean_mae_pct']:+.2f}%"
        )
    if report["errors"]:
        echo(f"取数与读图错误 {len(report['errors'])} 条，详见报告文件。")
