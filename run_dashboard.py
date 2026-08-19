#!/usr/bin/env python3
"""启动 Web 仪表盘。用法：python run_dashboard.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from stock_ai.config import DASHBOARD_PORT

if __name__ == "__main__":
    uvicorn.run("dashboard.app:app", host="0.0.0.0", port=DASHBOARD_PORT, reload=False)
