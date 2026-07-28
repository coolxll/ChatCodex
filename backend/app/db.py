"""数据库层:stdlib sqlite3(默认)或 PostgreSQL(psycopg,可选)。

仅做两件事:建表 + 提供线程安全的连接。模型在 models.py。
用 DB-API 参数占位;sqlite 用 ?,postgres 用 %s —— 由驱动决定,这里统一 sqlite 起步。
"""
from __future__ import annotations

import sqlite3
import threading
import os
import warnings
from contextlib import contextmanager
from typing import Iterator

from .config import Settings, _default_database_path, _legacy_database_path
from .file_security import restrict_path_to_owner

SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_context (
  id                    TEXT PRIMARY KEY,
  map_key               TEXT UNIQUE NOT NULL,
  conversation_id       TEXT NOT NULL,
  user_id               TEXT NOT NULL,
  cwd                    TEXT NOT NULL,
  workspace_roots       TEXT NOT NULL,
  sandbox_mode          TEXT NOT NULL,
  permission_profile_id TEXT,
  approval_policy       TEXT NOT NULL,
  approvals_reviewer    TEXT NOT NULL DEFAULT 'user',
  work_mode             TEXT NOT NULL,
  platform              TEXT,
  appserver_instance_id TEXT,
  version               INTEGER NOT NULL DEFAULT 1,
  status                TEXT NOT NULL DEFAULT 'active',
  created_at            INTEGER NOT NULL,
  updated_at            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_context_user
  ON execution_context(user_id);
CREATE INDEX IF NOT EXISTS idx_execution_context_conversation
  ON execution_context(conversation_id);

CREATE TABLE IF NOT EXISTS approval_audit (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  operation_id    TEXT,
  source          TEXT NOT NULL,
  state           TEXT NOT NULL,
  kind            TEXT,
  request_id      TEXT,
  summary         TEXT,
  payload         TEXT,
  decision        TEXT,
  decided_by      TEXT,
  action_digest   TEXT,
  context_version INTEGER,
  request_version INTEGER,
  created_at      INTEGER,
  decided_at      INTEGER
);

CREATE TABLE IF NOT EXISTS kv_config (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


class Database:
    """sqlite 起步;database_url 为 postgresql:// 时抛提示(后续接 psycopg)。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._local = threading.local()
        url = settings.database_url
        if url.startswith("sqlite:///"):
            self.kind = "sqlite"
            self.path = os.path.abspath(os.path.expanduser(url[len("sqlite:///"):]))
        elif url.startswith("postgres"):
            # 预留:接 psycopg。当前显式报错,避免误以为已支持。
            raise NotImplementedError(
                "PostgreSQL 暂未接驱动;请先用 sqlite:/// 或安装 extras[postgres] 并扩展 db.py"
            )
        else:
            raise ValueError(f"unsupported database_url: {url}")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if os.path.normcase(self.path) == os.path.normcase(_default_database_path()):
            self._try_restrict(os.path.dirname(self.path), directory=True)
            self._migrate_legacy_default()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._restrict_sqlite_files()
        return conn

    def _restrict_sqlite_files(self) -> None:
        for candidate in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(candidate):
                self._try_restrict(candidate)

    @staticmethod
    def _try_restrict(path: str, *, directory: bool = False) -> None:
        try:
            restrict_path_to_owner(path, directory=directory)
        except OSError as exc:
            warnings.warn(
                f"could not restrict local ChatCodex state permissions for {path}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    def _migrate_legacy_default(self) -> None:
        """Copy the old checkout-local database once, then remove stale secrets."""
        legacy = _legacy_database_path()
        if os.path.exists(self.path) or not os.path.isfile(legacy):
            return
        source = sqlite3.connect(legacy)
        destination = sqlite3.connect(self.path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        for candidate in (legacy, legacy + "-wal", legacy + "-shm"):
            try:
                os.unlink(candidate)
            except FileNotFoundError:
                pass
            except OSError as exc:
                warnings.warn(
                    f"migrated ChatCodex state but could not remove {candidate}: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        """每线程一个连接,简单可靠(sqlite 写串行)。"""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise

    def close(self) -> None:
        """Close the connection owned by the current thread, if one exists."""
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def _init_schema(self) -> None:
        with self.conn() as c:
            c.executescript(SCHEMA)
            audit_columns = {
                row["name"] for row in c.execute("PRAGMA table_info(approval_audit)")
            }
            for name, definition in (
                ("conversation_id", "TEXT"),
                ("operation_id", "TEXT"),
                ("source", "TEXT"),
                ("state", "TEXT"),
                ("action_digest", "TEXT"),
                ("context_version", "INTEGER"),
                ("request_version", "INTEGER"),
            ):
                if name not in audit_columns:
                    c.execute(
                        f"ALTER TABLE approval_audit ADD COLUMN {name} {definition}"
                    )
            c.execute(
                """CREATE INDEX IF NOT EXISTS idx_audit_conversation
                   ON approval_audit(conversation_id)"""
            )
