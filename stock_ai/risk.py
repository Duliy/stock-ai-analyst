"""硬风控闸门：AI 无权豁免。所有下单提案必须过 gate()。"""

from . import db, market_data
from .config import RISK


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


def gate(proposal: dict, account: dict, positions: list[dict]) -> tuple[bool, str]:
    """下单前硬闸门。返回 (通过?, 原因)。"""
    action, symbol = proposal["action"], proposal["symbol"]

    if proposal.get("confidence", 0) < RISK["min_confidence"]:
        return False, f"置信度 {proposal.get('confidence')} < {RISK['min_confidence']}"

    if action == "buy":
        if len(positions) >= RISK["max_positions"]:
            return False, f"持仓数已达上限 {RISK['max_positions']}"
        if any(p["symbol"] == symbol for p in positions):
            return False, f"{symbol} 已在持仓中，不重复加仓"
        if proposal.get("position_pct", 1) > RISK["max_position_pct"]:
            return (
                False,
                f"目标仓位 {proposal['position_pct']:.0%} 超过单票上限 {RISK['max_position_pct']:.0%}",
            )
        est_cost = account["equity"] * proposal.get("position_pct", 0)
        if est_cost > account["cash"]:
            return False, "现金不足"

    elif action == "sell":
        if not any(p["symbol"] == symbol for p in positions):
            return False, f"{symbol} 不在持仓中，无法卖出"

    return True, ""


def check_stop_loss(positions: list[dict]) -> list[dict]:
    """检查持仓硬止损（-7%），返回需立即平仓的持仓列表。"""
    out = []
    for p in positions:
        if p["unrealized_plpc"] <= -RISK["stop_loss_pct"]:
            out.append(p)
            db.log_event(
                "alert",
                f"{p['symbol']} 触发硬止损：浮亏 {p['unrealized_plpc']:.1%} ≤ -{RISK['stop_loss_pct']:.0%}",
            )
    return out
