import json
import logging
import signal
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments import account_journal, market_monitor, miniqmt, web_fetch
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
    # 买入单点触发已被禁止：price_lte 的真实语义是越跌越买，跳空砸穿也会成交。
    single_point_buy = {
        "plan_id": "buy-600000",
        "stock_code": "600000.SH",
        "side": "BUY",
        "trigger": {"type": "price_lte", "value": 10},
        "order": {"volume": 100, "price": 10},
    }
    assert monitor.replace([single_point_buy])["ok"] is False
    plan = {
        "plan_id": "buy-600000",
        "stock_code": "600000.SH",
        "side": "BUY",
        "trigger": {"type": "price_range", "value": 10, "upper": 10.5},
        "order": {"volume": 100},
    }
    assert monitor.replace([plan])["ok"] is True
    # 买入限价必须由交易工具在提交那一刻推导：提前钉死的限价在价格从区间下沿触发时必然撞破偏离上限。
    assert monitor.replace([{**plan, "order": {"volume": 100, "price": 10.2}}])["ok"] is False
    # 换仓必须成对：声明了资金来源就得有那只票的卖出计划。
    rotation = {**plan, "rotate_from": "000651.SZ"}
    assert monitor.replace([rotation])["ok"] is False

    class FakeQuoteClient:
        def __init__(self, price):
            self.price = price

        def quotes(self, stock_codes):
            assert stock_codes == ["600000.SH"]
            return {
                "ok": True,
                "data": {"ticks": {"600000.SH": {"last_price": self.price, "time": "2026-09-03T10:00:00+08:00"}}},
            }

    # 跌破区间下界不触发：这正是趋势跟随和接刀的区别。
    assert monitor.poll(FakeQuoteClient(9.5))["data"]["events"] == []
    first = monitor.poll(FakeQuoteClient(10.2))
    assert [event["stock_code"] for event in first["data"]["events"]] == ["600000.SH"]
    second = monitor.poll(FakeQuoteClient(10.2))
    assert second["data"]["events"] == []
    assert json.loads((tmp_path / "market-monitor.json").read_text())["plans"][0]["fired"] is True

    # 重算整张表不能让已成交的计划复活；想重新布防必须换 plan_id。
    replaced = monitor.replace([plan])
    assert replaced["data"]["kept_fired"] == ["buy-600000"]
    assert monitor.poll(FakeQuoteClient(10.2))["data"]["events"] == []

    # 只保留全量覆盖一个写入口：clear 已取消，清空必须显式提交空数组。
    env = LocalEnvironment(timeout=5, account_journal_dir=str(tmp_path))
    assert "invalid_argument" in env._execute_account_monitor({"operation": "clear"})["stdout"]
    emptied = env._execute_account_monitor({"operation": "replace", "plans": []})
    assert json.loads(emptied["stdout"])["data"]["plans"] == []


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
    # 盘中候选发现窗口落在 monitor 时段内，两个窗口各自只跑一次。
    assert mini._intraday_scan_key(datetime(2026, 9, 3, 9, 35, tzinfo=tz)) is None
    assert mini._intraday_scan_key(datetime(2026, 9, 3, 10, 5, tzinfo=tz)) == "2026-09-03-1000"
    assert mini._intraday_scan_key(datetime(2026, 9, 3, 10, 11, tzinfo=tz)) is None
    assert mini._intraday_scan_key(datetime(2026, 9, 3, 13, 31, tzinfo=tz)) == "2026-09-03-1330"


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
