import json
import os
import platform
import selectors
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from minisweagent.environments.account_journal import (
    append_account_cycle,
    read_account_journal,
)
from minisweagent.environments.bash_policy import analyze_bash_command
from minisweagent.environments.editor import execute_editor
from minisweagent.environments.financial_calc import execute_financial_calc
from minisweagent.environments.miniqmt import MiniQMTClient
from minisweagent.environments.web_fetch import execute_web_fetch
from minisweagent.environments.web_search import (
    DEFAULT_SEARCH_ENGINES,
    execute_web_search,
)
from minisweagent.exceptions import CommandNotApproved, InterruptAgentFlow, Submitted
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
        "MINIQMT_ACCOUNT_ID",
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
    web_search_engines: list[str] = Field(default_factory=lambda: list(DEFAULT_SEARCH_ENGINES))
    web_search_max_results: int = Field(default=8, ge=1)
    miniqmt_bridge_url: str = Field(
        default_factory=lambda: os.getenv("MINIQMT_BRIDGE_URL", "http://127.0.0.1:8023")
    )
    miniqmt_mode: str = Field(default_factory=lambda: os.getenv("MINIQMT_AGENT_MODE", "observe"))
    account_journal_dir: str = ".sessions/account-manager"
    account_cycle_id: str = Field(default_factory=lambda: f"manual-{time.time_ns()}")
    account_review_mode: bool = False
    agent_profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    agent_common_config: dict[str, Any] = Field(default_factory=dict)
    agent_model_config: dict[str, Any] = Field(default_factory=dict)
    agent_call_limit: int = Field(default=4, ge=1, le=20)


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
        self._miniqmt: MiniQMTClient | None = None
        self._agent_call_count = 0
        # 主 Agent 的交接阶段由宿主记录，避免模型跳过研究或组合风控直接交易。
        self._agent_call_roles: list[str] = []

    def execute(self, action: dict, cwd: str = "", *, timeout: float | None = None) -> dict[str, Any]:
        """Execute a command in the local environment and return the result as a dict."""
        if action.get("tool") == "str_replace_editor":
            return self._execute_editor(action, cwd)
        if action.get("tool") == "web_search":
            return self._execute_web_search(action, timeout=timeout)
        if action.get("tool") == "web_fetch":
            return execute_web_fetch(
                action.get("url", ""),
                timeout=timeout if timeout is not None else self.config.timeout,
            )
        if action.get("tool") == "financial_calc":
            result = execute_financial_calc(action.get("operation", ""), action.get("inputs", {}))
            return _json_tool_output("financial_calc", result)
        if action.get("tool") == "miniqmt_quotes":
            return _json_tool_output("miniqmt_quotes", self._get_miniqmt().quotes(action.get("stock_codes", [])))
        if action.get("tool") == "miniqmt_account":
            return _json_tool_output("miniqmt_account", self._get_miniqmt().account(action.get("view", "")))
        if action.get("tool") == "miniqmt_trade":
            return self._execute_miniqmt_trade(action)
        if action.get("tool") == "account_journal":
            return self._execute_account_journal(action)
        if action.get("tool") == "agent_call":
            return self._execute_agent_call(action)
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
                "path": None,
                "operation": None,
                "content_hash": None,
                "exception_info": f"执行命令时发生错误：{e}",
                "extra": {"exception_type": type(e).__name__, "exception": str(e)},
            }
        self._check_finished(output)
        return output

    def _execute_editor(self, action: dict, cwd: str) -> dict[str, Any]:
        workspace = cwd or self.config.cwd or os.getcwd()
        operation = action.get("command")
        if operation in {"create", "str_replace", "insert"}:
            approval_text = f"str_replace_editor {operation}: {action.get('path', '')}"
            if not self.approval_callback(approval_text, "文件修改操作"):
                self._stop_for_approval(approval_text, "文件修改操作", hard_denied=False)
        try:
            return {**execute_editor(action, workspace), "exception_info": ""}
        except Exception as error:
            return {
                "stdout": "",
                "stderr": "",
                "returncode": -1,
                "exit_code": None,
                "status": "error",
                "timed_out": False,
                "signal": None,
                "termination": None,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "stdout_spill_path": None,
                "stderr_spill_path": None,
                "path": action.get("path"),
                "operation": action.get("command"),
                "content_hash": None,
                "exception_info": f"文件编辑失败：{error}",
                "extra": {"error_code": getattr(error, "code", type(error).__name__)},
            }

    def _execute_web_search(self, action: dict, *, timeout: float | None) -> dict[str, Any]:
        search_timeout = timeout if timeout is not None else self.config.timeout
        return execute_web_search(
            action.get("queries", []),
            timeout=search_timeout,
            engines=self.config.web_search_engines,
            max_results=self.config.web_search_max_results,
        )

    def _execute_miniqmt_trade(self, action: dict) -> dict[str, Any]:
        operation = action.get("operation", "")
        inputs = action.get("inputs", {})
        if self.config.miniqmt_mode == "execute":
            summary = json.dumps(
                {"tool": "miniqmt_trade", "operation": operation, "inputs": inputs},
                ensure_ascii=False,
                sort_keys=True,
            )
            if not self.approval_callback(summary, "个人账户交易操作"):
                self._stop_for_approval(
                    summary,
                    "个人账户交易操作",
                    hard_denied=False,
                    subject="工具调用",
                )
        return _json_tool_output("miniqmt_trade", self._get_miniqmt().trade(operation, inputs))

    def _execute_account_journal(self, action: dict) -> dict[str, Any]:
        operation = action.get("operation", "")
        if operation == "read":
            result = read_account_journal(self.config.account_journal_dir)
        elif operation == "append":
            result = append_account_cycle(
                self.config.account_journal_dir,
                self.config.account_cycle_id,
                action.get("record"),
            )
        else:
            result = {
                "ok": False,
                "status": "error",
                "operation": None,
                "data": None,
                "error": {"code": "invalid_argument", "detail": "account_journal 只支持 read 或 append"},
            }
        return _json_tool_output("account_journal", result)

    def _execute_agent_call(self, action: dict) -> dict[str, Any]:
        """在宿主侧运行固定子 Agent，避免模型自行获得账户工具或递归编排能力。"""
        role = action.get("role", "")
        task = action.get("task", "")
        allowed_roles = {"financial_research", "portfolio_manager", "account_trader"}
        if role not in allowed_roles:
            return _json_tool_output(
                "agent_call",
                {"ok": False, "status": "invalid_argument", "error": {"code": "unknown_role", "detail": "不允许的子 Agent 角色"}},
            )
        if not isinstance(task, str) or not task.strip() or len(task) > 12000:
            return _json_tool_output(
                "agent_call",
                {"ok": False, "status": "invalid_argument", "error": {"code": "invalid_task", "detail": "子 Agent 任务必须是 1 到 12000 个字符"}},
            )
        if self._agent_call_count >= self.config.agent_call_limit:
            return _json_tool_output(
                "agent_call",
                {"ok": False, "status": "blocked", "error": {"code": "agent_call_limit", "detail": "已达到本次主 Agent 的子 Agent 调用上限"}},
            )
        profile = self.config.agent_profiles.get(role)
        if not isinstance(profile, dict):
            return _json_tool_output(
                "agent_call",
                {"ok": False, "status": "configuration_error", "error": {"code": "missing_role_profile", "detail": f"未配置子 Agent：{role}"}},
            )
        phase_error = self._validate_agent_call_phase(role)
        if phase_error is not None:
            return _json_tool_output("agent_call", phase_error)
        self._agent_call_count += 1
        try:
            # 延迟导入避免 agents -> environments 的循环依赖。
            from minisweagent.agents import get_agent
            from minisweagent.models import get_model

            child_model_config = dict(self.config.agent_model_config)
            child_model_config["stream_output"] = False
            child_model = get_model(child_model_config)
            child_settings = recursive_merge(self.config.agent_common_config, dict(profile))
            child_settings.pop("description", None)
            child_settings["agent_name"] = role
            child_settings["output_path"] = None
            child_environment = LocalEnvironment(
                cwd=self.config.cwd,
                env=dict(self.config.env),
                timeout=self.config.timeout,
                web_search_engines=list(self.config.web_search_engines),
                web_search_max_results=self.config.web_search_max_results,
                miniqmt_bridge_url=self.config.miniqmt_bridge_url,
                miniqmt_mode="observe" if self.config.account_review_mode else self.config.miniqmt_mode,
                account_journal_dir=self.config.account_journal_dir,
                account_cycle_id=self.config.account_cycle_id,
                account_review_mode=self.config.account_review_mode,
                approval_callback=self.approval_callback,
            )
            child_agent = get_agent(child_model, child_environment, child_settings)
            result = child_agent.run(task)
            if result.get("exit_status") != "Submitted":
                return _json_tool_output(
                    "agent_call",
                    {
                        "ok": False,
                        "status": "error",
                        "error": {
                            "code": result.get("exit_status", "child_agent_incomplete"),
                            "detail": "子 Agent 未提交完整结果，不能把该阶段视为完成",
                        },
                    },
                )
            self._agent_call_roles.append(role)
            return _json_tool_output(
                "agent_call",
                {
                    "ok": True,
                    "status": "success",
                    "data": {
                        "role": role,
                        "exit_status": result.get("exit_status", "unknown"),
                        "submission": result.get("submission", ""),
                        "api_calls": child_agent.n_calls,
                    },
                },
            )
        except InterruptAgentFlow as error:
            message = error.messages[-1] if error.messages else {}
            extra = message.get("extra", {})
            status = extra.get("exit_status", type(error).__name__)
            detail = message.get("content", str(error))
            return _json_tool_output(
                "agent_call",
                {
                    "ok": False,
                    "status": "blocked" if isinstance(error, CommandNotApproved) else "error",
                    "error": {"code": status, "detail": detail},
                },
            )
        except Exception as error:
            return _json_tool_output(
                "agent_call",
                {
                    "ok": False,
                    "status": "error",
                    "error": {"code": type(error).__name__, "detail": f"子 Agent 执行失败：{error}"},
                },
            )

    def _validate_agent_call_phase(self, role: str) -> dict[str, Any] | None:
        """检查账户管理工作流的最小顺序，交易权限仍由交易工具再次校验。"""
        if role == "portfolio_manager" and "financial_research" not in self._agent_call_roles:
            return {
                "ok": False,
                "status": "blocked",
                "error": {
                    "code": "workflow_order",
                    "detail": "必须先完成 financial_research，再调用 portfolio_manager",
                },
            }
        if role == "account_trader" and "portfolio_manager" not in self._agent_call_roles:
            return {
                "ok": False,
                "status": "blocked",
                "error": {
                    "code": "workflow_order",
                    "detail": "必须先完成 portfolio_manager，再调用 account_trader",
                },
            }
        return None

    def _get_miniqmt(self) -> MiniQMTClient:
        if self._miniqmt is None:
            self._miniqmt = MiniQMTClient(
                base_url=self.config.miniqmt_bridge_url,
                timeout=self.config.timeout,
                mode=self.config.miniqmt_mode,
                state_dir=self.config.account_journal_dir,
                cycle_id=self.config.account_cycle_id,
            )
        return self._miniqmt

    @staticmethod
    def _stop_for_approval(
        command: str,
        reason: str,
        *,
        hard_denied: bool,
        subject: str = "Bash 命令",
    ) -> None:
        status = "CommandBlocked" if hard_denied else "CommandNotApproved"
        prefix = "安全策略禁止执行" if hard_denied else "用户未批准执行"
        submission = f"{prefix}{subject}：{reason}\n{command}"
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


def _json_tool_output(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    """将结构化宿主工具结果适配到统一 observation。"""
    ok = result.get("ok") is True
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    return {
        "stdout": json.dumps(result, ensure_ascii=False, sort_keys=True),
        "stderr": "" if ok else str(error.get("detail") or "工具调用失败"),
        "returncode": 0 if ok else -1,
        "exit_code": 0 if ok else None,
        "status": result.get("status", "success" if ok else "error"),
        "timed_out": False,
        "signal": None,
        "termination": None,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "stdout_spill_path": None,
        "stderr_spill_path": None,
        "path": None,
        "operation": operation,
        "content_hash": result.get("input_hash"),
        "exception_info": "",
        "extra": {"error_code": error.get("code")},
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
        "path": None,
        "operation": None,
        "content_hash": None,
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
