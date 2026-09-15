#!/usr/bin/env python3
"""Run the single-model DeepSeek agent, or a full three-stage trading day."""

import logging
import os
import plistlib
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import typer
from prompt_toolkit import prompt as terminal_prompt
from rich.console import Console

from minisweagent.agents import get_agent
from minisweagent.backtest.replay import BacktestDataError
from minisweagent.backtest.runner import BacktestError, BacktestRunner
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.trading.pipeline import TradingPipeline
from minisweagent.utils.cli_display import clear_recent_full_blocks, render_recent_full_blocks
from minisweagent.utils.serialize import UNSET, recursive_merge

DEFAULT_CONFIG_FILE = builtin_config_dir / "deepseek.yaml"
MINIQMT_MODES = ("observe", "execute", "auto_execute")
console = Console(highlight=False)
app = typer.Typer(add_completion=False)
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


def _get_agent_settings(settings: dict, agent_name: str) -> dict:
    """将公共 Agent 配置与所选角色配置合并。"""
    profile = dict((settings.get("agents") or {}).get(agent_name) or {})
    if not profile:
        raise ValueError(f"配置的 agents 里没有 {agent_name}")
    profile.pop("description", None)
    return recursive_merge(settings.get("agent", {}), profile, {"agent_name": agent_name})


def _new_session_record() -> tuple[Path, str, str]:
    """为一次 CLI 会话生成可排序且低碰撞的轨迹路径和元数据。"""
    started_at = datetime.now().astimezone()
    day = started_at.strftime("%Y%m%d")
    timestamp = started_at.strftime("%Y%m%d-%H%M%S-%f")
    session_id = f"{timestamp}-{os.urandom(4).hex()}"
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


def _journal_dir() -> Path:
    directory = Path(os.environ["MINIQMT_AGENT_STATE_DIR"])
    return directory if directory.is_absolute() else Path.cwd() / directory


def _build_pipeline(settings: dict, miniqmt_mode: str | None) -> TradingPipeline:
    """装配交易日流水线。下单权限只能从这里进来，一处可查。"""
    if miniqmt_mode is not None:
        settings = recursive_merge(settings, {"environment": {"miniqmt_mode": miniqmt_mode}})
    mode = (settings.get("environment") or {}).get("miniqmt_mode", "observe")
    console.print(f"交易模式：[bold]{mode}[/bold]" + ("（会真实下单）" if mode != "observe" else "（只读，不会下单）"))
    return TradingPipeline(
        settings,
        sessions_dir=Path.cwd() / ".sessions",
        journal_dir=_journal_dir(),
        echo=lambda text: console.print(text),
    )


def _acquire_trading_lock() -> Any:
    """同一个状态目录只允许一个交易进程：两个进程会各自按自己的额度下单。"""
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError("当前系统不支持交易流水线所需的文件锁") from exc
    directory = _journal_dir()
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "trading.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("已有交易进程持有当前状态目录的运行锁") from exc
    return handle


def _install_schedule(config: Path) -> Path:
    """安装 macOS 工作日 09:15 启动的交易日任务。"""
    if sys.platform != "darwin":
        raise RuntimeError("--install-schedule 仅支持 macOS launchd")
    working_directory = Path.cwd().resolve()
    logs_dir = _journal_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.minisweagent.trading-day.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": "com.minisweagent.trading-day",
        "ProgramArguments": [
            sys.executable,
            "-m",
            "minisweagent.run.mini",
            "--trading-day",
            "--config",
            str(config.resolve()),
        ],
        "WorkingDirectory": str(working_directory),
        # 09:15 启动，留几分钟给进程拉起和板块缓存预热，09:20 正好跑盘前选池。
        "StartCalendarInterval": [{"Weekday": weekday, "Hour": 9, "Minute": 15} for weekday in range(1, 6)],
        "RunAtLoad": False,
        "ProcessType": "Background",
        "ThrottleInterval": 30,
        "StandardOutPath": str(logs_dir / "trading-day.out.log"),
        "StandardErrorPath": str(logs_dir / "trading-day.err.log"),
    }
    plist_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    console.print(f"已安装交易日定时任务：{plist_path}")
    return plist_path


