#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "📦 创建虚拟环境..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -r requirements.txt -q
echo "🚀 启动加密货币仪表盘 http://0.0.0.0:8080"
uvicorn app:app --host 0.0.0.0 --port 8080
