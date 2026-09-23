"""受限的 MiniQMT HTTP 工具。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from minisweagent.environments.account_journal import append_trade_audit

MAX_RESPONSE_BYTES = 1_000_000
MAX_ERROR_BODY_CHARS = 400
STOCK_CODE_PATTERN = re.compile(r"^(?:[036]\d{5})\.(?:SH|SZ)$")
TRADING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
# 账户只开通了沪深主板和创业板；科创板要单独签风险协议，买入到券商侧才会被拒，不如在本地就拦住。
# 只拦买入：万一历史上持有过科创板股票，卖出路径必须留着。
BUY_BLOCKED_PREFIXES = ("688", "689")
LOT_SIZE = 100
QUOTE_BATCH_SIZE = 1000
SCREEN_UNIVERSE_LIMIT = 6000
HISTORY_CODE_LIMIT = 20
HISTORY_PERIODS = ("1d", "5m", "1m")
# 排序键与是否倒序。close_position 是"收在当日振幅哪个位置"，收盘贴最高价才是强势；
# 只靠涨幅榜挑趋势票等于用体温计测血压，涨幅榜给的永远是今天已经涨完的。
SCREEN_SORTS = {
    "change_pct_desc": (lambda row: row["change_pct"], True),
    "change_pct_asc": (lambda row: row["change_pct"], False),
    "amount_desc": (lambda row: row["amount"], True),
    "close_position_desc": (lambda row: row["close_position"] if row["close_position"] is not None else -1.0, True),
}
# 趋势字段必须读日线，一次 history 最多 20 只，所以带 enrich_trend 的筛选强制收窄到 20 只。
TREND_ENRICH_LIMIT = HISTORY_CODE_LIMIT
TREND_MIN_BARS = 21
# A 股一个交易日 240 分钟。日线里的当日 bar 是盘中实时更新的，10:00 只有 30 分钟成交量，
# 直接和全天基准量比会让 vol_ratio 系统性偏低八倍，盘中永远扫不出放量突破。
TRADING_MINUTES_PER_DAY = 240
TRADING_MINUTES_FLOOR = 15
# 突破买点区间取自 Minervini 式 buy-stop-limit：向上穿越 pivot 才算突破，
# 高于 pivot 2% 就是跳空追高，宁可放弃这一次。止损参考给 10 日结构低下方 1%。
BREAKOUT_TRIGGER_RATIO = 1.001
BREAKOUT_CEILING_RATIO = 1.02
STOP_REF_RATIO = 0.99
# 板块族前缀。短线资金炒的是概念（TGN），申万二级（SW2）用来交叉验证这个概念背后有没有行业级资金。
# 涨幅榜挑不出候选：榜首要么涨停封死买不进，要么一手成本就超过单笔上限。板块热度才是入口。
SECTOR_FAMILIES = ("TGN", "THY", "SW1", "SW2")
SECTOR_MEMBER_CACHE = "sector-members.json"
MARKET_UNIVERSE_SECTOR = "沪深A股"
SECTOR_RANK_MIN_MEMBERS = 5
# 申万每个行业都有一个"加权"孪生板块，成分股完全相同，留着只会让热度榜一半是重复项。
SECTOR_NAME_SUFFIX_SKIP = ("加权",)
logger = logging.getLogger("minisweagent.miniqmt")
_PROJECT_ENV_VALUES: dict[str, str] = {}
_PROJECT_ENV_VALUES: dict[str, str] = {}


class MiniQMTClient:
    """只连接宿主配置的 MiniQMT Bridge；模型不能指定地址、账户或凭据。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        mode: str = "observe",
        state_dir: str | Path = ".sessions/account-manager",
        cycle_id: str = "manual",
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("MINIQMT_BRIDGE_URL 必须是无路径、无凭据的 http 或 https 地址")
        if mode not in {"observe", "execute", "auto_execute"}:
            raise ValueError("MINIQMT_AGENT_MODE 只能是 observe、execute 或 auto_execute")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.mode = mode
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.cycle_id = cycle_id
        self._account_ready = False
        self._opener = build_opener(ProxyHandler({}))

    def quotes(self, stock_codes: list[str]) -> dict[str, Any]:
        if not 1 <= len(stock_codes) <= 20:
            return _error("invalid_argument", "stock_codes 必须包含 1 到 20 项")
        try:
            codes = [_stock_code(code) for code in stock_codes]
        except ValueError as exc:
            return _error("invalid_argument", str(exc))
        if len(set(codes)) != len(codes):
            return _error("invalid_argument", "stock_codes 不能重复")
        return self._request("POST", "/api/v1/market/full-tick", payload={"codes": codes})

    def sectors(self, sector_name: str = "", *, name_filter: str = "", limit: int = 60) -> dict[str, Any]:
        """不带板块名返回板块名列表，带板块名返回成分股代码；候选发现的起点。

        板块全表实测超过 30 万字符，一次调用就能吃掉整轮研究的上下文，所以列表侧强制过滤加分页，
        并把总数和被截断的事实显式交出去，不静默丢数据。
        """
        if sector_name:
            if len(sector_name) > 30:
                return _error("invalid_argument", "sector_name 过长")
            return self._request("GET", f"/api/v1/market/sectors/{quote(sector_name, safe='')}/stocks")
        if not 1 <= limit <= 200:
            return _error("invalid_argument", "limit 必须是 1 到 200")
        listed = self._request("GET", "/api/v1/market/sectors")
        if not listed["ok"]:
            return listed
        names = [str(name) for name in (listed["data"].get("items") or []) if str(name).strip()]
        keyword = name_filter.strip()
        matched = [name for name in names if keyword in name] if keyword else names
        return _success(
            operation="market_sectors",
            data={
                "name_filter": keyword,
                "total": len(names),
                "matched": len(matched),
                "truncated": len(matched) > limit,
                "sectors": matched[:limit],
            },
        )

    def screen(
        self,
        *,
        sector_name: str = "",
        stock_codes: list[str] | None = None,
        sort_by: str = "change_pct_desc",
        limit: int = 20,
        enrich_trend: bool = False,
    ) -> dict[str, Any]:
        """按板块或指定代码批量取实时行情，只回排序后的紧凑行，避免整版 tick 淹没上下文。"""
        if sort_by not in SCREEN_SORTS:
            return _error("invalid_argument", f"sort_by 只能是 {'、'.join(SCREEN_SORTS)}")
        if not 1 <= limit <= 50:
            return _error("invalid_argument", "limit 必须是 1 到 50")
        if enrich_trend and limit > TREND_ENRICH_LIMIT:
            return _error("invalid_argument", f"enrich_trend 需要读日线，limit 最多 {TREND_ENRICH_LIMIT}")
        if bool(sector_name) == bool(stock_codes):
            return _error("invalid_argument", "sector_name 与 stock_codes 必须且只能提供一个")
        if sector_name:
            resolved = self.sectors(sector_name)
            if not resolved["ok"]:
                return resolved
            universe = [code for code in resolved["data"].get("stocks", []) if STOCK_CODE_PATTERN.match(code)]
        else:
            try:
                universe = [_stock_code(code) for code in stock_codes or []]
            except ValueError as exc:
                return _error("invalid_argument", str(exc))
        universe = list(dict.fromkeys(universe))
        if not universe:
            return _error("empty_universe", f"板块或代码列表里没有可用 A 股代码：{sector_name or '自定义列表'}")
        if len(universe) > SCREEN_UNIVERSE_LIMIT:
            return _error("invalid_argument", f"股票池最多 {SCREEN_UNIVERSE_LIMIT} 只，当前 {len(universe)} 只")
        rows: list[dict[str, Any]] = []
        quote_at = ""
        returned = 0
        # 单笔买入上限决定了哪些票根本买不起：候选发现阶段就把它交给模型，别等下单被 blocked 才知道。
        try:
            max_buy_notional = _positive_float_env("MINIQMT_MAX_BUY_NOTIONAL")
        except ValueError as exc:
            return _error("configuration_error", str(exc))
        for start in range(0, len(universe), QUOTE_BATCH_SIZE):
            batch = self._request(
                "POST", "/api/v1/market/full-tick", payload={"codes": universe[start : start + QUOTE_BATCH_SIZE]}
            )
            if not batch["ok"]:
                return batch
            ticks = batch["data"].get("ticks") or {}
            returned += len(ticks)
            for code, tick in ticks.items():
                row = _screen_row(code, tick, max_buy_notional)
                if row:
                    rows.append(row)
                quote_at = max(quote_at, str(tick.get("timetag") or ""))
        rows.sort(key=SCREEN_SORTS[sort_by][0], reverse=SCREEN_SORTS[sort_by][1])
        selected = rows[:limit]
        trend_errors: list[str] = []
        if enrich_trend and selected:
            trend_errors = self._enrich_trend(selected, quote_at)
        data = {
            "sector": sector_name,
            "quote_at": quote_at,
            "universe_size": len(universe),
            "quoted": len(rows),
            # 行情缺失和停牌不能混进榜单，也不能被静默丢掉，单独计数交出去。
            "no_tick_count": len(universe) - returned,
            "unquotable_count": returned - len(rows),
            # 不可买的票仍然留在榜单里用于判断情绪，但必须带着为什么买不了。
            "buy_limits": {
                "lot_size": LOT_SIZE,
                "max_buy_notional": max_buy_notional,
                "max_buyable_price": round(max_buy_notional / LOT_SIZE, 2),
                "blocked_boards": "科创板 688/689 无交易权限",
            },
            "sort_by": sort_by,
            "rows": selected,
        }
        if enrich_trend:
            counts: dict[str, int] = {}
            for row in selected:
                gate = str(row.get("trend_gate") or "missing")
                counts[gate] = counts.get(gate, 0) + 1
            data["trend_gate_counts"] = counts
            data["trend_gate_meaning"] = (
                "breakout=站上 20 日线且贴近 20 日新高且放量，可按 breakout_entry 布防；"
                "pullback=多头排列回踩到 5 日线下方但未破 20 日线；extended=离 20 日线过远，追高风险；"
                "broken=跌破 20 日线或空头排列；insufficient_data=日线不足。"
                "这些标签和 pivot、stop_ref、breakout_entry 都是工具算出的确定性字段，只能原样引用，不得自行重判。"
            )
            if trend_errors:
                data["trend_errors"] = trend_errors
        return _success(operation="market_screen", data=data)

    def _enrich_trend(self, rows: list[dict[str, Any]], quote_at: str) -> list[str]:
        """给榜单行补日线趋势字段，把是否顺势从模型的目测变成代码判定。

        日线走 akshare（东财），且经 akshare_board 的当日缓存：大 QMT 撤极简接口后 bridge 的日线只剩
        当日 1 根，算不出均线和前高；而缓存下沉在 akshare_board，这条路径和 _bars 共享同一份，同一只票
        同一天最多打一次东财（+重试），不再每轮对所有票直连东财撞限流。akshare_board 反向依赖本模块常量，
        模块顶层互相 import 会成环，所以在这里延迟导入。
        """
        from minisweagent.environments import akshare_board

        now = datetime.now(TRADING_TZ)
        codes = [row["stock_code"] for row in rows]
        try:
            # spacing=1.5：首次填缓存（盘前几十只候选）时按节奏取，别连打；命中缓存的轮次不真的取，spacing 不生效。
            frames = akshare_board.daily_history(codes, now, cache_dir=self.state_dir, spacing=1.5)
        except akshare_board.BoardDataError as exc:
            for row in rows:
                row["trend_gate"] = "insufficient_data"
            return [f"日线读取失败（akshare），全部行按 insufficient_data 处理：{exc}"]
        errors = [f"{code} 无日线" for code in codes if not frames.get(code)]
        # 当日 bar 由缓存层过滤掉了（只存截至昨日），frames 是纯历史；今日事实用 tick 的 last/volume。
        today, elapsed = _session_progress(quote_at)
        for row in rows:
            row.update(
                _trend_metrics(row["last_price"], row["volume"], frames.get(row["stock_code"]) or [], today, elapsed)
            )
        return errors

    def sector_rank(self, *, family: str = "TGN", limit: int = 15, min_buyable: int = 3) -> dict[str, Any]:
        """按板块聚合当日行情，给出热度榜。

        候选发现必须从板块开始而不是个股涨幅榜：涨幅榜前排要么涨停封死买不进，要么一手成本
        就超过单笔上限，所以每个板块都要带上"可买家数"和"只算可买票的中位涨幅"——
        一个只有龙头在涨、可买小票不动的板块，对这个账户没有意义。
        """
        if family not in SECTOR_FAMILIES:
            return _error("invalid_argument", f"family 只能是 {'、'.join(SECTOR_FAMILIES)}")
        if not 1 <= limit <= 30:
            return _error("invalid_argument", "limit 必须是 1 到 30")
        members_result = self._sector_members(family)
        if not members_result["ok"]:
            return members_result
        members = members_result["data"]["members"]
        failed = members_result["data"]["failed"]
        if not members:
            return _error("empty_universe", f"{family} 板块族没有取到任何成分股")
        try:
            max_buy_notional = _positive_float_env("MINIQMT_MAX_BUY_NOTIONAL")
        except ValueError as exc:
            return _error("configuration_error", str(exc))
        ticks = self._market_ticks()
        if not ticks["ok"]:
            return ticks
        quotes = ticks["data"]["quotes"]
        quote_at = ticks["data"]["quote_at"]
        ranked = []
        for name, codes in members.items():
            summary = _sector_summary(name, codes, quotes, max_buy_notional)
            if summary and summary["members_quoted"] >= SECTOR_RANK_MIN_MEMBERS and summary["buyable_count"] >= min_buyable:
                ranked.append(summary)
        # 按可买票的中位涨幅排序：整个板块的中位数会被买不起的龙头抬高，那不是这个账户能吃到的行情。
        ranked.sort(key=lambda item: item["buyable_median_change_pct"], reverse=True)
        return _success(
            operation="market_sector_rank",
            data={
                "family": family,
                "quote_at": quote_at,
                "sectors_scanned": len(members),
                "sectors_returned": min(limit, len(ranked)),
                "universe_quoted": len(quotes),
                "min_buyable": min_buyable,
                "buy_limits": {
                    "lot_size": LOT_SIZE,
                    "max_buy_notional": max_buy_notional,
                    "max_buyable_price": round(max_buy_notional / LOT_SIZE, 2),
                },
                "sort_by": "buyable_median_change_pct",
                "member_fetch_failures": failed[:10],
                "sectors": ranked[:limit],
            },
        )

    def _market_ticks(self) -> dict[str, Any]:
        """一次拉全沪深 A 股 tick，板块聚合全部在本地算，避免按板块重复取行情。"""
        listed = self.sectors(MARKET_UNIVERSE_SECTOR)
        if not listed["ok"]:
            return listed
        universe = [code for code in listed["data"].get("stocks", []) if STOCK_CODE_PATTERN.match(code)]
        if not universe:
            return _error("empty_universe", f"{MARKET_UNIVERSE_SECTOR} 没有取到成分股，板块数据可能未下载")
        quotes: dict[str, dict[str, Any]] = {}
        quote_at = ""
        for start in range(0, len(universe), QUOTE_BATCH_SIZE):
            batch = self._request(
                "POST", "/api/v1/market/full-tick", payload={"codes": universe[start : start + QUOTE_BATCH_SIZE]}
            )
            if not batch["ok"]:
                return batch
            for code, tick in (batch["data"].get("ticks") or {}).items():
                quotes[code] = tick
                quote_at = max(quote_at, str(tick.get("timetag") or ""))
        return _success(operation="market_ticks", data={"quotes": quotes, "quote_at": quote_at})

    def _sector_members(self, family: str) -> dict[str, Any]:
        """取板块族的成分股映射，按交易日缓存在状态目录里。

        一个板块族有几百个板块，每次重拉要几百次请求；板块成分不会日内变动，所以缓存到当天结束。
        """
        cache_path = self.state_dir / SECTOR_MEMBER_CACHE
        today = datetime.now(TRADING_TZ).strftime("%Y%m%d")
        cached: dict[str, Any] = {}
        if cache_path.is_file():
            try:
                loaded = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("trade_date") == today:
                    cached = loaded
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                # 缓存坏了就当没有，重新拉一次即可，不要因为缓存问题让整轮研究失败。
                cached = {}
        families = cached.get("families") if isinstance(cached.get("families"), dict) else {}
        if isinstance(families.get(family), dict) and families[family]:
            return _success(operation="sector_members", data={"members": families[family], "failed": [], "cached": True})
        listed = self._request("GET", "/api/v1/market/sectors")
        if not listed["ok"]:
            return listed
        names = [
            str(name)
            for name in (listed["data"].get("items") or [])
            if str(name).startswith(family) and not str(name).endswith(SECTOR_NAME_SUFFIX_SKIP)
        ]
        if not names:
            return _error("empty_universe", f"板块列表里没有 {family} 开头的板块")
        members: dict[str, list[str]] = {}
        failed: list[str] = []
        for name in names:
            resolved = self.sectors(name)
            if not resolved["ok"]:
                failed.append(name)
                continue
            codes = [code for code in resolved["data"].get("stocks", []) if STOCK_CODE_PATTERN.match(code)]
            if codes:
                members[name] = codes
        families[family] = members
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"trade_date": today, "families": families}, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(cache_path)
        except OSError as exc:
            logger.warning("板块成分缓存写入失败：%s", type(exc).__name__)
        return _success(operation="sector_members", data={"members": members, "failed": failed, "cached": False})

    def history(
        self, stock_codes: list[str], *, period: str = "1d", start_time: str = "", end_time: str = ""
    ) -> dict[str, Any]:
        """先按范围补下载再读本地 K 线；xtdata 不下载就只返回空表，静默的空数据比报错更危险。

        不截断 bar 数量：这些 K 线现在只用来在宿主侧渲染图，截断会让分钟图只剩最后一小时。
        """
        if not 1 <= len(stock_codes) <= HISTORY_CODE_LIMIT:
            return _error("invalid_argument", f"stock_codes 必须包含 1 到 {HISTORY_CODE_LIMIT} 项")
        try:
            codes = [_stock_code(code) for code in stock_codes]
        except ValueError as exc:
            return _error("invalid_argument", str(exc))
        codes = list(dict.fromkeys(codes))
        if period not in HISTORY_PERIODS:
            return _error("invalid_argument", f"period 只能是 {'、'.join(HISTORY_PERIODS)}")
        if not (_is_day_stamp(start_time) and _is_day_stamp(end_time)) or start_time > end_time:
            return _error("invalid_argument", "start_time 和 end_time 必须是 YYYYMMDD，且开始不晚于结束")
        payload = {"stock_list": codes, "period": period, "start_time": start_time, "end_time": end_time}
        downloaded = self._request("POST", "/api/v1/market/history/download2", payload=payload)
        if not downloaded["ok"]:
            return downloaded
        local = self._request("POST", "/api/v1/market/history/local", payload={**payload, "count": -1})
        if not local["ok"]:
            return local
        bars = {code: _history_rows(frame) for code, frame in (local["data"].get("data") or {}).items()}
        empty = sorted(code for code, rows in bars.items() if not rows)
        return _success(
            operation="market_history",
            data={
                "period": period,
                "start_time": start_time,
                "end_time": end_time,
                "bars": {code: rows for code, rows in bars.items() if rows},
                "empty_codes": empty,
            },
        )

    def download_sectors(self) -> dict[str, Any]:
        """板块数据必须先下载才读得到；没下载时接口返回 200 但成分股是空数组。"""
        return self._request("POST", "/api/v1/market/sectors/download")

    def account(self, view: str) -> dict[str, Any]:
        _load_project_env()
        account_id = os.getenv("MINIQMT_ACCOUNT_ID", "").strip()
        if not account_id:
            return _error("configuration_error", "宿主未配置 MINIQMT_ACCOUNT_ID")
        paths = {
            "snapshot": ("/api/v1/trader/asset", "/api/v1/trader/positions"),
            "orders": ("/api/v1/trader/orders",),
            "trades": ("/api/v1/trader/trades",),
        }
        if view not in paths:
            return _error("invalid_argument", f"不支持的账户视图：{view}")
        ready = self._ensure_account_ready(account_id)
        if not ready["ok"]:
            return ready
        responses = [self._request("GET", path, query={"account_id": account_id}) for path in paths[view]]
        failed = next((result for result in responses if not result["ok"]), None)
        if failed:
            return failed
        data = (
            {"assets": responses[0]["data"], "positions": responses[1]["data"]}
            if view == "snapshot"
            else responses[0]["data"]
        )
        return _success(
            operation=f"account_{view}",
            data={"account_id_hash": _account_hash(account_id), "snapshot_at": _now(), "result": data},
        )

    def trade(self, operation: str, inputs: dict[str, Any]) -> dict[str, Any]:
        _load_project_env()
        if self.mode != "execute":
            if self.mode != "auto_execute":
                return self._audited(operation, inputs, _error("blocked", "交易工具处于 observe 模式"))
        try:
            kill_switch = _truthy_env("MINIQMT_KILL_SWITCH")
        except ValueError as exc:
            return self._audited(operation, inputs, _error("configuration_error", str(exc)))
        if kill_switch:
            return self._audited(operation, inputs, _error("blocked", "宿主 kill switch 已开启"))
        if self.mode == "auto_execute" and not _is_trading_time(datetime.now(TRADING_TZ)):
            return self._audited(operation, inputs, _error("blocked", "auto_execute 只允许在 A 股连续竞价时段交易"))
        account_id = os.getenv("MINIQMT_ACCOUNT_ID", "").strip()
        if not account_id:
            return self._audited(operation, inputs, _error("configuration_error", "宿主未配置 MINIQMT_ACCOUNT_ID"))
        intent_id = inputs.get("client_intent_id")
        if not isinstance(intent_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", intent_id):
            return self._audited(
                operation,
                inputs,
                _error("invalid_argument", "client_intent_id 必须是 8 到 128 位的稳定标识"),
            )
        existing = self._intent_exists(account_id, intent_id)
        if not existing["ok"] or existing["data"]["exists"]:
            result = existing if not existing["ok"] else _error("duplicate_intent", "该 client_intent_id 已被持久化处理，禁止重复提交")
            return self._audited(operation, inputs, result)
        ready = self._ensure_account_ready(account_id)
        if not ready["ok"]:
            return self._audited(operation, inputs, ready)
        if operation == "submit":
            validation = _order_payload(inputs, account_id)
            if isinstance(validation, str):
                return self._audited(operation, inputs, _error("invalid_argument", validation))
            path, payload = "/api/v1/trader/order/live", validation
            # price_cap 是宿主内部概念，Bridge 只认具体限价：推导后必须从 payload 里摘掉。
            price_cap = payload.pop("price_cap", None)
            safety = self._validate_order_safety(account_id, payload, price_cap)
            if not safety["ok"]:
                return self._audited(operation, inputs, safety)
            payload["price"] = safety["data"]["price"]
            notional = float(safety["data"].get("notional") or 0.0)
        elif operation == "cancel":
            unknown = set(inputs) - {"client_intent_id", "order_id"}
            if unknown:
                return self._audited(
                    operation,
                    inputs,
                    _error("invalid_argument", f"cancel 包含未知字段：{', '.join(sorted(unknown))}"),
                )
            order_id = inputs.get("order_id")
            if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", order_id):
                return self._audited(operation, inputs, _error("invalid_argument", "order_id 格式无效"))
            orders = self._request(
                "GET",
                "/api/v1/trader/orders",
                query={"account_id": account_id, "cancelable_only": "true"},
            )
            if not orders["ok"] or not _contains_order(orders.get("data"), order_id):
                return self._audited(operation, inputs, _error("blocked", "未确认该委托属于当前账户且可撤，禁止撤单"))
            path = "/api/v1/trader/order/cancel"
            payload = {"account_id": account_id, "order_id": order_id}
            notional = 0.0
        else:
            return self._audited(operation, inputs, _error("invalid_argument", f"不支持的交易 operation：{operation}"))

        # 在发起 Bridge 请求前持久化冻结意图；进程重启或请求超时也不能重复提交。
        reservation = self._reserve_intent(
            account_id=account_id,
            intent_id=intent_id,
            operation=operation,
            stock_code=str(inputs.get("stock_code") or ""),
            side=str(inputs.get("side") or ""),
            volume=int(inputs.get("volume") or 0),
            notional=notional,
        )
        if not reservation["ok"]:
            return self._audited(operation, inputs, reservation)
        result = self._request(
            "POST",
            path,
            payload=payload,
            unknown_on_network_error=True,
        )
        result["operation"] = operation
        result["audit"] = {
            "client_intent_id": intent_id,
            "account_id_hash": _account_hash(account_id),
            "submitted_at": _now(),
        }
        self._finish_intent(account_id, intent_id, result)
        return self._audited(operation, inputs, result)

    def _validate_order_safety(
        self, account_id: str, payload: dict[str, Any], price_cap: float | None = None
    ) -> dict[str, Any]:
        side = payload["order_type"]
        volume = int(payload["order_volume"])
        price = payload.get("price")
        max_volume_name = "MINIQMT_MAX_BUY_VOLUME" if side == "BUY" else "MINIQMT_MAX_SELL_VOLUME"
        try:
            max_volume = _positive_int_env(max_volume_name)
        except ValueError as exc:
            return _error("configuration_error", str(exc))
        if volume > max_volume:
            return _error("blocked", f"{side} 数量超过宿主单笔上限 {max_volume}")

        quote_result = self._request("POST", "/api/v1/market/full-tick", payload={"codes": [payload["stock_code"]]})
        if not quote_result["ok"]:
            return _error("blocked", "安全检查无法取得最新行情，禁止交易")
        quote = _extract_quote(quote_result["data"], payload["stock_code"])
        if quote is None:
            return _error("blocked", "最新行情缺少有效价格或时间，禁止交易")
        last_price, quote_at = quote
        try:
            max_deviation_bps = _positive_float_env("MINIQMT_MAX_PRICE_DEVIATION_BPS")
        except ValueError as exc:
            return _error("configuration_error", str(exc))
        if price_cap is not None:
            price = _derive_limit_price(last_price, float(price_cap), max_deviation_bps)
            if price is None:
                # 两种拒单原因完全不同，必须分开讲清楚：一个是行情跑了，一个是这只票的报价
                # 单位本身就装不下偏离额度。混成一句话会让人以为是同一个问题。
                if float(price_cap) <= last_price:
                    return _error("blocked", f"最新价 {last_price} 已达到追高上限 {price_cap}，属于跳空追高，不下单")
                tick_bps = TICK_SIZE / last_price * 10_000
                return _error(
                    "blocked",
                    f"最小报价单位 {TICK_SIZE} 元在 {last_price} 上已占 {tick_bps:.1f}bp，"
                    f"超过推导可用的 {max_deviation_bps * LIMIT_PREMIUM_RATIO:.0f}bp 上浮额度，推不出可成交限价",
                )
        if self.mode == "auto_execute":
            try:
                max_age = _positive_int_env("MINIQMT_MAX_QUOTE_AGE_SECONDS")
            except ValueError as exc:
                return _error("configuration_error", str(exc))
            age = (datetime.now(TRADING_TZ) - quote_at.astimezone(TRADING_TZ)).total_seconds()
            if age < -5 or age > max_age:
                return _error("blocked", f"行情已过期或时间异常：age_seconds={age:.1f}")
            if price is None:
                return _error("blocked", f"{side} 必须使用固定限价，不能使用最新价委托")
            deviation_bps = abs(float(price) - last_price) / last_price * 10_000
            if deviation_bps > max_deviation_bps:
                return _error("blocked", f"委托价偏离最新价 {deviation_bps:.1f}bp，超过上限")

        if side == "BUY":
            if price is None:
                return _error("blocked", "BUY 必须使用固定限价，不能使用最新价委托")
            if payload["stock_code"].startswith(BUY_BLOCKED_PREFIXES):
                return _error("blocked", "账户没有科创板交易权限，禁止买入 688/689 代码")
            notional = round(float(price) * volume, 2)
            try:
                max_notional = _positive_float_env("MINIQMT_MAX_BUY_NOTIONAL")
                min_cash_ratio = _ratio_env("MINIQMT_MIN_CASH_RATIO")
            except ValueError as exc:
                return _error("configuration_error", str(exc))
            if notional > max_notional:
                return _error("blocked", f"BUY 金额超过宿主单笔上限 {max_notional:.2f}")
            asset_result = self._request("GET", "/api/v1/trader/asset", query={"account_id": account_id})
            asset = _extract_asset(asset_result.get("data")) if asset_result["ok"] else None
            if asset is None:
                return _error("blocked", "账户资产缺少可用资金或总资产，禁止买入")
            available_cash, total_asset = asset
            if notional > available_cash or available_cash - notional < total_asset * min_cash_ratio:
                return _error("blocked", "买入后将突破可用资金或现金下限")
            return _success(
                operation="order_safety", data={"notional": notional, "last_price": last_price, "price": price}
            )

        positions_result = self._request("GET", "/api/v1/trader/positions", query={"account_id": account_id})
        position = _extract_position(positions_result.get("data"), payload["stock_code"]) if positions_result["ok"] else None
        if position is None:
            return _error("blocked", "持仓成本或可卖数量缺失，禁止卖出")
        avg_cost, can_use_volume = position
        if volume > can_use_volume:
            return _error("blocked", f"SELL 数量超过可卖数量 {can_use_volume}")
        # 趋势跟踪策略要求小亏就走，所以不再按浮亏比例阻断卖出；亏损幅度只上报，退出纪律由组合计划负责。
        loss_ratio = (last_price - avg_cost) / avg_cost
        notional = round(last_price * volume, 2)
        return _success(
            operation="order_safety",
            data={
                "notional": notional,
                "last_price": last_price,
                "loss_ratio": loss_ratio,
                "price": price,
            },
        )

    def _reserve_intent(
        self,
        *,
        account_id: str,
        intent_id: str,
        operation: str,
        stock_code: str,
        side: str,
        volume: int,
        notional: float,
    ) -> dict[str, Any]:
        try:
            max_daily_buy = _positive_float_env("MINIQMT_MAX_DAILY_BUY_NOTIONAL")
        except ValueError as exc:
            return _error("configuration_error", str(exc))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        account_hash = _account_hash(account_id)
        trading_day = datetime.now(TRADING_TZ).date().isoformat()
        try:
            with sqlite3.connect(self.state_dir / "trade_state.sqlite3", timeout=10) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS intents (
                        account_hash TEXT NOT NULL,
                        intent_id TEXT NOT NULL,
                        cycle_id TEXT NOT NULL,
                        trading_day TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        stock_code TEXT NOT NULL,
                        side TEXT NOT NULL,
                        volume INTEGER NOT NULL,
                        notional REAL NOT NULL,
                        status TEXT NOT NULL,
                        result_json TEXT,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (account_hash, intent_id)
                    )
                    """
                )
                conn.execute("BEGIN IMMEDIATE")
                duplicate = conn.execute(
                    "SELECT 1 FROM intents WHERE account_hash = ? AND intent_id = ?",
                    (account_hash, intent_id),
                ).fetchone()
                if duplicate:
                    return _error("duplicate_intent", "该 client_intent_id 已被持久化处理，禁止重复提交")
                daily_buy = conn.execute(
                    "SELECT COALESCE(SUM(notional), 0) FROM intents WHERE account_hash = ? AND trading_day = ? AND side = 'BUY'",
                    (account_hash, trading_day),
                ).fetchone()[0]
                if side == "BUY" and float(daily_buy) + notional > max_daily_buy:
                    return _error("blocked", f"当日累计买入金额将超过上限 {max_daily_buy:.2f}")
                conn.execute(
                    "INSERT INTO intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', NULL, ?)",
                    (
                        account_hash,
                        intent_id,
                        self.cycle_id,
                        trading_day,
                        operation,
                        stock_code,
                        side,
                        volume,
                        notional,
                        _now(),
                    ),
                )
                conn.commit()
        except sqlite3.Error as exc:
            return _error("state_error", f"交易状态持久化失败：{type(exc).__name__}")
        return _success(operation="intent_reserved", data={"intent_id": intent_id})

    def _intent_exists(self, account_id: str, intent_id: str) -> dict[str, Any]:
        path = self.state_dir / "trade_state.sqlite3"
        if not path.is_file():
            return _success(operation="intent_lookup", data={"exists": False})
        try:
            with sqlite3.connect(path, timeout=10) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM intents WHERE account_hash = ? AND intent_id = ?",
                    (_account_hash(account_id), intent_id),
                ).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return _success(operation="intent_lookup", data={"exists": False})
            return _error("state_error", f"交易状态读取失败：{type(exc).__name__}")
        except sqlite3.Error as exc:
            return _error("state_error", f"交易状态读取失败：{type(exc).__name__}")
        return _success(operation="intent_lookup", data={"exists": exists})

    def _finish_intent(self, account_id: str, intent_id: str, result: dict[str, Any]) -> None:
        # 当日买入额度只该统计真正占用了市场的委托。下单被券商拒、或干净地失败（根本没发出去），
        # 都不占额度——否则失败单会把额度白白吃满，把整天买入锁死。释放时把 notional 归零即可：
        # 审计行仍在（保留决策留痕与去重），SUM(notional) 自然不再计入。
        # 唯一例外是 status=unknown（5xx/断网，提交结果未知）：宁可保守占额度，也不能因为误判没成交而重复下单撞穿上限。
        committed = _intent_committed(result)
        try:
            with sqlite3.connect(self.state_dir / "trade_state.sqlite3", timeout=10) as conn:
                if committed:
                    conn.execute(
                        "UPDATE intents SET status = ?, result_json = ? WHERE account_hash = ? AND intent_id = ?",
                        (
                            str(result.get("status") or "unknown"),
                            json.dumps(result, ensure_ascii=False, sort_keys=True),
                            _account_hash(account_id),
                            intent_id,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE intents SET status = ?, result_json = ?, notional = 0 WHERE account_hash = ? AND intent_id = ?",
                        (
                            str(result.get("status") or "unknown"),
                            json.dumps(result, ensure_ascii=False, sort_keys=True),
                            _account_hash(account_id),
                            intent_id,
                        ),
                    )
                conn.commit()
        except sqlite3.Error:
            # 意图已经冻结；更新失败也不能通过重试再次下单。
            pass

    def _audited(self, operation: str, inputs: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        try:
            append_trade_audit(self.state_dir, self.cycle_id, operation, inputs, result)
        except OSError as exc:
            result = {**result, "journal_error": f"交易 Markdown 审计写入失败：{type(exc).__name__}"}
        return result

    def _ensure_account_ready(self, account_id: str) -> dict[str, Any]:
        if self._account_ready:
            return _success(operation="account_ready", data={"ready": True})
        result = self._request(
            "POST",
            "/api/v1/trader/ensure-ready",
            payload={"account_id": account_id},
        )
        if result["ok"]:
            self._account_ready = True
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        unknown_on_network_error: bool = False,
    ) -> dict[str, Any]:
        _load_project_env()
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        api_key = os.getenv("MINIQMT_BRIDGE_API_KEY", "").strip()
        if api_key:
            headers["X-Api-Key"] = api_key
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    return _error("response_too_large", "MiniQMT 响应超过 1 MB 限制")
                data = json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            # Bridge 把真正的原因放在响应体里（例如 trader not connected），丢掉它等于让 Agent 瞎猜。
            body = _error_body(exc)
            scene = f"{method} {path} HTTP {exc.code}：{body}"
            logger.error("%s MiniQMT 请求失败 %s", _now(), scene)
            if unknown_on_network_error and exc.code >= 500:
                return _error("unknown", f"交易提交结果未知，请查询委托后人工确认（{scene}）")
            error_code = "authentication_error" if exc.code in {401, 403} else "http_error"
            return _error(error_code, f"MiniQMT Bridge {scene}")
        except (URLError, TimeoutError, OSError) as exc:
            scene = f"{method} {path} {type(exc).__name__}: {_mask_account(str(exc))}"
            logger.error("%s MiniQMT 连接失败 %s", _now(), scene)
            code = "unknown" if unknown_on_network_error else "network_error"
            detail = (
                f"交易提交结果未知，请查询委托后人工确认（{scene}）"
                if unknown_on_network_error
                else f"MiniQMT 连接失败：{scene}"
            )
            return _error(code, detail)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error("parse_error", "MiniQMT 返回的不是有效 UTF-8 JSON")
        return _success(operation=path, data=_redact(data))


def host_limits() -> dict[str, Any]:
    """宿主的交易硬限额，一份定义两个用途：交易工具照它拦单，prompt 照它注入。

    两处各读一遍环境变量必然会漂移，模型就会按一套限额做计划、撞上另一套被拒。
    """
    return {
        "lot_size": LOT_SIZE,
        "max_buy_notional": _positive_float_env("MINIQMT_MAX_BUY_NOTIONAL"),
        "max_buyable_price": round(_positive_float_env("MINIQMT_MAX_BUY_NOTIONAL") / LOT_SIZE, 2),
        "max_daily_buy_notional": _positive_float_env("MINIQMT_MAX_DAILY_BUY_NOTIONAL"),
        "min_order_notional": _positive_float_env("MINIQMT_MIN_ORDER_NOTIONAL"),
        "max_buy_volume": _positive_int_env("MINIQMT_MAX_BUY_VOLUME"),
        "max_sell_volume": _positive_int_env("MINIQMT_MAX_SELL_VOLUME"),
        "min_cash_ratio": _ratio_env("MINIQMT_MIN_CASH_RATIO"),
        "max_price_deviation_bps": _positive_float_env("MINIQMT_MAX_PRICE_DEVIATION_BPS"),
        "kill_switch": _truthy_env("MINIQMT_KILL_SWITCH"),
        "blocked_boards": "科创板 688/689 无交易权限，禁止买入",
        "min_buy_price": round(TICK_SIZE / (_positive_float_env("MINIQMT_MAX_PRICE_DEVIATION_BPS") * LIMIT_PREMIUM_RATIO / 10_000), 2),
    }


def _order_payload(inputs: dict[str, Any], account_id: str) -> dict[str, Any] | str:
    unknown = set(inputs) - {"client_intent_id", "stock_code", "side", "volume", "price", "price_cap"}
    if unknown:
        return f"submit 包含未知字段：{', '.join(sorted(unknown))}"
    stock_code = inputs.get("stock_code")
    try:
        stock_code = _stock_code(stock_code)
    except ValueError as exc:
        return str(exc)
    side = inputs.get("side")
    if side not in {"BUY", "SELL"}:
        return "side 只能是 BUY 或 SELL"
    volume = inputs.get("volume")
    if isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0:
        return "volume 必须是正整数"
    try:
        max_volume = _positive_int_env("MINIQMT_MAX_ORDER_VOLUME")
    except ValueError as exc:
        return str(exc)
    if volume > max_volume:
        return f"volume 超过宿主单笔上限 {max_volume}"
    if side == "BUY" and volume % 100 != 0:
        return "A 股买入数量必须是 100 股的整数倍"
    price = inputs.get("price")
    if price is not None and (
        isinstance(price, bool)
        or not isinstance(price, (int, float))
        or not math.isfinite(price)
        or price <= 0
    ):
        return "price 必须是正数或 null"
    # price_cap 是"追高上限"：只说最多愿意出到多少，实际限价由宿主在提交那一刻按最新价推导。
    # 调用方提前算好的固定 price 一旦遇到价格反向移动就会撞破偏离上限，price_cap 把这个
    # 时间差从调用方手里拿掉，代价是调用方无法精确控制成交价——这正是我们要的。
    price_cap = inputs.get("price_cap")
    if price_cap is not None and (
        isinstance(price_cap, bool)
        or not isinstance(price_cap, (int, float))
        or not math.isfinite(price_cap)
        or price_cap <= 0
    ):
        return "price_cap 必须是正数或 null"
    if price is not None and price_cap is not None:
        return "price 与 price_cap 互斥：给了固定限价就不要再给追高上限"
    return {
        "account_id": account_id,
        "stock_code": stock_code,
        "order_type": side,
        "order_volume": volume,
        "price_type": "LATEST" if price is None and price_cap is None else "FIX",
        "price": price,
        "price_cap": price_cap,
        # 意图 id 直接当券商侧的 order_remark：Bridge 提交后不再等 QMT 回填合同编号
        # （那一步要付 10s 级的委托全量查询），返回时可能没有 order_id，只能靠这个
        # remark 在下一轮的委托列表里把委托认出来。
        "order_remark": str(inputs.get("client_intent_id") or ""),
    }


def _stock_code(value: Any) -> str:
    if not isinstance(value, str) or not STOCK_CODE_PATTERN.fullmatch(value.upper()):
        raise ValueError("股票代码必须是 600000.SH 形式的 A 股代码")
    return value.upper()


# A 股最小报价单位。限价必须落在这个网格上，否则柜台直接拒单。
TICK_SIZE = 0.01
# 推导限价时只用掉偏离额度的 80%，余下 20% 吸收取整误差和请求往返里的价格漂移。
LIMIT_PREMIUM_RATIO = 0.8


def _tick_floor(value: float) -> float:
    """向下取整到最小报价单位；round 先消掉二进制浮点的尾差。"""
    return round(math.floor(round(value / TICK_SIZE, 6)) * TICK_SIZE, 2)


def _derive_limit_price(last_price: float, price_cap: float, max_deviation_bps: float) -> float | None:
    """按最新价推导买入限价：上浮一点保证吃得到卖盘，但不越过调用方给的追高上限。

    必须向下取整：低价股一个 tick 就值几十个 bp，向上取整会让宿主拒掉自己刚算出来的价格。
    推不出严格高于最新价的限价时返回 None——要么最新价已经顶到追高上限，要么报价单位粗到
    吃不下上浮额度。这两种情况都不下单：挂一个等于最新价的限价只会留下不成交的悬单，
    让主 Agent 以为没买到而实际账户里多一笔在途委托。
    """
    premium_bps = max_deviation_bps * LIMIT_PREMIUM_RATIO
    limit = min(_tick_floor(last_price * (1 + premium_bps / 10_000)), _tick_floor(price_cap))
    return limit if limit > last_price else None


def _is_day_stamp(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 8 and value.isdigit()


def _screen_row(code: str, tick: dict[str, Any], max_buy_notional: float) -> dict[str, Any] | None:
    """把整版 tick 压成筛选需要的几个字段；停牌或无成交的直接丢掉，不当成 0% 涨幅。"""
    last = _finite(tick.get("lastPrice"))
    close = _finite(tick.get("lastClose"))
    if last is None or close is None or last <= 0 or close <= 0:
        return None
    lot_cost = round(last * LOT_SIZE, 2)
    high = _finite(tick.get("high"))
    low = _finite(tick.get("low"))
    row = {
        "stock_code": code,
        "last_price": last,
        "last_close": close,
        "change_pct": round((last - close) / close * 100, 2),
        "open": _finite(tick.get("open")),
        "high": high,
        "low": low,
        # 收在当日振幅的哪个位置：1 是收在最高价，0 是收在最低价。强势票收盘贴上沿，
        # 涨幅相同但收在下沿的是冲高回落，两者在涨幅榜上长得一样。
        "close_position": _close_position(last, high, low),
        "volume": _finite(tick.get("volume")) or 0.0,
        "amount": _finite(tick.get("amount")) or 0.0,
        # 一手成本和可买标记直接给结论：模型不用自己乘 100，也不用猜宿主限额。
        "lot_cost": lot_cost,
        "buyable": lot_cost <= max_buy_notional and not code.startswith(BUY_BLOCKED_PREFIXES),
    }
    if not row["buyable"]:
        row["unbuyable"] = "科创板无权限" if code.startswith(BUY_BLOCKED_PREFIXES) else "一手成本超过单笔买入上限"
    return row


def _close_position(last: float, high: float | None, low: float | None) -> float | None:
    if high is None or low is None or high <= low:
        return None
    return round((last - low) / (high - low), 2)


def _session_progress(quote_at: str) -> tuple[int, int]:
    """从行情时间戳解出交易日和已交易分钟数。

    时间戳形如 "20260908 11:30:01"。解析不出来就按整个交易日算，宁可让 vol_ratio 偏保守，
    也不要凭空放大成交量比。
    """
    stamp = _parse_time(quote_at)
    if stamp is None:
        return 0, TRADING_MINUTES_PER_DAY
    day = int(stamp.strftime("%Y%m%d"))
    minutes = stamp.hour * 60 + stamp.minute
    open_am, close_am = 9 * 60 + 30, 11 * 60 + 30
    open_pm, close_pm = 13 * 60, 15 * 60
    if minutes <= open_am:
        elapsed = 0
    elif minutes <= close_am:
        elapsed = minutes - open_am
    elif minutes <= open_pm:
        elapsed = close_am - open_am
    elif minutes <= close_pm:
        elapsed = close_am - open_am + minutes - open_pm
    else:
        elapsed = TRADING_MINUTES_PER_DAY
    return day, max(elapsed, TRADING_MINUTES_FLOOR)


def _trend_metrics(
    last: float, volume_today: float, bars: list[dict[str, Any]], today: int, elapsed_minutes: int
) -> dict[str, Any]:
    """用日线算出顺势判定所需的确定性字段；数据不足就明说，不用估算糊过去。"""
    # 缺任一字段的 K 线整根丢掉：分字段过滤会让均线和前高错位到不同的日期上。
    usable = [
        bar
        for bar in bars
        if all(_finite(bar.get(name)) is not None for name in ("close", "high", "low", "volume"))
        and isinstance(bar.get("date"), int)
    ]
    # 当日 bar 在盘中是实时更新的，必须从历史里剔除：pivot 要的是"前高"，
    # 拿含今天的最高价当 pivot，突破条件就退化成"突破自己"，永远成立也永远没意义。
    prior = [bar for bar in usable if bar["date"] != today]
    if len(usable) < TREND_MIN_BARS or len(prior) < 20:
        return {"trend_gate": "insufficient_data", "bars_used": len(usable), "prior_bars": len(prior)}
    closes = [float(bar["close"]) for bar in usable]
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    base_volume = _median(sorted(float(bar["volume"]) for bar in prior[-5:]))
    # 按已交易时间折算基准量，否则 10:00 的半小时成交量和全天基准量比永远不算放量。
    expected_volume = base_volume * elapsed_minutes / TRADING_MINUTES_PER_DAY
    pivot = max(float(bar["high"]) for bar in prior[-20:])
    swing_low = min(float(bar["low"]) for bar in prior[-10:])
    metrics = {
        "bars_used": len(usable),
        "prior_bars": len(prior),
        "session_minutes": elapsed_minutes,
        "ma5": round(ma5, 2),
        "ma10": round(ma10, 2),
        "ma20": round(ma20, 2),
        "ma_stack": "bull" if ma5 > ma10 > ma20 else "bear" if ma5 < ma10 < ma20 else "mixed",
        "ma20_gap_pct": round((last / ma20 - 1) * 100, 2),
        "vol_ratio": round(volume_today / expected_volume, 2) if expected_volume > 0 else None,
        "pivot": round(pivot, 2),
        "high_20d_gap_pct": round((last / pivot - 1) * 100, 2),
        "swing_low_10d": round(swing_low, 2),
        "stop_ref": round(swing_low * STOP_REF_RATIO, 2),
    }
    metrics["trend_gate"] = _trend_gate(last, metrics)
    if metrics["trend_gate"] in {"breakout", "pullback"}:
        # 突破区间由代码给：下界是穿越 pivot，上界是追高天花板。组合经理只能引用，不用自己乘系数。
        metrics["breakout_entry"] = {
            "lower": round(pivot * BREAKOUT_TRIGGER_RATIO, 2),
            "upper": round(pivot * BREAKOUT_CEILING_RATIO, 2),
        }
    return metrics


def _trend_gate(last: float, metrics: dict[str, Any]) -> str:
    if last < metrics["ma20"] or metrics["ma_stack"] == "bear":
        return "broken"
    if metrics["ma20_gap_pct"] > 20:
        return "extended"
    vol_ratio = metrics["vol_ratio"]
    if metrics["high_20d_gap_pct"] >= -1 and vol_ratio is not None and vol_ratio >= 1.5:
        return "breakout"
    if metrics["ma_stack"] == "bull" and last < metrics["ma5"]:
        return "pullback"
    return "holding"


def _sector_summary(
    name: str, codes: list[str], quotes: dict[str, dict[str, Any]], max_buy_notional: float
) -> dict[str, Any] | None:
    """把一个板块的成分股 tick 聚合成热度行。"""
    rows = [row for row in (_screen_row(code, quotes[code], max_buy_notional) for code in codes if code in quotes) if row]
    if not rows:
        return None
    buyable = [row for row in rows if row["buyable"]]
    changes = sorted(row["change_pct"] for row in rows)
    # 只算可买票的中位涨幅：板块整体中位数会被买不起的龙头抬高，那不是这个账户能吃到的行情。
    buyable_changes = sorted(row["change_pct"] for row in buyable)
    top = sorted(buyable, key=lambda row: row["change_pct"], reverse=True)[:3]
    return {
        "sector": name,
        "members": len(codes),
        "members_quoted": len(rows),
        "up_count": sum(1 for row in rows if row["change_pct"] > 0),
        "up_ratio": round(sum(1 for row in rows if row["change_pct"] > 0) / len(rows), 2),
        "median_change_pct": _median(changes),
        "amount": round(sum(row["amount"] for row in rows), 2),
        "buyable_count": len(buyable),
        "buyable_median_change_pct": _median(buyable_changes) if buyable_changes else -100.0,
        # 板块内可买且最强的三只，作为下钻起点；还要用 miniqmt_screen 的 enrich_trend 复查结构。
        "top_buyable": [
            {
                "stock_code": row["stock_code"],
                "change_pct": row["change_pct"],
                "last_price": row["last_price"],
                "lot_cost": row["lot_cost"],
                "close_position": row["close_position"],
            }
            for row in top
        ],
    }


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    middle = len(values) // 2
    if len(values) % 2:
        return round(values[middle], 2)
    return round((values[middle - 1] + values[middle]) / 2, 2)


def _history_rows(frame: Any) -> list[dict[str, Any]]:
    """把 bridge 的 dataframe 字典压成按日期排序的紧凑行。"""
    if not isinstance(frame, dict):
        return []
    columns = frame.get("columns") or []
    wanted = [name for name in ("open", "high", "low", "close", "volume", "amount", "preClose") if name in columns]
    rows = []
    for stamp, values in zip(frame.get("index") or [], frame.get("data") or [], strict=False):
        row: dict[str, Any] = {"date": stamp}
        for name in wanted:
            row[name] = _finite(values[columns.index(name)])
        rows.append(row)
    return rows


def _finite(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _error_body(exc: HTTPError) -> str:
    """读取 Bridge 的错误响应体：真正的故障原因在这里，脱敏并截断后必须交出去。"""
    try:
        raw = exc.read(MAX_ERROR_BODY_CHARS * 4)
    except OSError as read_error:
        return f"<响应体读取失败：{type(read_error).__name__}>"
    text = _mask_account(" ".join(raw.decode("utf-8", "replace").split())) or "<空响应体>"
    return text[:MAX_ERROR_BODY_CHARS] + ("…" if len(text) > MAX_ERROR_BODY_CHARS else "")


def _mask_account(text: str) -> str:
    """错误现场可能带上账户号，落盘和回报前一律替换成占位符。"""
    account_id = os.getenv("MINIQMT_ACCOUNT_ID", "").strip()
    return text.replace(account_id, "<account_id>") if account_id else text


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key.endswith(("accountid", "accountno", "accountnumber", "secuaccount")):
                result["account_id_hash"] = _account_hash(str(item))
            else:
                result[key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _account_hash(account_id: str) -> str:
    return "sha256:" + hashlib.sha256(account_id.encode("utf-8")).hexdigest()


def _success(*, operation: str, data: Any) -> dict[str, Any]:
    return {"ok": True, "status": "success", "operation": operation, "data": data, "error": None}


def _error(code: str, detail: str) -> dict[str, Any]:
    status = "unknown" if code == "unknown" else ("blocked" if code == "blocked" else "error")
    return {"ok": False, "status": status, "operation": None, "data": None, "error": {"code": code, "detail": detail}}


def _intent_committed(result: dict[str, Any]) -> bool:
    """这笔委托是否真正占用了市场（决定它算不算当日买入额度）。

    只有拿到"确定没成交"的正面证据才释放额度，否则一律保守占用——漏计会导致重复下单撞穿上限，
    比偶尔少买一次严重得多：
    - status=unknown（5xx/断网，提交结果未知）：保守占用。
    - HTTP 200 且 Bridge 明确回报 accepted=False：券商拒单、没占市场，释放。
    - 非 unknown 的干净错误（4xx 等请求被拒、根本没提交）：释放。
    - 其余（accepted=True，或旧响应没这个字段的 200）：保守占用。
    """
    status = result.get("status")
    if status == "unknown":
        return True
    if status != "success":
        return False
    data = result.get("data")
    return not (isinstance(data, dict) and data.get("accepted") is False)


def _now() -> str:
    return datetime.now(TRADING_TZ).isoformat(timespec="seconds")


def _load_project_env() -> None:
    """每次宿主读配置前刷新项目 .env，让长驻的下午进程也能立即使用新值。"""
    path = Path.cwd() / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        # 项目 .env 是长驻进程的默认来源；测试或调用方显式改过的值不被刷新覆盖。
        current = os.environ.get(name)
        if current is None or current == _PROJECT_ENV_VALUES.get(name):
            # .env 是长驻进程的配置来源；调用方显式改过的环境值不被覆盖。
            current = os.environ.get(name)
            if current is None or current == _PROJECT_ENV_VALUES.get(name):
                os.environ[name] = value
            _PROJECT_ENV_VALUES[name] = value
        _PROJECT_ENV_VALUES[name] = value


def _required_env(name: str, *, allow_empty: bool = False) -> str:
    _load_project_env()
    value = os.getenv(name)
    if value is None or (not allow_empty and not value.strip()):
        raise ValueError(f"宿主未配置 {name}，请在项目 .env 中填写")
    return value.strip()


def _truthy_env(name: str) -> bool:
    return _required_env(name).lower() in {"1", "true", "yes", "on"}


def _positive_int_env(name: str) -> int:
    raw = _required_env(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"宿主 {name} 配置无效") from exc
    if value <= 0:
        raise ValueError(f"宿主 {name} 必须大于 0")
    return value


def _positive_float_env(name: str) -> float:
    raw = _required_env(name)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"宿主 {name} 配置无效") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"宿主 {name} 必须是大于 0 的有限数")
    return value


def _ratio_env(name: str) -> float:
    value = _positive_float_env(name)
    if value >= 1:
        raise ValueError(f"宿主 {name} 必须小于 1")
    return value


def _is_trading_time(value: datetime) -> bool:
    local = value.astimezone(TRADING_TZ)
    if local.weekday() >= 5:
        return False
    current = local.time()
    return time(9, 30) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0)


def _extract_quote(data: Any, stock_code: str) -> tuple[float, datetime] | None:
    items = data.get("ticks") if isinstance(data, dict) else None
    item = items.get(stock_code) if isinstance(items, dict) else None
    if item is None and isinstance(items, list):
        item = next((row for row in items if isinstance(row, dict) and row.get("stock_code") == stock_code), None)
    if not isinstance(item, dict):
        return None
    price = _first_number(item, ("lastPrice", "last_price", "price", "close"))
    raw_time = item.get("time") or item.get("timestamp") or item.get("quote_time") or item.get("data_time")
    quote_at = _parse_time(raw_time)
    if price is None or price <= 0 or quote_at is None:
        return None
    return price, quote_at


def _extract_asset(data: Any) -> tuple[float, float] | None:
    asset = data.get("asset") if isinstance(data, dict) else None
    if not isinstance(asset, dict):
        return None
    available = _first_number(asset, ("cash", "m_dAvailable", "available_cash", "enable_balance"))
    total = _first_number(asset, ("total_asset", "m_dBalance", "asset", "total_balance", "nav_asset"))
    if available is None or total is None or available < 0 or total <= 0:
        return None
    return available, total


def _extract_position(data: Any, stock_code: str) -> tuple[float, int] | None:
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    item = next((row for row in items if isinstance(row, dict) and str(row.get("stock_code") or "").upper() == stock_code), None)
    if item is None:
        return None
    cost = _first_number(item, ("avg_price", "avg_cost", "m_dOpenPrice", "open_price", "cost_price"))
    volume = _first_number(item, ("can_use_volume", "m_nCanUseVolume", "enable_amount"))
    if cost is None or cost <= 0 or volume is None or volume < 100:
        return None
    return cost, int(volume)


def _contains_order(data: Any, order_id: str) -> bool:
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("order_id") or item.get("broker_order_id") or item.get("m_nOrderID")
        if str(value) == order_id:
            return True
    return False


def _first_number(item: dict[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        value = item.get(name)
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value) / (1000 if value > 10_000_000_000 else 1), tz=TRADING_TZ)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    for parser in (
        lambda: datetime.fromisoformat(raw.replace("Z", "+00:00")),
        lambda: datetime.strptime(raw, "%Y%m%d%H%M%S"),
        lambda: datetime.strptime(raw, "%Y-%m-%d %H:%M:%S"),
        # bridge 的 tick timetag 实际格式，例如 "20260908 11:30:01"。
        lambda: datetime.strptime(raw, "%Y%m%d %H:%M:%S"),
    ):
        try:
            parsed = parser()
            return parsed.replace(tzinfo=TRADING_TZ) if parsed.tzinfo is None else parsed
        except ValueError:
            continue
    return None


# 直接导入宿主模块时也读取项目配置；CLI 和长驻进程会在每次请求前继续刷新。
_load_project_env()
