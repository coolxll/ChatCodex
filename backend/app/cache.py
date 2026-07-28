"""性能: TTL 缓存(避免重复的 app-server 往返)。"""
from __future__ import annotations

import time
from typing import Any, Optional


class TTLCache:
    def __init__(self, ttl: float = 30.0):
        self.ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        e = self._store.get(key)
        if not e:
            return None
        exp, val = e
        if exp < time.time():
            self._store.pop(key, None)
            return None
        return val

    def set(self, key: str, val: Any, ttl: Optional[float] = None) -> None:
        self._store[key] = (time.time() + (ttl or self.ttl), val)

    def invalidate(self, prefix: str = "") -> None:
        if not prefix:
            self._store.clear()
            return
        for k in [k for k in self._store if k.startswith(prefix)]:
            self._store.pop(k, None)
