"""Mini DeepSeek Agent 的配置文件和辅助函数。"""

from pathlib import Path

import yaml

from minisweagent.agents import AGENT_FLOWS
from minisweagent.models.utils.actions_toolcall import TOOL_DEFINITIONS_BY_NAME

builtin_config_dir = Path(__file__).parent
PROMPT_KINDS = ("system", "instance")
PROMPT_DIRNAME = "prompts"


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
    settings = load_config_file(path)
    _resolve_prompt_paths(settings, path.parent)
    return settings


def load_config_file(path: Path) -> dict:
    """读原始 YAML，不把 prompt 路径读成内容；配置编辑必须看到 *_template_path 本身。"""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def save_config_file(path: Path, settings: dict) -> None:
    """整份配置原子替换。配置文件是纯数据，说明写在 AGENTS.md，不放注释——机器会重写它。"""
    text = yaml.safe_dump(settings, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def prompt_path(base_dir: Path, role: str, kind: str) -> Path:
    """prompt 文件名是约定：<角色>.<system|instance>.md，放在配置目录的 prompts/ 下。

    路径由 role 和 kind 现算，不查配置：新增角色时 prompt 文件必须先于配置存在，
    否则校验通不过，而那时配置里还没有它的路径可查。
    """
    if kind not in PROMPT_KINDS:
        raise ValueError(f"prompt 类型只能是 {' 或 '.join(PROMPT_KINDS)}")
    if not role or "/" in role or "\\" in role or role.startswith("."):
        raise ValueError(f"非法角色名：{role}")
    return base_dir / PROMPT_DIRNAME / f"{role}.{kind}.md"


def resolve_prompt_path(base_dir: Path, relative: str, *, must_exist: bool = True) -> Path:
    """prompt 路径必须落在配置目录内：这是配置编辑唯一能写的文件区域。"""
    root = base_dir.resolve()
    path = (base_dir / relative).resolve()
    if root not in path.parents:
        raise ValueError(f"prompt 路径越出配置目录：{relative}")
    if must_exist and not path.is_file():
        raise ValueError(f"prompt 文件不存在：{relative}")
    return path


def _resolve_prompt_paths(settings: dict, base_dir: Path) -> None:
    """把角色配置里的 *_template_path 读成 *_template，让 prompt 独立成文件便于查找和修改。"""
    for role, profile in (settings.get("agents") or {}).items():
        for key in ("system_template", "instance_template"):
            relative = profile.pop(f"{key}_path", None)
            if relative is None:
                continue
            if key in profile:
                raise ValueError(f"agents.{role} 不能同时提供 {key} 和 {key}_path")
            profile[key] = resolve_prompt_path(base_dir, relative).read_text(encoding="utf-8")


def validate_agents(settings: dict, base_dir: Path) -> None:
    """整份角色配置自检。写回配置前必须过这一关，否则配置错误要等下一次启动才暴露。"""
    agents = settings.get("agents") or {}
    if not agents:
        raise ValueError("配置里至少要有一个 agent")
    for name, profile in agents.items():
        _validate_profile(name, profile, agents, base_dir)


def _validate_profile(name: str, profile: dict, agents: dict, base_dir: Path) -> None:
    """一个角色的全部硬约束，扁平列举：每条都是独立的配置事实，不是分支逻辑。"""
    if not isinstance(profile, dict):
        raise ValueError(f"agents.{name} 必须是对象")
    flow = profile.get("flow", "iterative")
    if flow not in AGENT_FLOWS:
        raise ValueError(f"agents.{name}.flow 不支持 {flow}；可选：{', '.join(AGENT_FLOWS)}")
    tools = profile.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise ValueError(f"agents.{name}.tools 必须是数组")
    unknown = [tool for tool in tools or [] if tool not in TOOL_DEFINITIONS_BY_NAME]
    if unknown:
        raise ValueError(f"agents.{name}.tools 含未知工具：{', '.join(unknown)}")
    if flow == "single_shot" and tools:
        raise ValueError(f"agents.{name} 用 single_shot flow 时不能配置工具")
    _validate_delegation(name, profile, agents, tools or [])
    for role in profile.get("requires") or []:
        if role not in agents:
            raise ValueError(f"agents.{name}.requires 引用了不存在的角色：{role}")
    for kind in PROMPT_KINDS:
        _validate_prompt_source(name, profile, base_dir, f"{kind}_template")


def _validate_delegation(name: str, profile: dict, agents: dict, tools: list) -> None:
    delegates = profile.get("delegates_to") or []
    for role in delegates:
        if role not in agents:
            raise ValueError(f"agents.{name}.delegates_to 引用了不存在的角色：{role}")
        if (agents[role] or {}).get("delegates_to"):
            raise ValueError(f"agents.{name} 委派的 {role} 自己也声明了 delegates_to；宿主只支持一层委派")
    if delegates and "agent_call" not in tools:
        raise ValueError(f"agents.{name} 声明了 delegates_to，但 tools 里没有 agent_call")
    if not delegates and "agent_call" in tools:
        raise ValueError(f"agents.{name} 配了 agent_call 工具，但没有声明 delegates_to")


def _validate_prompt_source(name: str, profile: dict, base_dir: Path, key: str) -> None:
    relative = profile.get(f"{key}_path")
    if key in profile and relative is not None:
        raise ValueError(f"agents.{name} 不能同时提供 {key} 和 {key}_path")
    if relative is not None:
        resolve_prompt_path(base_dir, relative)
        return
    if key not in profile:
        raise ValueError(f"agents.{name} 缺少 {key} 或 {key}_path")


__all__ = [
    "PROMPT_KINDS",
    "builtin_config_dir",
    "get_config_from_spec",
    "get_config_path",
    "load_config_file",
    "prompt_path",
    "resolve_prompt_path",
    "save_config_file",
    "validate_agents",
]
