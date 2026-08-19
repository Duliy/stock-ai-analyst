"""Alpaca 下单执行 + 交易开平仓登记。

仓位数量不由 LLM 决定：execute() 内部调用 risk 模块按风险预算计算，
LLM 的 position_pct 仅作为意向上限参考。
"""

import json

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from . import db, market_data, risk


def _submit(
    symbol: str,
    side: OrderSide,
    qty: float | None = None,
    notional: float | None = None,
):
    req = MarketOrderRequest(
        symbol=symbol,
        qty=round(qty, 4) if qty else None,
        notional=round(notional, 2) if notional else None,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    return market_data.trading().submit_order(req)


def execute(decision_id: int) -> dict:
    """执行一条已通过的决策（风控通过 / 人工批准 / 出场引擎）。"""
    d = db.get_decision(decision_id)
    if not d:
        return {"ok": False, "error": "decision 不存在"}
    if d["status"] == "executed":
        return {"ok": False, "error": "已执行过"}

    ctx = json.loads(d["context_json"] or "{}")
    positions = market_data.get_positions()
    pos = next((p for p in positions if p["symbol"] == d["symbol"]), None)

    if d["action"] == "buy":
        account = market_data.get_account()
        ind = (ctx.get("analyses") or {}).get("indicators") or {}
        price = market_data.latest_price(d["symbol"]) or ind.get("price")
        atr = ind.get("atr14")
        if not price:
            db.set_decision_status(decision_id, "skipped", "无法获取现价")
            return {"ok": False, "error": "无法获取现价"}

        if pos:
            # 金字塔补仓
            t = next(
                (x for x in db.get_open_trades() if x["symbol"] == d["symbol"]), None
            )
            if not t:
                db.set_decision_status(decision_id, "skipped", "无交易记录")
                return {"ok": False, "error": "无交易记录"}
            qty = risk.size_add(t, pos, account["equity"], account["cash"], price)
            if qty * price < 200:
                db.set_decision_status(decision_id, "skipped", "补仓空间不足")
                return {"ok": False, "error": "补仓空间不足"}
            db.update_trade_state(t["id"], tranches=t["tranches"] + 1)
            sizing = None
        else:
            sizing = risk.size_new_position(
                account["equity"], account["cash"], price, atr
            )
            if not sizing:
                db.set_decision_status(decision_id, "skipped", "风险预算下仓位过小")
                return {"ok": False, "error": "风险预算下仓位过小"}
            qty = sizing["qty"]

        order = _submit(d["symbol"], OrderSide.BUY, qty=qty)
        if sizing:
            ctx["sizing"] = sizing
            with db.db() as conn:
                conn.execute(
                    "UPDATE decisions SET context_json=? WHERE id=?",
                    (json.dumps(ctx, ensure_ascii=False), decision_id),
                )

    else:  # sell
        if not pos:
            db.set_decision_status(decision_id, "skipped", "无持仓可卖")
            return {"ok": False, "error": "无持仓可卖"}
        fraction = ctx.get("sell_fraction", 1)
        qty = pos["qty"] if fraction >= 1 else round(pos["qty"] * fraction, 4)
        order = _submit(d["symbol"], OrderSide.SELL, qty=qty)

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


def _register_trade(symbol: str, qty: float, entry_price: float):
    """开仓登记：优先从对应买入决策的 sizing 恢复风险参数，否则用 ATR 兜底。"""
    row = db.query(
        "SELECT context_json FROM decisions WHERE symbol=? AND action='buy' "
        "AND status='executed' ORDER BY id DESC LIMIT 1",
        (symbol,),
    )
    sizing = None
    if row:
        sizing = (json.loads(row[0]["context_json"] or "{}").get("sizing")) or None

    if sizing and sizing.get("r"):
        # 按实际成交价重定止损（止损距离不变）
        stop = entry_price - sizing["r"]
        db.open_trade(
            symbol,
            qty,
            entry_price,
            stop=stop,
            r=sizing["r"],
            planned_qty=sizing["planned_qty"],
            atr=None,
        )
    else:
        # 兜底（如历史遗留仓位）：用当前 ATR 计算，计划仓位=现有仓位（不再补仓）
        from . import indicators

        ind = indicators.compute(market_data.get_bars(symbol))
        atr = ind.get("atr14")
        dist = risk.stop_distance(entry_price, atr)
        db.open_trade(
            symbol,
            qty,
            entry_price,
            stop=entry_price - dist,
            r=dist,
            planned_qty=qty,
            atr=atr,
            tranches=1 + risk.S["pyramid_max_adds"],  # 视为已满仓，禁止补仓
        )


def sync_trades():
    """对账：登记新开仓、检测已平仓并触发 LLM 复盘。"""
    from . import llm

    positions = {p["symbol"]: p for p in market_data.get_positions()}
    open_trades = {t["symbol"]: t for t in db.get_open_trades()}

    # 新开仓登记
    for sym, p in positions.items():
        if sym not in open_trades:
            _register_trade(sym, p["qty"], p["avg_entry_price"])
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
                    "stop_price": t["stop_price"],
                    "half_sold": bool(t["half_sold"]),
                },
                {"lessons_used": db.get_lessons(sym, limit=5)},
            )
            db.close_trade(t["id"], exit_price, review["review"], review["lesson"])
            db.log_event(
                "info",
                f"{sym} 平仓，回报率 {(exit_price - t['entry_price']) / t['entry_price']:.2%}，教训：{review['lesson']}",
            )
