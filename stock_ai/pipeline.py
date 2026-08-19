"""决策流水线：每小时一轮的主流程编排。

流程：熔断检查 → 对账/复盘 → 出场引擎（止损/保本/止盈，不走 LLM）
     → 新闻(flash) → 指标 → pro 决策 → 反方复审 → 硬闸门 → 执行/挂起待批准
"""

from . import db, execution, indicators, llm, market_data, news, notify, risk
from .config import FINNHUB_API_KEY, NEWS, RISK, STRATEGY, WATCHLIST


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

    # 2. 对账：新开仓登记 + 已平仓复盘
    execution.sync_trades()

    # 3. 出场引擎（最高优先级，纯规则不走 LLM）：
    #    初始止损 → +1R 保本 → +2R 卖半 → 吊灯移动止损
    positions = market_data.get_positions()
    open_trades = {t["symbol"]: t for t in db.get_open_trades()}
    for p in positions:
        t = open_trades.get(p["symbol"])
        if not t:
            continue
        ind = indicators.compute(market_data.get_bars(p["symbol"]))
        actions, peak, stop = risk.evaluate_exits(p, t, ind.get("atr14"))
        db.update_trade_state(t["id"], peak=peak, stop=stop)
        for a in actions:
            did = db.record_decision(
                {
                    "symbol": p["symbol"],
                    "action": "sell",
                    "confidence": 10,
                    "reasoning": a["reason"],
                    "status": "approved",
                    "context": {
                        "trigger": "exit_engine",
                        "exit_type": a["type"],
                        "sell_fraction": 0.5 if a["type"] == "sell_half" else 1,
                    },
                }
            )
            result = execution.execute(did)
            if result.get("ok") and a["type"] == "sell_half":
                db.update_trade_state(t["id"], half_sold=True)
            db.log_event("alert", f"出场引擎：{a['reason']}")
        if any(a["type"] == "stop_all" for a in actions):
            open_trades.pop(p["symbol"], None)

    # 4. 收集上下文
    account = market_data.get_account()
    db.record_equity_snapshot(account["equity"], account["cash"])
    positions = market_data.get_positions()
    position_symbols = {p["symbol"] for p in positions}
    symbols = sorted(set(WATCHLIST["core"] + (extra_symbols or [])) | position_symbols)

    # 3.5 全局环境检查（逻辑见 README 策略节）
    # a. 大盘 regime：SPY 跌破日线 50 均线 → 禁止一切买入（做多胜率与大盘强相关）
    spy_ind = indicators.compute(market_data.get_daily_bars("SPY", 120))
    market_weak = not spy_ind.get("above_sma50", True)
    if market_weak:
        db.log_event(
            "alert", f"大盘弱势：SPY ${spy_ind.get('price')} < 50日均线，本轮禁止开新仓"
        )
    # b. 尾盘禁开仓：收盘前买入=立即暴露隔夜跳空且止损隔夜不生效
    clock = market_data.trading().get_clock()
    mins_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60
    near_close = mins_to_close < STRATEGY["no_entry_before_close_min"]
    no_new_buys = market_weak or near_close

    # 4. 逐股分析：新闻（带时效标注 + 摘要缓存）+ 技术指标
    import hashlib
    import json as _json
    from datetime import datetime, timezone

    cache_ttl = NEWS.get("cache_ttl_hours", 2) * 3600
    analyses = {}
    for sym in symbols:
        articles = news.get_news(sym)
        art_hash = hashlib.sha1(
            "|".join(str(a.get("id")) for a in articles).encode()
        ).hexdigest()
        cached = db.get_news_cache(sym)
        cache_age = (
            (
                datetime.now(timezone.utc) - datetime.fromisoformat(cached["ts"])
            ).total_seconds()
            if cached
            else 1e18
        )
        if cached and cached["articles_hash"] == art_hash and cache_age < cache_ttl:
            news_summary = _json.loads(cached["summary_json"])  # 输入未变 → 复用摘要
        else:
            news_summary = llm.summarize_news(sym, articles)
            db.set_news_cache(sym, art_hash, news_summary)
        ind = indicators.compute(market_data.get_bars(sym))
        analyses[sym] = {
            "news": news_summary,
            "news_freshness_min": min((a["age_min"] for a in articles), default=None),
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
        "market_regime": "weak(SPY<50日线)" if market_weak else "normal",
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

        # 全局禁买窗口：大盘弱势 / 临近收盘（卖出与止损永远允许）
        if proposal["action"] == "buy" and no_new_buys:
            why = (
                "大盘弱势（SPY<50日均线）"
                if market_weak
                else f"距收盘仅 {mins_to_close:.0f} 分钟"
            )
            db.record_decision(
                {**base, "status": "gated", "gate_reason": f"禁买窗口：{why}"}
            )
            db.log_event("info", f"禁买窗口拦截：{sym}（{why}）")
            continue

        # 财报黑洞：财报是二元跳空事件，止损单会在 gap 中失效（需 Finnhub key）
        if (
            proposal["action"] == "buy"
            and FINNHUB_API_KEY
            and not any(p["symbol"] == sym for p in positions)
        ):
            earn = news.get_earnings_calendar(sym)
            if earn and earn.get("date"):
                from datetime import date

                days_to = (date.fromisoformat(earn["date"]) - date.today()).days
                if 0 <= days_to <= STRATEGY["earnings_blackout_days"]:
                    db.record_decision(
                        {
                            **base,
                            "status": "gated",
                            "gate_reason": f"财报黑洞：{days_to} 天后（{earn['date']}）发布财报",
                        }
                    )
                    db.log_event("info", f"财报黑洞拦截：{sym} {earn['date']} 发财报")
                    continue

        ok, gate_reason = risk.gate(proposal, account, positions, open_trades)
        if not ok:
            db.record_decision({**base, "status": "gated", "gate_reason": gate_reason})
            db.log_event("info", f"提案被风控拦截：{sym} {gate_reason}")
            continue

        did = db.record_decision({**base, "status": "approved"})
        result = execution.execute(did)
        if result.get("ok"):
            # 成交后刷新账户、持仓、交易记录，供后续提案的风控判断
            account = market_data.get_account()
            positions = market_data.get_positions()
            open_trades = {t["symbol"]: t for t in db.get_open_trades()}

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
