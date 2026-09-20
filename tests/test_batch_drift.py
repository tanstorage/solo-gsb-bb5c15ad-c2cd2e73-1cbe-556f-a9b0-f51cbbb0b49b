"""GET /batches/{batch_id}/drift：批次漂移审计（只读）。

以批次不可变结果快照中的原始会话树（threads）为基线，按原始先序逐节点
对照当前归档状态：deleted / id_reused / reparented / root_changed /
unchanged，并校验各状态计数、人工纠偏与保留期清理后的漂移结论、清理后
同 ID 重归档、历史祖先的来源一致性，以及审计的只读性。
"""

from __future__ import annotations


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


def _seed_forest(client):
    """一批两棵树：A -> B -> C；D -> E。返回批次响应。"""
    return client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
        _m("C", irt="B", refs=["A", "B"], sent="2026-09-18T10:00:00+00:00"),
        _m("D", sent="2026-09-18T11:00:00+00:00"),
        _m("E", irt="D", refs=["D"], sent="2026-09-18T12:00:00+00:00"),
    ]}).json()


def test_drift_unknown_batch_returns_404(client):
    r = client.get("/batches/ghost-batch/drift")
    assert r.status_code == 404
    assert r.json()["detail"] == "batch not found"


def test_drift_unchanged_baseline_preorder_and_counts(client):
    batch = _seed_forest(client)
    r = client.get(f"/batches/{batch['batch_id']}/drift")
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] == batch["batch_id"]
    assert body["submitted_at"] == batch["submitted_at"]

    # 按快照原始先序逐封输出（树间、子节点间均按归档时的稳定排序）
    assert [i["message_id"] for i in body["results"]] == ["A", "B", "C", "D", "E"]
    by_id = {i["message_id"]: i for i in body["results"]}

    a = by_id["A"]
    assert a["original_parent_id"] is None
    assert a["original_root_id"] == "A"
    assert a["current_parent_id"] is None
    assert a["current_root_id"] == "A"
    assert a["status"] == "unchanged"

    c = by_id["C"]
    assert c["original_parent_id"] == "B"
    assert c["original_root_id"] == "A"
    assert c["current_parent_id"] == "B"
    assert c["current_root_id"] == "A"
    assert c["status"] == "unchanged"

    e = by_id["E"]
    assert e["original_parent_id"] == "D"
    assert e["original_root_id"] == "D"
    assert e["current_parent_id"] == "D"
    assert e["current_root_id"] == "D"
    assert e["status"] == "unchanged"

    assert body["summary"] == {
        "total": 5, "deleted": 0, "id_reused": 0,
        "reparented": 0, "root_changed": 0, "unchanged": 5,
    }


def test_drift_after_manual_correction_reparented_and_root_changed(client):
    # A -> C -> D 与 P -> Q 同批归档
    batch = client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("C", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
        _m("D", irt="C", refs=["A", "C"], sent="2026-09-18T10:00:00+00:00"),
        _m("P", sent="2026-09-18T07:00:00+00:00"),
        _m("Q", irt="P", refs=["P"], sent="2026-09-18T07:30:00+00:00"),
    ]}).json()

    # 人工纠偏：把根 A（连同子树 C、D）挂到另一棵树的 Q 下
    rev = client.get("/threads/A").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "A", "new_parent_id": "Q", "expected_revision": rev,
    })
    assert r.status_code == 200

    body = client.get(f"/batches/{batch['batch_id']}/drift").json()
    by_id = {i["message_id"]: i for i in body["results"]}

    # A：父节点 None -> Q，根 A -> P
    assert by_id["A"]["status"] == "reparented"
    assert by_id["A"]["original_parent_id"] is None
    assert by_id["A"]["original_root_id"] == "A"
    assert by_id["A"]["current_parent_id"] == "Q"
    assert by_id["A"]["current_root_id"] == "P"

    # C/D：父节点未变，仅根 A -> P
    assert by_id["C"]["status"] == "root_changed"
    assert by_id["C"]["original_parent_id"] == "A"
    assert by_id["C"]["current_parent_id"] == "A"
    assert by_id["C"]["current_root_id"] == "P"
    assert by_id["D"]["status"] == "root_changed"
    assert by_id["D"]["current_parent_id"] == "C"
    assert by_id["D"]["current_root_id"] == "P"

    # P/Q 未受影响
    assert by_id["P"]["status"] == "unchanged"
    assert by_id["Q"]["status"] == "unchanged"

    assert body["summary"] == {
        "total": 5, "deleted": 0, "id_reused": 0,
        "reparented": 1, "root_changed": 2, "unchanged": 2,
    }


