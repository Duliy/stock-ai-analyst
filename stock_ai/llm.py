"""charmland LLM 客户端：flash（摘要粗活）/ pro（决策、反方复审、复盘）。"""

import json
import re

from openai import OpenAI

from .config import CHARMLAND_API_KEY, MODELS

_client = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=CHARMLAND_API_KEY, base_url=MODELS["base_url"])
    return _client


def _chat(model: str, system: str, user: str, max_tokens: int = 2000) -> str:
    resp = client().chat.completions.create(
        model=model,
        temperature=MODELS["temperature"],
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()


def _parse_json(text: str) -> dict:
    """容错解析 LLM 输出的 JSON。"""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


# ---------- flash：新闻摘要打分 ----------
def summarize_news(symbol: str, articles: list[dict]) -> dict:
    """输入新闻列表，输出 {sentiment: -1~1, importance: 0~10, summary}。"""
    if not articles:
        return {"sentiment": 0, "importance": 0, "summary": "无相关新闻"}
    items = "\n".join(
        f"[{i + 1}] {a['headline']} — {a.get('summary', '')[:200]}"
        for i, a in enumerate(articles)
    )
    out = _chat(
        MODELS["flash"],
        "你是财经新闻分析助手。只输出 JSON，不要任何额外文字。",
        f"以下是股票 {symbol} 的最新新闻：\n{items}\n\n"
        '请输出 JSON：{"sentiment": -1.0到1.0的数字, "importance": 0到10的整数'
        '（对股价短期影响的重大程度）, "summary": "100字以内中文综合摘要"}',
        max_tokens=500,
    )
    r = _parse_json(out)
    return {
        "sentiment": float(r.get("sentiment", 0) or 0),
        "importance": int(r.get("importance", 0) or 0),
        "summary": r.get("summary", out[:200]),
    }


# ---------- pro：交易决策 ----------
DECIDE_SYSTEM = """你是一名谨慎的美股分析师。基于给定的新闻情绪、技术指标、当前持仓和历史教训，
输出交易决策。只输出 JSON，不要任何额外文字。格式：
{"action": "buy"|"sell"|"hold",
 "symbol": "股票代码",
 "position_pct": 0到0.10的数字（buy 时目标仓位占总资产比例）,
 "confidence": 1到10的整数,
 "reasoning": "200字以内中文理由"}
规则：
- 没把握就 hold，宁缺毋滥
- buy 只能针对候选名单中的股票
- sell 只能针对当前持仓
- 必须参考历史教训，避免重复犯错"""


def decide(context: dict) -> dict:
    out = _chat(
        MODELS["pro"],
        DECIDE_SYSTEM,
        json.dumps(context, ensure_ascii=False, indent=1),
        max_tokens=800,
    )
    r = _parse_json(out)
    return {
        "action": r.get("action", "hold"),
        "symbol": r.get("symbol", ""),
        "position_pct": float(r.get("position_pct", 0) or 0),
        "confidence": int(r.get("confidence", 0) or 0),
        "reasoning": r.get("reasoning", out[:300]),
    }


# ---------- pro：反方风控官（devil's advocate）----------
ADVOCATE_SYSTEM = """你是反方风控官，专门挑刺。给定一笔拟执行的交易提案及其决策依据，
你的任务是找出它可能错在哪里：新闻是否有反向解读？技术形态是否假突破？
是否忽略了系统性风险？只输出 JSON：
{"verdict": "approve"|"downgrade"|"escalate",
 "objections": ["异议1", "异议2"],
 "comment": "100字以内中文总结"}
- approve: 无实质异议
- downgrade: 有顾虑但不致命，建议降低置信度
- escalate: 有重大疑点，应转人工审批"""


def advocate(proposal: dict, context: dict) -> dict:
    out = _chat(
        MODELS["pro"],
        ADVOCATE_SYSTEM,
        f"拟交易提案：\n{json.dumps(proposal, ensure_ascii=False)}\n\n"
        f"决策上下文：\n{json.dumps(context, ensure_ascii=False, indent=1)}",
        max_tokens=600,
    )
    r = _parse_json(out)
    return {
        "verdict": r.get("verdict", "escalate"),
        "objections": r.get("objections", []),
        "comment": r.get("comment", out[:200]),
    }


# ---------- pro：平仓复盘 ----------
REVIEW_SYSTEM = """你是交易复盘分析师。给定一笔已完成交易及其开平仓时的上下文，
输出 JSON：{"review": "300字以内中文复盘：做对了什么、错在哪、市场如何演变",
"lesson": "一句话可复用的教训（30字以内，将注入未来决策）"}"""


def review_trade(trade: dict, context: dict) -> dict:
    out = _chat(
        MODELS["pro"],
        REVIEW_SYSTEM,
        f"交易记录：\n{json.dumps(trade, ensure_ascii=False)}\n\n"
        f"相关上下文：\n{json.dumps(context, ensure_ascii=False, indent=1)}",
        max_tokens=800,
    )
    r = _parse_json(out)
    return {
        "review": r.get("review", out[:500]),
        "lesson": r.get("lesson", ""),
    }
