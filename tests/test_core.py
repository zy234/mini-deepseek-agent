import base64
import json
import logging
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from minisweagent import config
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments import local as local_env
from minisweagent.environments import miniqmt, web_fetch
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError
from minisweagent.models.deepseek_model import DEFAULT_OBSERVATION_TEMPLATE, DeepSeekModel
from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages
from minisweagent.run import inspect, mini
from minisweagent.trading import pipeline

# 1x1 透明 PNG：只用来验证图片被读成 base64 塞进请求，不需要真图。
TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
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
            "extra": {"actions": [{"tool": "bash", "command": command, "tool_call_id": f"call_{self.calls}"}]},
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


def test_buy_limit_price_is_derived_from_latest_quote_within_deviation_cap(tmp_path, monkeypatch):
    """买入限价必须由宿主按提交时最新价推导：计划提前钉死限价时价格从区间下沿触发必然被自己的偏离上限拒掉。"""
    monkeypatch.setenv("MINIQMT_ACCOUNT_ID", "test-account")
    monkeypatch.setattr(miniqmt, "_is_trading_time", lambda _now: True)
    client = miniqmt.MiniQMTClient(
        base_url="http://bridge.local", timeout=5, mode="auto_execute", state_dir=tmp_path
    )
    last_price = 14.81
    submitted: list[dict] = []

    def fake_request(method, path, payload=None, params=None, query=None, unknown_on_network_error=False):
        if path == "/api/v1/trader/ensure-ready":
            return {"ok": True, "status": "success", "operation": path, "data": {}, "error": None}
        if path == "/api/v1/market/full-tick":
            tick = {"lastPrice": last_price, "time": datetime.now(miniqmt.TRADING_TZ).isoformat()}
            return {
                "ok": True,
                "status": "success",
                "operation": path,
                "data": {"ticks": {"000902.SZ": tick}},
                "error": None,
            }
        if path == "/api/v1/trader/asset":
            asset = {"cash": 126_411.31, "total_asset": 140_757.31}
            return {"ok": True, "status": "success", "operation": path, "data": {"asset": asset}, "error": None}
        if path == "/api/v1/trader/order/live":
            submitted.append(dict(payload))
            return {"ok": True, "status": "success", "operation": path, "data": {"order_id": "1"}, "error": None}
        raise AssertionError(f"未预期的请求 {path}")

    monkeypatch.setattr(client, "_request", fake_request)
    base = {"stock_code": "000902.SZ", "side": "BUY", "volume": 800}
    # 追高上限 14.99 相对现价 14.81 偏离 121bp：作为固定限价必被拒，作为 price_cap 则推导出合规限价。
    accepted = client.trade("submit", {**base, "client_intent_id": "cap-derives-price", "price_cap": 14.99})
    assert accepted["ok"] is True
    price = submitted[0]["price"]
    assert submitted[0]["price_type"] == "FIX" and "price_cap" not in submitted[0]
    # 推导价高于现价才吃得到卖盘，同时必须落在宿主偏离上限内，且不越过追高上限。
    assert last_price <= price <= 14.99
    assert abs(price - last_price) / last_price * 10_000 <= 50.0
    assert round(price * 100) == price * 100  # 必须落在 0.01 元报价网格上

    blocked = client.trade("submit", {**base, "client_intent_id": "fixed-price-blocked", "price": 14.99})
    assert blocked["error"]["code"] == "blocked" and "偏离" in blocked["error"]["detail"]

    # 现价已高过追高上限就是跳空追高，不是计划里的突破。
    gapped = client.trade("submit", {**base, "client_intent_id": "gap-up-blocked", "price_cap": 14.50})
    assert gapped["error"]["code"] == "blocked" and "跳空追高" in gapped["error"]["detail"]

    both = client.trade("submit", {**base, "client_intent_id": "both-rejected", "price": 14.8, "price_cap": 14.99})
    assert both["error"]["code"] == "invalid_argument"
    assert len(submitted) == 1


