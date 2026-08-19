#!/usr/bin/env bash
# 安装 systemd 服务：决策轮（每小时）+ 异动检查（每10分钟）+ 日报（收盘后）+ 仪表盘常驻
# 会自动把项目当前绝对路径写入 unit 文件。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
USER_NAME="$(whoami)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

# --- 决策轮：美东时间 9:30-16:00 每小时（程序内部会再校验是否开盘）---
cat > "$UNIT_DIR/stock-ai-cycle.service" <<EOF
[Unit]
Description=Stock AI decision cycle
[Service]
Type=oneshot
WorkingDirectory=$ROOT
ExecStart=$PY $ROOT/run_cycle.py
EOF
cat > "$UNIT_DIR/stock-ai-cycle.timer" <<EOF
[Unit]
Description=Run stock AI cycle hourly
[Timer]
OnCalendar=Mon..Fri *-*-* 09..16:30:00 America/New_York
Persistent=true
[Install]
WantedBy=timers.target
EOF

# --- 异动检查：每 10 分钟 ---
cat > "$UNIT_DIR/stock-ai-spike.service" <<EOF
[Unit]
Description=Stock AI spike check
[Service]
Type=oneshot
WorkingDirectory=$ROOT
ExecStart=$PY $ROOT/run_cycle.py --spike
EOF
cat > "$UNIT_DIR/stock-ai-spike.timer" <<EOF
[Unit]
Description=Run spike check every 10 min
[Timer]
OnCalendar=Mon..Fri *-*-* 09..16:*:00 America/New_York
Persistent=true
[Install]
WantedBy=timers.target
EOF

# --- 收盘日报：美东 16:10 ---
cat > "$UNIT_DIR/stock-ai-report.service" <<EOF
[Unit]
Description=Stock AI daily report
[Service]
Type=oneshot
WorkingDirectory=$ROOT
ExecStart=$PY $ROOT/run_cycle.py --report
EOF
cat > "$UNIT_DIR/stock-ai-report.timer" <<EOF
[Unit]
Description=Daily report after market close
[Timer]
OnCalendar=Mon..Fri *-*-* 16:10:00 America/New_York
Persistent=true
[Install]
WantedBy=timers.target
EOF

# --- 仪表盘常驻 ---
cat > "$UNIT_DIR/stock-ai-dashboard.service" <<EOF
[Unit]
Description=Stock AI dashboard
After=network.target
[Service]
WorkingDirectory=$ROOT
ExecStart=$PY $ROOT/run_dashboard.py
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now stock-ai-cycle.timer stock-ai-spike.timer stock-ai-report.timer stock-ai-dashboard.service

# 用户注销后服务继续运行（需要 linger）
if command -v loginctl >/dev/null; then
  loginctl enable-linger "$USER_NAME" 2>/dev/null || echo "提示: 无法自动 enable-linger，注销后服务可能停止。可执行: sudo loginctl enable-linger $USER_NAME"
fi

echo "✅ systemd 服务已安装并启动："
echo "   systemctl --user list-timers | grep stock-ai"
echo "   仪表盘: http://localhost:${DASHBOARD_PORT:-8100}"
