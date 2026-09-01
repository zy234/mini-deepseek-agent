"""Mini DeepSeek Agent 的配置文件和辅助函数。"""

from pathlib import Path

import yaml

builtin_config_dir = Path(__file__).parent


def get_config_path(config_spec: str | Path) -> Path:
    """Get the path to a config file."""
    config_spec = Path(config_spec)
    if config_spec.suffix != ".yaml":
        config_spec = config_spec.with_suffix(".yaml")
    candidates = [Path(config_spec), builtin_config_dir / config_spec]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(f"Could not find config file for {config_spec} (tried: {candidates})")


def get_config_from_spec(config_spec: str | Path) -> dict:
    """Load one YAML configuration file."""
    path = get_config_path(config_spec)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


__all__ = ["builtin_config_dir", "get_config_path", "get_config_from_spec"]
