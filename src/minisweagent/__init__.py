"""Small, single-model Bash agent primitives."""

from pathlib import Path
from typing import Any, Protocol

__version__ = "2.4.6"
package_dir = Path(__file__).resolve().parent


class Model(Protocol):
    config: Any

    def query(self, messages: list[dict[str, Any]], **kwargs) -> dict: ...

    def format_message(self, **kwargs) -> dict: ...

    def format_observation_messages(
        self, message: dict, outputs: list[dict], template_vars: dict | None = None
    ) -> list[dict]: ...

    def get_template_vars(self, **kwargs) -> dict[str, Any]: ...

    def serialize(self) -> dict: ...


class Environment(Protocol):
    config: Any

    def execute(self, action: dict, cwd: str = "") -> dict[str, Any]: ...

    def get_template_vars(self, **kwargs) -> dict[str, Any]: ...

    def serialize(self) -> dict: ...


class Agent(Protocol):
    config: Any

    def run(self, task: str, **kwargs) -> dict: ...

    def save(self, path: Path | None, *extra_dicts) -> dict: ...


__all__ = ["Agent", "Environment", "Model", "__version__", "package_dir"]
