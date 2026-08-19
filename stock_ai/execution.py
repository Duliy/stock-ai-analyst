"""Alpaca 下单执行 + 交易开平仓登记。"""

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from . import db, market_data


def execute(decision_id: int) -> dict:
    """执行一条已通过的决策（executed/gated 通过/人工批准）。"""
    d = db.get_decision(decision_id)
    if not d:
        return {"ok": False, "error": "decision 不存在"}
    if d["status"] == "executed":
        return {"ok": False, "error": "已执行过"}

    side = OrderSide.BUY if d["action"] == "buy" else OrderSide.SELL

    if d["action"] == "buy":
        account = market_data.get_account()
        import json

        ctx = json.loads(d["context_json"] or "{}")
        pct = ctx.get("proposal", {}).get("position_pct", 0.05)
        notional = round(account["equity"] * min(pct, 0.10), 2)
        req = MarketOrderRequest(
            symbol=d["symbol"],
            notional=notional,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
    else:
        pos = [p for p in market_data.get_positions() if p["symbol"] == d["symbol"]]
        if not pos:
            db.set_decision_status(decision_id, "skipped", "无持仓可卖")
            return {"ok": False, "error": "无持仓可卖"}
        req = MarketOrderRequest(
            symbol=d["symbol"],
            qty=pos[0]["qty"],
            side=side,
            time_in_force=TimeInForce.DAY,
        )

    order = market_data.trading().submit_order(req)
    db.record_order(
        {
            "symbol": d["symbol"],
            "side": d["action"],
            "qty": float(order.qty) if order.qty else None,
            "notional": float(order.notional) if order.notional else None,
            "alpaca_id": str(order.id),
            "status": str(order.status),
            "decision_id": decision_id,
        }
    )
    db.set_decision_status(decision_id, "executed")
    db.log_event(
        "info", f"已执行 {d['action']} {d['symbol']} (decision #{decision_id})"
    )
    return {"ok": True, "alpaca_id": str(order.id)}


def sync_trades():
    """对账：根据 Alpaca 实际持仓，登记新开仓、检测已平仓并触发复盘。

    简化假设（模拟盘 v1）：每只股票同时只有一笔 open trade，
    买入=开仓，清仓=平仓。部分成交/加仓在二期完善。
    """
    from . import llm

    positions = {p["symbol"]: p for p in market_data.get_positions()}
    open_trades = {t["symbol"]: t for t in db.get_open_trades()}

    # 新开仓登记
    for sym, p in positions.items():
        if sym not in open_trades:
            db.open_trade(sym, p["qty"], p["avg_entry_price"])
            db.log_event("info", f"登记开仓 {sym} @ {p['avg_entry_price']}")

    # 平仓检测 + LLM 复盘
    for sym, t in open_trades.items():
        if sym not in positions:
            exit_price = market_data.latest_price(sym) or t["entry_price"]
            review = llm.review_trade(
                {
                    "symbol": sym,
                    "qty": t["qty"],
                    "entry_price": t["entry_price"],
                    "exit_price": exit_price,
                    "return_pct": round(
                        (exit_price - t["entry_price"]) / t["entry_price"], 4
                    ),
                    "entry_ts": t["entry_ts"],
                },
                {"lessons_used": db.get_lessons(sym, limit=5)},
            )
            db.close_trade(t["id"], exit_price, review["review"], review["lesson"])
            db.log_event(
                "info",
                f"{sym} 平仓，回报率 {(exit_price - t['entry_price']) / t['entry_price']:.2%}，教训：{review['lesson']}",
            )
