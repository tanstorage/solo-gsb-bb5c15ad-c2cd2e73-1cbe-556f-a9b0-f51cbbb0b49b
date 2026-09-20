"""显式版本迁移（PRAGMA user_version）的测试。"""

from __future__ import annotations

import sqlite3

import pytest


def _user_version(db) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _table_names(db) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def test_empty_database_migrates_straight_to_latest(tmp_path):
    from app import database

    db = tmp_path / "empty.db"
    database.init_db(db_path=db)

    assert _user_version(db) == database.SCHEMA_VERSION

    conn = sqlite3.connect(db)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "batches",
            "messages",
            "batch_duplicates",
            "batch_missing_refs",
            "batch_cycles",
            "idempotency_keys",
            "batch_results",
            "retention_freezes",
        } <= tables
        columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        assert "retain_until" in columns
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {
            "idx_messages_batch",
            "idx_messages_parent",
            "idx_messages_sent",
            "idx_messages_retain",
        } <= indexes
        assert "retention_freeze_members" not in tables
    finally:
        conn.close()


def test_init_db_is_idempotent_on_latest_database(tmp_path):
    from app import database

    db = tmp_path / "empty.db"
    database.init_db(db_path=db)
    tables_before = _table_names(db)

    # 已是最新版：再次初始化不报错、不改动结构
    database.init_db(db_path=db)

    assert _user_version(db) == database.SCHEMA_VERSION
    assert _table_names(db) == tables_before


def test_legacy_v0_database_upgrades_stepwise_and_keeps_data(tmp_path):
    # 早于版本化机制的库：messages 无 retain_until 列，
    # 冻结成员快照存于 retention_freeze_members；user_version = 0。
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE messages (
            message_id   TEXT PRIMARY KEY,
            in_reply_to  TEXT,
            "references" TEXT NOT NULL DEFAULT '[]',
            sent_at      TEXT NOT NULL,
            subject      TEXT NOT NULL,
            parent_id    TEXT,
            is_invalid   INTEGER NOT NULL DEFAULT 0,
            batch_id     TEXT NOT NULL,
            submitted_seq INTEGER NOT NULL,
            archived_at  TEXT NOT NULL
        );
        CREATE TABLE retention_freezes (
            anchor_id TEXT PRIMARY KEY,
            frozen_at TEXT NOT NULL
        );
        CREATE TABLE retention_freeze_members (
            anchor_id  TEXT NOT NULL,
            message_id TEXT NOT NULL,
            PRIMARY KEY (anchor_id, message_id),
            FOREIGN KEY (anchor_id)
                REFERENCES retention_freezes(anchor_id) ON DELETE CASCADE
        );
        INSERT INTO messages (message_id, "references", sent_at, subject,
                              batch_id, submitted_seq, archived_at)
            VALUES ('A', '[]', '2026-09-19T00:00:00+00:00', 's', 'b1', 1,
                    '2026-09-19T00:00:00+00:00');
        INSERT INTO retention_freezes
            VALUES ('A', '2026-09-19T00:00:00+00:00');
        INSERT INTO retention_freeze_members VALUES ('A', 'A');
        """
    )
    conn.commit()
    conn.close()

    from app import database

    database.init_db(db_path=db)
    database.init_db(db_path=db)  # 幂等：重复执行不报错、结果不变

    assert _user_version(db) == database.SCHEMA_VERSION
    tables = _table_names(db)
    assert "retention_freeze_members" not in tables
    assert "retention_freezes" in tables

    conn = sqlite3.connect(db)
    try:
        # 既有数据原样保留
        row = conn.execute(
            "SELECT message_id, subject, retain_until FROM messages"
        ).fetchone()
        assert row == ("A", "s", None)
        assert conn.execute(
            "SELECT anchor_id, frozen_at FROM retention_freezes"
        ).fetchall() == [("A", "2026-09-19T00:00:00+00:00")]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_messages_retain'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_v1_database_upgrades_through_v2_and_v3(tmp_path, monkeypatch):
    # 停在 v1 的库：无 retain_until 列、无其索引、保留废弃快照表
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE messages (
            message_id   TEXT PRIMARY KEY,
            in_reply_to  TEXT,
            "references" TEXT NOT NULL DEFAULT '[]',
            sent_at      TEXT NOT NULL,
            subject      TEXT NOT NULL,
            parent_id    TEXT,
            is_invalid   INTEGER NOT NULL DEFAULT 0,
            batch_id     TEXT NOT NULL,
            submitted_seq INTEGER NOT NULL,
            archived_at  TEXT NOT NULL
        );
        CREATE TABLE retention_freeze_members (anchor_id TEXT, message_id TEXT);
        INSERT INTO messages (message_id, "references", sent_at, subject,
                              batch_id, submitted_seq, archived_at)
            VALUES ('M', '[]', '2026-09-19T00:00:00+00:00', 's', 'b', 1,
                    '2026-09-19T00:00:00+00:00');
        """
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    from app import database

    # 只升到 v2：retain_until 与索引出现，快照表仍在
    monkeypatch.setattr(database, "SCHEMA_VERSION", 2)
    database.init_db(db_path=db)
    assert _user_version(db) == 2

    conn = sqlite3.connect(db)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        assert "retain_until" in columns
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_messages_retain'"
        ).fetchone() is not None
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "retention_freeze_members" in tables  # v3 尚未执行
        assert conn.execute("SELECT message_id FROM messages").fetchall() == [
            ("M",)
        ]
    finally:
        conn.close()

    # 再升到 v3：快照表删除，数据保留
    monkeypatch.setattr(database, "SCHEMA_VERSION", 3)
    database.init_db(db_path=db)
    assert _user_version(db) == 3
    assert "retention_freeze_members" not in _table_names(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT message_id FROM messages").fetchall() == [
            ("M",)
        ]
    finally:
        conn.close()