def test_low_price_stock_buy_is_blocked_instead_of_resting_unfilled(tmp_path, monkeypatch):
    """0.01 元的报价单位在低价股上就吃掉整个上浮额度：推不出高于现价的限价就必须拒单。

    挂一个等于最新价的限价只会留下不成交的悬单，让主 Agent 以为没买到而账户里多一笔在途委托。
    """
    monkeypatch.setenv("MINIQMT_ACCOUNT_ID", "test-account")
    monkeypatch.setattr(miniqmt, "_is_trading_time", lambda _now: True)
    client = miniqmt.MiniQMTClient(
        base_url="http://bridge.local", timeout=5, mode="auto_execute", state_dir=tmp_path
    )
    submitted: list[dict] = []

    def fake_request(method, path, payload=None, params=None, query=None, unknown_on_network_error=False):
        if path == "/api/v1/trader/ensure-ready":
            return {"ok": True, "status": "success", "operation": path, "data": {}, "error": None}
        if path == "/api/v1/market/full-tick":
            tick = {"lastPrice": 1.50, "time": datetime.now(miniqmt.TRADING_TZ).isoformat()}
            return {
                "ok": True,
                "status": "success",
                "operation": path,
                "data": {"ticks": {"000902.SZ": tick}},
                "error": None,
            }
        if path == "/api/v1/trader/order/live":
            submitted.append(dict(payload))
            return {"ok": True, "status": "success", "operation": path, "data": {"order_id": "1"}, "error": None}
        raise AssertionError(f"未预期的请求 {path}")

    monkeypatch.setattr(client, "_request", fake_request)
    # 1.50 元一个 tick 就是 66.7bp，远超 40bp 上浮额度：报 1.51 已经违规，留在 1.50 挂不上。
    result = client.trade(
        "submit",
        {
            "stock_code": "000902.SZ",
            "side": "BUY",
            "volume": 800,
            "client_intent_id": "low-price-blocked",
            "price_cap": 1.60,
        },
    )
    assert result["error"]["code"] == "blocked"
    # 拒单原因必须和跳空追高分开讲：一个是行情跑了，一个是这只票的报价单位装不下额度。
    assert "报价单位" in result["error"]["detail"] and "跳空" not in result["error"]["detail"]
    assert submitted == []


def test_sector_rank_ranks_by_buyable_strength_and_caches_members(tmp_path, monkeypatch):
    """候选发现从板块热度进入：只有龙头在涨、可买小票不动的板块对这个账户没有意义。"""
    client = miniqmt.MiniQMTClient(base_url="http://bridge.local", timeout=5, state_dir=tmp_path)
    # 20 元股一手 2000 元买得起；300 元股一手 30000 元超过默认 20000 单笔上限。
    cheap_up = {f"00000{i}.SZ": (20.0, 19.0) for i in range(1, 7)}
    cheap_flat = {f"00001{i}.SZ": (19.8, 20.0) for i in range(1, 7)}
    pricey_up = {f"60000{i}.SH": (330.0, 300.0) for i in range(1, 7)}
    prices = {**cheap_up, **cheap_flat, **pricey_up}
    members = {
        # 可买票普涨：本账户真能吃到的行情。
        "TGN可买普涨": list(cheap_up),
        # 龙头独舞：贵的在涨，可买的在跌。
        "TGN龙头独舞": list(pricey_up)[:1] + list(cheap_flat),
        # 全是买不起的票：可买家数不足，直接不返回。
        "TGN买不起": list(pricey_up),
        "SW2无关行业": list(cheap_flat),
    }
    calls: list[str] = []

    def fake_request(method, path, payload=None, params=None):
        calls.append(path)
        if path == "/api/v1/market/sectors":
            return {"ok": True, "status": "success", "operation": "s", "data": {"items": list(members)}, "error": None}
        if path.startswith("/api/v1/market/sectors/"):
            name = path.split("/")[-2]
            from urllib.parse import unquote

            stocks = list(prices) if unquote(name) == miniqmt.MARKET_UNIVERSE_SECTOR else members[unquote(name)]
            return {"ok": True, "status": "success", "operation": "m", "data": {"stocks": stocks}, "error": None}
        if path == "/api/v1/market/full-tick":
            ticks = {
                code: {"lastPrice": last, "lastClose": close, "high": last, "low": close, "volume": 100, "amount": 1e7}
                for code, (last, close) in prices.items()
                if code in (payload or {}).get("codes", [])
            }
            return {"ok": True, "status": "success", "operation": "t", "data": {"ticks": ticks}, "error": None}
        raise AssertionError(f"未预期的请求 {path}")

    monkeypatch.setattr(client, "_request", fake_request)
    result = client.sector_rank(family="TGN", limit=5, min_buyable=2)
    assert result["ok"] is True
    ranked = [row["sector"] for row in result["data"]["sectors"]]
    # 买不起的板块被 min_buyable 挡掉；龙头独舞排在可买普涨之后。
    assert ranked == ["TGN可买普涨", "TGN龙头独舞"]
    strongest = result["data"]["sectors"][0]
    assert strongest["buyable_count"] == 6 and strongest["buyable_median_change_pct"] > 0
    assert result["data"]["sectors"][1]["buyable_median_change_pct"] < 0
    assert all(row["lot_cost"] <= 20_000 for row in strongest["top_buyable"])

    # 成分股按交易日缓存：同一天第二次调用不再逐个板块拉成分股。
    assert (tmp_path / miniqmt.SECTOR_MEMBER_CACHE).is_file()
    calls.clear()
    again = client.sector_rank(family="TGN", limit=5, min_buyable=2)
    assert again["ok"] is True
    assert "/api/v1/market/sectors" not in calls
    assert sum(1 for path in calls if path.startswith("/api/v1/market/sectors/")) == 1


