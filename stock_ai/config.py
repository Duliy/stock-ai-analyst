"""配置加载：config.yaml + .env，全部走相对项目根目录路径，方便整体打包迁移。"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)

MODELS = _cfg["models"]
WATCHLIST = _cfg["watchlist"]
RISK = _cfg["risk"]
STRATEGY = _cfg["strategy"]
SCHEDULE = _cfg["schedule"]
NEWS = _cfg["news"]
STORAGE = _cfg["storage"]

DB_PATH = ROOT / STORAGE["db_path"]
REPORT_DIR = ROOT / STORAGE["report_dir"]
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ---- 密钥（来自 .env）----
CHARMLAND_API_KEY = os.getenv("CHARMLAND_API_KEY", "")
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8100"))

ALL_SYMBOLS = WATCHLIST["core"] + WATCHLIST["observe"]


def require_keys():
    missing = [
        k
        for k, v in {
            "CHARMLAND_API_KEY": CHARMLAND_API_KEY,
            "ALPACA_API_KEY": ALPACA_API_KEY,
            "ALPACA_SECRET_KEY": ALPACA_SECRET_KEY,
        }.items()
        if not v
    ]
    if missing:
        raise RuntimeError(f"缺少环境变量: {', '.join(missing)}，请检查 .env")