@app.command()
def main(
    task: str | None = typer.Option(None, "-t", "--task", help="Task for the agent."),
    config: Path = typer.Option(DEFAULT_CONFIG_FILE, "-c", "--config", help="Agent YAML configuration."),
    agent_name: str | None = typer.Option(None, "--agent", help="Agent 角色；交互终端省略时显示选择列表。"),
    output: Path | None = typer.Option(None, "-o", "--output", help="Save the trajectory JSON here."),
    step_limit: int | None = typer.Option(None, "--step-limit", min=0, help="Maximum model calls; 0 disables."),
    timeout: int | None = typer.Option(None, "--timeout", min=1, help="Bash timeout in seconds."),
    trading_day: bool = typer.Option(False, "--trading-day", help="跑完整交易日：09:20 盘前选池，盘中每 10 分钟读图与执行，收盘退出。"),
    premarket: bool = typer.Option(False, "--premarket", help="只跑一次盘前选池，写出当日待观测清单。"),
    trading_round: bool = typer.Option(False, "--round", help="只跑一轮盘中读图与汇总执行，需要当日待观测清单已存在。"),
    miniqmt_mode: str | None = typer.Option(None, "--miniqmt-mode", help=f"交易权限：{'、'.join(MINIQMT_MODES)}；默认读配置。"),
    install_schedule: bool = typer.Option(False, "--install-schedule", help="安装 macOS 工作日 09:15 自动运行交易日的定时任务。"),
    backtest: str | None = typer.Option(None, "--backtest", help="回测指定交易日（YYYY-MM-DD）：重放历史行情问模型拿读图结论，纸面模拟收益；不会发真实委托。"),
    backtest_codes: str | None = typer.Option(None, "--codes", help="回测标的，逗号分隔的股票代码；缺省读该日的待观测清单。"),
    backtest_at: str | None = typer.Option(None, "--at", help="只回测该时刻的槽位，HH:MM；缺省跑全天全部槽位。"),
    extra_slots: str | None = typer.Option(None, "--extra-slots", help="在固定间隔的槽位之外追加的时刻，逗号分隔的 HH:MM，如 14:40。"),
    initial_cash: float = typer.Option(100_000.0, "--initial-cash", min=1000.0, help="回测初始资金。"),
) -> Any:
    """Run one agent interactively, or drive the three-stage trading pipeline."""
    _load_dotenv()
    settings = get_config_from_spec(config)
    if miniqmt_mode is not None and miniqmt_mode not in MINIQMT_MODES:
        raise typer.BadParameter(f"--miniqmt-mode 只能是 {'、'.join(MINIQMT_MODES)}")
    trading_flags = (trading_day, premarket, trading_round)
    if backtest:
        if any(trading_flags) or install_schedule:
            raise typer.BadParameter("--backtest 不能与交易运行参数同时使用")
        try:
            trade_date = date.fromisoformat(backtest)
        except ValueError as exc:
            raise typer.BadParameter("--backtest 需要 YYYY-MM-DD 日期") from exc
        codes = [code.strip() for code in (backtest_codes or "").split(",") if code.strip()] or None
        moments = [moment.strip() for moment in (extra_slots or "").split(",") if moment.strip()] or None
        try:
            BacktestRunner(
                settings,
                sessions_dir=Path.cwd() / ".sessions",
                journal_dir=_journal_dir(),
                echo=lambda text: console.print(text),
                trade_date=trade_date,
                codes=codes,
                at=backtest_at,
                extra_slots=moments,
                initial_cash=initial_cash,
            ).run()
        except (BacktestError, BacktestDataError) as error:
            # 业务失败（标的来源不成立、数据缺口）是一句话说清的事，不需要 traceback。
            console.print(f"[red]回测失败：{error}[/red]")
            raise typer.Exit(1) from error
        except Exception as error:
            # 其他失败：一行人话加完整 traceback，两者都要——只留一行是把故障藏起来，
            # 只甩裸 traceback 是让人猜。print_exception 在 except 里才有现场。
            console.print(f"[red]回测失败：{type(error).__name__}: {error}[/red]")
            console.print_exception()
            raise typer.Exit(1) from error
        return None
        if any(trading_flags):
            raise typer.BadParameter("--install-schedule 不能与交易运行参数同时使用")
        _install_schedule(config)
        return None
    if sum(trading_flags) > 1:
        raise typer.BadParameter("--trading-day、--premarket 与 --round 不能同时使用")
    if any(trading_flags):
        lock = _acquire_trading_lock()
        try:
            pipeline = _build_pipeline(settings, miniqmt_mode)
            if premarket:
                pipeline.premarket()
            elif trading_round:
                pipeline.run_round()
            else:
                pipeline.run_day()
        finally:
            lock.close()
        return None

    agent_name = _select_agent_name(settings.get("agents", {}), agent_name)
    agent_settings = _get_agent_settings(settings, agent_name)
    session_output, session_id, session_started_at = _new_session_record()
    agent_settings = recursive_merge(
        agent_settings,
        {
            "output_path": output or agent_settings.get("output_path") or session_output,
            "session_id": session_id,
            "session_started_at": session_started_at,
            "session_cwd": str(Path.cwd()),
            "cycle_kind": "manual",
            "step_limit": step_limit if step_limit is not None else UNSET,
        },
    )
    environment_settings = recursive_merge(
        settings.get("environment", {}),
        {
            "timeout": timeout if timeout is not None else UNSET,
            "miniqmt_mode": miniqmt_mode if miniqmt_mode is not None else UNSET,
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
