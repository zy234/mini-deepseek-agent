"""历史行情重放：把过去的某个交易日截断到指定时刻，装成实盘那一轮的数据包。

回测的命门是口径：模型在 T 时刻看到的图和数字，必须和实盘同一时刻看到的一致。所以这里
不自己发明指标——行情行、趋势字段、时段进度、K 线渲染全部复用 miniqmt 和 trading.context
的现成函数。回测另写一套口径，等于在回测一个不存在的策略。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from minisweagent.environments.miniqmt import (
    TRADING_TZ,
    MiniQMTClient,
    _screen_row,
    _session_progress,
    _trend_metrics,
)
from minisweagent.trading import charts
from minisweagent.trading.context import _bars, _render_pair

# 连续竞价时段（一天内分钟数）：9:30-11:30、13:00-15:00，与流水线 SESSIONS 同一口径。
SESSION_MINUTES = ((570, 690), (780, 900))


class BacktestDataError(RuntimeError):
    """重放数据凑不齐。宁可这一轮明确失败，也不要拿错位的图去问模型。"""


def fetch_bars(
    client: MiniQMTClient, codes: list[str], trade_date: date, errors: list[str]
) -> tuple[dict[str, list], dict[str, list]]:
    """拉全部日线（90 天窗口）和当日分钟线。槽位截断在内存里做，不重复请求。

    走 context._bars 而不是另写一份取数：它按 20 只一批、先下载再读、空数据报错，这些
    行为回测和实盘必须一致，抄第二份必然漂移。传"当日 15:00"当 now，窗口正好落在回测日。
    """
    clock = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 0, tzinfo=TRADING_TZ)
    return _bars(client, codes, clock, errors)


def slot_times(index_minutes: list[dict], trade_date: date, interval_minutes: int) -> list[str]:
    """生成槽位时刻：和实盘 _round_slot 一样按固定间隔对齐时段，指数分钟线做有效性闸门。

    少于 2 根 bar 画不出分钟图；之后连 1 根 bar 都没有就没有评估窗口。这两种槽位直接不跑，
    所以最后一个槽位天然是"还剩至少一分钟"的那个。
    """
    stamps = [stamp for stamp in (_minute_stamp(bar) for bar in index_minutes) if stamp]
    day = trade_date.strftime("%Y%m%d")
    slots = []
    for start, end in SESSION_MINUTES:
        for minutes in range(start, end + 1, interval_minutes):
            hhmm = f"{minutes // 60:02d}{minutes % 60:02d}"
            cutoff = int(f"{day}{hhmm}00")
            if sum(1 for stamp in stamps if stamp <= cutoff) >= 2 and any(stamp > cutoff for stamp in stamps):
                slots.append(hhmm)
    return slots


def slot_view(
    daily: dict[str, list],
    intraday: dict[str, list],
    *,
    trade_date: date,
    hhmm: str,
    stock_codes: list[str],
    index_codes: list[str],
    max_buy_notional: float,
) -> dict[str, Any]:
    """装出 hhmm 时刻的视角：分钟线截断、当日日 bar 用截断分钟线重构、指标全部现算。"""
    day = trade_date.strftime("%Y%m%d")
    cutoff = int(f"{day}{hhmm}00")
    as_of = datetime(
        trade_date.year, trade_date.month, trade_date.day, int(hhmm[:2]), int(hhmm[2:]), tzinfo=TRADING_TZ
    ).isoformat(timespec="seconds")
    quote_at = f"{day} {hhmm[:2]}:{hhmm[2:]}:00"
    errors: list[str] = []
    stocks = {code: entry for code in stock_codes if (entry := _stock_view(code, daily, intraday, cutoff, day, quote_at, max_buy_notional, errors))}
    indexes: list[dict[str, Any]] = []
    index_data: dict[str, dict] = {}
    for code in index_codes:
        minutes = _truncated(intraday.get(code) or [], cutoff)
        prior_close = _prior_close(daily.get(code) or [], day)
        if len(minutes) < 2 or prior_close is None:
            errors.append(f"指数 {code} 在 {hhmm} 凑不齐分钟线或昨收")
            indexes.append({"stock_code": code, "error": "行情缺失"})
            continue
        last = float(minutes[-1]["close"])
        indexes.append(
            {
                "stock_code": code,
                "last": last,
                "prev_close": prior_close,
                "change_pct": round((last / prior_close - 1) * 100, 2),
                "amount": round(sum(float(bar["amount"] or 0) for bar in minutes), 2),
                "quote_at": quote_at,
            }
        )
        index_data[code] = {"daily": daily.get(code) or [], "minutes": minutes}
    return {
        "as_of": as_of,
        "cutoff": cutoff,
        "stocks": stocks,
        "indexes": indexes,
        "index_data": index_data,
        "errors": errors,
    }


def render_pair(chart_dir, code: str, entry: dict, daily_days: int, errors: list[str], *, average: bool = True) -> dict[str, str]:
    """渲染一只标的的日线图和分钟图，直接走实盘的 _render_pair。"""
    return _render_pair(chart_dir, code, entry["daily"], entry["minutes"], daily_days, errors, average=average)


def _stock_view(
    code: str,
    daily: dict[str, list],
    intraday: dict[str, list],
    cutoff: int,
    day: str,
    quote_at: str,
    max_buy_notional: float,
    errors: list[str],
) -> dict[str, Any] | None:
    minutes = _truncated(intraday.get(code) or [], cutoff)
    prior_close = _prior_close(daily.get(code) or [], day)
    if len(minutes) < 2 or prior_close is None:
        errors.append(f"{code} 在该时刻凑不齐分钟线或昨收，本槽位不注入")
        return None
    as_of_daily = _daily_as_of(daily.get(code) or [], minutes, day)
    row = _screen_row(code, _tick_from(minutes, prior_close), max_buy_notional)
    if row is None:
        errors.append(f"{code} 行情行拼不出来")
        return None
    today, elapsed = _session_progress(quote_at)
    row.update(_trend_metrics(row["last_price"], row["volume"], as_of_daily, today, elapsed))
    return {"row": row, "daily": as_of_daily, "minutes": minutes, "prev_close": prior_close}


def _truncated(intraday_bars: list[dict], cutoff: int) -> list[dict]:
    """丢掉不完整和形状不对的 bar 后，按时刻截断。"""
    usable = charts.usable_bars(intraday_bars)
    return [bar for bar in usable if (stamp := _minute_stamp(bar)) is not None and stamp <= cutoff]


def _minute_stamp(bar: dict) -> int | None:
    """1m bar 的 index 是 YYYYMMDDHHMMSS 十四位整数；别的形状一律不认，不猜。"""
    stamp = bar.get("date")
    return stamp if isinstance(stamp, int) and len(str(stamp)) == 14 else None


def _prior_close(daily_bars: list[dict], day: str) -> float | None:
    """昨收 = 该交易日之前的最后一根日线。当日（含完整）bar 不能当昨收。"""
    prior = [
        bar
        for bar in daily_bars
        if bar.get("date") != int(day) and isinstance(bar.get("close"), (int, float)) and bar["close"]
    ]
    return float(prior[-1]["close"]) if prior else None


def _daily_as_of(daily_bars: list[dict], minutes: list[dict], day: str) -> list[dict]:
    """当日日 bar 用截断分钟线重构：实盘里它本来就是盘中实时长出来的，重放必须同样处理。

    _trend_metrics 和 render_daily 都会把最后一根当"当日 bar"排除出 pivot 计算，所以
    重构 bar 放在末尾即可与实盘口径对齐。
    """
    bars = [bar for bar in daily_bars if bar.get("date") != int(day)]
    bars.append(
        {
            "date": int(day),
            "open": float(minutes[0]["open"]),
            "high": max(float(bar["high"]) for bar in minutes),
            "low": min(float(bar["low"]) for bar in minutes),
            "close": float(minutes[-1]["close"]),
            "volume": sum(float(bar["volume"]) for bar in minutes),
            "amount": sum(float(bar["amount"] or 0) for bar in minutes),
        }
    )
    return bars


def _tick_from(minutes: list[dict], prior_close: float) -> dict[str, Any]:
    """把截断分钟线聚成 _screen_row 要的 tick 形状，让它和实盘走同一条路算行情行。"""
    return {
        "lastPrice": float(minutes[-1]["close"]),
        "lastClose": prior_close,
        "open": float(minutes[0]["open"]),
        "high": max(float(bar["high"]) for bar in minutes),
        "low": min(float(bar["low"]) for bar in minutes),
        "volume": sum(float(bar["volume"]) for bar in minutes),
        "amount": sum(float(bar["amount"] or 0) for bar in minutes),
    }
