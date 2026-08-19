from pathlib import Path
from types import SimpleNamespace

import pytest

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.bash_policy import analyze_bash_command
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import CommandNotApproved, Submitted
from minisweagent.models.deepseek_model import DeepSeekModel
from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages
from minisweagent.run import mini


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
            observation_template="{{ output.output }}",
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
    assert result["output"] == "ENV_OK"
    assert result["returncode"] == 0


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
    assert secret["output"] == ""
    assert "DS_KEY" not in env.get_template_vars()


def test_local_environment_executes_approved_command(tmp_path):
    target = tmp_path / "approved.txt"
    target.write_text("ok")
    env = LocalEnvironment(timeout=5, approval_callback=lambda _command, _reason: True)

    result = env.execute({"command": f"chmod 600 {target}"})

    assert result["returncode"] == 0


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
    assert [message["role"] for message in agent.messages] == ["system", "user", "assistant", "exit"]


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
    assert message["extra"]["actions"] == [{"command": "printf MODEL_OK", "tool_call_id": "call_1"}]
    assert message["extra"]["reasoning_content"] == "thinking "
    assert message["reasoning_content"] == "thinking "
    assert "[思考] thinking" in capsys.readouterr().out
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


def test_tool_observation_truncates_output():
    messages = format_toolcall_observation_messages(
        actions=[{"command": "printf", "tool_call_id": "call_1"}],
        outputs=[{"output": "x" * 1001, "returncode": 0, "exception_info": ""}],
        observation_template="{{ output.output }}",
    )

    assert len(messages[0]["content"]) == 1000
    assert len(messages[0]["extra"]["raw_output"]) == 1000
    assert messages[0]["extra"]["output_truncated"] is True
