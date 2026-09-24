"""纸面账户：按读图结论以固定规则模拟成交。

成交价一律用决策时刻最后一根分钟 bar 的收盘价——模型就是看着这张图做的决定，这个价就是
它眼里的事实，不引入滑点猜测。仓位规则写死在这里，透明可复算；这不是 execution_manager
的回放，回测测的是"读图结论 × 固定规则"的收益，规则本身是另一件要单独调的事。

规则（全部来自宿主硬限额 host_limits，与实盘交易工具同一份定义）：

- SELL 结论：可卖部分全部卖出；当日买入的部分 T+1 不可卖，记跳过。
- BUY 结论：按 confidence 降序尝试，受单笔金额、当日买入总额、现金
  保留比例约束；数量按一手取整。已持仓的票允许加仓。
"""

from __future__ import annotations

from typing import Any

from minisweagent.environments.miniqmt import LOT_SIZE

# A 股常规费率假设：佣金双向、卖出印花税。这是明说的假设，不是从 bridge 读来的事实。
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_TAX_SELL = 0.001


class PaperAccount:
    """一个交易日的模拟账户：现金、持仓、当日买入量（T+1）和成交记录。"""

    def __init__(self, initial_cash: float, limits: dict[str, Any]):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.limits = limits
        # {code: {"volume": 总量, "bought_today": 当日买入量, "avg_cost": 均价, "last_price": 最近成交价}}
        self.positions: dict[str, dict[str, Any]] = {}
        self.daily_buy_notional = 0.0

    def apply(self, slot: str, verdicts: list[dict], prices: dict[str, float]) -> tuple[list[dict], list[dict]]:
        """应用一个槽位的全部结论，返回（成交，跳过）。"""
        orders: list[dict] = []
        skipped: list[dict] = []
        sells = [verdict for verdict in verdicts if verdict.get("action") == "SELL"]
        # 买单按 confidence 降序：最有把握的先占额度，排后面的额度不够就记跳过，和实盘
        # "额度用完被工具拒单"是同一件事。
        buys = sorted(
            (verdict for verdict in verdicts if verdict.get("action") == "BUY" and verdict["stock_code"] in prices),
            key=lambda verdict: float(verdict.get("confidence") or 0),
            reverse=True,
        )
        for verdict in sells:
            self._sell(slot, verdict, prices, orders, skipped)
        for verdict in buys:
            self._buy(slot, verdict, prices, orders, skipped)
        return orders, skipped

    def position_view(self, code: str, price: float) -> dict[str, Any]:
        """持仓注入 prompt 的形状，与实盘 _position_view 对齐：chart_reader 靠 can_use_volume 判 T+1。"""
        position = self.positions[code]
        can_use = position["volume"] - position["bought_today"]
        profit = (price - position["avg_cost"]) * position["volume"]
        return {
            "stock_code": code,
            "name": "",
            "volume": position["volume"],
            "can_use_volume": can_use,
            "yesterday_volume": can_use,
            "avg_price": position["avg_cost"],
            "last_price": price,
            "market_value": round(price * position["volume"], 2),
            "float_profit": round(profit, 2),
            "profit_rate": round((price / position["avg_cost"] - 1) * 100, 2) if position["avg_cost"] else 0.0,
        }

    def roll_to_next_day(self) -> None:
        """跨到下一交易日：昨日买入的量今天起可卖（T+1 解锁），当日买入额度清零。

        回测多日连跑时，每天开盘前调一次——bought_today 归零让隔夜持仓变成 can_use，
        daily_buy_notional 归零重置当日买入上限。持仓、现金、均价都结转不动。
        """
        for position in self.positions.values():
            position["bought_today"] = 0
        self.daily_buy_notional = 0.0

    def mark_to_market(self, closes: dict[str, float]) -> dict[str, Any]:
        """收盘价估值，给出最终资金与收益率。停牌缺价的持仓用最近成交价兜底并留痕。"""
        positions = []
        market_value = 0.0
        for code, position in self.positions.items():
            price = closes.get(code, position["last_price"])
            value = price * position["volume"]
            market_value += value
            positions.append(
                {
                    "stock_code": code,
                    "volume": position["volume"],
                    "avg_cost": position["avg_cost"],
                    "last_price": price,
                    "market_value": round(value, 2),
                    "priced_at_close": code in closes,
                }
            )
        total = self.cash + market_value
        return {
            "initial_cash": self.initial_cash,
            "cash": round(self.cash, 2),
            "market_value": round(market_value, 2),
            "total": round(total, 2),
            "return_pct": round((total / self.initial_cash - 1) * 100, 3),
            "positions": positions,
        }

    def _sell(self, slot, verdict, prices, orders, skipped) -> None:
        code = verdict["stock_code"]
        entry = {"slot": slot, "stock_code": code, "action": "SELL"}
        position = self.positions.get(code)
        if position is None:
            skipped.append({**entry, "reason": "无持仓"})
            return
        can_use = position["volume"] - position["bought_today"]
        if can_use <= 0:
            skipped.append({**entry, "reason": "T+1 当日买入不可卖"})
            return
        price = prices.get(code)
        if price is None:
            skipped.append({**entry, "reason": "本槽位无行情"})
            return
        proceeds = price * can_use
        fee = max(COMMISSION_MIN, proceeds * COMMISSION_RATE) + proceeds * STAMP_TAX_SELL
        self.cash += proceeds - fee
        position["volume"] -= can_use
        position["last_price"] = price
        if position["volume"] <= 0:
            del self.positions[code]
        orders.append(
            {
                **entry,
                "price": price,
                "volume": can_use,
                "fee": round(fee, 2),
                "cash_after": round(self.cash, 2),
            }
        )

    def _buy(self, slot, verdict, prices, orders, skipped) -> None:
        code = verdict["stock_code"]
        price = prices[code]
        entry = {"slot": slot, "stock_code": code, "action": "BUY"}
        total_asset = self.cash + self._market_value(prices)
        budget = min(self.limits["max_buy_notional"], self.cash - self.limits["min_cash_ratio"] * total_asset)
        if budget < price * LOT_SIZE:
            skipped.append({**entry, "reason": "现金或单笔额度不足一手"})
            return
        volume = min(
            self.limits["max_buy_volume"],
            int(budget / (price * (1 + COMMISSION_RATE)) / LOT_SIZE) * LOT_SIZE,
        )
        if volume < LOT_SIZE:
            skipped.append({**entry, "reason": "额度不足一手"})
            return
        # 佣金最低 5 元可能让整单超出预算，超了就一手一手减，减到不足一手就放弃。
        cost = price * volume
        while volume >= LOT_SIZE and cost + max(COMMISSION_MIN, cost * COMMISSION_RATE) > self.cash:
            volume -= LOT_SIZE
            cost = price * volume
        fee = max(COMMISSION_MIN, cost * COMMISSION_RATE)
        if volume < LOT_SIZE or cost + fee > self.cash:
            skipped.append({**entry, "reason": "现金不足"})
            return
        if self.daily_buy_notional + cost > self.limits["max_daily_buy_notional"]:
            skipped.append({**entry, "reason": "当日买入金额已满"})
            return
        self.cash -= cost + fee
        self.daily_buy_notional += cost
        position = self.positions.setdefault(
            code, {"volume": 0, "bought_today": 0, "avg_cost": 0.0, "last_price": price}
        )
        position["avg_cost"] = round((position["avg_cost"] * position["volume"] + cost) / (position["volume"] + volume), 3)
        position["volume"] += volume
        position["bought_today"] += volume
        position["last_price"] = price
        orders.append(
            {
                **entry,
                "price": price,
                "volume": volume,
                "fee": round(fee, 2),
                "cash_after": round(self.cash, 2),
            }
        )

    def _market_value(self, prices: dict[str, float]) -> float:
        return sum(
            position["volume"] * prices.get(code, position["last_price"]) for code, position in self.positions.items()
        )
