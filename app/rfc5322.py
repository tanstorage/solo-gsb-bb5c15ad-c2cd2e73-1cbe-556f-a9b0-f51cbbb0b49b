"""RFC 5322 原始邮件头解析（仅使用 Python 标准库）。

输入为一组原始邮件头文本（可含正文，正文前的空行之后一律忽略），输出归一
后的邮件字段，供 ``POST /imports/rfc5322`` 复用现有批次归档服务写入。

解析要点：

- **折行（folding）**：续行（CRLF 后接 WSP）由 ``email.parser`` 与
  ``policy.default`` 按 RFC 规则展开；
- **重复字段**：同名字段按头中出现顺序全部保留（``raw_items``）；
- **RFC 2047 编码词**：``Subject`` 等非结构化字段由 ``policy.default``
  自动解码（``=?charset?b/q?…?=``）；
- **候选 ID**：``Message-ID`` / ``In-Reply-To`` / ``References`` 中的
  msg-id 一律从尖括号 token（``<local@domain>``）提取，剥掉 CFWS
  （空白与嵌套注释）后按 addr-spec 校验，多个候选严格按其在头中出现的
  顺序处理——``Message-ID`` 取第一个合法候选，``In-Reply-To`` 同样取
  第一个合法候选（字段可空），``References`` 保留全部合法候选（保序、
  不去重，与 ``POST /batches`` 的 references 语义一致）；裸文本（无尖
  括号）不作为候选。
- 必填字段为 ``Message-ID``、``Date``、``Subject``：字段缺失记
  ``missing``，字段存在但无合法取值记 ``invalid``。

整组文本逐项解析、错误全部收集（带输入数组下标）；任一项存在错误即抛
``Rfc5322ParseError``，调用方不得写库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.errors import InvalidDateDefect
from email.message import Message
from email.parser import Parser
from typing import Optional

# ---------------------------------------------------------------------------
# addr-spec（msg-id 尖括号内的内容）校验
# ---------------------------------------------------------------------------

# atext（RFC 5321/5322 dot-atom 允许字符）
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_DOT_ATOM = _ATEXT + r"(?:\." + _ATEXT + r")*"
# 带引号的 local-part：可打印 ASCII（32-126）去掉引号/反斜杠，或 quoted-pair
_QUOTED = r'"(?:[ !#-\[\]-~]|\\.)*"'
_LOCAL = rf"(?:{_DOT_ATOM}|{_QUOTED})"
# 域名：dot-atom 或标准 domain-literal（[ ] 之间为 dtext，33-90 / 94-126）
_DOMAIN = rf"(?:{_DOT_ATOM}|\[[!-Z^-~]*\])"
_ADDRSPEC_RE = re.compile(rf"(?:{_LOCAL})@(?:{_DOMAIN})\Z")

# 折叠展开：仅删掉续行前的 CRLF，保留其 WSP（随后作为 CFWS 处理）
_FOLD_RE = re.compile(r"\r?\n(?=[ \t])")


@dataclass(frozen=True)
class ParsedHeader:
    """单封邮件头归一结果（sent_at 已换算到 UTC）。"""

    message_id: str
    in_reply_to: Optional[str]
    references: tuple[str, ...]
    sent_at: datetime
    subject: str


@dataclass(frozen=True)
class FieldError:
    """一项输入的一个字段错误；index 为请求 headers 数组的下标（从 0 起）。"""

    index: int
    field: str
    code: str
    message: str

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "field": self.field,
            "code": self.code,
            "message": self.message,
        }


class Rfc5322ParseError(Exception):
    """整组头文本中存在解析失败项；errors 已按 (index, 字段顺序) 排序。"""

    def __init__(self, errors: list[FieldError]) -> None:
        self.errors = errors
        super().__init__(f"RFC 5322 header parse failed for {len(errors)} field(s)")


# 必填字段在错误明细中的稳定排列顺序
_FIELD_ORDER = {"message_id": 0, "date": 1, "subject": 2}


def _unfold(value: str) -> str:
    """展开 RFC 折叠（删除续行 CRLF，保留引导 WSP）。"""

    return _FOLD_RE.sub("", value)


def _remove_comments(token: str) -> str:
    """删除嵌套括号注释（反斜杠转义下一字符，quoted-string 原样保留）。"""

    out: list[str] = []
    in_quote = False
    depth = 0
    i = 0
    n = len(token)
    while i < n:
        ch = token[i]
        if in_quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(token[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quote = False
        elif ch == '"':
            in_quote = True
            out.append(ch)
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
        i += 1
    return "".join(out)


def _remove_unquoted_fws(text: str) -> str:
    """删除引号外的全部空白（FWS）；引号内空白保留。"""

    out: list[str] = []
    in_quote = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quote = False
        elif ch == '"':
            in_quote = True
            out.append(ch)
        elif not ch.isspace():
            out.append(ch)
        i += 1
    return "".join(out)


def _unquoted_fws_allowed(text: str) -> bool:
    """引号外空白是否只出现在合法 CFWS 位置（首尾、``@`` / ``.`` 两侧）。

    RFC 5322 的 ``msg-id`` 中，FWS（含折行展开后残留的空白）只能位于
    dot-atom 之间的 CFWS 位置（``@`` 或 ``.`` 两侧）或整体首尾；出现
    在 atom 字符中间（如 ``a@b extra``）即非法。引号内空白不参与判断。
    """

    s = text.strip()
    in_quote = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_quote = False
            i += 1
            continue
        if ch == '"':
            in_quote = True
        elif ch.isspace():
            j = i + 1
            while j < n and s[j].isspace():
                j += 1
            prev_c = s[i - 1] if i > 0 else ""
            next_c = s[j] if j < n else ""
            if prev_c not in "@." and next_c not in "@.":
                return False
            i = j
            continue
        i += 1
    return True


def _valid_addrspec(inner: str) -> Optional[str]:
    """校验单个尖括号内 token，返回归一 addr-spec；非法返回 None。"""

    no_comments = _remove_comments(inner)
    if not _unquoted_fws_allowed(no_comments):
        return None
    candidate = _remove_unquoted_fws(no_comments)
    if candidate and _ADDRSPEC_RE.fullmatch(candidate):
        return candidate
    return None


def _extract_angle_tokens(value: str) -> list[str]:
    """提取字段值顶层成对尖括号中的原始内容（注释 / 引号感知）。

    注释（可嵌套、反斜杠转义）与 quoted-string 内的尖括号不作为分隔符；
    不成对、或内部再嵌套尖括号的内容跳过（格式非法，调用方继续尝试
    后续候选）。
    """

    tokens: list[str] = []
    depth = 0                       # 注释嵌套深度
    in_quote = False
    current: Optional[list[str]] = None
    broken = False                  # 当前候选是否出现过嵌套 <
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if in_quote:
            if current is not None:
                current.append(ch)
            if ch == "\\" and i + 1 < n:
                if current is not None:
                    current.append(value[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quote = False
        elif depth > 0:
            # 注释内：跳过转义对与嵌套括号，任何字符都不作为分隔符
            if ch == "\\":
                i += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        elif ch == '"':
            in_quote = True
            if current is not None:
                current.append(ch)
        elif ch == "(":
            depth += 1
        elif ch == "<":
            if current is None:
                current = []
                broken = False
            else:
                broken = True       # 嵌套尖括号：整段判非法
                current.append(ch)
        elif ch == ">":
            if current is not None:
                if not broken:
                    tokens.append("".join(current))
                current = None
        elif current is not None:
            current.append(ch)
        i += 1
    # 值结束仍有未闭合尖括号：该候选丢弃
    return tokens


def _iter_candidate_ids(values: list[str]):
    """按字段值顺序、再按尖括号出现顺序产出**合法**候选 addr-spec。

    多个候选严格按头中出现顺序处理；注释里的伪 token、引号里的尖括号、
    嵌套/未闭合尖括号以及非 addr-spec 内容均跳过，交给后续候选。
    """

    for value in values:
        for inner in _extract_angle_tokens(value):
            candidate = _valid_addrspec(inner)
            if candidate is not None:
                yield candidate


def _first_valid_id(values: list[str]) -> Optional[str]:
    """多个候选 ID 按出现顺序处理，取第一个格式合法者。"""

    return next(iter(_iter_candidate_ids(values)), None)


def _all_valid_ids(values: list[str]) -> list[str]:
    """收集全部合法候选 ID，保序、不去重。"""

    return list(_iter_candidate_ids(values))


def _header_groups(msg: Message) -> dict[str, list[str]]:
    """按字段名（小写）聚合全部重复出现的原始值，保持头中顺序并展开折行。"""

    groups: dict[str, list[str]] = {}
    for name, raw_value in msg.raw_items():
        groups.setdefault(name.lower(), []).append(_unfold(raw_value))
    return groups


def _parse_one(index: int, raw: str) -> tuple[Optional[ParsedHeader], list[FieldError]]:
    """解析单封邮件头；返回归一结果（有错误时为 None）与该输入的字段错误。"""

    errors: list[FieldError] = []

    # 去掉前导空行：否则首个空行会让解析器把全部内容当成正文
    try:
        parser = Parser(policy=policy.default)
        msg = parser.parsestr(raw.lstrip())
        groups = _header_groups(msg)
        date_values = msg.get_all("date") or []
        subject_values = msg.get_all("subject") or []
    except Exception as exc:  # 极端畸形输入不应导致整批 500
        return None, [FieldError(
            index=index,
            field="message_id",
            code="invalid",
            message=f"header block cannot be parsed: {exc.__class__.__name__}",
        )]

    # ---- Message-ID（必填）：多个候选按出现顺序取第一个合法者 ----------
    mid_values = groups.get("message-id", [])
    message_id: Optional[str] = None
    if not mid_values:
        errors.append(FieldError(
            index=index,
            field="message_id",
            code="missing",
            message="required header Message-ID is absent",
        ))
    else:
        message_id = _first_valid_id(mid_values)
        if message_id is None:
            errors.append(FieldError(
                index=index,
                field="message_id",
                code="invalid",
                message="no well-formed msg-id (angle-bracketed addr-spec) found in Message-ID",
            ))

    # ---- Date（必填）：policy.default 解析 RFC 5322 日期 ---------------
    sent_at: Optional[datetime] = None
    if not date_values:
        errors.append(FieldError(
            index=index,
            field="date",
            code="missing",
            message="required header Date is absent",
        ))
    else:
        header = date_values[0]
        parsed_dt = None
        try:
            has_date_defect = any(
                isinstance(d, InvalidDateDefect) for d in header.defects
            )
            if not has_date_defect:
                parsed_dt = header.datetime
        except (ValueError, TypeError, AttributeError, IndexError):
            parsed_dt = None
        if parsed_dt is None:
            errors.append(FieldError(
                index=index,
                field="date",
                code="invalid",
                message="Date is not a valid RFC 5322 date-time",
            ))
        else:
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
            sent_at = parsed_dt.astimezone(timezone.utc)

    # ---- Subject（必填）：policy.default 解码 RFC 2047 编码词 -----------
    subject = ""
    if not subject_values:
        errors.append(FieldError(
            index=index,
            field="subject",
            code="missing",
            message="required header Subject is absent",
        ))
    else:
        # 重复 Subject 按出现顺序取第一个（编码词由 policy.default 解码）
        try:
            subject = str(subject_values[0]).strip()
        except (ValueError, TypeError):
            subject = ""
        if not subject:
            errors.append(FieldError(
                index=index,
                field="subject",
                code="invalid",
                message="Subject is empty or cannot be decoded",
            ))

    # ---- In-Reply-To（可选）/ References（可选）------------------------
    # 字段存在但无合法 token 时按缺省处理（可选字段不报错）。
    in_reply_to = _first_valid_id(groups.get("in-reply-to", []))
    references = tuple(_all_valid_ids(groups.get("references", [])))

    if errors:
        return None, errors
    return ParsedHeader(
        message_id=message_id,  # type: ignore[arg-type]
        in_reply_to=in_reply_to,
        references=references,
        sent_at=sent_at,  # type: ignore[arg-type]
        subject=subject,
    ), []


def parse_header_blocks(blocks: list[str]) -> list[ParsedHeader]:
    """逐项解析一组 RFC 5322 原始邮件头。

    全部输入解析成功时返回与输入同序的 ``ParsedHeader`` 列表；任一项缺
    失必填字段或字段格式非法，均收集为带输入序号的错误，最终统一抛出
    ``Rfc5322ParseError``（不返回任何部分结果，调用方不得写库）。
    """

    parsed: list[ParsedHeader] = []
    errors: list[FieldError] = []
    for index, raw in enumerate(blocks):
        item, item_errors = _parse_one(index, raw)
        if item_errors:
            errors.extend(item_errors)
        else:
            parsed.append(item)

    if errors:
        errors.sort(key=lambda e: (e.index, _FIELD_ORDER[e.field]))
        raise Rfc5322ParseError(errors)
    return parsed
