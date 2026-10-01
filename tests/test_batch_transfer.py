"""批次产线归属更正（换线）测试。

覆盖：
- 基本换线：凭有效批次号与另一条已登记产线编号更正归属；批次移至目标线末尾
  获得新顺序号，原线顺序号留空不复用；响应返回新旧归属、两线重算结果与受影响核验单；
- 两线重算：原线从移出位置起、目标线从移入批次起按各自有效批次顺序重算，
  被重算批次追加 TRANSFER_LINE 历史版本；历史版本中的原产线与顺序号不改；
- 出货资格：直接用料未变，资格版本不因换线递增（含批号已锁定场景）；
- 核验单：含被重算批次的已确认单据转待复核，历次确认快照保持原样；草稿不转态；
- 拒绝：未知批次 404、未知目标线 404、已作废批次 409、目标线与原线相同 409、
  空目标线 400；失败整体回滚，不留下部分归属或裁决版本；
- 兼容性：批次号不变，换线后查询/更正/核验/资格接口约定保持可用。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no, material="原料", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def transfer(client, batch_no, target_line_id):
    return client.post(
        f"/api/batches/{batch_no}/transfer", json={"target_line_id": target_line_id}
    )


def create_verification(client, verification_no, batch_nos):
    return client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )


# ---------------------------------------------------------------- 基本换线


def test_transfer_moves_batch_to_target_line_tail(client):
    add_batch(client, "A1", line_id="L1")
    add_batch(client, "A2", line_id="L1")
    add_batch(client, "A3", line_id="L1")
    add_batch(client, "B1", line_id="L2")

    resp = transfer(client, "A2", "L2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 新旧归属
    assert body["batch_no"] == "A2"
    assert body["from"] == {"line_id": "L1", "seq": 2}
    assert body["to"] == {"line_id": "L2", "seq": 2}
    # 转移后的完整批次记录：批次号不变、归属更新
    moved = body["transferred"]
    assert moved["batch_no"] == "A2"
    assert moved["line_id"] == "L2" and moved["seq"] == 2
    assert moved["status"] == "ACTIVE"
    assert moved["adjudication"]["version"] == 2  # 目标线重算追加版本
    # 两线重算结果与受影响核验单字段齐全
    assert [s["batch_no"] for s in body["recomputed_target"]] == ["A2"]
    assert [s["batch_no"] for s in body["recomputed_origin"]] == ["A3"]
    assert body["affected_verifications"] == []

    # 详情与列表反映新归属
    detail = client.get("/api/batches/A2").json()
    assert detail["line_id"] == "L2" and detail["seq"] == 2
    l1 = client.get("/api/batches", params={"line_id": "L1"}).json()["batches"]
    assert [b["batch_no"] for b in l1] == ["A1", "A3"]
    l2 = client.get("/api/batches", params={"line_id": "L2"}).json()["batches"]
    assert [b["batch_no"] for b in l2] == ["B1", "A2"]


def test_transfer_leaves_source_seq_gap_unreused(client):
    add_batch(client, "A1", line_id="L1")
    add_batch(client, "A2", line_id="L1")
    add_batch(client, "A3", line_id="L1")
    add_batch(client, "B1", line_id="L2")

    transfer(client, "A2", "L2")
    # 原线顺序号 2 留空：新登记批次顺延下一号，不复用空洞
    a4 = add_batch(client, "A4", line_id="L1")
    assert a4["seq"] == 4
    # 目标线末尾顺延
    b2 = add_batch(client, "B2", line_id="L2")
    assert b2["seq"] == 3


def test_transfer_target_seq_skips_voided_batches(client):
    add_batch(client, "A1", line_id="L1")
    add_batch(client, "B1", line_id="L2")
    add_batch(client, "B2", line_id="L2")
    client.post("/api/batches/B2/void", json={"reason": "误登记"})

    resp = transfer(client, "A1", "L2")
    # 目标线已作废批次占用的 seq 同样不复用
    assert resp.json()["to"] == {"line_id": "L2", "seq": 3}


def test_transfer_last_batch_leaves_origin_without_recompute(client):
    add_batch(client, "A1", line_id="L1")
    add_batch(client, "A2", line_id="L1")
    add_batch(client, "B1", line_id="L2")

    body = transfer(client, "A2", "L2").json()
    assert body["recomputed_origin"] == []  # 原线移出位置之后无有效批次
    assert [s["batch_no"] for s in body["recomputed_target"]] == ["A2"]
    assert client.get("/api/batches/A1").json()["adjudication"]["version"] == 1


# ---------------------------------------------------------------- 两线重算


def test_transfer_recomputes_both_lines_by_carryover(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_lot(client, "RM-M", "奶粉", ["milk"])
    # L1：A1 残留 peanut；A2 开工前清洁 peanut；A3 无残留 RELEASE
    add_batch(client, "A1", line_id="L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "A2", line_id="L1", lots=[], declared=[], cleaned=["peanut"])
    add_batch(client, "A3", line_id="L1", lots=[], declared=[])
    # L2：B1 残留 milk；B2 未清洁、声明 milk（RELEASE，仍带 milk 残留）
    add_batch(client, "B1", line_id="L2", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", line_id="L2", lots=[], declared=["milk"])

    resp = transfer(client, "A2", "L2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from"] == {"line_id": "L1", "seq": 2}
    assert body["to"] == {"line_id": "L2", "seq": 3}

    # 原线从移出位置起重算：A2 的清洁不再发生，A3 重新继承 A1 的 peanut -> HOLD
    rec_o = {s["batch_no"]: s for s in body["recomputed_origin"]}
    assert list(rec_o) == ["A3"]
    assert rec_o["A3"]["actual"] == ["peanut"]
    assert rec_o["A3"]["disposition"] == "HOLD"
    assert rec_o["A3"]["version"] == 2

    # 目标线从移入批次起重算：A2 带入 B2 留下的 milk 残留且未声明 -> HOLD
    rec_t = {s["batch_no"]: s for s in body["recomputed_target"]}
    assert list(rec_t) == ["A2"]
    assert rec_t["A2"]["actual"] == ["milk"]
    assert rec_t["A2"]["undeclared"] == ["milk"]
    assert rec_t["A2"]["disposition"] == "HOLD"
    assert rec_t["A2"]["version"] == 2

    # 两线其余批次不受影响，不产生新版本
    assert client.get("/api/batches/A1").json()["adjudication"]["version"] == 1
    assert client.get("/api/batches/B1").json()["adjudication"]["version"] == 1
    assert client.get("/api/batches/B2").json()["adjudication"]["version"] == 1


def test_transfer_appends_history_and_keeps_original_line_seq(client):
    add_batch(client, "A1", line_id="L1")
    add_batch(client, "A2", line_id="L1")
    add_batch(client, "B1", line_id="L2")
    add_batch(client, "B2", line_id="L2")

    transfer(client, "A1", "L2")  # A1: L1#1 -> L2#3

    hist = client.get("/api/batches/A1/history").json()
    assert hist["line_id"] == "L2" and hist["seq"] == 3  # 顶层为当前归属
    versions = hist["history"]
    assert [v["version"] for v in versions] == [1, 2]
    # 历史版本中的原产线与顺序号不改
    assert (versions[0]["line_id"], versions[0]["seq"]) == ("L1", 1)
    assert versions[0]["trigger"] == "REGISTER"
    # 换线重算追加的版本记录新归属
    assert (versions[1]["line_id"], versions[1]["seq"]) == ("L2", 3)
    assert versions[1]["trigger"] == "TRANSFER_LINE"


def test_transfer_back_and_forth_appends_versions_with_each_home(client):
    add_batch(client, "A1", line_id="L1")
    add_batch(client, "A2", line_id="L1")
    add_batch(client, "B1", line_id="L2")

    transfer(client, "A1", "L2")
    body = transfer(client, "A1", "L1").json()
    assert body["from"] == {"line_id": "L2", "seq": 2}
    # 回到原线仍取末尾新号，原 seq=1 空洞不复用
    assert body["to"] == {"line_id": "L1", "seq": 3}

    hist = client.get("/api/batches/A1/history").json()["history"]
    assert [(v["line_id"], v["seq"]) for v in hist] == [("L1", 1), ("L2", 2), ("L1", 3)]
    assert [v["trigger"] for v in hist] == ["REGISTER", "TRANSFER_LINE", "TRANSFER_LINE"]


# ---------------------------------------------------------------- 出货资格


def test_transfer_does_not_advance_shipment_eligibility(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "A1", line_id="L1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B1", line_id="L2")
    # 批号锁定使 A1 资格版本升至 v2（BLOCKED）
    client.post("/api/material-lots/RM-M/lock", json={"reason": "待检风险"})
    assert client.get("/api/batches/A1").json()["shipment_eligibility"]["version"] == 2

    resp = transfer(client, "A1", "L2")
    assert resp.status_code == 200, resp.text
    # 直接用料未变：出货资格版本不因换线递增，状态与锁定批号保持
    se = resp.json()["transferred"]["shipment_eligibility"]
    assert se["version"] == 2
    assert se["status"] == "BLOCKED"
    assert se["locked_lots"] == ["RM-M"]
    # 资格历史无新条目
    elig = client.get("/api/batches/A1/eligibility").json()
    assert elig["line_id"] == "L2"
    assert [v["version"] for v in elig["history"]] == [1, 2]
    assert [v["trigger"] for v in elig["history"]] == ["REGISTER", "LOT_LOCK"]


# ---------------------------------------------------------------- 核验单联动


def test_transfer_flips_confirmed_verifications_and_keeps_snapshots(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "A1", line_id="L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "A2", line_id="L1", lots=[], declared=[], cleaned=["peanut"])
    add_batch(client, "A3", line_id="L1", lots=[], declared=[])
    add_batch(client, "B1", line_id="L2")
    # 一张单据含转移批次本身，一张只含原线下游被重算批次
    create_verification(client, "V-MOVE", ["A2"])
    create_verification(client, "V-DOWN", ["A3"])
    assert client.post("/api/verifications/V-MOVE/confirm").status_code == 200
    assert client.post("/api/verifications/V-DOWN/confirm").status_code == 200

    resp = transfer(client, "A2", "L2")
    affected = {v["verification_no"]: v["status"] for v in resp.json()["affected_verifications"]}
    assert affected == {"V-MOVE": "PENDING_REVIEW", "V-DOWN": "PENDING_REVIEW"}

    doc = client.get("/api/verifications/V-MOVE").json()
    assert doc["status"] == "PENDING_REVIEW"
    # 历次确认快照保持原样
    assert len(doc["confirmations"]) == 1
    snap = doc["confirmations"][0]["batches"][0]
    assert snap["batch_no"] == "A2"
    assert snap["version"] == 1 and snap["disposition"] == "RELEASE"
    # 逐批记录反映新归属与新版本，变化原因可见 TRANSFER_LINE
    entry = doc["batches"][0]
    assert entry["line_id"] == "L2" and entry["seq"] == 2
    assert entry["current"]["version"] == 2
    assert entry["change_reasons"][0]["trigger"] == "TRANSFER_LINE"

    # 转移批次在目标线无残留仍 RELEASE：重新核验后可再次确认
    assert client.post("/api/verifications/V-MOVE/reverify").status_code == 200
    assert client.post("/api/verifications/V-MOVE/confirm").status_code == 200
    doc = client.get("/api/verifications/V-MOVE").json()
    assert doc["status"] == "CONFIRMED"
    assert len(doc["confirmations"]) == 2  # 两次确认快照均保留


def test_draft_verification_not_flipped_by_transfer(client):
    add_batch(client, "A1", line_id="L1")
    add_batch(client, "B1", line_id="L2")
    create_verification(client, "VD", ["A1"])

    transfer(client, "A1", "L2")
    doc = client.get("/api/verifications/VD").json()
    assert doc["status"] == "DRAFT"  # 草稿不转态，快照待重新核验刷新
    assert doc["batches"][0]["line_id"] == "L2"


# ---------------------------------------------------------------- 拒绝与原子性


def test_transfer_rejects_unknown_batch_and_unknown_line(client):
    add_batch(client, "A1", line_id="L1")

    resp = transfer(client, "NOPE", "L1")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_FOUND"

    resp = transfer(client, "A1", "L9")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "LINE_NOT_FOUND"


def test_transfer_rejects_same_line(client):
    add_batch(client, "A1", line_id="L1")
    add_batch(client, "A2", line_id="L1")

    resp = transfer(client, "A2", "L1")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SAME_LINE_TRANSFER"


def test_transfer_rejects_voided_batch(client):
    add_batch(client, "A1", line_id="L1")
    add_batch(client, "B1", line_id="L2")
    client.post("/api/batches/A1/void", json={"reason": "误登记"})

    resp = transfer(client, "A1", "L2")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_VOIDED"


def test_transfer_rejects_blank_target_line(client):
    add_batch(client, "A1", line_id="L1")
    for bad in ("", "   "):
        resp = transfer(client, "A1", bad)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"


def test_failed_transfer_is_atomic_no_partial_home_or_versions(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "A1", line_id="L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "A2", line_id="L1", lots=[], declared=[])
    add_batch(client, "B1", line_id="L2")
    v_before = client.get("/api/batches/A2").json()["adjudication"]["version"]

    for resp in (
        transfer(client, "A2", "L1"),   # 目标线与原线相同
        transfer(client, "A2", "L9"),   # 目标线未登记
        transfer(client, "A2", " "),    # 空目标线
    ):
        assert resp.status_code in (400, 404, 409)
        a2 = client.get("/api/batches/A2").json()
        # 归属不变、不产生部分裁决版本
        assert a2["line_id"] == "L1" and a2["seq"] == 2
        assert a2["adjudication"]["version"] == v_before
        hist = client.get("/api/batches/A2/history").json()["history"]
        assert len(hist) == v_before
        # 两线其他批次同样无新版本
        assert client.get("/api/batches/B1").json()["adjudication"]["version"] == 1


# ---------------------------------------------------------------- 兼容性


def test_existing_interfaces_keep_working_after_transfer(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "A1", line_id="L1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B1", line_id="L2")
    transfer(client, "A1", "L2")

    # 批次号不变：更正接口在目标线按序重算
    resp = client.put("/api/batches/A1", json={"declared_allergens": []})
    assert resp.status_code == 200, resp.text
    assert resp.json()["corrected"]["line_id"] == "L2"
    assert [s["batch_no"] for s in resp.json()["recomputed"]] == ["A1"]
    assert client.get("/api/batches/A1").json()["adjudication"]["disposition"] == "HOLD"
    # 原产线不受更正影响
    assert client.get("/api/batches/B1").json()["adjudication"]["version"] == 1

    # 清洁更正入口同样可用
    resp = client.put("/api/batches/A1/cleaning", json={"cleaned_allergens": ["milk"]})
    assert resp.status_code == 200

    # 新核验单可关联换线批次并按约定工作
    client.put("/api/batches/A1", json={"declared_allergens": ["milk"]})
    assert create_verification(client, "V1", ["A1"]).status_code == 201
    assert client.post("/api/verifications/V1/confirm").status_code == 200

    # 出货资格与统计接口可用且反映新归属
    elig = client.get("/api/batches/A1/eligibility").json()
    assert elig["line_id"] == "L2"
    rows = client.get("/api/eligibilities", params={"line_id": "L2"}).json()["eligibilities"]
    assert "A1" in {r["batch_no"] for r in rows}
