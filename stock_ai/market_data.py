"""Alpaca 行情与账户：clock、account、positions、K线、最新价。"""

from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient

from .config import ALPACA_API_KEY, ALPACA_SECRET_KEY, SCHEDULE

_trading = None
_data = None


def trading() -> TradingClient:
    global _trading
    if _trading is None:
        _trading = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
    return _trading


def data() -> StockHistoricalDataClient:
    global _data
    if _data is None:
        _data = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return _data


def is_market_open() -> bool:
    return bool(trading().get_clock().is_open)


def market_day() -> str:
    """当前美东交易日（用于熔断记录按天对齐）。"""
    return str(trading().get_clock().timestamp.date())


def get_account() -> dict:
    a = trading().get_account()
    return {
        "equity": float(a.equity),
        "cash": float(a.cash),
        "buying_power": float(a.buying_power),
        "last_equity": float(a.last_equity),
    }


def get_positions() -> list[dict]:
    out = []
    for p in trading().get_all_positions():
        out.append(
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
        )
    return out


_TF_MAP = {
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame.Hour,
    "1Day": TimeFrame.Day,
}


def get_bars(symbol: str, days: int | None = None) -> pd.DataFrame:
    days = days or SCHEDULE["lookback_days"]
    tf = _TF_MAP.get(SCHEDULE["bar_timeframe"], TimeFrame(15, TimeFrameUnit.Minute))
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=datetime.now(timezone.utc) - timedelta(days=days),
    )
    df = data().get_stock_bars(req).df
    if df.empty:
        return df
    if isinstance(df.index, pd.MultiIndex):
        df = (
            df.xs(symbol, level="symbol")
            if symbol in df.index.get_level_values("symbol")
            else df
        )
    return df.sort_index()


def latest_price(symbol: str) -> float | None:
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        return float(data().get_stock_latest_trade(req)[symbol].price)
    except Exception:
        return None


def get_daily_bars(symbol: str, days: int = 120) -> pd.DataFrame:
    """日线 K线（大盘 regime 判断用）。"""
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=days),
    )
    df = data().get_stock_bars(req).df
    if df.empty:
        return df
    if isinstance(df.index, pd.MultiIndex):
        df = (
            df.xs(symbol, level="symbol")
            if symbol in df.index.get_level_values("symbol")
            else df
        )
    return df.sort_index()


def five_min_change(symbol: str) -> float:
    """最近 5 分钟涨跌幅（异动检测用）。数据不足返回 0。"""
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    df = data().get_stock_bars(req).df
    if len(df) < 2:
        return 0.0
    closes = df["close"].tolist()
    return (closes[-1] - closes[0]) / closes[0]
