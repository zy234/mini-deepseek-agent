#!/usr/bin/env python3
"""Run the single-model DeepSeek Bash agent."""

import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from prompt_toolkit import prompt as terminal_prompt
from rich.console import Console

from minisweagent.agents import get_agent
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.utils.cli_display import clear_recent_full_blocks, render_recent_full_blocks
from minisweagent.utils.serialize import UNSET, recursive_merge

DEFAULT_CONFIG_FILE = Path(
    os.getenv("MSWEA_MINI_CONFIG_PATH", builtin_config_dir / "deepseek.yaml")
)
console = Console(highlight=False)
app = typer.Typer(add_completion=False)


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
) -> Any:
    """Run DeepSeek V4 Flash with host-owned Bash, editor, and web search tools."""
    _load_dotenv()
    settings = get_config_from_spec(config)
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
