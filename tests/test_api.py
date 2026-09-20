"""端到端 API 测试：提交、跨批归并、诊断、查询、断链补链。"""

from __future__ import annotations

import importlib


def _m(mid, irt=None, refs=None, sent="2026-09-18T10:00:00+00:00", subject="hello",
       retain_until=None):
    msg = {
        "message_id": mid,
        "in_reply_to": irt,
        "references": refs or [],
        "sent_at": sent,
        "subject": subject,
    }
    if retain_until is not None:
        msg["retain_until"] = retain_until
    return msg


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_submit_and_get_batch_snapshot(client):
    payload = {"messages": [
        _m("A", sent="2026-09-18T09:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T10:00:00+00:00"),
    ]}
    r = client.post("/batches", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["totals"]["submitted"] == 2
    assert body["totals"]["trees"] == 1

    # 快照查询结果一致
    again = client.get(f"/batches/{body['batch_id']}").json()
    assert again == body

    listed = client.get("/batches").json()
    assert listed["total"] == 1
    assert listed["batches"][0]["batch_id"] == body["batch_id"]


def test_missing_references_and_broken_chain(client):
    r = client.post("/batches", json={"messages": [
        _m("X", irt="nope", refs=["g1", "g2"]),
    ]}).json()

    assert r["threads"][0]["message_id"] == "X"
    assert r["threads"][0]["parent_id"] is None
    assert r["missing_references"] == [
        {"message_id": "X", "missing": ["g1", "g2", "nope"]}
    ]


def test_full_cycle_invalid_and_no_partial_attach(client):
    body = client.post("/batches", json={"messages": [
        _m("a", irt="c", refs=["c"], sent="2026-09-18T01:00:00+00:00"),
        _m("b", irt="a", refs=["c", "a"], sent="2026-09-18T02:00:00+00:00"),
        _m("c", irt="b", refs=["c", "a", "b"], sent="2026-09-18T03:00:00+00:00"),
    ]}).json()

    assert body["threads"] == []
    assert body["totals"]["invalid"] == 3
    assert body["totals"]["cycles"] == 1
    cyc = body["cycles"][0]
    assert set(cyc["members"]) == {"a", "b", "c"}
    assert sorted(i["message_id"] for i in body["invalid_messages"]) == ["a", "b", "c"]

    # 环邮件查询：消息存在但线程接口返回 409
    got = client.get("/messages/a").json()
    assert got["is_invalid"] is True
    assert got["parent_id"] is None
    resp = client.get("/threads/a")
    assert resp.status_code == 409


def test_duplicate_ids_in_batch(client):
    body = client.post("/batches", json={"messages": [
        _m("A"),
        _m("D", irt="A", sent="2026-09-18T11:00:00+00:00"),
        _m("D", irt=None, refs=[], sent="2026-09-18T12:00:00+00:00"),
    ]}).json()

    dups = body["duplicates"]
    assert len(dups) == 1
    assert dups[0]["message_id"] == "D"
    assert dups[0]["archived"] is False
    assert dups[0]["occurrences"] == [2, 3]
    assert body["totals"]["submitted"] == 3
    assert body["totals"]["unique_new"] == 2


def test_cross_batch_threading_and_archived_duplicate(client):
    b1 = client.post("/batches", json={"messages": [_m("A")]}).json()
    b2 = client.post("/batches", json={"messages": [
        _m("B", irt="A", sent="2026-09-18T11:00:00+00:00"),
        _m("A"),  # 已归档重复
    ]}).json()

    # B 跨批挂到历史 A 之下
    tree = b2["threads"][0]
    assert tree["message_id"] == "A"
    assert [c["message_id"] for c in tree["children"]] == ["B"]

    assert b2["duplicates"] == [{
        "message_id": "A",
        "archived": True,
        "first_submitted_seq": None,
        "occurrences": [2],
    }]
    assert b2["totals"]["archived_duplicates"] == 1

    # 线程查询返回完整跨批树
    t = client.get("/threads/B").json()
    assert t["root_id"] == "A"
    assert t["tree"]["children"][0]["message_id"] == "B"


def test_cycle_tail_falls_back_to_valid_reference(client):
    body = client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T00:00:00+00:00"),
        _m("x", irt="y", refs=["y"], sent="2026-09-18T01:00:00+00:00"),
        _m("y", irt="x", refs=["x"], sent="2026-09-18T02:00:00+00:00"),
        _m("z", irt="y", refs=["R"], sent="2026-09-18T03:00:00+00:00"),
    ]}).json()

    # x/y 整环 invalid；z 回退挂到 R
    assert body["totals"]["invalid"] == 2
    tree = body["threads"][0]
    assert tree["message_id"] == "R"
    assert [c["message_id"] for c in tree["children"]] == ["z"]


def test_message_not_found(client):
    assert client.get("/messages/zzz").status_code == 404
    assert client.get("/threads/zzz").status_code == 404
    assert client.get("/batches/zzz").status_code == 404


def test_empty_batch_rejected(client):
    assert client.post("/batches", json={"messages": []}).status_code == 422


# ---------------------------------------------------------------------------
# POST /relinks：迟到邮件归档后批量修复断链
# ---------------------------------------------------------------------------


def test_relink_uses_late_in_reply_to_and_updates_threads_and_roots(client):
    b1 = client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("D", irt="late", refs=["ghost"], sent="2026-09-18T09:00:00+00:00"),
    ]}).json()
    assert client.get("/messages/D").json()["parent_id"] is None

    # 迟到邮件归档（其本身挂到 A 下）
    client.post("/batches", json={"messages": [
        _m("late", irt="A", refs=["A"], sent="2026-09-18T07:00:00+00:00"),
    ]})

    r = client.post("/relinks", json={"message_ids": ["D"]})
    assert r.status_code == 200
    body = r.json()
    assert body["relinked"] == 1
    assert body["results"] == [{
        "message_id": "D",
        "old_parent_id": None,
        "new_parent_id": "late",
        "old_root_id": "D",
        "new_root_id": "A",
    }]

    # GET /threads 随即反映新树：A -> late -> D
    tree = client.get("/threads/D").json()
    assert tree["root_id"] == "A"
    assert tree["tree"]["message_id"] == "A"
    assert [c["message_id"] for c in tree["tree"]["children"]] == ["late"]
    assert [g["message_id"] for g in tree["tree"]["children"][0]["children"]] == ["D"]
    assert client.get("/messages/D").json()["parent_id"] == "late"

    # 既有批次结果快照保持不变：快照里 D 仍是断链根
    snap = client.get(f"/batches/{b1['batch_id']}").json()
    snap_d = [t for t in snap["threads"] if t["message_id"] == "D"][0]
    assert snap_d["parent_id"] is None
    assert snap == b1


def test_relink_is_idempotent(client):
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("D", irt="A-missing", refs=["late-A"], sent="2026-09-18T09:00:00+00:00"),
    ]})
    client.post("/batches", json={"messages": [
        _m("late-A", sent="2026-09-18T07:00:00+00:00"),
    ]})
    first = client.post("/relinks", json={"message_ids": ["D"]}).json()
    assert first["relinked"] == 1

    again = client.post("/relinks", json={"message_ids": ["D"]}).json()
    assert again["relinked"] == 0
    assert again["results"][0] == {
        "message_id": "D",
        "old_parent_id": "late-A",
        "new_parent_id": "late-A",
        "old_root_id": "late-A",
        "new_root_id": "late-A",
    }


def test_relink_falls_back_through_references_right_to_left(client):
    # 归档时所有引用都不存在 -> 断链根；迟到两个候选后补链，取 references 最右已知项
    client.post("/batches", json={"messages": [
        _m("F", irt="still-missing", refs=["g1", "late1", "late2"],
           sent="2026-09-18T10:00:00+00:00"),
    ]})
    client.post("/batches", json={"messages": [
        _m("late1", sent="2026-09-18T06:00:00+00:00"),
        _m("late2", irt="late1", refs=["late1"], sent="2026-09-18T07:00:00+00:00"),
    ]})
    body = client.post("/relinks", json={"message_ids": ["F"]}).json()
    item = body["results"][0]
    assert item["old_parent_id"] is None
    assert item["new_parent_id"] == "late2"
    assert item["old_root_id"] == "F"
    assert item["new_root_id"] == "late1"


def test_relink_no_candidate_stays_unchanged(client):
    client.post("/batches", json={"messages": [
        _m("G", irt="nope", refs=["n1", "n2"], sent="2026-09-18T10:00:00+00:00"),
    ]})
    body = client.post("/relinks", json={"message_ids": ["G"]}).json()
    assert body["relinked"] == 0
    assert body["results"][0] == {
        "message_id": "G",
        "old_parent_id": None,
        "new_parent_id": None,
        "old_root_id": "G",
        "new_root_id": "G",
    }
    assert client.get("/messages/G").json()["parent_id"] is None


