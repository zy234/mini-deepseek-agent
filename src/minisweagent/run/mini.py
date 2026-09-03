#!/usr/bin/env python3
"""Run the single-model DeepSeek Bash agent."""

import json
import os
import plistlib
import re
import secrets
import subprocess
import sys
import time
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
from minisweagent.environments.account_journal import append_account_cycle, append_cycle_fallback
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


def _get_agent_settings(settings: dict, agent_name: str) -> dict:
    """将公共 Agent 配置与所选角色配置合并。"""
    profile = dict(settings["agents"][agent_name])
    profile.pop("description", None)
    return recursive_merge(settings.get("agent", {}), profile, {"agent_name": agent_name})


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
) -> None:
    """每次创建全新 Agent；跨轮状态只从每日账本和状态库恢复。"""
    started = datetime.now(TRADING_TZ)
    cycle_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
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
        },
    )
    journal_dir = Path(os.getenv("MINIQMT_AGENT_STATE_DIR", ".sessions/account-manager"))
    if not journal_dir.is_absolute():
        journal_dir = Path.cwd() / journal_dir
    environment_settings = recursive_merge(
        settings.get("environment", {}),
        {
            "miniqmt_mode": "observe" if close_review or premarket or midday_review else "auto_execute",
            "account_journal_dir": str(journal_dir),
            "account_cycle_id": cycle_id,
            "account_review_mode": close_review,
        },
    )
    child_roles = {"financial_research", "portfolio_manager", "account_trader"}
    agent_profiles = {
        role: dict(settings["agents"][role])
        for role in child_roles
        if role in settings.get("agents", {})
    }
    environment_settings = recursive_merge(
        environment_settings,
        {
            "agent_profiles": agent_profiles,
            "agent_common_config": settings.get("agent", {}),
            "agent_model_config": settings.get("model", {}),
        },
    )
    if premarket:
        task = (
            f"09:20 盘前分析模式（只读，不得下单），交易日 {started.date().isoformat()}；"
            "必须以 account_journal.previous 中前一交易日收盘记录为基准，结合今日新闻和账户持仓完成研究、组合取舍，"
            f"并将后续行情触发计划写入 account_monitor：{task}"
        )
    elif midday_review:
        task = (
            f"12:50 午盘前计划复核模式（只读，不得下单），交易日 {started.date().isoformat()}；"
            "先读取 account_journal.today 和 account_monitor 当前计划，再结合上午收盘行情、上午新增新闻和最新账户状态，"
            f"重新调用研究与组合 Agent；保留、修改或撤销计划后，用 account_monitor replace 写入完整新计划：{task}"
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
        result = {
            "exit_status": type(exc).__name__,
            "submission": f"账户管理周期异常：{type(exc).__name__}: {exc}",
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
    profile = dict(settings["agents"][role])
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
        "请查询账户和当前行情，独立完成 risk_check；只有风险通过才提交原 order，"
        "交易后如需新的止盈、止损或撤单条件，读取现有监控计划并用 account_monitor replace 更新。"
    )
    try:
        model = get_model({**settings.get("model", {}), "stream_output": False})
        environment = get_environment(environment_settings)
        agent = get_agent(model, environment, agent_settings)
        result = agent.run(task)
    except Exception as exc:
        result = {"exit_status": type(exc).__name__, "submission": f"交易触发处理异常：{type(exc).__name__}: {exc}"}
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
        console.print(f"[bold]行情监控异常：{type(exc).__name__}[/bold]")
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
) -> Any:
    """Run DeepSeek V4 Flash with host-owned Bash, editor, and web search tools."""
    _load_dotenv()
    settings = get_config_from_spec(config)
    if install_account_schedule:
        if account_loop or account_day or close_review:
            raise typer.BadParameter("--install-account-schedule 不能与账户运行参数同时使用")
        _install_account_schedule(config)
        return None
    if account_loop or account_day or close_review:
        if sum((account_loop, account_day, close_review)) > 1:
            raise typer.BadParameter("--account-loop、--account-day 与 --close-review 不能同时使用")
        cycle_task = task or "观察账户、行情和未完成委托，判断是否需要交易并记录本轮完整决策。"
        if close_review:
            _account_cycle(settings, cycle_task, close_review=True)
            return None
        loop_lock = _acquire_account_loop_lock()
        console.print("账户管理循环已启动：09:20 盘前分析，12:50 午盘前复核，盘中监控触发交易，15:10 收盘复盘。")
        premarket_day = ""
        midday_day = ""
        review_day = ""
        journal_dir = Path(os.getenv("MINIQMT_AGENT_STATE_DIR", ".sessions/account-manager"))
        if not journal_dir.is_absolute():
            journal_dir = Path.cwd() / journal_dir
        try:
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
        "step_limit": step_limit if step_limit is not None else UNSET,
    }
    agent_settings = recursive_merge(agent_settings, agent_overrides)
    environment_settings = recursive_merge(
        settings.get("environment", {}),
        {"timeout": timeout if timeout is not None else UNSET},
    )
    if agent_name == "financial_manager":
        # 主 Agent 的委派配置由宿主注入；子 Agent 不会继承这两个字段。
        child_roles = {"financial_research", "portfolio_manager", "account_trader"}
        environment_settings = recursive_merge(
            environment_settings,
            {
                "agent_profiles": {
                    role: settings["agents"][role]
                    for role in child_roles
                    if role in settings.get("agents", {})
                },
                "agent_common_config": settings.get("agent", {}),
                "agent_model_config": settings.get("model", {}),
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
