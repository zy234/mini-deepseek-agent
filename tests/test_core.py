import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments import editor
from minisweagent.environments.bash_policy import analyze_bash_command
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import CommandNotApproved, Submitted
from minisweagent.models.deepseek_model import DeepSeekModel
from minisweagent.models.utils.actions_toolcall import (
    format_toolcall_observation_messages,
    parse_toolcall_actions,
)
from minisweagent.run import mini
from minisweagent.utils.cli_display import (
    clear_recent_full_blocks,
    render_block,
    render_recent_full_blocks,
    render_tool_actions,
)


class FakeModel:
    def __init__(self):
        self.calls = 0
        self.config = SimpleNamespace(model_name="fake")

    def format_message(self, **kwargs):
        return kwargs

    def query(self, messages):
        self.calls += 1
        command = "printf 'AGENT_OK'"
        if self.calls == 2:
            command = "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nfinished'"
        return {
            "role": "assistant",
            "content": None,
            "extra": {"actions": [{"command": command, "tool_call_id": f"call_{self.calls}"}]},
        }

    def format_observation_messages(self, message, outputs, template_vars=None):
        return format_toolcall_observation_messages(
            actions=message["extra"]["actions"],
            outputs=outputs,
            observation_template="{{ output.stdout }}",
            template_vars=template_vars,
        )

    def get_template_vars(self, **kwargs):
        return {}

    def serialize(self):
        return {"info": {"model": "fake"}}


def test_agent_runs_bash_and_saves_submission(tmp_path: Path):
    trajectory = tmp_path / "trajectory.json"
    agent = DefaultAgent(
        FakeModel(),
        LocalEnvironment(timeout=5),
        system_template="You are an agent.",
        instance_template="{{ task }}",
        step_limit=3,
        output_path=trajectory,
    )

    result = agent.run("verify the loop")

    assert result == {"exit_status": "Submitted", "submission": "finished"}
    assert trajectory.exists()
    assert any(message.get("role") == "tool" for message in agent.messages)


def test_local_environment_captures_output_and_completion():
    env = LocalEnvironment(timeout=5)
    result = env.execute({"command": "printf ENV_OK"})
    assert result["stdout"] == "ENV_OK"
    assert result["returncode"] == 0
    assert result["status"] == "success"
    assert result["timed_out"] is False


def test_local_environment_uses_bash_and_noninteractive_environment():
    env = LocalEnvironment(approval_callback=lambda _command, _reason: True)

    result = env.execute({"command": "printf '%s' \"${BASH_VERSION:+BASH_OK}\""})

    assert result["returncode"] == 0
    assert result["stdout"] == "BASH_OK"


def test_local_environment_explicit_env_overrides_terminal_defaults():
    env = LocalEnvironment(
        env={"TERM": "custom-terminal"},
        approval_callback=lambda _command, _reason: True,
    )

    result = env.execute({"command": "printf '%s' \"$TERM\""})

    assert result["returncode"] == 0
    assert result["stdout"] == "custom-terminal"


def test_local_environment_separates_streams():
    env = LocalEnvironment(timeout=5, approval_callback=lambda _command, _reason: True)

    result = env.execute({"command": "printf OUT; printf ERR >&2"})

    assert result["stdout"] == "OUT"
    assert result["stderr"] == "ERR"
    assert result["stdout_truncated"] is False
    assert result["stderr_truncated"] is False


def test_local_environment_keeps_tail_and_spills_full_output():
    env = LocalEnvironment(timeout=5, approval_callback=lambda _command, _reason: True)
    command = "printf 'HEAD-'; for i in $(seq 1 100000); do printf x; done; printf '%s' '-TAIL'"

    result = env.execute({"command": command})

    assert result["stdout_truncated"] is True
    assert result["stdout"].endswith("-TAIL")
    assert result["stdout_spill_path"] is not None
    spill = Path(result["stdout_spill_path"])
    assert spill.read_text().startswith("HEAD-")
    assert spill.read_text().endswith("-TAIL")


def test_local_environment_drops_spill_when_full_output_exceeds_spill_cap(monkeypatch):
    from minisweagent.environments import local

    monkeypatch.setattr(local, "STREAM_MAX_BYTES", 4)
    monkeypatch.setattr(local, "STREAM_SPILL_MAX_BYTES", 8)
    env = LocalEnvironment(timeout=5, approval_callback=lambda _command, _reason: True)

    result = env.execute({"command": "printf 0123456789abcdef"})

    assert result["stdout_truncated"] is True
    assert result["stdout_spill_path"] is None

