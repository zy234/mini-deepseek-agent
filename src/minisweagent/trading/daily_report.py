"""把追加式账本整理成可扫描、可归因的每日报告。"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path


def _lines(text: str, pattern: str) -> list[str]:
    return [line.strip()[2:].strip() for line in text.splitlines() if re.search(pattern, line, re.I)]


def build_daily_report(state_dir: str | Path, trading_day: date) -> Path:
    root = Path(state_dir).expanduser().resolve()
    source = root / "journals" / f"{trading_day.isoformat()}.md"
    ledger = source.read_text(encoding="utf-8") if source.is_file() else ""
    cycles = len(re.findall(r"<!-- cycle:", ledger))
    audits = len(re.findall(r"<!-- trade:", ledger))
    orders = _lines(ledger, r"操作：")
    errors = _lines(ledger, r"工具错误：")
    pitfalls = _lines(ledger, r"踩坑：")
    decisions = _lines(ledger, r"决策：")
    followups = _lines(ledger, r"后续观察：")

    # 从账本里的踩坑/错误/决策文字归纳出「下次固定动作」，命中哪条经验就落哪条。
    lessons = []
    if any(re.search(r"偏离|price_hint", x, re.I) for x in pitfalls + errors):
        lessons.append(("限价与最新价偏离", "报单前重取最新价并校验偏离阈值"))
    if any(re.search(r"超时|缺失|冲突", x, re.I) for x in pitfalls + errors):
        lessons.append(("数据不完整或冲突", "标记 data_quality；缺关键字段禁止 BUY"))
    if any(re.search(r"T\+1|不可卖|can_use_volume", x, re.I) for x in decisions + followups):
        lessons.append(("T+1 限制", "下单前读取 can_use_volume，今日买入进入次日队列"))
    if any(re.search(r"翻转|SELL.*HOLD|HOLD.*SELL", x, re.I) for x in decisions + pitfalls):
        lessons.append(("信号翻转", "SELL 需结构恢复证据才能撤销；BUY 要求连续确认"))
    if not lessons:
        lessons.append(("暂无明确新经验", "继续记录触发条件、动作和结果"))

    out = [
        f"# 每日交易账本与复盘 · {trading_day.isoformat()}",
        "",
        "## 结构化摘要",
        "",
        "|字段|值|",
        "|-|-|",
        f"|交易周期|{cycles}|",
        f"|交易审计|{audits}|",
        f"|委托记录|{len(orders)}|",
        f"|异常记录|{len(errors)}|",
        "",
        "## 经验 → 下次行为",
        "",
        "|本日观察|下次固定动作|",
        "|-|-|",
    ]
    out.extend(f"|{a}|{b}|" for a, b in lessons)
    out += ["", "## 下轮观察", ""] + [f"- {x}" for x in followups[-20:]]
    out += ["", "## 原始账本", "", ledger[-30000:] or "当日没有账本记录。", ""]

    # 原子写入：报告可能边跑边被读，覆盖写会读到半截内容。
    target = root / "reports" / f"{trading_day.isoformat()}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text("\n".join(out), encoding="utf-8")
    temporary.replace(target)
    return target
