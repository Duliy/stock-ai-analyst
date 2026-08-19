#!/usr/bin/env bash
# 一键安装：创建 venv + 安装依赖。在目标机（Ubuntu/Debian 系 Linux）上运行。
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null; then
  echo "错误: 未找到 python3，请先安装（Ubuntu: sudo apt install python3 python3-venv）"
  exit 1
fi

python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已生成 .env 模板，请编辑填入 API 密钥: nano .env"
fi

echo "✅ 安装完成。下一步：编辑 .env 后运行 scripts/install_systemd.sh"