def test_bash_action_accepts_optional_execution_metadata():
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(
            name="bash",
            arguments='{"command":"pwd","workdir":"/tmp","timeout":2.5,"description":"检查目录"}',
        ),
    )

    assert parse_toolcall_actions([tool_call], format_error_template="{{ error }}") == [
        {
            "tool": "bash",
            "command": "pwd",
            "workdir": "/tmp",
            "timeout": 2.5,
            "description": "检查目录",
            "tool_call_id": "call_1",
        }
    ]


def test_bash_action_rejects_invalid_optional_metadata():
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="bash", arguments='{"command":"pwd","timeout":0}'),
    )

    with pytest.raises(Exception) as exc_info:
        parse_toolcall_actions([tool_call], format_error_template="{{ error }}")

    assert "timeout 必须是正数" in exc_info.value.messages[0]["content"]


def test_bash_action_rejects_non_object_arguments():
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="bash", arguments="[]"),
    )

    with pytest.raises(Exception) as exc_info:
        parse_toolcall_actions([tool_call], format_error_template="{{ error }}")

    assert "参数必须是对象" in exc_info.value.messages[0]["content"]


def test_bash_action_rejects_unknown_arguments():
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="bash", arguments='{"command":"pwd","shell":"bash"}'),
    )

    with pytest.raises(Exception) as exc_info:
        parse_toolcall_actions([tool_call], format_error_template="{{ error }}")

    assert "未知参数：shell" in exc_info.value.messages[0]["content"]


def test_editor_action_parses_structured_fields():
    tool_call = SimpleNamespace(
        id="edit_1",
        function=SimpleNamespace(
            name="str_replace_editor",
            arguments=(
                '{"command":"str_replace","path":"README.md",'
                '"old_str":"old","new_str":"new","expected_hash":"abc"}'
            ),
        ),
    )

    assert parse_toolcall_actions([tool_call], format_error_template="{{ error }}") == [
        {
            "tool": "str_replace_editor",
            "command": "str_replace",
            "path": "README.md",
            "old_str": "old",
            "new_str": "new",
            "expected_hash": "abc",
            "tool_call_id": "edit_1",
        }
    ]


def test_editor_action_requires_operation_specific_fields():
    tool_call = SimpleNamespace(
        id="edit_1",
        function=SimpleNamespace(
            name="str_replace_editor",
            arguments='{"command":"create","path":"new.txt"}',
        ),
    )

    with pytest.raises(Exception) as exc_info:
        parse_toolcall_actions([tool_call], format_error_template="{{ error }}")

    assert "create 必须提供 file_text" in exc_info.value.messages[0]["content"]


def test_editor_create_view_replace_and_insert(tmp_path: Path):
    approvals = []
    env = LocalEnvironment(
        cwd=str(tmp_path),
        approval_callback=lambda command, reason: approvals.append((command, reason)) or True,
    )

    created = env.execute(
        {"tool": "str_replace_editor", "command": "create", "path": "note.txt", "file_text": "one\ntwo\n"}
    )
    viewed = env.execute(
        {"tool": "str_replace_editor", "command": "view", "path": "note.txt", "view_range": [1, 1]}
    )
    replaced = env.execute(
        {
            "tool": "str_replace_editor",
            "command": "str_replace",
            "path": "note.txt",
            "old_str": "two",
            "new_str": "THREE",
            "expected_hash": viewed["content_hash"],
        }
    )
    inserted = env.execute(
        {
            "tool": "str_replace_editor",
            "command": "insert",
            "path": "note.txt",
            "insert_line": 1,
            "new_str": "middle",
            "expected_hash": replaced["content_hash"],
        }
    )

    assert created["status"] == "success"
    assert viewed["stdout"] == "     1\tone\n"
    assert inserted["status"] == "success"
    assert (tmp_path / "note.txt").read_text() == "one\nmiddle\nTHREE\n"
    assert len(approvals) == 3


