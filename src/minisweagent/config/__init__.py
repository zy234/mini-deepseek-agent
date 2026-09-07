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
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    _resolve_prompt_paths(settings, path.parent)
    return settings


def _resolve_prompt_paths(settings: dict, base_dir: Path) -> None:
    """把角色配置里的 *_template_path 读成 *_template，让 prompt 独立成文件便于查找和修改。"""
    for role, profile in (settings.get("agents") or {}).items():
        for key in ("system_template", "instance_template"):
            prompt_path = profile.pop(f"{key}_path", None)
            if prompt_path is None:
                continue
            if key in profile:
                raise ValueError(f"agents.{role} 不能同时提供 {key} 和 {key}_path")
            resolved = base_dir / prompt_path
            if not resolved.is_file():
                raise FileNotFoundError(f"agents.{role}.{key}_path 指向的 prompt 文件不存在：{resolved}")
            profile[key] = resolved.read_text(encoding="utf-8")


__all__ = ["builtin_config_dir", "get_config_path", "get_config_from_spec"]
