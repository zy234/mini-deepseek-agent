#!/usr/bin/env python3
"""Run the single-model DeepSeek Bash agent."""

import os
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from minisweagent.agents import get_agent
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.utils.serialize import UNSET, recursive_merge

DEFAULT_CONFIG_FILE = Path(
    os.getenv("MSWEA_MINI_CONFIG_PATH", builtin_config_dir / "deepseek.yaml")
)
console = Console(highlight=False)
app = typer.Typer(add_completion=False)


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
    result = agent.run(task)
    console.print(f"[bold]{result.get('exit_status', 'unknown')}[/bold]")
    if result.get("submission"):
        console.print(result["submission"])
    return agent


if __name__ == "__main__":
    app()
