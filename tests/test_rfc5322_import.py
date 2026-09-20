"""POST /imports/rfc5322：RFC 5322 原始头导入（折行、重复字段、编码词、错误定位）。"""

from __future__ import annotations


def _headers_a():
    return (
        "Received: by mx.example.com\r\n"
        "Message-ID: <root@example.com>\r\n"
        "Date: Fri, 18 Sep 2026 08:00:00 +0000\r\n"
        "Subject: =?UTF-8?B?5L2g5aW9?= =?UTF-8?Q?=20World?=\r\n"
        "\r\nignored body\r\n"
    )


def _headers_b():
    # References 折行 + 重复字段；In-Reply-To 多个候选 ID
    return (
        "Message-ID: <reply@example.com>\n"
        "In-Reply-To: <nope-not-bracketed> <root@example.com> <other@example.com>\n"
        "References: <root@example.com>\n"
        "\t<x@external>\n"
        "References: <root@example.com>\n"
        "Date: Fri, 18 Sep 2026 17:30:00 +0800\n"
        "Subject: Re:\n =?UTF-8?B?5L2g5aW9?= World\n"
    )


def test_import_basic_folding_encoded_words_and_threading(client):
    r = client.post("/imports/rfc5322", json={"headers": [_headers_a(), _headers_b()]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["totals"]["submitted"] == 2
    assert body["totals"]["trees"] == 1

    root = body["threads"][0]
    assert root["message_id"] == "root@example.com"
    assert root["subject"] == "你好 World"
    reply = root["children"][0]
    assert reply["message_id"] == "reply@example.com"
    # 主题折行 + RFC 2047 编码词
    assert reply["subject"] == "Re: 你好 World"
    # In-Reply-To 裸文本不合法 -> 取首个合法候选；references 保序不去重，含折行展开
    assert reply["in_reply_to"] == "root@example.com"
    assert reply["references"] == [
        "root@example.com",
        "x@external",
        "root@example.com",
    ]
    # +08:00 换算为 UTC
    assert reply["sent_at"] == "2026-09-18T09:30:00Z"

    # 复用既有批次模型：快照可查，邮件可查
    snap = client.get(f"/batches/{body['batch_id']}").json()
    assert snap == body
    got = client.get("/messages/reply@example.com").json()
    assert got["parent_id"] == "root@example.com"


def test_import_repeated_message_id_uses_first_well_formed(client):
    raw = (
        "Message-ID: garbage-no-angle\n"
        "Message-ID: <first@example.com> <second@example.com>\n"
        "Message-ID: <third@example.com>\n"
        "Date: 18 Sep 2026 08:00:00 GMT\n"
        "Subject: order\n"
    )
    body = client.post("/imports/rfc5322", json={"headers": [raw]}).json()
    assert body["threads"][0]["message_id"] == "first@example.com"


def test_import_cfws_comments_in_msg_id(client):
    raw = (
        "Message-ID: <(a comment) root@example.com (nested (deep)) >\n"
        "Date: Fri, 18 Sep 2026 08:00:00 GMT\n"
        "Subject: cfws\n"
    )
    body = client.post("/imports/rfc5322", json={"headers": [raw]}).json()
    assert body["threads"][0]["message_id"] == "root@example.com"


def test_import_fake_angle_token_in_comment_is_skipped(client):
    # 注释里的伪 msg-id 不是候选；其后真正的尖括号 token 才被采用
    raw = (
        "Message-ID: (<fake@nope>) <real@example.com>\n"
        "Date: Fri, 18 Sep 2026 08:00:00 GMT\n"
        "Subject: c\n"
    )
    body = client.post("/imports/rfc5322", json={"headers": [raw]}).json()
    assert body["threads"][0]["message_id"] == "real@example.com"

    # 只有注释伪 token 时整项拒绝
    raw_only_comment = (
        "Message-ID: (<fake@nope>)\n"
        "Date: Fri, 18 Sep 2026 08:00:00 GMT\n"
        "Subject: c\n"
    )
    r = client.post("/imports/rfc5322", json={"headers": [raw_only_comment]})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["code"] == "invalid"


def test_import_trailing_junk_after_valid_token_uses_first_candidate(client):
    # 第一个尖括号 token 合法即采用（多候选按出现顺序取首个合法者）
    raw = (
        "Message-ID: <a@b> (comment) extra\n"
        "Date: Fri, 18 Sep 2026 08:00:00 GMT\n"
        "Subject: c\n"
    )
    body = client.post("/imports/rfc5322", json={"headers": [raw]}).json()
    assert body["threads"][0]["message_id"] == "a@b"


def test_import_candidate_internal_garbage_rejected(client):
    # 尖括号内 atom 中间出现空白属于非法格式；后续合法候选仍按顺序兜底
    raw = (
        "Message-ID: <a@b extra> <good@example.com>\n"
        "Date: Fri, 18 Sep 2026 08:00:00 GMT\n"
        "Subject: c\n"
    )
    body = client.post("/imports/rfc5322", json={"headers": [raw]}).json()
    assert body["threads"][0]["message_id"] == "good@example.com"


def test_import_optional_in_reply_to_and_references_missing(client):
    raw = (
        "Message-ID: <solo@example.com>\n"
        "Date: Fri, 18 Sep 2026 08:00:00 GMT\n"
        "Subject: solo\n"
    )
    body = client.post("/imports/rfc5322", json={"headers": [raw]}).json()
    node = body["threads"][0]
    assert node["in_reply_to"] is None
    assert node["references"] == []


def test_import_errors_per_index_and_no_write(client):
    good = _headers_a()
    missing_mid = (
        "Date: Fri, 18 Sep 2026 08:00:00 GMT\n"
        "Subject: no id\n"
    )
    bad_date = (
        "Message-ID: <baddate@example.com>\n"
        "Date: yesterday-ish\n"
        "Subject: bad date\n"
    )
    empty_subject = (
        "Message-ID: <emptysubject@example.com>\n"
        "Date: Fri, 18 Sep 2026 08:00:00 GMT\n"
        "Subject:    \n"
    )

    r = client.post(
        "/imports/rfc5322",
        json={"headers": [good, missing_mid, bad_date, empty_subject]},
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    errors = detail["errors"]
    # index=0 合法；其余逐项带输入序号，按 (index, 字段顺序) 排列
    assert [(e["index"], e["field"], e["code"]) for e in errors] == [
        (1, "message_id", "missing"),
        (2, "date", "invalid"),
        (3, "subject", "invalid"),
    ]

    # 任一项失败：整批不写库
    assert client.get("/batches").json()["total"] == 0
    assert client.get("/messages/root@example.com").status_code == 404
    assert client.get("/messages/baddate@example.com").status_code == 404


def test_import_invalid_message_id_angle_but_bad_addr(client):
    raw = (
        "Message-ID: <not-an-addr-spec>\n"
        "Date: Fri, 18 Sep 2026 08:00:00 GMT\n"
        "Subject: x\n"
    )
    r = client.post("/imports/rfc5322", json={"headers": [raw]})
    assert r.status_code == 422
    err = r.json()["detail"]["errors"][0]
    assert err["index"] == 0
    assert err["field"] == "message_id"
    assert err["code"] == "invalid"


def test_import_duplicate_ids_go_through_batch_diagnostics(client):
    body = client.post(
        "/imports/rfc5322",
        json={"headers": [_headers_a(), _headers_a()]},
    ).json()
    assert body["totals"]["submitted"] == 2
    assert len(body["duplicates"]) == 1
    assert body["duplicates"][0]["message_id"] == "root@example.com"
    assert body["duplicates"][0]["occurrences"] == [1, 2]


def test_import_archived_duplicate_with_subsequent_batch(client):
    first = client.post("/imports/rfc5322", json={"headers": [_headers_a()]}).json()
    assert first["totals"]["unique_new"] == 1
    # 同一 ID 再次导入：走现有归档的“历史归档重复”诊断，不重复落库
    second = client.post("/imports/rfc5322", json={"headers": [_headers_a()]}).json()
    assert second["totals"]["submitted"] == 1
    assert second["totals"]["archived_duplicates"] == 1
    assert second["duplicates"][0]["archived"] is True


def test_import_empty_list_rejected(client):
    r = client.post("/imports/rfc5322", json={"headers": []})
    assert r.status_code == 422


def test_import_batches_endpoint_behavior_unchanged(client):
    # /batches 仍然可用，且不认识 headers 请求体之外的变化
    r = client.post("/batches", json={"messages": [
        {
            "message_id": "A",
            "in_reply_to": None,
            "references": [],
            "sent_at": "2026-09-18T08:00:00Z",
            "subject": "batches still works",
        }
    ]})
    assert r.status_code == 201
    assert r.json()["threads"][0]["message_id"] == "A"


def test_import_naive_date_treated_as_utc(client):
    raw = (
        "Message-ID: <naive@example.com>\n"
        # 不带时区的日期（罕见但可解析）：与 /batches 一致按 UTC 处理
        "Date: Fri, 18 Sep 2026 08:00:00\n"
        "Subject: naive\n"
    )
    body = client.post("/imports/rfc5322", json={"headers": [raw]}).json()
    assert body["threads"][0]["sent_at"] == "2026-09-18T08:00:00Z"
