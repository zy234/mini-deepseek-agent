"""交易日三阶段流水线：盘前选池、盘中读图、汇总执行。

阶段之间只由宿主传递结构化数据，没有任何 Agent 能调度另一个 Agent：

- 阶段一（默认 09:20）：宿主取板块热度榜和热门板块内部的个股结构，`candidate_scout` 选出
  若干板块各若干只票，落盘成当日待观测清单。
- 阶段二（开盘后每 interval 分钟）：宿主为每只待观测股和大盘渲染日线图与当日分钟图，按板块
  分组并行交给 `chart_reader`，每组一次带图请求，直接给出买卖结论。
- 阶段三：宿主把本轮全部结论、账户快照、当日委托成交、交易账本和硬限额注入 `execution_manager`，
  由它决定并提交交易。

三个阶段的模式完全相同：宿主取数 → 模型判断 → 宿主校验。模型给出的股票代码必须落在宿主注入的
池子里，否则这一步作废重来——凭记忆编出来的代码会让账户买到完全无关的票。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from minisweagent.agents import get_agent
from minisweagent.environments import get_environment
from minisweagent.environments.account_journal import append_account_cycle, append_cycle_fallback
from minisweagent.environments.local import LocalEnvironmentConfig
from minisweagent.environments.miniqmt import TRADING_TZ, MiniQMTClient
from minisweagent.models import get_model
from minisweagent.trading import context
from minisweagent.utils.serialize import recursive_merge

logger = logging.getLogger("minisweagent.trading")

ACTIONS = ("BUY", "SELL", "HOLD")
# 连续竞价时段。收盘前最后一轮跑完就结束当天，尾盘集中竞价不参与。
SESSIONS = ((clock_time(9, 30), clock_time(11, 30)), (clock_time(13, 0), clock_time(15, 0)))
# 盘前清单缺失时的补跑上限：09:20 首跑一次，之后每个槽位各补一次，共 10 次约覆盖 100 分钟，
# 足够等一次 Bridge/QMT 抖动恢复，又不至于在真故障时空转到收盘。
MAX_PREMARKET_ATTEMPTS = 10


class PipelineError(RuntimeError):
    """某一阶段拿不到可用结果。这一轮明确失败，不许拿残缺结论继续往下走。"""


class TradingConfig(BaseModel):
    """流水线节奏与规模。改这些不需要碰代码，但它们不是 prompt，放在配置的 trading 段。"""

    premarket_at: str = "09:20"
    round_interval_minutes: int = Field(default=10, ge=1, le=60)
    sector_count: int = Field(default=5, ge=1, le=10)
    picks_per_sector: int = Field(default=2, ge=1, le=5)
    sectors_scanned: int = Field(default=8, ge=1, le=20)
    rows_per_sector: int = Field(default=8, ge=1, le=20)
    index_codes: list[str] = Field(default_factory=lambda: ["000001.SH", "399006.SZ"])
    daily_chart_days: int = Field(default=30, ge=10, le=120)
    max_parallel_groups: int = Field(default=6, ge=1, le=12)
    round_json_attempts: int = Field(default=2, ge=1, le=4)

    def premarket_time(self) -> clock_time:
        hour, _, minute = self.premarket_at.partition(":")
        return clock_time(int(hour), int(minute))


class TradingPipeline:
    """一个交易日的全部编排。每一轮都新建 Agent，跨轮状态只从账本、待观测清单和账户恢复。"""

    def __init__(self, settings: dict[str, Any], *, sessions_dir: Path, journal_dir: Path, echo: Callable[[str], None]):
        self.settings = settings
        self.config = TradingConfig(**(settings.get("trading") or {}))
        self.sessions_dir = sessions_dir
        self.journal_dir = journal_dir
        self.echo = echo

    # ---------- 阶段一：盘前选池 ----------

    def premarket(self) -> dict[str, Any]:
        """取盘前数据、选出待观测清单并落盘。清单是当天阶段二的唯一输入。"""
        started = datetime.now(TRADING_TZ)
        pack = context.premarket_context(
            self._data_client(),
            journal_dir=self.journal_dir,
            index_codes=self.config.index_codes,
            sectors_scanned=self.config.sectors_scanned,
            rows_per_sector=self.config.rows_per_sector,
        )
        self.echo(
            f"盘前数据就绪：{len(pack['sector_candidates'])} 个热门板块，"
            f"{sum(len(item['rows']) for item in pack['sector_candidates'])} 只候选行情"
            + (f"；取数警告 {len(pack['errors'])} 条" if pack["errors"] else "")
        )
        trace, session_id = self._trace(started, "premarket")
        agent = self._build_agent("candidate_scout", trace=trace, session_id=session_id, cycle_kind="premarket")
        pool = _candidate_pool(pack)
        payload = self._ask_json(
            agent,
            task=(
                f"交易日 {pack['trade_date']} 盘前 {pack['as_of']} 选池：从注入的热门板块里选出"
                f"{self.config.sector_count} 个板块，每个板块 {self.config.picks_per_sector} 只票。"
            ),
            validate=lambda data: _validate_watchlist(data, pool, self.config),
            template_vars={
                "as_of": pack["as_of"],
                "trade_date": pack["trade_date"],
                "sector_count": self.config.sector_count,
                "picks_per_sector": self.config.picks_per_sector,
                "indexes": context.block(pack["indexes"]),
                "sector_ranks": context.block(pack["sector_ranks"]),
                "sector_candidates": context.block(pack["sector_candidates"]),
                "account": context.block(pack["account"]),
                "limits": context.block(pack["limits"]),
                "journal_previous": pack["journal"]["previous"],
                "data_errors": context.block(pack["errors"]),
            },
        )
        watchlist = {
            "trade_date": pack["trade_date"],
            "generated_at": pack["as_of"],
            "market_view": str(payload.get("market_view") or ""),
            "sectors": payload["sectors"],
            "data_errors": pack["errors"],
        }
        self._write_watchlist(watchlist)
        append_account_cycle(
            self.journal_dir,
            f"premarket-{started.strftime('%H%M%S')}",
            {
                "action": "REVIEW",
                "market_view": watchlist["market_view"][:2000],
                "account_risk": f"可用资金 {pack['account']['asset']['available_cash']}，持仓 {len(pack['account']['positions'])} 只",
                "decision": "盘前待观测清单：" + "；".join(
                    f"{sector['sector']}→{'/'.join(pick['stock_code'] for pick in sector['picks'])}"
                    for sector in watchlist["sectors"]
                ),
                "follow_up": "开盘后每轮渲染日线与分钟图交给读图 Agent。",
                "orders": [],
                "pitfalls": [],
                "tool_errors": pack["errors"],
            },
        )
        self.echo(f"待观测清单已落盘：{self._watchlist_path(watchlist['trade_date'])}")
        return watchlist

    # ---------- 阶段二与阶段三：盘中一轮 ----------

    def run_round(self) -> dict[str, Any]:
        """一轮 = 并行读图 + 一次汇总执行。读图组失败不打死整轮，但失败必须进汇总的输入。"""
        started = datetime.now(TRADING_TZ)
        watchlist = self._read_watchlist(started.date())
        trace, session_id = self._trace(started, "round")
        pack = context.round_context(
            self._data_client(),
            watchlist=watchlist,
            journal_dir=self.journal_dir,
            index_codes=self.config.index_codes,
            chart_dir=trace.parent / "charts" / started.strftime("%H%M%S"),
            daily_days=self.config.daily_chart_days,
        )
        index_charts = [path for index in pack["indexes"] for path in (index.get("charts") or {}).values()]
        self.echo(
            f"{started.strftime('%H:%M:%S')} 本轮 {len(pack['groups'])} 组、"
            f"{sum(len(group['stocks']) for group in pack['groups'])} 只标的，图已渲染"
        )
        readings, errors = self._read_charts(pack, index_charts, trace, session_id)
        errors.extend(pack["errors"])
        result = self._execute(pack, readings, errors, trace, session_id, started)
        return {"readings": readings, "errors": errors, "result": result}

    def _read_charts(
        self, pack: dict[str, Any], index_charts: list[str], trace: Path, session_id: str
    ) -> tuple[list[dict], list[str]]:
        """每组一个独立 Agent 和独立轨迹，并行发请求。组之间没有共享状态，所以可以直接并行。"""
        groups = pack["groups"]
        workers = min(self.config.max_parallel_groups, len(groups))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(self._read_group, group, pack, index_charts, trace, session_id, position)
                for position, group in enumerate(groups, 1)
            ]
            outcomes = [future.result() for future in futures]
        readings, errors = [], []
        for group, (verdicts, error) in zip(groups, outcomes, strict=True):
            if error:
                errors.append(f"读图组 {group['name']} 失败：{error}")
                continue
            readings.append(verdicts)
        if not readings:
            raise PipelineError(f"本轮全部读图组失败：{'；'.join(errors)}")
        return readings, errors

    def _read_group(
        self,
        group: dict[str, Any],
        pack: dict[str, Any],
        index_charts: list[str],
        trace: Path,
        session_id: str,
        position: int,
    ) -> tuple[dict[str, Any], str]:
        """一个板块（或持仓组）一次带图请求。返回结论或错误文本，不在这里抛。"""
        child = trace.with_name(f"{trace.stem}-{position:02d}-chart_reader.json")
        codes = [stock["stock_code"] for stock in group["stocks"]]
        images = index_charts + [
            path for stock in group["stocks"] for path in (stock.get("charts") or {}).values()
        ]
        agent = self._build_agent(
            "chart_reader",
            trace=child,
            session_id=f"{session_id}-{position:02d}",
            cycle_kind="intraday",
            parent=session_id,
            label=group["name"],
        )
        try:
            payload = self._ask_json(
                agent,
                task=(
                    f"{pack['as_of']} 读图判断：分组 {group['name']}（{group['kind']}），"
                    f"标的 {'、'.join(codes)}。每只票都要给出结论。"
                ),
                validate=lambda data: _validate_verdicts(data, codes),
                images=images,
                template_vars={
                    "as_of": pack["as_of"],
                    "group_name": group["name"],
                    "group_kind": group["kind"],
                    "group_note": group.get("note", ""),
                    "group": context.block(group),
                    "indexes": context.block(pack["indexes"]),
                    "limits": context.block(pack["limits"]),
                    "image_order": context.block(
                        [Path(path).name for path in images]
                    ),
                },
            )
        except Exception as error:  # 一个组失败不能打死整轮，但失败要带着现场进汇总输入
            logger.exception("读图组 %s 失败", group["name"])
            return {}, f"{type(error).__name__}: {error}"
        return {"group": group["name"], "kind": group["kind"], **payload}, ""

    def _execute(
        self,
        pack: dict[str, Any],
        readings: list[dict],
        errors: list[str],
        trace: Path,
        session_id: str,
        started: datetime,
    ) -> dict[str, Any]:
        """汇总执行。这是唯一有交易工具的角色，账本和账户状态都注入给它。"""
        cycle_id = f"round-{started.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        environment_settings = recursive_merge(
            self.settings.get("environment", {}),
            {"account_journal_dir": str(self.journal_dir), "account_cycle_id": cycle_id},
        )
        agent = self._build_agent(
            "execution_manager",
            trace=trace,
            session_id=session_id,
            cycle_kind="intraday",
            environment_settings=environment_settings,
        )
        task = (
            f"{pack['as_of']} 汇总执行，周期 {cycle_id}。按注入的读图结论、账户状态、当日委托成交、"
            "交易账本和硬限额做最终决策，并用 miniqmt_trade 执行你决定要做的交易；"
            f"本轮 client_intent_id 统一用前缀 {cycle_id}- 加股票代码。"
        )
        try:
            result = agent.run(
                task,
                as_of=pack["as_of"],
                trade_date=pack["trade_date"],
                readings=context.block(readings),
                indexes=context.block(pack["indexes"]),
                account=context.block(pack["account"]),
                limits=context.block(pack["limits"]),
                journal_today=pack["journal"]["today"],
                data_errors=context.block(errors),
                cycle_id=cycle_id,
            )
        except Exception as error:
            logger.exception("汇总执行失败 cycle=%s trace=%s", cycle_id, trace)
            result = {
                "exit_status": type(error).__name__,
                "submission": f"汇总执行异常：{type(error).__name__}: {error}（轨迹 {trace}）",
            }
        # 账本必须有本轮记录：模型没写就宿主补写，否则下一轮读账本会以为这一轮没跑。
        append_cycle_fallback(self.journal_dir, cycle_id, result.get("submission", ""), result.get("exit_status", "unknown"))
        self.echo(f"{datetime.now(TRADING_TZ).strftime('%H:%M:%S')} 本轮结束：{result.get('exit_status', 'unknown')}")
        return result

    # ---------- 交易日循环 ----------

    def run_day(self) -> None:
        """按时钟推进一个交易日：先跑盘前，再按固定间隔跑盘中轮次，收盘后返回。

        盘前失败不锁死全天：清单缺失时 09:20 首跑一次，之后每个槽位补跑一次，
        上限 MAX_PREMARKET_ATTEMPTS 次。盘中迟到启动（Bridge 抖动恢复后重启进程）
        从下一个槽位的补跑开始，watchlist 已存在则直接进盘中轮次。
        """
        finished_slots: set[str] = set()
        attempts = 0
        # 上次盘前尝试对应的触发点（"premarket" 或槽位名）：同一触发点 5 秒一圈的循环里只试一次。
        attempted_key: str | None = None
        while True:
            now = datetime.now(TRADING_TZ)
            if now.weekday() >= 5:
                self.echo("周末不开盘，退出。")
                return
            if now.time() > SESSIONS[-1][1]:
                # 收盘后直接结束，包括"启动就已经过了收盘"这种迟到启动：否则会空转到明天。
                self.echo(f"已过收盘时间，交易日结束，本日跑了 {len(finished_slots)} 轮。")
                return
            slot = self._round_slot(now)
            # premarket 触发点只存在于 09:20 到开盘之间；开盘后由槽位接管，午休和收盘后都是 None。
            key = slot or (
                "premarket" if self.config.premarket_time() <= now.time() < SESSIONS[0][0] else None
            )
            missing = self._read_watchlist_or_none(now.date()) is None
            if key is not None and key != attempted_key:
                attempted_key = key
                if not missing:
                    if key == "premarket":
                        self.echo(f"今日待观测清单已存在，跳过盘前：{self._watchlist_path(now.date().isoformat())}")
                elif attempts < MAX_PREMARKET_ATTEMPTS:
                    attempts += 1
                    self._guarded(self.premarket, f"盘前选池（第 {attempts}/{MAX_PREMARKET_ATTEMPTS} 次）")
                else:
                    self.echo(f"盘前选池补跑已达 {MAX_PREMARKET_ATTEMPTS} 次上限，剩余槽位跳过。")
            # 清单落盘后当轮立即接上盘中：补跑成功不必等下一个槽位。
            if slot and slot not in finished_slots and self._read_watchlist_or_none(now.date()) is not None:
                finished_slots.add(slot)
                self._guarded(self.run_round, f"{slot} 盘中轮次")
            time.sleep(5)

    def _round_slot(self, now: datetime) -> str | None:
        """当前时刻属于哪个轮次槽位；不在连续竞价时段返回 None。槽位按固定间隔对齐时钟。"""
        current = now.time()
        if not any(start <= current <= end for start, end in SESSIONS):
            return None
        minute = now.minute - now.minute % self.config.round_interval_minutes
        return f"{now.hour:02d}{minute:02d}"

    def _guarded(self, action: Callable[[], Any], what: str) -> None:
        """单轮失败不终止整天：把现场写进日志和终端，等下一个槽位继续。"""
        try:
            action()
        except Exception as error:
            logger.exception("%s 失败", what)
            self.echo(f"[失败] {what}：{type(error).__name__}: {error}")

    # ---------- 装配与校验 ----------

    def _data_client(self) -> MiniQMTClient:
        """取数专用客户端，固定 observe 模式：这条路径永远不该下单。

        Bridge 地址和超时走 `LocalEnvironmentConfig` 解析，不在这里重写一遍默认值——
        那份默认值会读 `MINIQMT_BRIDGE_URL`，各写一份的结果就是取数连本机、交易连远端。
        """
        environment = LocalEnvironmentConfig(**(self.settings.get("environment") or {}))
        return MiniQMTClient(
            base_url=environment.miniqmt_bridge_url or os.environ["MINIQMT_BRIDGE_URL"],
            timeout=environment.timeout,
            mode="observe",
            state_dir=self.journal_dir,
            cycle_id="data",
        )

    def _build_agent(
        self,
        role: str,
        *,
        trace: Path,
        session_id: str,
        cycle_kind: str,
        parent: str = "",
        label: str = "",
        environment_settings: dict[str, Any] | None = None,
    ) -> Any:
        profile = dict((self.settings.get("agents") or {}).get(role) or {})
        if not profile:
            raise PipelineError(f"配置里没有角色 {role}")
        profile.pop("description", None)
        agent_settings = recursive_merge(
            self.settings.get("agent", {}),
            profile,
            {
                "agent_name": role,
                "output_path": trace,
                "session_id": session_id,
                "session_started_at": datetime.now(TRADING_TZ).isoformat(timespec="seconds"),
                "session_cwd": str(Path.cwd()),
                "cycle_kind": cycle_kind,
                "parent_session_id": parent,
                "session_label": label,
            },
        )
        model_settings = dict(self.settings.get("model", {}))
        # 并行读图时多个流会交错刷屏，所以除了汇总执行都关掉流式输出。
        model_settings["stream_output"] = bool(model_settings.get("stream_output")) and role == "execution_manager"
        environment = get_environment(
            environment_settings
            if environment_settings is not None
            # 取数和读图角色不需要任何工具，环境固定 observe，连误触交易的可能都不留。
            else recursive_merge(self.settings.get("environment", {}), {"miniqmt_mode": "observe"})
        )
        return get_agent(get_model(model_settings), environment, agent_settings)

    def _ask_json(
        self,
        agent: Any,
        *,
        task: str,
        validate: Callable[[dict], dict],
        template_vars: dict[str, Any],
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """问一次并解析校验；不合格就把原因回传给模型重来，用完次数就抛。

        校验和解析共用一个重试循环：对模型来说"输出不是 JSON"和"代码不在池子里"是同一类错误，
        都是它自己能改的，没必要给两套处理路径。
        """
        result = agent.run(task, images=images, **template_vars)
        for attempt in range(1, self.config.round_json_attempts + 1):
            if result.get("exit_status") != "Submitted":
                raise PipelineError(f"{agent.config.agent_name} 未正常提交：{result.get('exit_status')}")
            try:
                return validate(json.loads(result.get("submission") or ""))
            except (ValueError, KeyError) as error:
                if attempt >= self.config.round_json_attempts:
                    raise PipelineError(f"{agent.config.agent_name} 输出不可用：{error}") from error
                logger.warning("%s 输出不可用，重试：%s", agent.config.agent_name, error)
                result = agent.continue_run(f"上一次输出不可用：{error}。请只输出符合要求的 JSON 对象，不要解释。")
        raise PipelineError("不可达")  # pragma: no cover

    def _trace(self, started: datetime, kind: str) -> tuple[Path, str]:
        session_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}-{kind}"
        return self.sessions_dir / started.strftime("%Y%m%d") / f"{session_id}.json", session_id

    def _watchlist_path(self, trade_date: str) -> Path:
        return self.journal_dir / "watchlist" / f"{trade_date}.json"

    def _write_watchlist(self, watchlist: dict[str, Any]) -> None:
        path = self._watchlist_path(watchlist["trade_date"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(watchlist, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _read_watchlist(self, trade_date: date) -> dict[str, Any]:
        watchlist = self._read_watchlist_or_none(trade_date)
        if watchlist is None:
            raise PipelineError(f"没有 {trade_date.isoformat()} 的待观测清单，先跑一次盘前选池")
        return watchlist

    def _read_watchlist_or_none(self, trade_date: date) -> dict[str, Any] | None:
        path = self._watchlist_path(trade_date.isoformat())
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def _candidate_pool(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """板块 → 该板块可选代码集合。模型只能从这里挑，编不出池子外的代码。"""
    return {
        item["sector"]: {row["stock_code"]: row for row in item["rows"]}
        for item in pack["sector_candidates"]
    }


def _validate_watchlist(data: Any, pool: dict[str, dict[str, Any]], config: TradingConfig) -> dict[str, Any]:
    """校验盘前清单：板块必须来自注入的热度榜，个股必须来自该板块注入的行情行且可买。"""
    if not isinstance(data, dict):
        raise ValueError("输出必须是 JSON 对象")
    sectors = data.get("sectors")
    if not isinstance(sectors, list) or not 1 <= len(sectors) <= config.sector_count:
        raise ValueError(f"sectors 必须是 1 到 {config.sector_count} 个板块的数组")
    seen: set[str] = set()
    cleaned = []
    for sector in sectors:
        if not isinstance(sector, dict):
            raise ValueError("sectors 每一项必须是对象")
        name = sector.get("sector")
        if name not in pool:
            raise ValueError(f"板块 {name} 不在注入的候选板块里，可选：{'、'.join(pool)}")
        picks = sector.get("picks")
        if not isinstance(picks, list) or not 1 <= len(picks) <= config.picks_per_sector:
            raise ValueError(f"板块 {name} 的 picks 必须是 1 到 {config.picks_per_sector} 只")
        chosen = []
        for pick in picks:
            if not isinstance(pick, dict):
                raise ValueError("picks 每一项必须是对象")
            code = pick.get("stock_code")
            row = pool[name].get(code)
            if row is None:
                raise ValueError(f"{code} 不在板块 {name} 注入的行情里，不许凭记忆填代码")
            if not row.get("buyable"):
                raise ValueError(f"{code} 宿主标记为不可买（{row.get('unbuyable', '')}），不能选它")
            if code in seen:
                raise ValueError(f"{code} 被选了两次")
            seen.add(code)
            chosen.append(
                {
                    "stock_code": code,
                    "reason": str(pick.get("reason") or ""),
                    "risk": str(pick.get("risk") or ""),
                    "trend_gate": row.get("trend_gate", ""),
                    "premarket_price": row.get("last_price"),
                }
            )
        cleaned.append({"sector": name, "reason": str(sector.get("reason") or ""), "picks": chosen})
    return {**data, "sectors": cleaned}


def _validate_verdicts(data: Any, codes: list[str]) -> dict[str, Any]:
    """校验读图结论：每只票恰好一条结论，动作只能是三种，代码不能超出本组。"""
    if not isinstance(data, dict):
        raise ValueError("输出必须是 JSON 对象")
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, list):
        raise ValueError("verdicts 必须是数组")
    seen = {}
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            raise ValueError("verdicts 每一项必须是对象")
        code = verdict.get("stock_code")
        if code not in codes:
            raise ValueError(f"{code} 不在本组标的里，本组只有 {'、'.join(codes)}")
        if verdict.get("action") not in ACTIONS:
            raise ValueError(f"{code} 的 action 必须是 {'、'.join(ACTIONS)}")
        if code in seen:
            raise ValueError(f"{code} 给了两条结论")
        seen[code] = verdict
    missing = [code for code in codes if code not in seen]
    if missing:
        raise ValueError(f"这些标的没有结论：{'、'.join(missing)}")
    return {**data, "verdicts": [seen[code] for code in codes]}