def test_screen_trend_enrichment_labels_breakout_and_broken(monkeypatch):
    """趋势判定必须由代码给出：涨幅榜第一名可能是过热票，模型不能靠目测决定顺势与否。"""
    client = miniqmt.MiniQMTClient(base_url="http://bridge.local", timeout=5)
    # 盘中 10:00 只走了 30 分钟，日线里的当日 bar 也只有半小时成交量。
    intraday = "20260908 10:00:00"
    ticks = {
        # 放量突破不含当日的前高：突破。
        "600001.SH": {
            "lastPrice": 21.4,
            "lastClose": 21.0,
            "high": 21.5,
            "low": 20.9,
            "volume": 1500,
            "amount": 6.3e8,
            "timetag": intraday,
        },
        # 跌破 20 日线：破位，不管当日涨了多少都不能进候选。
        "600002.SH": {
            "lastPrice": 8.0,
            "lastClose": 7.6,
            "high": 8.1,
            "low": 7.5,
            "volume": 900,
            "amount": 7.2e8,
            "timetag": intraday,
        },
    }
    # 前 21 根历史加 1 根当日 bar；当日 bar 的最高价故意高于历史前高，用来验证 pivot 排除了今天。
    dates = [20260801 + i for i in range(21)] + [20260908]
    rising = [{"close": 15.0 + i * 0.3, "high": 15.2 + i * 0.3, "low": 14.8 + i * 0.3, "volume": 1000} for i in range(21)]
    rising.append({"close": 21.4, "high": 21.5, "low": 20.9, "volume": 1500})
    falling = [{"close": 20.0 - i * 0.5, "high": 20.2 - i * 0.5, "low": 19.8 - i * 0.5, "volume": 1000} for i in range(21)]
    falling.append({"close": 8.0, "high": 8.1, "low": 7.5, "volume": 900})

    def fake_request(method, path, payload=None, params=None):
        if path == "/api/v1/market/full-tick":
            return {"ok": True, "status": "success", "operation": "t", "data": {"ticks": ticks}, "error": None}
        if path == "/api/v1/market/history/download2":
            return {"ok": True, "status": "success", "operation": "d", "data": {}, "error": None}
        if path == "/api/v1/market/history/local":
            frame = {"columns": ["close", "high", "low", "volume"], "index": dates}
            return {
                "ok": True,
                "status": "success",
                "operation": "h",
                "data": {
                    "data": {
                        "600001.SH": {**frame, "data": [list(bar.values()) for bar in rising]},
                        "600002.SH": {**frame, "data": [list(bar.values()) for bar in falling]},
                    }
                },
                "error": None,
            }
        raise AssertionError(f"未预期的请求 {path}")

    monkeypatch.setattr(client, "_request", fake_request)
    result = client.screen(stock_codes=list(ticks), sort_by="amount_desc", limit=2, enrich_trend=True)
    assert result["ok"] is True
    rows = {row["stock_code"]: row for row in result["data"]["rows"]}
    assert {code: row["trend_gate"] for code, row in rows.items()} == {
        "600001.SH": "breakout",
        "600002.SH": "broken",
    }
    breakout = rows["600001.SH"]
    # pivot 是不含当日的前高 21.2；用了当日 bar 的 21.5 就等于"突破自己"，条件永远成立也永远没意义。
    assert breakout["pivot"] == 21.2
    assert breakout["high_20d_gap_pct"] > 0
    # 基准量按已交易的 30 分钟折算：1500 / (1000 × 30/240) = 12.0，不折算的话盘中永远算不出放量。
    assert breakout["session_minutes"] == 30
    assert breakout["vol_ratio"] == 12.0
    # 突破区间和止损参考由工具算好，组合经理直接引用，不用自己乘系数。
    assert breakout["breakout_entry"] == {"lower": 21.22, "upper": 21.62}
    assert breakout["stop_ref"] > 0
    assert "breakout_entry" not in rows["600002.SH"]
    assert result["data"]["trend_gate_counts"] == {"breakout": 1, "broken": 1}


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


