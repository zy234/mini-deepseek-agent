from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bashlex


@dataclass(frozen=True)
class BashRisk:
    reason: str
    hard_denied: bool = False


READ_ONLY_COMMANDS = frozenset(
    {
        "basename",
        "cat",
        "cmp",
        "comm",
        "cut",
        "date",
        "df",
        "diff",
        "dirname",
        "du",
        "echo",
        "env",
        "file",
        "find",
        "git",
        "grep",
        "head",
        "id",
        "jq",
        "ls",
        "md5",
        "md5sum",
        "printf",
        "pwd",
        "readlink",
        "realpath",
        "rg",
        "sed",
        "sha1sum",
        "sha256sum",
        "sort",
        "stat",
        "tail",
        "test",
        "tr",
        "true",
        "false",
        "type",
        "uname",
        "wc",
        "whereis",
        "which",
        "whoami",
        "yq",
    }
)
READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "blame",
        "describe",
        "diff",
        "diff-tree",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "reflog",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
        "version",
        "whatchanged",
    }
)
HARD_DENIED_COMMANDS = frozenset(
    {
        "dd",
        "diskutil",
        "doas",
        "fdisk",
        "halt",
        "mkfs",
        "parted",
        "poweroff",
        "reboot",
        "shutdown",
        "su",
        "sudo",
    }
)
EXECUTION_ENV_VARS = frozenset(
    {"BASH_ENV", "ENV", "IFS", "LD_LIBRARY_PATH", "LD_PRELOAD", "PATH", "SHELL"}
)
UNSUPPORTED_NODE_KINDS = frozenset(
    {"arithfor", "case", "compound", "for", "function", "if", "select", "until", "while"}
)
READ_ONLY_REDIRECTS = frozenset({"<", "<<", "<<<", "<&"})


def analyze_bash_command(command: str, cwd: str) -> BashRisk | None:
    """Return the first risk that requires approval, or None for a known read-only command."""
    try:
        trees = bashlex.parse(command)
    except (NotImplementedError, ValueError) as exc:
        return BashRisk(f"无法可靠解析 Bash 语法：{exc}")
    if not trees:
        return BashRisk("命令为空")

    workspace = Path(cwd).resolve()
    for tree in trees:
        for node in _walk(tree):
            if node.kind in UNSUPPORTED_NODE_KINDS:
                return BashRisk(f"包含需要审批的复杂 Shell 结构：{node.kind}")
            if node.kind == "redirect":
                risk = _analyze_redirect(node, workspace)
                if risk:
                    return risk
            if node.kind == "command":
                risk = _analyze_simple_command(node, workspace)
                if risk:
                    return risk
    return None


def _walk(node: Any):
    yield node
    for value in vars(node).values():
        if hasattr(value, "kind"):
            yield from _walk(value)
        elif isinstance(value, list):
            for item in value:
                if hasattr(item, "kind"):
                    yield from _walk(item)


def _analyze_redirect(node: Any, workspace: Path) -> BashRisk | None:
    redirect_type = getattr(node, "type", "")
    target = getattr(getattr(node, "output", None), "word", "")
    if redirect_type not in READ_ONLY_REDIRECTS:
        hard_denied = target == "/dev" or target.startswith("/dev/")
        return BashRisk(f"包含写入重定向 {redirect_type} {target}".strip(), hard_denied=hard_denied)
    return _outside_workspace_risk([target], workspace)


def _analyze_simple_command(node: Any, workspace: Path) -> BashRisk | None:
    assignments = [part.word for part in node.parts if part.kind == "assignment"]
    words = [part.word for part in node.parts if part.kind == "word"]
    for assignment in assignments:
        name = assignment.split("=", 1)[0].upper()
        if name in EXECUTION_ENV_VARS or name.startswith("DYLD_"):
            return BashRisk(f"修改了影响命令执行的环境变量：{name}")
    words, wrapper_risk = _unwrap_command(words)
    if wrapper_risk or not words:
        return wrapper_risk

    executable = os.path.basename(words[0])
    args = words[1:]
    if executable in HARD_DENIED_COMMANDS:
        return BashRisk(f"禁止执行主机级高风险命令：{executable}", hard_denied=True)
    hard_denied = _hard_denied_argument_risk(executable, args, workspace)
    if hard_denied:
        return hard_denied
    if executable == "cd":
        return _analyze_cd(args, workspace)
    if executable == "command" and any(arg in {"-v", "-V"} for arg in args):
        return None
    if executable not in READ_ONLY_COMMANDS:
        return BashRisk(f"命令不在只读允许列表中：{executable}")

    risk = _command_specific_risk(executable, args)
    if risk:
        return risk
    return _outside_workspace_risk(args, workspace)


