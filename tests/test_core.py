import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments import account_journal, market_monitor, web_fetch
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.deepseek_model import DEFAULT_OBSERVATION_TEMPLATE, DeepSeekModel
from minisweagent.models.utils.actions_toolcall import (
    format_toolcall_observation_messages,
)
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
            observation_template="{{ output.stdout }}",
            template_vars=template_vars,
        )

    def get_template_vars(self, **kwargs):
        return {}

    def serialize(self):
        return {"info": {"model": "fake"}}


class DirectAnswerModel(FakeModel):
    def query(self, messages, **kwargs):
        self.calls += 1
        self.query_kwargs = kwargs
        return {"role": "assistant", "content": "直接回答，不需要工具。", "extra": {"actions": []}}


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


def test_financial_manager_agent_call_order_is_host_enforced():
    env = LocalEnvironment(timeout=5)

    blocked_portfolio = env._validate_agent_call_phase("portfolio_manager")
    assert blocked_portfolio["status"] == "blocked"
    assert blocked_portfolio["error"]["code"] == "workflow_order"

    env._agent_call_roles.append("financial_research")
    assert env._validate_agent_call_phase("portfolio_manager") is None
    assert env._validate_agent_call_phase("account_trader")["error"]["code"] == "workflow_order"

    env._agent_call_roles.append("portfolio_manager")
    assert env._validate_agent_call_phase("account_trader") is None


def test_financial_agent_profiles_describe_daily_handoff():
    settings = mini.get_config_from_spec(mini.DEFAULT_CONFIG_FILE)
    research = settings["agents"]["financial_research"]["system_template"]
    portfolio = settings["agents"]["portfolio_manager"]["system_template"]
    trader = settings["agents"]["account_trader"]["system_template"]
    manager = settings["agents"]["financial_manager"]["system_template"]

    assert "buy_candidates" in research
    assert "previous_close_as_of" in research
    assert "selected_candidates" in portfolio
    assert "risk_check" in trader
    assert "financial_research" in manager
    assert "portfolio_manager" in manager
    assert "account_trader" in manager
    # 监控表只有主 Agent 能写：交易 Agent 成交后不可能顺手清掉其他持仓的风控计划。
    assert "account_monitor" not in settings["agents"]["account_trader"]["tools"]
    assert "account_monitor" in settings["agents"]["financial_manager"]["tools"]


def test_market_monitor_persists_plans_and_emits_each_trigger_once(tmp_path):
    monitor = market_monitor.MarketMonitor(tmp_path)
    plan = {
        "plan_id": "buy-600000",
        "stock_code": "600000.SH",
        "side": "BUY",
        "trigger": {"type": "price_lte", "value": 10},
        "order": {"volume": 100},
    }
    assert monitor.replace([plan])["ok"] is True

    class FakeQuoteClient:
        def quotes(self, stock_codes):
            assert stock_codes == ["600000.SH"]
            return {
                "ok": True,
                "data": {
                    "ticks": {
                        "600000.SH": {"last_price": 9.5, "time": "2026-09-03T10:00:00+08:00"}
                    }
                },
            }

    first = monitor.poll(FakeQuoteClient())
    assert [event["stock_code"] for event in first["data"]["events"]] == ["600000.SH"]
    second = monitor.poll(FakeQuoteClient())
    assert second["data"]["events"] == []
    assert json.loads((tmp_path / "market-monitor.json").read_text())["plans"][0]["fired"] is True

    # 只保留全量覆盖一个写入口：clear 已取消，清空必须显式提交空数组。
    env = LocalEnvironment(timeout=5, account_journal_dir=str(tmp_path))
    assert "invalid_argument" in env._execute_account_monitor({"operation": "clear"})["stdout"]
    emptied = env._execute_account_monitor({"operation": "replace", "plans": []})
    assert json.loads(emptied["stdout"])["data"]["plans"] == []


def test_account_loop_schedules_premarket_monitoring_and_close_review():
    tz = mini.TRADING_TZ
    assert mini._account_loop_slot(datetime(2026, 9, 3, 9, 20, tzinfo=tz)) == (
        "premarket",
        "2026-09-03",
    )
    assert mini._account_loop_slot(datetime(2026, 9, 3, 9, 35, tzinfo=tz)) == (
        "monitor",
        "2026-09-03",
    )
    assert mini._account_loop_slot(datetime(2026, 9, 3, 12, 50, tzinfo=tz)) == (
        "midday",
        "2026-09-03",
    )
    assert mini._account_loop_slot(datetime(2026, 9, 3, 15, 10, tzinfo=tz)) == (
        "review",
        "2026-09-03-close",
    )