def test_drift_after_correction_detach_to_new_root(client):
    batch = client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("C", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
        _m("D", irt="C", refs=["A", "C"], sent="2026-09-18T10:00:00+00:00"),
    ]}).json()

    # 人工纠偏拆根：C（连同子树 D）拆成新的根
    rev = client.get("/threads/C").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "C", "new_parent_id": None, "expected_revision": rev,
    })
    assert r.status_code == 200

    body = client.get(f"/batches/{batch['batch_id']}/drift").json()
    by_id = {i["message_id"]: i for i in body["results"]}
    assert by_id["A"]["status"] == "unchanged"
    # C：父 A -> None（reparented），根 A -> C
    assert by_id["C"]["status"] == "reparented"
    assert by_id["C"]["current_parent_id"] is None
    assert by_id["C"]["current_root_id"] == "C"
    # D：父仍为 C，仅根 A -> C（root_changed）
    assert by_id["D"]["status"] == "root_changed"
    assert by_id["D"]["current_parent_id"] == "C"
    assert by_id["D"]["current_root_id"] == "C"
    assert body["summary"] == {
        "total": 3, "deleted": 0, "id_reused": 0,
        "reparented": 1, "root_changed": 1, "unchanged": 1,
    }


def test_drift_after_relink_marks_reparented(client):
    # D 归档时其父 late 尚未到达 -> 断链自成根；迟到归档后补链
    batch = client.post("/batches", json={"messages": [
        _m("D", irt="late", refs=["late"], sent="2026-09-18T09:00:00+00:00"),
    ]}).json()
    client.post("/batches", json={"messages": [
        _m("late", sent="2026-09-18T07:00:00+00:00"),
    ]})
    r = client.post("/relinks", json={"message_ids": ["D"]})
    assert r.json()["relinked"] == 1

    body = client.get(f"/batches/{batch['batch_id']}/drift").json()
    (item,) = body["results"]
    assert item["message_id"] == "D"
    assert item["original_parent_id"] is None
    assert item["original_root_id"] == "D"
    assert item["current_parent_id"] == "late"
    assert item["current_root_id"] == "late"
    assert item["status"] == "reparented"
    assert body["summary"]["reparented"] == 1


def test_drift_after_cleanup_deleted_and_compressed(client):
    # R(到期) -> A(到期) -> C(未到期) -> E；R -> D(到期)
    batch = client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T08:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
        _m("A", irt="R", refs=["R"], sent="2026-09-18T09:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
        _m("C", irt="A", refs=["R", "A"], sent="2026-09-18T10:00:00+00:00"),
        _m("E", irt="C", refs=["C"], sent="2026-09-18T10:30:00+00:00"),
        _m("D", irt="R", refs=["R"], sent="2026-09-18T11:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]}).json()

    client.post("/retention/cleanups", json={"cutoff": "2026-09-20T00:00:00+00:00"})

    body = client.get(f"/batches/{batch['batch_id']}/drift").json()
    # 原始先序不变：R A C E D
    assert [i["message_id"] for i in body["results"]] == ["R", "A", "C", "E", "D"]
    by_id = {i["message_id"]: i for i in body["results"]}

    # 到期被删：当前不存在，current_* 为 null
    for mid in ("R", "A", "D"):
        assert by_id[mid]["status"] == "deleted"
        assert by_id[mid]["current_parent_id"] is None
        assert by_id[mid]["current_root_id"] is None
    assert by_id["R"]["original_parent_id"] is None
    assert by_id["R"]["original_root_id"] == "R"
    assert by_id["A"]["original_parent_id"] == "R"
    assert by_id["D"]["original_root_id"] == "R"

    # C：父 A -> None（清理压缩重挂）-> reparented；根 R -> C
    assert by_id["C"]["status"] == "reparented"
    assert by_id["C"]["current_parent_id"] is None
    assert by_id["C"]["current_root_id"] == "C"

    # E：父仍为 C，仅根 R -> C -> root_changed
    assert by_id["E"]["status"] == "root_changed"
    assert by_id["E"]["current_parent_id"] == "C"
    assert by_id["E"]["current_root_id"] == "C"

    assert body["summary"] == {
        "total": 5, "deleted": 3, "id_reused": 0,
        "reparented": 1, "root_changed": 1, "unchanged": 0,
    }


def test_drift_id_reused_after_cleanup_and_rearchive(client):
    # M 带保留期归档；清理删除后同 ID 由更晚批次重归档
    batch1 = client.post("/batches", json={"messages": [
        _m("M", sent="2026-09-18T08:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
        _m("K", irt="M", refs=["M"], sent="2026-09-18T09:00:00+00:00"),
    ]}).json()

    client.post("/retention/cleanups", json={"cutoff": "2026-09-20T00:00:00+00:00"})
    assert client.get("/messages/M").status_code == 404

    # 清理后、重归档前：M 判定为 deleted；K 被压缩为新根（父变化）
    body = client.get(f"/batches/{batch1['batch_id']}/drift").json()
    by_id = {i["message_id"]: i for i in body["results"]}
    assert by_id["M"]["status"] == "deleted"
    assert by_id["M"]["current_parent_id"] is None
    assert by_id["M"]["current_root_id"] is None
    assert by_id["K"]["status"] == "reparented"
    assert by_id["K"]["current_parent_id"] is None
    assert by_id["K"]["current_root_id"] == "K"
    assert body["summary"]["deleted"] == 1

    # 同 ID 重归档（更晚批次，挂在新的 root2 下）
    batch2 = client.post("/batches", json={"messages": [
        _m("root2", sent="2026-09-20T08:00:00+00:00"),
        _m("M", irt="root2", refs=["root2"], sent="2026-09-20T09:00:00+00:00"),
    ]}).json()
    assert client.get("/messages/M").json()["batch_id"] == batch2["batch_id"]

    # 原批次基线：M 当前属于其它批次 -> id_reused（current_* 反映新归档）
    body = client.get(f"/batches/{batch1['batch_id']}/drift").json()
    by_id = {i["message_id"]: i for i in body["results"]}
    assert by_id["M"]["status"] == "id_reused"
    assert by_id["M"]["original_parent_id"] is None
    assert by_id["M"]["original_root_id"] == "M"
    assert by_id["M"]["current_parent_id"] == "root2"
    assert by_id["M"]["current_root_id"] == "root2"
    # K 仍是压缩后的根（重归档不回填父指针）
    assert by_id["K"]["status"] == "reparented"
    assert body["summary"] == {
        "total": 2, "deleted": 0, "id_reused": 1,
        "reparented": 1, "root_changed": 0, "unchanged": 0,
    }

    # 新批次自己的基线：M 来源一致、未漂移
    body2 = client.get(f"/batches/{batch2['batch_id']}/drift").json()
    by_id2 = {i["message_id"]: i for i in body2["results"]}
    assert by_id2["M"]["status"] == "unchanged"
    assert by_id2["M"]["original_parent_id"] == "root2"
    assert by_id2["M"]["original_root_id"] == "root2"


def test_drift_historical_ancestors_are_not_id_reused(client):
    # 快照 threads 含历史祖先（上一批归档的 A）：来源仍是其原始批次，不算 id_reused
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
    ]})
    batch2 = client.post("/batches", json={"messages": [
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
        _m("C", irt="B", refs=["A", "B"], sent="2026-09-18T10:00:00+00:00"),
    ]}).json()
    # batch2 快照树含历史祖先 A（整棵树含历史祖先）
    assert [t["message_id"] for t in batch2["threads"]] == ["A"]

    body = client.get(f"/batches/{batch2['batch_id']}/drift").json()
    assert [i["message_id"] for i in body["results"]] == ["A", "B", "C"]
    assert all(i["status"] == "unchanged" for i in body["results"])
    assert body["summary"] == {
        "total": 3, "deleted": 0, "id_reused": 0,
        "reparented": 0, "root_changed": 0, "unchanged": 3,
    }


