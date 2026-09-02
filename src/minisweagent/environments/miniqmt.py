"""受限的 MiniQMT HTTP 工具。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 1_000_000
STOCK_CODE_PATTERN = re.compile(r"^(?:[036]\d{5})\.(?:SH|SZ)$")


class MiniQMTClient:
    """只连接宿主配置的 MiniQMT Bridge；模型不能指定地址、账户或凭据。"""

    def __init__(self, *, base_url: str, timeout: float, mode: str = "observe") -> None:
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
        if mode not in {"observe", "execute"}:
            raise ValueError("MINIQMT_AGENT_MODE 只能是 observe 或 execute")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.mode = mode
        self._account_ready = False
        self._submitted_intents: set[str] = set()
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
            return _error("blocked", "交易工具处于 observe 模式；需由宿主设置 MINIQMT_AGENT_MODE=execute")
        account_id = os.getenv("MINIQMT_ACCOUNT_ID", "").strip()
        if not account_id:
            return _error("configuration_error", "宿主未配置 MINIQMT_ACCOUNT_ID")
        ready = self._ensure_account_ready(account_id)
        if not ready["ok"]:
            return ready
        intent_id = inputs.get("client_intent_id")
        if not isinstance(intent_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", intent_id):
            return _error("invalid_argument", "client_intent_id 必须是 8 到 128 位的稳定标识")
        if intent_id in self._submitted_intents:
            return _error("duplicate_intent", "本会话已处理相同 client_intent_id，禁止重复提交")
        if operation == "submit":
            validation = _order_payload(inputs, account_id)
            if isinstance(validation, str):
                return _error("invalid_argument", validation)
            path, payload = "/api/v1/trader/order/live", validation
        elif operation == "cancel":
            unknown = set(inputs) - {"client_intent_id", "order_id"}
            if unknown:
                return _error("invalid_argument", f"cancel 包含未知字段：{', '.join(sorted(unknown))}")
            order_id = inputs.get("order_id")
            if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", order_id):
                return _error("invalid_argument", "order_id 格式无效")
            path = "/api/v1/trader/order/cancel"
            payload = {"account_id": account_id, "order_id": order_id}
        else:
            return _error("invalid_argument", f"不支持的交易 operation：{operation}")

        # 一旦开始请求即冻结意图。超时属于 unknown，调用方必须查询委托，不能自动重发。
        self._submitted_intents.add(intent_id)
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
        "price_type": "LATEST" if price is None else "FIXED",
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
            if normalized_key.endswith(("accountid", "accountno", "accountnumber")):
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
    return datetime.now().astimezone().isoformat(timespec="seconds")
