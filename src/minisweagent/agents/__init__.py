"""The default Agent loop."""

from minisweagent import Agent, Environment, Model

from .default import DefaultAgent


def get_agent(model: Model, env: Environment, config: dict | None = None) -> Agent:
    """Build the only supported Agent implementation."""
    return DefaultAgent(model, env, **(config or {}))


__all__ = ["DefaultAgent", "get_agent"]
