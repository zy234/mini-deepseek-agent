"""The one model adapter used by this project."""

from minisweagent import Model

from .deepseek_model import DeepSeekModel


def get_model(config: dict | None = None) -> Model:
    """Build the fixed DeepSeek V4 Flash model adapter."""
    return DeepSeekModel(**(config or {}))


__all__ = ["DeepSeekModel", "get_model"]
