"""板块热度榜与成分股数据源：akshare 的新浪接口（stock_sector_spot / stock_sector_detail）。

大 QMT 撤掉极简接口后，native xtdata 的行情服务连不上，`get_sector_list` 枚举和申万/通达信
板块成分在 ZMQ 后端都取不到（只剩 ~13 个内置板块）。所以板块「有哪些、哪个热、里面有哪些票」
改由 akshare 取——这部分是纯本地 HTTP，不碰交易终端；成分股的实时 tick 与日线趋势
仍然走 ZMQ（get_full_tick / get_market_data 都正常）。

板块榜原先走东财 push2 clist，2026-10-14 整天对本机出口 IP 掐连接（握手后空响应，三个榜单接口
全挂，没有同源备路），盘前选池 10 次全废。改走新浪：板块表和成分表各一次请求、约 0.1~0.2s，
成分行带昨收/开高低/成交额，正好映射成 tick 形状。新浪没有板块资金流，也没有可与新浪板块名
对上号的第三方资金流，所以榜单不再带主力净流入，宁可缺这个维度也不拿对不上口径的数硬凑。

口径：
- 概念 → 新浪概念板块（label 形如 gn_*），定当日主线。
- 行业 → 新浪行业板块（label 形如 hangye_*），交叉验证主线背后有没有行业级强度。

热度排序沿用原策略：不按板块指数涨幅（会被买不起的龙头抬高），而是取板块成分里「可买票」的
中位涨幅——复用 miniqmt 的 `_screen_row` / `_sector_summary`，把成分行情映射成 tick 形状。
没有资金流可排序，下钻哪些板块改按新浪板块自身涨跌幅取前 N；N 比以前大（新浪请求便宜），
免得被买不起的龙头抬高的板块涨幅把真正可买的方向挡在下钻名单外。
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

# 单次请求先重试 3 次，板块仍失败时由 board_rank 在全局 5 分钟窗口内继续重试，
# 避免一次板块失败拖完全部时间。
RETRY_ATTEMPTS = 3
RETRY_SLEEP_SECONDS = 4.0
MEMBER_RETRY_WINDOW_SECONDS = 300.0
# 没有资金流排名，按新浪板块涨跌幅取前 N 个下钻拉成分。新浪一次成分请求约 0.1~0.2s，
# 每族 40 个合计十几秒，比只看前 15 个更不容易漏掉「板块均价被龙头拉高/拉低」的可买方向。
DEFAULT_SCAN_BOARDS = 40
# 日线历史统一取 90 天：MA20 从图上第一根就有值，趋势字段也只用尾部 20 根，多给无害。
DAILY_LOOKBACK_DAYS = 90
# 单只票日线当天的取数配额：所有调用方（盘前预热、盘中读图取数、趋势字段）共用这一份缓存和配额。
# 永远取不到的票（停牌/退市/新股无 90 天历史/代码写错）到顶就放弃，不再每轮白等一次重试还持续送请求。
MAX_DAILY_FETCH_ATTEMPTS = 8


class BoardDataError(RuntimeError):
    """板块数据取不到。盘前必须有主线板块，宁可这一轮明确失败也不拿残缺数据选池。"""


# 两族到新浪 stock_sector_spot 的 indicator 映射。
FAMILY_INDICATORS = {"概念": "概念", "行业": "行业"}


def _retry(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """公开行情接口会随机断连，重试几次；用尽还失败就抛，让调用方把原因带进 errors。"""
    last: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # akshare 抛的是 requests/解析异常，统一按可重试处理
            last = exc
            # 每次失败立即追加，之后重试成功或进程退出也不会丢失现场；只记录公开行情参数。
            now = datetime.now(TRADING_TZ)
            record = {
                "at": now.isoformat(timespec="seconds"), "function": getattr(fn, "__name__", type(fn).__name__),
                "symbol": kwargs.get("symbol") or kwargs.get("sector") or kwargs.get("indicator"), "attempt": attempt + 1,
                "max_attempts": RETRY_ATTEMPTS,
                "error_type": type(exc).__name__, "error": str(exc),
            }
            try:
                path = Path(".sessions/diagnostics") / f"akshare-retries-{now.date().isoformat()}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as log_error:
                logger.warning("行情重试诊断落盘失败：%s；原始异常：%s", log_error, record)
            if attempt + 1 < RETRY_ATTEMPTS:
                time.sleep(RETRY_SLEEP_SECONDS)
    raise BoardDataError(f"akshare {getattr(fn, '__name__', fn)} 失败：{type(last).__name__}: {last}") from last


def _to_qmt_code(raw: Any) -> str | None:
    """6 位纯代码补 .SH/.SZ 后缀成 QMT/ZMQ 认的格式；非沪深 A 股返回 None。"""
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


def _board_list(family: str) -> list[tuple[str, str]]:
    """取一族的板块表，返回 [(label, 板块名)]，按新浪板块自身涨跌幅从高到低。

    label 是新浪成分接口的入参。涨跌幅缺失（NaN）的板块排在最后，不当作 0%。
    """
    ak = _load_ak()
    frame = _retry(ak.stock_sector_spot, indicator=FAMILY_INDICATORS[family])
    rows = []
    for _, row in frame.iterrows():
        label, name = str(row.get("label") or "").strip(), str(row.get("板块") or "").strip()
        if label and name:
            change = _num(row.get("涨跌幅"))
            rows.append((change if change is not None else float("-inf"), label, name))
    rows.sort(key=lambda item: item[0], reverse=True)
    return [(label, name) for _, label, name in rows]


def _cons_ticks(label: str) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """取一个板块的成分股，映射成 miniqmt tick 形状（复用 _screen_row / _sector_summary）。"""
    ak = _load_ak()
    frame = _retry(ak.stock_sector_detail, sector=label)
    codes: list[str] = []
    quotes: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        code = _to_qmt_code(row.get("code"))
        if code is None or code in quotes:
            continue
        codes.append(code)
        shares = _num(row.get("volume"))
        quotes[code] = {
            "lastPrice": row.get("trade"),
            "lastClose": row.get("settlement"),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            # 新浪成交量是股，tick 契约里的 volume 是手，和下游其他来源保持同一量纲。
            "volume": shares / LOT_SIZE if shares is not None else None,
            "amount": row.get("amount"),
        }
    return codes, quotes


def board_rank(
    family: str,
    *,
    limit: int = 12,
    min_buyable: int = 3,
    max_buy_notional: float,
    scan_boards: int = DEFAULT_SCAN_BOARDS,
    retry_until: float | None = None,
) -> dict[str, Any]:
    """板块热度榜：新浪板块行情取涨幅靠前的若干板块下钻，算「可买中位涨幅」。

    排序沿用原策略——按可买票中位涨幅排（不被买不起的龙头抬高）。新浪没有板块资金流，下钻名单只能按
    板块自身涨跌幅取前 scan_boards 个，把成分请求数压在可控范围内。
    """
    if family not in FAMILY_INDICATORS:
        raise BoardDataError(f"family 只能是 {'、'.join(FAMILY_INDICATORS)}")
    boards = _board_list(family)
    ranked: list[dict[str, Any]] = []
    failed: list[str] = []
    # 失败板块继续保留简短名称供兼容；原始异常另存，便于事后区分超时、断连、限流和解析错误。
    failed_details: dict[str, str] = {}
    pending = boards[:scan_boards]
    retry_until = retry_until if retry_until is not None else time.monotonic() + MEMBER_RETRY_WINDOW_SECONDS
    while pending:
        next_pending: list[tuple[str, str]] = []
        for label, name in pending:
            try:
                codes, quotes = _cons_ticks(label)
            except BoardDataError as exc:
                failed_details[name] = f"{name}: {exc}"
                next_pending.append((label, name))
                continue
            summary = _sector_summary(name, codes, quotes, max_buy_notional)
            if not summary or summary["members_quoted"] < SECTOR_RANK_MIN_MEMBERS or summary["buyable_count"] < min_buyable:
                continue
            summary["member_codes"] = [code for code in codes if STOCK_CODE_PATTERN.match(code)]
            ranked.append(summary)
        if not next_pending or time.monotonic() >= retry_until:
            failed = [name for _, name in next_pending]
            break
        pending = next_pending
        time.sleep(RETRY_SLEEP_SECONDS)
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
        "member_fetch_errors": [failed_details[name] for name in failed[:10]],
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


# 新浪分钟成交量是「股」，东财日线成交量是「手」（100 股），vol_ratio 拿分钟合成的当日量比历史日线量，
# 两边单位必须一致，所以分钟量除以 100 折成手。成交额两边都是元，不动。
_SINA_MINUTE_VOLUME_PER_LOT = 100


def _minute_rows(frame: Any, day_int: int) -> list[dict[str, Any]]:
    """新浪分钟表 → bridge 1m bar 形状，只留 trade_date 当天：{date:int14, open, high, low, close, volume(手), amount}。"""
    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        compact = str(row.get("day")).replace("-", "").replace(":", "").replace(" ", "")
        if len(compact) != 14 or not compact.isdigit() or int(compact[:8]) != day_int:
            continue
        volume = _num(row.get("volume"))
        rows.append(
            {
                "date": int(compact),
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
                "close": _num(row.get("close")),
                "volume": volume / _SINA_MINUTE_VOLUME_PER_LOT if volume is not None else None,
                "amount": _num(row.get("amount")),
            }
        )
    return rows


def minute_history(codes: Iterable[str], trade_date: Any) -> dict[str, list[dict]]:
    """某历史交易日的当日 1m 分钟线，形状对齐 bridge：只给回测用。

    bridge 只留当日 1m、历史日期取不到，所以回测的分钟线从新浪 stock_zh_a_minute 回补——它个股和
    指数同一个接口都认（symbol 走 sh/sz+6 位），返回最近约 8 个交易日的 1m，够回测最近的日子。
    单只票拿不到（超出新浪窗口/停牌）给空列表，由调用方按缺图留痕。
    """
    ak = _load_ak()
    day_int = int(trade_date.strftime("%Y%m%d"))
    out: dict[str, list[dict]] = {}
    for code in codes:
        try:
            frame = _retry(ak.stock_zh_a_minute, symbol=f"{code[-2:].lower()}{code[:6]}", period="1", adjust="")
        except BoardDataError:
            out[code] = []
            continue
        out[code] = _minute_rows(frame, day_int)
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