def test_browser_survives_page_errors_and_reports_failed_cleanup(monkeypatch, caplog):
    """页面级抓取失败不能销毁浏览器单例；连接真断了才重建；清理失败必须留痕，否则孤儿进程无人知晓。"""
    from playwright.sync_api import Error as PlaywrightError

    class FakeBrowser:
        def __init__(self, connected=True, close_error=None):
            self.connected = connected
            self.close_error = close_error
            self.closed = False

        def is_connected(self):
            return self.connected

        def new_context(self, **_kwargs):
            raise PlaywrightError("net::ERR_CONNECTION_REFUSED")

        def close(self):
            self.closed = True
            if self.close_error is not None:
                raise self.close_error

    class FakeRuntime:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    def fetch_expecting_failure():
        try:
            web_fetch._browser_get("https://example.test/page", timeout=5)
        except web_fetch.WebFetchError as error:
            return error
        raise AssertionError("抓取应当失败")

    # 抓取失败但连接健在：单例必须留着，销毁它等于给下一次抓取白加一到两秒冷启动。
    alive = FakeBrowser()
    monkeypatch.setattr(web_fetch, "_BROWSER", alive)
    monkeypatch.setattr(web_fetch, "_BROWSER_RUNTIME", FakeRuntime())
    assert fetch_expecting_failure().code == "WEB_FETCH_BROWSER_ERROR"
    assert web_fetch._BROWSER is alive and alive.closed is False

    # 连接已断：必须连 driver 一起收掉重建，否则后续抓取全都撞在同一个死浏览器上。
    dead, runtime = FakeBrowser(connected=False), FakeRuntime()
    monkeypatch.setattr(web_fetch, "_BROWSER", dead)
    monkeypatch.setattr(web_fetch, "_BROWSER_RUNTIME", runtime)
    fetch_expecting_failure()
    assert web_fetch._BROWSER is None and dead.closed is True and runtime.stopped is True

    # 关不掉意味着 chromium 残留：全局照样置空以便重建，但必须留下能追溯的日志。
    stubborn = FakeBrowser(connected=False, close_error=PlaywrightError("target closed"))
    monkeypatch.setattr(web_fetch, "_BROWSER", stubborn)
    monkeypatch.setattr(web_fetch, "_BROWSER_RUNTIME", FakeRuntime())
    with caplog.at_level(logging.ERROR, logger="minisweagent.web_fetch"):
        fetch_expecting_failure()
    assert web_fetch._BROWSER is None and "chromium 进程可能残留" in caplog.text


def test_sigterm_hook_closes_browser_without_stealing_existing_handler():
    """launchd 停任务发 SIGTERM，Python 默认不跑 atexit：不接这个信号就会漏下 chromium 孤儿。"""
    original = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        web_fetch._install_sigterm_hook()
        assert signal.getsignal(signal.SIGTERM) is web_fetch._on_sigterm

        # 宿主自己装了收尾逻辑时不许抢：踩掉别人的 handler 比漏几个进程更糟。
        def host_handler(_signum, _frame):
            raise AssertionError("不应被调用")

        signal.signal(signal.SIGTERM, host_handler)
        web_fetch._install_sigterm_hook()
        assert signal.getsignal(signal.SIGTERM) is host_handler
    finally:
        signal.signal(signal.SIGTERM, original)


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

    assert captured["model"] == "deepseek-flash"
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

    # 读图角色：轨迹里只存图片路径，发请求这一刻才读成 base64 塞进 user 消息。
    chart = Path(__file__).parent / "fixture-chart.png"
    chart.write_bytes(base64.b64decode(TINY_PNG))
    try:
        content = model._api_messages(
            [{"role": "user", "content": "看图", "extra": {"images": [str(chart)]}}]
        )[0]["content"]
        assert content[0] == {"type": "text", "text": "看图"}
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        # 平台只接受 user 消息里的图片，别处带图会 400，所以宿主自己先拦住。
        with pytest.raises(ValueError, match="不能携带图片"):
            model._api_messages([{"role": "system", "content": "s", "extra": {"images": [str(chart)]}}])
    finally:
        chart.unlink()

    # JSON 输出角色：请求里带 response_format，且没有工具可用——它真去调工具就是格式错误。
    captured.clear()
    with pytest.raises(FormatError):
        model.query([{"role": "user", "content": "只要 JSON"}], tools=[], json_output=True)
    assert captured["response_format"] == {"type": "json_object"}
    assert "tools" not in captured


