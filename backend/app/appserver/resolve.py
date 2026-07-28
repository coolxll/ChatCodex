"""codex 可执行解析(共享)。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

CLIENT_NAME = "chatcodex-gateway"
CLIENT_VERSION = "0.1.0"


def resolve_codex_executable(preferred: str) -> list[str]:
    """解析 codex 可执行,返回 argv 前缀。

    Windows 上 npm 的 `codex` 是 .cmd(node shim),create_subprocess_exec 无法直接跑;
    优先使用 vendor 里的 Rust 原生 codex.exe,找不到再退 .cmd(经 cmd /c)。
    """
    is_path = os.path.isabs(preferred) or os.sep in preferred
    found = preferred if is_path and os.path.isfile(preferred) else shutil.which(preferred)
    if found and not found.lower().endswith((".cmd", ".bat", ".ps1")):
        return [found]
    npm = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@openai" / "codex"
    if sys.platform == "win32":
        native = (npm / "node_modules" / "@openai" / "codex-win32-x64" / "vendor"
                  / "x86_64-pc-windows-msvc" / "codex" / "codex.exe")
        if native.exists():
            return [str(native)]
    if found:
        return ["cmd", "/c", found]
    return [preferred]