def test_editor_rejects_ambiguous_and_stale_replacements(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_text("same same\n")
    env = LocalEnvironment(cwd=str(tmp_path), approval_callback=lambda *_args: True)

    viewed = env.execute({"tool": "str_replace_editor", "command": "view", "path": "note.txt"})
    ambiguous = env.execute(
        {
            "tool": "str_replace_editor",
            "command": "str_replace",
            "path": "note.txt",
            "old_str": "same",
            "new_str": "new",
            "expected_hash": viewed["content_hash"],
        }
    )
    path.write_text("external change\n")
    stale = env.execute(
        {
            "tool": "str_replace_editor",
            "command": "str_replace",
            "path": "note.txt",
            "old_str": "same same",
            "new_str": "new",
            "expected_hash": viewed["content_hash"],
        }
    )

    assert ambiguous["extra"]["error_code"] == "ambiguous_edit"
    assert stale["extra"]["error_code"] == "stale_file"
    assert path.read_text() == "external change\n"


def test_editor_rejects_paths_and_symlinks_outside_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (workspace / "link.txt").symlink_to(outside)
    env = LocalEnvironment(cwd=str(workspace), approval_callback=lambda *_args: True)

    relative_escape = env.execute(
        {"tool": "str_replace_editor", "command": "view", "path": "../outside.txt"}
    )
    symlink_escape = env.execute(
        {"tool": "str_replace_editor", "command": "view", "path": "link.txt"}
    )

    assert relative_escape["extra"]["error_code"] == "outside_workspace"
    assert symlink_escape["extra"]["error_code"] == "outside_workspace"


def test_editor_atomic_failure_preserves_original_file(tmp_path: Path, monkeypatch):
    path = tmp_path / "note.txt"
    path.write_text("old\n")
    env = LocalEnvironment(cwd=str(tmp_path), approval_callback=lambda *_args: True)
    viewed = env.execute({"tool": "str_replace_editor", "command": "view", "path": "note.txt"})
    monkeypatch.setattr(editor.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("failed")))

    result = env.execute(
        {
            "tool": "str_replace_editor",
            "command": "str_replace",
            "path": "note.txt",
            "old_str": "old",
            "new_str": "new",
            "expected_hash": viewed["content_hash"],
        }
    )

    assert result["status"] == "error"
    assert path.read_text() == "old\n"
    assert list(tmp_path.iterdir()) == [path]


def test_local_environment_action_timeout_is_reported():
    env = LocalEnvironment(timeout=5, approval_callback=lambda _command, _reason: True)

    result = env.execute({"command": "sleep 1", "timeout": 0.01})

    assert result["status"] == "timeout"
    assert result["timed_out"] is True
    assert result["exit_code"] is None
    assert result["termination"] in {"graceful", "forced"}


def test_local_environment_timeout_allows_graceful_term_handler():
    env = LocalEnvironment(timeout=0.05, approval_callback=lambda _command, _reason: True)

    result = env.execute({"command": "trap 'exit 0' TERM; sleep 10"})

    assert result["status"] == "timeout"
    assert result["timed_out"] is True
    assert result["termination"] == "graceful"


def test_local_environment_timeout_forces_process_group_that_ignores_term():
    env = LocalEnvironment(timeout=0.05, approval_callback=lambda _command, _reason: True)

    result = env.execute({"command": "trap '' TERM; sleep 10"})

    assert result["status"] == "timeout"
    assert result["timed_out"] is True
    assert result["termination"] == "forced"


def test_local_environment_reports_signal_exit():
    env = LocalEnvironment(timeout=5, approval_callback=lambda _command, _reason: True)

    result = env.execute({"command": "kill -TERM $$"})

    assert result["status"] == "signal"
    assert result["returncode"] < 0
    assert result["exit_code"] is None
    assert result["signal"] == signal.SIGTERM


def test_local_environment_rejects_action_timeout_above_global_limit():
    env = LocalEnvironment(timeout=1, approval_callback=lambda _command, _reason: True)

    result = env.execute({"command": "printf TOO_LATE", "timeout": 2})

    assert result["status"] == "error"
    assert "不能超过全局上限" in result["exception_info"]


