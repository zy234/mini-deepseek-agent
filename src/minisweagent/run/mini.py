#!/usr/bin/env python3
"""Run the single-model DeepSeek Bash agent."""

import json
import logging
import os
import plistlib
import re
import secrets
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from datetime import time as clock_time
from pathlib import Path
from typing import Any

import typer
from prompt_toolkit import prompt as terminal_prompt
from rich.console import Console

from minisweagent.agents import get_agent
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.environments.account_journal import (
    append_account_cycle,
    append_cycle_fallback,
    read_observation_todo,
)
from minisweagent.environments.market_monitor import MarketMonitor
from minisweagent.models import get_model
from minisweagent.utils.cli_display import clear_recent_full_blocks, render_recent_full_blocks
from minisweagent.utils.serialize import UNSET, recursive_merge

DEFAULT_CONFIG_FILE = Path(
    os.getenv("MSWEA_MINI_CONFIG_PATH", builtin_config_dir / "deepseek.yaml")
)
console = Console(highlight=False)
app = typer.Typer(add_completion=False)
TRADING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
logger = logging.getLogger("minisweagent.run")


def _load_dotenv(path: Path | None = None) -> None:
    """读取当前目录的简单 .env；已有 shell 环境变量优先。"""
    env_path = path or Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _select_agent_name(profiles: dict, requested: str | None) -> str:
    """校验指定角色，或在交互终端显示简单的编号选择。"""
    if not profiles:
        raise ValueError("配置中没有 agents")
    if requested:
        if requested not in profiles:
            raise ValueError(f"未知 Agent：{requested}；可选值：{', '.join(profiles)}")
        return requested
    if not sys.stdin.isatty():
        raise ValueError("非交互运行必须使用 --agent 指定角色")

    console.print("[bold]选择 Agent[/bold]")
    names = list(profiles)
    for index, name in enumerate(names, 1):
        description = profiles[name].get("description", "")
        suffix = f" - {description}" if description else ""
        console.print(f"  {index}. {name}{suffix}")
    while True:
        choice = terminal_prompt("Agent: ").strip()
        if choice in profiles:
            return choice
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            return names[int(choice) - 1]
        console.print("请输入角色名称或列表中的编号。")


def _require_agent(settings: dict, role: str) -> dict:
    """宿主的调度入口写死了角色名；配置里没有它时给出明确错误，而不是一个裸 KeyError。"""
    profile = (settings.get("agents") or {}).get(role)
    if profile is None:
        raise ValueError(f"当前调度需要角色 {role}，但配置的 agents 里没有它")
    return dict(profile)


def _get_agent_settings(settings: dict, agent_name: str) -> dict:
    """将公共 Agent 配置与所选角色配置合并。"""
    profile = _require_agent(settings, agent_name)
    profile.pop("description", None)
    return recursive_merge(settings.get("agent", {}), profile, {"agent_name": agent_name})


def _delegate_profiles(settings: dict, agent_settings: dict) -> dict[str, dict]:
    """按角色声明的 delegates_to 收集可委派子角色的完整 profile。

    引用不存在的角色是配置错误，必须在启动时就炸掉；等到模型真去 agent_call 才失败，
    那一轮的研究和组合上下文已经白跑了。
    """
    profiles = {}
    for role in agent_settings.get("delegates_to") or []:
        profile = (settings.get("agents") or {}).get(role)
        if profile is None:
            raise ValueError(f"delegates_to 引用了不存在的 Agent：{role}")
        profiles[role] = dict(profile)
    if profiles and "agent_call" not in (agent_settings.get("tools") or []):
        # 否则 delegates_to 静默失效：模型看不到 agent_call，宿主却准备好了一整套子 Agent。
        raise ValueError(f"{agent_settings.get('agent_name', '该角色')} 声明了 delegates_to，但 tools 里没有 agent_call")
    return profiles


