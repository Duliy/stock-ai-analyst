"""Web 仪表盘：持仓 / 净值 / 订单 / 交易复盘 / 待批准提案（批准/拒绝）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from stock_ai import db, execution, market_data

app = FastAPI(title="美股 AI 分析师")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)


@app.on_event("startup")
def startup():
    db.init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/status")
def status():
    try:
        account = market_data.get_account()
        positions = market_data.get_positions()
        open_now = market_data.is_market_open()
    except Exception as e:
        raise HTTPException(502, f"Alpaca 连接失败: {e}")
    return {"account": account, "positions": positions, "market_open": open_now}


@app.get("/api/equity")
def equity(limit: int = 500):
    return db.query(
        "SELECT ts, equity, cash FROM equity_snapshots ORDER BY id DESC LIMIT ?",
        (limit,),
    )[::-1]


@app.get("/api/decisions")
def decisions(status: str | None = None, limit: int = 50):
    sql = "SELECT * FROM decisions"
    args: tuple = ()
    if status:
        sql += " WHERE status=?"
        args = (status,)
    sql += " ORDER BY id DESC LIMIT ?"
    args += (limit,)
    return db.query(sql, args)


@app.get("/api/orders")
def orders(limit: int = 50):
    return db.query("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/trades")
def trades(limit: int = 100):
    return db.query("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/events")
def events(limit: int = 50):
    return db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


@app.post("/api/decisions/{decision_id}/approve")
def approve(decision_id: int):
    d = db.get_decision(decision_id)
    if not d:
        raise HTTPException(404, "decision 不存在")
    if d["status"] != "pending_approval":
        raise HTTPException(400, f"当前状态 {d['status']}，不可批准")
    result = execution.execute(decision_id)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "执行失败"))
    return {"ok": True}


@app.post("/api/decisions/{decision_id}/reject")
def reject(decision_id: int):
    d = db.get_decision(decision_id)
    if not d:
        raise HTTPException(404, "decision 不存在")
    if d["status"] != "pending_approval":
        raise HTTPException(400, f"当前状态 {d['status']}，不可拒绝")
    db.set_decision_status(decision_id, "rejected", "人工拒绝")
    db.log_event("info", f"提案 #{decision_id} 被人工拒绝")
    return {"ok": True}