def test_drift_only_audits_threads_nodes(client):
    # 环内 invalid 邮件不在 threads 中，不参与审计
    batch = client.post("/batches", json={"messages": [
        _m("Z", sent="2026-09-18T08:00:00+00:00"),
        _m("x", irt="y", refs=["y"], sent="2026-09-18T09:00:00+00:00"),
        _m("y", irt="x", refs=["x"], sent="2026-09-18T10:00:00+00:00"),
    ]}).json()
    assert batch["totals"]["invalid"] == 2

    body = client.get(f"/batches/{batch['batch_id']}/drift").json()
    assert [i["message_id"] for i in body["results"]] == ["Z"]
    assert body["summary"] == {
        "total": 1, "deleted": 0, "id_reused": 0,
        "reparented": 0, "root_changed": 0, "unchanged": 1,
    }


def test_drift_empty_threads_batch(client):
    # 整批成环：threads 为空 -> 空结果、计数全 0
    batch = client.post("/batches", json={"messages": [
        _m("x", irt="y", refs=["y"]),
        _m("y", irt="x", refs=["x"]),
    ]}).json()
    assert batch["threads"] == []

    body = client.get(f"/batches/{batch['batch_id']}/drift").json()
    assert body["results"] == []
    assert body["summary"] == {
        "total": 0, "deleted": 0, "id_reused": 0,
        "reparented": 0, "root_changed": 0, "unchanged": 0,
    }


def test_drift_is_read_only_and_does_not_rewrite_snapshot(client):
    batch = _seed_forest(client)
    snap_before = client.get(f"/batches/{batch['batch_id']}").json()
    msg_before = client.get("/messages/B").json()
    thread_before = client.get("/threads/A").json()

    first = client.get(f"/batches/{batch['batch_id']}/drift")
    assert first.status_code == 200
    # 重复审计结果一致
    again = client.get(f"/batches/{batch['batch_id']}/drift").json()
    assert again == first.json()

    # 批次快照、邮件、会话树均不被改写
    assert client.get(f"/batches/{batch['batch_id']}").json() == snap_before == batch
    assert client.get("/messages/B").json() == msg_before
    assert client.get("/threads/A").json() == thread_before
