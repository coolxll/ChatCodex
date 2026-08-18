#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/backend"

CHATCODEX_DISPLAY_PORT=$(uv run --locked python -c 'from app.config import Settings; print(Settings().port)')
echo "[ChatCodex] 启动中... 首次启动会分别生成 Web/MCP Access Token 并打印"
echo "[ChatCodex] 管理面板: http://127.0.0.1:${CHATCODEX_DISPLAY_PORT}/"
exec uv run --locked python -m app.main
