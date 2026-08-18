#!/usr/bin/env bash
set -euo pipefail

# ChatCodex Gateway 一键启动(零参数,全部配置在 Web 管理面板)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

if [[ ! -d "$BACKEND_DIR" ]]; then
    echo "[ChatCodex] 未找到 backend 目录: $BACKEND_DIR" >&2
    exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "[ChatCodex] 未找到 uv，请先安装 uv: https://docs.astral.sh/uv/" >&2
    exit 1
fi

cd "$BACKEND_DIR"
CHATCODEX_DISPLAY_PORT="$(uv run --locked python -c 'from app.config import Settings; print(Settings().port)')"
echo "[ChatCodex] 启动中... 首次会分别生成 Web/MCP Access Token 并打印"
echo "[ChatCodex] 管理面板: http://127.0.0.1:${CHATCODEX_DISPLAY_PORT}/"
exec uv run --locked python -m app.main
