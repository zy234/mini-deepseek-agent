"""回测编排：day-1 挑一个板块按槽位筛选建仓，之后每天只跑持仓逻辑找安全卖点，跨日结转。

装配、校验、JSON 重试、并行读图全部复用 TradingPipeline 的现成代码——那是打过仗的，重抄一份
必然漂移。回测回答的是实盘那条链真实的盈亏：day-1 一个板块每 25 分钟（省 token，实盘是 10 分钟）
读图给 BUY/SELL、纸面账户按额度/T+1/费率成交建仓；day-2 起不再筛选新票，只把持仓喂回 chart_reader
的 holding 组，每 25 分钟看有没有安全卖出，卖出即兑现。持仓、现金跨日结转，T+1 由 roll_to_next_day
解锁。执行环节是 PaperAccount 的固定规则（不额外跑 execution_manager，省 token）。模型请求是真的
DeepSeek 调用，数据和交易纯本地，环境锁死 observe，没有任何真实下单路径。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from minisweagent.backtest import evaluate, replay
from minisweagent.backtest.replay import SESSION_MINUTES, BacktestDataError
from minisweagent.backtest.simulate import COMMISSION_MIN, COMMISSION_RATE, STAMP_TAX_SELL, PaperAccount
from minisweagent.environments.miniqmt import TRADING_TZ, host_limits
from minisweagent.trading.pipeline import PipelineError, TradingPipeline

# 回测默认槽位间隔：实盘 10 分钟，回测拉到 25 分钟省 token（逻辑不变，只是少跑几个槽位）。
BACKTEST_INTERVAL_MINUTES = 25


class BacktestError(RuntimeError):
    """回测跑不起来：标的来源、日期或数据不成立。"""


class BacktestRunner:
    """多日回测：day-1 一个板块建仓，之后每日只管持仓找卖点，跨日结转，末日估值出报告。"""

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
        through: date | None = None,
        interval: int = BACKTEST_INTERVAL_MINUTES,
        sector: str | None = None,
    ):
        self.pipeline = TradingPipeline(settings, sessions_dir=sessions_dir, journal_dir=journal_dir, echo=echo)
        self.sessions_dir = sessions_dir
        self.journal_dir = journal_dir
        self.echo = echo
        self.trade_date = trade_date  # day-1：唯一筛选建仓的一天
        self.through = through or trade_date  # 最后一个持仓管理日；缺省只跑 day-1
        if self.through < trade_date:
            raise BacktestError("--through 不能早于回测起始日")
        self.interval = interval
        self.sector = sector
        self.codes = codes
        self.at = self._parse_at(at)
        if self.at and self.through != trade_date:
            raise BacktestError("--at 只跑单日单槽位，不能与多日 --through 同时用")
        if at and extra_slots:
            raise BacktestError("--at 只跑单个槽位，与额外槽位不能同时指定")
        self.extra_slots = sorted({self._parse_at(moment) for moment in extra_slots or []} - {None})
        self.initial_cash = initial_cash
        self.account = PaperAccount(initial_cash, host_limits())
        self._current_date = trade_date  # 逐日循环里指向"正在跑的那天"，_run_slot/_trace 都读它
        self._last_closes: dict[str, float] = {}  # 末个交易日的收盘价，给期末估值

    def run(self) -> dict[str, Any]:
        """逐个交易日跑：day-1 建仓、之后管持仓，跨日结转，末日估值落报告。"""
        limits = host_limits()
        config = self.pipeline.config
        day1_universe = self._day1_universe()
        days_out: list[dict] = []
        errors: list[str] = []
        day = self.trade_date
        while day <= self.through:
            screen = not days_out  # 第一个真正跑起来的交易日才筛选建仓
            result = self._run_day(day, screen, day1_universe, limits, config, errors)
            if result is not None:
                days_out.append(result)
                self.account.roll_to_next_day()  # 收盘结转：隔夜持仓 T+1 解锁、当日额度清零
            day += timedelta(days=1)
        if not days_out:
            raise BacktestDataError("范围内没有可回测的交易日（分钟线都取不到）")
        trades = [order for day_out in days_out for order in day_out["orders"]]
        final = self.account.mark_to_market(self._last_closes)
        report = {
            "trade_date": self.trade_date.isoformat(),
            "through": self.through.isoformat(),
            "trading_days": [day_out["date"] for day_out in days_out],
            "universe_source": self.universe_source,
            "interval_minutes": self.interval,
            "initial_cash": self.initial_cash,
            "fees": {
                "commission_rate": COMMISSION_RATE,
                "commission_min": COMMISSION_MIN,
                "stamp_tax_sell": STAMP_TAX_SELL,
            },
            "days": days_out,
            "trades": trades,
            "final": final,
            "errors": errors,
        }
        path = self._write_report(report)
        report["report_path"] = str(path)
        evaluate.print_summary(report, self.echo)
        self.echo(f"报告已落盘：{path}")
        return report

    def _run_day(
        self, day: date, screen: bool, day1_universe: list[dict], limits: dict, config: Any, errors: list[str]
    ) -> dict[str, Any] | None:
        """跑一个交易日的全部槽位。非交易日（分钟线取不到）返回 None，由上层跳过。"""
        self._current_date = day
        universe = day1_universe if screen else []  # day-2 起不筛新票，只有持仓组
        codes = list(dict.fromkeys(pick["stock_code"] for sector in universe for pick in sector.get("picks") or []))
        codes += [code for code in self.account.positions if code not in codes]
        if not codes:
            return None  # 没建成仓、也没有候选：这天没什么可跑
        daily, intraday = replay.fetch_bars(
            None, codes + config.index_codes, day, errors, index_codes=config.index_codes, journal_dir=self.journal_dir
        )
        index_minutes = intraday.get(config.index_codes[0]) or []
        if not index_minutes:
            return None  # 非交易日/停市：没有指数分钟线定位不了槽位
        if self.at and screen:
            slots = [self.at]
        else:
            slots = replay.slot_times(index_minutes, day, self.interval)
        if screen and self.extra_slots:
            slots = sorted(set(slots) | set(self.extra_slots))
        if not slots:
            return None
        self.echo(
            f"{day.isoformat()}（{'建仓' if screen else '持仓'}）：{len(slots)} 个槽位、{len(codes)} 只标的"
        )
        slots_out = [self._run_slot(hhmm, daily, intraday, universe, limits) for hhmm in slots]
        orders = [{**order, "date": day.isoformat()} for slot in slots_out for order in slot["orders"]]
        self._last_closes = {
            code: close for code, bars in intraday.items() if bars and (close := _day_close(bars)) is not None
        }
        return {"date": day.isoformat(), "screen": screen, "slots": slots_out, "orders": orders}

    # ---------- 单个槽位 ----------

    def _run_slot(
        self, hhmm: str, daily: dict[str, list], intraday: dict[str, list], universe: list[dict], limits: dict
    ) -> dict[str, Any]:
        """重放一个槽位：装数据包、渲染图、读图、模拟成交。"""
        config = self.pipeline.config
        view = replay.slot_view(
            daily,
            intraday,
            trade_date=self._current_date,
            hhmm=hhmm,
            stock_codes=list(
                dict.fromkeys(
                    [pick["stock_code"] for sector in universe for pick in sector.get("picks") or []]
                    + list(self.account.positions)
                )
            ),
            index_codes=config.index_codes,
            max_buy_notional=limits["max_buy_notional"],
        )
        errors = list(view["errors"])
        groups = self._groups(universe, view, errors)
        if not groups:
            return {"slot": hhmm, "orders": [], "errors": errors + ["本槽位没有任何可读标的"]}
        trace, session_id = self._trace(hhmm)
        chart_dir = trace.parent / "charts" / hhmm
        for group in groups:
            for stock in group["stocks"]:
                stock["chart"], stock["chart_missing"] = replay.render_pair(
                    chart_dir, stock["stock_code"], view["stocks"][stock["stock_code"]], config.daily_chart_days, errors
                )
        index_charts: list[str] = []
        for index in view["indexes"]:
            entry = view["index_data"].get(index["stock_code"])
            if entry is None:
                continue
            index["chart"], index["chart_missing"] = replay.render_pair(
                chart_dir, index["stock_code"], entry, config.daily_chart_days, errors, average=False
            )
            if index["chart"]:
                index_charts.append(index["chart"])
        pack = {
            "as_of": view["as_of"],
            "trade_date": self._current_date.isoformat(),
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
        self.echo(
            f"  {hhmm}：{len(groups)} 组、{len(verdicts)} 条结论、成交 {len(orders)} 笔"
            + (f"、跳过 {len(skipped)} 笔" if skipped else "")
        )
        return {
            "slot": hhmm,
            "as_of": view["as_of"],
            "readings": [{"group": reading["group"], "index_view": reading.get("index_view", "")} for reading in readings],
            "verdicts": [self._record(hhmm, verdict, view) for verdict in verdicts],
            "orders": orders,
            "skipped": skipped,
            "cash": round(self.account.cash, 2),
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

    def _record(self, hhmm: str, verdict: dict, view: dict) -> dict:
        """一条结论的基础记录；前向收益在 run() 里拉完前向日线后统一补上（T+1 之后才有兑现窗口）。"""
        code = verdict["stock_code"]
        entry = view["stocks"].get(code)
        price = entry["row"]["last_price"] if entry else None
        return {
            "slot": hhmm,
            "stock_code": code,
            "action": verdict.get("action"),
            "confidence": verdict.get("confidence"),
            "price": price,
            "reason": str(verdict.get("reason") or "")[:500],
        }

    # ---------- 装配 ----------

    def _day1_universe(self) -> list[dict]:
        """day-1 建仓标的：只锁一个板块（--codes 手动指定，或清单里 --sector 指定/第一个）。

        回测有意只做一个板块——多板块全跑每槽好几次带图请求，太烧 token。
        """
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
        if self.sector:
            sectors = [sector for sector in sectors if sector["sector"] == self.sector]
            if not sectors:
                raise BacktestError(f"清单里没有板块 {self.sector}")
        chosen = sectors[0]  # 只取一个板块；没指定 --sector 就用清单里的第一个
        self.universe_source = f"待观测清单·{chosen['sector']}"
        self.echo(f"day-1 建仓板块：{chosen['sector']}（{[p['stock_code'] for p in chosen['picks']]}）")
        return [chosen]

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
        session_id = f"{started:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}-backtest-{self._current_date:%Y%m%d}-{hhmm}"
        return self.sessions_dir / started.strftime("%Y%m%d") / f"{session_id}.json", session_id

    def _write_report(self, report: dict) -> Path:
        path = self.journal_dir / "backtest" / f"{self.trade_date:%Y%m%d}-{self.through:%Y%m%d}-{datetime.now(TRADING_TZ):%H%M%S}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(path)
        return path


def _day_close(bars: list[dict]) -> float | None:
    """当日最后一根分钟 bar 的收盘价；没有可用 bar 就是 None，估值时用最近成交价兜底。"""
    usable = [bar for bar in bars if isinstance(bar.get("close"), (int, float)) and bar["close"]]
    return float(usable[-1]["close"]) if usable else None
