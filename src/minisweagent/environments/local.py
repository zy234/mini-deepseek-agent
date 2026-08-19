import os
import platform
import selectors
import signal
import subprocess
import tempfile
import time
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
STREAM_MAX_BYTES = 64_000
STREAM_SPILL_MAX_BYTES = 64 * 1024 * 1024
TERMINATION_GRACE_SECONDS = 0.25


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
            result = _run(
                command,
                cwd,
                _safe_environment(self.config.env),
                requested_timeout,
            )
            output = {
                **result,
                "exception_info": "",
            }
            if action.get("description"):
                output["extra"] = {"description": action["description"]}
        except Exception as e:
            output = {
                "stdout": "",
                "stderr": "",
                "returncode": -1,
                "exit_code": None,
                "status": "timeout" if isinstance(e, subprocess.TimeoutExpired) else "error",
                "timed_out": isinstance(e, subprocess.TimeoutExpired),
                "signal": None,
                "termination": None,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "stdout_spill_path": None,
                "stderr_spill_path": None,
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
        lines = output.get("stdout", "").lstrip().splitlines(keepends=True)
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


def _run(
    command: str,
    cwd: str,
    env: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    """Run Bash while collecting bounded stdout/stderr tails and optional spill files."""
    execution_env = {
        "NO_COLOR": "1",
        "TERM": "dumb",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        **env,
    }
    process = subprocess.Popen(
        ["bash", "-c", command],
        shell=False,
        text=False,
        cwd=cwd,
        env=execution_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    collectors = {
        "stdout": _OutputCollector("stdout", STREAM_MAX_BYTES, STREAM_SPILL_MAX_BYTES),
        "stderr": _OutputCollector("stderr", STREAM_MAX_BYTES, STREAM_SPILL_MAX_BYTES),
    }
    selector = selectors.DefaultSelector()
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        assert stream is not None
        selector.register(stream, selectors.EVENT_READ, name)
    timed_out = False
    termination = None
    forced_kill = False
    deadline = time.monotonic() + timeout
    termination_deadline = None
    while selector.get_map():
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0 and process.poll() is None and not timed_out:
            timed_out = True
            termination = "graceful"
            termination_deadline = now + TERMINATION_GRACE_SECONDS
            _signal_process_group(process, signal.SIGTERM)
        elif timed_out and process.poll() is None and termination_deadline is not None and now >= termination_deadline:
            forced_kill = True
            termination = "forced"
            termination_deadline = None
            _signal_process_group(process, signal.SIGKILL)
        wait_for = 0.1 if timed_out else max(0, min(remaining, 0.1))
        events = selector.select(wait_for)
        for key, _ in events:
            chunk = key.fileobj.read(8192)
            if chunk:
                collectors[key.data].feed(chunk)
            else:
                selector.unregister(key.fileobj)
                key.fileobj.close()
    returncode = process.wait()
    streams = {name: collector.finish() for name, collector in collectors.items()}
    return {
        "stdout": streams["stdout"]["text"],
        "stderr": streams["stderr"]["text"],
        "stdout_truncated": streams["stdout"]["truncated"],
        "stderr_truncated": streams["stderr"]["truncated"],
        "stdout_spill_path": streams["stdout"]["spill_path"],
        "stderr_spill_path": streams["stderr"]["spill_path"],
        "returncode": returncode,
        "exit_code": returncode if returncode >= 0 else None,
        "status": (
            "timeout"
            if timed_out
            else ("signal" if returncode < 0 else ("success" if returncode == 0 else "failed"))
        ),
        "timed_out": timed_out,
        "signal": -returncode if returncode < 0 else None,
        "termination": termination or ("forced" if forced_kill else None),
    }


def _signal_process_group(process: subprocess.Popen, sig: signal.Signals) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
    else:
        if sig == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()


class _OutputCollector:
    def __init__(self, name: str, max_bytes: int = STREAM_MAX_BYTES, spill_max_bytes: int = STREAM_SPILL_MAX_BYTES):
        self.name = name
        self.max_bytes = max_bytes
        self.spill_max_bytes = spill_max_bytes
        self.tail = bytearray()
        self.total_bytes = 0
        self.spill_bytes = 0
        self.spill_file = None
        self.spill_path = None

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        if self.total_bytes > self.max_bytes and self.spill_file is None:
            self._start_spill()
        if self.spill_file is not None and self.spill_bytes < self.spill_max_bytes:
            writable = chunk[: self.spill_max_bytes - self.spill_bytes]
            self.spill_file.write(writable)
            self.spill_bytes += len(writable)
            if len(writable) < len(chunk):
                self._discard_spill()
        self.tail.extend(chunk)
        if len(self.tail) > self.max_bytes:
            del self.tail[: len(self.tail) - self.max_bytes]

    def _start_spill(self) -> None:
        spill_dir = tempfile.mkdtemp(prefix="minisweagent-")
        os.chmod(spill_dir, 0o700)
        self.spill_path = os.path.join(spill_dir, f"{self.name}.log")
        self.spill_file = open(self.spill_path, "wb", opener=lambda path, flags: os.open(path, flags, 0o600))
        self.spill_file.write(bytes(self.tail))
        self.spill_bytes = len(self.tail)

    def _discard_spill(self) -> None:
        if self.spill_file is not None:
            self.spill_file.close()
            self.spill_file = None
        if self.spill_path:
            try:
                os.unlink(self.spill_path)
                os.rmdir(os.path.dirname(self.spill_path))
            except OSError:
                pass
        self.spill_path = None

    def finish(self) -> dict[str, Any]:
        if self.spill_file is not None:
            self.spill_file.flush()
            self.spill_file.close()
            self.spill_file = None
        return {
            "text": bytes(self.tail).decode("utf-8", errors="replace"),
            "truncated": self.total_bytes > self.max_bytes,
            "spill_path": self.spill_path,
        }


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
