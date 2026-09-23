"""宿主侧数据装配：把行情、账户和账本取好、算好、排好版，直接注入 prompt。

这条流水线里模型不再自己调工具取数。原因很直接：取数是确定性动作，让模型决定取什么、
取几次，只会引入"忘了取"、"取错时间窗"和"上下文被整版 tick 淹掉"三类故障。宿主取数则
每一轮的输入都是同一份形状，出错立刻可见。

所有对外函数返回的 dict 里都有 errors 字段：取数失败必须跟着数据一起进 prompt，
模型要知道自己是在残缺数据上做判断，而不是以为市场上就没有那些票。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from minisweagent.environments import akshare_board
from minisweagent.environments.account_journal import read_account_journal
from minisweagent.environments.miniqmt import TRADING_TZ, MiniQMTClient, host_limits
from minisweagent.trading import charts

# 概念板块定当日主线（短线资金炒概念），行业板块交叉验证背后有没有行业级资金。
# 数据源：akshare（东财）。大 QMT 撤极简接口后，QMT 已取不到申万/通达信板块数据，见 akshare_board。
SECTOR_FAMILIES = ("概念", "行业")
SECTOR_RANK_LIMIT = 12
DAILY_LOOKBACK_DAYS = 90
QUOTE_BATCH = 20
# QMT 的委托状态码。模型必须能区分"已成"和"已报未成"，否则会把在途委托当成没下单又下一遍。
ORDER_STATUS = {
    48: "未报",
    49: "待报",
    50: "已报",
    51: "已报待撤",
    52: "部成待撤",
    53: "部撤",
    54: "已撤",
    55: "部成",
    56: "已成",
    57: "废单",
}
# offset_flag 48 是开仓（买入），49 是平仓（卖出）。
OFFSET_SIDE = {48: "BUY", 49: "SELL"}
# 终态委托：已经尘埃落定，不会再产生新成交。其余状态（在途、部成、撤单在途，以及任何不认识的码）
# 都当"可能还有在途委托"处理。成交明细（trades）单次查询要付约 10.5s，而它 ⊆ orders 的世界——
# 能确定没有在途委托时，这一轮跳过 trades 不丢任何决策信息：成没成、有没有重复下单都靠 orders 判断。
TERMINAL_ORDER_STATUS = {"部撤", "已撤", "已成", "废单"}


class MarketDataError(RuntimeError):
    """关键数据取不到。宁可这一轮明确失败，也不要拿残缺数据当完整数据去下单。"""


def premarket_context(
    client: MiniQMTClient,
    *,
    journal_dir: str | Path,
    index_codes: list[str],
    sectors_scanned: int = 8,
    rows_per_sector: int = 8,
) -> dict[str, Any]:
    """盘前数据包：板块热度榜、热板块内部个股结构、大盘、账户和账本。

    9:20 跑，此时集合竞价已经开始撮合，tick 里是今日竞价价格；日线的当日 bar 还没有，
    所以个股结构字段全部基于昨收，这一点必须在 prompt 里讲明，否则模型会当成今日实时结构。
    """
    now = datetime.now(TRADING_TZ)
    errors: list[str] = []
    limits = host_limits()
    # 板块热度榜与成分股走 akshare（东财）；成分股的实时行情/趋势仍走 ZMQ（client.screen）。
    ranks: dict[str, list[dict]] = {}
    for family in SECTOR_FAMILIES:
        try:
            result = akshare_board.board_rank(
                family, limit=SECTOR_RANK_LIMIT, min_buyable=3, max_buy_notional=limits["max_buy_notional"]
            )
        except akshare_board.BoardDataError as exc:
            errors.append(f"{family} 板块热度榜失败：{exc}")
            continue
        ranks[family] = result["sectors"]
    if not ranks.get(SECTOR_FAMILIES[0]):
        raise MarketDataError(f"{SECTOR_FAMILIES[0]} 板块热度榜没有数据，盘前无法选池：{'；'.join(errors)}")
    candidates = []
    for sector in ranks[SECTOR_FAMILIES[0]][:sectors_scanned]:
        # 成分股由 akshare 给出代码，实时行情与日线趋势字段走 ZMQ：拿代码列表让 client.screen 出确定性结论。
        codes = sector.get("member_codes") or []
        if not codes:
            errors.append(f"板块 {sector['sector']} 无可用成分股代码")
            continue
        screened = client.screen(
            stock_codes=codes,
            sort_by="close_position_desc",
            limit=rows_per_sector,
            enrich_trend=True,
        )
        if not screened["ok"]:
            errors.append(f"板块 {sector['sector']} 个股筛选失败：{(screened.get('error') or {}).get('detail', '')}")
            continue
        candidates.append(
            {
                "sector": sector["sector"],
                "sector_stats": {
                    key: sector.get(key)
                    for key in (
                        "members_quoted",
                        "up_ratio",
                        "median_change_pct",
                        "buyable_count",
                        "buyable_median_change_pct",
                        "amount",
                        "main_net_inflow_yi",
                        "main_net_inflow_pct",
                    )
                },
                "quote_at": screened["data"]["quote_at"],
                "rows": screened["data"]["rows"],
            }
        )
    if not candidates:
        raise MarketDataError(f"热门板块个股筛选全部失败，盘前无法选池：{'；'.join(errors)}")
    account = account_context(client, errors)
    return {
        "as_of": now.isoformat(timespec="seconds"),
        "trade_date": now.date().isoformat(),
        "indexes": index_quotes(client, index_codes, errors),
        "sector_ranks": ranks,
        "sector_candidates": candidates,
        "account": account,
        "limits": limits,
        "journal": read_account_journal(journal_dir)["data"],
        "errors": errors,
    }


def round_context(
    client: MiniQMTClient,
    *,
    watchlist: dict[str, Any],
    journal_dir: str | Path,
    index_codes: list[str],
    chart_dir: Path,
    daily_days: int = 30,
) -> dict[str, Any]:
    """盘中一轮的数据包：按板块分组的候选、持仓组、大盘，每只票两张图。

    持仓票只出现在持仓组：同一只票在两个组里各出一次结论，汇总阶段就得先解决两份互相矛盾的
    判断，那是自己给自己造的特殊情况。
    """
    now = datetime.now(TRADING_TZ)
    errors: list[str] = []
    account = account_context(client, errors)
    held = {position["stock_code"]: position for position in account["positions"]}
    groups = []
    for sector in watchlist.get("sectors") or []:
        stocks = [pick for pick in sector.get("picks") or [] if pick["stock_code"] not in held]
        if stocks:
            groups.append({"name": sector["sector"], "kind": "candidate", "note": sector.get("reason", ""), "stocks": stocks})
    if held:
        groups.append(
            {
                "name": "当前持仓",
                "kind": "holding",
                "note": "持仓票的唯一判断入口；卖出必须看 can_use_volume（T+1）。",
                "stocks": [{"stock_code": code} for code in held],
            }
        )
    if not groups:
        raise MarketDataError("本轮既没有候选也没有持仓，无可观测标的")
    codes = [stock["stock_code"] for group in groups for stock in group["stocks"]]
    metrics = _screen_metrics(client, codes, errors)
    daily, intraday = _bars(client, codes + index_codes, now, errors, index_codes=index_codes, journal_dir=journal_dir)
    for group in groups:
        for stock in group["stocks"]:
            code = stock["stock_code"]
            stock.update(metrics.get(code) or {})
            stock["position"] = held.get(code)
            stock["charts"] = _render_pair(
                chart_dir, code, daily.get(code) or [], intraday.get(code) or [], daily_days, errors
            )
    indexes = index_quotes(client, index_codes, errors)
    for index in indexes:
        code = index["stock_code"]
        index["charts"] = _render_pair(
            chart_dir, code, daily.get(code) or [], intraday.get(code) or [], daily_days, errors, average=False
        )
    return {
        "as_of": now.isoformat(timespec="seconds"),
        "trade_date": now.date().isoformat(),
        "indexes": indexes,
        "groups": groups,
        "account": account,
        "limits": host_limits(),
        "journal": read_account_journal(journal_dir)["data"],
        "errors": errors,
    }


def account_context(client: MiniQMTClient, errors: list[str]) -> dict[str, Any]:
    """账户快照、当日委托和成交。持仓量为 0 的记录是今日已清仓的票，不算持仓但要留在成交里。"""
    snapshot = client.account("snapshot")
    if not snapshot["ok"]:
        # 账户拿不到就不许继续：没有可用资金和可卖数量，任何交易判断都是猜的。
        raise MarketDataError(f"账户快照失败：{(snapshot.get('error') or {}).get('detail', '')}")
    result = snapshot["data"]["result"]
    asset = (result.get("assets") or {}).get("asset") or {}
    positions = [
        _position_view(item)
        for item in (result.get("positions") or {}).get("items") or []
        if float(item.get("volume") or 0) > 0
    ]
    # orders 每轮必查：它既是重复下单的判据（注入里带 order_remark，等于自己上一轮那笔的 client_intent_id），
    # 又带 traded_volume/status 直接回答"成没成"。trades 只是成交价明细。
    # 判据写成"能不能确定没有在途委托"，而不是"有没有在途委托"：查询失败、状态码不认识时判不准，
    # 一律按"可能还有在途、可能再冒新成交"处理，照查 trades——判不准就别省那一次查询。
    before = len(errors)
    orders = _order_views(client, "orders", errors)
    orders_failed = len(errors) > before
    maybe_pending = orders_failed or any(order["status"] not in TERMINAL_ORDER_STATUS for order in orders)
    trades_note = None
    if maybe_pending:
        trades = _order_views(client, "trades", errors)
    else:
        # 有委托但全部终态：本轮没刷成交明细。这不是取数故障，不进 errors（errors 是"数据残缺、判断打折"信号，
        # 混进来会持续投喂假的降级暗示）；单独用 trades_note 显式说明，别让空 trades 被读成"今天没成交"。
        trades = []
        if orders:
            trades_note = "本轮无未成交委托，未刷新成交明细；成交结果以 orders 的 status/traded_volume 为准。"
    return {
        "snapshot_at": snapshot["data"]["snapshot_at"],
        "asset": {
            "available_cash": asset.get("cash"),
            "total_asset": asset.get("total_asset"),
            "market_value": asset.get("market_value"),
            "frozen_cash": asset.get("frozen_cash"),
        },
        "positions": positions,
        "orders": orders,
        "trades": trades,
        "trades_note": trades_note,
    }


def index_quotes(client: MiniQMTClient, index_codes: list[str], errors: list[str]) -> list[dict[str, Any]]:
    """大盘指数行情。指数取不到不该打死整轮，但必须显式留痕。"""
    result = client.quotes(index_codes)
    if not result["ok"]:
        errors.append(f"指数行情失败：{(result.get('error') or {}).get('detail', '')}")
        return [{"stock_code": code, "error": "行情缺失"} for code in index_codes]
    quotes = []
    for code in index_codes:
        tick = (result["data"].get("ticks") or {}).get(code)
        if not isinstance(tick, dict):
            errors.append(f"指数 {code} 没有 tick")
            quotes.append({"stock_code": code, "error": "行情缺失"})
            continue
        last, close = tick.get("lastPrice"), tick.get("lastClose")
        quotes.append(
            {
                "stock_code": code,
                "last": last,
                "prev_close": close,
                "change_pct": round((last / close - 1) * 100, 2) if last and close else None,
                "amount": tick.get("amount"),
                "quote_at": tick.get("timetag"),
            }
        )
    return quotes


def _screen_metrics(client: MiniQMTClient, codes: list[str], errors: list[str]) -> dict[str, dict]:
    """逐批取实时行情加日线趋势字段。这些是工具算出的确定性事实，模型只能引用不能重判。"""
    metrics: dict[str, dict] = {}
    for start in range(0, len(codes), QUOTE_BATCH):
        batch = codes[start : start + QUOTE_BATCH]
        result = client.screen(stock_codes=batch, limit=len(batch), enrich_trend=True)
        if not result["ok"]:
            errors.append(f"行情与趋势取数失败（{'、'.join(batch)}）：{(result.get('error') or {}).get('detail', '')}")
            continue
        for row in result["data"]["rows"]:
            metrics[row["stock_code"]] = row
        for detail in result["data"].get("trend_errors") or []:
            errors.append(f"趋势字段：{detail}")
    missing = [code for code in codes if code not in metrics]
    if missing:
        errors.append(f"这些代码没有取到行情，可能停牌：{'、'.join(missing)}")
    return metrics


def _bars(
    client: MiniQMTClient,
    codes: list[str],
    now: datetime,
    errors: list[str],
    *,
    index_codes: Iterable[str] = (),
    journal_dir: str | Path,
) -> tuple[dict[str, list], dict[str, list]]:
    """取渲染用的日线和当日分钟线。日线窗口给足 90 天，保证 30 根图上的 MA20 从第一根就有值。

    日线历史（截至昨日）当日不变，走 akshare（东财）全天取一次、按代码缓存住，各轮只补没取过的
    代码——东财对单 IP 有突发限流，每轮把整份 watchlist 的日线重打一遍必被掐连（见 2026-09-23
    盘中日线整批缺失）。盘前 prefetch_daily 已把待观测清单的历史预热进缓存，盘中这里通常直接命中，
    只有持仓票这类不在清单里的代码才在这补取。当日那根 bar 不问东财，用 bridge 已经取到的当日
    1m 现合成接在历史后面。分钟线仍走 bridge：bigqmt 靠实时订阅回补当日 bar。
    """
    today = now.strftime("%Y%m%d")
    today_int = int(today)
    history = _ensure_daily_history(journal_dir, codes, now, errors, index_codes=index_codes)
    intraday: dict[str, list] = {}
    for begin in range(0, len(codes), QUOTE_BATCH):
        batch = codes[begin : begin + QUOTE_BATCH]
        result = client.history(batch, period="1m", start_time=today, end_time=today)
        if not result["ok"]:
            errors.append(f"1m K 线取数失败（{'、'.join(batch)}）：{(result.get('error') or {}).get('detail', '')}")
            continue
        intraday.update(result["data"]["bars"])
        for code in result["data"].get("empty_codes") or []:
            errors.append(f"{code} 没有 1m K 线数据")
    daily: dict[str, list] = {}
    for code in codes:
        prior = history.get(code) or []
        today_bar = _today_daily_bar(intraday.get(code) or [], today_int)
        daily[code] = prior + ([today_bar] if today_bar else [])
        if not daily[code]:
            errors.append(f"{code} 没有 1d K 线数据")
    return daily, intraday


def _ensure_daily_history(
    journal_dir: str | Path,
    codes: Iterable[str],
    now: datetime,
    errors: list[str],
    *,
    index_codes: Iterable[str] = (),
) -> dict[str, list[dict[str, Any]]]:
    """把这些代码「截至昨日」的日线历史取齐并持久化到当日缓存，返回 {code: rows}。

    日线一天只需取一次：命中缓存的直接复用，只对没取过的代码打东财。东财对单 IP 有突发限流，
    这是把请求数压到最低的关键（见记忆 akshare-eastmoney-ip-burst-rate-limit）。取空的代码
    不写进缓存，留给调用方（盘前多跑几遍、盘中下一轮）继续补，不把失败固化下来。
    """
    codes = list(codes)
    today_int = int(now.strftime("%Y%m%d"))
    start = (now - timedelta(days=DAILY_LOOKBACK_DAYS)).strftime("%Y%m%d")
    cache_path = _daily_cache_path(journal_dir, now.date().isoformat())
    history = _load_daily_cache(cache_path)
    index_set = {str(code) for code in index_codes}
    # 指数排在待取列表最前：批量尾部最容易撞上限流墙，别让大盘图老吃这一记。
    ordered = [code for code in codes if code in index_set] + [code for code in codes if code not in index_set]
    missing = [code for code in ordered if code not in history]
    if not missing:
        return history
    try:
        fetched = akshare_board.daily_history(missing, start, now.strftime("%Y%m%d"), index_codes=index_codes)
    except akshare_board.BoardDataError as exc:
        errors.append(f"日线取数失败（akshare）：{exc}")
        fetched = {}
    changed = False
    for code, rows in fetched.items():
        # 只把截至昨日的历史写进缓存；当日 bar 由 1m 合成，取空的代码不固化进缓存，留给下一遍继续补。
        prior = [bar for bar in rows if isinstance(bar.get("date"), int) and bar["date"] < today_int]
        if prior:
            history[code] = prior
            changed = True
    if changed:
        _save_daily_cache(cache_path, history)
    return history


def prefetch_daily(
    journal_dir: str | Path,
    codes: Iterable[str],
    now: datetime,
    errors: list[str],
    *,
    index_codes: Iterable[str] = (),
    passes: int = 4,
    pause_seconds: float = 3.0,
) -> list[str]:
    """盘前把待观测清单的日线历史取齐并持久化，返回最终仍缺的代码。

    日线和板块热度榜是同一类问题：一天只需取一次，盘前请求成功过一次就落盘，盘中各轮直接读缓存，
    不再每轮打东财撞限流。东财封控是间歇的（同一节奏时好时坏），所以对还没取到的代码多跑几遍、
    每遍之间歇一下，靠"多试几次"而不是"控速"绕过去。每遍中途失败不进 errors，只在耗尽后把最终
    还缺的代码报一次；这些留给盘中各轮继续补，不打死盘前。
    """
    codes = list(codes)
    missing = codes
    for attempt in range(passes):
        history = _ensure_daily_history(journal_dir, codes, now, [], index_codes=index_codes)
        missing = [code for code in codes if not history.get(code)]
        if not missing:
            break
        if attempt + 1 < passes:
            time.sleep(pause_seconds)
    if missing:
        errors.append(f"盘前日线预取仍缺 {len(missing)} 只（东财限流，盘中各轮会继续补）：{'、'.join(missing[:10])}")
    return missing


def _today_daily_bar(intraday_bars: list[dict], today_int: int) -> dict[str, Any] | None:
    """当日日线 bar 由 bridge 的当日 1m 现合成：日线历史缓存住不动，只有这根随盘中更新。

    open 取首根开、high/low 取全程极值、close 取末根收、volume/amount 累加。1m 与东财日线量纲
    同为手/元，接在历史后面画蜡烛不会错位。1m 还没有（开盘前或取空）就返回 None，日线图退回只画历史。
    """
    rows = charts.usable_bars(intraday_bars)
    if not rows:
        return None
    return {
        "date": today_int,
        "open": float(rows[0]["open"]),
        "high": max(float(row["high"]) for row in rows),
        "low": min(float(row["low"]) for row in rows),
        "close": float(rows[-1]["close"]),
        "volume": sum(float(row["volume"]) for row in rows),
        "amount": sum(float(row["amount"]) for row in rows if isinstance(row.get("amount"), (int, float))),
    }


def _daily_cache_path(journal_dir: str | Path, date_iso: str) -> Path:
    return Path(journal_dir).expanduser().resolve() / "daily_cache" / f"{date_iso}.json"


def _load_daily_cache(path: Path) -> dict[str, list[dict[str, Any]]]:
    """当日日线历史缓存 {code: [截至昨日的 rows]}；不存在或读坏就当空缓存重取，别让半截文件打死取数。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_daily_cache(path: Path, cache: dict[str, list[dict[str, Any]]]) -> None:
    """原子替换落盘：各轮会边跑边读，覆盖写会让下一轮读到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _render_pair(
    chart_dir: Path,
    code: str,
    daily_bars: list[dict],
    intraday_bars: list[dict],
    daily_days: int,
    errors: list[str],
    *,
    average: bool = True,
) -> dict[str, str]:
    """渲染一只标的的日线图和分钟图。缺图必须报出来：读图 Agent 看不到图只会瞎猜。"""
    result: dict[str, str] = {}
    prev_close = _prev_close(daily_bars)
    for kind, render in (
        ("daily", lambda path: charts.render_daily(path, code, daily_bars, days=daily_days)),
        (
            "intraday",
            lambda path: charts.render_intraday(path, code, intraday_bars, prev_close=prev_close, show_average=average),
        ),
    ):
        path = chart_dir / f"{code}-{kind}.png"
        try:
            result[kind] = str(render(path))
        except (charts.ChartDataMissing, OSError, ValueError) as error:
            errors.append(f"{code} {kind} 图渲染失败：{type(error).__name__}: {error}")
    return result


def _prev_close(daily_bars: list[dict]) -> float | None:
    """昨收取最后一根不是今天的日线；日线里的当日 bar 盘中是实时更新的，不能当昨收。"""
    usable = charts.usable_bars(daily_bars)
    if len(usable) < 2:
        return None
    today = usable[-1]["date"]
    prior = [bar for bar in usable if bar["date"] != today]
    return float(prior[-1]["close"]) if prior else None


def _position_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "stock_code": item.get("stock_code"),
        "name": item.get("instrument_name"),
        "volume": item.get("volume"),
        # can_use_volume 是券商侧算好的可卖量，今日买入的部分不在里面，这就是 T+1 的事实来源。
        "can_use_volume": item.get("can_use_volume"),
        "yesterday_volume": item.get("yesterday_volume"),
        "avg_price": round(float(item.get("avg_price") or 0), 3),
        "last_price": item.get("last_price"),
        "market_value": item.get("market_value"),
        "float_profit": item.get("float_profit"),
        "profit_rate": round(float(item.get("profit_rate") or 0) * 100, 2),
    }


def _order_views(client: MiniQMTClient, view: str, errors: list[str]) -> list[dict[str, Any]]:
    result = client.account(view)
    if not result["ok"]:
        errors.append(f"账户 {view} 查询失败：{(result.get('error') or {}).get('detail', '')}")
        return []
    items = (result["data"]["result"] or {}).get("items") or []
    if view == "orders":
        return [
            {
                "order_id": str(item.get("order_id")),
                "stock_code": item.get("stock_code"),
                "name": item.get("instrument_name"),
                "side": OFFSET_SIDE.get(item.get("offset_flag"), str(item.get("offset_flag"))),
                # order_remark 原样带回宿主下单时发下去的 client_intent_id：模型据此在注入里直接认出自己上一轮那笔委托，
                # 不用再单独调 miniqmt_account 去对账。
                "order_remark": item.get("order_remark"),
                "price": item.get("price"),
                "order_volume": item.get("order_volume"),
                "traded_volume": item.get("traded_volume"),
                "status": ORDER_STATUS.get(item.get("order_status"), str(item.get("order_status"))),
                "order_at": _epoch_text(item.get("order_time")),
            }
            for item in items
        ]
    return [
        {
            "stock_code": item.get("stock_code"),
            "name": item.get("instrument_name"),
            "side": OFFSET_SIDE.get(item.get("offset_flag"), str(item.get("offset_flag"))),
            "traded_price": item.get("traded_price"),
            "traded_volume": item.get("traded_volume"),
            "traded_amount": item.get("traded_amount"),
            "traded_at": _epoch_text(item.get("traded_time")),
        }
        for item in items
    ]


def _epoch_text(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return ""
    return datetime.fromtimestamp(float(value), TRADING_TZ).isoformat(timespec="seconds")


def block(data: Any) -> str:
    """注入 prompt 的数据一律用 JSON：字段名就是含义，不需要再解释表头。"""
    return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False, default=str)