class FakeMiniQMT:
    """假 MiniQMT：形状与真实 Bridge 一致，数据是构造的，图能真的渲染出来。

    真实客户端的取数细节已经由 miniqmt 的测试覆盖，这里要验的是流水线：宿主取什么、
    注入什么、模型看到什么图、最后下出什么单。
    """

    submissions: list[dict] = []

    def __init__(self, **kwargs):
        self.mode = kwargs.get("mode", "observe")
        self.pool = {
            "TGN热门一": ["600001.SH", "600002.SH", "600003.SH"],
            "TGN热门二": ["000004.SZ", "000005.SZ", "000006.SZ"],
        }
        self.held = "603386.SH"

    # ---- 行情 ----
    def download_sectors(self):
        return _ok({"downloaded": True})

    def sector_rank(self, *, family="TGN", limit=15, min_buyable=3):
        return _ok(
            {
                "family": family,
                "quote_at": "20260910 09:20:03",
                "sectors": [
                    {
                        "sector": name,
                        "members": len(codes),
                        "members_quoted": len(codes),
                        "up_count": len(codes),
                        "up_ratio": 0.8,
                        "median_change_pct": 3.1,
                        "amount": 1.2e9,
                        "buyable_count": len(codes),
                        "buyable_median_change_pct": 4.2 - index,
                        "top_buyable": [],
                    }
                    for index, (name, codes) in enumerate(self.pool.items())
                ][:limit],
            }
        )

    def screen(self, *, sector_name="", stock_codes=None, sort_by="change_pct_desc", limit=20, enrich_trend=False):
        codes = self.pool.get(sector_name, []) if sector_name else list(stock_codes or [])
        return _ok(
            {
                "sector": sector_name,
                "quote_at": "20260910 10:00:03",
                "universe_size": len(codes),
                "rows": [_row(code) for code in codes[:limit]],
                "trend_gate_counts": {"breakout": len(codes)},
            }
        )

    def quotes(self, stock_codes):
        return _ok(
            {
                "ticks": {
                    code: {"lastPrice": 3900.0, "lastClose": 3880.0, "amount": 9.9e11, "timetag": "20260910 10:00:03"}
                    for code in stock_codes
                }
            }
        )

    def history(self, stock_codes, *, period="1d", start_time="", end_time=""):
        return _ok(
            {
                "period": period,
                "bars": {code: (_daily_bars() if period == "1d" else _minute_bars()) for code in stock_codes},
                "empty_codes": [],
            }
        )

    # ---- 账户 ----
    def account(self, view):
        if view == "snapshot":
            return _ok(
                {
                    "snapshot_at": "2026-09-10T10:00:03+08:00",
                    "result": {
                        "assets": {"asset": {"cash": 60000.0, "total_asset": 100000.0, "market_value": 40000.0, "frozen_cash": 0.0}},
                        "positions": {
                            "items": [
                                {
                                    "stock_code": self.held,
                                    "instrument_name": "骏亚科技",
                                    "volume": 600,
                                    "can_use_volume": 600,
                                    "yesterday_volume": 600,
                                    "avg_price": 15.0,
                                    "last_price": 15.6,
                                    "market_value": 9360.0,
                                    "float_profit": 360.0,
                                    "profit_rate": 0.04,
                                }
                            ]
                        },
                    },
                }
            )
        return _ok({"snapshot_at": "2026-09-10T10:00:03+08:00", "result": {"items": []}})

    def trade(self, operation, inputs):
        FakeMiniQMT.submissions.append({"operation": operation, "inputs": inputs, "mode": self.mode})
        if self.mode == "observe":
            return {"ok": False, "status": "blocked", "operation": operation, "data": None,
                    "error": {"code": "blocked", "detail": "交易工具处于 observe 模式"}}
        return _ok({"order_id": "9001"})


def _ok(data):
    return {"ok": True, "status": "success", "operation": "fake", "data": data, "error": None}


def _row(code):
    return {
        "stock_code": code,
        "last_price": 15.6,
        "last_close": 15.0,
        "change_pct": 4.0,
        "open": 15.1,
        "high": 15.7,
        "low": 15.0,
        "close_position": 0.86,
        "volume": 12000.0,
        "amount": 1.8e8,
        "lot_cost": 1560.0,
        "buyable": True,
        "trend_gate": "breakout",
        "ma20": 14.2,
        "vol_ratio": 1.9,
        "pivot": 15.4,
        "stop_ref": 14.3,
    }


def _daily_bars():
    """40 根递增日线，最后一根是当日 bar。图要真的画出来，所以字段必须齐。"""
    bars = []
    for index in range(40):
        close = 12.0 + index * 0.1
        bars.append(
            {
                "date": int(f"2026080{index + 1}") if index < 9 else 20260810 + index - 9,
                "open": close - 0.05,
                "high": close + 0.12,
                "low": close - 0.15,
                "close": close,
                "volume": 9000.0 + index * 30,
                "amount": (9000.0 + index * 30) * close * 100,
            }
        )
    return bars


