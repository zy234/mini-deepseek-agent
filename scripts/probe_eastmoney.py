#!/usr/bin/env python3
"""东财接口恢复探测：盘前板块补全依赖东财两个 host，这里温和轮询等它们回血。

补全要用的两个 host：
- push2.eastmoney.com    实时 clist：板块列表 + 成分股（cons_em）走它，是补全的闸门。
- push2his.eastmoney.com 历史服务：板块历史资金流（fflow daykline）走它。

探测本身必须克制——我们就是被突发限流坑过的（见项目记忆）。所以每轮每个 host 只发一个最轻的请求，
间隔默认 90s，连续成功达到阈值才判恢复，避免被一次偶发 200 骗了。判恢复后打印补全命令并退出。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime

HOSTS = {
    # 实时 clist：拿行业板块列表第一行即可，pz=1 最轻。
    "push2(clist)": (
        "https://push2.eastmoney.com/api/qt/clist/get?"
        "pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f3"
    ),
    # 历史资金流：半导体板块(90.BK1036)只要 1 根 K。
    "push2his(fflow)": (
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
        "lmt=1&klt=101&fields1=f1,f2,f3,f7&fields2=f51,f52,f57&secid=90.BK1036"
    ),
}


def _probe(url: str, timeout: float) -> tuple[bool, str]:
    """单发探测：200 + body 带 data 才算活。返回 (是否活, 简短原因)。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        if not body:
            return False, "空响应体"
        data = json.loads(body).get("data")
        return (data is not None), ("ok" if data is not None else "data 为空")
    except Exception as exc:  # 连接重置/超时/非 JSON 都算没活
        return False, f"{type(exc).__name__}: {str(exc)[:50]}"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def main() -> None:
    parser = argparse.ArgumentParser(description="温和轮询东财 host，等补全依赖的接口恢复")
    parser.add_argument("--interval", type=float, default=90.0, help="轮询间隔秒，默认 90（别调太小，会二次触发限流）")
    parser.add_argument("--threshold", type=int, default=2, help="连续成功多少次才判恢复，默认 2")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--ref-date", default="20260930", help="恢复后提示的补全窗口结束日")
    parser.add_argument("--keep", action="store_true", help="全部恢复后不退出，继续探测")
    args = parser.parse_args()

    sys.stdout.reconfigure(line_buffering=True)  # 后台/管道运行时也能逐行看到进度
    streak = {host: 0 for host in HOSTS}
    print(f"[{_ts()}] 开始探测，间隔 {args.interval:.0f}s，连续 {args.threshold} 次成功判恢复。Ctrl-C 停止。")
    while True:
        for host, url in HOSTS.items():
            alive, why = _probe(url, args.timeout)
            streak[host] = streak[host] + 1 if alive else 0
            flag = "✅" if alive else "❌"
            print(f"[{_ts()}] {flag} {host:<16} streak={streak[host]} {why}")
        if all(count >= args.threshold for count in streak.values()):
            print(f"\n[{_ts()}] 两个 host 都连续 {args.threshold} 次成功，东财恢复。可以跑补全：")
            print(f"  .venv/bin/python -m minisweagent.backtest.backfill_sectors --ref-date {args.ref_date} --days 10")
            if not args.keep:
                return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
