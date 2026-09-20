"""FastAPI 入口：启动即建表。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response

from . import database, service
from .config import settings
from .schemas import (
    BatchDriftReport,
    BatchIn,
    BatchResult,
    BatchRollbackPreview,
    BatchSummary,
    CleanupIn,
    CleanupResult,
    FreezeIn,
    FreezeResult,
    MessageOut,
    RelinkIn,
    RelinkResult,
    Rfc5322ImportIn,
    ThreadCorrectionIn,
    ThreadCorrectionResult,
    ThreadNodeOut,
    ThreadSlaEvaluateIn,
    ThreadSlaReport,
    ThreadSubjectAuditIn,
    ThreadSubjectAuditReport,
    UnfreezeIn,
    UnfreezeResult,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title=settings.app_title,
    version="1.9.0",
    description=(
        "批量提交邮件头信息，归并为稳定排序的会话树，诊断重复 ID / 缺失引用 / 引用环；"
        "POST /batches 支持 Idempotency-Key 幂等提交（同键同内容重放首次批次响应，"
        "同键不同内容 409，未携带键保持原有行为）；"
        "支持 RFC 5322 原始邮件头导入（POST /imports/rfc5322，解析折行、重复字段与 "
        "RFC 2047 编码词）、邮件保留期（retain_until）、会话活跃冻结/解除冻结与到期批量清理，"
        "人工会话纠偏（POST /thread-corrections），"
        "会话响应时限评估（POST /thread-sla/evaluate，只读）、"
        "会话主题漂移审计（POST /thread-subject-audit，只读）、"
        "批次漂移审计（GET /batches/{batch_id}/drift，只读），"
        "以及批次撤销预演（POST /batches/{batch_id}/rollback-preview，只读）。"
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/batches",
    response_model=BatchResult,
    status_code=201,
    tags=["batches"],
    summary="提交一批邮件并归并会话（支持 Idempotency-Key 幂等提交）",
)
def submit_batch(
    payload: BatchIn,
    response: Response,
    idempotency_key: Annotated[
        Optional[str],
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
            pattern=r"^[\x21-\x7e]+$",
            description=(
                "幂等键：1～255 个可见 ASCII 字符（不含空白）。携带时同键同内容重放"
                "首次批次响应（201，响应头 Idempotency-Replayed: true），同键不同内容"
                "返回 409；不携带则保持每次提交都归档的原有行为。"
            ),
        ),
    ] = None,
) -> dict:
    try:
        result, replayed = service.submit_batch(payload, idempotency_key)
    except service.IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "idempotency_conflict",
                "message": (
                    "Idempotency-Key has already been used with a different request payload"
                ),
            },
        )
    # 首次提交与重放统一返回 201；重放额外给出提示响应头
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
    return result


@app.post(
    "/imports/rfc5322",
    response_model=BatchResult,
    status_code=201,
    tags=["imports"],
    summary="导入一组 RFC 5322 原始邮件头，解析归一后一次性归档",
)
def import_rfc5322(payload: Rfc5322ImportIn) -> dict:
    try:
        return service.import_rfc5322(payload)
    except service.Rfc5322ImportErrors as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "one or more RFC 5322 header blocks are invalid; nothing was written",
                "errors": exc.errors,
            },
        )


@app.get(
    "/batches",
    tags=["batches"],
    summary="列出批次（按提交时间倒序）",
)
def list_batches(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    return service.list_batches(limit, offset)


@app.get(
    "/batches/{batch_id}",
    response_model=BatchResult,
    tags=["batches"],
    summary="按批次 ID 查询归档结果（线程安全的快照）",
)
def get_batch(batch_id: str) -> dict:
    result = service.get_batch(batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return result


@app.get(
    "/batches/{batch_id}/drift",
    response_model=BatchDriftReport,
    tags=["batches"],
    summary="批次漂移审计：以该批次快照的原始会话树为基线，只读逐封判定漂移状态",
)
def audit_batch_drift(batch_id: str) -> dict:
    result = service.audit_batch_drift(batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return result


@app.post(
    "/batches/{batch_id}/rollback-preview",
    response_model=BatchRollbackPreview,
    tags=["batches"],
    summary=(
        "批次撤销预演：模拟撤销该批次首次归档且当前仍归属该批次的有效邮件，"
        "按保留期清理的同一套压缩语义在内存中为重挂存活子孙（只读，不写库）"
    ),
)
def preview_batch_rollback(batch_id: str) -> dict:
    result = service.preview_batch_rollback(batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return result


@app.post(
    "/relinks",
    response_model=RelinkResult,
    tags=["messages"],
    summary="迟到邮件归档后，批量修复此前断链的有效邮件",
)
def relink_messages(payload: RelinkIn) -> dict:
    try:
        return service.relink_messages(payload)
    except service.RelinkNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"message": "message not found", "message_ids": exc.message_ids},
        )
    except service.RelinkCycleConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "relink would create a reference cycle; no changes were written",
                "cycles": exc.cycles,
            },
        )


@app.get(
    "/messages/{message_id}",
    response_model=MessageOut,
    tags=["messages"],
    summary="按 message_id 查询邮件及其最终父节点",
)
def get_message(message_id: str) -> dict:
    msg = service.get_message(message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    return msg


@app.get(
    "/threads/{message_id}",
    response_model=ThreadNodeOut,
    tags=["threads"],
    summary="查询某邮件所属的完整会话树与当前拓扑 revision",
)
def get_thread(message_id: str) -> dict:
    result = service.get_thread(message_id)
    if result is None:
        raise HTTPException(status_code=404, detail="message not found")
    if result.get("invalid"):
        raise HTTPException(
            status_code=409,
            detail="message belongs to a reference cycle and is not attached to any thread",
        )
    return result


@app.post(
    "/thread-corrections",
    response_model=ThreadCorrectionResult,
    tags=["threads"],
    summary="人工会话纠偏：调整一封有效邮件的父节点（null 拆成根），带 revision 乐观校验",
)
def correct_thread(payload: ThreadCorrectionIn) -> dict:
    try:
        return service.correct_thread(payload)
    except service.CorrectionMessageNotFound:
        raise HTTPException(status_code=404, detail="message not found")
    except service.CorrectionParentNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"message": "new parent not found", "new_parent_id": exc.parent_id},
        )
    except service.CorrectionInvalidMessage:
        raise HTTPException(
            status_code=409,
            detail="message belongs to a reference cycle and is not attached to any thread",
        )
    except service.CorrectionInvalidParent:
        raise HTTPException(
            status_code=409,
            detail="new_parent_id belongs to a reference cycle and is not attached to any thread",
        )
    except service.CorrectionRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "revision_conflict",
                "message": (
                    "expected_revision does not match the current thread topology; "
                    "no changes were written"
                ),
                "current_revision": exc.current_revision,
            },
        )
    except service.CorrectionCycleConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "correction would create a reference cycle; no changes were written",
                "cycles": exc.cycles,
            },
        )


@app.get(
    "/threads/{message_id}/page",
    tags=["threads"],
    summary="游标分页查询会话树（先序扁平展开，含 depth）",
)
def get_thread_page(
    message_id: str,
    limit: int = Query(50, ge=1, le=200, description="每页条数（1～200）"),
    cursor: Optional[str] = Query(None, description="上一页返回的 next_cursor；首次请求留空"),
) -> dict:
    try:
        result = service.get_thread_page(message_id, limit, cursor)
    except service.CursorMalformed:
        raise HTTPException(status_code=422, detail="cursor is malformed")
    except service.CursorThreadMismatch:
        raise HTTPException(
            status_code=422,
            detail="cursor does not belong to the thread of the path message_id",
        )
    except service.CursorStale:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "cursor_stale",
                "message": "the thread has changed since the cursor was issued; restart from the first page",
            },
        )
    if result is None:
        raise HTTPException(status_code=404, detail="message not found")
    if result.get("invalid"):
        raise HTTPException(
            status_code=409,
            detail="message belongs to a reference cycle and is not attached to any thread",
        )
    return result


@app.post(
    "/thread-sla/evaluate",
    response_model=ThreadSlaReport,
    tags=["threads"],
    summary="只读评估某邮件当前整棵会话的响应时限（SLA）",
)
def evaluate_thread_sla(payload: ThreadSlaEvaluateIn) -> dict:
    result = service.evaluate_thread_sla(payload)
    if result is None:
        raise HTTPException(status_code=404, detail="message not found")
    if result.get("invalid"):
        raise HTTPException(
            status_code=409,
            detail="message belongs to a reference cycle and is not attached to any thread",
        )
    return result


@app.post(
    "/thread-subject-audit",
    response_model=ThreadSubjectAuditReport,
    tags=["threads"],
    summary="只读审计某邮件当前整棵会话的主题漂移",
)
def audit_thread_subject(payload: ThreadSubjectAuditIn) -> dict:
    result = service.audit_thread_subject(payload)
    if result is None:
        raise HTTPException(status_code=404, detail="message not found")
    if result.get("invalid"):
        raise HTTPException(
            status_code=409,
            detail="message belongs to a reference cycle and is not attached to any thread",
        )
    return result


# ---------------------------------------------------------------------------
# 保留期：会话冻结 / 解除冻结 / 批量清理
# ---------------------------------------------------------------------------


@app.post(
    "/retention/freezes",
    response_model=FreezeResult,
    tags=["retention"],
    summary="活跃冻结某封有效邮件当前所在的整棵会话，使其不被保留期清理删除",
)
def freeze_thread(payload: FreezeIn) -> dict:
    try:
        return service.freeze_thread(payload)
    except service.RetentionAnchorNotFound:
        raise HTTPException(status_code=404, detail="message not found")
    except service.RetentionAnchorInvalid:
        raise HTTPException(
            status_code=409,
            detail="message belongs to a reference cycle and is not attached to any thread",
        )


@app.post(
    "/retention/unfreezes",
    response_model=UnfreezeResult,
    tags=["retention"],
    summary="解除以某封邮件为锚点的会话冻结",
)
def unfreeze_thread(payload: UnfreezeIn) -> dict:
    try:
        return service.unfreeze_thread(payload)
    except service.RetentionFreezeNotFound:
        raise HTTPException(
            status_code=404, detail="no freeze found anchored at this message"
        )


@app.post(
    "/retention/cleanups",
    response_model=CleanupResult,
    tags=["retention"],
    summary="批量清理截止时间前已到期且未受冻结保护的有效邮件（可 dry_run 预览）",
)
def cleanup_messages(payload: CleanupIn) -> dict:
    return service.cleanup_messages(payload)
