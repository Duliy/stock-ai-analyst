"""决策流水线：每小时一轮的主流程编排。

流程：熔断检查 → 止损扫描 → 对账/复盘 → 新闻(flash) → 指标 → pro 决策
     → 反方复审 → 硬闸门 → 执行 / 挂起待批准 → 通知
"""

from . import db, execution, indicators, llm, market_data, news, notify, risk
from .config import RISK, WATCHLIST


def run_cycle(extra_symbols: list[str] | None = None):
    """跑一轮决策。extra_symbols 为异动加轮的触发来源（仅记录）。"""
    db.init_db()

    if not market_data.is_market_open():
        db.log_event("info", "市场未开盘，跳过本轮")
        return

    # 1. 熔断
    halted, reason = risk.check_circuit_breaker()
    if halted:
        notify.send("🚨 熔断停机", reason)
        return

    # 2. 持仓硬止损（最高优先级，直接执行，不走 LLM）
    positions = market_data.get_positions()
    for p in risk.check_stop_loss(positions):
        did = db.record_decision(
            {
                "symbol": p["symbol"],
                "action": "sell",
                "confidence": 10,
                "reasoning": f"硬止损触发：浮亏 {p['unrealized_plpc']:.1%}",
                "status": "gated",
                "context": {"trigger": "stop_loss"},
            }
        )
        execution.execute(did)

    # 3. 对账：新开仓登记 + 已平仓复盘
    execution.sync_trades()

    # 4. 收集上下文
    account = market_data.get_account()
    db.record_equity_snapshot(account["equity"], account["cash"])
    positions = market_data.get_positions()
    position_symbols = {p["symbol"] for p in positions}
    symbols = sorted(set(WATCHLIST["core"] + (extra_symbols or [])) | position_symbols)

    analyses = {}
    for sym in symbols:
        articles = news.get_news(sym)
        news_summary = llm.summarize_news(sym, articles)
        ind = indicators.compute(market_data.get_bars(sym))
        analyses[sym] = {
            "news": news_summary,
            "indicators": ind,
            "held": sym in position_symbols,
        }

    # 5. pro 决策（L2：注入历史教训）
    context = {
        "account": account,
        "positions": positions,
        "candidates": WATCHLIST["core"],
        "analyses": analyses,
        "lessons": db.get_lessons(limit=10),
        "constraints": {
            "max_position_pct": RISK["max_position_pct"],
            "max_positions": RISK["max_positions"],
            "min_confidence": RISK["min_confidence"],
        },
    }
    decisions = llm.decide(context)
    if not decisions:
        db.log_event("alert", "pro 模型未返回有效决策列表，本轮跳过")
        return

    # 先卖后买（释放仓位和现金），同方向按置信度降序
    decisions.sort(key=lambda d: (d["action"] != "sell", -(d.get("confidence") or 0)))

    pending_msgs = []
    for proposal in decisions:
        sym = proposal["symbol"]
        if proposal["action"] == "hold":
            db.record_decision(
                {
                    "symbol": sym,
                    "action": "hold",
                    "confidence": proposal.get("confidence"),
                    "reasoning": proposal.get("reasoning"),
                    "status": "skipped",
                    "context": {"analyses": analyses.get(sym)},
                }
            )
            continue

        # 反方风控官复审（仅对下单提案）
        adv = llm.advocate(proposal, context)
        if adv["verdict"] == "downgrade":
            proposal["confidence"] = max(1, proposal.get("confidence", 5) - 1)

        base = {
            "symbol": sym,
            "action": proposal["action"],
            "confidence": proposal["confidence"],
            "reasoning": proposal["reasoning"],
            "advocate": adv,
            "context": {"proposal": proposal, "analyses": analyses.get(sym)},
        }

        if adv["verdict"] == "escalate":
            did = db.record_decision(
                {
                    **base,
                    "status": "pending_approval",
                    "gate_reason": "反方风控官有重大异议: " + adv["comment"],
                }
            )
            pending_msgs.append(
                f"**#{did} {proposal['action'].upper()} {sym}** 置信度 {proposal['confidence']}/10\n"
                f"理由：{proposal['reasoning']}\n反方异议：{adv['comment']}"
            )
            continue

        ok, gate_reason = risk.gate(proposal, account, positions)
        if not ok:
            db.record_decision({**base, "status": "gated", "gate_reason": gate_reason})
            db.log_event("info", f"提案被风控拦截：{sym} {gate_reason}")
            continue

        did = db.record_decision({**base, "status": "approved"})
        result = execution.execute(did)
        if result.get("ok"):
            # 成交后刷新账户和持仓，供后续提案的风控判断
            account = market_data.get_account()
            positions = market_data.get_positions()

    if pending_msgs:
        notify.send(
            "⏳ 待批准提案",
            "\n\n".join(pending_msgs) + "\n\n请到仪表盘批准或拒绝",
        )


def run_spike_check():
    """异动加轮：持仓 5 分钟 ±3% 时触发，仅对异动股票重新评估。"""
    if not market_data.is_market_open():
        return
    spiked = []
    for p in market_data.get_positions():
        chg = market_data.five_min_change(p["symbol"])
        if abs(chg) >= RISK["spike_pct"]:
            spiked.append(p["symbol"])
            db.log_event("alert", f"{p['symbol']} 5分钟异动 {chg:+.1%}，触发加轮")
    if spiked:
        run_cycle(extra_symbols=spiked)


def daily_report() -> str:
    """收盘日报：写 Markdown 归档 + 推飞书。"""
    from datetime import datetime

    from .config import REPORT_DIR

    account = market_data.get_account()
    positions = market_data.get_positions()
    closed = db.query(
        "SELECT * FROM trades WHERE status='closed' ORDER BY id DESC LIMIT 20"
    )
    today_decisions = db.query("SELECT * FROM decisions ORDER BY id DESC LIMIT 50")

    lines = [
        f"# 美股 AI 日报 {datetime.now():%Y-%m-%d}",
        f"\n## 账户\n- 总资产: ${account['equity']:,.2f}（现金 ${account['cash']:,.2f}）",
        "\n## 当前持仓",
    ]
    for p in positions:
        lines.append(
            f"- {p['symbol']}: {p['qty']} 股 @ {p['avg_entry_price']}，浮盈 {p['unrealized_plpc']:+.1%}"
        )
    lines.append("\n## 近期已平仓（最近20笔）")
    for t in closed:
        lines.append(
            f"- {t['symbol']}: {t['return_pct']:+.2%}，持有 {t['holding_hours']}h — {t.get('lesson') or ''}"
        )
    lines.append(f"\n## 今日决策数: {len(today_decisions)}")
    report = "\n".join(lines)

    path = REPORT_DIR / f"{datetime.now():%Y%m%d}.md"
    path.write_text(report, encoding="utf-8")
    notify.send("📊 收盘日报", report[:2000])
    return str(path)
