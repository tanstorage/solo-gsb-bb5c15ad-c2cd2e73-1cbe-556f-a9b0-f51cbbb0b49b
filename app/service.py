"""业务编排：调用纯归并逻辑，落库 SQLite，组装查询结果。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import database, threading
from .rfc5322 import Rfc5322ParseError, parse_header_blocks
from .schemas import (
    BatchIn,
    CleanupIn,
    FreezeIn,
    MessageIn,
    RelinkIn,
    Rfc5322ImportIn,
    ThreadCorrectionIn,
    ThreadSlaEvaluateIn,
    ThreadSubjectAuditIn,
    UnfreezeIn,
)
from .threading import RetentionCleanupPlan, ThreadMessage


class Rfc5322ImportErrors(Exception):
    """POST /imports/rfc5322：输入头文本存在字段错误（-> 422，不写库）。"""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__(f"RFC 5322 import rejected with {len(errors)} field error(s)")


class IdempotencyConflict(Exception):
    """POST /batches：Idempotency-Key 已被不同请求体登记（-> 409，不写库）。"""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"idempotency key {key!r} was already used with a different payload")


class RelinkNotFound(Exception):
    """POST /relinks：请求中存在未归档的 message_id。"""

    def __init__(self, message_ids: list[str]) -> None:
        self.message_ids = message_ids
        super().__init__(f"messages not found: {message_ids}")


class RelinkCycleConflict(Exception):
    """POST /relinks：合并候选关系后出现环，整次拒绝、不写库。"""

    def __init__(self, cycles: list[list[str]]) -> None:
        self.cycles = cycles
        super().__init__(f"relink would create cycles: {cycles}")


class CursorMalformed(Exception):
    """GET /threads/{id}/page：游标格式无法解析或字段非法（-> 422）。"""


class CursorThreadMismatch(Exception):
    """游标根 ID 与路径所属会话不是同一会话（-> 422）。"""


class CursorStale(Exception):
    """游标 revision / 根归属与当前树不一致（树在补链中变化，-> 409）。"""


class RetentionAnchorNotFound(Exception):
    """冻结/解冻锚点邮件未归档（-> 404）。"""


class RetentionAnchorInvalid(Exception):
    """冻结锚点是引用环内 invalid 邮件，不属于任何会话（-> 409）。"""


class RetentionFreezeNotFound(Exception):
    """解除冻结：不存在以该邮件为锚点的冻结（-> 404）。"""


class CorrectionMessageNotFound(Exception):
    """POST /thread-corrections：待调整邮件未归档（-> 404）。"""


class CorrectionParentNotFound(Exception):
    """POST /thread-corrections：目标父节点未归档（-> 404）。"""

    def __init__(self, parent_id: str) -> None:
        self.parent_id = parent_id
        super().__init__(f"parent not found: {parent_id}")


class CorrectionInvalidMessage(Exception):
    """POST /thread-corrections：待调整邮件是引用环内 invalid 邮件（-> 409）。"""


class CorrectionInvalidParent(Exception):
    """POST /thread-corrections：目标父节点是引用环内 invalid 邮件（-> 409）。"""


class CorrectionRevisionConflict(Exception):
    """expected_revision 与当前会话拓扑不匹配（-> 409，不写库）。"""

    def __init__(self, current_revision: str) -> None:
        self.current_revision = current_revision
        super().__init__(f"revision conflict; current revision is {current_revision}")


class CorrectionCycleConflict(Exception):
    """调整会导致自挂或引用环（-> 409，不写库）。"""

    def __init__(self, cycles: list[list[str]]) -> None:
        self.cycles = cycles
        super().__init__(f"correction would create cycles: {cycles}")


# 游标载荷版本号；编码结构调整时可据此区分。
_CURSOR_VERSION = 1
# 合法的 base64url（无填充）字符集，先于解码拦截伪造字符。
_CURSOR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 请求体摘要载荷版本号；归一/摘要规则调整时可据此区分。
_REQUEST_DIGEST_VERSION = 1


def request_digest(normalized_messages: list[MessageIn]) -> str:
    """对归一后的请求体内容计算稳定摘要（hex sha256）。

    摘要基于按 schema 规范化后的 JSON：与请求 JSON 文本的排版、字段书写
    顺序无关；时间统一换算到 UTC（naive 视为 UTC），因此同一时刻的不同
    写法（``Z`` / ``+00:00`` / 等价偏移）摘要一致。
    """

    body = BatchIn(messages=list(normalized_messages))
    canonical = json.dumps(
        {"v": _REQUEST_DIGEST_VERSION, "body": body.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_to_message(row: sqlite3.Row) -> ThreadMessage:
    return ThreadMessage(
        message_id=row["message_id"],
        in_reply_to=row["in_reply_to"],
        references=tuple(json.loads(row["references"])),
        sent_at=row["sent_at"],
        subject=row["subject"],
    )


def _climb_root(conn: sqlite3.Connection, message_id: str) -> str:
    """沿 parent_id 向上爬到根（新邮件均为有效邮件，链不会断）。"""

    cur = message_id
    seen: set[str] = set()
    while True:
        row = conn.execute(
            "SELECT parent_id FROM messages WHERE message_id = ?", (cur,)
        ).fetchone()
        pid = row["parent_id"] if row else None
        if pid is None or pid in seen:
            return cur
        seen.add(cur)
        cur = pid


def _load_subtree(conn: sqlite3.Connection, root_id: str) -> tuple[dict[str, ThreadMessage], dict[str, Optional[str]]]:
    """递归取出某根下全部有效节点（parent_id 在树内）。"""

    rows = conn.execute(
        """
        WITH RECURSIVE tree(message_id, in_reply_to, "references", sent_at, subject, parent_id, depth) AS (
            SELECT message_id, in_reply_to, "references", sent_at, subject, parent_id, 0
            FROM messages WHERE message_id = ?
            UNION ALL
            SELECT m.message_id, m.in_reply_to, m."references", m.sent_at, m.subject, m.parent_id, t.depth + 1
            FROM messages m JOIN tree t ON m.parent_id = t.message_id
            WHERE m.is_invalid = 0
        )
        SELECT message_id, in_reply_to, "references", sent_at, subject, parent_id
        FROM tree ORDER BY depth
        """,
        (root_id,),
    ).fetchall()

    messages: dict[str, ThreadMessage] = {}
    parent_of: dict[str, Optional[str]] = {}
    for row in rows:
        mid = row["message_id"]
        messages[mid] = _row_to_message(row)
        parent_of[mid] = row["parent_id"]
    return messages, parent_of


def _build_payload(
    batch_id: str,
    submitted_at: str,
    normalized_messages: list,
    resolution: threading.BatchResolution,
    archived_duplicates: dict[str, int],
    threads: list[dict],
) -> dict:
    """组装对外响应（同时也是持久化快照）。"""

    duplicates = []
    # 已在历史批次归档的重复 ID
    for mid in sorted(archived_duplicates):
        duplicates.append({
            "message_id": mid,
            "archived": True,
            "first_submitted_seq": None,
            "occurrences": [archived_duplicates[mid]],
        })
    # 本批内的重复 ID
    for dup in resolution.duplicates:
        duplicates.append({
            "message_id": dup.message_id,
            "archived": False,
            "first_submitted_seq": dup.first_submitted_seq,
            "occurrences": dup.occurrences,
        })
    duplicates.sort(key=lambda d: (d["message_id"], 0 if d["archived"] else 1))

    missing_references = [
        {"message_id": m.message_id, "missing": m.missing}
        for m in resolution.missing_refs
    ]

    by_id = {m.message_id: m for m in normalized_messages}
    cycles_out: list[dict] = []
    invalid_messages: list[dict] = []
    for idx, cyc in enumerate(resolution.cycles, start=1):
        cycles_out.append({"index": idx, "members": list(cyc), "invalid_messages": list(cyc)})
        for mid in cyc:
            m = by_id[mid]
            invalid_messages.append({
                "message_id": mid,
                "in_reply_to": m.in_reply_to,
                "references": list(m.references),
                "sent_at": m.sent_at.isoformat(),
                "subject": m.subject,
                "reason": "reference_cycle",
                "cycle_index": idx,
            })
    invalid_messages.sort(key=lambda im: im["message_id"])

    total_count = len(normalized_messages)
    unique_count = len(resolution.first_seen)
    invalid_count = len(resolution.invalid_ids)

    return {
        "batch_id": batch_id,
        "submitted_at": submitted_at,
        "totals": {
            "submitted": total_count,
            "unique_new": unique_count,
            "archived_duplicates": len(archived_duplicates),
            "invalid": invalid_count,
            "missing_reference_messages": len(resolution.missing_refs),
            "cycles": len(resolution.cycles),
            "trees": len(threads),
        },
        "threads": threads,
        "duplicates": duplicates,
        "missing_references": missing_references,
        "cycles": cycles_out,
        "invalid_messages": invalid_messages,
    }


def submit_batch(
    payload: BatchIn, idempotency_key: Optional[str] = None
) -> tuple[dict, bool]:
    """归并、落库、返回完整结果与“是否为重放”标记。同库写操作串行化。

    - 不带 ``idempotency_key``：保持原有行为，每次调用都正常归档一批；
    - 带 ``idempotency_key``：在同一写事务内先查幂等登记表——键首次出现
      时把“请求体摘要 + 键”与本次归档结果**原子登记**；同键再次提交且
      请求体摘要一致时**重放首次结果、不重复归档**（返回标记 replayed=True）；
      同键但请求体摘要不同则抛 ``IdempotencyConflict``（-> 409，不写库）。
    """

    normalized = [m.normalized() for m in payload.messages]
    digest = request_digest(normalized)

    with database.write_lock():
        conn = database.connect()
        try:
            # 单写事务视图：幂等登记、归并读取与全部写入原子提交
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                row = conn.execute(
                    "SELECT request_digest, batch_id FROM idempotency_keys WHERE key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    if row["request_digest"] != digest:
                        raise IdempotencyConflict(idempotency_key)
                    # 相同键 + 相同内容：原样重放首次批次响应，绝不再次归档
                    replay = json.loads(
                        conn.execute(
                            "SELECT result_json FROM batch_results WHERE batch_id = ?",
                            (row["batch_id"],),
                        ).fetchone()["result_json"]
                    )
                    conn.rollback()
                    return replay, True

            result_payload = _archive_batch(conn, normalized)

            if idempotency_key is not None:
                # 与批次结果同事务登记；提交前崩溃则整事务回滚，键可安全重试
                conn.execute(
                    "INSERT INTO idempotency_keys (key, request_digest, batch_id, created_at) VALUES (?, ?, ?, ?)",
                    (
                        idempotency_key,
                        digest,
                        result_payload["batch_id"],
                        result_payload["submitted_at"],
                    ),
                )

            conn.commit()
            return result_payload, False
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _archive_batch(conn: sqlite3.Connection, normalized: list[MessageIn]) -> dict:
    """在调用方持有的事务/连接上归并并写入一批邮件，返回完整结果（不提交）。"""

    existing_ids = {
        r["message_id"]
        for r in conn.execute("SELECT message_id FROM messages")
    }

    # 已归档 ID 整组剔出，作为“重复 ID（历史归档）”诊断
    archived_duplicates: dict[str, int] = {}
    fresh: list = []
    for seq, m in enumerate(normalized, start=1):
        if m.message_id in existing_ids:
            archived_duplicates.setdefault(m.message_id, seq)
        else:
            fresh.append(m)
    thread_messages = [
        ThreadMessage(
            message_id=m.message_id,
            in_reply_to=m.in_reply_to,
            references=tuple(m.references),
            sent_at=m.sent_at.isoformat(),
            subject=m.subject,
        )
        for m in fresh
    ]
    resolution = threading.resolve_batch(
        thread_messages, existing_ids=existing_ids
    )

    batch_id = uuid.uuid4().hex
    submitted_at = _utc_now_iso()

    # 1) 批次元数据（messages/batch_* 均以外键指向它）
    conn.execute(
        """
        INSERT INTO batches
            (batch_id, submitted_at, total_count, unique_count, invalid_count, tree_count)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            submitted_at,
            len(normalized),
            len(resolution.first_seen),
            len(resolution.invalid_ids),
            0,  # 树数量在构建后回填
        ),
    )

    # 2) 写入新邮件（含 invalid；invalid 父指针置 NULL，绝不部分挂接）
    inserted_ids: set[str] = set()
    for seq, m in enumerate(normalized, start=1):
        if m.message_id in inserted_ids or m.message_id in archived_duplicates:
            continue  # 本批重复的后续出现、历史归档重复，均不重复落库
        inserted_ids.add(m.message_id)
        is_invalid = m.message_id in resolution.invalid_ids
        parent_id = None if is_invalid else resolution.parent_of.get(m.message_id)
        conn.execute(
            """
            INSERT INTO messages
                (message_id, in_reply_to, "references", sent_at, subject,
                 parent_id, is_invalid, batch_id, submitted_seq, archived_at,
                 retain_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                m.message_id,
                m.in_reply_to,
                json.dumps(m.references, ensure_ascii=False),
                m.sent_at.isoformat(),
                m.subject,
                parent_id,
                1 if is_invalid else 0,
                batch_id,
                seq,
                submitted_at,
                m.retain_until.isoformat() if m.retain_until else None,
            ),
        )

    # 3) 本批所有有效新邮件向上爬到根，取整棵树（含历史祖先）
    new_valid_ids = [
        mid for mid in resolution.first_seen if mid not in resolution.invalid_ids
    ]
    root_ids = {_climb_root(conn, mid) for mid in new_valid_ids}

    all_messages: dict[str, ThreadMessage] = {}
    all_parents: dict[str, Optional[str]] = {}
    for root_id in root_ids:
        msgs, parents = _load_subtree(conn, root_id)
        all_messages.update(msgs)
        all_parents.update(parents)

    threads = threading.build_forest(all_messages, all_parents)

    result_payload = _build_payload(
        batch_id,
        submitted_at,
        normalized,
        resolution,
        archived_duplicates,
        threads,
    )

    # 4) 回填树数量 + 诊断明细
    conn.execute(
        "UPDATE batches SET tree_count = ? WHERE batch_id = ?",
        (len(threads), batch_id),
    )
    for dup in result_payload["duplicates"]:
        conn.execute(
            "INSERT INTO batch_duplicates (batch_id, message_id, archived, first_submitted_seq, occurrences) VALUES (?, ?, ?, ?, ?)",
            (
                batch_id,
                dup["message_id"],
                1 if dup["archived"] else 0,
                dup["first_submitted_seq"],
                json.dumps(dup["occurrences"]),
            ),
        )
    for mr in result_payload["missing_references"]:
        conn.execute(
            "INSERT INTO batch_missing_refs (batch_id, message_id, missing) VALUES (?, ?, ?)",
            (batch_id, mr["message_id"], json.dumps(mr["missing"], ensure_ascii=False)),
        )
    for cyc in result_payload["cycles"]:
        conn.execute(
            "INSERT INTO batch_cycles (batch_id, idx, members) VALUES (?, ?, ?)",
            (batch_id, cyc["index"], json.dumps(cyc["members"], ensure_ascii=False)),
        )

    # 5) 结果快照
    conn.execute(
        "INSERT INTO batch_results (batch_id, result_json) VALUES (?, ?)",
        (batch_id, json.dumps(result_payload, ensure_ascii=False)),
    )

    return result_payload


def import_rfc5322(payload: Rfc5322ImportIn) -> dict:
    """解析一组 RFC 5322 原始头并归一为现有批次模型后归档。

    先对全部输入逐项解析（折行展开、重复字段按出现顺序处理、RFC 2047
    编码词解码），任一输入缺失或含非法必填字段即抛
    ``Rfc5322ImportErrors``（错误逐项带输入下标），**绝不写库**；全部
    通过后构造 ``BatchIn`` 复用 submit_batch 的同一套归并、落库与快照
    逻辑，一次性写入并返回 BatchResult。
    """

    try:
        parsed = parse_header_blocks(payload.headers)
    except Rfc5322ParseError as exc:
        raise Rfc5322ImportErrors([e.as_dict() for e in exc.errors])

    batch = BatchIn(messages=[
        MessageIn(
            message_id=item.message_id,
            in_reply_to=item.in_reply_to,
            references=list(item.references),
            sent_at=item.sent_at,
            subject=item.subject,
        )
        for item in parsed
    ])
    result, _replayed = submit_batch(batch)
    return result


def _climb_root_inmem(
    message_id: str, parent_of: dict[str, Optional[str]]
) -> str:
    """在内存父指针图上向上爬到根（遇到不存在或环时回退为当前节点）。"""

    cur = message_id
    seen: set[str] = set()
    while True:
        pid = parent_of.get(cur)
        if pid is None or pid in seen:
            return cur
        seen.add(cur)
        cur = pid


def relink_messages(payload: RelinkIn) -> dict:
    """迟到邮件归档后，批量修复此前断链的有效邮件。

    - 每封邮件仅在当前 parent_id 为空（断链自成根）且非环内 invalid 时处理；
    - 候选父仍按原规则：先取现存且有效的 in_reply_to，再从 references
      右向左回退；无法命中则保持不变；
    - 全部候选关系合并到既有父指针图上统一检测环，任一变更成环则整次
      抛 RelinkCycleConflict，绝不写库；
    - 无环时在单事务中更新 parent_id，返回每封邮件的新旧 parent/root。
    """

    with database.write_lock():
        conn = database.connect()
        try:
            rows = conn.execute(
                """
                SELECT message_id, in_reply_to, "references", parent_id, is_invalid
                FROM messages
                WHERE message_id IN (%s)
                """ % ",".join("?" * len(payload.message_ids)),
                payload.message_ids,
            ).fetchall() if payload.message_ids else []

            by_id = {r["message_id"]: r for r in rows}
            missing = [mid for mid in payload.message_ids if mid not in by_id]
            if missing:
                raise RelinkNotFound(missing)

            # 既有整图：所有已归档有效邮件的当前父指针
            base_parents: dict[str, Optional[str]] = {
                r["message_id"]: r["parent_id"]
                for r in conn.execute(
                    "SELECT message_id, parent_id FROM messages WHERE is_invalid = 0"
                )
            }
            # 环内 invalid 邮件不是“有效”节点，不可作为候选父
            invalid_ids = {
                r["message_id"]
                for r in conn.execute(
                    "SELECT message_id FROM messages WHERE is_invalid = 1"
                )
            }

            # 逐封计算候选；仅有效且当前为断链根的邮件参与
            candidates: dict[str, Optional[str]] = {}
            for mid in payload.message_ids:
                row = by_id[mid]
                if row["is_invalid"] or row["parent_id"] is not None:
                    continue
                msg = ThreadMessage(
                    message_id=mid,
                    in_reply_to=row["in_reply_to"],
                    references=tuple(json.loads(row["references"])),
                    sent_at="",
                    subject="",
                )
                # 已知集合 = 有效邮件全集；环成员(invalid) 不作为父
                candidates[mid] = threading.pick_candidate(
                    msg, set(base_parents), invalid_ids=invalid_ids
                )

            # 合并全部候选关系后统一检测环
            merged_parents = dict(base_parents)
            for mid, new_pid in candidates.items():
                merged_parents[mid] = new_pid

            cycles = threading.find_cycles(merged_parents)
            if cycles:
                raise RelinkCycleConflict(cycles)

            results: list[dict] = []
            relinked = 0
            updates: list[tuple[Optional[str], str]] = []
            for mid in payload.message_ids:
                row = by_id[mid]
                old_pid = row["parent_id"]
                new_pid = candidates.get(mid, old_pid)
                changed = new_pid != old_pid
                if changed:
                    relinked += 1
                    updates.append((new_pid, mid))
                old_root = (
                    mid
                    if old_pid is None or row["is_invalid"]
                    else _climb_root_inmem(mid, base_parents)
                )
                new_root = (
                    mid
                    if new_pid is None or row["is_invalid"]
                    else _climb_root_inmem(mid, merged_parents)
                )
                results.append({
                    "message_id": mid,
                    "old_parent_id": old_pid,
                    "new_parent_id": new_pid,
                    "old_root_id": old_root,
                    "new_root_id": new_root,
                })

            # 单事务提交全部父指针变更
            for new_pid, mid in updates:
                conn.execute(
                    "UPDATE messages SET parent_id = ? WHERE message_id = ?",
                    (new_pid, mid),
                )
            conn.commit()
            return {"relinked": relinked, "results": results}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def list_batches(limit: int, offset: int) -> dict:
    conn = database.connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM batches ORDER BY submitted_at DESC, batch_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        items = [
            {
                "batch_id": r["batch_id"],
                "submitted_at": r["submitted_at"],
                "total_count": r["total_count"],
                "unique_count": r["unique_count"],
                "invalid_count": r["invalid_count"],
                "tree_count": r["tree_count"],
            }
            for r in rows
        ]
        return {"total": total, "limit": limit, "offset": offset, "batches": items}
    finally:
        conn.close()


def get_batch(batch_id: str) -> Optional[dict]:
    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT result_json FROM batch_results WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return json.loads(row["result_json"]) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /batches/{batch_id}/drift：批次漂移审计（只读）
# ---------------------------------------------------------------------------


def audit_batch_drift(batch_id: str) -> Optional[dict]:
    """只读审计某批次不可变快照中原始会话树的漂移。

    以快照 threads 森林为基线（原始先序），逐节点对照当前 messages 表。
    当前行 archived_at 不晚于批次 submitted_at 即仍是快照时的同一归档
    （来源一致；历史祖先由其原始批次归档、同样一致）；晚于批次
    submitted_at 说明原归档已被删除、同 ID 由更晚的批次重新归档
    （id_reused）。未知批次返回 None（-> 404）。不写库、不改写批次快照
    与诊断明细。
    """

    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT result_json FROM batch_results WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            return None
        snapshot = json.loads(row["result_json"])
        submitted_at = snapshot["submitted_at"]

        baseline = threading.flatten_snapshot_threads(snapshot["threads"])
        baseline_ids = [node.message_id for node in baseline]

        # 当前整幅父指针图：一次装载后在内存中上溯各节点的当前根
        parent_map: dict[str, Optional[str]] = {
            r["message_id"]: r["parent_id"]
            for r in conn.execute("SELECT message_id, parent_id FROM messages")
        }
        current: dict[str, threading.DriftCurrentState] = {}
        if baseline_ids:
            placeholders = ",".join("?" * len(baseline_ids))
            rows = conn.execute(
                "SELECT message_id, parent_id, archived_at FROM messages "
                f"WHERE message_id IN ({placeholders})",
                baseline_ids,
            ).fetchall()
            for r in rows:
                mid = r["message_id"]
                current[mid] = threading.DriftCurrentState(
                    parent_id=r["parent_id"],
                    root_id=_climb_root_inmem(mid, parent_map),
                    # 归档时间不晚于快照时刻 => 仍是快照时的同一行（同一归档）
                    source_consistent=r["archived_at"] <= submitted_at,
                )

        audit = threading.audit_batch_drift(baseline, current)
        return {
            "batch_id": batch_id,
            "submitted_at": submitted_at,
            "results": [
                {
                    "message_id": item.message_id,
                    "original_parent_id": item.original_parent_id,
                    "original_root_id": item.original_root_id,
                    "current_parent_id": item.current_parent_id,
                    "current_root_id": item.current_root_id,
                    "status": item.status,
                }
                for item in audit.items
            ],
            "summary": audit.summary,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POST /batches/{batch_id}/rollback-preview：批次撤销预演（只读）
# ---------------------------------------------------------------------------


def preview_batch_rollback(batch_id: str) -> Optional[dict]:
    """只读预演撤销某批次首次归档、当前仍归属该批次的有效邮件。

    拟删除集只包含 ``messages.batch_id == 该批次`` 且 ``is_invalid = 0``
    的当前行，因此天然排除：

    - 快照树中的**历史祖先**（由更早批次归档，batch_id 不同，永不删除）；
    - 本批归档的**环内 invalid 邮件**（is_invalid = 1，从不参与有效森林）；
    - **已被更晚批次重用同 ID** 的邮件——原归档被保留期清理删除后由更晚
      批次重新归档，当前行 batch_id 已指向更晚批次。

    压缩与保留期清理共用 ``threading.plan_batch_rollback``（底层为同一
    ``compress_forest``）：存活子孙沿父链跳过被删节点、重挂到最近存活
    祖先，链顶无存活祖先则提升为新根。撤销不引入保留期 / 冻结概念。
    全程只读：显式只读事务内取一致视图后 rollback，绝不写库；未知批次
    返回 None（-> 404）。
    """

    conn = database.connect()
    try:
        # 显式只读事务：批次存在性、计数与整幅森林的读取落在同一事务视图
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT result_json FROM batch_results WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        snapshot = json.loads(row["result_json"])
        submitted_at = snapshot["submitted_at"]

        # 快照 threads 基线中的全部有效节点（环内 invalid 不在其中）
        baseline = threading.flatten_snapshot_threads(snapshot["threads"])
        baseline_ids = [node.message_id for node in baseline]

        # 本批首次归档、当前仍归属本批的有效邮件 => 拟删除集；
        # 基线中当前行 archived_at 晚于批次时刻者 => 同 ID 已被更晚批次重用。
        candidate_ids: set[str] = set()
        reused_ids: set[str] = set()
        if baseline_ids:
            placeholders = ",".join("?" * len(baseline_ids))
            for r in conn.execute(
                "SELECT message_id, batch_id, archived_at FROM messages "
                f"WHERE message_id IN ({placeholders})",
                baseline_ids,
            ):
                if r["batch_id"] == batch_id:
                    candidate_ids.add(r["message_id"])
                if r["archived_at"] > submitted_at:
                    reused_ids.add(r["message_id"])

        # 本批归档的环内 invalid 邮件（invalid 行从不参与清理、不会被重用）
        skipped_invalid = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE batch_id = ? AND is_invalid = 1",
            (batch_id,),
        ).fetchone()["c"]

        # 当前整幅有效邮件森林的父指针（环内 invalid 邮件不在其中）
        parent_rows = conn.execute(
            "SELECT message_id, parent_id FROM messages WHERE is_invalid = 0"
        ).fetchall()
        current_parent_of: dict[str, Optional[str]] = {
            r["message_id"]: r["parent_id"] for r in parent_rows
        }

        plan = threading.plan_batch_rollback(
            parent_of=current_parent_of,
            candidate_delete_ids=candidate_ids,
        )

        # 仅为受触及旧会话的存活节点装载归并/排序所需的完整字段，
        # 在压缩后父指针上按与 build_flat_tree 相同的稳定排序先序展开，
        # 再按 depth=0 切分。压缩只会沿同一父链跳过被删祖先、绝不跨树
        # 挂接，故 touched 存活节点的新父要么为 None、要么仍是同组 touched
        # 存活节点（子图闭合）。
        touched = set(plan.touched_survivors)
        impact_by_id = {impact.message_id: impact for impact in plan.impacts}

        touched_messages: dict[str, ThreadMessage] = {}
        if touched:
            placeholders = ",".join("?" * len(touched))
            for r in conn.execute(
                "SELECT message_id, in_reply_to, \"references\", sent_at, subject "
                f"FROM messages WHERE message_id IN ({placeholders})",
                sorted(touched),
            ):
                touched_messages[r["message_id"]] = _row_to_message(r)
        conn.rollback()

        def new_parent_of(mid: str) -> Optional[str]:
            impact = impact_by_id.get(mid)
            return (
                impact.new_parent_id
                if impact is not None
                else current_parent_of[mid]
            )

        touched_parents = {mid: new_parent_of(mid) for mid in touched}
        flat = threading.build_flat_tree(touched_messages, touched_parents)

        root_groups: list[dict] = []
        current: Optional[dict] = None
        for item in flat:
            if item["depth"] == 0:
                current = {"root_id": item["message_id"], "members": []}
                root_groups.append(current)
            # 先序展开保证每个节点都在某个 depth=0 的根之后
            current["members"].append(item["message_id"])  # type: ignore[index]
        for group in root_groups:
            group["size"] = len(group["members"])

        return {
            "batch_id": batch_id,
            "submitted_at": submitted_at,
            "deleted_ids": plan.deleted_ids,
            "affected": [
                {
                    "message_id": impact.message_id,
                    "old_parent_id": impact.old_parent_id,
                    "new_parent_id": impact.new_parent_id,
                    "old_root_id": impact.old_root_id,
                    "new_root_id": impact.new_root_id,
                }
                for impact in plan.impacts
            ],
            "root_groups": root_groups,
            "summary": {
                "deleted_count": len(plan.deleted_ids),
                "affected_count": len(plan.impacts),
                "skipped_invalid_count": skipped_invalid,
                "skipped_reused_count": len(reused_ids),
                "touched_tree_count": len(plan.touched_old_roots),
                "root_group_count": len(root_groups),
                "surviving_count": len(touched),
            },
        }
    finally:
        conn.close()


def get_message(message_id: str) -> Optional[dict]:
    conn = database.connect()
    try:
        row = conn.execute(
            'SELECT message_id, in_reply_to, "references", sent_at, subject, parent_id, is_invalid, batch_id, retain_until FROM messages WHERE message_id = ?',
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        # 活跃冻结：与任一冻结锚点同属一棵当前会话树（根相同）即受保护；
        # invalid 邮件不属于任何会话树，其“根”即自身，永不命中保护根。
        frozen = _climb_root(conn, message_id) in _protected_roots(conn)
        return {
            "message_id": row["message_id"],
            "in_reply_to": row["in_reply_to"],
            "references": json.loads(row["references"]),
            "sent_at": row["sent_at"],
            "subject": row["subject"],
            "parent_id": row["parent_id"],
            "is_invalid": bool(row["is_invalid"]),
            "batch_id": row["batch_id"],
            "retain_until": row["retain_until"],
            "frozen": frozen,
        }
    finally:
        conn.close()


def get_thread(message_id: str) -> Optional[dict]:
    """返回某邮件所在会话的完整树与当前 revision。环邮件不挂接，返回 409。"""

    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT is_invalid FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        if row["is_invalid"]:
            return {"invalid": True, "message_id": message_id}

        root_id = _climb_root(conn, message_id)
        msgs, parents = _load_subtree(conn, root_id)
        forest = threading.build_forest(msgs, parents)
        return {
            "root_id": root_id,
            "tree": forest[0] if forest else None,
            "revision": _tree_revision(msgs, parents),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POST /thread-corrections：人工会话纠偏
# ---------------------------------------------------------------------------


def correct_thread(payload: ThreadCorrectionIn) -> dict:
    """人工调整一封有效邮件的父节点（new_parent_id 为 None 即拆成根）。

    - 仅允许调整引用环之外的有效邮件，目标父节点也必须有效（存在且非环）：
      待调整邮件/目标父不存在 -> 404；任一为环内 invalid 邮件 -> 409；
    - expected_revision 必须等于该邮件**当前所在整棵会话拓扑**的 revision
      （即 GET /threads/{message_id} 最近返回的值），不匹配 -> 409 且不写库，
      响应 detail 给出当前 revision 供调用方重新读取；
    - 禁止自挂（new_parent_id == message_id），禁止把节点挂到自身后代下
      形成引用环：候选关系合并到既有父指针图上统一检测，命中 -> 409
      （给出环成员）且不写库；
    - 通过后在单事务内更新 parent_id，并基于新拓扑重新装载整棵会话计算
      新 revision；返回节点调整前后的父节点、根节点与新 revision。
    批次快照与诊断明细不参与、不改写；活跃冻结按新拓扑自然生效。
    """

    message_id = payload.message_id
    new_parent_id = payload.new_parent_id

    with database.write_lock():
        conn = database.connect()
        try:
            # 单事务视图：revision 校验、环检测与写入基于同一快照
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                "SELECT parent_id, is_invalid FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise CorrectionMessageNotFound("message not found")
            if row["is_invalid"]:
                raise CorrectionInvalidMessage(
                    "message belongs to a reference cycle and is not attached to any thread"
                )
            old_parent_id = row["parent_id"]

            if new_parent_id is not None:
                prow = conn.execute(
                    "SELECT is_invalid FROM messages WHERE message_id = ?",
                    (new_parent_id,),
                ).fetchone()
                if prow is None:
                    raise CorrectionParentNotFound(new_parent_id)
                if prow["is_invalid"]:
                    raise CorrectionInvalidParent(
                        "new_parent_id belongs to a reference cycle and is not attached to any thread"
                    )

            # 既有整图：全部有效邮件的当前父指针
            base_parents: dict[str, Optional[str]] = {
                r["message_id"]: r["parent_id"]
                for r in conn.execute(
                    "SELECT message_id, parent_id FROM messages WHERE is_invalid = 0"
                )
            }

            # revision 以该邮件当前所在的整棵会话拓扑计算（与 GET /threads 一致）
            old_root_id = _climb_root_inmem(message_id, base_parents)
            cur_msgs, cur_parents = _load_subtree(conn, old_root_id)
            if payload.expected_revision != _tree_revision(cur_msgs, cur_parents):
                raise CorrectionRevisionConflict(
                    _tree_revision(cur_msgs, cur_parents)
                )

            # 候选关系合并到既有图上统一检测环（自挂即单节点环）
            merged_parents = dict(base_parents)
            merged_parents[message_id] = new_parent_id
            cycles = threading.find_cycles(merged_parents)
            if cycles:
                raise CorrectionCycleConflict(cycles)

            conn.execute(
                "UPDATE messages SET parent_id = ? WHERE message_id = ?",
                (new_parent_id, message_id),
            )

            # 新拓扑：重新装载该邮件当前所在整棵会话并计算新 revision
            new_root_id = _climb_root_inmem(message_id, merged_parents)
            new_msgs, new_parents = _load_subtree(conn, new_root_id)
            new_revision = _tree_revision(new_msgs, new_parents)

            conn.commit()
            return {
                "message_id": message_id,
                "old_parent_id": old_parent_id,
                "new_parent_id": new_parent_id,
                "old_root_id": old_root_id,
                "new_root_id": new_root_id,
                "revision": new_revision,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# GET /threads/{message_id}/page：稳定游标分页
# ---------------------------------------------------------------------------


def _tree_revision(
    messages: dict[str, ThreadMessage],
    parent_of: dict[str, Optional[str]],
) -> str:
    """根据整棵树的 message_id、parent_id、sent_at 生成 revision。

    节点按 message_id 排序后逐行摘要，补链改变父指针、新邮件并入树等任何
    结构变化都会使摘要改变，从而让旧游标失效。
    """

    digest = hashlib.sha256()
    for mid in sorted(messages):
        msg = messages[mid]
        line = "\x1f".join(
            [mid, parent_of.get(mid) or "", msg.sent_at]
        )
        digest.update(line.encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def _encode_cursor(root_id: str, offset: int, revision: str) -> str:
    """把根 ID、下一偏移、树 revision 封装为不透明 base64url 游标。"""

    payload = {
        "v": _CURSOR_VERSION,
        "root_id": root_id,
        "offset": offset,
        "revision": revision,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, int, str]:
    """解析游标；任何格式 / 字段问题统一抛 CursorMalformed。"""

    try:
        if not _CURSOR_TOKEN_RE.fullmatch(cursor):
            raise ValueError("bad charset")
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise CursorMalformed("cursor is malformed") from exc

    if (
        not isinstance(payload, dict)
        or set(payload) != {"v", "root_id", "offset", "revision"}
        or payload["v"] != _CURSOR_VERSION
        or not isinstance(payload["root_id"], str)
        or not isinstance(payload["offset"], int)
        or isinstance(payload["offset"], bool)
        or payload["offset"] < 0
        or not isinstance(payload["revision"], str)
    ):
        raise CursorMalformed("cursor is malformed")
    return payload["root_id"], payload["offset"], payload["revision"]


def get_thread_page(
    message_id: str,
    limit: int,
    cursor: Optional[str],
) -> dict:
    """先序扁平分页查询某邮件所属会话。

    首次请求不带 cursor：从偏移 0 起；后续请求携带上次响应的 next_cursor。
    游标封装根 ID、下一偏移与整树 revision：树在补链中发生变化返回
    CursorStale（409 cursor_stale）；游标与路径所属会话不符返回
    CursorThreadMismatch（422）。
    """

    start_offset = 0
    cursor_root: Optional[str] = None
    cursor_revision: Optional[str] = None
    if cursor:
        cursor_root, start_offset, cursor_revision = _decode_cursor(cursor)

    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT is_invalid FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        if row["is_invalid"]:
            return {"invalid": True, "message_id": message_id}

        root_id = _climb_root(conn, message_id)
        msgs, parents = _load_subtree(conn, root_id)
        items = threading.build_flat_tree(msgs, parents)
        revision = _tree_revision(msgs, parents)

        if cursor_root is not None:
            if cursor_root != root_id:
                # 游标根是当前树内节点（补链并树后下沉为子节点）：树已变 -> 409
                if cursor_root in msgs:
                    raise CursorStale("cursor revision is stale; restart from the first page")
                # 游标根已被保留期清理删除（同一谱系被压缩）：树已变 -> 409
                ghost = conn.execute(
                    "SELECT 1 FROM messages WHERE message_id = ?", (cursor_root,)
                ).fetchone()
                if ghost is None:
                    raise CursorStale(
                        "cursor root has been removed by retention cleanup; "
                        "restart from the first page"
                    )
                # 游标根不属于路径所属会话 -> 422
                raise CursorThreadMismatch(
                    "cursor does not belong to the thread of the path message_id"
                )
            if cursor_revision != revision:
                raise CursorStale("cursor revision is stale; restart from the first page")
            if start_offset > len(items):
                raise CursorStale("cursor offset is beyond the current thread")

        page = items[start_offset:start_offset + limit]
        next_offset = start_offset + len(page)
        next_cursor = (
            _encode_cursor(root_id, next_offset, revision)
            if next_offset < len(items)
            else None
        )

        return {
            "root_id": root_id,
            "items": page,
            "next_cursor": next_cursor,
            "limit": limit,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POST /thread-sla/evaluate：会话响应时限评估（只读）
# ---------------------------------------------------------------------------


def evaluate_thread_sla(payload: ThreadSlaEvaluateIn) -> Optional[dict]:
    """只读评估某邮件当前所在整棵会话的响应时限。

    未知邮件返回 None（-> 404）；环内 invalid 邮件返回 {"invalid": True}
    （-> 409）。只读取当前 messages 表：revision 与逐封判定基于同一次
    装载的整棵会话，不写库、不改写批次快照与诊断明细。
    """

    as_of = payload.normalized_as_of()
    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT is_invalid FROM messages WHERE message_id = ?",
            (payload.message_id,),
        ).fetchone()
        if row is None:
            return None
        if row["is_invalid"]:
            return {"invalid": True, "message_id": payload.message_id}

        root_id = _climb_root(conn, payload.message_id)
        msgs, parents = _load_subtree(conn, root_id)
        evaluation = threading.evaluate_thread_sla(
            msgs, parents, as_of, payload.threshold_seconds
        )
        return {
            "message_id": payload.message_id,
            "root_id": root_id,
            "as_of": as_of.isoformat(),
            "threshold_seconds": payload.threshold_seconds,
            "revision": _tree_revision(msgs, parents),
            "results": [
                {
                    "message_id": item.message_id,
                    "parent_id": item.parent_id,
                    "sent_at": item.sent_at,
                    "depth": item.depth,
                    "status": item.status,
                    "response_message_id": item.response_message_id,
                    "response_sent_at": item.response_sent_at,
                    "response_delay_seconds": item.response_delay_seconds,
                    "age_seconds": item.age_seconds,
                }
                for item in evaluation.items
            ],
            "summary": evaluation.summary,
            "chronology_anomalies": [
                {
                    "message_id": a.message_id,
                    "parent_id": a.parent_id,
                    "sent_at": a.sent_at,
                    "parent_sent_at": a.parent_sent_at,
                }
                for a in evaluation.anomalies
            ],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POST /thread-subject-audit：会话主题漂移审计（只读）
# ---------------------------------------------------------------------------


def audit_thread_subject(payload: ThreadSubjectAuditIn) -> Optional[dict]:
    """只读审计某邮件当前所在整棵会话的主题漂移。

    未知邮件返回 None（-> 404）；环内 invalid 邮件返回 {"invalid": True}
    （-> 409）。只读取当前 messages 表：revision 与逐节点判定基于同一次
    装载的整棵会话，不写库、不改写批次快照与诊断明细。
    """

    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT is_invalid FROM messages WHERE message_id = ?",
            (payload.message_id,),
        ).fetchone()
        if row is None:
            return None
        if row["is_invalid"]:
            return {"invalid": True, "message_id": payload.message_id}

        root_id = _climb_root(conn, payload.message_id)
        msgs, parents = _load_subtree(conn, root_id)
        audit = threading.audit_thread_subjects(msgs, parents)
        return {
            "message_id": payload.message_id,
            "root_id": root_id,
            "revision": _tree_revision(msgs, parents),
            "results": [
                {
                    "message_id": item.message_id,
                    "parent_id": item.parent_id,
                    "depth": item.depth,
                    "subject": item.subject,
                    "canonical_subject": item.canonical_subject,
                    "baseline_subject": item.baseline_subject,
                    "drifted": item.drifted,
                }
                for item in audit.items
            ],
            "drift_groups": [
                {
                    "members": group.members,
                    "baseline_subject": group.baseline_subject,
                }
                for group in audit.drift_groups
            ],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 保留期：会话冻结 / 解除冻结 / 批量清理
#
# 活跃冻结：冻结只记录锚点，不保存成员快照。保护集合在使用时（冻结/解冻
# 响应、GET /messages 的 frozen、清理计划）按当前父子关系实时展开锚点
# 所在的整棵会话；多个冻结重叠时取并集。因此新批次归档或 POST /relinks
# 并入被冻结会话的邮件自动受保护，无需再次冻结。
# ---------------------------------------------------------------------------


def _protected_roots(conn: sqlite3.Connection) -> set[str]:
    """各冻结锚点当前所在会话的根 ID 集合（按当前父指针实时上溯）。"""

    return {
        _climb_root(conn, r["anchor_id"])
        for r in conn.execute("SELECT anchor_id FROM retention_freezes")
    }


def _load_protected_ids(conn: sqlite3.Connection) -> set[str]:
    """当前被任一冻结覆盖的全部有效邮件 ID（多冻结取并集）。

    按当前父子关系展开每个锚点所在的整棵会话；与调用方使用同一连接，
    保证与所在的清理/解冻事务看到同一视图。
    """

    protected: set[str] = set()
    for root_id in _protected_roots(conn):
        msgs, _ = _load_subtree(conn, root_id)
        protected.update(msgs)
    return protected


def freeze_thread(payload: FreezeIn) -> dict:
    """冻结锚点邮件当前所在的整棵会话（活跃冻结）。

    冻结只记录锚点：保护范围随会话拓扑实时扩张——之后经新批次归档或
    POST /relinks 并入锚点当前会话的邮件自动受保护，无需再次冻结。
    对同一锚点重复调用仅刷新 frozen_at（created=False）；返回的 members
    始终反映调用时刻的当前会话成员。锚点不存在 -> 404；锚点为环内
    invalid 邮件（不属于任何会话树）-> 409。
    """

    anchor_id = payload.message_id
    with database.write_lock():
        conn = database.connect()
        try:
            row = conn.execute(
                "SELECT is_invalid FROM messages WHERE message_id = ?",
                (anchor_id,),
            ).fetchone()
            if row is None:
                raise RetentionAnchorNotFound("message not found")
            if row["is_invalid"]:
                raise RetentionAnchorInvalid(
                    "message belongs to a reference cycle and is not attached to any thread"
                )

            root_id = _climb_root(conn, anchor_id)
            msgs, parents = _load_subtree(conn, root_id)
            # 成员顺序与会话查询/分页一致：稳定排序的先序
            members = [item["message_id"] for item in threading.build_flat_tree(msgs, parents)]

            existing = conn.execute(
                "SELECT frozen_at FROM retention_freezes WHERE anchor_id = ?",
                (anchor_id,),
            ).fetchone()
            frozen_at = _utc_now_iso()
            if existing is None:
                created = True
                conn.execute(
                    "INSERT INTO retention_freezes (anchor_id, frozen_at) VALUES (?, ?)",
                    (anchor_id, frozen_at),
                )
            else:
                created = False
                conn.execute(
                    "UPDATE retention_freezes SET frozen_at = ? WHERE anchor_id = ?",
                    (frozen_at, anchor_id),
                )
            conn.commit()
            return {
                "anchor_id": anchor_id,
                "root_id": root_id,
                "frozen_at": frozen_at,
                "created": created,
                "members": members,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def unfreeze_thread(payload: UnfreezeIn) -> dict:
    """解除以锚点邮件建立的冻结。

    释放范围按解除时刻的当前保护关系计算：该锚点当前会话的成员中，
    不再被任何剩余冻结覆盖的进入 released_members；仍被其它锚点覆盖
    （多冻结重叠取并集）的进入 still_protected，清理时依旧受保护。
    """

    anchor_id = payload.message_id
    with database.write_lock():
        conn = database.connect()
        try:
            row = conn.execute(
                "SELECT frozen_at FROM retention_freezes WHERE anchor_id = ?",
                (anchor_id,),
            ).fetchone()
            if row is None:
                raise RetentionFreezeNotFound("freeze not found for anchor message")
            frozen_at = row["frozen_at"]

            # 该冻结当前覆盖的成员 = 锚点当前所在的整棵会话（按当前父指针）
            root_id = _climb_root(conn, anchor_id)
            msgs, _ = _load_subtree(conn, root_id)
            members = sorted(msgs)

            conn.execute(
                "DELETE FROM retention_freezes WHERE anchor_id = ?",
                (anchor_id,),
            )

            # 剩余冻结按当前拓扑重新取并集；仍被覆盖的成员不得释放
            remaining = _load_protected_ids(conn)
            released_members = [mid for mid in members if mid not in remaining]
            still_protected = [mid for mid in members if mid in remaining]

            conn.commit()
            return {
                "anchor_id": anchor_id,
                "root_id": root_id,
                "frozen_at": frozen_at,
                "released_members": released_members,
                "still_protected": still_protected,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _build_cleanup_plan(
    conn: sqlite3.Connection, cutoff_iso: str
) -> tuple[RetentionCleanupPlan, dict[str, Optional[str]]]:
    """读取当前有效邮件森林并计算到期清理计划（dry_run 与实跑共用）。

    保护集合在同一连接（同一事务视图）内按当前父子关系实时展开——
    活跃冻结的并集，dry_run 与实际执行因此基于完全一致的拓扑结论。
    """

    rows = conn.execute(
        "SELECT message_id, parent_id FROM messages WHERE is_invalid = 0"
    ).fetchall()
    parent_of: dict[str, Optional[str]] = {r["message_id"]: r["parent_id"] for r in rows}

    expired_rows = conn.execute(
        "SELECT message_id FROM messages WHERE is_invalid = 0 AND retain_until IS NOT NULL AND retain_until <= ?",
        (cutoff_iso,),
    ).fetchall()
    expired_ids = {r["message_id"] for r in expired_rows}
    protected_ids = _load_protected_ids(conn)

    plan = threading.plan_retention_cleanup(parent_of, expired_ids, protected_ids)
    return plan, parent_of


def cleanup_messages(payload: CleanupIn) -> dict:
    """批量清理截止时刻前已到期、且未被活跃冻结保护的有效邮件。

    筛出删除集后先在内存父指针图上做整林压缩（最近存活祖先重挂 /
    提升为新根），dry_run 返回同一算法结论但不写库；实际清理在单事务
    内完成全部 DELETE 与 parent_id UPDATE。dry_run 与实际执行在同一
    事务视图中按当前父子关系计算保护集合，结论一致。
    """

    cutoff_dt = payload.normalized_cutoff() or datetime.now(timezone.utc)
    cutoff_iso = cutoff_dt.isoformat()

    if payload.dry_run:
        conn = database.connect()
        try:
            # 显式只读事务：保护集合与到期集的全部读取落在同一事务视图
            conn.execute("BEGIN")
            plan, _ = _build_cleanup_plan(conn, cutoff_iso)
            conn.rollback()
        finally:
            conn.close()
        return _cleanup_result(payload.dry_run, cutoff_dt, plan)

    with database.write_lock():
        conn = database.connect()
        try:
            # 单事务视图：计划计算与全部写入基于同一快照（WAL 下读不阻塞）
            conn.execute("BEGIN IMMEDIATE")
            plan, _ = _build_cleanup_plan(conn, cutoff_iso)

            # 单事务：先重挂存活后代，再删除到期节点
            for impact in plan.impacts:
                conn.execute(
                    "UPDATE messages SET parent_id = ? WHERE message_id = ?",
                    (impact.new_parent_id, impact.message_id),
                )
            if plan.deleted_ids:
                placeholders = ",".join("?" * len(plan.deleted_ids))
                conn.execute(
                    f"DELETE FROM messages WHERE message_id IN ({placeholders})",
                    plan.deleted_ids,
                )
            conn.commit()
            return _cleanup_result(payload.dry_run, cutoff_dt, plan)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _cleanup_result(
    dry_run: bool, cutoff_dt: datetime, plan: RetentionCleanupPlan
) -> dict:
    return {
        "dry_run": dry_run,
        "cutoff": cutoff_dt.isoformat(),
        "deleted_ids": plan.deleted_ids,
        "affected": [
            {
                "message_id": impact.message_id,
                "old_parent_id": impact.old_parent_id,
                "new_parent_id": impact.new_parent_id,
                "old_root_id": impact.old_root_id,
                "new_root_id": impact.new_root_id,
            }
            for impact in plan.impacts
        ],
        "deleted_count": len(plan.deleted_ids),
        "affected_count": len(plan.impacts),
        "expired_count": len(plan.expired_ids),
        "protected_count": len(plan.protected_expired_ids),
    }
