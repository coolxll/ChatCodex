@echo off
setlocal

REM ChatCodex Gateway 一键启动(零参数,全部配置在 Web 管理面板)
cd /d "%~dp0backend"
if errorlevel 1 (
    echo [ChatCodex] 无法进入 backend 目录。
    exit /b 1
)

where uv >nul 2>&1
if errorlevel 1 (
    echo [ChatCodex] 未找到 uv，请先安装 uv: https://docs.astral.sh/uv/
    exit /b 1
)

for /f "delims=" %%p in ('uv run --locked python -c "from app.config import Settings; print(Settings().port)"') do set "CHATCODEX_DISPLAY_PORT=%%p"
if not defined CHATCODEX_DISPLAY_PORT (
    echo [ChatCodex] 无法读取网关端口。
    exit /b 1
)

echo [ChatCodex] 启动中... 首次会分别生成 Web/MCP Access Token 并打印
echo [ChatCodex] 管理面板: http://127.0.0.1:%CHATCODEX_DISPLAY_PORT%/
echo.
uv run --locked python -m app.main
set "CHATCODEX_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %CHATCODEX_EXIT_CODE%
