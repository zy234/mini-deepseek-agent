"""盘前输入快照：保存并读取 Agent 当天实际看到的结构化数据。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any


def snapshot_path(journal_dir: str | Path, trade_date: str) -> Path:
    return Path(journal_dir) / "premarket" / f"{trade_date}.json"


def write_snapshot(journal_dir: str | Path, pack: dict[str, Any], watchlist: dict[str, Any], trace: Path) -> Path:
    """原子写入盘前快照；trace 只保存相对路径，便于迁移观测目录。"""
    path = snapshot_path(journal_dir, pack["trade_date"])
    path.parent.mkdir(parents=True, exist_ok=True)
    queries = []
    try:
        raw = json.loads(trace.read_text(encoding="utf-8"))
        for message in raw.get("messages") or []:
            extra = message.get("extra") or {}
            if extra.get("tool") != "candidate_details":
                continue
            try:
                result = json.loads(message.get("content") or "")
            except ValueError:
                result = {"raw": message.get("content", "")}
            queries.append({"args": extra.get("args") or {}, "status": extra.get("status", ""), "result": result})
    except (OSError, ValueError, TypeError):
        pass
    payload = {
        "trade_date": pack["trade_date"], "as_of": pack["as_of"],
        "indexes": pack.get("indexes", []), "sector_ranks": pack.get("sector_ranks", []),
        "sector_catalog": pack.get("sector_catalog", {}), "account": pack.get("account", {}),
        "limits": pack.get("limits", {}), "errors": pack.get("errors", []),
        "watchlist": watchlist, "candidate_queries": queries, "trace": str(trace),
    }
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read_snapshot(journal_dir: str | Path, trade_date: str | date) -> dict[str, Any] | None:
    key = trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date)
    path = snapshot_path(journal_dir, key)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_snapshots(journal_dir: str | Path, limit: int = 10) -> list[dict[str, Any]]:
    root = Path(journal_dir) / "premarket"
    rows = []
    for path in sorted(root.glob("*.json"), reverse=True):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({"trade_date": item.get("trade_date", path.stem), "as_of": item.get("as_of", ""),
                     "errors": len(item.get("errors") or []), "snapshot": True,
                     "trace": bool(item.get("trace")), "path": path.name})
        if len(rows) >= limit:
            break
    return rows
