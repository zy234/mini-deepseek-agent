"""交易日关键节点推送到个人微信（WxPusher）。

这是交易主路径之外的旁路：未配置就静默跳过，推送失败只回显加日志，绝不 raise。
交易决策的严格性留在流水线里；一条通知发不出去不该打死一个交易日。

凭证从环境变量读（WXPUSHER_APP_TOKEN、WXPUSHER_UIDS），和 DS_KEY、MINIQMT_* 一样只进 .env，
永远不落盘、不记日志、不塞进消息文本。这个模块只管把一段 markdown 发出去，说什么由流水线决定。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from minisweagent.trading.daily_report import build_daily_report

logger = logging.getLogger("minisweagent.notify")

_ENDPOINT = "https://wxpusher.zjiecode.com/api/send/message"
_TIMEOUT = 10
# WxPusher 通知栏摘要上限 20 字符，超了会被拒，宿主先截断。正文用 markdown（contentType=3）。
_SUMMARY_LIMIT = 20
_MARKDOWN = 3
_opener = build_opener(ProxyHandler({}))


def _config() -> tuple[str, list[str]] | None:
    """读凭证：缺 token 或 uid 就返回 None，整条通道视为关闭。"""
    token = os.getenv("WXPUSHER_APP_TOKEN", "").strip()
    uids = [uid.strip() for uid in os.getenv("WXPUSHER_UIDS", "").split(",") if uid.strip()]
    return (token, uids) if token and uids else None


def enabled() -> bool:
    """通道是否可用。宿主据此在没配凭证时跳过所有取数与排版，零额外开销、零额外 Bridge 调用。"""
    return _config() is not None


def push(title: str, markdown: str, *, echo: Callable[[str], None]) -> None:
    """把一段 markdown 推到配置的所有 uid。失败只回显加日志，绝不抛回流水线。"""
    config = _config()
    if config is None:
        echo("[通知] 未配置 WXPUSHER_APP_TOKEN/WXPUSHER_UIDS，跳过微信推送。")
        return
    token, uids = config
    payload = {
        "appToken": token,
        "content": markdown,
        "summary": title[:_SUMMARY_LIMIT],
        "contentType": _MARKDOWN,
        "uids": uids,
    }
    request = Request(
        _ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _opener.open(request, timeout=_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        # 通知是旁路：发失败只留痕，不影响交易。异常类型不含 token，可以安全回显。
        logger.warning("微信推送失败：%s", type(error).__name__)
        echo(f"[通知] 微信推送失败：{type(error).__name__}")
        return
    # WxPusher 用 code=1000 表示受理成功；msg 是它给的处理说明，不含我们的 token。
    if data.get("code") != 1000:
        echo(f"[通知] 微信推送被拒：{data.get('msg', '未知原因')}")


# ---------- 交易日四个汇报点：每个函数自带开关检查，宿主直接调，未配置就整段跳过 ----------


def premarket(watchlist: dict[str, Any], pack: dict[str, Any], *, echo: Callable[[str], None]) -> None:
    """盘前一条：接口检查（取数告警）加运行情况加候选清单。取数告警就是接口是否正常的直接证据。"""
    if not enabled():
        return
    errors = pack["errors"]
    asset = pack["account"]["asset"]
    lines = [
        f"# 盘前就绪 · {watchlist['trade_date']}",
        "",
        f"可用资金 {asset.get('available_cash')}，持仓 {len(pack['account']['positions'])} 只",
        "",
        "**接口检查**：" + ("全部正常" if not errors else f"{len(errors)} 条告警"),
    ]
    lines += [f"- {error}" for error in errors[:8]]
    if watchlist.get("market_view"):
        lines += ["", f"**行情观点**：{watchlist['market_view'][:200]}"]
    lines += ["", "**待观测清单**"]
    for sector in watchlist["sectors"]:
        lines.append(f"- {sector['sector']}")
        lines += [f"  - {pick['stock_code']} {(pick.get('reason') or '')[:60]}" for pick in sector["picks"]]
    push(f"盘前就绪 {watchlist['trade_date']}", "\n".join(lines), echo=echo)


def premarket_failed(max_attempts: int, *, echo: Callable[[str], None]) -> None:
    """盘前补跑耗尽：接口检查没通过，今日不交易。调用方负责一天只调一次。"""
    if not enabled():
        return
    push(
        "盘前失败",
        f"# 盘前选池失败\n\n补跑 {max_attempts} 次仍未生成待观测清单，接口检查未通过，今日不参与交易。详见运行日志。",
        echo=echo,
    )


def trades(new_orders: list[dict[str, Any]], submission: str, when: datetime, *, echo: Callable[[str], None]) -> None:
    """本轮真实下单才推。new_orders 由调用方用委托差集算好；为空说明这轮只是持有，不打扰。"""
    if not enabled() or not new_orders:
        return
    lines = [f"# 交易执行 · {when.strftime('%H:%M')}", ""]
    lines += [
        f"- {order.get('side')} {order.get('stock_code')} {order.get('name') or ''} "
        f"{order.get('order_volume')}股 @{order.get('price')} · {order.get('status')}"
        for order in new_orders
    ]
    if submission.strip():
        lines += ["", "**执行说明**", submission.strip()[:600]]
    push(f"交易执行 {when.strftime('%H:%M')}", "\n".join(lines), echo=echo)


def summary(journal_dir: str | Path, trade_date: date, *, echo: Callable[[str], None]) -> None:
    """盘后总结：复用固定复盘报告，只推结构化摘要，原始账本太长不进微信。"""
    if not enabled():
        return
    try:
        text = build_daily_report(journal_dir, trade_date).read_text(encoding="utf-8")
    except OSError as error:
        echo(f"[通知] 盘后报告生成失败：{type(error).__name__}")
        return
    concise = text.split("## 原始账本")[0].strip()
    push(f"盘后总结 {trade_date.isoformat()}", concise[:2000], echo=echo)
