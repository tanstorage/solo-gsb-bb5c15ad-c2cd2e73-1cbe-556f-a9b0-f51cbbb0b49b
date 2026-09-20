"""PRAGMA user_version 显式版本迁移测试。"""

from __future__ import annotations

import importlib
import sqlite3

import pytest


def _load_database(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    from app import config, database

    importlib.reload(config)
    importlib.reload(database)
    return database


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _v1_schema_script(database) -> str:
    return ";\n".join(database._V1_STATEMENTS)


def test_empty_database_bootstraps_directly_to_latest(tmp_path, monkeypatch):
    database = _load_database(monkeypatch, tmp_path)
    db = tmp_path / "fresh.db"

    database.init_db(db_path=db)

    conn = sqlite3.connect(db)
    try:
        assert _user_version(conn) == database.LATEST_VERSION
        assert "retain_until" in _columns(conn, "messages")
        names = _table_names(conn)
        assert "retention_freeze_members" not in names
        assert {
            "batches",
            "messages",
            "batch_duplicates",
            "batch_missing_refs",
            "batch_cycles",
            "idempotency_keys",
            "batch_results",
            "retention_freezes",
        } <= names
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert {
            "idx_messages_batch",
            "idx_messages_parent",
            "idx_messages_sent",
            "idx_messages_retain",
        } <= indexes
    finally:
        conn.close()


def test_already_latest_is_idempotent_and_noop(tmp_path, monkeypatch):
    database = _load_database(monkeypatch, tmp_path)
    db = tmp_path / "fresh.db"
    database.init_db(db_path=db)

    # 再跑两次：不报错，版本与结构不变
    database.init_db(db_path=db)
    database.init_db(db_path=db)

    conn = sqlite3.connect(db)
    try:
        assert _user_version(conn) == database.LATEST_VERSION
    finally:
        conn.close()


def test_legacy_unversioned_database_is_baseline_upgraded(tmp_path, monkeypatch):
    # 早于版本化迁移发布的程序创建的库：无 user_version 记录，
    # messages 无 retain_until 列，且含废弃快照表。
    database = _load_database(monkeypatch, tmp_path)
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
            PRIMARY KEY (anchor_id, message_id)
        );
        """
    )
    conn.execute(
        "INSERT INTO retention_freezes VALUES ('A', '2026-09-19T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO messages VALUES "
        "('m1', NULL, '[]', 't', 's', NULL, 0, 'b', 1, 'a')"
    )
    conn.commit()
    conn.close()
    assert _user_version(sqlite3.connect(db)) == 0

    database.init_db(db_path=db)

    conn = sqlite3.connect(db)
    try:
        assert _user_version(conn) == database.LATEST_VERSION
        names = _table_names(conn)
        assert "retention_freeze_members" not in names
        # 既有数据原样保留
        assert conn.execute(
            "SELECT anchor_id FROM retention_freezes"
        ).fetchall() == [("A",)]
        assert conn.execute(
            "SELECT message_id, retain_until FROM messages"
        ).fetchall() == [("m1", None)]
        assert "retain_until" in _columns(conn, "messages")
    finally:
        conn.close()


def test_stamped_v1_database_upgrades_stepwise_and_keeps_data(
    tmp_path, monkeypatch
):
    database = _load_database(monkeypatch, tmp_path)
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db)
    conn.executescript(_v1_schema_script(database))
    conn.execute(
        "INSERT INTO batches VALUES "
        "('b1', '2026-09-18T00:00:00+00:00', 1, 1, 0, 1)"
    )
    conn.execute(
        "INSERT INTO messages VALUES "
        "('m1', NULL, '[]', '2026-09-18T00:00:00+00:00', "
        "'subject', NULL, 0, 'b1', 1, '2026-09-18T00:00:00+00:00')"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    database.init_db(db_path=db)

    conn = sqlite3.connect(db)
    try:
        assert _user_version(conn) == database.LATEST_VERSION
        # 旧数据保留
        assert conn.execute(
            "SELECT total_count FROM batches"
        ).fetchall() == [(1,)]
        row = conn.execute(
            "SELECT subject, retain_until FROM messages"
        ).fetchone()
        assert row == ("subject", None)
        assert "retain_until" in _columns(conn, "messages")
        assert "retention_freeze_members" not in _table_names(conn)
    finally:
        conn.close()


def test_stamped_v2_database_only_runs_remaining_migrations(
    tmp_path, monkeypatch
):
    database = _load_database(monkeypatch, tmp_path)
    db = tmp_path / "v2.db"
    conn = sqlite3.connect(db)
    conn.executescript(_v1_schema_script(database))
    conn.execute("ALTER TABLE messages ADD COLUMN retain_until TEXT")
    conn.execute(
        "CREATE INDEX idx_messages_retain ON messages(retain_until)"
    )
    conn.execute(
        "CREATE TABLE retention_freeze_members (anchor_id TEXT, message_id TEXT)"
    )
    conn.execute("INSERT INTO retention_freeze_members VALUES ('a', 'm')")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    database.init_db(db_path=db)

    conn = sqlite3.connect(db)
    try:
        assert _user_version(conn) == 3
        assert "retention_freeze_members" not in _table_names(conn)
    finally:
        conn.close()


def test_future_version_is_rejected_and_left_untouched(tmp_path, monkeypatch):
    database = _load_database(monkeypatch, tmp_path)
    db = tmp_path / "future.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.execute("CREATE TABLE future_only_table(a)")
    conn.commit()
    conn.close()

    with pytest.raises(database.DatabaseVersionTooNew) as excinfo:
        database.init_db(db_path=db)

    assert excinfo.value.db_version == 99
    assert excinfo.value.supported_version == database.LATEST_VERSION
    message = str(excinfo.value)
    assert "99" in message and "refusing to start" in message

    # 拒绝启动时不改动数据库
    conn = sqlite3.connect(db)
    try:
        assert _user_version(conn) == 99
        assert "future_only_table" in _table_names(conn)
    finally:
        conn.close()


def test_migration_failure_rolls_back_entire_transaction(tmp_path, monkeypatch):
    # 单个写事务：迁移中途失败 -> 版本号与结构整体回滚，
    # 不会停在“迁移了一半”的中间版本。
    database = _load_database(monkeypatch, tmp_path)
    db = tmp_path / "half.db"
    conn = sqlite3.connect(db)
    conn.executescript(_v1_schema_script(database))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    def failing_v3(conn):
        conn.execute("CREATE TABLE migration_halfway_marker(a)")
        raise RuntimeError("simulated migration failure")

    database.MIGRATIONS[3] = failing_v3
    try:
        with pytest.raises(RuntimeError, match="simulated migration failure"):
            database.init_db(db_path=db)
    finally:
        del database.MIGRATIONS[3]

    conn = sqlite3.connect(db)
    try:
        # 同一事务里的 v2 补列/建索引也一并回滚
        assert _user_version(conn) == 1
        assert "retain_until" not in _columns(conn, "messages")
        assert "migration_halfway_marker" not in _table_names(conn)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'idx_messages_retain'"
        ).fetchall() == []
    finally:
        conn.close()

    # 修复后重试可正常升级
    database.init_db(db_path=db)
    conn = sqlite3.connect(db)
    try:
        assert _user_version(conn) == database.LATEST_VERSION
    finally:
        conn.close()


def test_app_startup_refuses_future_version_database(tmp_path, monkeypatch):
    # 端到端：lifespan 启动时 init_db 抛错，应用无法启动（而非带病服务）。
    db = tmp_path / "future.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 4242")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DATABASE_PATH", str(db))
    from app import config, database, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    from app import main

    importlib.reload(main)

    from fastapi.testclient import TestClient

    with pytest.raises(database.DatabaseVersionTooNew):
        with TestClient(main.app):
            pytest.fail("application must not start against a newer schema")
