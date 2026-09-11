"""回测编排：重放一个交易日的全部槽位，问模型拿结论，模拟成交，落报告。

装配、校验、JSON 重试、并行读图全部复用 TradingPipeline 的现成代码——那是打过仗的，
重抄一份必然漂移。标的来自该日已落盘的待观测清单或 --codes 指定；模型请求是真的
DeepSeek 调用，数据和交易是纯本地的，环境锁死 observe，不存在任何真实下单路径。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from minisweagent.backtest import evaluate, replay
from minisweagent.backtest.replay import SESSION_MINUTES, BacktestDataError
from minisweagent.backtest.simulate import COMMISSION_MIN, COMMISSION_RATE, STAMP_TAX_SELL, PaperAccount
from minisweagent.environments.miniqmt import TRADING_TZ, host_limits
from minisweagent.trading.pipeline import PipelineError, TradingPipeline


class BacktestError(RuntimeError):
    """回测跑不起来：标的来源、日期或数据不成立。"""


class BacktestRunner:
    """一天的回测：每个槽位一次重放 + 读图 + 模拟成交，收盘估值出报告。"""

    def __init__(
        self,
        settings: dict[str, Any],
        *,
        sessions_dir: Path,
        journal_dir: Path,
        echo: Callable[[str], None],
        trade_date: date,
        codes: list[str] | None = None,
        at: str | None = None,
        extra_slots: list[str] | None = None,
        initial_cash: float = 100_000.0,
    ):
        self.pipeline = TradingPipeline(settings, sessions_dir=sessions_dir, journal_dir=journal_dir, echo=echo)
        self.sessions_dir = sessions_dir
        self.journal_dir = journal_dir
        self.echo = echo
        self.trade_date = trade_date
        self.codes = codes
        self.at = self._parse_at(at)
        if at and extra_slots:
            raise BacktestError("--at 只跑单个槽位，与额外槽位不能同时指定")
        # 固定间隔对齐出来的网格覆盖不了的时刻（比如尾盘 14:40）从这里补，逐个校验时段。
        self.extra_slots = sorted({self._parse_at(moment) for moment in extra_slots or []} - {None})
        self.initial_cash = initial_cash
        self.account = PaperAccount(initial_cash, host_limits())

    def run(self) -> dict[str, Any]:
        """跑完一天并落报告。单个槽位失败不打死整天，失败跟着数据进报告。"""
        limits = host_limits()
        config = self.pipeline.config
        universe = self._universe()
        codes = list(
            dict.fromkeys(pick["stock_code"] for sector in universe for pick in sector.get("picks") or [])
        )
        if not codes:
            raise BacktestError("回测标的是空的")
        errors: list[str] = []
        daily, intraday = replay.fetch_bars(self.pipeline._data_client(), codes + config.index_codes, self.trade_date, errors)
        index_minutes = intraday.get(config.index_codes[0]) or []
        if not index_minutes:
            raise BacktestDataError(f"指数 {config.index_codes[0]} 没有当日分钟线，无法定位槽位")
        slots = [self.at] if self.at else replay.slot_times(index_minutes, self.trade_date, config.round_interval_minutes)
        if self.extra_slots:
            slots = sorted(set(slots) | set(self.extra_slots))
        if not slots:
            raise BacktestDataError("没有可跑的槽位：分钟线覆盖不到连续竞价时段")
        self.echo(
            f"回测 {self.trade_date.isoformat()}：{len(slots)} 个槽位、{len(codes)} 只标的，"
            f"标的来自{self.universe_source}，初始资金 {self.initial_cash:.2f}"
        )
        slots_out = [self._run_slot(hhmm, daily, intraday, universe, limits) for hhmm in slots]
        records = [record for slot in slots_out for record in slot.pop("records")]
        closes = {
            code: close for code, bars in intraday.items() if bars and (close := _day_close(bars)) is not None
        }
        report = {
            "trade_date": self.trade_date.isoformat(),
            "universe_source": self.universe_source,
            "interval_minutes": config.round_interval_minutes,
            "initial_cash": self.initial_cash,
            "fees": {
                "commission_rate": COMMISSION_RATE,
                "commission_min": COMMISSION_MIN,
                "stamp_tax_sell": STAMP_TAX_SELL,
            },
            "slots": slots_out,
            "verdicts": records,
            "stats": evaluate.action_stats(records),
            "final": self.account.mark_to_market(closes),
            "errors": errors,
        }
        path = self._write_report(report)
        report["report_path"] = str(path)
        evaluate.print_summary(report, self.echo)
        self.echo(f"报告已落盘：{path}")
        return report

    # ---------- 单个槽位 ----------

    def _run_slot(
        self, hhmm: str, daily: dict[str, list], intraday: dict[str, list], universe: list[dict], limits: dict
    ) -> dict[str, Any]:
        """重放一个槽位：装数据包、渲染图、读图、模拟成交。"""
        config = self.pipeline.config
        view = replay.slot_view(
            daily,
            intraday,
            trade_date=self.trade_date,
            hhmm=hhmm,
            stock_codes=list(
                dict.fromkeys(pick["stock_code"] for sector in universe for pick in sector.get("picks") or [])
            ),
            index_codes=config.index_codes,
            max_buy_notional=limits["max_buy_notional"],
        )
        errors = list(view["errors"])
        groups = self._groups(universe, view, errors)
        orders: list[dict] = []
        records: list[dict] = []
        if not groups:
            return {"slot": hhmm, "orders": orders, "records": records, "errors": errors + ["本槽位没有任何可读标的"]}
        trace, session_id = self._trace(hhmm)
        chart_dir = trace.parent / "charts" / hhmm
        for group in groups:
            for stock in group["stocks"]:
                stock["charts"] = replay.render_pair(
                    chart_dir, stock["stock_code"], view["stocks"][stock["stock_code"]], config.daily_chart_days, errors
                )
        index_charts: list[str] = []
        for index in view["indexes"]:
            entry = view["index_data"].get(index["stock_code"])
            if entry is None:
                continue
            index["charts"] = replay.render_pair(
                chart_dir, index["stock_code"], entry, config.daily_chart_days, errors, average=False
            )
            index_charts.extend((index.get("charts") or {}).values())
        pack = {
            "as_of": view["as_of"],
            "trade_date": self.trade_date.isoformat(),
            "indexes": view["indexes"],
            "groups": groups,
            "limits": limits,
            "errors": errors,
        }
        readings: list[dict] = []
        try:
            readings, read_errors = self.pipeline._read_charts(pack, index_charts, trace, session_id)
            errors.extend(read_errors)
        except PipelineError as error:
            errors.append(f"读图全部失败：{error}")
        verdicts = [verdict for reading in readings for verdict in reading.get("verdicts") or []]
        prices = {code: entry["row"]["last_price"] for code, entry in view["stocks"].items()}
        orders, skipped = self.account.apply(hhmm, verdicts, prices)
        records = [self._record(hhmm, verdict, view, intraday) for verdict in verdicts]
        self.echo(
            f"{hhmm} 槽位：{len(groups)} 组、{len(verdicts)} 条结论、成交 {len(orders)} 笔"
            + (f"、跳过 {len(skipped)} 笔" if skipped else "")
        )
        return {
            "slot": hhmm,
            "as_of": view["as_of"],
            "readings": [{"group": reading["group"], "index_view": reading.get("index_view", "")} for reading in readings],
            "orders": orders,
            "skipped": skipped,
            "cash": round(self.account.cash, 2),
            "records": records,
            "errors": errors,
        }

    def _groups(self, universe: list[dict], view: dict, errors: list[str]) -> list[dict]:
        """按实盘 round_context 的规则分组：候选板块剔除已持仓，持仓单独成组。"""
        held = set(self.account.positions)
        groups = []
        for sector in universe:
            stocks = []
            for pick in sector.get("picks") or []:
                code = pick["stock_code"]
                entry = view["stocks"].get(code)
                if code in held or entry is None:
                    continue
                stocks.append({**pick, **entry["row"], "position": None})
            if stocks:
                groups.append({"name": sector["sector"], "kind": "candidate", "note": sector.get("reason", ""), "stocks": stocks})
        if held:
            stocks = []
            for code in sorted(held):
                entry = view["stocks"].get(code)
                if entry is not None:
                    stocks.append(
                        {
                            "stock_code": code,
                            **entry["row"],
                            "position": self.account.position_view(code, entry["row"]["last_price"]),
                        }
                    )
            if stocks:
                groups.append(
                    {
                        "name": "回测持仓",
                        "kind": "holding",
                        "note": "持仓票的唯一判断入口；卖出必须看 can_use_volume（T+1）。",
                        "stocks": stocks,
                    }
                )
            else:
                errors.append("持仓标的本槽位都没有行情")
        return groups

    def _record(self, hhmm: str, verdict: dict, view: dict, intraday: dict[str, list]) -> dict:
        """一条结论加上它事后的走势验证：前向收益来自模型没看到的未来 bar。"""
        code = verdict["stock_code"]
        entry = view["stocks"].get(code)
        price = entry["row"]["last_price"] if entry else None
        record = {
            "slot": hhmm,
            "stock_code": code,
            "action": verdict.get("action"),
            "confidence": verdict.get("confidence"),
            "price": price,
            "reason": str(verdict.get("reason") or "")[:500],
        }
        if price:
            record.update(evaluate.forward_view(intraday.get(code) or [], view["cutoff"], price) or {})
        return record

    # ---------- 装配 ----------

    def _universe(self) -> list[dict]:
        """回测标的：--codes 显式指定，或该日已落盘的待观测清单。"""
        if self.codes:
            self.universe_source = "--codes"
            return [{"sector": "手动指定", "reason": "命令行指定标的", "picks": [{"stock_code": code} for code in self.codes]}]
        path = self.journal_dir / "watchlist" / f"{self.trade_date.isoformat()}.json"
        if not path.is_file():
            raise BacktestError(f"{self.trade_date.isoformat()} 没有待观测清单（{path}），也没有 --codes 指定标的")
        watchlist = json.loads(path.read_text(encoding="utf-8"))
        if watchlist.get("trade_date") != self.trade_date.isoformat():
            raise BacktestError(f"清单里的 trade_date 与回测日期不一致：{watchlist.get('trade_date')}")
        sectors = [sector for sector in watchlist.get("sectors") or [] if sector.get("picks")]
        if not sectors:
            raise BacktestError("清单里没有任何标的")
        self.universe_source = "待观测清单"
        return sectors

    def _parse_at(self, at: str | None) -> str | None:
        if at is None:
            return None
        hour, _, minute = at.partition(":")
        try:
            minutes = int(hour) * 60 + int(minute)
        except ValueError as exc:
            raise BacktestError("--at 需要 HH:MM 时刻") from exc
        if not any(start <= minutes <= end for start, end in SESSION_MINUTES):
            raise BacktestError(f"--at {at} 不在连续竞价时段内")
        return f"{minutes // 60:02d}{minutes % 60:02d}"

    def _trace(self, hhmm: str) -> tuple[Path, str]:
        """轨迹与实盘同一目录结构，观测端直接能看；kind 标 backtest 便于区分。"""
        started = datetime.now(TRADING_TZ)
        session_id = f"{started:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}-backtest-{self.trade_date:%Y%m%d}-{hhmm}"
        return self.sessions_dir / started.strftime("%Y%m%d") / f"{session_id}.json", session_id

    def _write_report(self, report: dict) -> Path:
        path = self.journal_dir / "backtest" / f"{self.trade_date:%Y%m%d}-{datetime.now(TRADING_TZ):%H%M%S}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(path)
        return path


def _day_close(bars: list[dict]) -> float | None:
    """当日最后一根分钟 bar 的收盘价；没有可用 bar 就是 None，估值时用最近成交价兜底。"""
    usable = [bar for bar in bars if isinstance(bar.get("close"), (int, float)) and bar["close"]]
    return float(usable[-1]["close"]) if usable else None
