"""请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class MessageIn(BaseModel):
    message_id: str = Field(..., min_length=1, description="RFC 2822 Message-ID")
    in_reply_to: Optional[str] = Field(None, description="直接回复的邮件 ID")
    references: list[str] = Field(default_factory=list, description="引用链，按邮件头顺序")
    sent_at: datetime = Field(..., description="发送时间，无时区时按 UTC 处理")
    subject: str
    retain_until: Optional[datetime] = Field(
        None, description="保留截止时间（可选）；截止后且未被冻结保护的邮件可被清理；无时区按 UTC 处理"
    )

    @field_validator("message_id", "in_reply_to", "subject")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("references")
    @classmethod
    def _strip_refs(cls, v: list[str]) -> list[str]:
        return [r.strip() for r in v if r and r.strip()]

    def normalized(self) -> "MessageIn":
        """把时间统一换算到 UTC（naive datetime 视为 UTC）。"""

        update: dict[str, datetime] = {}
        dt = self.sent_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        update["sent_at"] = dt.astimezone(timezone.utc)
        if self.retain_until is not None:
            ru = self.retain_until
            if ru.tzinfo is None:
                ru = ru.replace(tzinfo=timezone.utc)
            update["retain_until"] = ru.astimezone(timezone.utc)
        return self.model_copy(update=update)


class BatchIn(BaseModel):
    messages: list[MessageIn] = Field(..., min_length=1)


class Rfc5322ImportIn(BaseModel):
    """POST /imports/rfc5322：一组 RFC 5322 原始邮件头文本（按下标顺序归档）。"""

    headers: list[str] = Field(
        ...,
        min_length=1,
        description="每个元素是一封邮件的原始头文本（可含折行、重复字段、RFC 2047 编码词；空行后的正文忽略）",
    )


class RelinkIn(BaseModel):
    """POST /relinks：待补链的 message_id 列表。"""

    message_ids: list[str] = Field(
        ..., min_length=1, description="待修复断链邮件的 message_id（自动去空白、去重，保序处理）"
    )

    @field_validator("message_ids")
    @classmethod
    def _strip_ids(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw in v:
            mid = raw.strip()
            if mid and mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out

    @model_validator(mode="after")
    def _require_nonempty(self) -> "RelinkIn":
        if not self.message_ids:
            raise ValueError("message_ids must contain at least one non-empty id")
        return self


class TreeNode(BaseModel):
    message_id: str
    parent_id: Optional[str]
    in_reply_to: Optional[str]
    references: list[str]
    sent_at: datetime
    subject: str
    children: list["TreeNode"] = Field(default_factory=list)


class DuplicateOut(BaseModel):
    message_id: str
    archived: bool = Field(..., description="True 表示该 ID 已在此前批次归档")
    first_submitted_seq: Optional[int]
    occurrences: list[int]


class MissingRefsOut(BaseModel):
    message_id: str
    missing: list[str]


class CycleOut(BaseModel):
    index: int
    members: list[str]
    invalid_messages: list[str]


class InvalidMessageOut(BaseModel):
    message_id: str
    in_reply_to: Optional[str]
    references: list[str]
    sent_at: datetime
    subject: str
    reason: str
    cycle_index: int


class BatchSummary(BaseModel):
    batch_id: str
    submitted_at: datetime
    total_count: int
    unique_count: int
    invalid_count: int
    tree_count: int


class BatchResult(BaseModel):
    batch_id: str
    submitted_at: datetime
    totals: dict
    threads: list[TreeNode]
    duplicates: list[DuplicateOut]
    missing_references: list[MissingRefsOut]
    cycles: list[CycleOut]
    invalid_messages: list[InvalidMessageOut]


class MessageOut(BaseModel):
    message_id: str
    in_reply_to: Optional[str]
    references: list[str]
    sent_at: datetime
    subject: str
    parent_id: Optional[str]
    is_invalid: bool
    batch_id: str
    retain_until: Optional[datetime] = None
    frozen: bool = Field(False, description="当前是否被任一活跃会话冻结保护")


class ThreadNodeOut(BaseModel):
    """GET /threads/{message_id}：完整会话树 + 当前整棵会话拓扑的 revision。"""

    root_id: str
    tree: Optional["TreeNode"]
    revision: str = Field(
        ...,
        description=(
            "按当前整棵会话拓扑（每个节点的 message_id / parent_id / sent_at）"
            "计算的摘要；拓扑变化（补链、纠偏、清理压缩、新邮件并入）后改变"
        ),
    )


class ThreadCorrectionIn(BaseModel):
    """POST /thread-corrections：人工调整一封有效邮件的父节点。"""

    message_id: str = Field(..., min_length=1, description="待调整邮件 ID（必须是引用环外的有效邮件）")
    new_parent_id: Optional[str] = Field(
        None, description="新父节点 ID（必须为有效邮件）；null 表示把该邮件拆成新的根"
    )
    expected_revision: str = Field(
        ...,
        min_length=1,
        description="调用方最近一次 GET /threads/{message_id} 得到的 revision；与当前拓扑不匹配返回 409 且不写库",
    )

    @field_validator("message_id", "expected_revision")
    @classmethod
    def _strip_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("value must not be empty")
        return v

    @field_validator("new_parent_id")
    @classmethod
    def _strip_parent(cls, v: Optional[str]) -> Optional[str]:
        # null 是合法取值（拆成根）；空字符串/纯空白不是合法 ID
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("new_parent_id must be null or a non-empty message id")
        return v


class ThreadCorrectionResult(BaseModel):
    message_id: str
    old_parent_id: Optional[str] = Field(..., description="调整前的父节点（拆根前可能已在某节点下）")
    new_parent_id: Optional[str] = Field(..., description="调整后的父节点；null 表示已拆为根")
    old_root_id: str = Field(..., description="调整前该邮件所在会话的根")
    new_root_id: str = Field(..., description="调整后该邮件所在会话的根")
    revision: str = Field(
        ...,
        description="调整后该邮件当前所在整棵会话的新 revision（随即 GET /threads/{message_id} 得到同一值）",
    )


class RelinkItem(BaseModel):
    message_id: str
    old_parent_id: Optional[str]
    new_parent_id: Optional[str]
    old_root_id: str
    new_root_id: str


class RelinkResult(BaseModel):
    relinked: int = Field(..., description="实际发生父节点变更的邮件数")
    results: list[RelinkItem] = Field(..., description="按请求顺序逐封给出新旧父/根；未变更者新旧值相同")


# ---------------------------------------------------------------------------
# 保留期：会话冻结 / 解除冻结 / 批量清理
# ---------------------------------------------------------------------------


class FreezeIn(BaseModel):
    """POST /retention/freezes：以某封有效邮件为锚点活跃冻结其当前整棵会话。"""

    message_id: str = Field(..., min_length=1, description="冻结锚点邮件 ID")

    @field_validator("message_id")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message_id must not be empty")
        return v


class FreezeResult(BaseModel):
    anchor_id: str = Field(..., description="冻结锚点邮件 ID（会话内任一封邮件）")
    root_id: str = Field(..., description="冻结时刻该会话的根节点 ID")
    frozen_at: datetime = Field(..., description="冻结（或最近一次刷新）时间，UTC")
    created: bool = Field(
        ..., description="True=本次新建冻结；False=锚点已有冻结，已刷新冻结时间"
    )
    members: list[str] = Field(
        ...,
        description="锚点当前会话的全部成员 ID（活跃冻结随会话拓扑实时扩张），按先序排列",
    )


class UnfreezeIn(BaseModel):
    """POST /retention/unfreezes：解除以某封邮件为锚点的冻结。"""

    message_id: str = Field(..., min_length=1, description="冻结锚点邮件 ID（非保护成员）")

    @field_validator("message_id")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message_id must not be empty")
        return v


class UnfreezeResult(BaseModel):
    anchor_id: str
    root_id: str = Field(..., description="解除时刻锚点当前会话的根 ID")
    frozen_at: datetime
    released_members: list[str] = Field(
        ..., description="本次解除后不再被任何冻结覆盖的成员 ID（按解除时当前保护关系计算）"
    )
    still_protected: list[str] = Field(
        ..., description="本次解除的成员中仍被其它冻结覆盖、清理时依旧受保护的 ID"
    )


class CleanupIn(BaseModel):
    """POST /retention/cleanups：批量清理到期且未受保护的有效邮件。"""

    cutoff: Optional[datetime] = Field(
        None, description="保留截止判定时刻，缺省为当前 UTC；retain_until <= cutoff 视为到期"
    )
    dry_run: bool = Field(
        False, description="true 时使用同一算法计算结果但不写库"
    )

    def normalized_cutoff(self) -> Optional[datetime]:
        """cutoff 统一换算到 UTC（naive datetime 视为 UTC）。"""

        if self.cutoff is None:
            return None
        c = self.cutoff
        if c.tzinfo is None:
            c = c.replace(tzinfo=timezone.utc)
        return c.astimezone(timezone.utc)


class CleanupItem(BaseModel):
    """压缩后父节点或根节点发生变化的存活节点。"""

    message_id: str
    old_parent_id: Optional[str]
    new_parent_id: Optional[str]
    old_root_id: str
    new_root_id: str


class CleanupResult(BaseModel):
    dry_run: bool
    cutoff: datetime = Field(..., description="本次判定实际使用的截止时刻，UTC")
    deleted_ids: list[str] = Field(..., description="将被/已被删除的邮件 ID，升序")
    affected: list[CleanupItem] = Field(
        ..., description="存活但父节点或根节点变化的邮件，按 message_id 升序"
    )
    deleted_count: int
    affected_count: int
    expired_count: int = Field(..., description="有效邮件中已到期（retain_until <= cutoff）的数量")
    protected_count: int = Field(
        ..., description="已到期但被冻结保护而未删除的数量"
    )


# ---------------------------------------------------------------------------
# 会话响应时限（SLA）评估
# ---------------------------------------------------------------------------


class ThreadSlaEvaluateIn(BaseModel):
    """POST /thread-sla/evaluate：只读评估某邮件当前整棵会话的响应时限。"""

    message_id: str = Field(..., min_length=1, description="会话内任一有效邮件 ID（评估锚点）")
    as_of: datetime = Field(..., description="评估时刻；无时区按 UTC 处理")
    threshold_seconds: float = Field(..., ge=0, description="响应时限阈值（秒）")

    @field_validator("message_id")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message_id must not be empty")
        return v

    def normalized_as_of(self) -> datetime:
        """as_of 统一换算到 UTC（naive datetime 视为 UTC）。"""

        dt = self.as_of
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)


class ThreadSlaItem(BaseModel):
    """整棵会话中一封邮件的响应时限判定。"""

    message_id: str
    parent_id: Optional[str]
    sent_at: datetime
    depth: int
    status: Literal["within_sla", "late", "pending", "overdue"] = Field(
        ...,
        description=(
            "within_sla=已有有效响应且耗时不超过阈值；late=已有有效响应但超时；"
            "pending=尚无有效响应且年龄未超阈值；overdue=尚无有效响应且已超阈值"
        ),
    )
    response_message_id: Optional[str] = Field(
        None, description="有效响应（sent_at 不早于本邮件的最早直接子邮件）；无有效响应为 null"
    )
    response_sent_at: Optional[datetime] = None
    response_delay_seconds: Optional[float] = Field(
        None, description="响应耗时（秒）；无有效响应为 null"
    )
    age_seconds: Optional[float] = Field(
        None, description="无有效响应时以 as_of 计算的年龄（秒）；已有有效响应为 null"
    )


class ChronologyAnomalyOut(BaseModel):
    """sent_at 早于父邮件的直接子邮件：时序异常，不得作为响应。"""

    message_id: str
    parent_id: str
    sent_at: datetime
    parent_sent_at: datetime


class ThreadSlaSummary(BaseModel):
    total: int
    within_sla: int
    late: int
    pending: int
    overdue: int


class ThreadSlaReport(BaseModel):
    """POST /thread-sla/evaluate：整棵会话的响应时限评估结论。"""

    message_id: str = Field(..., description="评估锚点邮件 ID")
    root_id: str = Field(..., description="评估时刻该邮件所在会话的根")
    as_of: datetime = Field(..., description="实际使用的评估时刻（UTC）")
    threshold_seconds: float
    revision: str = Field(
        ...,
        description="评估基于的当前整棵会话拓扑 revision（与 GET /threads/{message_id} 一致）",
    )
    results: list[ThreadSlaItem] = Field(..., description="逐封判定结果，按稳定先序排列")
    summary: ThreadSlaSummary = Field(..., description="按状态分类的计数汇总")
    chronology_anomalies: list[ChronologyAnomalyOut] = Field(
        ..., description="sent_at 早于父邮件的直接子邮件；不得作为响应、不消除超时"
    )


# ---------------------------------------------------------------------------
# 会话主题漂移审计
# ---------------------------------------------------------------------------


class ThreadSubjectAuditIn(BaseModel):
    """POST /thread-subject-audit：只读审计某邮件当前整棵会话的主题漂移。"""

    message_id: str = Field(..., min_length=1, description="会话内任一有效邮件 ID（审计锚点）")

    @field_validator("message_id")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message_id must not be empty")
        return v


class ThreadSubjectAuditItem(BaseModel):
    """整棵会话中一封邮件的主题漂移判定。"""

    message_id: str
    parent_id: Optional[str]
    depth: int
    subject: str = Field(..., description="归档时的原始主题")
    canonical_subject: str = Field(
        ..., description="重复剥离大小写不敏感的 Re/Fw/Fwd 前缀并折叠空白后的规范主题"
    )
    baseline_subject: Optional[str] = Field(
        None, description="分支基线：最近祖先（父节点）的规范主题；根节点为 null"
    )
    drifted: bool = Field(
        ..., description="规范主题是否偏离分支基线；根节点无基线、恒为 false"
    )


class SubjectDriftGroupOut(BaseModel):
    """先序序列中连续的一段偏离节点。"""

    members: list[str] = Field(..., description="先序连续的偏离节点 message_id")
    baseline_subject: str = Field(
        ..., description="该组首个节点所偏离的分支基线（其最近祖先的规范主题）"
    )


class ThreadSubjectAuditReport(BaseModel):
    """POST /thread-subject-audit：整棵会话的主题漂移审计结论。"""

    message_id: str = Field(..., description="审计锚点邮件 ID")
    root_id: str = Field(..., description="审计时刻该邮件所在会话的根")
    revision: str = Field(
        ...,
        description="审计基于的当前整棵会话拓扑 revision（与 GET /threads/{message_id} 一致）",
    )
    results: list[ThreadSubjectAuditItem] = Field(
        ..., description="逐节点判定结果，按稳定先序排列"
    )
    drift_groups: list[SubjectDriftGroupOut] = Field(
        ..., description="按先序合并的连续偏离节点分组"
    )


# ---------------------------------------------------------------------------
# 批次漂移审计
# ---------------------------------------------------------------------------


class BatchDriftItem(BaseModel):
    """批次快照 threads 森林中一个节点的漂移判定。"""

    message_id: str
    original_parent_id: Optional[str] = Field(
        ..., description="快照中该节点的父节点（根节点为 null）"
    )
    original_root_id: str = Field(..., description="快照中该节点所在树的根")
    current_parent_id: Optional[str] = Field(
        ..., description="当前父节点；已删除（deleted）为 null"
    )
    current_root_id: Optional[str] = Field(
        ..., description="当前所在树的根；已删除（deleted）为 null"
    )
    status: Literal[
        "deleted", "id_reused", "reparented", "root_changed", "unchanged"
    ] = Field(
        ...,
        description=(
            "deleted=当前不存在；id_reused=同 ID 当前属于其它批次（原归档被清理后重归档）；"
            "reparented=来源一致但父节点变化；root_changed=仅根节点变化；unchanged=未漂移"
        ),
    )


class BatchDriftSummary(BaseModel):
    total: int
    deleted: int
    id_reused: int
    reparented: int
    root_changed: int
    unchanged: int


class BatchDriftReport(BaseModel):
    """GET /batches/{batch_id}/drift：批次快照会话树的漂移审计结论。"""

    batch_id: str
    submitted_at: datetime = Field(..., description="批次归档时间（快照基线时刻），UTC")
    results: list[BatchDriftItem] = Field(
        ..., description="逐节点判定结果，按快照原始先序排列"
    )
    summary: BatchDriftSummary = Field(..., description="按状态分类的计数汇总")


# ---------------------------------------------------------------------------
# 批次撤销预演
# ---------------------------------------------------------------------------


class RollbackItem(BaseModel):
    """撤销压缩后父节点或根节点发生变化的存活邮件。"""

    message_id: str
    old_parent_id: Optional[str]
    new_parent_id: Optional[str]
    old_root_id: str
    new_root_id: str


class RollbackRootGroup(BaseModel):
    """撤销后由同一新根统领的一组存活邮件（仅含受撤销触及的旧会话）。"""

    root_id: str = Field(..., description="压缩后的新根 ID")
    size: int = Field(..., description="该分组（含新根本身）的邮件数")
    members: list[str] = Field(
        ..., description="组内全部邮件 ID，按撤销后结构的稳定先序排列"
    )


class RollbackSummary(BaseModel):
    """撤销预演计数汇总。"""

    deleted_count: int = Field(..., description="拟删除邮件数（本批首次归档且当前仍归属本批的有效邮件）")
    affected_count: int = Field(..., description="存活但父节点或根节点将变化的邮件数")
    skipped_invalid_count: int = Field(
        ..., description="本批归档的环内 invalid 邮件数（撤销不处理）"
    )
    skipped_reused_count: int = Field(
        ...,
        description=(
            "本批首次归档但当前已不归属本批的邮件数——原归档已被保留期清理删除、"
            "同 ID 由更晚批次重用（撤销不处理）"
        ),
    )
    touched_tree_count: int = Field(
        ..., description="撤销触及的旧会话树数量（删除集成员压缩前所属旧根去重）"
    )
    root_group_count: int = Field(..., description="撤销后的根分组数量")
    surviving_count: int = Field(
        ..., description="受触及旧会话中撤销后仍存活的邮件数（根分组成员总数）"
    )


class BatchRollbackPreview(BaseModel):
    """POST /batches/{batch_id}/rollback-preview：批次撤销预演结论。"""

    batch_id: str
    submitted_at: datetime = Field(..., description="拟撤销批次的归档时间，UTC")
    deleted_ids: list[str] = Field(
        ..., description="拟删除的邮件 ID（本批有效、当前仍归属本批），升序"
    )
    affected: list[RollbackItem] = Field(
        ..., description="存活但父节点或根节点将变化的邮件，按 message_id 升序"
    )
    root_groups: list[RollbackRootGroup] = Field(
        ...,
        description=(
            "撤销后的根分组：仅覆盖受撤销触及旧会话的存活子孙，按新根的"
            "稳定顺序（sent_at, message_id）排列，组内成员按撤销后先序排列"
        ),
    )
    summary: RollbackSummary = Field(..., description="计数汇总")


TreeNode.model_rebuild()
