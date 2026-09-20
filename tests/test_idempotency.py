"""POST /batches：Idempotency-Key 幂等提交的端到端测试。

覆盖：同键同内容重放首次响应且不重复归档、同键不同内容 409、未携带键保持
原有行为、键格式校验、归一化摘要（字段顺序/排版/等价时区写法）、跨键独立，
以及真实并发下“以请求体摘要和键原子登记首次结果”（仅首个请求归档）。
"""

from __future__ import annotations

import threading


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


KEY = "idem-key-001"


def _post(client, payload, key=None, headers=None):
    h = dict(headers or {})
    if key is not None:
        h["Idempotency-Key"] = key
    return client.post("/batches", json=payload, headers=h)


def test_replay_same_key_same_payload_returns_original_batch_and_does_not_rearchive(client):
    payload = {"messages": [
        _m("A", sent="2026-09-18T09:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T10:00:00+00:00"),
    ]}

    first = _post(client, payload, KEY)
    assert first.status_code == 201
    assert first.headers["idempotency-replayed"] == "false"
    body = first.json()
    assert body["totals"]["unique_new"] == 2

    replay = _post(client, payload, KEY)
    assert replay.status_code == 201
    # 重放的是原始 batch 响应（含同一 batch_id / submitted_at），不产生新批次
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == body

    listed = client.get("/batches").json()
    assert listed["total"] == 1
    assert listed["batches"][0]["batch_id"] == body["batch_id"]
    assert client.get(f"/batches/{body['batch_id']}").json() == body

    # 幂等登记行恰好一条，指向唯一批次
    import sqlite3

    from app import config

    conn = sqlite3.connect(config.settings.database_path)
    try:
        rows = conn.execute(
            "SELECT key, request_digest, batch_id FROM idempotency_keys"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(KEY, rows[0][1], body["batch_id"])]
    assert len(rows[0][1]) == 64  # hex sha256


def test_replay_is_not_archived_even_when_ids_would_now_be_duplicates(client):
    payload = {"messages": [_m("A")]}
    first = _post(client, payload, KEY).json()
    assert first["totals"]["unique_new"] == 1
    assert first["duplicates"] == []

    # 同键同内容重放：A 已归档，但重放必须原样返回首次响应，
    # 不能走正常归档路径把 A 记成“历史归档重复”
    replay = _post(client, payload, KEY)
    assert replay.status_code == 201
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == first

    # 不带键的另一批相同内容仍按既有行为归档为历史重复
    normal = _post(client, payload)
    assert normal.status_code == 201
    assert normal.headers["idempotency-replayed"] == "false"
    nbody = normal.json()
    assert nbody["batch_id"] != first["batch_id"]
    assert nbody["totals"]["submitted"] == 1
    assert nbody["totals"]["unique_new"] == 0
    assert nbody["totals"]["archived_duplicates"] == 1


def test_same_key_different_payload_returns_409_and_writes_nothing(client):
    first = _post(client, {"messages": [_m("A")]}, KEY)
    assert first.status_code == 201

    conflict = _post(client, {"messages": [_m("B")]}, KEY)
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["error"] == "idempotency_conflict"
    assert "different request payload" in detail["message"]

    # 冲突响应不提供重放标记，且不产生新批次
    assert "idempotency-replayed" not in conflict.headers
    listed = client.get("/batches").json()
    assert listed["total"] == 1
    assert listed["batches"][0]["batch_id"] == first.json()["batch_id"]
    # B 未被归档
    assert client.get("/messages/B").status_code == 404

    # 冲突后，原键 + 原内容仍可正常重放首次结果
    replay = _post(client, {"messages": [_m("A")]}, KEY)
    assert replay.status_code == 201
    assert replay.json() == first.json()


def test_requests_without_key_keep_existing_behavior(client):
    r1 = client.post("/batches", json={"messages": [_m("A")]})
    r2 = client.post("/batches", json={"messages": [_m("A")]})
    assert r1.status_code == r2.status_code == 201
    # 每次都归档：两个不同批次
    assert r1.json()["batch_id"] != r2.json()["batch_id"]
    assert r2.json()["totals"]["archived_duplicates"] == 1
    assert client.get("/batches").json()["total"] == 2


def test_distinct_keys_archive_independently(client):
    payload = {"messages": [_m("A")]}
    k1 = _post(client, payload, "key-one")
    k2 = _post(client, payload, "key-two")
    assert k1.status_code == k2.status_code == 201
    assert k1.json()["batch_id"] != k2.json()["batch_id"]
    # 各自独立重放
    assert _post(client, payload, "key-one").json() == k1.json()
    assert _post(client, payload, "key-two").json() == k2.json()
    assert client.get("/batches").json()["total"] == 2


def test_canonical_digest_ignores_json_layout_field_order_and_equivalent_times(client):
    # 同一内容的不同 JSON 表达：紧凑/空白、字段顺序、等价时区写法（naive 视为 UTC）
    raw_a = (
        '{"messages":[{"message_id":"A","in_reply_to":null,"references":[],'
        '"sent_at":"2026-09-18T09:00:00Z","subject":"hello"}]}'
    )
    raw_b = (
        '{\n  "messages": [\n    {\n'
        '      "subject":  "hello",\n'
        '      "sent_at": "2026-09-18T09:00:00+00:00",\n'
        '      "references": [], "in_reply_to": null, "message_id": "A"\n'
        '    }\n  ]\n}'
    )

    first = client.post(
        "/batches", content=raw_a, headers={"Idempotency-Key": KEY, "Content-Type": "application/json"}
    )
    assert first.status_code == 201, first.text

    replay = client.post(
        "/batches", content=raw_b, headers={"Idempotency-Key": KEY, "Content-Type": "application/json"}
    )
    assert replay.status_code == 201, replay.text
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == first.json()
    assert client.get("/batches").json()["total"] == 1