def test_newer_than_supported_version_refuses_and_writes_nothing(tmp_path):
    from app import database

    db = tmp_path / "future.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()

    with pytest.raises(database.DatabaseVersionTooNew) as excinfo:
        database.init_db(db_path=db)

    message = str(excinfo.value)
    assert "999" in message
    assert str(database.SCHEMA_VERSION) in message
    # 版本号未被改写，也没有建立任何业务表
    assert _user_version(db) == 999
    assert _table_names(db) == set()


def test_failed_migration_rolls_back_entire_transaction(tmp_path, monkeypatch):
    from app import database

    # 构造停在 v1 的库
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE messages (
            message_id   TEXT PRIMARY KEY,
            in_reply_to  TEXT,
            "references" TEXT NOT NULL DEFAULT '[]',
            sent_at      TEXT NOT NULL,
            subject      TEXT NOT NULL,
            parent_id    TEXT,
            is_invalid   INTEGER NOT NULL DEFAULT 0,
            batch_id     TEXT NOT NULL,
            submitted_seq INTEGER NOT NULL,
            archived_at  TEXT NOT NULL
        )
        """
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    real_migrate_2 = database._migrate_2

    def failing_migrate_2(conn):
        real_migrate_2(conn)  # 已补列、建索引，但随后失败
        raise RuntimeError("boom")

    monkeypatch.setattr(
        database,
        "MIGRATIONS",
        (database._migrate_1, failing_migrate_2, database._migrate_3),
    )

    with pytest.raises(RuntimeError, match="boom"):
        database.init_db(db_path=db)

    # 整个写事务回滚：版本仍停在 v1，v2 的补列也不落库
    assert _user_version(db) == 1
    conn = sqlite3.connect(db)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        assert "retain_until" not in columns
    finally:
        conn.close()


def test_app_refuses_to_start_with_newer_database(tmp_path, monkeypatch):
    import importlib

    db = tmp_path / "future.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DATABASE_PATH", str(db))

    from app import config, database, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)

    from fastapi.testclient import TestClient

    from app import main

    importlib.reload(main)

    expected = database.DatabaseVersionTooNew
    with pytest.raises(expected):
        with TestClient(main.app):
            pytest.fail("app must not start when schema version is too new")
