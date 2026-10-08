"""盘前板块热度榜历史补全：给 mock 环境造最近 N 个交易日的板块榜。

实盘的 board_rank 是实时快照，过了那天就复现不了。这里用三类数据重建，口径对齐实盘：
- 板块选择 + 资金流：东财。板块用 N 日资金流排名一次定（每族 2 次调用），资金流明细走历史
  资金流接口按板块补（push2his，akshare 自带的 hist 包装器开头要查实时 clist，实时服务器一抽风
  整个函数就废，所以这里绕开它直接打 push2his）。
- 成分名单：东财 cons_em。bridge 撤极简接口后取不到板块成分，这步只能走东财。
- 成分日线：bridge（QMT history），不走东财，省调用、躲限流——日线是这里调用量最大的一块。

change_pct / 可买中位涨幅 / up_ratio 全部复用 miniqmt._sector_summary，不另写口径。

东财对单 IP 有突发限流（见项目记忆）：每类调用串行 + sleep + 退避，连续失败到顶即停，不硬刚；
各阶段落盘缓存，被限流打断后重跑自动从断点续。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from minisweagent.environments import akshare_board
from minisweagent.environments.miniqmt import (
    HISTORY_CODE_LIMIT,
    STOCK_CODE_PATTERN,
    TRADING_TZ,
    MiniQMTClient,
    _sector_summary,
    host_limits,
)

# 两族到东财接口的映射：板块列表（带板块代码）、成分股、资金流 sector_type。
FAMILY_LIST_FUNC = {"概念": "stock_board_concept_name_em", "行业": "stock_board_industry_name_em"}
FAMILY_CONS_FUNC = {"概念": "stock_board_concept_cons_em", "行业": "stock_board_industry_cons_em"}
FAMILY_FLOW_TYPE = {"概念": "概念资金流", "行业": "行业资金流"}

EASTMONEY_SLEEP = 1.8          # 每次东财调用后固定间隔，躲突发限流
EASTMONEY_RETRY = 4           # 单次调用的重试次数
MAX_CONSEC_FAIL = 6           # 连续失败到顶就停：继续喂请求只会把限流窗口拖得更长

_consec_fail = {"n": 0}


class BackfillError(RuntimeError):
    """补全凑不齐数据。宁可明确失败，也不要拿残缺板块榜喂给 mock。"""


# ---- 节流与缓存 ------------------------------------------------------------

def _sleep_throttle() -> None:
    time.sleep(EASTMONEY_SLEEP + random.uniform(0, 0.6))


def _ak_call(fn_name: str, **kwargs: Any) -> Any:
    """东财 akshare 调用：重试 + 退避 + 调用后固定 sleep；连续失败到顶直接抛，不硬刚限流。"""
    import akshare as ak

    func = getattr(ak, fn_name)
    last: Exception | None = None
    for attempt in range(EASTMONEY_RETRY):
        try:
            out = func(**kwargs)
            _consec_fail["n"] = 0
            _sleep_throttle()
            return out
        except Exception as exc:  # akshare 抛的是 requests/解析异常，统一按可重试处理
            last = exc
            time.sleep(EASTMONEY_SLEEP * (attempt + 1))
    _consec_fail["n"] += 1
    if _consec_fail["n"] >= MAX_CONSEC_FAIL:
        raise BackfillError(f"东财连续 {MAX_CONSEC_FAIL} 次失败，疑似 IP 限流，停止补全：{type(last).__name__}: {last}")
    raise BackfillError(f"东财 {fn_name} 失败：{type(last).__name__}: {last}")


def _http_json(url: str) -> dict[str, Any]:
    """直连 push2his 历史接口：同样串行节流 + 退避，走统一的连续失败熔断。"""
    last: Exception | None = None
    for attempt in range(EASTMONEY_RETRY):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                out = json.load(resp)
            _consec_fail["n"] = 0
            _sleep_throttle()
            return out
        except Exception as exc:
            last = exc
            time.sleep(EASTMONEY_SLEEP * (attempt + 1))
    _consec_fail["n"] += 1
    if _consec_fail["n"] >= MAX_CONSEC_FAIL:
        raise BackfillError(f"push2his 连续 {MAX_CONSEC_FAIL} 次失败，疑似 IP 限流，停止补全：{type(last).__name__}: {last}")
    raise BackfillError(f"push2his 请求失败：{type(last).__name__}: {last}")


def _cache_path(root: Path, name: str) -> Path:
    return root / "_cache" / name


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".bf-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=1, default=str)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _iso(day_int: int) -> str:
    s = str(day_int)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


# ---- 东财：板块选择、成分、资金流（各自落盘，可断点续）------------------------

def _board_codemap(family: str, root: Path) -> dict[str, str]:
    """板块名 -> 东财板块代码（BKxxxx）。板块列表一天一次，缓存住。"""
    cache = _cache_path(root, f"codemap_{family}.json")
    saved = _load(cache)
    if isinstance(saved, dict) and saved:
        return saved
    frame = _ak_call(FAMILY_LIST_FUNC[family])
    codemap: dict[str, str] = {}
    for _, row in frame.iterrows():
        name = str(row.get("板块名称") or "").strip()
        code = str(row.get("板块代码") or "").strip()
        if name and code:
            codemap[name] = code
    if not codemap:
        raise BackfillError(f"{family} 板块列表为空，拿不到板块代码")
    _save(cache, codemap)
    return codemap


def _select_boards(family: str, top_n: int, root: Path) -> list[str]:
    """按 N 日资金流排名取前 top_n 个板块名；东财该接口已按主力净流入排好序。"""
    cache = _cache_path(root, f"selected_{family}.json")
    saved = _load(cache)
    if isinstance(saved, list) and saved:
        return saved
    frame = _ak_call("stock_sector_fund_flow_rank", indicator="10日", sector_type=FAMILY_FLOW_TYPE[family])
    names = [str(row.get("名称") or "").strip() for _, row in frame.iterrows()]
    picked = [name for name in names if name][:top_n]
    if not picked:
        raise BackfillError(f"{family} 资金流排名为空，选不出板块")
    _save(cache, picked)
    return picked


def _constituents(family: str, name: str, root: Path) -> list[str]:
    """一个板块的成分代码（补 .SH/.SZ、剔北交所）。只要代码，日线另走 bridge。"""
    cache = _cache_path(root, f"cons_{family}_{name}.json")
    saved = _load(cache)
    if isinstance(saved, list):
        return saved
    frame = _ak_call(FAMILY_CONS_FUNC[family], symbol=name)
    codes: list[str] = []
    for _, row in frame.iterrows():
        code = akshare_board._to_qmt_code(row.get("代码"))
        if code and code not in codes:
            codes.append(code)
    _save(cache, codes)
    return codes


def _flow_hist(secid: str, root: Path) -> dict[str, dict[str, float]]:
    """板块历史每日主力资金：push2his 一次返回全历史，缓存住。返回 {YYYY-MM-DD: {净额亿, 净占比}}。"""
    cache = _cache_path(root, f"fflow_{secid}.json")
    saved = _load(cache)
    if isinstance(saved, dict):
        return saved
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
        "lmt=0&klt=101&fields1=f1,f2,f3,f7&fields2=f51,f52,f57&secid=" + secid
    )
    data = _http_json(url)
    out: dict[str, dict[str, float]] = {}
    for line in (data.get("data") or {}).get("klines") or []:
        cells = line.split(",")
        if len(cells) < 3:
            continue
        out[cells[0]] = {
            "main_net_inflow_yi": round(float(cells[1]) / 1e8, 2),
            "main_net_inflow_pct": round(float(cells[2]), 2),
        }
    _save(cache, out)
    return out


# ---- bridge：交易日历与成分日线（不碰东财）----------------------------------

def _trading_days(client: MiniQMTClient, ref_date: datetime, days: int) -> list[int]:
    """用指数日线定交易日历：bar 的日期就是交易日，取窗口末尾 days 个。"""
    start = (ref_date - timedelta(days=days * 2 + 25)).strftime("%Y%m%d")
    end = ref_date.strftime("%Y%m%d")
    result = client.history(["000001.SH"], period="1d", start_time=start, end_time=end)
    if not result["ok"]:
        raise BackfillError(f"交易日历取数失败：{(result.get('error') or {}).get('detail', '')}")
    bars = (result["data"]["bars"] or {}).get("000001.SH") or []
    all_days = sorted({int(str(bar["date"])[:8]) for bar in bars})
    if len(all_days) < days:
        raise BackfillError(f"指数日线只有 {len(all_days)} 个交易日，不足 {days} 个")
    return all_days[-days:]


def _daily_bars(client: MiniQMTClient, codes: list[str], days: list[int], errors: list[str]) -> dict[str, list[dict]]:
    """批量取成分日线：窗口往前多留 12 天保证首个交易日也有昨收。date 归一成 8 位 int。"""
    start = (datetime.strptime(str(days[0]), "%Y%m%d") - timedelta(days=12)).strftime("%Y%m%d")
    end = str(days[-1])
    bars: dict[str, list[dict]] = {}
    for begin in range(0, len(codes), HISTORY_CODE_LIMIT):
        batch = codes[begin : begin + HISTORY_CODE_LIMIT]
        result = client.history(batch, period="1d", start_time=start, end_time=end)
        if not result["ok"]:
            errors.append(f"日线取数失败（{'、'.join(batch)}）：{(result.get('error') or {}).get('detail', '')}")
            continue
        for code, rows in (result["data"]["bars"] or {}).items():
            bars[code] = [{**row, "date": int(str(row["date"])[:8])} for row in rows]
    return bars


def _bar_on(rows: list[dict], day: int) -> dict | None:
    for row in rows:
        if row["date"] == day:
            return row
    return None


def _prev_close(rows: list[dict], day: int) -> float | None:
    prior = [row for row in rows if row["date"] < day and isinstance(row.get("close"), (int, float)) and row["close"]]
    return float(prior[-1]["close"]) if prior else None


# ---- 单日板块榜：复用实盘 _sector_summary 口径 ------------------------------

def _rank_day(
    day: int, meta: dict[str, dict], cons: dict[str, list[str]], daily: dict[str, list[dict]],
    flows: dict[str, dict], max_buy_notional: float, limit: int,
) -> list[dict]:
    rows: list[dict] = []
    for name in meta:
        codes = cons.get(name) or []
        quotes: dict[str, dict] = {}
        for code in codes:
            bar = _bar_on(daily.get(code) or [], day)
            prev = _prev_close(daily.get(code) or [], day)
            if bar is None or prev is None:
                continue
            quotes[code] = {
                "lastPrice": bar.get("close"), "lastClose": prev, "open": bar.get("open"),
                "high": bar.get("high"), "low": bar.get("low"),
                "volume": bar.get("volume"), "amount": bar.get("amount"),
            }
        summary = _sector_summary(name, [c for c in codes if c in quotes], quotes, max_buy_notional)
        if not summary:
            continue
        flow = (flows.get(name) or {}).get(_iso(day)) or {}
        summary["main_net_inflow_yi"] = flow.get("main_net_inflow_yi")
        summary["main_net_inflow_pct"] = flow.get("main_net_inflow_pct")
        summary["member_codes"] = [c for c in codes if STOCK_CODE_PATTERN.match(c)]
        rows.append(summary)
    # fund_rank：当日这些板块按主力净流入自排名次；缺资金流的排在已知值之后。
    ranked = sorted(
        rows, key=lambda r: r["main_net_inflow_yi"] if r.get("main_net_inflow_yi") is not None else float("-inf"),
        reverse=True,
    )
    for pos, row in enumerate(ranked, start=1):
        row["fund_rank"] = pos if row.get("main_net_inflow_yi") is not None else None
    rows.sort(key=lambda r: r["buyable_median_change_pct"], reverse=True)
    return rows[:limit]


# ---- 编排 ------------------------------------------------------------------

def _client_from_env() -> MiniQMTClient:
    url = os.environ.get("MINIQMT_BRIDGE_URL", "").strip()
    if not url:
        raise BackfillError("缺少 MINIQMT_BRIDGE_URL，无法连 bridge 取日线")
    return MiniQMTClient(base_url=url, timeout=30, mode="observe")


def backfill(
    journal_dir: str | Path, ref_date: datetime, *, days: int = 10,
    families: tuple[str, ...] = ("概念", "行业"), top_n: int = 15, limit: int = 12,
) -> dict[str, Any]:
    """重建最近 days 个交易日的板块榜，逐日落盘到 journal_dir/sector_backfill/<date>.json。"""
    root = Path(journal_dir).expanduser().resolve() / "sector_backfill"
    errors: list[str] = []
    client = _client_from_env()
    trade_days = _trading_days(client, ref_date, days)
    max_buy_notional = host_limits()["max_buy_notional"]
    # 阶段一：东财定板块、取成分与资金流（可能被限流打断，重跑续）。
    meta: dict[str, dict[str, dict]] = {}
    cons: dict[str, dict[str, list[str]]] = {}
    flows: dict[str, dict[str, dict]] = {}
    for family in families:
        codemap = _board_codemap(family, root)
        names = [name for name in _select_boards(family, top_n, root) if name in codemap]
        meta[family] = {name: {"code": codemap[name], "secid": f"90.{codemap[name]}"} for name in names}
        cons[family] = {name: _constituents(family, name, root) for name in meta[family]}
        flows[family] = {name: _flow_hist(meta[family][name]["secid"], root) for name in meta[family]}
    # 阶段二：bridge 取全部成分日线（量最大，不碰东财）。
    all_codes = sorted({code for family in families for codes in cons[family].values() for code in codes})
    daily = _daily_bars(client, all_codes, trade_days, errors)
    # 阶段三：逐日按实盘口径算榜并落盘。
    for day in trade_days:
        out = {
            "trade_date": _iso(day),
            "families": {
                family: _rank_day(day, meta[family], cons[family], daily, flows[family], max_buy_notional, limit)
                for family in families
            },
        }
        _save(root / f"{_iso(day)}.json", out)
    _save(root / "_errors.json", {"ref_date": ref_date.date().isoformat(), "trade_days": trade_days, "errors": errors})
    return {"root": str(root), "trade_days": trade_days, "codes": len(all_codes), "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description="补全最近 N 个交易日的盘前板块热度榜（给 mock 用）")
    parser.add_argument("--journal-dir", default=".sessions/account-manager")
    parser.add_argument("--ref-date", default=datetime.now(TRADING_TZ).strftime("%Y%m%d"), help="窗口结束日 YYYYMMDD")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--top-n", type=int, default=15)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    ref = datetime.strptime(args.ref_date, "%Y%m%d").replace(tzinfo=TRADING_TZ)
    summary = backfill(args.journal_dir, ref, days=args.days, top_n=args.top_n, limit=args.limit)
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
