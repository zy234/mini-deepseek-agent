"""把 K 线渲染成 PNG。

模型在这条流水线里只能看图，所以图上必须自带全部判断依据：价格刻度、日期、均线、
成交量和关键价位。图上一律不写中文——mac 默认字体没有中文字形，缺字会渲染成方块，
而方块是不会报错的静默损坏；中文说明放在 prompt 文本里。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # 必须在 pyplot 之前：宿主没有显示设备
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

# A 股配色：涨红跌绿。看图的是模型，但人也要能一眼核对。
UP_COLOR = "#d32f2f"
DOWN_COLOR = "#2e7d32"
MA_COLORS = {5: "#f57c00", 10: "#1976d2", 20: "#7b1fa2"}
FIGSIZE = (7.6, 4.6)
DPI = 100
BAR_FIELDS = ("open", "high", "low", "close", "volume")
# A 股一手 100 股：xtdata 的分钟成交量是手，成交额是元，算均价必须乘回来。
LOT_SIZE = 100


class ChartDataMissing(RuntimeError):
    """K 线不足以画图。读图 Agent 拿不到图只会瞎猜，所以这里必须炸而不是画个空白框。"""


def usable_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """缺任一价量字段的整根丢掉：按字段补零会让均线和形态错位到不同的时间上。"""
    return [
        bar
        for bar in bars
        if isinstance(bar.get("date"), int) and all(isinstance(bar.get(name), (int, float)) for name in BAR_FIELDS)
    ]


def render_daily(path: Path, code: str, bars: list[dict[str, Any]], *, days: int = 30) -> Path:
    """日线蜡烛图：均线在完整序列上算，只显示最近 days 根，避免首根均线是空的。"""
    rows = usable_bars(bars)
    if len(rows) < 2:
        raise ChartDataMissing(f"{code} 日线可用 bar 只有 {len(rows)} 根，画不出日线图")
    closes = [float(row["close"]) for row in rows]
    mas = {window: _moving_average(closes, window) for window in MA_COLORS}
    start = max(0, len(rows) - days)
    shown = rows[start:]
    labels = [_day_label(row["date"]) for row in shown]
    figure, (price_ax, volume_ax) = _make_axes()
    _draw_candles(price_ax, shown)
    for window, color in MA_COLORS.items():
        series = mas[window][start:]
        if any(value is not None for value in series):
            price_ax.plot(range(len(shown)), series, color=color, linewidth=1.2, label=f"MA{window}")
    # pivot 与结构低必须排除当日 bar：当日 bar 盘中实时更新，把它算进前高会让"突破"永远成立。
    prior = shown[:-1] if len(shown) > 1 else shown
    pivot = max(float(row["high"]) for row in prior[-20:])
    swing_low = min(float(row["low"]) for row in prior[-10:])
    price_ax.axhline(pivot, color="#616161", linestyle="--", linewidth=0.9)
    price_ax.annotate(f"pivot {pivot:.2f}", (len(shown) - 1, pivot), fontsize=8, color="#424242", va="bottom", ha="right")
    price_ax.axhline(swing_low, color="#9e9e9e", linestyle=":", linewidth=0.9)
    price_ax.annotate(
        f"low10 {swing_low:.2f}", (len(shown) - 1, swing_low), fontsize=8, color="#616161", va="top", ha="right"
    )
    last = closes[-1]
    change = (last / closes[-2] - 1) * 100 if len(closes) >= 2 else 0.0
    ma20 = mas[20][-1]
    title = f"{code}  DAILY x{len(shown)}  last {last:.2f} ({change:+.2f}%)"
    price_ax.set_title(f"{title}  MA20 {ma20:.2f}" if ma20 is not None else title, fontsize=10)
    price_ax.legend(fontsize=7, loc="upper left", framealpha=0.6)
    _draw_volumes(volume_ax, shown)
    _apply_labels(volume_ax, labels, max(1, len(shown) // 6))
    return _save(figure, path)


def render_intraday(
    path: Path, code: str, bars: list[dict[str, Any]], *, prev_close: float | None, show_average: bool = True
) -> Path:
    """当日分钟线：收盘价折线加均价线。中午休市在 x 轴上按位置排列，不留空洞。

    指数没有"每股价格"这个概念，成交额除成交量算不出点位，所以指数图必须 show_average=False。
    """
    rows = usable_bars(bars)
    if len(rows) < 2:
        raise ChartDataMissing(f"{code} 当日分钟线只有 {len(rows)} 根，画不出分钟图")
    closes = [float(row["close"]) for row in rows]
    labels = [_minute_label(row["date"]) for row in rows]
    figure, (price_ax, volume_ax) = _make_axes()
    price_ax.plot(range(len(rows)), closes, color="#212121", linewidth=1.1, label="close")
    average = _running_vwap(rows, closes) if show_average else None
    if average:
        price_ax.plot(range(len(rows)), average, color="#f57c00", linewidth=1.0, label="avg")
    if prev_close:
        price_ax.axhline(prev_close, color="#616161", linestyle="--", linewidth=0.9)
        price_ax.annotate(
            f"prev close {prev_close:.2f}",
            (len(rows) - 1, prev_close),
            fontsize=8,
            color="#424242",
            va="bottom",
            ha="right",
        )
    last = closes[-1]
    change = (last / prev_close - 1) * 100 if prev_close else 0.0
    title = f"{code}  1MIN {labels[0]}-{labels[-1]}  last {last:.2f}"
    price_ax.set_title(f"{title} ({change:+.2f}% vs prev close)" if prev_close else title, fontsize=10)
    price_ax.legend(fontsize=7, loc="upper left", framealpha=0.6)
    volume_ax.bar(
        range(len(rows)),
        [float(row["volume"]) for row in rows],
        color=[UP_COLOR if _minute_up(rows, index) else DOWN_COLOR for index in range(len(rows))],
        width=1.0,
    )
    _apply_labels(volume_ax, labels, max(1, len(rows) // 6))
    return _save(figure, path)


def _make_axes():
    figure, axes = plt.subplots(
        2, 1, figsize=FIGSIZE, dpi=DPI, sharex=True, gridspec_kw={"height_ratios": [3, 1], "hspace": 0.05}
    )
    for axis in axes:
        axis.grid(True, linewidth=0.4, alpha=0.35)
        axis.tick_params(labelsize=8)
    axes[1].set_ylabel("vol", fontsize=8)
    axes[1].yaxis.set_major_formatter(FuncFormatter(_compact_number))
    return figure, axes


def _draw_candles(axis, rows: list[dict[str, Any]]) -> None:
    for index, row in enumerate(rows):
        open_, close = float(row["open"]), float(row["close"])
        color = UP_COLOR if close >= open_ else DOWN_COLOR
        axis.vlines(index, float(row["low"]), float(row["high"]), color=color, linewidth=0.8)
        height = abs(close - open_) or max(close * 0.0005, 0.01)
        axis.add_patch(
            plt.Rectangle((index - 0.3, min(open_, close)), 0.6, height, facecolor=color, edgecolor=color)
        )


def _draw_volumes(axis, rows: list[dict[str, Any]]) -> None:
    colors = [UP_COLOR if float(row["close"]) >= float(row["open"]) else DOWN_COLOR for row in rows]
    axis.bar(range(len(rows)), [float(row["volume"]) for row in rows], color=colors, width=0.6)


def _apply_labels(axis, labels: list[str], step: int) -> None:
    ticks = list(range(0, len(labels), step))
    if ticks and ticks[-1] != len(labels) - 1:
        ticks.append(len(labels) - 1)
    axis.set_xticks(ticks)
    axis.set_xticklabels([labels[index] for index in ticks], fontsize=8)
    axis.set_xlim(-1, len(labels))


def _save(figure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, bbox_inches="tight")
    plt.close(figure)
    return path


def _moving_average(values: list[float], window: int) -> list[float | None]:
    return [
        sum(values[index + 1 - window : index + 1]) / window if index + 1 >= window else None
        for index in range(len(values))
    ]


def _running_vwap(rows: list[dict[str, Any]], closes: list[float]) -> list[float] | None:
    """当日均价 = 累计成交额 / (累计成交量 × 100)：xtdata 的 volume 单位是手，amount 是元。

    算出来的均价必须落在当日价格区间附近，否则说明这个代码的量额口径不是"手/元"（指数就是），
    这时候不画这条线——画一条量纲错误的线比不画更糟，模型会拿它当均价读。
    """
    if any(not isinstance(row.get("amount"), (int, float)) for row in rows):
        return None
    amount = volume = 0.0
    series = []
    for row in rows:
        amount += float(row["amount"])
        volume += float(row["volume"]) * LOT_SIZE
        series.append(amount / volume if volume > 0 else float(row["close"]))
    low, high = min(closes), max(closes)
    if not all(low * 0.9 <= value <= high * 1.1 for value in series):
        return None
    return series


def _minute_up(rows: list[dict[str, Any]], index: int) -> bool:
    if index == 0:
        return float(rows[0]["close"]) >= float(rows[0]["open"])
    return float(rows[index]["close"]) >= float(rows[index - 1]["close"])


def _day_label(stamp: int) -> str:
    text = str(stamp)
    return f"{text[4:6]}/{text[6:8]}" if len(text) >= 8 else text


def _minute_label(stamp: int) -> str:
    """1m bar 的 index 是 20260910093000 这种 14 位整数；解析不了就原样显示，不猜。"""
    text = str(stamp)
    try:
        return datetime.strptime(text, "%Y%m%d%H%M%S").strftime("%H:%M")
    except ValueError:
        return text[-6:-2] if len(text) >= 14 else text


def _compact_number(value: float, _position: int) -> str:
    for unit, scale in (("M", 1e6), ("k", 1e3)):
        if abs(value) >= scale:
            return f"{value / scale:.0f}{unit}"
    return f"{value:.0f}"
