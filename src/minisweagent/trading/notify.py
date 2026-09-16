"""交易日关键节点推送到个人微信（企业微信自建应用）。

这是交易主路径之外的旁路：未配置就静默跳过，推送失败只回显加日志，绝不 raise。
交易决策的严格性留在流水线里；一条通知发不出去不该打死一个交易日。

用企业微信而不是第三方中转：持仓、金额、决策原因是敏感财务数据，官方通道只过腾讯，
不落到第三方服务器。凭证从环境变量读（WECOM_CORPID、WECOM_CORPSECRET、WECOM_AGENTID、
可选 WECOM_TOUSER），和 DS_KEY、MINIQMT_* 一样只进 .env，永远不落盘、不记日志、不进消息文本。

消息类型 markdown：在企业微信 App 里看，不要求转发到微信端（那条路只保证 text）。
markdown.content 硬上限 2048 字节（utf8），超了整条会被拒，所以本地先按字节裁。
这个模块只管把一段 markdown 发出去，说什么由流水线决定。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from minisweagent.trading.daily_report import build_daily_report

logger = logging.getLogger("minisweagent.notify")

_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
_TIMEOUT = 10
# markdown.content 上限 2048 字节；留一点余量给截断标记，按字节裁而不是字符。
_CONTENT_BYTE_LIMIT = 2000
# access_token 官方有效期 7200 秒；提前 300 秒过期，避免卡在边界上用到刚失效的 token。
_TOKEN_TTL_MARGIN = 300
# token 失效类错误码：命中就清缓存重取一次，而不是把过期 token 的失败当成发送失败。
_TOKEN_ERRCODES = {40014, 42001}
_opener = build_opener(ProxyHandler({}))
# 进程内缓存 token：gettoken 有调用频率限制，每条通知都换一次必然被限流。
_token_cache: dict[str, Any] = {"value": "", "expires_at": 0.0}


def _config() -> tuple[str, str, str, str] | None:
    """读凭证：缺 corpid、secret 或 agentid 就返回 None，整条通道视为关闭。touser 缺省推给全体成员。"""
    corpid = os.getenv("WECOM_CORPID", "").strip()
    secret = os.getenv("WECOM_CORPSECRET", "").strip()
    agentid = os.getenv("WECOM_AGENTID", "").strip()
    touser = os.getenv("WECOM_TOUSER", "").strip() or "@all"
    return (corpid, secret, agentid, touser) if corpid and secret and agentid else None


def enabled() -> bool:
    """通道是否可用。宿主据此在没配凭证时跳过所有取数与排版，零额外开销、零额外 Bridge 调用。"""
    return _config() is not None


def push(markdown: str, *, echo: Callable[[str], None]) -> None:
    """把一段 markdown 推给配置的成员。失败只回显加日志，绝不抛回流水线。"""
    config = _config()
    if config is None:
        echo("[通知] 未配置 WECOM_CORPID/WECOM_CORPSECRET/WECOM_AGENTID，跳过企业微信推送。")
        return
    corpid, secret, agentid, touser = config
    body = _clip_bytes(markdown, _CONTENT_BYTE_LIMIT)
    # 第一次用缓存 token；token 失效再强制刷新重试一次，其余错误直接留痕。
    for force_refresh in (False, True):
        token = _access_token(corpid, secret, echo, force_refresh=force_refresh)
        if not token:
            return
        result = _send(token, agentid, touser, body, echo)
        if result is None:
            return  # 网络层失败已在 _send 里留痕
        if result.get("errcode") == 0:
            return
        if result.get("errcode") in _TOKEN_ERRCODES and not force_refresh:
            continue
        echo(f"[通知] 企业微信推送被拒：errcode={result.get('errcode')} {result.get('errmsg', '')}")
        return


def _send(token: str, agentid: str, touser: str, content: str, echo: Callable[[str], None]) -> dict[str, Any] | None:
    payload = {"touser": touser, "msgtype": "markdown", "agentid": agentid, "markdown": {"content": content}}
    request = Request(
        f"{_BASE}/message/send?access_token={token}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _opener.open(request, timeout=_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        logger.warning("企业微信推送失败：%s", type(error).__name__)
        echo(f"[通知] 企业微信推送失败：{type(error).__name__}")
        return None


def _access_token(corpid: str, secret: str, echo: Callable[[str], None], *, force_refresh: bool) -> str:
    if not force_refresh and _token_cache["value"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["value"]
    query = urlencode({"corpid": corpid, "corpsecret": secret})
    try:
        with _opener.open(f"{_BASE}/gettoken?{query}", timeout=_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        # gettoken 的 URL 带 secret，异常类型名不含它，只回显类型名是安全的。
        logger.warning("企业微信取 token 失败：%s", type(error).__name__)
        echo(f"[通知] 企业微信取 token 失败：{type(error).__name__}")
        return ""
    if data.get("errcode") != 0 or not data.get("access_token"):
        echo(f"[通知] 企业微信取 token 被拒：errcode={data.get('errcode')} {data.get('errmsg', '')}")
        return ""
    _token_cache["value"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + max(int(data.get("expires_in", 7200)) - _TOKEN_TTL_MARGIN, 0)
    return _token_cache["value"]


def _clip_bytes(text: str, limit: int) -> str:
    """按 utf8 字节裁到上限，不在多字节字符中间切断；被裁了就带个省略号。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore").rstrip() + "…"


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
    push("\n".join(lines), echo=echo)


def premarket_failed(max_attempts: int, *, echo: Callable[[str], None]) -> None:
    """盘前补跑耗尽：接口检查没通过，今日不交易。调用方负责一天只调一次。"""
    if not enabled():
        return
    push(
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
    push("\n".join(lines), echo=echo)


def summary(journal_dir: str | Path, trade_date: date, *, echo: Callable[[str], None]) -> None:
    """盘后总结：复用固定复盘报告，只推结构化摘要，原始账本太长不进微信。"""
    if not enabled():
        return
    try:
        text = build_daily_report(journal_dir, trade_date).read_text(encoding="utf-8")
    except OSError as error:
        echo(f"[通知] 盘后报告生成失败：{type(error).__name__}")
        return
    push(text.split("## 原始账本")[0].strip(), echo=echo)
