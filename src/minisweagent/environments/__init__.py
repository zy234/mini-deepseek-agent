"""The local Bash execution environment."""

from minisweagent import Environment

from .local import LocalEnvironment


def get_environment(config: dict | None = None) -> Environment:
    """Build the local environment used by the agent."""
    return LocalEnvironment(**(config or {}))


__all__ = ["LocalEnvironment", "get_environment"]
