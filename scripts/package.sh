#!/usr/bin/env bash
# 打包交付：生成不含密钥/数据/venv 的 tar.gz，可直接发给甲方。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(dirname "$ROOT")/stock-ai-$(date +%Y%m%d).tar.gz"

tar czf "$OUT" \
  --exclude='.venv' --exclude='data' --exclude='.env' \
  --exclude='__pycache__' --exclude='.git' --exclude='*.tar.gz' \
  -C "$(dirname "$ROOT")" "$(basename "$ROOT")"

echo "✅ 已打包: $OUT"
echo "   甲方解压后依次运行: scripts/install.sh → 编辑 .env → scripts/install_systemd.sh"
