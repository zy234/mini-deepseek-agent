from pathlib import Path
from types import SimpleNamespace

import pytest

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import Submitted
from minisweagent.models.deepseek_model import DeepSeekModel
from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages


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


def test_local_environment_extracts_submission():
    env = LocalEnvironment(timeout=5)
    command = "printf '%s\\n' 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' 'report'"

    with pytest.raises(Submitted) as exc_info:
        env.execute({"command": command})

    assert exc_info.value.messages[0]["extra"] == {
        "exit_status": "Submitted",
        "submission": "report\n",
    }


class _FakeToolCall:
    id = "call_1"
    function = SimpleNamespace(name="bash", arguments='{"command":"printf MODEL_OK"}')

    def model_dump(self, exclude_none=True):
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": "bash", "arguments": self.function.arguments},
        }


class _FakeResponse:
    choices = [
        SimpleNamespace(
            finish_reason="tool_calls",
            message=SimpleNamespace(content=None, tool_calls=[_FakeToolCall()]),
        )
    ]
    usage = SimpleNamespace(model_dump=lambda exclude_none=True: {"total_tokens": 5})


def test_deepseek_model_emits_only_bash_tool_call(monkeypatch):
    monkeypatch.setenv("DS_KEY", "test")
    model = DeepSeekModel(retry_attempts=1)
    captured = {}

    def create(**request):
        captured.update(request)
        return _FakeResponse()

    model.client.chat.completions.create = create
    message = model.query(
        [
            {"role": "system", "content": "system", "extra": {"ignored": True}},
            {"role": "user", "content": "task"},
        ]
    )

    assert captured["model"] == "deepseek-v4-flash"
    assert captured["tool_choice"] == "required"
    assert captured["tools"][0]["function"]["name"] == "bash"
    assert message["extra"]["actions"] == [{"command": "printf MODEL_OK", "tool_call_id": "call_1"}]
    assert model._api_messages([{"role": "user", "content": "x", "extra": {"secret": True}}]) == [
        {"role": "user", "content": "x"}
    ]
