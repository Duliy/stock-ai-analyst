"""硬风控闸门 + 专业仓位/出场引擎（AI 无权豁免）。

策略内核（Van Tharp 风险百分比模型 + 金字塔加仓 + 吊灯止损）：
- 仓位由风险预算决定：股数 = (净值 × risk_per_trade_pct) ÷ 止损距离
- 止损距离 = 2×ATR（波动率自适应，封顶 stop_loss_pct）
- 首仓 1/2，浮盈 ≥1R 才允许补仓（只加赢家）
- 出场：+1R 保本 → +2R 卖一半 → 吊灯移动止损收尾
"""

from . import db, market_data
from .config import RISK, STRATEGY as S


# ---------- 仓位计算 ----------
def stop_distance(price: float, atr: float | None) -> float:
    """止损距离 = min(2×ATR, 价格×max_stop_pct)。"""
    cap = price * RISK["stop_loss_pct"]
    if not atr or atr <= 0:
        return cap
    return min(S["atr_stop_mult"] * atr, cap)


def size_new_position(
    equity: float, cash: float, price: float, atr: float | None
) -> dict | None:
    """计算新开仓的计划仓位和首仓数量。"""
    dist = stop_distance(price, atr)
    if dist <= 0 or price <= 0:
        return None
    risk_amount = equity * S["risk_per_trade_pct"]
    planned = min(risk_amount / dist, equity * RISK["max_position_pct"] / price)
    qty = min(planned * S["first_tranche_pct"], cash * 0.98 / price)
    if qty * price < 200:  # 名义金额太小不值得开
        return None
    return {"planned_qty": planned, "qty": qty, "stop": price - dist, "r": dist}


def size_add(
    trade: dict, position: dict, equity: float, cash: float, price: float
) -> float:
    """金字塔补仓数量：计划仓位的剩余部分均分到每次补仓。"""
    add_qty = (
        trade["planned_qty"] * (1 - S["first_tranche_pct"]) / S["pyramid_max_adds"]
    )
    room_value = equity * RISK["max_position_pct"] - position["market_value"]
    return max(0.0, min(add_qty, room_value / price, cash * 0.98 / price))


def portfolio_heat(open_trades: list[dict], equity: float) -> float:
    """组合总敞口风险 = Σ(持仓股数 × 每股风险) / 净值。"""
    total = sum(t["qty"] * (t["r_per_share"] or 0) for t in open_trades)
    return total / equity if equity else 1.0


# ---------- 出场引擎 ----------
def evaluate_exits(
    position: dict, trade: dict, atr: float | None
) -> tuple[list[dict], float, float]:
    """评估一个持仓的出场/止损动作。

    返回 (actions, new_peak, new_stop)。
    actions 元素: {"type": "stop_all"|"sell_half", "reason": str}
    """
    entry = trade["entry_price"]
    price = position["current_price"]
    r = trade["r_per_share"] or stop_distance(entry, atr)

    peak = max(trade["peak_price"] or entry, price)
    stop = trade["stop_price"] or (entry - r)

    # 吊灯止损（只上移）：持仓期最高价 - 2.5×ATR
    if atr and atr > 0:
        stop = max(stop, peak - S["chandelier_mult"] * atr)
    # 保本：浮盈曾达到 +1R，止损至少移到成本
    if peak >= entry + S["breakeven_after_r"] * r:
        stop = max(stop, entry)

    if price <= (trade["stop_price"] or (entry - r)):
        gain_r = (price - entry) / r
        return (
            [
                {
                    "type": "stop_all",
                    "reason": f"触发止损 @ {price:.2f}（止损线 {stop:.2f}，{gain_r:+.1f}R）",
                }
            ],
            peak,
            stop,
        )

    if not trade["half_sold"] and price >= entry + S["take_profit_r"] * r:
        return (
            [
                {
                    "type": "sell_half",
                    "reason": f"浮盈达 +{S['take_profit_r']:.0f}R @ {price:.2f}，卖出一半锁定利润，余仓移动止损",
                }
            ],
            peak,
            stop,
        )

    # 时间止损（Minervini 原则：好的入场会很快见效；死仓位占坑占风险预算）
    from datetime import datetime, timezone

    age_days = (
        datetime.now(timezone.utc) - datetime.fromisoformat(trade["entry_ts"])
    ).total_seconds() / 86400
    gain_r = (price - entry) / r
    if age_days >= S["time_stop_days"] and gain_r < S["time_stop_min_r"]:
        return (
            [
                {
                    "type": "stop_all",
                    "reason": f"时间止损：持仓 {age_days:.1f} 天浮盈仅 {gain_r:+.2f}R（<{S['time_stop_min_r']}R），资金效率过低离场",
                }
            ],
            peak,
            stop,
        )

    return [], peak, stop


