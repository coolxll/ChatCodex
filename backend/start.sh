#!/usr/bin/env bash
# ChatCodex Gateway 一键启动(零参数,全部配置在 Web 管理面板)
cd "$(dirname "$0")"
CHATCODEX_DISPLAY_PORT=$(python -c 'from app.config import Settings; print(Settings().port)')
echo "[ChatCodex] 启动中... 首次会分别生成 Web/MCP Access Token 并打印"
echo "[ChatCodex] 管理面板: http://127.0.0.1:${CHATCODEX_DISPLAY_PORT}/"
exec python -m app.main