def test_relink_skips_already_attached_and_invalid(client):
    client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T00:00:00+00:00"),
        _m("H", irt="R", refs=["R"], sent="2026-09-18T11:00:00+00:00"),
        _m("cyc1", irt="cyc2", refs=["cyc2"], sent="2026-09-18T12:00:00+00:00"),
        _m("cyc2", irt="cyc1", refs=["cyc1"], sent="2026-09-18T13:00:00+00:00"),
    ]})
    body = client.post("/relinks", json={"message_ids": ["H", "cyc1"]}).json()
    assert body["relinked"] == 0
    assert body["results"] == [
        {"message_id": "H", "old_parent_id": "R", "new_parent_id": "R",
         "old_root_id": "R", "new_root_id": "R"},
        {"message_id": "cyc1", "old_parent_id": None, "new_parent_id": None,
         "old_root_id": "cyc1", "new_root_id": "cyc1"},
    ]


def test_relink_unknown_id_returns_404_and_writes_nothing(client):
    client.post("/batches", json={"messages": [_m("A")]})
    r = client.post("/relinks", json={"message_ids": ["A", "ghost-id"]})
    assert r.status_code == 404
    assert r.json()["detail"]["message_ids"] == ["ghost-id"]
    # A 即便本可命中也整批不处理
    assert client.get("/messages/A").json()["parent_id"] is None


def test_relink_empty_and_blank_ids_rejected(client):
    assert client.post("/relinks", json={"message_ids": []}).status_code == 422
    assert client.post("/relinks", json={"message_ids": ["   "]}).status_code == 422


def test_relink_dedupes_ids_and_preserves_order(client):
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("G", irt="nope", refs=["n1"], sent="2026-09-18T09:00:00+00:00"),
    ]})
    body = client.post("/relinks", json={"message_ids": ["G", "A", "G", " A "]}).json()
    assert [r["message_id"] for r in body["results"]] == ["G", "A"]


def _open_db():
    """按当前 DATABASE_PATH 直连测试库（绕过服务连接）。"""
    import sqlite3

    from app import config

    importlib.reload(config)
    conn = sqlite3.connect(config.settings.database_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_relink_merged_cycle_returns_409_and_writes_nothing(client):
    # 真实归档不会自然产生两个互指的有效根；直接构造该初始状态，
    # 验证候选关系合并后统一检测环、整次 409 且不写库。
    client.post("/batches", json={"messages": [_m("p", sent="2026-09-18T14:00:00+00:00")]})
    client.post("/batches", json={"messages": [_m("q", sent="2026-09-18T15:00:00+00:00")]})
    conn = _open_db()
    try:
        conn.execute("UPDATE messages SET in_reply_to='q', \"references\"='[\"q\"]' WHERE message_id='p'")
        conn.execute("UPDATE messages SET in_reply_to='p', \"references\"='[\"p\"]' WHERE message_id='q'")
        conn.commit()
    finally:
        conn.close()

    r = client.post("/relinks", json={"message_ids": ["p", "q"]})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["cycles"] == [["p", "q"]]

    conn = _open_db()
    try:
        rows = {row["message_id"]: row["parent_id"]
                for row in conn.execute("SELECT message_id, parent_id FROM messages")}
    finally:
        conn.close()
    assert rows == {"p": None, "q": None}

    # 只修一封可以成功；再修第二封时与既有边合并又成环 -> 409 且不写库
    assert client.post("/relinks", json={"message_ids": ["p"]}).json()["results"][0]["new_parent_id"] == "q"
    r2 = client.post("/relinks", json={"message_ids": ["q"]})
    assert r2.status_code == 409
    conn = _open_db()
    try:
        row = conn.execute("SELECT parent_id FROM messages WHERE message_id='q'").fetchone()
    finally:
        conn.close()
    assert row["parent_id"] is None


def test_relink_invalid_cycle_members_are_not_candidate_parents(client):
    # ca/cb 同批成环 invalid；M 只引用环成员、N 的 references 指向迟到邮件：
    # 环剔除后 M/N 都断链为根。补链时环成员同样不得作为候选父。
    client.post("/batches", json={"messages": [
        _m("ca", irt="cb", refs=["cb"], sent="2026-09-18T18:00:00+00:00"),
        _m("cb", irt="ca", refs=["ca"], sent="2026-09-18T18:10:00+00:00"),
        _m("M", irt="ca", refs=["ca"], sent="2026-09-18T18:20:00+00:00"),
        _m("N", irt="ca", refs=["lateV"], sent="2026-09-18T18:30:00+00:00"),
    ]})
    assert client.get("/messages/M").json()["parent_id"] is None
    assert client.get("/messages/N").json()["parent_id"] is None

    body = client.post("/relinks", json={"message_ids": ["M"]}).json()
    assert body["relinked"] == 0
    assert body["results"][0] == {
        "message_id": "M", "old_parent_id": None, "new_parent_id": None,
        "old_root_id": "M", "new_root_id": "M",
    }

    # lateV 迟到归档后，N 越过环成员回退到 references 中的有效项
    client.post("/batches", json={"messages": [
        _m("lateV", sent="2026-09-18T05:00:00+00:00"),
    ]})
    item = client.post("/relinks", json={"message_ids": ["N"]}).json()["results"][0]
    assert item["old_parent_id"] is None
    assert item["new_parent_id"] == "lateV"
    assert item["new_root_id"] == "lateV"


def test_relink_self_candidate_is_a_cycle(client):
    client.post("/batches", json={"messages": [_m("zz", sent="2026-09-18T16:00:00+00:00")]})
    conn = _open_db()
    try:
        conn.execute("UPDATE messages SET in_reply_to='zz', \"references\"='[]' WHERE message_id='zz'")
        conn.commit()
    finally:
        conn.close()
    r = client.post("/relinks", json={"message_ids": ["zz"]})
    assert r.status_code == 409
    assert r.json()["detail"]["cycles"] == [["zz"]]


# ---------------------------------------------------------------------------
# GET /threads/{message_id}/page：稳定游标分页
# ---------------------------------------------------------------------------


def _seed_paged_thread(client):
    """归档 R -> B -> C、R -> D 的会话；先序应为 R(0) B(1) C(2) D(1)。"""

    client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T08:00:00+00:00"),
        _m("B", irt="R", refs=["R"], sent="2026-09-18T09:00:00+00:00"),
        _m("C", irt="B", refs=["R", "B"], sent="2026-09-18T10:00:00+00:00"),
        _m("D", irt="R", refs=["R"], sent="2026-09-18T11:00:00+00:00"),
    ]})