# ---------- 闸门 ----------
def check_circuit_breaker() -> tuple[bool, str]:
    """熔断检查。返回 (是否停机, 原因)。"""
    day = market_data.market_day()
    account = market_data.get_account()
    db.set_day_start_equity(day, account["last_equity"])
    rec = db.get_day(day)
    start = rec["start_equity"] if rec else account["last_equity"]

    if account["equity"] < start * (1 - RISK["daily_loss_circuit_pct"]):
        db.mark_circuit_triggered(day)
        db.log_event(
            "alert",
            f"触发日内熔断：equity {account['equity']:.0f} < 起始 {start:.0f} 的 {1 - RISK['daily_loss_circuit_pct']:.0%}",
        )
        n = db.consecutive_circuit_days()
        if n >= RISK["max_consecutive_circuit_days"]:
            db.log_event("halt", f"连续 {n} 天熔断，系统停机，需人工介入")
            return True, f"连续 {n} 天熔断，系统停机"
        return True, "触发日内熔断，今日停止开新仓"
    return False, ""


def gate(
    proposal: dict,
    account: dict,
    positions: list[dict],
    open_trades: dict[str, dict],
) -> tuple[bool, str]:
    """下单前硬闸门。open_trades: {symbol: trade_row}。返回 (通过?, 原因)。"""
    action, symbol = proposal["action"], proposal["symbol"]
    equity = account["equity"]

    if proposal.get("confidence", 0) < RISK["min_confidence"]:
        return False, f"置信度 {proposal.get('confidence')} < {RISK['min_confidence']}"

    pos = next((p for p in positions if p["symbol"] == symbol), None)

    if action == "buy":
        if pos:
            # 金字塔补仓：只加赢家
            t = open_trades.get(symbol)
            if not t or not t["r_per_share"]:
                return False, f"{symbol} 缺少交易记录/风险参数，无法评估补仓"
            gain_r = (pos["current_price"] - t["entry_price"]) / t["r_per_share"]
            if gain_r < S["pyramid_min_gain_r"]:
                return (
                    False,
                    f"浮盈 {gain_r:+.1f}R < {S['pyramid_min_gain_r']}R，不满足金字塔加仓（只加赢家）",
                )
            if t["tranches"] > S["pyramid_max_adds"]:
                return False, f"{symbol} 补仓次数已用完（{S['pyramid_max_adds']} 次）"
            if pos["market_value"] >= equity * RISK["max_position_pct"]:
                return (
                    False,
                    f"{symbol} 已达单票市值上限 {RISK['max_position_pct']:.0%}",
                )
        else:
            if len(positions) >= RISK["max_positions"]:
                return False, f"持仓数已达上限 {RISK['max_positions']}"
            heat = portfolio_heat(list(open_trades.values()), equity)
            if heat + S["risk_per_trade_pct"] > S["max_portfolio_heat_pct"]:
                return (
                    False,
                    f"组合总风险 {heat:.1%}+{S['risk_per_trade_pct']:.1%} 超上限 {S['max_portfolio_heat_pct']:.0%}",
                )
            # 板块集中度：相关性风险不体现在个股止损上，必须在组合层限制
            sectors = S.get("sectors", {})
            my_sector = sectors.get(symbol)
            if my_sector:
                same = sum(
                    1 for p in positions if sectors.get(p["symbol"]) == my_sector
                )
                if same >= S["max_per_sector"]:
                    return (
                        False,
                        f"{my_sector}板块持仓已达 {S['max_per_sector']} 只上限（相关性风险）",
                    )
            if account["cash"] < 200:
                return False, "现金不足"

    elif action == "sell":
        if not pos:
            return False, f"{symbol} 不在持仓中，无法卖出"

    return True, ""
