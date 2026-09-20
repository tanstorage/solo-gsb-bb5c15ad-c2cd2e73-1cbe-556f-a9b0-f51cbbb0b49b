"""SQLite 持久化：启动时自动建目录，并按 PRAGMA user_version 执行显式版本迁移。

迁移按版本顺序（1 -> LATEST_VERSION）执行，每个版本的步骤自身幂等；
全部待执行迁移在**单个写事务**内完成，并逐级把 ``PRAGMA user_version``
推进到该版本，因此：

- 空库：从版本 0 起依次执行全部迁移，直接到最新版（建表即最终结构）；
- 旧库：从其记录的版本起逐级升级，既有数据原样保留；
- 重复启动：已是最新版时不开启任何写事务；
- 版本过高：数据库版本高于程序支持的 LATEST_VERSION 时拒绝启动。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

from .config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 版本化迁移
# ---------------------------------------------------------------------------
#
# 版本历史：
#   v1  初始建表：batches / messages（此时无 retain_until 列）/
#       batch_duplicates / batch_missing_refs / batch_cycles /
#       idempotency_keys / batch_results / retention_freezes，
#       及 messages 的 batch_id / parent_id / sent_at 三个索引；
#   v2  messages 增加 retain_until 列并建索引 idx_messages_retain；
#   v3  删除已废弃的冻结成员快照表 retention_freeze_members
#       （改为按当前父子关系实时计算保护集合的活跃冻结）。
#
# 每个迁移只允许写增量结构；新版本一律追加在末尾，禁止改动已发布的迁移。
# 各迁移步骤均保持幂等（IF [NOT] EXISTS / 先查列再补列），以便历史上
# 未记录 user_version 的数据库（version 0）也能安全地补跑到最新版。

# v1 的每条 DDL 单独列出：迁移在单个显式写事务内逐条 execute，
# 不能使用 executescript（它会先隐式 COMMIT，破坏事务原子性）。
_V1_STATEMENTS: tuple[str, ...] = (
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


def _migrate_v1(conn: sqlite3.Connection) -> None:
    for statement in _V1_STATEMENTS:
        conn.execute(statement)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    # 早期版本 messages 无 retain_until 列 -> 补列（幂等）。
    columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if "retain_until" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN retain_until TEXT")
    # 列就绪后再建索引（新库随建表、旧库随补列，均幂等）。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_retain ON messages(retain_until)"
    )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    # 活跃冻结迁移：废弃快照成员表（重复执行为 no-op；锚点表 retention_freezes
    # 保留，保护范围自升级起随当前会话拓扑实时计算）。
    conn.execute("DROP TABLE IF EXISTS retention_freeze_members")


# 版本号 -> 迁移步骤；按版本顺序执行。新增迁移时把 LATEST_VERSION 一并推进。
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_v1,
    2: _migrate_v2,
    3: _migrate_v3,
}

LATEST_VERSION: int = max(MIGRATIONS)


class DatabaseVersionTooNew(RuntimeError):
    """数据库 user_version 高于本程序支持的最高版本。"""

    def __init__(self, db_version: int, supported_version: int, path: Path) -> None:
        self.db_version = db_version
        self.supported_version = supported_version
        self.path = path
        super().__init__(
            f"database schema version {db_version} is newer than this program "
            f"supports (version {supported_version}); refusing to start to "
            f"protect its data. Please upgrade the application. database: {path}"
        )


_write_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _is_empty_database(conn: sqlite3.Connection) -> bool:
    """是否为完全空白的库（无任何用户表、索引等对象）。"""

    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return int(row[0]) == 0


def init_db(db_path: Optional[Path] = None) -> None:
    """确保数据目录存在，并把数据库迁移到 LATEST_VERSION（幂等）。

    - 空库：在单个写事务内依次执行 v1..最新 全部迁移，直接到最新版；
    - 旧库（含未记录版本、由更早版本程序创建的 version 0 库）：保留既有
      数据，自当前版本起逐级升级；
    - 已是最新版：不开启写事务，直接返回；
    - user_version 高于 LATEST_VERSION：抛 DatabaseVersionTooNew，拒绝启动。

    所有迁移在单个写事务内提交，任一步失败则整体回滚，不会留下
    “迁移到一半”的结构或版本号。
    """

    path = db_path or settings.database_path
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    try:
        # WAL 是日志模式设置，必须在事务外执行（与历史行为保持一致）。
        conn.execute("PRAGMA journal_mode=WAL")

        version = _user_version(conn)
        if version > LATEST_VERSION:
            raise DatabaseVersionTooNew(version, LATEST_VERSION, path)
        if version == LATEST_VERSION:
            return

        if version == 0:
            if _is_empty_database(conn):
                logger.info(
                    "creating new database at schema version %d (%s)",
                    LATEST_VERSION,
                    path,
                )
            else:
                logger.info(
                    "legacy database without schema version detected (%s); "
                    "migrating %d -> %d",
                    path,
                    version,
                    LATEST_VERSION,
                )

        # 全部待执行迁移放入单个写事务：逐级执行并推进 user_version，
        # 任一失败整体回滚。
        conn.isolation_level = None  # 手动管理事务
        committed = False
        conn.execute("BEGIN IMMEDIATE")
        try:
            for target in range(version + 1, LATEST_VERSION + 1):
                MIGRATIONS[target](conn)
                # PRAGMA 不支持参数绑定；target 为程序内整数，安全。
                conn.execute(f"PRAGMA user_version = {target}")
                logger.info("migrated schema to version %d (%s)", target, path)
            conn.execute("COMMIT")
            committed = True
        finally:
            if not committed:
                # BEGIN 本身失败时并无活动事务（in_transaction 为 False），
                # 仅在确有活动事务时回滚。
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            conn.isolation_level = ""
    finally:
        conn.close()


def write_lock() -> threading.Lock:
    """批量写操作的进程内互斥锁。"""

    return _write_lock
