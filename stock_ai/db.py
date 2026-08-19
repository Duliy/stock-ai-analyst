"""SQLite 存储：决策、订单、完整交易（round-trip）、复盘、熔断记录。"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,          -- buy / sell / hold
    confidence INTEGER,
    reasoning TEXT,
    advocate_verdict TEXT,         -- 反方风控官结论 JSON
    status TEXT NOT NULL,          -- executed / pending_approval / rejected / skipped / gated
    gate_reason TEXT,              -- 被风控拦截的原因
    context_json TEXT              -- 决策时上下文快照（新闻摘要+指标）
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,            -- buy / sell
    qty REAL,
    notional REAL,
    filled_price REAL,
    alpaca_id TEXT,
    status TEXT,
    decision_id INTEGER
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_ts TEXT NOT NULL,
    exit_price REAL,
    exit_ts TEXT,
    return_pct REAL,
    holding_hours REAL,
    status TEXT NOT NULL DEFAULT 'open',   -- open / closed
    review TEXT,                            -- LLM 复盘全文
    lesson TEXT                             -- 一句话教训（供 L2 召回）
);
CREATE TABLE IF NOT EXISTS circuit_days (
    day TEXT PRIMARY KEY,          -- YYYY-MM-DD（美东）
    start_equity REAL,
    triggered INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,           -- info / alert / halt
    message TEXT
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS news_cache (
    symbol TEXT PRIMARY KEY,
    articles_hash TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    ts TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        # 老库迁移：补齐 v2 策略列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
        for name, ddl in _TRADE_NEW_COLS.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {ddl}")


def log_event(level: str, message: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO events(ts, level, message) VALUES (?,?,?)",
            (utcnow(), level, message),
        )


# ---------- decisions ----------
def record_decision(d: dict) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO decisions(ts,symbol,action,confidence,reasoning,
               advocate_verdict,status,gate_reason,context_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                utcnow(),
                d["symbol"],
                d["action"],
                d.get("confidence"),
                d.get("reasoning"),
                json.dumps(d.get("advocate"), ensure_ascii=False),
                d["status"],
                d.get("gate_reason"),
                json.dumps(d.get("context", {}), ensure_ascii=False),
            ),
        )
        return cur.lastrowid


def set_decision_status(decision_id: int, status: str, gate_reason: str | None = None):
    with db() as conn:
        conn.execute(
            "UPDATE decisions SET status=?, gate_reason=COALESCE(?,gate_reason) WHERE id=?",
            (status, gate_reason, decision_id),
        )


def get_decision(decision_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM decisions WHERE id=?", (decision_id,)
        ).fetchone()
        return dict(row) if row else None


# ---------- orders ----------
def record_order(o: dict) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO orders(ts,symbol,side,qty,notional,filled_price,
               alpaca_id,status,decision_id) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                utcnow(),
                o["symbol"],
                o["side"],
                o.get("qty"),
                o.get("notional"),
                o.get("filled_price"),
                o.get("alpaca_id"),
                o.get("status"),
                o.get("decision_id"),
            ),
        )
        return cur.lastrowid


# ---------- trades ----------
# v2 策略引擎新增列（通过 init_db 里的迁移自动添加）：
#   atr_entry/stop_price/peak_price/r_per_share/planned_qty/tranches/half_sold

_TRADE_NEW_COLS = {
    "atr_entry": "atr_entry REAL",
    "stop_price": "stop_price REAL",
    "peak_price": "peak_price REAL",
    "r_per_share": "r_per_share REAL",
    "planned_qty": "planned_qty REAL",
    "tranches": "tranches INTEGER DEFAULT 1",
    "half_sold": "half_sold INTEGER DEFAULT 0",
}


def open_trade(
    symbol: str,
    qty: float,
    entry_price: float,
    stop: float | None = None,
    r: float | None = None,
    planned_qty: float | None = None,
    atr: float | None = None,
    tranches: int = 1,
) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO trades(symbol,qty,entry_price,entry_ts,status,
               atr_entry,stop_price,peak_price,r_per_share,planned_qty,tranches)
               VALUES (?,?,?,?,'open',?,?,?,?,?,?)""",
            (
                symbol,
                qty,
                entry_price,
                utcnow(),
                atr,
                stop,
                entry_price,
                r,
                planned_qty,
                tranches,
            ),
        )
        return cur.lastrowid


