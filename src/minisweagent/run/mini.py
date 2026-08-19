#!/usr/bin/env python3
"""Run the single-model DeepSeek Bash agent."""

import os
import sys
from pathlib import Path
from typing import Any

import typer
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
            request = typer.prompt("继续提问（/open 展开，/exit 退出）").strip()
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
    output: Path | None = typer.Option(
        None, "-o", "--output", help="Save the trajectory JSON here."
    ),
    step_limit: int | None = typer.Option(
        None, "--step-limit", min=0, help="Maximum model calls; 0 disables."
    ),
    timeout: int | None = typer.Option(None, "--timeout", min=1, help="Bash timeout in seconds."),
) -> Any:
    """Run DeepSeek V4 Flash with one Bash tool in the current directory."""
    settings = get_config_from_spec(config)
    overrides = {
        "agent": {"output_path": output or UNSET, "step_limit": step_limit if step_limit is not None else UNSET},
        "environment": {"timeout": timeout if timeout is not None else UNSET},
    }
    settings = recursive_merge(settings, overrides)
    task = task or typer.prompt("Task")

    model = get_model(settings.get("model", {}))
    environment = get_environment(settings.get("environment", {}))
    agent = get_agent(model, environment, settings.get("agent", {}))
    _run_session(agent, task, interactive=sys.stdin.isatty())
    return agent


if __name__ == "__main__":
    app()
