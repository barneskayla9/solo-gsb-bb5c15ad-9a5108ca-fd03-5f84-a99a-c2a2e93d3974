#!/usr/bin/env python3
"""食品换线放行服务 —— 批次产线归属更正（换线）端到端调用示例（仅使用 Python 标准库）。

演示流程（独立产线 LT-A / LT-B，避免与其他示例的批次冲突）：
  1. 登记原料批号 TR-RM-P（peanut）、TR-RM-M（milk）与两条产线的批次：
       LT-A：TA1 使用 TR-RM-P、声明 peanut -> RELEASE，peanut 残留在产线
             TA2 开工前清洁 peanut、无原料 -> RELEASE（产线被清洁干净）
             TA3 无原料无清洁 -> RELEASE（无残留可继承）
       LT-B：TB1 使用 TR-RM-M、声明 milk -> RELEASE，milk 残留在产线
             TB2 无清洁、声明 milk -> RELEASE（仍带 milk 残留）
  2. 创建并确认出货核验单 TV-<时间戳>：[TA3]
  3. 拒绝场景：未知批次 404、未知目标产线 404、目标线与原线相同 409、空目标线 400
  4. 换线 TA2：LT-A -> LT-B（误归产线更正归属）：
       TA2 移至 LT-B 末尾获得新顺序号（LT-A 的 seq=2 留空不复用）；
       原线 LT-A 从移出位置起重算：TA2 的清洁不再发生，TA3 重新继承 TA1 的
       peanut -> HOLD 并追加 v2；目标线 LT-B 从移入批次起重算：TA2 带入 TB2
       留下的 milk 残留且未声明 -> HOLD 并追加 v2；
       含 TA3 的已确认核验单 TV 转 PENDING_REVIEW，历次确认快照保持原样。
  5. 历史版本：TA2 的 v1 仍记录原产线 LT-A/seq2，v2 记录新归属 LT-B/seq3。
  6. 出货资格：直接用料未变，资格版本不因换线递增。
  7. 原线顺序号留空：新登记 TA4 在 LT-A 顺延至 seq=4（不复用 2）。
  8. 作废批次不得换线（409 BATCH_VOIDED）。
  9. 现有接口保持可用：更正 TA3 声明 -> 重新核验 -> 再次确认。

用法：
    python examples/batch_transfer_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/batch_transfer_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE_A = "LT-A"
LINE_B = "LT-B"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, status: int, body: dict) -> None:
    print(f"[{status}] {label}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def ensure_lot(lot_no: str, material: str, allergens: list[str]) -> None:
    status, body = call("POST", "/api/material-lots", {
        "lot_no": lot_no, "material": material, "allergens": allergens,
    })
    if status == 201:
        print(f"  已登记原料批号 {lot_no}（{material}，{allergens}）")
    elif status == 409:
        print(f"  原料批号 {lot_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记原料批号失败: {body}")


def ensure_batch(batch_no: str, payload: dict) -> None:
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        adj = body["adjudication"]
        print(f"  已登记批次 {batch_no}（{payload['line_id']}#{body['seq']}）"
              f" => {adj['disposition']}（v{adj['version']}）")
    elif status == 409:
        print(f"  批次 {batch_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记批次失败: {body}")


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))

    section("1. 准备数据：LT-A（TA1 残留 peanut、TA2 清洁、TA3）与 LT-B（TB1/TB2 带 milk 残留）")
    ensure_lot("TR-RM-P", "换线示例花生酱", ["peanut"])
    ensure_lot("TR-RM-M", "换线示例奶粉", ["milk"])
    ensure_batch("TA1", {
        "batch_no": "TA1", "line_id": LINE_A,
        "material_lots": ["TR-RM-P"], "declared_allergens": ["peanut"], "cleaned_allergens": [],
    })
    ensure_batch("TA2", {
        "batch_no": "TA2", "line_id": LINE_A,
        "material_lots": [], "declared_allergens": [], "cleaned_allergens": ["peanut"],
    })
    ensure_batch("TA3", {
        "batch_no": "TA3", "line_id": LINE_A,
        "material_lots": [], "declared_allergens": [], "cleaned_allergens": [],
    })
    ensure_batch("TB1", {
        "batch_no": "TB1", "line_id": LINE_B,
        "material_lots": ["TR-RM-M"], "declared_allergens": ["milk"], "cleaned_allergens": [],
    })
    ensure_batch("TB2", {
        "batch_no": "TB2", "line_id": LINE_B,
        "material_lots": [], "declared_allergens": ["milk"], "cleaned_allergens": [],
    })

    doc_no = f"TV-{suffix}"
    section(f"2. 创建并确认核验单 {doc_no}：[TA3]")
    status, doc = call("POST", "/api/verifications", {
        "verification_no": doc_no, "batch_nos": ["TA3"],
    })
    assert status == 201, doc
    status, doc = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("确认（预期 200 CONFIRMED）", status, doc)
    assert status == 200 and doc["status"] == "CONFIRMED", doc

    section("3. 拒绝场景：未知批次 404 / 未知目标产线 404 / 同线 409 / 空目标线 400")
    status, body = call("POST", "/api/batches/NO-SUCH/transfer", {"target_line_id": LINE_B})
    print(f"[{status}] 未知批次换线: {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "BATCH_NOT_FOUND", body
    status, body = call("POST", "/api/batches/TA2/transfer", {"target_line_id": "L-NOPE"})
    print(f"[{status}] 未知目标产线: {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "LINE_NOT_FOUND", body
    status, body = call("POST", "/api/batches/TA2/transfer", {"target_line_id": LINE_A})
    print(f"[{status}] 目标线与原线相同: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "SAME_LINE_TRANSFER", body
    status, body = call("POST", "/api/batches/TA2/transfer", {"target_line_id": "  "})
    print(f"[{status}] 空目标产线: {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "INVALID_PAYLOAD", body
    # 失败不留部分归属或裁决版本
    ta2 = call("GET", "/api/batches/TA2")[1]
    assert ta2["line_id"] == LINE_A and ta2["seq"] == 2
    assert ta2["adjudication"]["version"] == 1

    section(f"4. 换线 TA2：{LINE_A} -> {LINE_B}（误归产线更正归属）")
    elig_before = call("GET", "/api/batches/TA2")[1]["shipment_eligibility"]["version"]
    status, moved = call("POST", "/api/batches/TA2/transfer", {"target_line_id": LINE_B})
    show("换线结果（新旧归属 + 两线重算 + 受影响核验单）", status, moved)
    assert status == 200, moved
    assert moved["from"] == {"line_id": LINE_A, "seq": 2}
    assert moved["to"] == {"line_id": LINE_B, "seq": 3}
    assert moved["transferred"]["line_id"] == LINE_B
    assert moved["transferred"]["batch_no"] == "TA2"  # 批次号不变

    # 原线从移出位置起重算：TA2 的清洁不再发生，TA3 重新继承 TA1 的 peanut -> HOLD
    rec_o = {s["batch_no"]: s for s in moved["recomputed_origin"]}
    assert list(rec_o) == ["TA3"], rec_o
    assert rec_o["TA3"]["disposition"] == "HOLD" and rec_o["TA3"]["version"] == 2
    assert rec_o["TA3"]["actual"] == ["peanut"]
    # 目标线从移入批次起重算：TA2 带入 TB2 留下的 milk 残留且未声明 -> HOLD
    rec_t = {s["batch_no"]: s for s in moved["recomputed_target"]}
    assert list(rec_t) == ["TA2"], rec_t
    assert rec_t["TA2"]["disposition"] == "HOLD" and rec_t["TA2"]["version"] == 2
    assert rec_t["TA2"]["actual"] == ["milk"]
    # 含被重算批次的已确认核验单转待复核
    affected = {v["verification_no"]: v["status"] for v in moved["affected_verifications"]}
    assert affected == {doc_no: "PENDING_REVIEW"}, affected
    doc = call("GET", f"/api/verifications/{doc_no}")[1]
    assert doc["status"] == "PENDING_REVIEW"
    # 历次确认快照保持原样
    assert len(doc["confirmations"]) == 1
    assert doc["confirmations"][0]["batches"][0]["version"] == 1

    section("5. 历史版本：原产线与顺序号不改，换线追加 TRANSFER_LINE 版本")
    status, hist = call("GET", "/api/batches/TA2/history")
    show("TA2 历史裁决版本", status, hist)
    versions = hist["history"]
    assert hist["line_id"] == LINE_B and hist["seq"] == 3  # 顶层为当前归属
    assert [(v["line_id"], v["seq"]) for v in versions] == [(LINE_A, 2), (LINE_B, 3)]
    assert [v["trigger"] for v in versions] == ["REGISTER", "TRANSFER_LINE"]

    section("6. 出货资格：直接用料未变，资格版本不因换线递增")
    elig = call("GET", "/api/batches/TA2/eligibility")[1]
    assert elig["eligibility"]["version"] == elig_before == 1
    assert [v["trigger"] for v in elig["history"]] == ["REGISTER"]
    print(f"  TA2 出货资格版本仍为 v{elig['eligibility']['version']}（{elig['eligibility']['status']}），未因换线递增")

    section("7. 原线顺序号留空不复用：新登记 TA4 顺延至 LT-A#4")
    status, ta4 = call("POST", "/api/batches", {
        "batch_no": "TA4", "line_id": LINE_A,
        "material_lots": [], "declared_allergens": ["peanut"], "cleaned_allergens": [],
    })
    assert status == 201, ta4
    print(f"  TA4 登记于 {ta4['line_id']}#{ta4['seq']}（seq=2 空洞未复用）")
    assert ta4["seq"] == 4

    section("8. 作废批次不得换线（409 BATCH_VOIDED）")
    status, body = call("POST", "/api/batches/TA1/void", {"reason": "误登记，实际未投产"})
    assert status == 200 and body["changed"] is True, body
    status, body = call("POST", "/api/batches/TA1/transfer", {"target_line_id": LINE_B})
    print(f"[{status}] 作废批次换线: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "BATCH_VOIDED", body

    section("9. 现有接口保持可用：更正 TA3 声明 -> 重新核验 -> 再次确认")
    # TA1 作废退出残留链后 TA3 已无残留；更正声明走既有批次更正接口（目标线重算）
    status, corrected = call("PUT", "/api/batches/TA3", {"declared_allergens": ["peanut"]})
    assert status == 200, corrected
    assert corrected["corrected"]["adjudication"]["disposition"] == "RELEASE"
    print(f"  更正 TA3 声明 peanut -> {corrected['corrected']['adjudication']['disposition']}")
    status, doc = call("POST", f"/api/verifications/{doc_no}/reverify")
    assert status == 200 and doc["status"] == "DRAFT", doc
    status, doc = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("再次确认（预期 200 CONFIRMED，确认历史只追加）", status, doc)
    assert status == 200 and doc["status"] == "CONFIRMED", doc
    assert len(doc["confirmations"]) == 2

    section("完成")
    print(f"TA2 已移至 {LINE_B}#3（批次号不变），{LINE_A} 的 seq=2 留空不复用；")
    print("两线各自重算并追加历史版本，历史版本中的原产线与顺序号保持原样；")
    print(f"核验单 {doc_no} 经历 确认 -> 待复核 -> 重新核验 -> 再次确认，历次确认快照均保留。")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