def _minute_bars():
    return [
        {
            "date": int(f"20260910{9 + (index + 30) // 60:02d}{(index + 30) % 60:02d}00"),
            "open": 15.4,
            "high": 15.65,
            "low": 15.35,
            "close": 15.4 + index * 0.005,
            "volume": 120.0 + index,
            "amount": (120.0 + index) * 15.5 * 100,
        }
        for index in range(45)
    ]


class ScriptedModel:
    """按 system prompt 分辨角色的假模型。第一次选池故意编一个池子外的代码，验证宿主会打回。"""

    seen: dict[str, int] = {}
    images: list[list[str]] = []

    def __init__(self, **kwargs):
        self.config = SimpleNamespace(model_name="fake", stream_output=False)
        self.calls = 0

    def format_message(self, **kwargs):
        return {key: value for key, value in kwargs.items() if value is not None}

    def query(self, messages, **kwargs):
        self.calls += 1
        system = messages[0]["content"]
        # 汇总执行的 prompt 里也会提到读图，所以先认它，再认另外两个。
        if "汇总执行" in system:
            return self._executor()
        if "盘前选池" in system:
            return self._scout(kwargs)
        return self._reader(messages, kwargs)

    def _scout(self, kwargs):
        assert kwargs.get("json_output") is True, "选池角色必须走 JSON 输出"
        count = ScriptedModel.seen["scout"] = ScriptedModel.seen.get("scout", 0) + 1
        if count == 1:
            # 凭记忆编的代码：宿主必须打回，不能让它进待观测清单。
            picks = [{"stock_code": "600519.SH", "reason": "记错了", "risk": "无"}]
        else:
            picks = [{"stock_code": "600001.SH", "reason": "站上前高", "risk": "破 14.3 走"}]
        return _answer(
            json.dumps(
                {
                    "market_view": "指数强势",
                    "sectors": [
                        {"sector": "TGN热门一", "reason": "主线", "picks": picks},
                        {
                            "sector": "TGN热门二",
                            "reason": "补涨",
                            "picks": [{"stock_code": "000004.SZ", "reason": "放量", "risk": "跌破均价"}],
                        },
                    ],
                },
                ensure_ascii=False,
            )
        )

    def _reader(self, messages, kwargs):
        assert kwargs.get("json_output") is True, "读图角色必须走 JSON 输出"
        images = (messages[1].get("extra") or {}).get("images") or []
        ScriptedModel.images.append(images)
        assert images, "读图角色必须真的收到图片路径"
        # 本组标的从任务行里取：注入的正文里还有大盘代码，按 JSON 字段乱抓会把指数也算进来。
        listed = re.search(r"标的 ([^。]+)。", messages[1]["content"])
        unique = list(dict.fromkeys((listed.group(1) if listed else "").split("、")))
        ScriptedModel.seen["reader"] = ScriptedModel.seen.get("reader", 0) + 1
        return _answer(
            json.dumps(
                {
                    "index_view": "大盘在均价上方",
                    "verdicts": [
                        {
                            "stock_code": code,
                            "action": "BUY" if code.startswith("6000") else "HOLD",
                            "confidence": 0.7,
                            "price_hint": 15.8,
                            "reason": "日线突破，分钟站上均价",
                            "risk": "破均价走",
                        }
                        for code in unique
                    ],
                },
                ensure_ascii=False,
            )
        )

    def _executor(self):
        ScriptedModel.seen["executor"] = ScriptedModel.seen.get("executor", 0) + 1
        if self.calls == 1:
            return _actions(
                [
                    {
                        "tool": "miniqmt_trade",
                        "tool_call_id": "call_trade",
                        "operation": "submit",
                        "inputs": {
                            "client_intent_id": "round-test-600001",
                            "stock_code": "600001.SH",
                            "side": "BUY",
                            "volume": 100,
                            "price_cap": 15.8,
                        },
                    }
                ]
            )
        if self.calls == 2:
            return _actions(
                [
                    {
                        "tool": "account_journal",
                        "tool_call_id": "call_journal",
                        "operation": "append",
                        "record": {
                            "action": "BUY",
                            "market_view": "指数强势",
                            "account_risk": "现金充足",
                            "decision": "买入 600001.SH 一手",
                            "follow_up": "盯 14.3 止损",
                            "orders": ["BUY 600001.SH 100"],
                            "pitfalls": [],
                            "tool_errors": [],
                        },
                    }
                ]
            )
        return _answer("本轮买入 600001.SH 一手，其余持有。")

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
        return {"info": {"model": "scripted"}}


