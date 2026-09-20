"""SQLite 持久化：启动时自动建目录，并按显式版本迁移初始化。

迁移机制：
- 用 ``PRAGMA user_version`` 记录数据库结构版本（0 表示早于版本化机制的库）；
- ``MIGRATIONS`` 按版本顺序登记每个版本的迁移，每个迁移本身幂等，可安全重放；
- 初始化在**单个写事务**内依次执行 ``current_version + 1 .. SCHEMA_VERSION``，
  每完成一个迁移即把 ``user_version`` 推进到对应版本；
- 空库（user_version = 0）一次性顺序执行全部迁移直接到最新版；
- 旧库保留既有数据逐级升级；数据库版本高于程序支持的 ``SCHEMA_VERSION``
  时抛出 :class:`DatabaseVersionTooNew`，拒绝启动。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

from .config import settings

logger = logging.getLogger(__name__)

# 当前程序支持的数据库结构版本。新增迁移时在此 +1，并在 MIGRATIONS 末尾追加。
SCHEMA_VERSION = 3


class DatabaseVersionTooNew(RuntimeError):
    """数据库结构版本高于当前程序支持的版本（拒绝启动）。"""


# ---------------------------------------------------------------------------
# 各版本迁移（按版本顺序执行；函数内全部 DDL 幂等，可安全重放）
# ---------------------------------------------------------------------------


# v1 建表/索引语句逐条执行（不用 executescript：它会隐式提交当前事务，
# 无法纳入 init_db 的单个写事务）；全部 IF NOT EXISTS，可安全重放。
_MIGRATION_1_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS batches (
        batch_id     TEXT PRIMARY KEY,
        submitted_at TEXT NOT NULL,
        total_count  INTEGER NOT NULL,
        unique_count INTEGER NOT NULL,
        invalid_count INTEGER NOT NULL,
        tree_count   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        message_id   TEXT PRIMARY KEY,
        in_reply_to  TEXT,
        "references" TEXT NOT NULL DEFAULT '[]',
        sent_at      TEXT NOT NULL,
        subject      TEXT NOT NULL,
        parent_id    TEXT,
        is_invalid   INTEGER NOT NULL DEFAULT 0,
        batch_id     TEXT NOT NULL,
        submitted_seq INTEGER NOT NULL,
        archived_at  TEXT NOT NULL,
        FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_batch ON messages(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_sent ON messages(sent_at)",
    """
    CREATE TABLE IF NOT EXISTS batch_duplicates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0,
        first_submitted_seq INTEGER,
        occurrences TEXT NOT NULL,
        FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_missing_refs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        missing TEXT NOT NULL,
        FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        members TEXT NOT NULL,
        FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
    )
    """,
    # POST /batches 幂等登记表：以 Idempotency-Key 为主键，原子登记首次提交的
    # 请求体摘要（request_digest）与归档批次。相同键重放时按摘要区分：摘要
    # 一致则原样重放首次响应（不再归档），不一致则由服务层拒绝（409）。
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        key            TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        batch_id       TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_results (
        batch_id TEXT PRIMARY KEY,
        result_json TEXT NOT NULL,
        FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
    )
    """,
    # 保留期冻结：每次冻结以某封“有效邮件”为锚点，活跃保护其当前所在的
    # 整棵会话。不存成员快照——保护集合在清理/查询时按当前父子关系实时
    # 展开锚点所在会话计算，此后并入该会话的邮件自动受保护；解除冻结
    # 删除锚点行即可。
    """
    CREATE TABLE IF NOT EXISTS retention_freezes (
        anchor_id    TEXT PRIMARY KEY,
        frozen_at    TEXT NOT NULL
    )
    """,
)


def _migrate_1(conn: sqlite3.Connection) -> None:
    """v1：基础建表与基础索引（对应最早一版服务结构）。

    此时 messages 尚无 retain_until 列，idx_messages_retain 也未创建
    （两者在 v2 引入）。
    """

    for statement in _MIGRATION_1_STATEMENTS:
        conn.execute(statement)


def _migrate_2(conn: sqlite3.Connection) -> None:
    """v2：messages 增加可选 retain_until 列及其清理用索引。"""

    columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if "retain_until" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN retain_until TEXT")
    # 列就绪后再建索引（新库随建表、旧库随补列，均幂等）
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_retain ON messages(retain_until)"
    )


def _migrate_3(conn: sqlite3.Connection) -> None:
    """v3：活跃冻结取代成员快照，删除已废弃的 retention_freeze_members 表。

    冻结锚点 retention_freezes 保留；保护集合自此按当前父子关系实时展开。
    """

    conn.execute("DROP TABLE IF EXISTS retention_freeze_members")


# 迁移注册表：索引 i 对应版本 i + 1 的迁移函数。
MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migrate_1,
    _migrate_2,
    _migrate_3,
)


_write_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _version_too_new_message(found: int, path: Path) -> str:
    return (
        f"database schema version is {found}, but this program only supports "
        f"up to version {SCHEMA_VERSION} (database: {path}); upgrade the program "
        f"or restore a backup taken with an older schema version"
    )


def init_db(db_path: Optional[Path] = None) -> None:
    """确保数据目录存在，并把数据库结构迁移到 :data:`SCHEMA_VERSION`。

    - 空库 / 早期无版本信息库（``PRAGMA user_version`` 为 0）：顺序执行全部
      迁移，直接到最新版；
    - 旧库：从当前版本的下一个版本起逐级升级，既有数据原样保留；
    - 全部待执行迁移与 ``user_version`` 更新在**单个写事务**内提交，
      任一迁移失败则整体回滚；
    - 数据库版本高于程序支持版本时抛出 :class:`DatabaseVersionTooNew`，
      不做任何写入。
    """

    path = db_path or settings.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        # WAL 是数据库级持久设置，需在事务外设置（会隐式提交）。
        conn.execute("PRAGMA journal_mode=WAL")

        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version > SCHEMA_VERSION:
            raise DatabaseVersionTooNew(
                _version_too_new_message(current_version, path)
            )
        if current_version == SCHEMA_VERSION:
            return

        with _write_lock:
            # 取得写锁后重读版本，避免与并发初始化互相覆盖。
            current_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if current_version > SCHEMA_VERSION:
                # 并发的另一进程可能在此期间写入了更高版本。
                raise DatabaseVersionTooNew(
                    _version_too_new_message(current_version, path)
                )
            try:
                conn.execute("BEGIN IMMEDIATE")
                for version in range(current_version + 1, SCHEMA_VERSION + 1):
                    MIGRATIONS[version - 1](conn)
                    # 参数绑定不适用于 PRAGMA，版本号为内部整数常量，可安全内插。
                    conn.execute(f"PRAGMA user_version = {version}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        logger.info(
            "migrated database %s from version %s to %s",
            path,
            current_version,
            SCHEMA_VERSION,
        )
    finally:
        conn.close()


def write_lock() -> threading.Lock:
    """批量写操作的进程内互斥锁。"""

    return _write_lock
