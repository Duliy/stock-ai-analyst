"""飞书通知：异常警报 / 待批准提案 / 日报。未配置 webhook 时只写日志。"""

import httpx

from . import db
from .config import FEISHU_WEBHOOK


def send(title: str, content: str):
    db.log_event("info", f"[notify] {title}: {content[:100]}")
    if not FEISHU_WEBHOOK:
        return
    try:
        httpx.post(
            FEISHU_WEBHOOK,
            json={
                "msg_type": "interactive",
                "card": {
                    "header": {
                        "title": {"tag": "plain_text", "content": f"📈 {title}"},
                        "template": "blue",
                    },
                    "elements": [{"tag": "markdown", "content": content}],
                },
            },
            timeout=10,
        )
    except Exception as e:
        db.log_event("alert", f"飞书通知发送失败: {e}")