def _answer(content):
    return {"role": "assistant", "content": content, "extra": {"actions": [], "timestamp": time.time()}}


def _actions(actions):
    return {"role": "assistant", "content": None, "extra": {"actions": actions, "timestamp": time.time()}}


def _pipeline_settings(tmp_path: Path) -> dict:
    """用仓库真实配置和真实 prompt 跑，只把规模和目录换成测试值。

    prompt 用 StrictUndefined 渲染，所以这条测试同时在验"宿主注入的变量和 prompt 要求的变量一致"，
    少注入一个变量会直接炸在这里，而不是等到某天盘中那一轮。
    """
    settings = mini.get_config_from_spec(mini.DEFAULT_CONFIG_FILE)
    settings["trading"] = {
        "premarket_at": "09:20",
        "round_interval_minutes": 10,
        "sector_count": 2,
        "picks_per_sector": 1,
        "sectors_scanned": 2,
        "rows_per_sector": 3,
        "index_codes": ["000001.SH"],
        "daily_chart_days": 30,
        "max_parallel_groups": 2,
        "round_json_attempts": 2,
    }
    settings["environment"] = {**settings.get("environment", {}), "miniqmt_mode": "auto_execute", "timeout": 5}
    return settings


def test_trading_pipeline_runs_three_stages_and_executes(tmp_path, monkeypatch):
    """盘前选池 → 并行读图 → 汇总执行的完整一天：图真的渲染，单真的提交，账本真的落盘。"""
    FakeMiniQMT.submissions.clear()
    ScriptedModel.seen.clear()
    ScriptedModel.images.clear()
    monkeypatch.setattr(pipeline, "MiniQMTClient", FakeMiniQMT)
    monkeypatch.setattr(local_env, "MiniQMTClient", FakeMiniQMT)
    monkeypatch.setattr(pipeline, "get_model", lambda config: ScriptedModel(**config))
    monkeypatch.chdir(tmp_path)

    sessions_dir = tmp_path / ".sessions"
    journal_dir = tmp_path / "state"
    day = pipeline.TradingPipeline(
        _pipeline_settings(tmp_path), sessions_dir=sessions_dir, journal_dir=journal_dir, echo=lambda text: None
    )

    watchlist = day.premarket()
    # 编出来的代码被打回，重试一次后才落盘；两个板块各一只，且都来自注入的池子。
    assert ScriptedModel.seen["scout"] == 2
    picked = [pick["stock_code"] for sector in watchlist["sectors"] for pick in sector["picks"]]
    assert picked == ["600001.SH", "000004.SZ"]
    assert (journal_dir / "watchlist" / f"{watchlist['trade_date']}.json").is_file()

    outcome = day.run_round()

    # 两个候选板块各一组，加上持仓组，一共三组并行读图。
    assert ScriptedModel.seen["reader"] == 3
    assert len(outcome["readings"]) == 3
    # 每组都收到了大盘两张图加本组每只票两张图。
    assert all(len(images) >= 4 for images in ScriptedModel.images)
    charts = sorted(path.name for path in (sessions_dir).rglob("*.png"))
    assert "600001.SH-daily.png" in charts and "600001.SH-intraday.png" in charts
    assert "000001.SH-intraday.png" in charts and "603386.SH-daily.png" in charts

    # 汇总执行真的提交了买单，且用的是 price_cap 而不是自己算的固定价。
    assert len(FakeMiniQMT.submissions) == 1
    submitted = FakeMiniQMT.submissions[0]["inputs"]
    assert submitted["stock_code"] == "600001.SH" and submitted["price_cap"] == 15.8
    assert "price" not in submitted

    # 账本有本轮记录，轨迹按父子关系落盘，观测端能把并行读图挂在这一轮下面。
    journal = (journal_dir / "journals" / f"{watchlist['trade_date']}.md").read_text(encoding="utf-8")
    assert "买入 600001.SH 一手" in journal
    roots = inspect.TraceIndex(sessions_dir).sessions()
    round_root = next(root for root in roots if root["agent_name"] == "execution_manager")
    assert [child["agent_name"] for child in round_root["children"]] == ["chart_reader"] * 3
    assert any(child["images"] for child in round_root["children"])

    # 轮次槽位对齐时钟，非连续竞价时段不跑；收盘后启动必须直接退出，不能空转到第二天。
    tz = pipeline.TRADING_TZ
    assert day._round_slot(datetime(2026, 9, 10, 9, 35, tzinfo=tz)) == "0930"
    assert day._round_slot(datetime(2026, 9, 10, 11, 41, tzinfo=tz)) is None
    assert day._round_slot(datetime(2026, 9, 10, 14, 7, tzinfo=tz)) == "1400"
    monkeypatch.setattr(pipeline, "datetime", _FrozenClock(datetime(2026, 9, 10, 15, 30, tzinfo=tz)))
    day.run_day()