def test_different_offset_for_different_instant_conflicts(client):
    first = _post(client, {"messages": [_m("A", sent="2026-09-18T09:00:00+00:00")]}, KEY)
    assert first.status_code == 201
    # 晚 1 小时的同一时刻写法 -> 内容不同 -> 409
    conflict = _post(client, {"messages": [_m("A", sent="2026-09-18T09:00:00+01:00")]}, KEY)
    assert conflict.status_code == 409


def test_message_order_is_part_of_the_payload(client):
    first = _post(client, {"messages": [_m("A"), _m("B")]}, KEY)
    assert first.status_code == 201
    swapped = _post(client, {"messages": [_m("B"), _m("A")]}, KEY)
    assert swapped.status_code == 409


def test_known_duplicate_ids_inside_replay_do_not_become_archived_duplicates(client):
    # 首次提交含一封与更早批次重复的邮件（历史归档重复诊断）
    client.post("/batches", json={"messages": [_m("Z")]})
    payload = {"messages": [_m("A"), _m("Z")]}
    first = _post(client, payload, KEY).json()
    assert first["totals"]["archived_duplicates"] == 1

    # 重放逐字节复现首次快照（历史重复仍是历史重复，A 不会变成历史重复）
    replay = _post(client, payload, KEY)
    assert replay.status_code == 201
    assert replay.json() == first


def test_invalid_idempotency_keys_are_rejected_with_422(client):
    payload = {"messages": [_m("A")]}
    # 空键 / 含空白 / 控制字符 / 超长 均拒绝，且不写库
    cases = [
        "",                                   # 空值
        "   ",                                # 纯空白
        "with space",                         # 内含空格
        "tab\tkey",                           # 制表符
        "k" * 256,                            # 超过 255
    ]
    for bad in cases:
        r = _post(client, payload, bad)
        assert r.status_code == 422, repr(bad)
    assert client.get("/batches").json()["total"] == 0

    # 边界值合法：恰好 255 个可见字符
    ok = _post(client, payload, "k" * 255)
    assert ok.status_code == 201, ok.text
    assert ok.headers["idempotency-replayed"] == "false"


def test_invalid_payload_with_key_is_422_and_does_not_register_key(client):
    # 键合法但请求体非法（空批次）-> 422，不登记键；
    # 随后用同键提交合法内容应为首次提交而非 409
    bad = _post(client, {"messages": []}, KEY)
    assert bad.status_code == 422

    good = _post(client, {"messages": [_m("A")]}, KEY)
    assert good.status_code == 201
    assert good.headers["idempotency-replayed"] == "false"


# ---------------------------------------------------------------------------
# 并发：以请求体摘要和键原子登记首次结果
# ---------------------------------------------------------------------------


def test_concurrent_same_key_same_payload_archives_once(client):
    n = 12
    payload = {"messages": [
        _m("A", sent="2026-09-18T09:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T10:00:00+00:00"),
    ]}
    barrier = threading.Barrier(n)
    results: list = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        r = _post(client, payload, KEY)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n
    assert all(r.status_code == 201 for r in results)
    bodies = {r.json()["batch_id"]: r.json() for r in results}
    assert len(bodies) == 1  # 仅归档一次，全部拿到同一批次响应
    first_flags = [r.headers["idempotency-replayed"] for r in results]
    assert first_flags.count("false") == 1
    assert first_flags.count("true") == n - 1

    assert client.get("/batches").json()["total"] == 1
    body = next(iter(bodies.values()))
    assert body["totals"]["unique_new"] == 2
    # 同内容重放不重复归档：A、B 各仅一封
    assert client.get("/messages/A").status_code == 200
    assert client.get("/messages/B").status_code == 200


def test_concurrent_same_key_different_payloads_one_wins_rest_409(client):
    n = 8
    barrier = threading.Barrier(n)
    outcomes: list = []
    outcomes_lock = threading.Lock()

    def worker(idx: int) -> None:
        payload = {"messages": [_m(f"M{idx}")]}
        barrier.wait()
        r = _post(client, payload, KEY)
        with outcomes_lock:
            outcomes.append((idx, r))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    created = [(i, r) for i, r in outcomes if r.status_code == 201]
    conflicts = [(i, r) for i, r in outcomes if r.status_code == 409]
    assert len(created) == 1
    assert len(conflicts) == n - 1

    winner_idx, winner = created[0]
    assert winner.headers["idempotency-replayed"] == "false"
    # 只有获胜者的邮件被归档
    listed = client.get("/batches").json()
    assert listed["total"] == 1
    assert listed["batches"][0]["batch_id"] == winner.json()["batch_id"]
    for i, r in outcomes:
        if i != winner_idx:
            assert client.get(f"/messages/M{i}").status_code == 404
            assert r.json()["detail"]["error"] == "idempotency_conflict"
    assert client.get(f"/messages/M{winner_idx}").status_code == 200


def test_concurrent_distinct_keys_all_archive_independently(client):
    n = 8
    barrier = threading.Barrier(n)
    batch_ids: list = []
    ids_lock = threading.Lock()

    def worker(idx: int) -> None:
        payload = {"messages": [_m(f"D{idx}")]}
        barrier.wait()
        r = _post(client, payload, f"key-{idx}")
        assert r.status_code == 201, r.text
        with ids_lock:
            batch_ids.append(r.json()["batch_id"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(batch_ids)) == n
    assert client.get("/batches").json()["total"] == n