def _new_session_record() -> tuple[Path, str, str]:
    """为一次 CLI 会话生成可排序且低碰撞的轨迹路径和元数据。"""
    started_at = datetime.now().astimezone()
    day = started_at.strftime("%Y%m%d")
    timestamp = started_at.strftime("%Y%m%d-%H%M%S-%f")
    session_id = f"{timestamp}-{secrets.token_hex(4)}"
    path = Path.cwd() / ".sessions" / day / f"{session_id}.json"
    return path, session_id, started_at.isoformat()


def _submission_was_streamed(agent: Any) -> bool:
    if not getattr(agent.model.config, "stream_output", False):
        return False
    previous = agent.messages[-2] if len(agent.messages) >= 2 else {}
    return previous.get("role") == "assistant" and not previous.get("extra", {}).get("actions")


def _print_result(result: dict, *, submission_streamed: bool) -> None:
    """Print a final answer only when it was not already emitted by streaming."""
    status = result.get("exit_status", "unknown")
    if status != "Submitted":
        console.print(f"[bold]{status}[/bold]")
    if result.get("submission") and not submission_streamed:
        console.print(result["submission"])


def _run_session(agent: Any, task: str, *, interactive: bool) -> None:
    clear_recent_full_blocks()
    result = agent.run(task)
    _print_result(result, submission_streamed=_submission_was_streamed(agent))
    if not interactive:
        return
    while True:
        try:
            request = terminal_prompt("继续提问（/open 展开，/exit 退出）: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if request in {"/exit", "/quit"}:
            return
        if request in {"/open", "\x0f"}:
            render_recent_full_blocks()
            continue
        if not request:
            continue
        clear_recent_full_blocks()
        result = agent.continue_run(request)
        _print_result(result, submission_streamed=_submission_was_streamed(agent))


def _account_cycle(
    settings: dict,
    task: str,
    *,
    close_review: bool = False,
    premarket: bool = False,
    midday_review: bool = False,
    intraday_scan: bool = False,
) -> None:
    """每次创建全新 Agent；跨轮状态只从每日账本和状态库恢复。"""
    started = datetime.now(TRADING_TZ)
    cycle_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
    # 调度来源写进轨迹：观测端要按窗口对比同一角色的表现，不能靠 grep 任务文本反推。
    cycle_kind = (
        "premarket"
        if premarket
        else "midday"
        if midday_review
        else "intraday_scan"
        if intraday_scan
        else "close_review"
        if close_review
        else "auto_cycle"
    )
    agent_name = "financial_manager"
    agent_settings = _get_agent_settings(settings, agent_name)
    session_output, session_id, session_started_at = _new_session_record()
    agent_settings = recursive_merge(
        agent_settings,
        {
            "output_path": session_output,
            "session_id": session_id,
            "session_started_at": session_started_at,
            "session_cwd": str(Path.cwd()),
            "cycle_kind": cycle_kind,
        },
    )
    journal_dir = Path(os.getenv("MINIQMT_AGENT_STATE_DIR", ".sessions/account-manager"))
    if not journal_dir.is_absolute():
        journal_dir = Path.cwd() / journal_dir
    environment_settings = recursive_merge(
        settings.get("environment", {}),
        {
            # 盘中扫描同样只读：下单权只留给监控触发那条路径，扫描轮只能改布防。
            "miniqmt_mode": "observe"
            if close_review or premarket or midday_review or intraday_scan
            else "auto_execute",
            "account_journal_dir": str(journal_dir),
            "account_cycle_id": cycle_id,
            "account_review_mode": close_review,
        },
    )
    environment_settings = recursive_merge(
        environment_settings,
        {
            "agent_profiles": _delegate_profiles(settings, agent_settings),
            "agent_common_config": settings.get("agent", {}),
            "agent_model_config": settings.get("model", {}),
            "agent_trace_prefix": str(session_output.with_suffix("")),
            "agent_session_id": session_id,
            "agent_cycle_kind": cycle_kind,
        },
    )
    if premarket:
        task = (
            f"盘前分析模式（只读，不得下单），交易日 {started.date().isoformat()}，实际启动时间 {started.isoformat()}；"
            "必须以 account_journal.previous 中前一交易日收盘记录为基准，结合今日新闻和账户持仓完成研究、组合取舍，"
            f"research_as_of 和 data_cutoff 使用实际启动时间，不过滤启动前已经获得的新信息；并将后续行情触发计划写入 account_monitor：{task}"
        )
    elif midday_review:
        task = (
            f"12:50 午盘前计划复核模式（只读，不得下单），交易日 {started.date().isoformat()}；"
            "先读取 account_journal.today 和 account_monitor 当前计划，再结合上午收盘行情、上午新增新闻和最新账户状态，"
            f"重新调用研究与组合 Agent；保留、修改或撤销计划后，用 account_monitor replace 写入完整新计划：{task}"
        )
    elif intraday_scan:
        task = (
            f"盘中候选发现模式（只读，不得下单），交易日 {started.date().isoformat()}，实际启动 {started.isoformat()}；"
            "先读取 account_journal.today 和 account_monitor 当前计划，再用 miniqmt_sector_rank 看当日板块热度定主线，"
            "从主线板块内部用 miniqmt_screen 配 enrich_trend 找 buyable=true 且 trend_gate=breakout 的票；"
            "不要从个股涨幅榜捡票，那张榜前排要么涨停封死要么一手成本超过单笔上限。"
            "这一轮只找当日盘中新出现的突破机会，"
            "不是重做盘前研究：没有 breakout 就直接结束，不要为了交差降格用 holding 或 extended 的票。"
            "找到候选才调用 financial_research 和 portfolio_manager 做取舍，并且必须与现有最弱持仓对比，"
            "只有结构质量压倒性更优才建仓，同时在 BUY 计划里用 rotate_from 声明资金来自哪只卖出。"
            "重算监控表时只允许新增 BUY 或收紧 SELL，已有 SELL 计划一律不得放宽或删除；"
            f"plan_id 沿用未触发计划的原值，避免已成交的计划被复活：{task}"
        )
    elif close_review:
        task = (
            f"收盘复盘模式（只读，不得下单），交易日 {started.date().isoformat()}；"
            f"必须以 account_journal.previous 中前一交易日收盘记录为基准：{task}"
        )
    else:
        task = (
            f"自主账户交易周期 {cycle_id}，交易日 {started.date().isoformat()}；"
            "先以 account_journal.previous 的前一交易日收盘作为研究基准，再研究今日方案："
            f"{task}"
        )
    model = get_model(settings.get("model", {}))
    environment = get_environment(environment_settings)
    agent = get_agent(model, environment, agent_settings)
    try:
        result = agent.run(task)
    except Exception as exc:
        # 主 Agent 崩溃时把完整 traceback 和轨迹路径写到 stderr，账本里只记结论不足以复盘。
        logger.error(
            "%s 账户管理周期异常 cycle=%s trace=%s\n%s",
            datetime.now(TRADING_TZ).isoformat(),
            cycle_id,
            session_output,
            "".join(traceback.format_exception(exc)).strip(),
        )
        result = {
            "exit_status": type(exc).__name__,
            "submission": f"账户管理周期异常：{type(exc).__name__}: {exc}（轨迹 {session_output}）",
        }
    append_cycle_fallback(
        environment.config.account_journal_dir,
        cycle_id,
        result.get("submission", ""),
        result.get("exit_status", "unknown"),
    )
    _print_result(result, submission_streamed=_submission_was_streamed(agent))


def _run_trade_trigger(settings: dict, event: dict[str, Any], journal_dir: Path) -> dict[str, Any]:
    """行情触发后只启动 account_trader，不重新调用研究或组合 Agent。"""
    role = "account_trader"
    profile = _require_agent(settings, role)
    profile.pop("description", None)
    session_output, session_id, session_started_at = _new_session_record()
    cycle_id = f"trigger-{event['plan']['plan_id']}-{secrets.token_hex(4)}"
    agent_settings = recursive_merge(
        settings.get("agent", {}),
        profile,
        {
            "agent_name": role,
            "output_path": session_output,
            "session_id": session_id,
            "session_started_at": session_started_at,
            "session_cwd": str(Path.cwd()),
            "cycle_kind": "monitor_trigger",
        },
    )
    environment_settings = recursive_merge(
        settings.get("environment", {}),
        {
            "miniqmt_mode": "auto_execute",
            "account_journal_dir": str(journal_dir),
            "account_cycle_id": cycle_id,
            "account_review_mode": False,
        },
    )
    task = (
        "行情监控触发交易审核，不需要重新研究或调用其他 Agent。"
        f"触发计划：{json.dumps(event['plan'], ensure_ascii=False, sort_keys=True)}；"
        f"当前行情：stock_code={event['stock_code']}，price={event['price']}，quote_at={event['quote_at']}。"
        f"本次交易使用稳定 client_intent_id=monitor-{event['plan']['plan_id']}。"
        "请查询账户和当前行情，独立完成 risk_check；只有风险通过才按计划的数量提交，"
        "BUY 用 price_cap=trigger.upper 让宿主按最新价推导限价，SELL 用计划给的 order.price；"
        "提交后用 miniqmt_account 的 orders/trades 复核实际成交量并报告 filled_volume。监控计划由主 Agent 维护，你不要改。"
    )
    try:
        model = get_model({**settings.get("model", {}), "stream_output": False})
        environment = get_environment(environment_settings)
        agent = get_agent(model, environment, agent_settings)
        result = agent.run(task)
    except Exception as exc:
        logger.error(
            "%s 交易触发处理异常 cycle=%s plan=%s trace=%s\n%s",
            datetime.now(TRADING_TZ).isoformat(),
            cycle_id,
            event["plan"].get("plan_id", ""),
            session_output,
            "".join(traceback.format_exception(exc)).strip(),
        )
        result = {
            "exit_status": type(exc).__name__,
            "submission": f"交易触发处理异常：{type(exc).__name__}: {exc}（轨迹 {session_output}）",
        }
    action = event["plan"].get("side", "HOLD")
    append_account_cycle(
        journal_dir,
        cycle_id,
        {
            "action": action if action in {"BUY", "SELL"} else "HOLD",
            "market_view": f"监控触发 {event['stock_code']} @ {event['price']}",
            "account_risk": "由 account_trader 基于触发时账户和行情重新检查",
            "decision": result.get("submission", ""),
            "follow_up": "按 account_monitor 中未触发计划继续监控；交易结果未知时先查询委托和成交。",
            "orders": [json.dumps(event["plan"].get("order", {}), ensure_ascii=False, sort_keys=True)],
            "pitfalls": [],
            "tool_errors": [] if result.get("exit_status") == "Submitted" else [result.get("exit_status", "unknown")],
        },
    )
    _print_result(result, submission_streamed=False)
    return result


def _poll_market_monitor(settings: dict, journal_dir: Path) -> list[dict[str, Any]]:
    """轮询显式监控条件，返回本轮触发事件。"""
    monitor = MarketMonitor(journal_dir)
    current = monitor.read()
    if not current["ok"]:
        console.print(f"[bold]行情监控状态失败：{current['error']['detail']}[/bold]")
        return []
    if not current["data"].get("plans"):
        return []
    environment_settings = recursive_merge(
        settings.get("environment", {}),
        {"account_journal_dir": str(journal_dir), "miniqmt_mode": "observe"},
    )
    try:
        environment = get_environment(environment_settings)
        result = monitor.poll(environment._get_miniqmt())
    except Exception as exc:
        logger.error(
            "%s 行情监控异常\n%s",
            datetime.now(TRADING_TZ).isoformat(),
            "".join(traceback.format_exception(exc)).strip(),
        )
        console.print(f"[bold]行情监控异常：{type(exc).__name__}: {exc}[/bold]")
        return []
    if not result["ok"]:
        console.print(f"[bold]行情监控失败：{result['error']['detail']}[/bold]")
        return []
    return result["data"].get("events", [])


def _account_loop_slot(now: datetime) -> tuple[str, str] | None:
    if now.weekday() >= 5:
        return None
    current = now.time()
    if clock_time(9, 20) <= current < clock_time(9, 30):
        return "premarket", now.date().isoformat()
    if clock_time(12, 50) <= current < clock_time(13, 0):
        return "midday", now.date().isoformat()
    trading = clock_time(9, 30) <= current <= clock_time(11, 30) or clock_time(13, 0) <= current <= clock_time(15, 0)
    if trading:
        return "monitor", now.date().isoformat()
    if current >= clock_time(15, 10):
        return "review", f"{now.date().isoformat()}-close"
    return None


def _premarket_catchup_day(now: datetime) -> str | None:
    """进程在盘中启动时补跑当天盘前分析，确保后续监控有计划可读。"""
    if now.weekday() >= 5 or not (clock_time(9, 20) <= now.time() < clock_time(15, 10)):
        return None
    return now.date().isoformat()


# 盘中候选发现窗口。短线趋势跟随的买点出现在盘中放量突破那一刻，而盘前用的是昨收数据，
# 只靠盘前布防等于永远在追昨天的赢家；这两个窗口给系统一次用当日实时量价重算候选的机会。
# 窗口宽度 10 分钟：轮询间隔 10 秒，够宽到上一轮周期跑久了也不会整段错过。
INTRADAY_SCAN_WINDOWS = (
    (clock_time(10, 0), clock_time(10, 10)),
    (clock_time(13, 30), clock_time(13, 40)),
)


def _intraday_scan_key(now: datetime) -> str | None:
    for start, end in INTRADAY_SCAN_WINDOWS:
        if start <= now.time() < end:
            return f"{now.date().isoformat()}-{start.hour:02d}{start.minute:02d}"
    return None


def _acquire_account_loop_lock() -> Any:
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError("当前系统不支持账户管理循环所需的文件锁") from exc
    directory = Path(os.getenv("MINIQMT_AGENT_STATE_DIR", ".sessions/account-manager"))
    if not directory.is_absolute():
        directory = Path.cwd() / directory
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "account-loop.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("已有账户管理循环持有当前状态目录的运行锁") from exc
    return handle


def _install_account_schedule(config: Path) -> Path:
    """安装 macOS 工作日 09:20 启动的账户日任务。"""
    if sys.platform != "darwin":
        raise RuntimeError("--install-account-schedule 仅支持 macOS launchd")
    working_directory = Path.cwd().resolve()
    state_dir = Path(os.getenv("MINIQMT_AGENT_STATE_DIR", ".sessions/account-manager"))
    if not state_dir.is_absolute():
        state_dir = working_directory / state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = state_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.minisweagent.account-day.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": "com.minisweagent.account-day",
        "ProgramArguments": [
            sys.executable,
            "-m",
            "minisweagent.run.mini",
            "--account-day",
            "--config",
            str(config.resolve()),
        ],
        "WorkingDirectory": str(working_directory),
        "StartCalendarInterval": [
            {"Weekday": weekday, "Hour": 9, "Minute": 20} for weekday in range(1, 6)
        ],
        "RunAtLoad": False,
        "ProcessType": "Background",
        "ThrottleInterval": 30,
        "StandardOutPath": str(logs_dir / "account-day.out.log"),
        "StandardErrorPath": str(logs_dir / "account-day.err.log"),
    }
    plist_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    console.print(f"已安装账户日定时任务：{plist_path}")
    return plist_path


