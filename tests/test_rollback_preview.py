"""POST /batches/{batch_id}/rollback-preview：批次撤销预演（只读）。

预演只处理目标批次首次归档且当前仍归属该批次的有效邮件：不处理快照中
的历史祖先、环内 invalid 邮件或已被更晚批次重用的同 ID；存活子孙按与
保留期清理相同的压缩语义在内存中重挂到最近存活祖先。校验待删除 ID、
父节点将变化的邮件、撤销后的根分组、汇总计数，以及预演的只读性。
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


def _preview(client, batch_id):
    r = client.post(f"/batches/{batch_id}/rollback-preview")
    assert r.status_code == 200, r.text
    return r.json()


def test_rollback_preview_unknown_batch_returns_404(client):
    r = client.post("/batches/ghost-batch/rollback-preview")
    assert r.status_code == 404
    assert r.json()["detail"] == "batch not found"


def test_rollback_preview_middle_batch_keeps_historical_ancestor_and_later_descendants(client):
    # 批次 1：历史祖先 H
    b1 = client.post("/batches", json={"messages": [
        _m("H", sent="2026-09-18T08:00:00+00:00"),
    ]}).json()
    # 批次 2（拟撤销）：H -> B -> C
    b2 = client.post("/batches", json={"messages": [
        _m("B", irt="H", refs=["H"], sent="2026-09-18T09:00:00+00:00"),
        _m("C", irt="B", refs=["H", "B"], sent="2026-09-18T10:00:00+00:00"),
    ]}).json()
    # 批次 3（存活子孙）：D 挂在 C 下，E 挂在 B 下；另有无关树 R -> S
    client.post("/batches", json={"messages": [
        _m("D", irt="C", refs=["H", "B", "C"], sent="2026-09-18T11:00:00+00:00"),
        _m("E", irt="B", refs=["H", "B"], sent="2026-09-18T12:00:00+00:00"),
        _m("R", sent="2026-09-17T08:00:00+00:00"),
        _m("S", irt="R", refs=["R"], sent="2026-09-17T09:00:00+00:00"),
    ]})

    # 撤销批次 1（首批）：只删历史祖先 H；B/C/D/E 全部存活并压缩为一棵新树
    body = _preview(client, b1["batch_id"])
    assert body["deleted_ids"] == ["H"]

    affected = {i["message_id"]: i for i in body["affected"]}
    # B 父 H -> None、提升为新根；C/D/E 父指针不变（E 父仍是 B），仅根 H -> B
    assert affected["B"] == {
        "message_id": "B", "old_parent_id": "H", "new_parent_id": None,
        "old_root_id": "H", "new_root_id": "B",
    }
    for mid, pid in (("C", "B"), ("D", "C"), ("E", "B")):
        assert affected[mid]["old_parent_id"] == pid
        assert affected[mid]["new_parent_id"] == pid
        assert affected[mid]["old_root_id"] == "H"
        assert affected[mid]["new_root_id"] == "B"

    # 撤销后唯一根分组：B 为根，按稳定先序 B -> C -> D -> E
    assert body["root_groups"] == [
        {"root_id": "B", "size": 4, "members": ["B", "C", "D", "E"]},
    ]
    assert body["summary"] == {
        "deleted_count": 1,
        "affected_count": 4,
        "skipped_invalid_count": 0,
        "skipped_reused_count": 0,
        "touched_tree_count": 1,
        "root_group_count": 1,
        "surviving_count": 4,
    }

    # 撤销批次 2（核心场景）：只删 B/C；历史祖先 H 保留，更晚批的 D/E 存活
    body = _preview(client, b2["batch_id"])
    assert body["submitted_at"] == b2["submitted_at"]
    assert body["deleted_ids"] == ["B", "C"]

    affected = {i["message_id"]: i for i in body["affected"]}
    # D 越过 C、B 重挂到最近存活祖先 H；E 越过 B 重挂到 H；根始终是 H
    assert affected["D"] == {
        "message_id": "D", "old_parent_id": "C", "new_parent_id": "H",
        "old_root_id": "H", "new_root_id": "H",
    }
    assert affected["E"] == {
        "message_id": "E", "old_parent_id": "B", "new_parent_id": "H",
        "old_root_id": "H", "new_root_id": "H",
    }
    # 只有 D/E 两封父节点变化（H 存活且不变）
    assert body["summary"]["affected_count"] == 2

    # 撤销后根分组：H 为根，成员按 (sent_at, message_id) 稳定先序（D 在 E 前）
    assert body["root_groups"] == [
        {"root_id": "H", "size": 3, "members": ["H", "D", "E"]},
    ]
    assert body["summary"] == {
        "deleted_count": 2,
        "affected_count": 2,
        "skipped_invalid_count": 0,
        "skipped_reused_count": 0,
        "touched_tree_count": 1,
        "root_group_count": 1,
        "surviving_count": 3,
    }

    # 无关树 R/S 不出现在任何结论中
    for item in body["affected"]:
        assert item["message_id"] not in ("R", "S")
    for group in body["root_groups"]:
        assert "R" not in group["members"] and "S" not in group["members"]


def test_rollback_preview_first_batch_promotes_survivors_to_new_roots(client):
    # 首批（拟撤销）：A(root) -> B
    b1 = client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
    ]}).json()

    # 更晚批次的存活子孙：C 挂在 B 下、D 也挂在 B 下；A/B 被删后 C、D 各自成根
    b2 = client.post("/batches", json={"messages": [
        _m("C", irt="B", refs=["A", "B"], sent="2026-09-18T10:00:00+00:00"),
        _m("D", irt="B", refs=["A", "B"], sent="2026-09-18T11:00:00+00:00"),
    ]}).json()

    body = _preview(client, b1["batch_id"])
    assert body["deleted_ids"] == ["A", "B"]
    affected = {i["message_id"]: i for i in body["affected"]}
    # C、D 链顶无存活祖先 -> 各自提升为新根
    assert affected["C"]["new_parent_id"] is None
    assert affected["C"]["old_root_id"] == "A" and affected["C"]["new_root_id"] == "C"
    assert affected["D"]["new_parent_id"] is None
    assert affected["D"]["old_root_id"] == "A" and affected["D"]["new_root_id"] == "D"

    # 撤销后两个根分组：C 与 D（根按 sent_at 排序，C 早于 D）
    assert [g["root_id"] for g in body["root_groups"]] == ["C", "D"]
    assert body["root_groups"] == [
        {"root_id": "C", "size": 1, "members": ["C"]},
        {"root_id": "D", "size": 1, "members": ["D"]},
    ]
    assert body["summary"]["root_group_count"] == 2
    assert body["summary"]["surviving_count"] == 2
    assert body["summary"]["touched_tree_count"] == 1

    # 撤销更晚批次：只拥有 C/D，删除后旧树的其余节点 A/B 存活并受触及
    body2 = _preview(client, b2["batch_id"])
    assert body2["deleted_ids"] == ["C", "D"]
    # A/B 仍是 A -> B，父指针与根均无变化 -> 无 affected、根分组仍为原 A 树
    assert body2["affected"] == []
    assert body2["root_groups"] == [
        {"root_id": "A", "size": 2, "members": ["A", "B"]},
    ]
    assert body2["summary"] == {
        "deleted_count": 2,
        "affected_count": 0,
        "skipped_invalid_count": 0,
        "skipped_reused_count": 0,
        "touched_tree_count": 1,
        "root_group_count": 1,
        "surviving_count": 2,
    }


def test_rollback_preview_skips_invalid_cycle_messages(client):
    # Z 有效；x<->y 成环 invalid（同批）；撤销该批只删 Z，环邮件计入跳过数
    b1 = client.post("/batches", json={"messages": [
        _m("Z", sent="2026-09-18T08:00:00+00:00"),
        _m("x", irt="y", refs=["y"], sent="2026-09-18T09:00:00+00:00"),
        _m("y", irt="x", refs=["x"], sent="2026-09-18T10:00:00+00:00"),
    ]}).json()
    assert b1["totals"]["invalid"] == 2

    body = _preview(client, b1["batch_id"])
    assert body["deleted_ids"] == ["Z"]
    assert body["summary"]["deleted_count"] == 1
    assert body["summary"]["skipped_invalid_count"] == 2
    assert body["summary"]["skipped_reused_count"] == 0
    # 环邮件 x/y 不出现在删除集、影响或根分组中
    for group in body["root_groups"]:
        assert "x" not in group["members"] and "y" not in group["members"]

    # 只读：环邮件与有效邮件都原样保留
    assert client.get("/messages/x").json()["is_invalid"] is True
    assert client.get("/messages/y").json()["is_invalid"] is True
    assert client.get("/messages/Z").status_code == 200


def test_rollback_preview_skips_ids_reused_by_later_batches(client):
    # 批次 1 仅有 M（带保留期）：预演时原归档已被清理、同 ID 由更晚批次重用
    b1 = client.post("/batches", json={"messages": [
        _m("M", sent="2026-09-18T08:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]}).json()
    client.post("/retention/cleanups", json={"cutoff": "2026-09-20T00:00:00+00:00"})
    assert client.get("/messages/M").status_code == 404

    # 同 ID 由更晚批次重新归档（挂在新根 root2 下）
    b2 = client.post("/batches", json={"messages": [
        _m("root2", sent="2026-09-20T08:00:00+00:00"),
        _m("M", irt="root2", refs=["root2"], sent="2026-09-20T09:00:00+00:00"),
    ]}).json()
    assert client.get("/messages/M").json()["batch_id"] == b2["batch_id"]

    # 撤销 b1：M 已被更晚批次重用，不得删除；该批当前无任何归属有效邮件
    body = _preview(client, b1["batch_id"])
    assert body["deleted_ids"] == []
    assert body["affected"] == []
    assert body["root_groups"] == []
    assert body["summary"] == {
        "deleted_count": 0,
        "affected_count": 0,
        "skipped_invalid_count": 0,
        "skipped_reused_count": 1,
        "touched_tree_count": 0,
        "root_group_count": 0,
        "surviving_count": 0,
    }
    # 重用后的 M/root2 完好
    assert client.get("/messages/M").json()["parent_id"] == "root2"


def test_rollback_preview_is_read_only_and_repeatable(client):
    b1 = client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
    ]}).json()
    client.post("/batches", json={"messages": [
        _m("C", irt="B", refs=["A", "B"], sent="2026-09-18T10:00:00+00:00"),
    ]})

    snap_before = client.get(f"/batches/{b1['batch_id']}").json()
    forest_before = {
        mid: client.get(f"/messages/{mid}").json()["parent_id"]
        for mid in ("A", "B", "C")
    }
    thread_before = client.get("/threads/A").json()

    first = client.post(f"/batches/{b1['batch_id']}/rollback-preview")
    assert first.status_code == 200
    # 重复预演结论一致
    again = client.post(f"/batches/{b1['batch_id']}/rollback-preview").json()
    assert again == first.json()

    # 批次快照、邮件父指针、当前会话树均不被改写
    assert client.get(f"/batches/{b1['batch_id']}").json() == snap_before
    for mid, parent_id in forest_before.items():
        assert client.get(f"/messages/{mid}").json()["parent_id"] == parent_id
    assert client.get("/threads/A").json() == thread_before
    assert client.get("/batches").json()["total"] == 2


def test_rollback_preview_equivalent_to_retention_cleanup_compression(client):
    # 同一删除集：撤销预演的压缩结论必须与保留期 dry_run 清理逐字段一致。
    # 批次 1 拥有全部将到期节点 R/A/D；存活子孙 C/E 来自更晚批次（无保留期）。
    b1 = client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T08:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
        _m("A", irt="R", refs=["R"], sent="2026-09-18T09:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
        _m("D", irt="R", refs=["R"], sent="2026-09-18T11:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]}).json()
    client.post("/batches", json={"messages": [
        _m("C", irt="A", refs=["R", "A"], sent="2026-09-18T10:00:00+00:00"),
        _m("E", irt="C", refs=["C"], sent="2026-09-18T10:30:00+00:00"),
    ]})

    preview = _preview(client, b1["batch_id"])
    cleanup = client.post("/retention/cleanups", json={
        "cutoff": "2026-09-20T00:00:00+00:00", "dry_run": True,
    }).json()

    assert preview["deleted_ids"] == cleanup["deleted_ids"] == ["A", "D", "R"]
    assert preview["affected"] == cleanup["affected"]