def test_thread_page_first_page_is_flat_preorder_with_depth(client):
    _seed_paged_thread(client)
    r = client.get("/threads/R/page", params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["root_id"] == "R"
    assert body["limit"] == 2
    assert [(i["message_id"], i["depth"]) for i in body["items"]] == [
        ("R", 0),
        ("B", 1),
    ]
    assert body["next_cursor"]

    item = body["items"][1]
    assert set(item) == {
        "message_id", "parent_id", "in_reply_to", "references",
        "sent_at", "subject", "depth",
    }
    assert item["parent_id"] == "R"
    assert item["references"] == ["R"]


def test_thread_page_walks_all_pages_without_gaps_or_dupes(client):
    _seed_paged_thread(client)
    seen: list[tuple[str, int]] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/threads/D/page", params=params).json()
        pages += 1
        seen.extend((i["message_id"], i["depth"]) for i in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
        assert pages <= 3
    assert seen == [("R", 0), ("B", 1), ("C", 2), ("D", 1)]
    assert pages == 2  # 4 条 / 每页 2 条

    # 最后一页 next_cursor 为 null；重复使用倒数游标得到稳定的同一尾页
    last = client.get("/threads/R/page", params={"limit": 2}).json()
    mid_cursor = last["next_cursor"]
    tail1 = client.get("/threads/R/page", params={"limit": 2, "cursor": mid_cursor}).json()
    tail2 = client.get("/threads/R/page", params={"limit": 2, "cursor": mid_cursor}).json()
    assert tail1["next_cursor"] is None
    assert [i["message_id"] for i in tail1["items"]] == ["C", "D"]
    assert tail1 == tail2


def test_thread_page_large_limit_covers_everything_in_one_page(client):
    _seed_paged_thread(client)
    body = client.get("/threads/R/page", params={"limit": 200}).json()
    assert [i["message_id"] for i in body["items"]] == ["R", "B", "C", "D"]
    assert body["next_cursor"] is None


def test_thread_page_order_matches_nested_thread_preorder(client):
    _seed_paged_thread(client)

    def preorder(nodes):
        out = []
        for n in nodes:
            out.append(n["message_id"])
            out.extend(preorder(n["children"]))
        return out

    nested = client.get("/threads/C").json()["tree"]
    flat = client.get("/threads/R/page", params={"limit": 200}).json()["items"]
    assert [i["message_id"] for i in flat] == preorder([nested])


def test_thread_page_limit_bounds_validated(client):
    _seed_paged_thread(client)
    assert client.get("/threads/R/page", params={"limit": 0}).status_code == 422
    assert client.get("/threads/R/page", params={"limit": 201}).status_code == 422
    bad = client.get("/threads/R/page", params={"limit": "two"})
    assert bad.status_code == 422


def test_thread_page_unknown_message_returns_404(client):
    assert client.get("/threads/nope/page").status_code == 404


def test_thread_page_cycle_message_returns_409(client):
    client.post("/batches", json={"messages": [
        _m("x", irt="y", refs=["y"], sent="2026-09-18T12:00:00+00:00"),
        _m("y", irt="x", refs=["x"], sent="2026-09-18T13:00:00+00:00"),
    ]})
    r = client.get("/threads/x/page")
    assert r.status_code == 409


def test_thread_page_malformed_cursor_returns_422(client):
    _seed_paged_thread(client)
    import base64
    import json as _json

    def tok(obj) -> str:
        return base64.urlsafe_b64encode(
            _json.dumps(obj).encode()
        ).rstrip(b"=").decode()

    for bad in [
        "not a cursor!!!",          # 非法字符
        tok({"v": 1}),               # 缺字段
        tok({"v": 2, "root_id": "R", "offset": 0, "revision": "x"}),  # 版本不符
        tok({"v": 1, "root_id": "R", "offset": -1, "revision": "x"}),  # 偏移为负
        tok({"v": 1, "root_id": 9, "offset": 0, "revision": "x"}),    # 类型错误
        tok(["not", "an", "object"]),
        "eyJhIjoxfQ",                # 任意 base64url 但非本游标结构
    ]:
        r = client.get("/threads/R/page", params={"limit": 2, "cursor": bad})
        assert r.status_code == 422, bad


def test_thread_page_cursor_for_other_thread_returns_422(client):
    _seed_paged_thread(client)
    # 另一棵互不相干的树 P
    client.post("/batches", json={"messages": [
        _m("P", sent="2026-09-19T08:00:00+00:00"),
        _m("Q", irt="P", refs=["P"], sent="2026-09-19T09:00:00+00:00"),
    ]})
    foreign = client.get("/threads/P/page", params={"limit": 1}).json()["next_cursor"]
    r = client.get("/threads/R/page", params={"limit": 2, "cursor": foreign})
    assert r.status_code == 422
    # 反向同样不允许
    home = client.get("/threads/R/page", params={"limit": 1}).json()["next_cursor"]
    assert client.get("/threads/P/page", params={"cursor": home}).status_code == 422


def test_thread_page_stale_cursor_after_relink_returns_409(client):
    # D 先归档、其父 late 尚未到达 -> D 断链为根；E 正常挂在 D 下
    client.post("/batches", json={"messages": [
        _m("D", irt="late", refs=["late"], sent="2026-09-18T09:00:00+00:00"),
        _m("E", irt="D", refs=["D"], sent="2026-09-18T10:00:00+00:00"),
    ]})
    # 迟到邮件随后归档，自成独立根，L2 挂在 late 下
    client.post("/batches", json={"messages": [
        _m("late", sent="2026-09-18T07:00:00+00:00"),
        _m("L2", irt="late", refs=["late"], sent="2026-09-18T07:30:00+00:00"),
    ]})

    # 游标 1：late 树（根不变，但补链后会并入 D）-> revision 不符
    cur_same_root = client.get(
        "/threads/late/page", params={"limit": 1}
    ).json()["next_cursor"]
    # 游标 2：D 当时自成根的树（补链后 D 下沉为 late 的子节点）
    cur_old_root = client.get(
        "/threads/D/page", params={"limit": 1}
    ).json()["next_cursor"]
    assert cur_same_root and cur_old_root

    # 补链：D 挂到 late 下，两棵树合并、结构改变
    r = client.post("/relinks", json={"message_ids": ["D"]})
    assert r.status_code == 200
    assert r.json()["results"][0]["new_parent_id"] == "late"

    stale1 = client.get(
        "/threads/late/page", params={"limit": 1, "cursor": cur_same_root}
    )
    assert stale1.status_code == 409
    assert stale1.json()["detail"]["error"] == "cursor_stale"

    stale2 = client.get(
        "/threads/D/page", params={"limit": 1, "cursor": cur_old_root}
    )
    assert stale2.status_code == 409
    assert stale2.json()["detail"]["error"] == "cursor_stale"

    # 不带游标从头开始即正常：late 树下按先序 late L2 D E
    fresh = client.get("/threads/D/page", params={"limit": 200}).json()
    assert [(i["message_id"], i["depth"]) for i in fresh["items"]] == [
        ("late", 0),
        ("L2", 1),
        ("D", 1),
        ("E", 2),
    ]
    assert fresh["next_cursor"] is None


def test_thread_page_stale_cursor_when_thread_grows_via_new_batch(client):
    _seed_paged_thread(client)
    cur = client.get("/threads/R/page", params={"limit": 2}).json()["next_cursor"]

    # 新批次又往同一棵树挂一封邮件，revision 必须改变
    client.post("/batches", json={"messages": [
        _m("E", irt="D", refs=["R", "D"], sent="2026-09-18T12:00:00+00:00"),
    ]})
    r = client.get("/threads/R/page", params={"limit": 2, "cursor": cur})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "cursor_stale"


def test_thread_page_endpoint_does_not_change_existing_thread_endpoint(client):
    _seed_paged_thread(client)
    tree = client.get("/threads/R").json()
    assert tree["root_id"] == "R"
    assert tree["tree"]["message_id"] == "R"
    assert [c["message_id"] for c in tree["tree"]["children"]] == ["B", "D"]
    assert tree["tree"]["children"][0]["children"][0]["message_id"] == "C"


# ---------------------------------------------------------------------------
# retain_until：归档与查询
# ---------------------------------------------------------------------------


def test_retain_until_is_accepted_normalized_and_returned(client):
    client.post("/batches", json={"messages": [
        _m("A", retain_until="2026-09-20T10:00:00+00:00"),
        _m("B", irt="A", refs=["A"], retain_until="2026-09-20T12:00:00"),  # naive -> UTC
        _m("C", irt="A", refs=["A"]),  # 未设置
    ]})
    a = client.get("/messages/A").json()
    assert a["retain_until"] == "2026-09-20T10:00:00Z"
    assert a["frozen"] is False
    b = client.get("/messages/B").json()
    assert b["retain_until"] == "2026-09-20T12:00:00Z"  # naive -> UTC
    c = client.get("/messages/C").json()
    assert c["retain_until"] is None
    assert c["frozen"] is False


# ---------------------------------------------------------------------------
# POST /retention/freezes、/retention/unfreezes
# ---------------------------------------------------------------------------


def _seed_retention_tree(client):
    """R -> A -> C；R -> D。"""
    client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T08:00:00+00:00"),
        _m("A", irt="R", refs=["R"], sent="2026-09-18T09:00:00+00:00"),
        _m("C", irt="A", refs=["R", "A"], sent="2026-09-18T10:00:00+00:00"),
        _m("D", irt="R", refs=["R"], sent="2026-09-18T11:00:00+00:00"),
    ]})


def test_freeze_protects_whole_current_thread(client):
    _seed_retention_tree(client)
    r = client.post("/retention/freezes", json={"message_id": "C"})
    assert r.status_code == 200
    body = r.json()
    assert body["anchor_id"] == "C"
    assert body["root_id"] == "R"
    assert body["created"] is True
    # 成员按先序：R A C D
    assert body["members"] == ["R", "A", "C", "D"]
    assert "frozen_at" in body

    assert client.get("/messages/R").json()["frozen"] is True
    assert client.get("/messages/D").json()["frozen"] is True


