#!/usr/bin/env python3
"""每小时决策轮入口（由 systemd timer / cron 调用）。

用法：
  python run_cycle.py           # 常规决策轮
  python run_cycle.py --spike   # 持仓异动检查（可更频繁调用，内部自行判断是否需要加轮）
  python run_cycle.py --report  # 生成收盘日报
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_ai import pipeline
from stock_ai.config import require_keys

if __name__ == "__main__":
    require_keys()
    if "--spike" in sys.argv:
        pipeline.run_spike_check()
    elif "--report" in sys.argv:
        path = pipeline.daily_report()
        print(f"日报已生成: {path}")
    else:
        pipeline.run_cycle()
