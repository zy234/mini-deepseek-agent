#!/usr/bin/env python3
"""从已有盘前轨迹恢复结构化快照；缺失字段保留为空并标记 source=trace_recovered。"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from minisweagent.trading import premarket_replay


def _json_after(text: str, heading: str):
    marker = f"## {heading}"
    start = text.find(marker)
    if start < 0:
        return []
    start = text.find("\n", start) + 1
    end = text.find("\n## ", start)
    raw = text[start:] if end < 0 else text[start:end]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", raw)
        return json.loads(match.group(1)) if match else []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=Path, default=Path(".sessions"))
    parser.add_argument("--journal-dir", type=Path, default=Path(".sessions/account-manager"))
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    paths = sorted(args.sessions.glob("*/ *-premarket.json".replace(" ", "")), reverse=True)
    count = 0
    for path in paths:
        if count >= args.limit:
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            info = data.get("info") or {}
            messages = data.get("messages") or []
            user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
            started = ((info.get("session") or {}).get("started_at") or "")
            trade_date = started[:10] or path.parent.name
            sector_ranks = _json_after(user, "板块热度摘要（概念与行业）")
            if not sector_ranks:
                sector_ranks = _json_after(user, "板块热度榜")
            indexes = _json_after(user, "大盘指数")
            account = _json_after(user, "账户摘要")
            if not account:
                account = _json_after(user, "账户")
            submission = info.get("submission") or ""
            try:
                watchlist = json.loads(submission)
            except (TypeError, ValueError):
                watchlist = {"sectors": [], "market_view": ""}
            # 轨迹正文是唯一可靠历史来源；完整正文通过 trace 字段继续可查。
            target = premarket_replay.snapshot_path(args.journal_dir, trade_date)
            if target.exists():
                try:
                    existing = json.loads(target.read_text(encoding="utf-8"))
                    if existing.get("source") != "trace_recovered":
                        continue
                except (OSError, ValueError):
                    pass
            premarket_replay.write_snapshot(
                args.journal_dir,
                {"trade_date": trade_date, "as_of": started, "indexes": indexes, "sector_ranks": sector_ranks,
                 "sector_catalog": {}, "account": account, "limits": {}, "errors": [], "source": "trace_recovered"},
                {"trade_date": trade_date, "generated_at": started, "market_view": watchlist.get("market_view", ""),
                 "sectors": watchlist.get("sectors", []), "data_errors": []},
                path,
            )
            # 保存原始 prompt 的副本，前端可以明确提示历史数据来自轨迹。
            target.with_suffix(".prompt.txt").write_text(user, encoding="utf-8")
            count += 1
        except (OSError, ValueError, TypeError):
            continue
    print(f"generated {count} snapshots")


if __name__ == "__main__":
    main()
