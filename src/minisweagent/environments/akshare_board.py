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

import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
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
# 日线历史统一取 90 天：MA20 从图上第一根就有值，趋势字段也只用尾部 20 根，多给无害。
DAILY_LOOKBACK_DAYS = 90
# 单只票日线当天的取数配额：所有调用方（盘前预热、盘中读图取数、趋势字段）共用这一份缓存和配额。
# 永远取不到的票（停牌/退市/新股无 90 天历史/代码写错）到顶就放弃，不再每轮白等一次重试还持续送请求。
MAX_DAILY_FETCH_ATTEMPTS = 8


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


def _date_int(value: Any) -> int | None:
    """东财日期列（'2026-09-21' 或 date）统一成 int(YYYYMMDD)：charts 和 _trend_metrics 对 date 用整数。"""
    text = str(value).strip()[:10].replace("-", "")
    return int(text) if len(text) == 8 and text.isdigit() else None


def _num(value: Any) -> float | None:
    """转 float，NaN/inf/非数一律给 None：残缺 bar 由 usable_bars 整根丢掉，不能混进均线。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# 东财日线列名 → bars 行字段。新浪 stock_zh_index_daily 本来就是这套英文名，所以只在东财侧改名，
# 让 _daily_rows 只有一套列名要认——两套列名映射写进同一个循环，下次加通道就是第三个分支。
_EM_DAILY_RENAME = {
    "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
    "收盘": "close", "成交量": "volume", "成交额": "amount",
}


def _daily_rows(frame: Any) -> list[dict[str, Any]]:
    """日线表映射成 client.history 的 bars 行形状；列名已在取数处统一成英文。

    新浪指数表没有成交额这一列，row.get 给 None → amount 为 None。charts.BAR_FIELDS 不含 amount，
    usable_bars 不会因此丢行，日线图也不画成交额，所以缺就缺，绝不补零。
    """
    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        date = _date_int(row.get("date"))
        if date is None:
            continue
        rows.append(
            {
                "date": date,
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
                "close": _num(row.get("close")),
                "volume": _num(row.get("volume")),
                "amount": _num(row.get("amount")),
            }
        )
    return rows


def daily_history(
    codes: list[str], now: datetime, *, cache_dir: str | Path, index_codes: Iterable[str] = (), spacing: float = 0.0
) -> dict[str, list[dict[str, Any]]]:
    """截至 now 前一日的日线历史，返回 {code: [rows]}，行形状与 client.history 的 bars 完全对齐。

    缓存下沉在这一层，`(date, code)` 命中：`_bars`（读图取数）和 `_enrich_trend`（趋势字段）两条
    路径共享同一份当日缓存，同一只票同一天最多打东财 MAX_DAILY_FETCH_ATTEMPTS 次，之后放弃。以前
    `_enrich_trend` 每轮对所有票直连东财，一天几百个请求撞限流，就是因为缓存待在 context 里够不到它。
    缓存只存「截至昨日」的行（当日 bar 是盘中实时变动的，`_bars` 自己用 1m 合成、`_enrich_trend` 用
    tick 当今日事实），所以当天不变、可反复读。取空的代码累加失败次数、到顶放弃，都记进缓存。
    start/end 由 now 内部按 90 天算；需要更短窗口的调用方自己切片（趋势字段只用尾部 20 根，无需切）。
    index_codes 里的代码走新浪 stock_zh_index_daily，其余走东财 stock_zh_a_hist；spacing 拉大同批内两个请求的间隔。
    未安装 akshare 这类确定性故障会抛 BoardDataError（不计入配额），由调用方决定怎么留痕。
    """
    codes = list(codes)
    today_int = int(now.strftime("%Y%m%d"))
    cache_path = _daily_cache_path(cache_dir, now.date().isoformat())
    history, failed = _load_daily_cache(cache_path)
    index_set = {str(code) for code in index_codes}
    missing = [code for code in codes if code not in history and failed.get(code, 0) < MAX_DAILY_FETCH_ATTEMPTS]
    if missing:
        start = (now - timedelta(days=DAILY_LOOKBACK_DAYS)).strftime("%Y%m%d")
        fetched = _fetch_daily(missing, start, now.strftime("%Y%m%d"), index_set=index_set, spacing=spacing)
        for code in missing:
            # 只把截至昨日的历史写进缓存；取到就清失败计数，取空就 +1（到顶后不再进 missing，不会无限重试）。
            prior = [bar for bar in fetched.get(code) or [] if isinstance(bar.get("date"), int) and bar["date"] < today_int]
            if prior:
                history[code] = prior
                failed.pop(code, None)
            else:
                failed[code] = failed.get(code, 0) + 1
        _save_daily_cache(cache_path, history, failed)
    return {code: history.get(code, []) for code in codes}


def daily_cache_missing(codes: Iterable[str], now: datetime, *, cache_dir: str | Path) -> tuple[list[str], list[str]]:
    """返回 (retryable, abandoned)：当天还没缓存到、且配额未满/已满的代码。

    盘前预热用它区分"还有配额，盘中会继续补"和"已取满放弃，盘中不再试"——后者说"继续补"会把排查带偏。
    """
    history, failed = _load_daily_cache(_daily_cache_path(cache_dir, now.date().isoformat()))
    retryable, abandoned = [], []
    for code in codes:
        if code in history:
            continue
        (abandoned if failed.get(code, 0) >= MAX_DAILY_FETCH_ATTEMPTS else retryable).append(code)
    return retryable, abandoned


def _fetch_daily(
    codes: list[str], start_time: str, end_time: str, *, index_set: set[str], spacing: float = 0.0
) -> dict[str, list[dict[str, Any]]]:
    """逐个 code 取日线原始行，返回 {code: rows}（未按当日过滤）。取不到的 code 给空列表。

    指数走新浪 stock_zh_index_daily，个股走东财 stock_zh_a_hist。东财的 index_zh_a_hist 对
    000001.SH/399006.SZ 是稳定挂的（每次远端直接掐连接、重试无效），一天 8 次配额全废、大盘图
    永远缺日线那半张；新浪一把到昨日，还不占东财那个会突发限流的 IP 配额。
    个股不复权：趋势判定和图都用原始价，复权后昨收对不上实时 tick。spacing>0 时两个请求之间歇一下。
    新浪不认 start/end，返回 1990 年至今全历史（上证 8700+ 行），所以窗口截断放在出口统一做——
    对东财是 no-op，比给指数单开一条过滤路径少一个特例，也免得把全历史灌进当日缓存。
    """
    ak = _load_ak()
    start_int, end_int = int(start_time), int(end_time)
    out: dict[str, list[dict[str, Any]]] = {}
    for position, code in enumerate(codes):
        if position and spacing:
            time.sleep(spacing)
        try:
            if code in index_set:
                # 000001.SH → sh000001、399006.SZ → sz399006
                frame = _retry(ak.stock_zh_index_daily, symbol=f"{code[-2:].lower()}{code[:6]}")
                # 新浪指数 volume 的单位是股，bridge 的 1m（拿来合成当日 bar）是手，差 100 倍。不换算，
                # 日线量能柱上 30 根历史和当日那根就差两个数量级，当日量永远看着是零，读图会读成"大盘量塌了"。
                frame["volume"] = frame["volume"] / LOT_SIZE
            else:
                frame = _retry(
                    ak.stock_zh_a_hist,
                    symbol=code[:6],  # 东财只认 6 位纯代码
                    period="daily",
                    start_date=start_time,
                    end_date=end_time,
                    adjust="",
                ).rename(columns=_EM_DAILY_RENAME)
        except BoardDataError:
            out[code] = []
            continue
        out[code] = [row for row in _daily_rows(frame) if start_int <= row["date"] <= end_int]
    return out


def _daily_cache_path(cache_dir: str | Path, date_iso: str) -> Path:
    return Path(cache_dir).expanduser().resolve() / "daily_cache" / f"{date_iso}.json"


def _load_daily_cache(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    """当日日线缓存 {"history": {code: rows}, "failed": {code: 次数}}；读不到或形状不对就当空缓存重取。

    形状必须严格校验：json.loads("[]") 会成功返回 list，随后 history.get(code) 直接 AttributeError 打死
    整轮取数。缓存是当天的临时文件，格式变了就当空的重取，不写任何读旧格式的兼容代码。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(raw, dict):
        return {}, {}
    history, failed = raw.get("history"), raw.get("failed")
    if not isinstance(history, dict) or not isinstance(failed, dict):
        return {}, {}
    return history, failed


def _save_daily_cache(path: Path, history: dict[str, list[dict[str, Any]]], failed: dict[str, int]) -> None:
    """原子替换落盘：各轮会边跑边读，覆盖写会让下一轮读到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"history": history, "failed": failed}, handle, ensure_ascii=False)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
