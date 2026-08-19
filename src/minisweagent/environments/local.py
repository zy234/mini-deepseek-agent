import os
import platform
import signal
import subprocess
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from minisweagent.environments.bash_policy import analyze_bash_command
from minisweagent.exceptions import CommandNotApproved, Submitted
from minisweagent.utils.serialize import recursive_merge

SENSITIVE_ENV_NAMES = frozenset(
    {
        "DS_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "KUBECONFIG",
        "SSH_AUTH_SOCK",
    }
)
SENSITIVE_ENV_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PAT")


class LocalEnvironmentConfig(BaseModel):
    cwd: str = ""
    env: dict[str, str] = {}
    timeout: float = 30


class LocalEnvironment:
    def __init__(
        self,
        *,
        config_class: type = LocalEnvironmentConfig,
        approval_callback: Callable[[str, str], bool] | None = None,
        **kwargs,
    ):
        """This class executes bash commands directly on the local machine."""
        self.config = config_class(**kwargs)
        self.approval_callback = approval_callback or _prompt_for_approval

    def execute(self, action: dict, cwd: str = "", *, timeout: float | None = None) -> dict[str, Any]:
        """Execute a command in the local environment and return the result as a dict."""
        command = action.get("command", "")
        cwd = action.get("workdir") or cwd or self.config.cwd or os.getcwd()
        global_timeout = timeout if timeout is not None else self.config.timeout
        requested_timeout = action.get("timeout")
        risk = analyze_bash_command(command, cwd)
        if risk and (risk.hard_denied or not self.approval_callback(command, risk.reason)):
            self._stop_for_approval(command, risk.reason, hard_denied=risk.hard_denied)
        try:
            if requested_timeout is None:
                requested_timeout = global_timeout
            if isinstance(requested_timeout, bool) or not isinstance(requested_timeout, (int, float)):
                raise ValueError("命令 timeout 必须是数字")
            if requested_timeout <= 0:
                raise ValueError("命令 timeout 必须是正数")
            if requested_timeout > global_timeout:
                raise ValueError(f"命令 timeout 不能超过全局上限 {global_timeout} 秒")
            result = _run(command, cwd, _safe_environment(self.config.env), requested_timeout)
            returncode = result.returncode
            output = {
                "output": result.stdout,
                "returncode": returncode,
                "status": "success" if returncode == 0 else "failed",
                "timed_out": False,
                "signal": -returncode if returncode < 0 else None,
                "exception_info": "",
            }
            if action.get("description"):
                output["extra"] = {"description": action["description"]}
        except Exception as e:
            raw_output = getattr(e, "output", None)
            raw_output = (
                raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes) else (raw_output or "")
            )
            output = {
                "output": raw_output,
                "returncode": -1,
                "status": "timeout" if isinstance(e, subprocess.TimeoutExpired) else "error",
                "timed_out": isinstance(e, subprocess.TimeoutExpired),
                "signal": None,
                "exception_info": f"执行命令时发生错误：{e}",
                "extra": {"exception_type": type(e).__name__, "exception": str(e)},
            }
        self._check_finished(output)
        return output

    @staticmethod
    def _stop_for_approval(command: str, reason: str, *, hard_denied: bool) -> None:
        status = "CommandBlocked" if hard_denied else "CommandNotApproved"
        prefix = "安全策略禁止执行" if hard_denied else "用户未批准执行"
        submission = f"{prefix} Bash 命令：{reason}\n{command}"
        raise CommandNotApproved(
            {
                "role": "exit",
                "content": submission,
                "extra": {"exit_status": status, "submission": submission},
            }
        )

    def _check_finished(self, output: dict):
        """Raises Submitted if the output indicates task completion."""
        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and output["returncode"] == 0:
            submission = "".join(lines[1:])
            raise Submitted(
                {
                    "role": "exit",
                    "content": submission,
                    "extra": {"exit_status": "Submitted", "submission": submission},
                }
            )

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return recursive_merge(self.config.model_dump(), platform.uname()._asdict(), _safe_environment(), kwargs)

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "environment": self.config.model_dump(mode="json"),
                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }


def _run(command: str, cwd: str, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Like subprocess.run, but kills the whole process group on timeout so no children are orphaned."""
    process = subprocess.Popen(
        command,
        shell=True,
        text=True,
        cwd=cwd,
        env=env,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        os.killpg(process.pid, signal.SIGKILL) if os.name == "posix" else process.kill()
        stdout, _ = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout) from error
    return subprocess.CompletedProcess(command, process.returncode, stdout=stdout)


def _safe_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = {**os.environ, **(extra or {})}
    return {
        key: value
        for key, value in environment.items()
        if key.upper() not in SENSITIVE_ENV_NAMES and not key.upper().endswith(SENSITIVE_ENV_SUFFIXES)
    }


def _prompt_for_approval(command: str, reason: str) -> bool:
    print(f"\n[需要审批] {reason}\n{command}", flush=True)
    try:
        answer = input("允许执行这条命令吗？[y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in {"y", "yes"}
