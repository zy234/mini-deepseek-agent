"""板块热度榜与成分股数据源：akshare（东方财富公开接口）。

大 QMT 撤掉极简接口后，native xtdata 的行情服务连不上，`get_sector_list` 枚举和申万/通达信
板块成分在 ZMQ 后端都取不到（只剩 ~13 个内置板块）。所以板块「有哪些、哪个热、里面有哪些票」
改由 akshare 从东方财富取——这部分是纯本地 HTTP，不碰交易终端；成分股的实时 tick 与日线趋势
仍然走 ZMQ（get_full_tick / get_market_data 都正常）。

口径对齐 pipeline 原来的两族：
- 概念（原 TGN 通达信概念）→ 东财概念板块，定当日主线。
- 行业（原 SW2 申万二级）→ 东财行业板块，交叉验证主线背后有没有行业级资金。

热度排序沿用原策略：不按板块指数涨幅（会被买不起的龙头抬高），而是取板块成分里「可买票」的
中位涨幅——复用 miniqmt 的 `_screen_row` / `_sector_summary`，把 akshare 成分行情映射成 tick 形状。
每个板块还带上当日主力资金净流入，让模型能挑「可买票在动 且 主力在进」的方向。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from minisweagent.environments.miniqmt import (
    LOT_SIZE,
    SECTOR_RANK_MIN_MEMBERS,
    STOCK_CODE_PATTERN,
    TRADING_TZ,
    _sector_summary,
)

logger = logging.getLogger(__name__)

# 东财每天随机断连（实测行业榜首拉 RemoteDisconnected、重试即过），所有取数都要带重试。
RETRY_ATTEMPTS = 3
RETRY_SLEEP_SECONDS = 1.5
# 板块全表几百个，逐个拉成分太慢；只对按资金净流入排在前面的这些板块拉成分算可买热度。
DEFAULT_SCAN_BOARDS = 15


class BoardDataError(RuntimeError):
    """板块数据取不到。盘前必须有主线板块，宁可这一轮明确失败也不拿残缺数据选池。"""


# 两族到 akshare 三个接口的映射：板块行情表、成分股表、资金流排名的 sector_type。
# 概念=东财概念（原 TGN 通达信概念），行业=东财行业（原 SW2 申万二级）。
FAMILY_FUNCS = {
    "概念": ("stock_board_concept_name_em", "stock_board_concept_cons_em", "概念资金流"),
    "行业": ("stock_board_industry_name_em", "stock_board_industry_cons_em", "行业资金流"),
}


def _retry(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """东财会随机断连，重试几次；用尽还失败就抛，让调用方把原因带进 errors。"""
    last: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # akshare 抛的是 requests/解析异常，统一按可重试处理
            last = exc
            if attempt + 1 < RETRY_ATTEMPTS:
                time.sleep(RETRY_SLEEP_SECONDS)
    raise BoardDataError(f"akshare {getattr(fn, '__name__', fn)} 失败：{last}") from last


def _to_qmt_code(raw: Any) -> str | None:
    """东财返回 6 位纯代码，补 .SH/.SZ 后缀成 QMT/ZMQ 认的格式；非 A 股返回 None。"""
    code = str(raw).strip()
    if len(code) != 6 or not code.isdigit():
        return None
    if code[0] == "6":
        full = f"{code}.SH"
    elif code[0] in ("0", "3"):
        full = f"{code}.SZ"
    else:
        return None  # 4/8 开头是北交所/新三板，这个账户不做
    return full if STOCK_CODE_PATTERN.match(full) else None


def _load_ak() -> Any:
    """akshare 是重依赖且只在盘前用，延迟导入；缺失时给出可操作的报错。"""
    try:
        import akshare as ak
    except ImportError as exc:
        raise BoardDataError("未安装 akshare，盘前板块数据不可用：pip install akshare") from exc
    return ak


def _fund_flow(family: str) -> dict[str, dict[str, float]]:
    """按板块名取当日主力资金净流入；东财按净流入排序，序号即资金热度名次。

    返回 {板块名: {main_net_inflow_yi, main_net_inflow_pct, fund_rank}}。
    注意：09:20 盘前连续竞价未开始，「今日」资金流还没形成，此时拿到的是上一交易日尾盘口径，
    只能当昨日资金主线的延续参考，不能当今日实时资金。
    """
    ak = _load_ak()
    sector_type = FAMILY_FUNCS[family][2]
    frame = _retry(ak.stock_sector_fund_flow_rank, indicator="今日", sector_type=sector_type)
    flow: dict[str, dict[str, float]] = {}
    for rank, (_, row) in enumerate(frame.iterrows(), start=1):
        name = str(row.get("名称") or "").strip()
        if not name:
            continue
        flow[name] = {
            "main_net_inflow_yi": round(float(row.get("今日主力净流入-净额") or 0.0) / 1e8, 2),
            "main_net_inflow_pct": round(float(row.get("今日主力净流入-净占比") or 0.0), 2),
            "fund_rank": rank,
        }
    return flow


def _cons_ticks(family: str, sector_name: str) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """取一个板块的成分股，映射成 miniqmt tick 形状（复用 _screen_row / _sector_summary）。"""
    ak = _load_ak()
    cons_func = getattr(ak, FAMILY_FUNCS[family][1])
    frame = _retry(cons_func, symbol=sector_name)
    codes: list[str] = []
    quotes: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        code = _to_qmt_code(row.get("代码"))
        if code is None or code in quotes:
            continue
        codes.append(code)
        quotes[code] = {
            "lastPrice": row.get("最新价"),
            "lastClose": row.get("昨收"),
            "open": row.get("今开"),
            "high": row.get("最高"),
            "low": row.get("最低"),
            "volume": row.get("成交量"),
            "amount": row.get("成交额"),
        }
    return codes, quotes


def board_rank(
    family: str,
    *,
    limit: int = 12,
    min_buyable: int = 3,
    max_buy_notional: float,
    scan_boards: int = DEFAULT_SCAN_BOARDS,
) -> dict[str, Any]:
    """板块热度榜：东财板块行情 + 主力资金流，取资金最热的若干板块下钻算「可买中位涨幅」。

    排序沿用原策略——按可买票中位涨幅排（不被买不起的龙头抬高），但每行同时带上主力资金净流入，
    让模型能挑「可买票在动 且 主力在进」的方向。下钻板块按主力净流入名次选，把 cons 请求压到 scan_boards 个。
    """
    if family not in FAMILY_FUNCS:
        raise BoardDataError(f"family 只能是 {'、'.join(FAMILY_FUNCS)}")
    ak = _load_ak()
    board_frame = _retry(getattr(ak, FAMILY_FUNCS[family][0]))
    flow = _fund_flow(family)
    # 先按主力资金净流入名次挑要下钻的板块：资金在进的方向才值得逐个拉成分算可买热度。
    boards = [str(row.get("板块名称") or "").strip() for _, row in board_frame.iterrows()]
    boards = [name for name in boards if name]
    boards.sort(key=lambda name: flow.get(name, {}).get("fund_rank", 10**9))
    ranked: list[dict[str, Any]] = []
    failed: list[str] = []
    for name in boards[:scan_boards]:
        try:
            codes, quotes = _cons_ticks(family, name)
        except BoardDataError:
            failed.append(name)
            continue
        summary = _sector_summary(name, codes, quotes, max_buy_notional)
        if not summary or summary["members_quoted"] < SECTOR_RANK_MIN_MEMBERS or summary["buyable_count"] < min_buyable:
            continue
        summary.update(flow.get(name, {"main_net_inflow_yi": None, "main_net_inflow_pct": None, "fund_rank": None}))
        summary["member_codes"] = [code for code in codes if STOCK_CODE_PATTERN.match(code)]
        ranked.append(summary)
    ranked.sort(key=lambda item: item["buyable_median_change_pct"], reverse=True)
    return {
        "family": family,
        "quote_at": datetime.now(TRADING_TZ).isoformat(timespec="seconds"),
        "sectors_scanned": min(scan_boards, len(boards)),
        "sectors_returned": min(limit, len(ranked)),
        "min_buyable": min_buyable,
        "buy_limits": {
            "lot_size": LOT_SIZE,
            "max_buy_notional": max_buy_notional,
            "max_buyable_price": round(max_buy_notional / LOT_SIZE, 2),
        },
        "sort_by": "buyable_median_change_pct",
        "member_fetch_failures": failed[:10],
        "sectors": ranked[:limit],
    }