def test_freeze_is_active_and_covers_later_members_automatically(client):
    # 首次冻结时树只有 R -> A
    client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T08:00:00+00:00"),
        _m("A", irt="R", refs=["R"], sent="2026-09-18T09:00:00+00:00"),
    ]})
    first = client.post("/retention/freezes", json={"message_id": "A"}).json()
    assert first["created"] is True
    assert first["members"] == ["R", "A"]

    # 新邮件并入同一会话：活跃冻结随拓扑自动覆盖，无需再次冻结
    client.post("/batches", json={"messages": [
        _m("D", irt="R", refs=["R"], sent="2026-09-18T11:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]})
    assert client.get("/messages/D").json()["frozen"] is True

    # 再次冻结同一锚点 -> created=false，members 仍反映当前会话
    again = client.post("/retention/freezes", json={"message_id": "A"}).json()
    assert again["created"] is False
    assert again["members"] == ["R", "A", "D"]
    assert client.get("/messages/D").json()["frozen"] is True


def test_freeze_unknown_message_returns_404(client):
    r = client.post("/retention/freezes", json={"message_id": "nope"})
    assert r.status_code == 404


def test_freeze_invalid_cycle_message_returns_409(client):
    client.post("/batches", json={"messages": [
        _m("x", irt="y", refs=["y"]),
        _m("y", irt="x", refs=["x"]),
    ]})
    assert client.post("/retention/freezes", json={"message_id": "x"}).status_code == 409


def test_freeze_blank_anchor_rejected(client):
    assert client.post("/retention/freezes", json={"message_id": "   "}).status_code == 422


def test_unfreeze_releases_members_and_is_idempotently_404(client):
    _seed_retention_tree(client)
    client.post("/retention/freezes", json={"message_id": "C"})
    r = client.post("/retention/unfreezes", json={"message_id": "C"})
    assert r.status_code == 200
    body = r.json()
    assert body["anchor_id"] == "C"
    assert body["root_id"] == "R"
    assert body["released_members"] == ["A", "C", "D", "R"]
    assert body["still_protected"] == []
    assert client.get("/messages/A").json()["frozen"] is False

    assert client.post(
        "/retention/unfreezes", json={"message_id": "C"}
    ).status_code == 404


def test_unfreeze_one_freeze_keeps_overlapping_protection(client):
    _seed_retention_tree(client)
    client.post("/retention/freezes", json={"message_id": "A"})
    client.post("/retention/freezes", json={"message_id": "D"})

    body = client.post(
        "/retention/unfreezes", json={"message_id": "A"}
    ).json()
    # A 冻结释放后，所有成员仍被 D 冻结覆盖（同一棵树）
    assert body["released_members"] == []
    assert body["still_protected"] == ["A", "C", "D", "R"]
    assert client.get("/messages/C").json()["frozen"] is True

    body2 = client.post(
        "/retention/unfreezes", json={"message_id": "D"}
    ).json()
    assert body2["released_members"] == ["A", "C", "D", "R"]
    assert client.get("/messages/R").json()["frozen"] is False


def test_unfreeze_unknown_anchor_returns_404(client):
    assert client.post(
        "/retention/unfreezes", json={"message_id": "ghost"}
    ).status_code == 404


# ---------------------------------------------------------------------------
# POST /retention/cleanups
# ---------------------------------------------------------------------------


def _seed_cleanup_forest(client):
    """R(到期) -> A(到期) -> C(未到期) -> E(retain 更晚)；R -> D(到期, 冻结外另测)。"""
    client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T08:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
        _m("A", irt="R", refs=["R"], sent="2026-09-18T09:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
        _m("C", irt="A", refs=["R", "A"], sent="2026-09-18T10:00:00+00:00"),
        _m("E", irt="C", refs=["C"], sent="2026-09-18T10:30:00+00:00",
           retain_until="2026-10-01T00:00:00+00:00"),
        _m("D", irt="R", refs=["R"], sent="2026-09-18T11:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]})


CUTOFF = "2026-09-20T00:00:00+00:00"


def test_dry_run_uses_same_plan_but_writes_nothing(client):
    _seed_cleanup_forest(client)
    r = client.post("/retention/cleanups", json={"cutoff": CUTOFF, "dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["cutoff"] == "2026-09-20T00:00:00Z"
    assert body["deleted_ids"] == ["A", "D", "R"]
    assert body["deleted_count"] == 3
    assert body["expired_count"] == 3  # R/A/D 到期；E retain 在 10 月，未到期
    assert body["protected_count"] == 0

    impact = {i["message_id"]: i for i in body["affected"]}
    # R 被删 -> C 越过 A 成为新根；E 父仍为 C，但根 R -> C
    assert impact["C"] == {
        "message_id": "C", "old_parent_id": "A", "new_parent_id": None,
        "old_root_id": "R", "new_root_id": "C",
    }
    assert impact["E"] == {
        "message_id": "E", "old_parent_id": "C", "new_parent_id": "C",
        "old_root_id": "R", "new_root_id": "C",
    }
    assert body["affected_count"] == 2

    # dry_run 不写库：邮件都还在、结构不变
    assert client.get("/messages/R").status_code == 200
    assert client.get("/messages/C").json()["parent_id"] == "A"
    tree = client.get("/threads/E").json()
    assert tree["root_id"] == "R"


def test_actual_cleanup_compresses_thread_immediately(client):
    _seed_cleanup_forest(client)
    body = client.post(
        "/retention/cleanups", json={"cutoff": CUTOFF, "dry_run": False}
    ).json()
    assert body["dry_run"] is False
    assert body["deleted_ids"] == ["A", "D", "R"]

    # 删除的 ID 已不可查
    for mid in ("R", "A", "D"):
        assert client.get(f"/messages/{mid}").status_code == 404

    # 存活结构：C 为新根，E 挂在 C 下
    c = client.get("/messages/C").json()
    assert c["parent_id"] is None
    assert client.get("/messages/E").json()["parent_id"] == "C"
    tree = client.get("/threads/E").json()
    assert tree["root_id"] == "C"
    assert tree["tree"]["message_id"] == "C"
    assert [k["message_id"] for k in tree["tree"]["children"]] == ["E"]

    flat = client.get("/threads/C/page", params={"limit": 50}).json()
    assert [(i["message_id"], i["depth"]) for i in flat["items"]] == [
        ("C", 0), ("E", 1),
    ]


def test_freeze_blocks_cleanup_of_protected_expired_thread(client):
    _seed_cleanup_forest(client)
    # 以 D 为锚冻结 -> 整棵树 R A C E D 全部受保护
    fr = client.post("/retention/freezes", json={"message_id": "D"})
    assert fr.status_code == 200
    body = client.post(
        "/retention/cleanups", json={"cutoff": CUTOFF}
    ).json()
    assert body["deleted_ids"] == []
    assert body["protected_count"] == 3  # 到期的 R/A/D 全被保护
    assert body["expired_count"] == 3
    assert body["affected"] == []
    assert client.get("/messages/R").status_code == 200


def test_cleanup_only_deletes_subtree_outside_freeze(client):
    _seed_cleanup_forest(client)
    # 只冻结 C（冻结时其整棵当前会话其实是 R 树全体……），改为冻结另一棵树测试：
    # 独立树 P(到期) -> Q(到期)；冻结 P 后 P/Q 均不删，主树照常压缩
    client.post("/batches", json={"messages": [
        _m("P", sent="2026-09-17T08:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
        _m("Q", irt="P", refs=["P"], sent="2026-09-17T09:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]})
    client.post("/retention/freezes", json={"message_id": "Q"})

    body = client.post("/retention/cleanups", json={"cutoff": CUTOFF}).json()
    assert body["deleted_ids"] == ["A", "D", "R"]
    assert body["protected_count"] == 2  # P/Q 到期但被冻结
    # 受冻结树原样保留
    assert client.get("/messages/P").json()["parent_id"] is None
    assert client.get("/messages/Q").json()["parent_id"] == "P"


def test_cleanup_deletes_frozen_anchor_message_snapshot_does_not_apply_after_unfreeze(client):
    _seed_cleanup_forest(client)
    client.post("/retention/freezes", json={"message_id": "D"})
    assert client.post(
        "/retention/cleanups", json={"cutoff": CUTOFF}
    ).json()["deleted_ids"] == []
    # 解除冻结后同 cutoff 清理可正常删除
    client.post("/retention/unfreezes", json={"message_id": "D"})
    body = client.post("/retention/cleanups", json={"cutoff": CUTOFF}).json()
    assert body["deleted_ids"] == ["A", "D", "R"]


def test_cleanup_cutoff_boundary_is_inclusive(client):
    client.post("/batches", json={"messages": [
        _m("X", retain_until="2026-09-19T12:00:00+00:00"),
        _m("Y", retain_until="2026-09-19T12:00:01+00:00"),
    ]})
    body = client.post(
        "/retention/cleanups",
        json={"cutoff": "2026-09-19T12:00:00+00:00"},
    ).json()
    # retain_until == cutoff 视为到期；晚一秒保留
    assert body["deleted_ids"] == ["X"]


def test_cleanup_without_retain_until_never_deletes(client):
    client.post("/batches", json={"messages": [
        _m("A"), _m("B", irt="A", refs=["A"]),
    ]})
    body = client.post(
        "/retention/cleanups", json={"cutoff": "2030-01-01T00:00:00+00:00"}
    ).json()
    assert body["deleted_ids"] == []
    assert body["expired_count"] == 0


def test_cleanup_invalid_messages_are_never_touched(client):
    # 环内 invalid 邮件即便带 retain_until 也不参与清理
    client.post("/batches", json={"messages": [
        _m("x", irt="y", refs=["y"], retain_until="2026-09-01T00:00:00+00:00"),
        _m("y", irt="x", refs=["x"], retain_until="2026-09-01T00:00:00+00:00"),
    ]})
    body = client.post(
        "/retention/cleanups", json={"cutoff": "2026-09-20T00:00:00+00:00"}
    ).json()
    assert body["deleted_ids"] == []
    assert client.get("/messages/x").json()["is_invalid"] is True


def test_cleanup_idempotent_second_run_is_noop(client):
    _seed_cleanup_forest(client)
    first = client.post("/retention/cleanups", json={"cutoff": CUTOFF}).json()
    assert first["deleted_count"] == 3
    second = client.post("/retention/cleanups", json={"cutoff": CUTOFF}).json()
    assert second["deleted_ids"] == []
    assert second["affected"] == []
    assert second["expired_count"] == 0  # 剩余 C 无 retain_until、E retain 在 10 月
    assert second["protected_count"] == 0


def test_batch_snapshot_remains_unchanged_after_cleanup(client):
    b = client.post("/batches", json={"messages": [
        _m("R", retain_until="2026-09-19T00:00:00+00:00"),
        _m("A", irt="R", refs=["R"], retain_until="2026-09-19T00:00:00+00:00"),
    ]})
    snap = b.json()
    client.post("/retention/cleanups", json={"cutoff": CUTOFF})
    # 快照原样返回（含已删除节点）
    assert client.get(f"/batches/{snap['batch_id']}").json() == snap
    assert [t["message_id"] for t in snap["threads"]] == ["R"]


def test_paged_thread_cursor_is_stale_after_cleanup_compression(client):
    _seed_cleanup_forest(client)
    cur = client.get("/threads/R/page", params={"limit": 1}).json()["next_cursor"]
    client.post("/retention/cleanups", json={"cutoff": CUTOFF})
    # 旧游标基于已压缩前的树/根 R（已删除）；从存活节点重新翻页
    stale = client.get("/threads/C/page", params={"limit": 1, "cursor": cur})
    assert stale.status_code == 409
    fresh = client.get("/threads/C/page", params={"limit": 50}).json()
    assert [i["message_id"] for i in fresh["items"]] == ["C", "E"]
    assert fresh["next_cursor"] is None


def test_node_joining_thread_after_freeze_is_protected(client):
    # 冻结时树为 R -> A；之后新邮件 N 到期并挂到被冻结的 A 下。
    # 活跃冻结随会话拓扑扩张：N 自动受保护，清理不得删除。
    client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T08:00:00+00:00"),
        _m("A", irt="R", refs=["R"], sent="2026-09-18T09:00:00+00:00"),
    ]})
    client.post("/retention/freezes", json={"message_id": "A"})
    client.post("/batches", json={"messages": [
        _m("N", irt="A", refs=["R", "A"], sent="2026-09-18T12:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]})
    assert client.get("/messages/N").json()["frozen"] is True

    body = client.post("/retention/cleanups", json={"cutoff": CUTOFF}).json()
    assert body["deleted_ids"] == []
    assert body["expired_count"] == 1      # 仅 N 到期
    assert body["protected_count"] == 1    # N 被活跃冻结保护
    assert body["affected"] == []
    # 被冻结的 R/A/N 原样保留
    assert client.get("/messages/A").json()["parent_id"] == "R"
    tree = client.get("/threads/R").json()
    assert tree["root_id"] == "R"
    assert [c["message_id"] for c in tree["tree"]["children"]] == ["A"]
    assert [g["message_id"] for g in tree["tree"]["children"][0]["children"]] == ["N"]


def test_cleanup_keeps_frozen_thread_and_unfreeze_releases_it(client):
    # R(到期) 被冻结保护 -> 清理不删；解除冻结后再清理
    client.post("/batches", json={"messages": [
        _m("R", retain_until="2026-09-19T00:00:00+00:00"),
        _m("A", irt="R", refs=["R"]),
    ]})
    client.post("/retention/freezes", json={"message_id": "A"})
    client.post("/retention/cleanups", json={"cutoff": CUTOFF})
    unf = client.post("/retention/unfreezes", json={"message_id": "A"})
    assert unf.status_code == 200
    assert unf.json()["released_members"] == ["A", "R"]
    body = client.post("/retention/cleanups", json={"cutoff": CUTOFF}).json()
    assert body["deleted_ids"] == ["R"]
    impact = {i["message_id"]: i for i in body["affected"]}
    assert impact["A"]["new_parent_id"] is None
    assert impact["A"]["new_root_id"] == "A"


# ---------------------------------------------------------------------------
# 活跃冻结：保护范围随会话拓扑实时扩张
# ---------------------------------------------------------------------------


def test_relink_into_frozen_thread_is_protected(client):
    # D 断链为根（其父 late 尚未到达）；冻结 D 当前所在会话（此时仅 D 自身）
    client.post("/batches", json={"messages": [
        _m("D", irt="late", refs=["late"], sent="2026-09-18T09:00:00+00:00"),
    ]})
    client.post("/retention/freezes", json={"message_id": "D"})

    # 迟到邮件归档后补链：D 并入 late 的会话，活跃冻结随拓扑覆盖整棵新会话
    client.post("/batches", json={"messages": [
        _m("late", sent="2026-09-18T07:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]})
    r = client.post("/relinks", json={"message_ids": ["D"]})
    assert r.json()["results"][0]["new_parent_id"] == "late"

    # late 到期但被并入后的活跃冻结保护，无需再次冻结
    assert client.get("/messages/late").json()["frozen"] is True
    assert client.get("/messages/D").json()["frozen"] is True
    body = client.post("/retention/cleanups", json={"cutoff": CUTOFF}).json()
    assert body["deleted_ids"] == []
    assert body["protected_count"] == 1
    assert client.get("/messages/late").json()["parent_id"] is None


def test_overlapping_freezes_cover_later_members_until_all_released(client):
    client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T08:00:00+00:00"),
        _m("A", irt="R", refs=["R"], sent="2026-09-18T09:00:00+00:00"),
    ]})
    client.post("/retention/freezes", json={"message_id": "R"})
    client.post("/retention/freezes", json={"message_id": "A"})

    # 双重冻结后新邮件并入同一会话：两个锚点的保护都随拓扑覆盖 N
    client.post("/batches", json={"messages": [
        _m("N", irt="A", refs=["A"], sent="2026-09-18T10:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]})
    assert client.get("/messages/N").json()["frozen"] is True

    # 解除一个锚点：N 仍被另一锚点覆盖，不得释放
    body = client.post("/retention/unfreezes", json={"message_id": "R"}).json()
    assert body["released_members"] == []
    assert body["still_protected"] == ["A", "N", "R"]
    assert client.get("/messages/N").json()["frozen"] is True
    assert client.post(
        "/retention/cleanups", json={"cutoff": CUTOFF}
    ).json()["deleted_ids"] == []

    # 全部解除后才真正释放
    body2 = client.post("/retention/unfreezes", json={"message_id": "A"}).json()
    assert body2["released_members"] == ["A", "N", "R"]
    assert body2["still_protected"] == []
    assert client.get("/messages/N").json()["frozen"] is False
    assert client.post(
        "/retention/cleanups", json={"cutoff": CUTOFF}
    ).json()["deleted_ids"] == ["N"]


def test_cleanup_dry_run_and_actual_see_same_active_protection(client):
    # 冻结后新并入的到期邮件：dry_run 与实际执行按同一当前拓扑计算保护集合
    client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T08:00:00+00:00"),
    ]})
    client.post("/retention/freezes", json={"message_id": "R"})
    client.post("/batches", json={"messages": [
        _m("N", irt="R", refs=["R"], sent="2026-09-18T09:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]})

    dry = client.post(
        "/retention/cleanups", json={"cutoff": CUTOFF, "dry_run": True}
    ).json()
    assert dry["deleted_ids"] == []
    assert dry["protected_count"] == 1
    # dry_run 不写库
    assert client.get("/messages/N").status_code == 200

    actual = client.post("/retention/cleanups", json={"cutoff": CUTOFF}).json()
    assert actual["deleted_ids"] == dry["deleted_ids"]
    assert actual["protected_count"] == dry["protected_count"]
    assert actual["expired_count"] == dry["expired_count"]
    assert client.get("/messages/N").status_code == 200


# ---------------------------------------------------------------------------
# POST /thread-corrections：人工会话纠偏
# ---------------------------------------------------------------------------


def _seed_two_threads(client):
    """两棵独立会话：A -> C -> D；P -> Q。另归档一封断链根 Z。"""

    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("C", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
        _m("D", irt="C", refs=["A", "C"], sent="2026-09-18T10:00:00+00:00"),
        _m("P", sent="2026-09-18T07:00:00+00:00"),
        _m("Q", irt="P", refs=["P"], sent="2026-09-18T07:30:00+00:00"),
        _m("Z", irt="ghost", refs=["ghost"], sent="2026-09-18T12:00:00+00:00"),
    ]})


def test_get_thread_returns_stable_revision_for_all_members(client):
    _seed_two_threads(client)
    rev_a = client.get("/threads/A").json()["revision"]
    rev_c = client.get("/threads/C").json()["revision"]
    rev_d = client.get("/threads/D").json()["revision"]
    assert rev_a == rev_c == rev_d
    assert isinstance(rev_a, str) and len(rev_a) == 64  # sha256 hex
    # 同拓扑重复读取稳定
    assert client.get("/threads/A").json()["revision"] == rev_a
    # 另一棵会话的 revision 不同
    assert client.get("/threads/P").json()["revision"] != rev_a


def test_correction_moves_node_between_threads_and_reports_old_and_new(client):
    _seed_two_threads(client)
    b1 = client.get("/batches").json()["batches"][0]["batch_id"]
    snap_before = client.get(f"/batches/{b1}").json()
    rev = client.get("/threads/D").json()["revision"]

    # 把 D 从 A 树下移到另一棵树的 Q 下（D 的邮件头保持原样，仅改父指针）
    r = client.post("/thread-corrections", json={
        "message_id": "D",
        "new_parent_id": "Q",
        "expected_revision": rev,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["message_id"] == "D"
    assert body["old_parent_id"] == "C"
    assert body["new_parent_id"] == "Q"
    assert body["old_root_id"] == "A"
    assert body["new_root_id"] == "P"

    # 返回的新 revision 与随后 GET /threads 完全一致
    fresh = client.get("/threads/D").json()
    assert fresh["root_id"] == "P"
    assert fresh["revision"] == body["revision"]
    # P 树下仍按 (sent_at, id) 排序：P -> Q -> D（D 挂在 Q 下）
    assert [n["message_id"] for n in fresh["tree"]["children"]] == ["Q"]
    assert [n["message_id"] for n in fresh["tree"]["children"][0]["children"]] == ["D"]
    assert client.get("/messages/D").json()["parent_id"] == "Q"
    # 原会话不再包含 D
    old = client.get("/threads/A").json()
    assert old["root_id"] == "A"
    assert old["revision"] != rev

    # 既有批次快照保持归档时原样（D 仍挂 C 下）
    assert client.get(f"/batches/{b1}").json() == snap_before


def test_correction_detaches_node_to_new_root_with_null_parent(client):
    _seed_two_threads(client)
    rev = client.get("/threads/C").json()["revision"]

    # new_parent_id=null：C（及子树 D）拆成新的根
    r = client.post("/thread-corrections", json={
        "message_id": "C",
        "new_parent_id": None,
        "expected_revision": rev,
    })
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "message_id": "C",
        "old_parent_id": "A",
        "new_parent_id": None,
        "old_root_id": "A",
        "new_root_id": "C",
        "revision": body["revision"],
    }

    c_thread = client.get("/threads/C").json()
    assert c_thread["root_id"] == "C"
    assert c_thread["revision"] == body["revision"]
    # 子树 D 跟随 C 拆出
    assert client.get("/threads/D").json()["root_id"] == "C"
    # A 现在只剩自己
    a_thread = client.get("/threads/A").json()
    assert a_thread["root_id"] == "A"
    assert a_thread["tree"]["children"] == []
    # GET /messages 反映新父
    assert client.get("/messages/C").json()["parent_id"] is None


def test_correction_attach_orphan_root_into_existing_thread(client):
    _seed_two_threads(client)
    rev = client.get("/threads/Z").json()["revision"]
    assert rev
    r = client.post("/thread-corrections", json={
        "message_id": "Z",
        "new_parent_id": "A",
        "expected_revision": rev,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["old_parent_id"] is None
    assert body["new_parent_id"] == "A"
    assert body["old_root_id"] == "Z"
    assert body["new_root_id"] == "A"
    assert client.get("/messages/Z").json()["parent_id"] == "A"
    assert client.get("/threads/A").json()["revision"] == body["revision"]


def test_correction_revision_mismatch_returns_409_and_writes_nothing(client):
    _seed_two_threads(client)
    rev_d = client.get("/threads/D").json()["revision"]

    # 另一封邮件的并入改变了 A 树拓扑 -> 旧 revision 失效
    client.post("/batches", json={"messages": [
        _m("E", irt="A", refs=["A"], sent="2026-09-18T11:00:00+00:00"),
    ]})
    r = client.post("/thread-corrections", json={
        "message_id": "D",
        "new_parent_id": "Q",
        "expected_revision": rev_d,
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current_revision"] == client.get("/threads/D").json()["revision"]
    assert detail["current_revision"] != rev_d
    # 未写库：D 仍挂 C
    assert client.get("/messages/D").json()["parent_id"] == "C"

    # 带上当前 revision 重试即成功
    r2 = client.post("/thread-corrections", json={
        "message_id": "D",
        "new_parent_id": "Q",
        "expected_revision": detail["current_revision"],
    })
    assert r2.status_code == 200
    assert r2.json()["new_parent_id"] == "Q"


def test_correction_self_parent_returns_409_and_writes_nothing(client):
    _seed_two_threads(client)
    rev = client.get("/threads/C").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "C",
        "new_parent_id": "C",
        "expected_revision": rev,
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["cycles"] == [["C"]]
    assert client.get("/messages/C").json()["parent_id"] == "A"


def test_correction_under_own_descendant_returns_409_and_writes_nothing(client):
    _seed_two_threads(client)
    rev = client.get("/threads/A").json()["revision"]
    # 把根 A 挂到自己的后代 D 下 -> A -> C -> D -> A 成环
    r = client.post("/thread-corrections", json={
        "message_id": "A",
        "new_parent_id": "D",
        "expected_revision": rev,
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert set(detail["cycles"][0]) == {"A", "C", "D"}
    # 未写库：A 仍为根、C 的父仍为 A
    assert client.get("/messages/A").json()["parent_id"] is None
    assert client.get("/messages/C").json()["parent_id"] == "A"


def test_correction_unknown_message_returns_404(client):
    _seed_two_threads(client)
    rev = client.get("/threads/A").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "ghost",
        "new_parent_id": "A",
        "expected_revision": rev,
    })
    assert r.status_code == 404
    assert r.json()["detail"] == "message not found"


def test_correction_unknown_parent_returns_404_and_writes_nothing(client):
    _seed_two_threads(client)
    rev = client.get("/threads/C").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "C",
        "new_parent_id": "ghost-parent",
        "expected_revision": rev,
    })
    assert r.status_code == 404
    assert r.json()["detail"] == {
        "message": "new parent not found",
        "new_parent_id": "ghost-parent",
    }
    assert client.get("/messages/C").json()["parent_id"] == "A"


def test_correction_invalid_message_returns_409(client):
    _seed_two_threads(client)
    client.post("/batches", json={"messages": [
        _m("cy1", irt="cy2", refs=["cy2"], sent="2026-09-18T13:00:00+00:00"),
        _m("cy2", irt="cy1", refs=["cy1"], sent="2026-09-18T14:00:00+00:00"),
    ]})
    rev = client.get("/threads/A").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "cy1",
        "new_parent_id": "A",
        "expected_revision": rev,
    })
    assert r.status_code == 409


def test_correction_invalid_parent_returns_409_and_writes_nothing(client):
    _seed_two_threads(client)
    client.post("/batches", json={"messages": [
        _m("cy1", irt="cy2", refs=["cy2"], sent="2026-09-18T13:00:00+00:00"),
        _m("cy2", irt="cy1", refs=["cy1"], sent="2026-09-18T14:00:00+00:00"),
    ]})
    rev = client.get("/threads/Z").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "Z",
        "new_parent_id": "cy1",
        "expected_revision": rev,
    })
    assert r.status_code == 409
    assert client.get("/messages/Z").json()["parent_id"] is None


def test_correction_blank_and_missing_fields_rejected(client):
    _seed_two_threads(client)
    assert client.post("/thread-corrections", json={
        "message_id": "  ",
        "new_parent_id": None,
        "expected_revision": "x",
    }).status_code == 422
    assert client.post("/thread-corrections", json={
        "message_id": "C",
        "new_parent_id": "   ",
        "expected_revision": "x",
    }).status_code == 422
    assert client.post("/thread-corrections", json={
        "message_id": "C",
        "new_parent_id": None,
    }).status_code == 422
    assert client.post("/thread-corrections", json={
        "message_id": "C",
        "new_parent_id": None,
        "expected_revision": "   ",
    }).status_code == 422


def test_correction_same_parent_is_idempotent_and_returns_current_revision(client):
    _seed_two_threads(client)
    rev = client.get("/threads/D").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "D",
        "new_parent_id": "C",
        "expected_revision": rev,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["old_parent_id"] == body["new_parent_id"] == "C"
    assert body["old_root_id"] == body["new_root_id"] == "A"
    # 拓扑未变：revision 与调整前一致
    assert body["revision"] == rev


def test_correction_paged_cursor_is_stale_afterwards(client):
    _seed_two_threads(client)
    cur = client.get("/threads/A/page", params={"limit": 1}).json()["next_cursor"]
    rev = client.get("/threads/D").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "D",
        "new_parent_id": "Q",
        "expected_revision": rev,
    })
    assert r.status_code == 200
    # 旧分页游标基于纠偏前的 A 树 -> 409 cursor_stale
    stale = client.get("/threads/A/page", params={"limit": 1, "cursor": cur})
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "cursor_stale"
    # 不带游标重新翻页：A 树下 A -> C（D 已移走）
    fresh = client.get("/threads/A/page", params={"limit": 50}).json()
    assert [i["message_id"] for i in fresh["items"]] == ["A", "C"]


def test_correction_active_freeze_follows_new_topology_when_moving_in(client):
    _seed_two_threads(client)
    # 冻结 P 所在会话（P -> Q）；把未设保留期的 Z 拆开后再把到期邮件移入受保护会话
    client.post("/retention/freezes", json={"message_id": "P"})
    client.post("/batches", json={"messages": [
        _m("F", sent="2026-09-18T06:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]})
    rev = client.get("/threads/F").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "F",
        "new_parent_id": "Q",
        "expected_revision": rev,
    })
    assert r.status_code == 200

    # 活跃冻结按新拓扑自然生效：F 移入被冻结会话即受保护，不被清理
    assert client.get("/messages/F").json()["frozen"] is True
    body = client.post("/retention/cleanups", json={"cutoff": CUTOFF}).json()
    assert body["deleted_ids"] == []
    assert body["protected_count"] == 1


def test_correction_active_freeze_follows_new_topology_when_splitting_out(client):
    # A(到期) -> Z(到期)，冻结 A 整棵会话；纠偏把 Z 拆成根后脱离保护，可被清理
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
        _m("Z", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]})
    client.post("/retention/freezes", json={"message_id": "A"})
    rev = client.get("/threads/Z").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "Z",
        "new_parent_id": None,
        "expected_revision": rev,
    })
    assert r.status_code == 200

    # 拆出后 Z 不再与冻结锚点 A 同树 -> 失去保护
    assert client.get("/messages/A").json()["frozen"] is True
    assert client.get("/messages/Z").json()["frozen"] is False
    body = client.post("/retention/cleanups", json={"cutoff": CUTOFF}).json()
    assert body["deleted_ids"] == ["Z"]
    assert body["protected_count"] == 1
    assert client.get("/messages/A").status_code == 200


def test_init_db_migrates_legacy_snapshot_table(tmp_path):
    # 早期库：messages 无 retain_until 列，冻结成员快照存于 retention_freeze_members
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
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
            FOREIGN KEY (anchor_id) REFERENCES retention_freezes(anchor_id) ON DELETE CASCADE
        );
    """)
    conn.execute(
        "INSERT INTO retention_freezes VALUES ('A', '2026-09-19T00:00:00+00:00')"
    )
    conn.execute("INSERT INTO retention_freeze_members VALUES ('A', 'A')")
    conn.commit()
    conn.close()

    from app import database

    database.init_db(db_path=db)
    database.init_db(db_path=db)  # 幂等：重复执行不报错、结果不变

    conn = sqlite3.connect(db)
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "retention_freeze_members" not in tables  # 快照表已废弃
        assert "retention_freezes" in tables             # 锚点保留
        assert conn.execute(
            "SELECT anchor_id FROM retention_freezes"
        ).fetchall() == [("A",)]
        columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        assert "retain_until" in columns                 # 补列迁移仍生效
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POST /thread-sla/evaluate：会话响应时限评估（只读）
# ---------------------------------------------------------------------------


def test_thread_sla_statuses_summary_and_preorder(client):
    # A(08:00) <- B(08:30) <- C(10:30)；阈值 3600 秒
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T08:30:00+00:00"),
        _m("C", irt="B", refs=["A", "B"], sent="2026-09-18T10:30:00+00:00"),
    ]})

    r = client.post("/thread-sla/evaluate", json={
        "message_id": "C",   # 锚点可以是会话内任一封
        "as_of": "2026-09-18T11:00:00+00:00",
        "threshold_seconds": 3600,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["message_id"] == "C"
    assert body["root_id"] == "A"
    assert body["as_of"] == "2026-09-18T11:00:00Z"
    assert body["threshold_seconds"] == 3600.0
    # revision 与 GET /threads 一致（同一整棵会话拓扑）
    assert body["revision"] == client.get("/threads/C").json()["revision"]

    # 逐封结果按稳定先序排列
    assert [it["message_id"] for it in body["results"]] == ["A", "B", "C"]
    a, b, c = body["results"]
    assert a["depth"] == 0 and b["depth"] == 1 and c["depth"] == 2

    assert a["status"] == "within_sla"          # B 在 30 分钟后响应
    assert a["response_message_id"] == "B"
    assert a["response_sent_at"] == "2026-09-18T08:30:00Z"
    assert a["response_delay_seconds"] == 1800.0
    assert a["age_seconds"] is None

    assert b["status"] == "late"                # C 在 2 小时后响应，超过阈值
    assert b["response_message_id"] == "C"
    assert b["response_delay_seconds"] == 7200.0

    assert c["status"] == "pending"             # 无响应，年龄 30 分钟未超阈值
    assert c["response_message_id"] is None
    assert c["response_sent_at"] is None
    assert c["response_delay_seconds"] is None
    assert c["age_seconds"] == 1800.0

    assert body["summary"] == {
        "total": 3, "within_sla": 1, "late": 1, "pending": 1, "overdue": 0,
    }
    assert body["chronology_anomalies"] == []

    # as_of 后移使 C 年龄超过阈值 -> overdue
    again = client.post("/thread-sla/evaluate", json={
        "message_id": "C",
        "as_of": "2026-09-18T12:00:00+00:00",
        "threshold_seconds": 3600,
    }).json()
    assert again["results"][2]["status"] == "overdue"
    assert again["results"][2]["age_seconds"] == 5400.0
    assert again["summary"]["overdue"] == 1


def test_thread_sla_chronology_anomaly_does_not_clear_timeout(client):
    # B 是 A 的直接子邮件，但 sent_at 早于 A：时序异常，不得作为响应
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T10:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
    ]})

    body = client.post("/thread-sla/evaluate", json={
        "message_id": "A",
        "as_of": "2026-09-18T12:00:00+00:00",
        "threshold_seconds": 3600,
    }).json()

    a, b = body["results"]   # 先序：A(10:00) 在前，B(09:00) 作为子节点在后
    assert a["message_id"] == "A"
    assert a["status"] == "overdue"          # 异常子邮件不消除超时
    assert a["response_message_id"] is None
    assert a["age_seconds"] == 7200.0
    assert b["message_id"] == "B"
    assert b["status"] == "overdue"          # B 自身也无响应且年龄超阈值

    assert body["chronology_anomalies"] == [{
        "message_id": "B",
        "parent_id": "A",
        "sent_at": "2026-09-18T09:00:00Z",
        "parent_sent_at": "2026-09-18T10:00:00Z",
    }]
    assert body["summary"] == {
        "total": 2, "within_sla": 0, "late": 0, "pending": 0, "overdue": 2,
    }


def test_thread_sla_picks_earliest_valid_response(client):
    # A 的合格子邮件 B(08:30)、C(08:10) 中取最早的 C；D(07:00) 早于 A 属异常
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T08:30:00+00:00"),
        _m("C", irt="A", refs=["A"], sent="2026-09-18T08:10:00+00:00"),
        _m("D", irt="A", refs=["A"], sent="2026-09-18T07:00:00+00:00"),
    ]})

    body = client.post("/thread-sla/evaluate", json={
        "message_id": "A",
        "as_of": "2026-09-18T09:00:00+00:00",
        "threshold_seconds": 900,
    }).json()

    # 先序：A，然后子节点按 (sent_at, id)：D、C、B
    assert [it["message_id"] for it in body["results"]] == ["A", "D", "C", "B"]
    a = body["results"][0]
    assert a["response_message_id"] == "C"
    assert a["response_delay_seconds"] == 600.0
    assert a["status"] == "within_sla"
    assert [x["message_id"] for x in body["chronology_anomalies"]] == ["D"]


def test_thread_sla_threshold_boundary_is_inclusive(client):
    # 响应耗时 / 年龄恰好等于阈值：within_sla / pending（边界含等于）
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
    ]})
    body = client.post("/thread-sla/evaluate", json={
        "message_id": "A",
        "as_of": "2026-09-18T10:00:00+00:00",
        "threshold_seconds": 3600,
    }).json()
    a, b = body["results"]
    assert a["response_delay_seconds"] == 3600.0
    assert a["status"] == "within_sla"
    assert b["age_seconds"] == 3600.0
    assert b["status"] == "pending"


def test_thread_sla_naive_as_of_treated_as_utc(client):
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
    ]})
    body = client.post("/thread-sla/evaluate", json={
        "message_id": "A",
        "as_of": "2026-09-18T09:00:00",   # 无时区 -> 按 UTC 处理
        "threshold_seconds": 3600,
    }).json()
    assert body["as_of"] == "2026-09-18T09:00:00Z"
    assert body["results"][0]["age_seconds"] == 3600.0
    assert body["results"][0]["status"] == "pending"


def test_thread_sla_unknown_message_404(client):
    r = client.post("/thread-sla/evaluate", json={
        "message_id": "ghost",
        "as_of": "2026-09-18T12:00:00+00:00",
        "threshold_seconds": 60,
    })
    assert r.status_code == 404


def test_thread_sla_cycle_message_409(client):
    client.post("/batches", json={"messages": [
        _m("x", irt="y", refs=["y"]),
        _m("y", irt="x", refs=["x"]),
    ]})
    r = client.post("/thread-sla/evaluate", json={
        "message_id": "x",
        "as_of": "2026-09-18T12:00:00+00:00",
        "threshold_seconds": 60,
    })
    assert r.status_code == 409


def test_thread_sla_invalid_payload_422(client):
    assert client.post("/thread-sla/evaluate", json={
        "message_id": "A",
        "as_of": "2026-09-18T12:00:00+00:00",
        "threshold_seconds": -1,          # 阈值不得为负
    }).status_code == 422
    assert client.post("/thread-sla/evaluate", json={
        "message_id": "   ",              # 空白 ID
        "as_of": "2026-09-18T12:00:00+00:00",
        "threshold_seconds": 60,
    }).status_code == 422
    assert client.post("/thread-sla/evaluate", json={
        "message_id": "A",                # 缺 as_of / threshold_seconds
    }).status_code == 422


def test_thread_sla_is_read_only(client):
    batch = client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T08:30:00+00:00"),
    ]}).json()
    msg_before = client.get("/messages/A").json()
    thread_before = client.get("/threads/A").json()

    r = client.post("/thread-sla/evaluate", json={
        "message_id": "A",
        "as_of": "2026-09-19T00:00:00+00:00",
        "threshold_seconds": 60,
    })
    assert r.status_code == 200

    # 批次快照、邮件、会话树均不被改写
    assert client.get(f"/batches/{batch['batch_id']}").json() == batch
    assert client.get("/messages/A").json() == msg_before
    assert client.get("/threads/A").json() == thread_before


# ---------------------------------------------------------------------------
# POST /thread-subject-audit：会话主题漂移审计（只读）
# ---------------------------------------------------------------------------


def _seed_subject_drift_thread(client):
    """A(项目周报) -> B -> C；A -> D(团建通知, 漂移) -> E；D -> F(值班表, 漂移) -> G(聚餐, 漂移)。"""
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00", subject="项目周报"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00",
           subject="Re: 项目周报"),
        _m("C", irt="B", refs=["A", "B"], sent="2026-09-18T10:00:00+00:00",
           subject="RE: Fw: 项目周报"),
        _m("D", irt="A", refs=["A"], sent="2026-09-18T11:00:00+00:00",
           subject="Fwd: 团建通知"),
        _m("E", irt="D", refs=["A", "D"], sent="2026-09-18T12:00:00+00:00",
           subject="re: 团建通知"),
        _m("F", irt="D", refs=["A", "D"], sent="2026-09-18T13:00:00+00:00",
           subject="值班表"),
        _m("G", irt="F", refs=["A", "D", "F"], sent="2026-09-18T14:00:00+00:00",
           subject="Re: 聚餐"),
    ]})


def test_thread_subject_audit_reports_canonical_drift_and_groups(client):
    _seed_subject_drift_thread(client)
    r = client.post("/thread-subject-audit", json={"message_id": "G"})  # 锚点可以是深层节点
    assert r.status_code == 200
    body = r.json()
    assert body["message_id"] == "G"
    assert body["root_id"] == "A"
    # revision 与 GET /threads 一致（同一整棵会话拓扑）
    assert body["revision"] == client.get("/threads/G").json()["revision"]

    # 逐节点按稳定先序排列
    assert [it["message_id"] for it in body["results"]] == [
        "A", "B", "C", "D", "E", "F", "G",
    ]
    by_id = {it["message_id"]: it for it in body["results"]}

    root = by_id["A"]
    assert root["parent_id"] is None
    assert root["depth"] == 0
    assert root["subject"] == "项目周报"
    assert root["canonical_subject"] == "项目周报"
    assert root["baseline_subject"] is None
    assert root["drifted"] is False

    # 大小写不敏感 Re/Fw 前缀重复剥离后仍是根主题 -> 不偏离
    assert by_id["C"]["subject"] == "RE: Fw: 项目周报"
    assert by_id["C"]["canonical_subject"] == "项目周报"
    assert by_id["C"]["baseline_subject"] == "项目周报"
    assert by_id["C"]["drifted"] is False

    # D 换话题 -> 偏离；其分支基线仍是根主题
    assert by_id["D"]["canonical_subject"] == "团建通知"
    assert by_id["D"]["baseline_subject"] == "项目周报"
    assert by_id["D"]["drifted"] is True

    # E 沿用 D 的新话题：以最近祖先 D 的规范主题为分支基线 -> 不偏离
    assert by_id["E"]["canonical_subject"] == "团建通知"
    assert by_id["E"]["baseline_subject"] == "团建通知"
    assert by_id["E"]["drifted"] is False

    # F/G 连续偏离（G 的基线是 F 偏离后的新主题）
    assert by_id["F"]["baseline_subject"] == "团建通知"
    assert by_id["F"]["drifted"] is True
    assert by_id["G"]["canonical_subject"] == "聚餐"
    assert by_id["G"]["baseline_subject"] == "值班表"
    assert by_id["G"]["drifted"] is True

    # 先序连续偏离合并：D 单独一组（被不偏离的 E 打断），F/G 合并一组
    assert body["drift_groups"] == [
        {"members": ["D"], "baseline_subject": "项目周报"},
        {"members": ["F", "G"], "baseline_subject": "团建通知"},
    ]


def test_thread_subject_audit_no_drift_returns_empty_groups(client):
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00", subject="例会通知"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00",
           subject="Re: 例会通知"),
        _m("C", irt="B", refs=["A", "B"], sent="2026-09-18T10:00:00+00:00",
           subject="Fwd: 例会通知"),
    ]})
    body = client.post("/thread-subject-audit", json={"message_id": "C"}).json()
    assert [it["drifted"] for it in body["results"]] == [False, False, False]
    assert body["drift_groups"] == []


def test_thread_subject_audit_collapses_whitespace(client):
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00", subject="项目 周报"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00",
           subject="Re:  项目   周报 "),
        _m("C", irt="A", refs=["A"], sent="2026-09-18T10:00:00+00:00",
           subject="Re: 项目周报"),
    ]})
    body = client.post("/thread-subject-audit", json={"message_id": "A"}).json()
    by_id = {it["message_id"]: it for it in body["results"]}
    # 任意连续空白折叠为单个空格：B 与根规范主题一致 -> 不偏离
    assert by_id["B"]["canonical_subject"] == "项目 周报"
    assert by_id["B"]["drifted"] is False
    # C 折叠后无空格，与根规范主题不同 -> 偏离
    assert by_id["C"]["canonical_subject"] == "项目周报"
    assert by_id["C"]["drifted"] is True
    assert body["drift_groups"] == [
        {"members": ["C"], "baseline_subject": "项目 周报"},
    ]


def test_thread_subject_audit_unknown_message_404(client):
    r = client.post("/thread-subject-audit", json={"message_id": "ghost"})
    assert r.status_code == 404


def test_thread_subject_audit_cycle_message_409(client):
    client.post("/batches", json={"messages": [
        _m("x", irt="y", refs=["y"]),
        _m("y", irt="x", refs=["x"]),
    ]})
    r = client.post("/thread-subject-audit", json={"message_id": "x"})
    assert r.status_code == 409


def test_thread_subject_audit_invalid_payload_422(client):
    assert client.post(
        "/thread-subject-audit", json={"message_id": "   "}
    ).status_code == 422
    assert client.post("/thread-subject-audit", json={}).status_code == 422


def test_thread_subject_audit_is_read_only(client):
    batch = client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00", subject="周报"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00",
           subject="新话题"),
    ]}).json()
    msg_before = client.get("/messages/A").json()
    thread_before = client.get("/threads/A").json()

    r = client.post("/thread-subject-audit", json={"message_id": "A"})
    assert r.status_code == 200
    assert r.json()["drift_groups"] == [
        {"members": ["B"], "baseline_subject": "周报"},
    ]

    # 批次快照、邮件、会话树均不被改写
    assert client.get(f"/batches/{batch['batch_id']}").json() == batch
    assert client.get("/messages/A").json() == msg_before
    assert client.get("/threads/A").json() == thread_before