def test_local_environment_blocks_dangerous_commands_and_hides_keys(monkeypatch):
    monkeypatch.setenv("DS_KEY", "do-not-print")
    approval_requests = []
    env = LocalEnvironment(
        timeout=5,
        approval_callback=lambda command, reason: approval_requests.append((command, reason)) or False,
    )

    with pytest.raises(CommandNotApproved) as blocked:
        env.execute({"command": "rm -rf /"})
    with pytest.raises(CommandNotApproved) as network:
        env.execute({"command": "curl https://example.com"})
    secret = env.execute({"command": "printf '%s' \"$DS_KEY\""})

    assert blocked.value.messages[0]["extra"]["exit_status"] == "CommandBlocked"
    assert network.value.messages[0]["extra"]["exit_status"] == "CommandNotApproved"
    assert approval_requests == [("curl https://example.com", "命令不在只读允许列表中：curl")]
    assert secret["stdout"] == ""
    assert "DS_KEY" not in env.get_template_vars()


def test_local_environment_executes_approved_command(tmp_path):
    target = tmp_path / "approved.txt"
    target.write_text("ok")
    env = LocalEnvironment(timeout=5, approval_callback=lambda _command, _reason: True)

    result = env.execute({"command": f"chmod 600 {target}"})

    assert result["returncode"] == 0


def test_local_environment_allows_dev_null_redirect(tmp_path):
    command = "wc -l missing.py 2>/dev/null"
    env = LocalEnvironment(
        cwd=str(tmp_path),
        timeout=5,
        approval_callback=lambda _command, _reason: pytest.fail("/dev/null 不应请求审批"),
    )

    result = env.execute({"command": command})

    assert result["returncode"] == 1
    assert result["stdout"] == ""


def test_bash_policy_allows_redirects_to_tmp(tmp_path):
    assert analyze_bash_command("printf x > /tmp/minisweagent-output", str(tmp_path)) is None


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "git status && rg foo src | head -20",
        "sed -n '1,20p' README.md",
        "FOO=x timeout 10 rg foo .",
        "cat $(find . -name '*.py')",
    ],
)
def test_bash_policy_allows_known_read_only_commands(command, tmp_path):
    assert analyze_bash_command(command, str(tmp_path)) is None


@pytest.mark.parametrize(
    "command",
    [
        "printf x > output.txt",
        "printf x > /tmp/../etc/minisweagent-output",
        "cat $(rm output.txt)",
        "sed -i '' README.md",
        "git status && curl https://example.com",
        "python -c 'print(1)'",
        "rg --pre cat pattern .",
        "find . -delete",
        "cat /etc/passwd",
        "cat < /etc/passwd",
        "nohup cat README.md",
        'for file in *; do cat "$file"; done',
    ],
)
def test_bash_policy_requires_approval_for_non_read_only_commands(command, tmp_path):
    risk = analyze_bash_command(command, str(tmp_path))

    assert risk is not None
    assert risk.hard_denied is False


@pytest.mark.parametrize(
    "command",
    ["sudo ls", "rm -rf /", "rm -rf /*", "rm -rf ~/.cache", "rm -rf ../other", "printf x > /dev/disk0"],
)
def test_bash_policy_hard_denies_host_level_commands(command, tmp_path):
    risk = analyze_bash_command(command, str(tmp_path))

    assert risk is not None
    assert risk.hard_denied is True


def test_local_environment_extracts_submission():
    env = LocalEnvironment(timeout=5)
    command = "printf '%s\\n' 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' 'report'"

    with pytest.raises(Submitted) as exc_info:
        env.execute({"command": command})

    assert exc_info.value.messages[0]["extra"] == {
        "exit_status": "Submitted",
        "submission": "report\n",
    }


class DirectAnswerModel(FakeModel):
    def query(self, messages):
        return {"role": "assistant", "content": "直接回答，不需要工具。", "extra": {"actions": []}}


def test_agent_can_finish_without_using_bash():
    agent = DefaultAgent(
        DirectAnswerModel(),
        LocalEnvironment(timeout=5),
        system_template="你是助手。",
        instance_template="{{ task }}",
    )

    assert agent.run("回答问题") == {"exit_status": "Submitted", "submission": "直接回答，不需要工具。"}


def test_agent_continues_with_existing_conversation():
    agent = DefaultAgent(
        DirectAnswerModel(),
        LocalEnvironment(timeout=5),
        system_template="你是助手。",
        instance_template="{{ task }}",
    )

    agent.run("第一个问题")
    agent.continue_run("继续追问")

    assert [message["role"] for message in agent.messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "exit",
    ]
    assert agent.messages[1]["content"] == "第一个问题"
    assert agent.messages[3]["content"] == "继续追问"