def update_trade_state(
    trade_id: int,
    peak: float | None = None,
    stop: float | None = None,
    tranches: int | None = None,
    half_sold: bool | None = None,
):
    sets, args = [], []
    if peak is not None:
        sets.append("peak_price=?")
        args.append(peak)
    if stop is not None:
        sets.append("stop_price=?")
        args.append(stop)
    if tranches is not None:
        sets.append("tranches=?")
        args.append(tranches)
    if half_sold is not None:
        sets.append("half_sold=?")
        args.append(int(half_sold))
    if not sets:
        return
    args.append(trade_id)
    with db() as conn:
        conn.execute(f"UPDATE trades SET {', '.join(sets)} WHERE id=?", args)


def close_trade(trade_id: int, exit_price: float, review: str, lesson: str):
    with db() as conn:
        t = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        ret = (exit_price - t["entry_price"]) / t["entry_price"]
        entry = datetime.fromisoformat(t["entry_ts"])
        hours = (datetime.now(timezone.utc) - entry).total_seconds() / 3600
        conn.execute(
            """UPDATE trades SET exit_price=?, exit_ts=?, return_pct=?,
               holding_hours=?, status='closed', review=?, lesson=? WHERE id=?""",
            (
                exit_price,
                utcnow(),
                round(ret, 6),
                round(hours, 2),
                review,
                lesson,
                trade_id,
            ),
        )


def get_open_trades() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM trades WHERE status='open'").fetchall()
        return [dict(r) for r in rows]


def get_lessons(symbol: str | None = None, limit: int = 10) -> list[dict]:
    """L2：召回历史教训。优先同股票，其次全局最近。"""
    with db() as conn:
        rows = []
        if symbol:
            rows += conn.execute(
                "SELECT symbol, return_pct, lesson FROM trades "
                "WHERE status='closed' AND lesson IS NOT NULL AND symbol=? "
                "ORDER BY id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        rows += conn.execute(
            "SELECT symbol, return_pct, lesson FROM trades "
            "WHERE status='closed' AND lesson IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        seen, out = set(), []
        for r in rows:
            if r["lesson"] not in seen:
                seen.add(r["lesson"])
                out.append(dict(r))
        return out[:limit]


# ---------- circuit breaker ----------
def set_day_start_equity(day: str, equity: float):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO circuit_days(day, start_equity) VALUES (?,?)",
            (day, equity),
        )


def get_day(day: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM circuit_days WHERE day=?", (day,)).fetchone()
        return dict(row) if row else None


def mark_circuit_triggered(day: str):
    with db() as conn:
        conn.execute("UPDATE circuit_days SET triggered=1 WHERE day=?", (day,))


def consecutive_circuit_days() -> int:
    """连续触发熔断的天数（按最近记录往回数）。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT day, triggered FROM circuit_days ORDER BY day DESC LIMIT 10"
        ).fetchall()
    n = 0
    for r in rows:
        if r["triggered"]:
            n += 1
        else:
            break
    return n


# ---------- 净值快照 ----------
def record_equity_snapshot(equity: float, cash: float):
    with db() as conn:
        conn.execute(
            "INSERT INTO equity_snapshots(ts, equity, cash) VALUES (?,?,?)",
            (utcnow(), equity, cash),
        )


# ---------- 新闻摘要缓存 ----------
def get_news_cache(symbol: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM news_cache WHERE symbol=?", (symbol,)
        ).fetchone()
        return dict(row) if row else None


def set_news_cache(symbol: str, articles_hash: str, summary: dict):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO news_cache(symbol, articles_hash, summary_json, ts) VALUES (?,?,?,?)",
            (symbol, articles_hash, json.dumps(summary, ensure_ascii=False), utcnow()),
        )


# ---------- 仪表盘查询 ----------
def query(sql: str, args: tuple = ()) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
