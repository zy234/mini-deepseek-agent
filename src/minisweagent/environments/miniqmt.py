"""受限的 MiniQMT HTTP 工具。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from minisweagent.environments.account_journal import append_trade_audit

MAX_RESPONSE_BYTES = 1_000_000
STOCK_CODE_PATTERN = re.compile(r"^(?:[036]\d{5})\.(?:SH|SZ)$")
TRADING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
STOP_LOSS_RATIO = 0.10


class MiniQMTClient:
    """只连接宿主配置的 MiniQMT Bridge；模型不能指定地址、账户或凭据。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        mode: str = "observe",
        state_dir: str | Path = ".sessions/account-manager",
        cycle_id: str = "manual",
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("MINIQMT_BRIDGE_URL 必须是无路径、无凭据的 http 或 https 地址")
        if mode not in {"observe", "execute", "auto_execute"}:
            raise ValueError("MINIQMT_AGENT_MODE 只能是 observe、execute 或 auto_execute")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.mode = mode
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.cycle_id = cycle_id
        self._account_ready = False
        self._opener = build_opener(ProxyHandler({}))

    def quotes(self, stock_codes: list[str]) -> dict[str, Any]:
        if not 1 <= len(stock_codes) <= 20:
            return _error("invalid_argument", "stock_codes 必须包含 1 到 20 项")
        try:
            codes = [_stock_code(code) for code in stock_codes]
        except ValueError as exc:
            return _error("invalid_argument", str(exc))
        if len(set(codes)) != len(codes):
            return _error("invalid_argument", "stock_codes 不能重复")
        return self._request("POST", "/api/v1/market/full-tick", payload={"codes": codes})

    def account(self, view: str) -> dict[str, Any]:
        account_id = os.getenv("MINIQMT_ACCOUNT_ID", "").strip()
        if not account_id:
            return _error("configuration_error", "宿主未配置 MINIQMT_ACCOUNT_ID")
        paths = {
            "snapshot": ("/api/v1/trader/asset", "/api/v1/trader/positions"),
            "orders": ("/api/v1/trader/orders",),
            "trades": ("/api/v1/trader/trades",),
        }
        if view not in paths:
            return _error("invalid_argument", f"不支持的账户视图：{view}")
        ready = self._ensure_account_ready(account_id)
        if not ready["ok"]:
            return ready
        responses = [self._request("GET", path, query={"account_id": account_id}) for path in paths[view]]
        failed = next((result for result in responses if not result["ok"]), None)
        if failed:
            return failed
        data = (
            {"assets": responses[0]["data"], "positions": responses[1]["data"]}
            if view == "snapshot"
            else responses[0]["data"]
        )
        return _success(
            operation=f"account_{view}",
            data={"account_id_hash": _account_hash(account_id), "snapshot_at": _now(), "result": data},
        )

    def trade(self, operation: str, inputs: dict[str, Any]) -> dict[str, Any]:
        if self.mode != "execute":
            if self.mode != "auto_execute":
                return self._audited(operation, inputs, _error("blocked", "交易工具处于 observe 模式"))
        if _truthy_env("MINIQMT_KILL_SWITCH"):
            return self._audited(operation, inputs, _error("blocked", "宿主 kill switch 已开启"))
        if self.mode == "auto_execute" and not _is_trading_time(datetime.now(TRADING_TZ)):
            return self._audited(operation, inputs, _error("blocked", "auto_execute 只允许在 A 股连续竞价时段交易"))
        account_id = os.getenv("MINIQMT_ACCOUNT_ID", "").strip()
        if not account_id:
            return self._audited(operation, inputs, _error("configuration_error", "宿主未配置 MINIQMT_ACCOUNT_ID"))
        intent_id = inputs.get("client_intent_id")
        if not isinstance(intent_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", intent_id):
            return self._audited(
                operation,
                inputs,
                _error("invalid_argument", "client_intent_id 必须是 8 到 128 位的稳定标识"),
            )
        existing = self._intent_exists(account_id, intent_id)
        if not existing["ok"] or existing["data"]["exists"]:
            result = existing if not existing["ok"] else _error("duplicate_intent", "该 client_intent_id 已被持久化处理，禁止重复提交")
            return self._audited(operation, inputs, result)
        ready = self._ensure_account_ready(account_id)
        if not ready["ok"]:
            return self._audited(operation, inputs, ready)
        if operation == "submit":
            validation = _order_payload(inputs, account_id)
            if isinstance(validation, str):
                return self._audited(operation, inputs, _error("invalid_argument", validation))
            path, payload = "/api/v1/trader/order/live", validation
            safety = self._validate_order_safety(account_id, payload)
            if not safety["ok"]:
                return self._audited(operation, inputs, safety)
            notional = float(safety["data"].get("notional") or 0.0)
        elif operation == "cancel":
            unknown = set(inputs) - {"client_intent_id", "order_id"}
            if unknown:
                return self._audited(
                    operation,
                    inputs,
                    _error("invalid_argument", f"cancel 包含未知字段：{', '.join(sorted(unknown))}"),
                )
            order_id = inputs.get("order_id")
            if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", order_id):
                return self._audited(operation, inputs, _error("invalid_argument", "order_id 格式无效"))
            orders = self._request(
                "GET",
                "/api/v1/trader/orders",
                query={"account_id": account_id, "cancelable_only": "true"},
            )
            if not orders["ok"] or not _contains_order(orders.get("data"), order_id):
                return self._audited(operation, inputs, _error("blocked", "未确认该委托属于当前账户且可撤，禁止撤单"))
            path = "/api/v1/trader/order/cancel"
            payload = {"account_id": account_id, "order_id": order_id}
            notional = 0.0
        else:
            return self._audited(operation, inputs, _error("invalid_argument", f"不支持的交易 operation：{operation}"))

        # 在发起 Bridge 请求前持久化冻结意图；进程重启或请求超时也不能重复提交。
        reservation = self._reserve_intent(
            account_id=account_id,
            intent_id=intent_id,
            operation=operation,
            stock_code=str(inputs.get("stock_code") or ""),
            side=str(inputs.get("side") or ""),
            volume=int(inputs.get("volume") or 0),
            notional=notional,
        )
        if not reservation["ok"]:
            return self._audited(operation, inputs, reservation)
        result = self._request(
            "POST",
            path,
            payload=payload,
            unknown_on_network_error=True,
        )
        result["operation"] = operation
        result["audit"] = {
            "client_intent_id": intent_id,
            "account_id_hash": _account_hash(account_id),
            "submitted_at": _now(),
        }
        self._finish_intent(account_id, intent_id, result)
        return self._audited(operation, inputs, result)

    def _validate_order_safety(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        side = payload["order_type"]
        volume = int(payload["order_volume"])
        price = payload.get("price")
        max_volume_name = "MINIQMT_MAX_BUY_VOLUME" if side == "BUY" else "MINIQMT_MAX_SELL_VOLUME"
        try:
            max_volume = _positive_int_env(
                max_volume_name,
                _positive_int_env("MINIQMT_MAX_ORDER_VOLUME", 10_000),
            )
        except ValueError as exc:
            return _error("configuration_error", str(exc))
        if volume > max_volume:
            return _error("blocked", f"{side} 数量超过宿主单笔上限 {max_volume}")

        quote_result = self._request("POST", "/api/v1/market/full-tick", payload={"codes": [payload["stock_code"]]})
        if not quote_result["ok"]:
            return _error("blocked", "安全检查无法取得最新行情，禁止交易")
        quote = _extract_quote(quote_result["data"], payload["stock_code"])
        if quote is None:
            return _error("blocked", "最新行情缺少有效价格或时间，禁止交易")
        last_price, quote_at = quote
        if self.mode == "auto_execute":
            try:
                max_age = _positive_int_env("MINIQMT_MAX_QUOTE_AGE_SECONDS", 30)
            except ValueError as exc:
                return _error("configuration_error", str(exc))
            age = (datetime.now(TRADING_TZ) - quote_at.astimezone(TRADING_TZ)).total_seconds()
            if age < -5 or age > max_age:
                return _error("blocked", f"行情已过期或时间异常：age_seconds={age:.1f}")
            if price is None:
                return _error("blocked", f"{side} 必须使用固定限价，不能使用最新价委托")
            try:
                max_deviation_bps = _positive_float_env("MINIQMT_MAX_PRICE_DEVIATION_BPS", 50.0)
            except ValueError as exc:
                return _error("configuration_error", str(exc))
            deviation_bps = abs(float(price) - last_price) / last_price * 10_000
            if deviation_bps > max_deviation_bps:
                return _error("blocked", f"委托价偏离最新价 {deviation_bps:.1f}bp，超过上限")

        if side == "BUY":
            if price is None:
                return _error("blocked", "BUY 必须使用固定限价，不能使用最新价委托")
            notional = round(float(price) * volume, 2)
            try:
                max_notional = _positive_float_env("MINIQMT_MAX_BUY_NOTIONAL", 20_000.0)
                min_cash_ratio = _ratio_env("MINIQMT_MIN_CASH_RATIO", 0.10)
            except ValueError as exc:
                return _error("configuration_error", str(exc))
            if notional > max_notional:
                return _error("blocked", f"BUY 金额超过宿主单笔上限 {max_notional:.2f}")
            asset_result = self._request("GET", "/api/v1/trader/asset", query={"account_id": account_id})
            asset = _extract_asset(asset_result.get("data")) if asset_result["ok"] else None
            if asset is None:
                return _error("blocked", "账户资产缺少可用资金或总资产，禁止买入")
            available_cash, total_asset = asset
            if notional > available_cash or available_cash - notional < total_asset * min_cash_ratio:
                return _error("blocked", "买入后将突破可用资金或现金下限")
            return _success(operation="order_safety", data={"notional": notional, "last_price": last_price})

        positions_result = self._request("GET", "/api/v1/trader/positions", query={"account_id": account_id})
        position = _extract_position(positions_result.get("data"), payload["stock_code"]) if positions_result["ok"] else None
        if position is None:
            return _error("blocked", "持仓成本或可卖数量缺失，禁止卖出")
        avg_cost, can_use_volume = position
        if volume > can_use_volume:
            return _error("blocked", f"SELL 数量超过可卖数量 {can_use_volume}")
        loss_ratio = (last_price - avg_cost) / avg_cost
        if -STOP_LOSS_RATIO < loss_ratio < 0:
            return _error(
                "blocked",
                f"当前浮亏 {abs(loss_ratio) * 100:.2f}% 未达到 10% 止损线，禁止卖出",
            )
        if loss_ratio >= 0 and price is not None and float(price) < avg_cost:
            return _error("blocked", "当前未亏损，但卖出限价低于持仓成本，禁止可能产生浮亏的卖出")
        return _success(
            operation="order_safety",
            data={"notional": round(last_price * volume, 2), "last_price": last_price, "loss_ratio": loss_ratio},
        )

    def _reserve_intent(
        self,
        *,
        account_id: str,
        intent_id: str,
        operation: str,
        stock_code: str,
        side: str,
        volume: int,
        notional: float,
    ) -> dict[str, Any]:
        try:
            max_cycle = _positive_int_env("MINIQMT_MAX_ORDERS_PER_CYCLE", 2)
            max_day = _positive_int_env("MINIQMT_MAX_ORDERS_PER_DAY", 8)
            max_daily_buy = _positive_float_env("MINIQMT_MAX_DAILY_BUY_NOTIONAL", 50_000.0)
        except ValueError as exc:
            return _error("configuration_error", str(exc))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        account_hash = _account_hash(account_id)
        trading_day = datetime.now(TRADING_TZ).date().isoformat()
        try:
            with sqlite3.connect(self.state_dir / "trade_state.sqlite3", timeout=10) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS intents (
                        account_hash TEXT NOT NULL,
                        intent_id TEXT NOT NULL,
                        cycle_id TEXT NOT NULL,
                        trading_day TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        stock_code TEXT NOT NULL,
                        side TEXT NOT NULL,
                        volume INTEGER NOT NULL,
                        notional REAL NOT NULL,
                        status TEXT NOT NULL,
                        result_json TEXT,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (account_hash, intent_id)
                    )
                    """
                )
                conn.execute("BEGIN IMMEDIATE")
                duplicate = conn.execute(
                    "SELECT 1 FROM intents WHERE account_hash = ? AND intent_id = ?",
                    (account_hash, intent_id),
                ).fetchone()
                if duplicate:
                    return _error("duplicate_intent", "该 client_intent_id 已被持久化处理，禁止重复提交")
                cycle_count = conn.execute(
                    "SELECT COUNT(*) FROM intents WHERE account_hash = ? AND cycle_id = ?",
                    (account_hash, self.cycle_id),
                ).fetchone()[0]
                day_count = conn.execute(
                    "SELECT COUNT(*) FROM intents WHERE account_hash = ? AND trading_day = ?",
                    (account_hash, trading_day),
                ).fetchone()[0]
                daily_buy = conn.execute(
                    "SELECT COALESCE(SUM(notional), 0) FROM intents WHERE account_hash = ? AND trading_day = ? AND side = 'BUY'",
                    (account_hash, trading_day),
                ).fetchone()[0]
                if cycle_count >= max_cycle:
                    return _error("blocked", f"本轮写操作已达到上限 {max_cycle}")
                if day_count >= max_day:
                    return _error("blocked", f"当日写操作已达到上限 {max_day}")
                if side == "BUY" and float(daily_buy) + notional > max_daily_buy:
                    return _error("blocked", f"当日累计买入金额将超过上限 {max_daily_buy:.2f}")
                conn.execute(
                    "INSERT INTO intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', NULL, ?)",
                    (
                        account_hash,
                        intent_id,
                        self.cycle_id,
                        trading_day,
                        operation,
                        stock_code,
                        side,
                        volume,
                        notional,
                        _now(),
                    ),
                )
                conn.commit()
        except sqlite3.Error as exc:
            return _error("state_error", f"交易状态持久化失败：{type(exc).__name__}")
        return _success(operation="intent_reserved", data={"intent_id": intent_id})

    def _intent_exists(self, account_id: str, intent_id: str) -> dict[str, Any]:
        path = self.state_dir / "trade_state.sqlite3"
        if not path.is_file():
            return _success(operation="intent_lookup", data={"exists": False})
        try:
            with sqlite3.connect(path, timeout=10) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM intents WHERE account_hash = ? AND intent_id = ?",
                    (_account_hash(account_id), intent_id),
                ).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return _success(operation="intent_lookup", data={"exists": False})
            return _error("state_error", f"交易状态读取失败：{type(exc).__name__}")
        except sqlite3.Error as exc:
            return _error("state_error", f"交易状态读取失败：{type(exc).__name__}")
        return _success(operation="intent_lookup", data={"exists": exists})

    def _finish_intent(self, account_id: str, intent_id: str, result: dict[str, Any]) -> None:
        try:
            with sqlite3.connect(self.state_dir / "trade_state.sqlite3", timeout=10) as conn:
                conn.execute(
                    "UPDATE intents SET status = ?, result_json = ? WHERE account_hash = ? AND intent_id = ?",
                    (
                        str(result.get("status") or "unknown"),
                        json.dumps(result, ensure_ascii=False, sort_keys=True),
                        _account_hash(account_id),
                        intent_id,
                    ),
                )
                conn.commit()
        except sqlite3.Error:
            # 意图已经冻结；更新失败也不能通过重试再次下单。
            pass

    def _audited(self, operation: str, inputs: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        try:
            append_trade_audit(self.state_dir, self.cycle_id, operation, inputs, result)
        except OSError as exc:
            result = {**result, "journal_error": f"交易 Markdown 审计写入失败：{type(exc).__name__}"}
        return result

    def _ensure_account_ready(self, account_id: str) -> dict[str, Any]:
        if self._account_ready:
            return _success(operation="account_ready", data={"ready": True})
        result = self._request(
            "POST",
            "/api/v1/trader/ensure-ready",
            payload={"account_id": account_id},
        )
        if result["ok"]:
            self._account_ready = True
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        unknown_on_network_error: bool = False,
    ) -> dict[str, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        api_key = os.getenv("MINIQMT_BRIDGE_API_KEY", "").strip()
        if api_key:
            headers["X-Api-Key"] = api_key
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    return _error("response_too_large", "MiniQMT 响应超过 1 MB 限制")
                data = json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            if unknown_on_network_error and exc.code >= 500:
                return _error("unknown", "交易提交结果未知，请查询委托后人工确认")
            error_code = "authentication_error" if exc.code in {401, 403} else "http_error"
            return _error(error_code, f"MiniQMT Bridge 返回 HTTP {exc.code}")
        except (URLError, TimeoutError, OSError) as exc:
            code = "unknown" if unknown_on_network_error else "network_error"
            detail = "交易提交结果未知，请查询委托后人工确认" if unknown_on_network_error else f"MiniQMT 连接失败：{type(exc).__name__}"
            return _error(code, detail)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error("parse_error", "MiniQMT 返回的不是有效 UTF-8 JSON")
        return _success(operation=path, data=_redact(data))


def _order_payload(inputs: dict[str, Any], account_id: str) -> dict[str, Any] | str:
    unknown = set(inputs) - {"client_intent_id", "stock_code", "side", "volume", "price"}
    if unknown:
        return f"submit 包含未知字段：{', '.join(sorted(unknown))}"
    stock_code = inputs.get("stock_code")
    try:
        stock_code = _stock_code(stock_code)
    except ValueError as exc:
        return str(exc)
    side = inputs.get("side")
    if side not in {"BUY", "SELL"}:
        return "side 只能是 BUY 或 SELL"
    volume = inputs.get("volume")
    if isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0:
        return "volume 必须是正整数"
    try:
        max_volume = int(os.getenv("MINIQMT_MAX_ORDER_VOLUME", "10000"))
    except ValueError:
        return "宿主 MINIQMT_MAX_ORDER_VOLUME 配置无效"
    if max_volume <= 0:
        return "宿主 MINIQMT_MAX_ORDER_VOLUME 必须大于 0"
    if volume > max_volume:
        return f"volume 超过宿主单笔上限 {max_volume}"
    if side == "BUY" and volume % 100 != 0:
        return "A 股买入数量必须是 100 股的整数倍"
    price = inputs.get("price")
    if price is not None and (
        isinstance(price, bool)
        or not isinstance(price, (int, float))
        or not math.isfinite(price)
        or price <= 0
    ):
        return "price 必须是正数或 null"
    return {
        "account_id": account_id,
        "stock_code": stock_code,
        "order_type": side,
        "order_volume": volume,
        "price_type": "LATEST" if price is None else "FIX",
        "price": price,
    }


def _stock_code(value: Any) -> str:
    if not isinstance(value, str) or not STOCK_CODE_PATTERN.fullmatch(value.upper()):
        raise ValueError("股票代码必须是 600000.SH 形式的 A 股代码")
    return value.upper()


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key.endswith(("accountid", "accountno", "accountnumber", "secuaccount")):
                result["account_id_hash"] = _account_hash(str(item))
            else:
                result[key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _account_hash(account_id: str) -> str:
    return "sha256:" + hashlib.sha256(account_id.encode("utf-8")).hexdigest()


def _success(*, operation: str, data: Any) -> dict[str, Any]:
    return {"ok": True, "status": "success", "operation": operation, "data": data, "error": None}


def _error(code: str, detail: str) -> dict[str, Any]:
    status = "unknown" if code == "unknown" else ("blocked" if code == "blocked" else "error")
    return {"ok": False, "status": status, "operation": None, "data": None, "error": {"code": code, "detail": detail}}


def _now() -> str:
    return datetime.now(TRADING_TZ).isoformat(timespec="seconds")


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None or not raw.strip() else int(raw)
    except ValueError as exc:
        raise ValueError(f"宿主 {name} 配置无效") from exc
    if value <= 0:
        raise ValueError(f"宿主 {name} 必须大于 0")
    return value


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None or not raw.strip() else float(raw)
    except ValueError as exc:
        raise ValueError(f"宿主 {name} 配置无效") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"宿主 {name} 必须是大于 0 的有限数")
    return value


def _ratio_env(name: str, default: float) -> float:
    value = _positive_float_env(name, default)
    if value >= 1:
        raise ValueError(f"宿主 {name} 必须小于 1")
    return value


def _is_trading_time(value: datetime) -> bool:
    local = value.astimezone(TRADING_TZ)
    if local.weekday() >= 5:
        return False
    current = local.time()
    return time(9, 30) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0)


def _extract_quote(data: Any, stock_code: str) -> tuple[float, datetime] | None:
    items = data.get("ticks") if isinstance(data, dict) else None
    item = items.get(stock_code) if isinstance(items, dict) else None
    if item is None and isinstance(items, list):
        item = next((row for row in items if isinstance(row, dict) and row.get("stock_code") == stock_code), None)
    if not isinstance(item, dict):
        return None
    price = _first_number(item, ("lastPrice", "last_price", "price", "close"))
    raw_time = item.get("time") or item.get("timestamp") or item.get("quote_time") or item.get("data_time")
    quote_at = _parse_time(raw_time)
    if price is None or price <= 0 or quote_at is None:
        return None
    return price, quote_at


def _extract_asset(data: Any) -> tuple[float, float] | None:
    asset = data.get("asset") if isinstance(data, dict) else None
    if not isinstance(asset, dict):
        return None
    available = _first_number(asset, ("cash", "m_dAvailable", "available_cash", "enable_balance"))
    total = _first_number(asset, ("total_asset", "m_dBalance", "asset", "total_balance", "nav_asset"))
    if available is None or total is None or available < 0 or total <= 0:
        return None
    return available, total


def _extract_position(data: Any, stock_code: str) -> tuple[float, int] | None:
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    item = next((row for row in items if isinstance(row, dict) and str(row.get("stock_code") or "").upper() == stock_code), None)
    if item is None:
        return None
    cost = _first_number(item, ("avg_price", "avg_cost", "m_dOpenPrice", "open_price", "cost_price"))
    volume = _first_number(item, ("can_use_volume", "m_nCanUseVolume", "enable_amount"))
    if cost is None or cost <= 0 or volume is None or volume < 100:
        return None
    return cost, int(volume)


def _contains_order(data: Any, order_id: str) -> bool:
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("order_id") or item.get("broker_order_id") or item.get("m_nOrderID")
        if str(value) == order_id:
            return True
    return False


def _first_number(item: dict[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        value = item.get(name)
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value) / (1000 if value > 10_000_000_000 else 1), tz=TRADING_TZ)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    for parser in (
        lambda: datetime.fromisoformat(raw.replace("Z", "+00:00")),
        lambda: datetime.strptime(raw, "%Y%m%d%H%M%S"),
        lambda: datetime.strptime(raw, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            parsed = parser()
            return parsed.replace(tzinfo=TRADING_TZ) if parsed.tzinfo is None else parsed
        except ValueError:
            continue
    return None