def test_cli_does_not_repeat_streamed_submission(monkeypatch):
    output = []
    monkeypatch.setattr(mini.console, "print", lambda value: output.append(value))

    mini._print_result(
        {"exit_status": "Submitted", "submission": "已经流式输出。"},
        submission_streamed=True,
    )

    assert output == []


def test_cli_prints_unstreamed_submission_without_internal_status(monkeypatch):
    output = []
    monkeypatch.setattr(mini.console, "print", lambda value: output.append(value))

    mini._print_result(
        {"exit_status": "Submitted", "submission": "来自 Bash 完成标记。"},
        submission_streamed=False,
    )

    assert output == ["来自 Bash 完成标记。"]


def test_cli_detects_only_direct_answer_as_streamed():
    config = SimpleNamespace(stream_output=True)
    direct_agent = SimpleNamespace(
        model=SimpleNamespace(config=config),
        messages=[
            {"role": "assistant", "content": "直接回答", "extra": {"actions": []}},
            {"role": "exit"},
        ],
    )
    tool_agent = SimpleNamespace(
        model=SimpleNamespace(config=config),
        messages=[
            {"role": "assistant", "content": None, "extra": {"actions": [{"command": "printf"}]}},
            {"role": "exit"},
        ],
    )

    assert mini._submission_was_streamed(direct_agent) is True
    assert mini._submission_was_streamed(tool_agent) is False


def test_cli_session_keeps_context_until_exit(monkeypatch):
    agent = DefaultAgent(
        DirectAnswerModel(),
        LocalEnvironment(timeout=5),
        system_template="你是助手。",
        instance_template="{{ task }}",
    )
    requests = iter(["第二个问题", "/exit"])
    monkeypatch.setattr(mini, "terminal_prompt", lambda _label: next(requests))

    mini._run_session(agent, "第一个问题", interactive=True)

    assert [message["content"] for message in agent.messages if message["role"] == "user"] == [
        "第一个问题",
        "第二个问题",
    ]


class DangerousCommandModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.query_count = 0

    def query(self, messages):
        self.query_count += 1
        return {
            "role": "assistant",
            "content": None,
            "extra": {"actions": [{"command": "rm -rf /", "tool_call_id": "danger"}]},
        }


def test_agent_stops_without_sending_denial_back_to_model():
    model = DangerousCommandModel()
    agent = DefaultAgent(
        model,
        LocalEnvironment(timeout=5, approval_callback=lambda _command, _reason: True),
        system_template="你是助手。",
        instance_template="{{ task }}",
    )

    result = agent.run("执行危险操作")

    assert result["exit_status"] == "CommandBlocked"
    assert model.query_count == 1
    assert [message["role"] for message in agent.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "exit",
    ]


class _FakeToolDelta:
    def __init__(self, *, index=0, call_id=None, name=None, arguments=None):
        self.index = index
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class _FakeChunk:
    def __init__(self, *, delta=None, finish_reason=None, usage=None):
        self.choices = [SimpleNamespace(delta=delta, finish_reason=finish_reason)] if delta else []
        self.usage = usage


class _FakeStream:
    def __iter__(self):
        return iter(
            [
                _FakeChunk(
                    delta=SimpleNamespace(
                        reasoning_content="thinking ",
                        content=None,
                        tool_calls=[
                            _FakeToolDelta(
                                call_id="call_1", name="bash", arguments='{"command":"printf MODEL_OK"}'
                            )
                        ],
                    )
                ),
                _FakeChunk(
                    delta=SimpleNamespace(reasoning_content=None, content=None, tool_calls=[]),
                    finish_reason="tool_calls",
                ),
                _FakeChunk(usage=SimpleNamespace(model_dump=lambda exclude_none=True: {"total_tokens": 5})),
            ]
        )


class _FakeTextStream:
    def __iter__(self):
        return iter(
            [
                _FakeChunk(
                    delta=SimpleNamespace(
                        reasoning_content=None,
                        content="你好，我可以直接回答。",
                        tool_calls=[],
                    ),
                    finish_reason="stop",
                )
            ]
        )