def _show_observation_todo() -> None:
    """在终端展示给用户的待观测清单，不启动 Agent。"""
    journal_dir = Path(os.getenv("MINIQMT_AGENT_STATE_DIR", ".sessions/account-manager"))
    if not journal_dir.is_absolute():
        journal_dir = Path.cwd() / journal_dir
    todo = read_observation_todo(journal_dir)
    console.print(todo or "当前没有待观测清单。")


@app.command()
def main(
    task: str | None = typer.Option(None, "-t", "--task", help="Task for the agent."),
    config: Path = typer.Option(
        DEFAULT_CONFIG_FILE, "-c", "--config", help="Agent YAML configuration."
    ),
    agent_name: str | None = typer.Option(
        None, "--agent", help="Agent 角色；交互终端省略时显示选择列表。"
    ),
    output: Path | None = typer.Option(
        None, "-o", "--output", help="Save the trajectory JSON here."
    ),
    step_limit: int | None = typer.Option(
        None, "--step-limit", min=0, help="Maximum model calls; 0 disables."
    ),
    timeout: int | None = typer.Option(None, "--timeout", min=1, help="Bash timeout in seconds."),
    account_loop: bool = typer.Option(False, "--account-loop", help="运行盘前分析、盘中行情监控触发交易和收盘复盘。"),
    account_day: bool = typer.Option(False, "--account-day", help="运行一个交易日并在收盘复盘后退出，供定时任务使用。"),
    install_account_schedule: bool = typer.Option(False, "--install-account-schedule", help="安装 macOS 工作日 09:20 自动运行的账户日定时任务。"),
    close_review: bool = typer.Option(False, "--close-review", help="运行一次只读收盘复盘。"),
    premarket: bool = typer.Option(False, "--premarket", help="运行一次只读盘前分析，用于随时验证研究到组合的全流程。"),
    intraday_scan: bool = typer.Option(False, "--intraday-scan", help="运行一次只读盘中候选发现，用突破结构找新候选。"),
    show_observation_todo: bool = typer.Option(False, "--show-observation-todo", help="查看给用户的待观测清单，不启动 Agent。"),
) -> Any:
    """Run DeepSeek V4 Flash with host-owned Bash, editor, and web search tools."""
    _load_dotenv()
    settings = get_config_from_spec(config)
    if show_observation_todo:
        if account_loop or account_day or close_review or premarket or intraday_scan or install_account_schedule:
            raise typer.BadParameter("--show-observation-todo 不能与账户运行或安装参数同时使用")
        _show_observation_todo()
        return None
    if install_account_schedule:
        if account_loop or account_day or close_review or premarket or intraday_scan:
            raise typer.BadParameter("--install-account-schedule 不能与账户运行参数同时使用")
        _install_account_schedule(config)
        return None
    if account_loop or account_day or close_review or premarket or intraday_scan:
        if sum((account_loop, account_day, close_review, premarket, intraday_scan)) > 1:
            raise typer.BadParameter(
                "--account-loop、--account-day、--close-review、--premarket 与 --intraday-scan 不能同时使用"
            )
        cycle_task = task or "观察账户、行情和未完成委托，判断是否需要交易并记录本轮完整决策。"
        if close_review:
            _account_cycle(settings, cycle_task, close_review=True)
            return None
        if premarket:
            _account_cycle(settings, cycle_task, premarket=True)
            return None
        if intraday_scan:
            _account_cycle(
                settings,
                task or "用当日盘中实时量价寻找新的突破候选，并判断是否值得换掉最弱持仓。",
                intraday_scan=True,
            )
            return None
        loop_lock = _acquire_account_loop_lock()
        console.print(
            "账户管理循环已启动：09:20 盘前分析，10:00 与 13:30 盘中候选发现，12:50 午盘前复核，"
            "盘中监控触发交易，15:10 收盘复盘。"
        )
        premarket_day = ""
        midday_day = ""
        review_day = ""
        scan_slots: set[str] = set()
        journal_dir = Path(os.getenv("MINIQMT_AGENT_STATE_DIR", ".sessions/account-manager"))
        if not journal_dir.is_absolute():
            journal_dir = Path.cwd() / journal_dir
        try:
            now = datetime.now(TRADING_TZ)
            catchup_day = _premarket_catchup_day(now)
            if catchup_day:
                # launchd 延迟或人工晚启动时，不能直接进入 monitor 空转。
                premarket_day = catchup_day
                _account_cycle(settings, cycle_task, premarket=True)
            while True:
                now = datetime.now(TRADING_TZ)
                scheduled = _account_loop_slot(now)
                if scheduled:
                    kind, day_key = scheduled
                    if kind == "premarket" and day_key != premarket_day:
                        premarket_day = day_key
                        _account_cycle(settings, cycle_task, premarket=True)
                    elif kind == "midday" and day_key != midday_day:
                        midday_day = day_key
                        midday_task = "复核上午行情、新闻、成交和账户变化，判断是否保留、修改或撤销下午监控计划。"
                        _account_cycle(settings, midday_task, midday_review=True)
                    elif kind == "monitor":
                        for event in _poll_market_monitor(settings, journal_dir):
                            _run_trade_trigger(settings, event, journal_dir)
                        # 先处理触发再扫描：已布防的止损和到期退出优先于寻找新机会。
                        scan_key = _intraday_scan_key(now)
                        if scan_key and scan_key not in scan_slots:
                            scan_slots.add(scan_key)
                            scan_task = "用当日盘中实时量价寻找新的突破候选，并判断是否值得换掉最弱持仓。"
                            _account_cycle(settings, scan_task, intraday_scan=True)
                    elif kind == "review" and day_key != review_day:
                        review_day = day_key
                        review_task = "核对当日成交、收益、滑点、决策偏差、监控触发、监控更新和踩坑，并写出下一交易日观察计划。"
                        _account_cycle(settings, review_task, close_review=True)
                        MarketMonitor(journal_dir).clear()
                        if account_day:
                            return None
                # 轮询频率由宿主控制，不保留任何 Agent 上下文。
                time.sleep(10)
        finally:
            loop_lock.close()
    agent_name = _select_agent_name(settings.get("agents", {}), agent_name)
    agent_settings = _get_agent_settings(settings, agent_name)
    configured_output = agent_settings.get("output_path")
    session_output, session_id, session_started_at = _new_session_record()
    agent_overrides = {
        "output_path": output or configured_output or session_output,
        "session_id": session_id,
        "session_started_at": session_started_at,
        "session_cwd": str(Path.cwd()),
        "cycle_kind": "manual",
        "step_limit": step_limit if step_limit is not None else UNSET,
    }
    agent_settings = recursive_merge(agent_settings, agent_overrides)
    environment_settings = recursive_merge(
        settings.get("environment", {}),
        {"timeout": timeout if timeout is not None else UNSET},
    )
    if agent_settings.get("delegates_to"):
        # 委派配置由宿主注入；子 Agent 不会继承这些字段，所以拿不到再往下委派的能力。
        environment_settings = recursive_merge(
            environment_settings,
            {
                "agent_profiles": _delegate_profiles(settings, agent_settings),
                "agent_common_config": settings.get("agent", {}),
                "agent_model_config": settings.get("model", {}),
                # 子 Agent 轨迹跟随父会话轨迹落在同一日期目录下，便于失败后按序号回溯。
                "agent_trace_prefix": str(Path(agent_settings["output_path"]).with_suffix("")),
                "agent_session_id": session_id,
                "agent_cycle_kind": "manual",
            },
        )
    task = task or terminal_prompt("Task: ")

    model = get_model(settings.get("model", {}))
    environment = get_environment(environment_settings)
    agent = get_agent(model, environment, agent_settings)
    _run_session(agent, task, interactive=sys.stdin.isatty())
    return agent


if __name__ == "__main__":
    app()