def test_account_journal_reads_observation_todo(tmp_path):
    todo = tmp_path / "observation-todo.md"
    todo.write_text("明日核对现金和监控计划", encoding="utf-8")

    result = account_journal.read_account_journal(tmp_path)

    assert result["data"]["observation_todo"] == "明日核对现金和监控计划"


def test_web_search_failure_diagnostics_are_visible_in_model_observation():
    attempts = [{
        "query": "今日行情",
        "engine": "baidu_html",
        "status": "blocked",
        "result_count": 0,
        "detail": "HTTP 429",
    }]
    messages = format_toolcall_observation_messages(
        actions=[{"tool": "web_search", "tool_call_id": "search_1"}],
        outputs=[{
            "status": "error",
            "returncode": -1,
            "exit_code": None,
            "timed_out": False,
            "signal": None,
            "termination": None,
            "path": None,
            "operation": "web_search",
            "content_hash": None,
            "stdout": "",
            "stderr": "网页搜索引擎均不可用。",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout_spill_path": None,
            "stderr_spill_path": None,
            "exception_info": "网页搜索引擎均不可用。",
            "extra": {"error_code": "WEB_SEARCH_UNAVAILABLE", "attempts": attempts},
        }],
        observation_template=DEFAULT_OBSERVATION_TEMPLATE,
    )

    # 使用模型默认模板时，逐引擎状态必须位于发送给模型的 tool message 中。
    assert "WEB_SEARCH_UNAVAILABLE" in messages[0]["content"]
    assert "baidu_html" in messages[0]["content"]
    assert "HTTP 429" in messages[0]["content"]


def test_web_fetch_extracts_title_date_and_visible_content(monkeypatch):
    payload = """<!doctype html><html><head><title>测试文章</title>
    <meta property="article:published_time" content="2026-08-28T10:00:00+08:00">
    <style>.hidden { display: none }</style></head><body>
    <h1>测试文章</h1><p>这是正文内容。</p><script>alert('ignore')</script>
    </body></html>"""
    monkeypatch.setattr(
        web_fetch,
        "_http_get",
        lambda _url, *, timeout: (payload, "text/html", "utf-8"),
    )

    result = web_fetch.execute_web_fetch("https://example.test/article?utm_source=test", timeout=2)

    assert result["status"] == "success"
    assert result["extra"]["page"]["title"] == "测试文章"
    assert result["extra"]["page"]["published_at"] == "2026-08-28T10:00:00+08:00"
    assert "这是正文内容。" in result["stdout"]
    assert "alert" not in result["stdout"]
    assert result["extra"]["page"]["content_type"] == "text/html"


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


def test_cli_session_keeps_context_until_exit(tmp_path, monkeypatch):
    session_path = tmp_path / ".sessions" / "session.json"
    agent = DefaultAgent(
        DirectAnswerModel(),
        LocalEnvironment(timeout=5),
        system_template="你是助手。",
        instance_template="{{ task }}",
        output_path=session_path,
    )
    requests = iter(["第二个问题", "/exit"])
    monkeypatch.setattr(mini, "terminal_prompt", lambda _label: next(requests))

    mini._run_session(agent, "第一个问题", interactive=True)

    assert [message["content"] for message in agent.messages if message["role"] == "user"] == [
        "第一个问题",
        "第二个问题",
    ]
    saved = json.loads(session_path.read_text())
    assert [message["content"] for message in saved["messages"] if message["role"] == "user"] == [
        "第一个问题",
        "第二个问题",
    ]
    saved_text = session_path.read_text(encoding="utf-8")
    assert "你是助手。" in saved_text
    assert "第一个问题" in saved_text
    assert "\\u4f60" not in saved_text


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


def test_deepseek_model_streams_and_emits_configured_tool_calls(monkeypatch, capsys):
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
    assert "max_tokens" not in captured
    assert captured["tool_choice"] == "auto"
    assert captured["stream"] is True
    assert captured["timeout"] == 60
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert captured["tools"][0]["function"]["name"] == "bash"
    assert [tool["function"]["name"] for tool in captured["tools"]] == [
        "bash", "str_replace_editor", "web_search", "web_fetch"
    ]
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
