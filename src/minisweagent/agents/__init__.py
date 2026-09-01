"""The default Agent loop."""

from minisweagent import Agent, Environment, Model

from .default import DefaultAgent
from .single_shot import SingleShotAgent

AGENT_FLOWS = {
    "iterative": DefaultAgent,
    "single_shot": SingleShotAgent,
}


def get_agent(model: Model, env: Environment, config: dict | None = None) -> Agent:
    """根据角色配置选择一个内置流程。"""
    settings = config or {}
    flow = settings.get("flow", "iterative")
    try:
        agent_class = AGENT_FLOWS[flow]
    except KeyError as error:
        raise ValueError(f"未知 Agent flow：{flow}") from error
    return agent_class(model, env, **settings)


__all__ = ["DefaultAgent", "SingleShotAgent", "get_agent"]