def test_deepseek_model_streams_and_emits_only_bash_tool_call(monkeypatch, capsys):
    monkeypatch.setenv("DS_KEY", "test")
    model = DeepSeekModel(retry_attempts=1, thinking=True)
    captured = {}

    def create(**request):
        captured.update(request)
        return _FakeStream()

    model.client.chat.completions.create = create
    message = model.query(
        [
            {"role": "system", "content": "system", "extra": {"ignored": True}},
            {"role": "user", "content": "task"},
        ]
    )

    assert captured["model"] == "deepseek-v4-flash"
    assert captured["tool_choice"] == "auto"
    assert captured["stream"] is True
    assert captured["timeout"] == 60
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert captured["tools"][0]["function"]["name"] == "bash"
    assert [tool["function"]["name"] for tool in captured["tools"]] == ["bash", "str_replace_editor"]
    assert message["extra"]["actions"] == [
        {"tool": "bash", "command": "printf MODEL_OK", "tool_call_id": "call_1"}
    ]
    assert message["extra"]["reasoning_content"] == "thinking "
    assert message["reasoning_content"] == "thinking "
    cli_output = capsys.readouterr().out
    assert "思考" in cli_output
    assert "工具调用 1 · bash" in cli_output
    assert "printf MODEL_OK" in cli_output
    assert '{"command"' not in cli_output
    assert model.client._client.timeout.read == 60
    assert model._api_messages([{"role": "user", "content": "x", "extra": {"secret": True}}]) == [
        {"role": "user", "content": "x"}
    ]
    assert model._api_messages([message])[0]["reasoning_content"] == "thinking "


def test_deepseek_model_accepts_direct_answer(monkeypatch):
    monkeypatch.setenv("DS_KEY", "test")
    model = DeepSeekModel(retry_attempts=1, stream_output=False)
    model.client.chat.completions.create = lambda **_request: _FakeTextStream()

    message = model.query([{"role": "user", "content": "你是谁"}])

    assert message["content"] == "你好，我可以直接回答。"
    assert message["extra"]["actions"] == []
    assert "tool_calls" not in message


def test_deepseek_model_omits_empty_tool_calls_from_followup_request(monkeypatch):
    monkeypatch.setenv("DS_KEY", "test")
    model = DeepSeekModel(retry_attempts=1, thinking=True, stream_output=False)
    captured = {}

    def create(**request):
        captured.update(request)
        return _FakeTextStream()

    model.client.chat.completions.create = create
    model.query(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "第一个问题"},
            {"role": "assistant", "content": "第一个回答", "tool_calls": []},
            {"role": "user", "content": "继续追问"},
        ]
    )

    assert captured["messages"][2] == {"role": "assistant", "content": "第一个回答"}


def test_tool_observation_preserves_separate_streams():
    messages = format_toolcall_observation_messages(
        actions=[{"command": "printf", "tool_call_id": "call_1"}],
        outputs=[{"stdout": "out", "stderr": "err", "returncode": 0, "status": "success"}],
        observation_template="{{ output.stdout }}|{{ output.stderr }}",
    )

    assert messages[0]["content"] == "out|err"
    assert messages[0]["extra"]["stdout"] == "out"
    assert messages[0]["extra"]["stderr"] == "err"


def test_cli_render_block_truncates_long_text_without_prompt(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    truncated = render_block("工具", "x" * 1001)

    output = capsys.readouterr().out
    assert truncated is True
    assert len(output) < 1200
    assert "已截断" in output


def test_cli_render_block_only_expands_on_explicit_open(capsys):
    clear_recent_full_blocks()
    render_block("思考", "x" * 1001)
    preview = capsys.readouterr().out
    assert "完整" not in preview
    assert preview.count("x") == 1000

    assert render_recent_full_blocks() is True
    expanded = capsys.readouterr().out
    assert "完整" in expanded
    assert expanded.count("x") == 1001


def test_cli_renders_each_tool_action_with_parsed_fields(capsys):
    render_tool_actions(
        [
            {"command": "cat README.md", "description": "读取 README"},
            {
                "command": "cat pyproject.toml",
                "description": "读取配置",
                "workdir": "/workspace",
                "timeout": 5,
            },
        ]
    )

    output = capsys.readouterr().out
    assert "工具调用 1 · bash" in output
    assert "描述  读取 README" in output
    assert "cat README.md" in output
    assert "工具调用 2 · bash" in output
    assert "目录  /workspace" in output
    assert "超时  5 秒" in output


def test_cli_tool_command_truncates_at_1000_characters(capsys):
    render_tool_actions([{"command": "x" * 1200, "description": "长命令"}])

    output = capsys.readouterr().out
    assert output.count("x") == 1000
    assert "已截断，原文 1200 字符" in output
