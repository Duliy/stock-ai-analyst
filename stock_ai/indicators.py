"""技术指标：SMA、RSI、ATR、动量。用 pandas 手写，不依赖 pandas-ta（减少迁移负担）。"""

import pandas as pd


def compute(df: pd.DataFrame) -> dict:
    """输入 K线 DataFrame（含 close/high/low/volume），输出指标快照。"""
    if df is None or len(df) < 50:
        return {"error": "数据不足"}
    close = df["close"]
    high, low = df["high"], df["low"]

    sma20 = close.rolling(20).mean().iloc[-1]
    sma50 = close.rolling(50).mean().iloc[-1]
    price = close.iloc[-1]

    # RSI(14)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-10)
    rsi14 = (100 - 100 / (1 + rs)).iloc[-1]

    # ATR(14) 及波动率
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    atr14 = tr.rolling(14).mean().iloc[-1]

    # 动量：最近 20 根 K线收益率
    momentum_20 = price / close.iloc[-20] - 1

    return {
        "price": round(float(price), 2),
        "sma20": round(float(sma20), 2),
        "sma50": round(float(sma50), 2),
        "above_sma20": bool(price > sma20),
        "above_sma50": bool(price > sma50),
        "golden_cross": bool(sma20 > sma50),
        "rsi14": round(float(rsi14), 1),
        "atr14": round(float(atr14), 2),
        "atr_pct": round(float(atr14 / price), 4),
        "momentum_20bars": round(float(momentum_20), 4),
        "volume_ratio": round(
            float(
                df["volume"].iloc[-5:].mean() / max(df["volume"].iloc[-50:].mean(), 1)
            ),
            2,
        ),
    }