def _unwrap_command(words: list[str]) -> tuple[list[str], BashRisk | None]:
    words = list(words)
    while words:
        executable = os.path.basename(words[0])
        if executable == "env":
            words = words[1:]
            while words and (words[0].startswith("-") or "=" in words[0]):
                if "=" in words[0]:
                    name = words[0].split("=", 1)[0].upper()
                    if name in EXECUTION_ENV_VARS or name.startswith("DYLD_"):
                        return words, BashRisk(f"修改了影响命令执行的环境变量：{name}")
                words = words[1:]
            if not words:
                return ["env"], None
            continue
        if executable == "timeout":
            words = words[1:]
            while words and words[0].startswith("-"):
                option = words.pop(0)
                if option in {"-k", "--kill-after", "-s", "--signal"} and words:
                    words.pop(0)
            if words:
                words.pop(0)
            continue
        if executable == "nice":
            words = words[1:]
            if words[:1] == ["-n"]:
                words = words[2:]
            continue
        if executable == "command":
            if any(arg in {"-v", "-V"} for arg in words[1:]):
                return words, None
            words = [word for word in words[1:] if word != "--"]
            continue
        break
    return words, None


def _command_specific_risk(executable: str, args: list[str]) -> BashRisk | None:
    if executable == "git":
        return _git_risk(args)
    if executable == "sed" and any(arg == "-i" or arg.startswith("--in-place") for arg in args):
        return BashRisk("sed 将原地修改文件")
    if executable == "sort" and any(arg == "-o" or arg.startswith("--output=") for arg in args):
        return BashRisk("sort 将结果写入文件")
    if executable == "yq" and any(arg in {"-i", "--in-place"} for arg in args):
        return BashRisk("yq 将原地修改文件")
    if executable == "rg" and any(arg == "--pre" or arg.startswith("--pre=") for arg in args):
        return BashRisk("rg --pre 会执行外部命令")
    if executable == "find":
        unsafe_actions = {"-delete", "-exec", "-execdir", "-fprint", "-fprint0", "-fprintf", "-fls", "-ok", "-okdir"}
        if any(arg in unsafe_actions for arg in args):
            return BashRisk("find 包含写入或执行动作")
    return None


def _hard_denied_argument_risk(executable: str, args: list[str], workspace: Path) -> BashRisk | None:
    if executable not in {"rm", "rmdir"}:
        return None
    targets = [arg for arg in args if not arg.startswith("-")]
    for target in targets:
        expanded = os.path.expandvars(os.path.expanduser(target))
        path = Path(expanded)
        resolved = path.resolve() if path.is_absolute() else (workspace / path).resolve()
        if resolved == workspace or not resolved.is_relative_to(workspace):
            return BashRisk(f"禁止删除工作区本身或工作区外路径：{target}", hard_denied=True)
    return None


def _git_risk(args: list[str]) -> BashRisk | None:
    args = [arg for arg in args if not arg.startswith("-C") and arg not in {"--no-pager"}]
    subcommand = next((arg for arg in args if not arg.startswith("-")), "")
    if subcommand in READ_ONLY_GIT_SUBCOMMANDS:
        return None
    if subcommand == "branch":
        positional = [arg for arg in args[1:] if not arg.startswith("-")]
        if not positional or "--list" in args or "--show-current" in args:
            return None
    if subcommand == "remote" and all(arg in {"remote", "-v", "--verbose"} for arg in args):
        return None
    if subcommand == "config" and any(arg in {"--get", "--get-all", "--get-regexp", "--list", "-l"} for arg in args):
        return None
    return BashRisk(f"Git 子命令不是明确的只读操作：{subcommand or '(缺失)'}")


def _analyze_cd(args: list[str], workspace: Path) -> BashRisk | None:
    if not args:
        return BashRisk("cd 将离开工作区进入用户主目录")
    target = Path(os.path.expanduser(args[-1]))
    resolved = target.resolve() if target.is_absolute() else (workspace / target).resolve()
    if not resolved.is_relative_to(workspace):
        return BashRisk(f"cd 将离开工作区：{resolved}")
    return None


def _outside_workspace_risk(args: list[str], workspace: Path) -> BashRisk | None:
    for arg in args:
        if arg.startswith(("$HOME", "${HOME}", "~")):
            return BashRisk(f"参数可能访问用户主目录：{arg}")
        if arg == ".." or arg.startswith("../"):
            return BashRisk(f"参数可能访问工作区外路径：{arg}")
        path = Path(arg)
        if path.is_absolute() and not path.resolve().is_relative_to(workspace):
            return BashRisk(f"参数访问工作区外路径：{path}")
    return None
