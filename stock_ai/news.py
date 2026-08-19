"""新闻源：Alpaca News API（Benzinga）为主，Finnhub 财报日历为可选补充。"""

from datetime import datetime, timedelta, timezone

import httpx
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from .config import ALPACA_API_KEY, ALPACA_SECRET_KEY, FINNHUB_API_KEY, NEWS

_news_client = None


def _client() -> NewsClient:
    global _news_client
    if _news_client is None:
        _news_client = NewsClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return _news_client


def get_news(symbol: str) -> list[dict]:
    req = NewsRequest(
        symbols=symbol,
        start=datetime.now(timezone.utc) - timedelta(hours=NEWS["lookback_hours"]),
        limit=NEWS["max_articles_per_symbol"],
    )
    try:
        news = _client().get_news(req)
        now = datetime.now(timezone.utc)
        out = []
        for n in news.data.get("news", []):
            created = n.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_min = int((now - created).total_seconds() / 60)
            out.append(
                {
                    "id": n.id,
                    "headline": n.headline,
                    "summary": n.summary or "",
                    "source": n.source,
                    "created_at": str(n.created_at),
                    "age_min": age_min,  # 新闻年龄（分钟），时效性核心字段
                    "url": n.url,
                }
            )
        return out
    except Exception:
        return []


def get_earnings_calendar(symbol: str) -> dict | None:
    """Finnhub 可选补充：下一次财报日期。未配置 key 时返回 None。"""
    if not FINNHUB_API_KEY:
        return None
    try:
        today = datetime.now(timezone.utc).date()
        r = httpx.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={
                "from": str(today),
                "to": str(today + timedelta(days=30)),
                "symbol": symbol,
                "token": FINNHUB_API_KEY,
            },
            timeout=10,
        )
        items = r.json().get("earningsCalendar", [])
        return items[0] if items else None
    except Exception:
        return None
