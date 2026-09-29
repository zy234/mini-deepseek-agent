"""大 QMT 日线历史缓存。"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from minisweagent.environments.miniqmt import HISTORY_CODE_LIMIT, STOCK_CODE_PATTERN, TRADING_TZ, _finite

if TYPE_CHECKING:
    from minisweagent.environments.miniqmt import MiniQMTClient

LOOKBACK_DAYS = 90
RETRY_SECONDS = 60


def fetch(client: MiniQMTClient, codes: list[str], now: datetime, errors: list[str], *, cache_dir: str | Path, deadline: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """批量下载并读取最近 90 天日线；只缓存昨日及以前，失败留痕后冷却重试。"""
    today = int(now.strftime("%Y%m%d"))
    start = int((now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d"))
    end = int((now - timedelta(days=1)).strftime("%Y%m%d"))
    root = Path(cache_dir).expanduser().resolve() / "qmt_daily_cache" / str(today)
    history: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []
    for code in dict.fromkeys(codes):
        if not STOCK_CODE_PATTERN.fullmatch(code):
            errors.append(f"大 QMT 日线代码无效：{code}")
            continue
        saved = _read(root / f"{code}.json")
        rows = _rows(saved.get("rows"), start, today)
        if saved.get("source") == "qmt" and rows:
            history[code] = rows
        elif time.time() - float(saved.get("attempted_at", 0)) < RETRY_SECONDS:
            errors.append(f"{code} 大 QMT 日线仍缺：{saved.get('error', '下载未完成')}")
        else:
            missing.append(code)
    for offset in range(0, len(missing), HISTORY_CODE_LIMIT):
        if deadline and datetime.now(TRADING_TZ) >= deadline:
            errors.append(f"大 QMT 日线预热到截止时间，余下 {len(missing) - offset} 只留待盘中补")
            break
        batch = missing[offset : offset + HISTORY_CODE_LIMIT]
        try:
            result = client.history(batch, period="1d", start_time=str(start), end_time=str(end))
        except Exception as exc:
            result = {"ok": False, "error": {"detail": f"{type(exc).__name__}: {exc}"}}
        frames = result.get("data", {}).get("bars", {}) if result.get("ok") else {}
        detail = (result.get("error") or {}).get("detail", "请求失败")
        for code in batch:
            rows = _rows(frames.get(code, []), start, today)
            if rows:
                history[code] = rows
                detail_for_file = ""
            else:
                errors.append(f"{code} 大 QMT 日线缺失：{detail}")
                detail_for_file = str(detail)
            _write(root / f"{code}.json", {"source": "qmt", "attempted_at": time.time(), "rows": rows, "error": detail_for_file})
    # QMT 暂时不可用时保留开源日线回退；主路径成功时不触发，避免重复请求。
    fallback_codes = [code for code in codes if not history.get(code)]
    if fallback_codes:
        try:
            from minisweagent.environments import akshare_board

            fallback = akshare_board.daily_history(fallback_codes, now, cache_dir=cache_dir, spacing=1.5)
        except Exception as exc:
            errors.append(f"开源日线回退失败：{type(exc).__name__}: {exc}")
        else:
            for code, rows in fallback.items():
                if rows:
                    history[code] = rows
    return history


def _rows(raw: Any, start: int, today: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: dict[int, dict[str, Any]] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        stamp = str(row.get("date", ""))
        if len(stamp) not in (8, 14) or not stamp.isdigit():
            continue
        day = int(stamp[:8])
        if not start <= day < today:
            continue
        values = [_finite(row.get(name)) for name in ("open", "high", "low", "close", "volume")]
        if any(value is None or value <= 0 for value in values):
            return []
        result[day] = {**row, "date": day}
    return [result[day] for day in sorted(result)]


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".daily-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)