class _FrozenClock:
    """冻住时钟：收盘后启动这条路径靠真实时间验不了，但它一旦回归就是无限空转。"""

    def __init__(self, moment: datetime):
        self.moment = moment

    def now(self, _tz=None) -> datetime:
        return self.moment


def test_pipeline_role_configuration_is_guarded(tmp_path):
    """三个阶段角色的形态由校验守着：改坏了不会报错，只会安静地跑偏。"""
    settings = mini.get_config_from_spec(mini.DEFAULT_CONFIG_FILE)
    base_dir = config.builtin_config_dir
    raw = config.load_config_file(base_dir / "deepseek.yaml")
    config.validate_agents(raw, base_dir)

    without_json = json.loads(json.dumps(raw))
    del without_json["agents"]["chart_reader"]["json_output"]
    with pytest.raises(ValueError, match="json_output"):
        config.validate_agents(without_json, base_dir)

    without_trade = json.loads(json.dumps(raw))
    without_trade["agents"]["execution_manager"]["tools"] = ["miniqmt_account"]
    with pytest.raises(ValueError, match="miniqmt_trade"):
        config.validate_agents(without_trade, base_dir)

    missing_role = json.loads(json.dumps(raw))
    del missing_role["agents"]["candidate_scout"]
    with pytest.raises(ValueError, match="candidate_scout"):
        config.validate_agents(missing_role, base_dir)

    # 读图和选池角色不许有工具：它们的输入全部由宿主注入。
    assert settings["agents"]["chart_reader"]["tools"] == []
    assert settings["agents"]["candidate_scout"]["tools"] == []


def _spawn_sleeper(self, log_path: Path, kind: str, mode: str) -> None:
    """替掉真实的流水线启动：验证并发拦截和停止只需要一个长命子进程。"""
    self._close_log()
    self._log = log_path.open("w", encoding="utf-8")
    self._process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=self._log,
        stderr=subprocess.STDOUT,
    )


def test_inspect_edits_roles_and_starts_pipeline_runs(tmp_path, monkeypatch):
    """观测端能改角色配置和 prompt，并就地跑一段流水线；改坏的配置一个字节都不许落盘。"""
    config_dir = tmp_path / "cfg"
    shutil.copytree(config.builtin_config_dir, config_dir, ignore=shutil.ignore_patterns("__pycache__"))
    store = inspect.ConfigStore(config_dir / "deepseek.yaml")
    payload = store.read()
    assert set(config.PIPELINE_ROLES) <= set(payload["agents"])
    assert "miniqmt_trade" in payload["tool_names"] and "agent_call" not in payload["tool_names"]

    agents = payload["agents"]
    agents["chart_reader"]["json_output"] = False
    with pytest.raises(ValueError, match="json_output"):
        store.write_agents(agents)
    # 校验没过就不许落盘：磁盘上的配置还是能跑的那一份。
    assert config.load_config_file(store.path)["agents"]["chart_reader"]["json_output"] is True

    agents["chart_reader"]["json_output"] = True
    agents["execution_manager"]["step_limit"] = 8
    assert store.write_agents(agents)["ok"] is True
    assert config.load_config_file(store.path)["agents"]["execution_manager"]["step_limit"] == 8

    store.write_prompt("chart_reader", "system", "看图并给结论。{{ task }}")
    assert store.read_prompt("chart_reader", "system")["text"].endswith("{{ task }}")
    with pytest.raises(ValueError, match="模板语法错误"):
        store.write_prompt("chart_reader", "system", "{% for %}")

    sessions_dir = tmp_path / ".sessions"
    sessions_dir.mkdir()
    runner = inspect.Runner(sessions_dir, store)
    monkeypatch.setattr(inspect.Runner, "_spawn", _spawn_sleeper)
    assert runner.status()["idle"] is True
    with pytest.raises(ValueError, match="kind"):
        runner.start("agent", "observe")
    state = runner.start("round", "observe")
    assert state["running"] is True and state["kind"] == "round"
    # 同一时刻只允许一个：两轮会各自按自己的额度下单。
    with pytest.raises(inspect.Conflict):
        runner.start("premarket", "observe")
    assert runner.stop()["running"] is False
