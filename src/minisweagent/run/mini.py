#!/usr/bin/env python3
"""Run the single-model DeepSeek Bash agent."""

import os
import re
import secrets
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
from minisweagent.environments.account_journal import append_cycle_fallback
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


def _account_cycle(settings: dict, task: str, *, close_review: bool = False) -> None:
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
            "miniqmt_mode": "observe" if close_review else "auto_execute",
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
    if close_review:
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


def _account_loop_slot(now: datetime) -> tuple[str, str] | None:
    if now.weekday() >= 5:
        return None
    current = now.time()
    trading = clock_time(9, 20) <= current <= clock_time(11, 30) or clock_time(13, 0) <= current <= clock_time(15, 0)
    if trading:
        slot_minute = now.minute // 10 * 10
        return "trade", f"{now.date().isoformat()}-{now.hour:02d}{slot_minute:02d}"
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
    account_loop: bool = typer.Option(False, "--account-loop", help="每 10 分钟以全新上下文运行账户管理 Agent。"),
    close_review: bool = typer.Option(False, "--close-review", help="运行一次只读收盘复盘。"),
) -> Any:
    """Run DeepSeek V4 Flash with host-owned Bash, editor, and web search tools."""
    _load_dotenv()
    settings = get_config_from_spec(config)
    if account_loop or close_review:
        if account_loop and close_review:
            raise typer.BadParameter("--account-loop 与 --close-review 不能同时使用")
        cycle_task = task or "观察账户、行情和未完成委托，判断是否需要交易并记录本轮完整决策。"
        if close_review:
            _account_cycle(settings, cycle_task, close_review=True)
            return None
        loop_lock = _acquire_account_loop_lock()
        console.print("账户管理循环已启动：交易时段每 10 分钟使用全新上下文，15:10 执行只读复盘。")
        last_slot = ""
        try:
            while True:
                now = datetime.now(TRADING_TZ)
                scheduled = _account_loop_slot(now)
                if scheduled and scheduled[1] != last_slot:
                    kind, last_slot = scheduled
                    review_task = "核对当日成交、收益、滑点、决策偏差、踩坑，并写出下一交易日观察计划。"
                    _account_cycle(settings, review_task if kind == "review" else cycle_task, close_review=kind == "review")
                # 只等待调度，不保留任何 Agent 上下文。
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
