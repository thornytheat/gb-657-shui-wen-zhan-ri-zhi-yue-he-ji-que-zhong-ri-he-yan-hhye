"""M8 关账与补账：关账保持原账、跨月正负差额、连续补读、撤销反向分录、
中断续跑、同笔重试、过期决定拒绝与核对。

样例 readings_ledger.csv 的原始账单（已用管线复算核对）：
- M0002（TOTAL，倍率 40）：2026-01 26.87 / 2026-02 24.27 / 2026-03 26.87 / 2026-04 26.00
- M0008（峰平谷）：2026-01 230.64 / 2026-02 208.32 / 2026-03 230.64 / 2026-04 223.20

补读 A（M0002 2026-02-01 → 1001.0）：1 月 40.000 kWh → 26.00；
2 月 37.966 kWh → 24.68；3 月 42.034 kWh → 27.32。
补读 B（A + 2026-03-01 → 1002.0）：1/2/3/4 月各 40.000 kWh → 26.00。
补读 C（M0008 2026-02-01 → 1300.0）：1 月 220.14 / 2 月 213.30 / 3 月 236.16。
"""
from __future__ import annotations

import pytest

REREAD_A = [{"meter_id": "M0002", "register": "TOTAL",
             "read_at": "2026-02-01T00:00:00+08:00", "reading": "1001.0",
             "unit": "kWh"}]
REREAD_B = REREAD_A + [{"meter_id": "M0002", "register": "TOTAL",
                        "read_at": "2026-03-01T00:00:00+08:00", "reading": "1002.0",
                        "unit": "kWh"}]
REREAD_C = [{"meter_id": "M0008", "register": "TOTAL",
             "read_at": "2026-02-01T00:00:00+08:00", "reading": "1300.0",
             "unit": "kWh"}]

EXPECTED_BILLS = {
    ("2026-01", "M0002"): "26.87", ("2026-02", "M0002"): "24.27",
    ("2026-03", "M0002"): "26.87", ("2026-04", "M0002"): "26.00",
    ("2026-01", "M0008"): "230.64", ("2026-02", "M0008"): "208.32",
    ("2026-03", "M0008"): "230.64", ("2026-04", "M0008"): "223.20",
}


@pytest.fixture()
def ledger_client(client):
    response = client.post("/api/runs", json={"sample": "readings_ledger.csv"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done"
    return client


def _version(client) -> int:
    return client.get("/api/ledger").json()["version"]


def _close(client, month: str, version: int):
    return client.post("/api/ledger/close",
                       json={"month": month, "expected_version": version})


def _reread(client, readings) -> dict:
    response = client.post("/api/ledger/reread", json={"readings": readings})
    assert response.status_code == 200, response.text
    return response.json()


def _supplement(client, reread_id: str, target_month: str, version: int, **extra):
    return client.post("/api/ledger/supplement",
                       json={"reread_id": reread_id, "target_month": target_month,
                             "expected_version": version, **extra})


def _amounts(run: dict) -> dict:
    return {(e["source_month"], e["meter_id"]): e["amount_delta"]
            for e in run["entries"] if e["kind"] == "supplement"}


def _closed(client) -> dict:
    return {item["month"]: item
            for item in client.get("/api/ledger").json()["closed_months"]}


def test_sample_bills_match_pipeline(ledger_client):
    bills = ledger_client.post("/api/months/allocate").json()["bills"]
    got = {(b["month"], b["meter_id"]): b["amount_yuan"] for b in bills}
    assert got == EXPECTED_BILLS


def test_close_freezes_original_bill(ledger_client):
    client = ledger_client
    assert _close(client, "2026-01", 0).status_code == 200
    reread = _reread(client, REREAD_A)
    response = _supplement(client, reread["reread_id"], "2026-04", 1)
    assert response.status_code == 200, response.text
    # 原账快照保持原值，补账不改已关账月
    closed = _closed(client)
    assert closed["2026-01"]["amount_yuan"] == "257.51"  # 26.87 + 230.64
    # 原批次的账单也不被覆盖
    bills = client.post("/api/months/allocate").json()["bills"]
    got = {(b["month"], b["meter_id"]): b["amount_yuan"] for b in bills}
    assert got == EXPECTED_BILLS


def test_reread_recomputes_usage_by_actual_intervals(ledger_client):
    reread = _reread(ledger_client, REREAD_A)
    latest = {(b["month"], b["meter_id"]): (b["usage_kwh"], b["amount_yuan"])
              for b in reread["latest_bills"]}
    # 补读点落在 2 月 1 日：1 月区间缩短为整月 40.000 kWh，2/3 月按实际天数重切
    assert latest[("2026-01", "M0002")] == ("40.000", "26.00")
    assert latest[("2026-02", "M0002")] == ("37.966", "24.68")
    assert latest[("2026-03", "M0002")] == ("42.034", "27.32")
    assert latest[("2026-04", "M0002")] == ("40.000", "26.00")


def test_supplement_posts_cross_month_signed_diffs_to_open_month(ledger_client):
    client = ledger_client
    assert _close(client, "2026-01", 0).status_code == 200
    assert _close(client, "2026-02", 1).status_code == 200
    reread = _reread(client, REREAD_A)
    diffs = {(d["month"], d["meter_id"]): d["unposted_diff"]
             for d in reread["closed_month_diffs"]}
    assert diffs[("2026-01", "M0002")] == "-0.87"   # 退补
    assert diffs[("2026-02", "M0002")] == "0.41"    # 补收
    assert diffs[("2026-01", "M0008")] == "0.00"

    response = _supplement(client, reread["reread_id"], "2026-04", 2)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "done"
    entries = {e["source_month"]: e for e in run["entries"]}
    # 正负差额都按原价 0.65 计费，且都记到指定开放月 2026-04
    jan, feb = entries["2026-01"], entries["2026-02"]
    assert jan["amount_delta"] == "-0.87" and jan["direction"] == "refund"
    assert jan["usage_delta"] == "-1.334"
    assert jan["components"][0]["price"] == "0.65"
    assert feb["amount_delta"] == "0.41" and feb["direction"] == "charge"
    assert feb["usage_delta"] == "0.633"
    for entry in run["entries"]:
        assert entry["target_month"] == "2026-04"
        assert entry["reread_id"] == reread["reread_id"]  # 保留补读批次关系
        assert entry["status"] == "posted"
    # 核对：原账 + 净补账 = 最新应收
    reconcile = client.get("/api/ledger/reconcile").json()
    assert reconcile["all_balanced"] is True
    rows = {(r["month"], r["meter_id"]): r for r in reconcile["rows"]}
    assert rows[("2026-01", "M0002")]["original"] == "26.87"
    assert rows[("2026-01", "M0002")]["net_supplement"] == "-0.87"
    assert rows[("2026-01", "M0002")]["latest"] == "26.00"


def test_consecutive_rereads_bill_only_unposted_diff(ledger_client):
    client = ledger_client
    assert _close(client, "2026-01", 0).status_code == 200
    assert _close(client, "2026-02", 1).status_code == 200
    first = _reread(client, REREAD_A)
    run1 = _supplement(client, first["reread_id"], "2026-04", 2).json()
    assert _amounts(run1) == {("2026-01", "M0002"): "-0.87",
                              ("2026-02", "M0002"): "0.41"}
    # 第二次补读含第一次的读数：只补尚未入账的差额
    second = _reread(client, REREAD_B)
    run2 = _supplement(client, second["reread_id"], "2026-04", 3).json()
    # 1 月已补平（26.00 = 26.87 − 0.87），2 月只补 26.00 − 24.68 = 1.32
    assert _amounts(run2) == {("2026-02", "M0002"): "1.32"}
    assert client.get("/api/ledger/reconcile").json()["all_balanced"] is True


def test_reverse_uses_reversal_entries_and_keeps_originals(ledger_client):
    client = ledger_client
    assert _close(client, "2026-01", 0).status_code == 200
    assert _close(client, "2026-02", 1).status_code == 200
    reread = _reread(client, REREAD_A)
    run = _supplement(client, reread["reread_id"], "2026-04", 2).json()

    response = client.post("/api/ledger/reverse",
                           json={"request_id": run["request_id"],
                                 "expected_version": 3})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "reversed"
    reversals = {r["source_month"]: r for r in body["reversals"]}
    assert reversals["2026-01"]["amount_delta"] == "0.87"    # 原分录的反向
    assert reversals["2026-02"]["amount_delta"] == "-0.41"
    assert all(r["kind"] == "reversal" for r in body["reversals"])
    assert all(r["reversal_of"] for r in body["reversals"])

    entries = client.get("/api/ledger").json()["entries"]
    originals = [e for e in entries if e["kind"] == "supplement"]
    # 旧账不删：原分录仍在，状态置为 reversed
    assert len(originals) == 2
    assert all(e["status"] == "reversed" for e in originals)
    # 净补账归零 → 核对重新显示待补差额
    reconcile = client.get("/api/ledger/reconcile").json()
    assert reconcile["all_balanced"] is False
    rows = {(r["month"], r["meter_id"]): r for r in reconcile["rows"]}
    assert rows[("2026-01", "M0002")]["net_supplement"] == "0.00"
    assert rows[("2026-01", "M0002")]["drift"] == "-0.87"


def test_supplement_again_after_reverse(ledger_client):
    client = ledger_client
    assert _close(client, "2026-01", 0).status_code == 200
    assert _close(client, "2026-02", 1).status_code == 200
    reread = _reread(client, REREAD_A)
    run = _supplement(client, reread["reread_id"], "2026-04", 2).json()
    client.post("/api/ledger/reverse",
                json={"request_id": run["request_id"], "expected_version": 3})
    # 撤销后再补：按当前净额重新补全差额
    again = _supplement(client, reread["reread_id"], "2026-04", 4).json()
    assert _amounts(again) == {("2026-01", "M0002"): "-0.87",
                               ("2026-02", "M0002"): "0.41"}
    assert client.get("/api/ledger/reconcile").json()["all_balanced"] is True


def test_interrupted_posting_resumes_without_double_charge(ledger_client):
    client = ledger_client
    assert _close(client, "2026-01", 0).status_code == 200
    assert _close(client, "2026-02", 1).status_code == 200
    assert _close(client, "2026-03", 2).status_code == 200
    reread = _reread(client, REREAD_C)
    response = _supplement(client, reread["reread_id"], "2026-04", 3, crash_after=1)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "posting" and run["interrupted"] is True
    assert run["posted_total"] == 1 and run["pending"] == 2
    # 中断态核对：未落账的差额仍是待补
    mid = client.get("/api/ledger/reconcile").json()
    assert mid["all_balanced"] is False
    # 落账中断可续跑
    resumed = client.post("/api/ledger/supplement/resume",
                          json={"request_id": run["request_id"]})
    assert resumed.status_code == 200, resumed.text
    done = resumed.json()
    assert done["status"] == "done"
    assert _amounts(done) == {("2026-01", "M0008"): "-10.50",
                              ("2026-02", "M0008"): "4.98",
                              ("2026-03", "M0008"): "5.52"}
    # 重复续跑不再入账
    again = client.post("/api/ledger/supplement/resume",
                        json={"request_id": run["request_id"]}).json()
    assert again["idempotent_replay"] is True
    entries = client.get("/api/ledger").json()["entries"]
    assert len([e for e in entries if e["kind"] == "supplement"]) == 3
    assert client.get("/api/ledger/reconcile").json()["all_balanced"] is True


def test_same_request_retry_never_double_charges(ledger_client):
    client = ledger_client
    assert _close(client, "2026-01", 0).status_code == 200
    reread = _reread(client, REREAD_A)
    body = {"reread_id": reread["reread_id"], "target_month": "2026-04",
            "expected_version": 1, "request_id": "req-0001"}
    first = client.post("/api/ledger/supplement", json=body).json()
    assert first["status"] == "done"
    # 同一笔原样重试（版本已过期也无妨：幂等键优先）
    second = client.post("/api/ledger/supplement", json=body).json()
    assert second["idempotent_replay"] is True
    assert second["entries"] == first["entries"]
    entries = client.get("/api/ledger").json()["entries"]
    assert len([e for e in entries if e["kind"] == "supplement"]) == 1
    rows = client.get("/api/ledger/reconcile").json()["rows"]
    assert {(r["month"], r["meter_id"]): r["net_supplement"]
            for r in rows}[("2026-01", "M0002")] == "-0.87"


def test_second_active_run_for_same_pair_rejected(ledger_client):
    """同一 (补读, 目标月) 只允许一个未撤销的补账批次，防止换键重复收费。"""
    client = ledger_client
    assert _close(client, "2026-01", 0).status_code == 200
    reread = _reread(client, REREAD_A)
    body = {"reread_id": reread["reread_id"], "target_month": "2026-04",
            "expected_version": 1, "request_id": "req-a"}
    assert client.post("/api/ledger/supplement", json=body).json()["status"] == "done"
    # 换一个幂等键对同一 (补读, 目标月) 再补 → 拒绝，不会双重入账
    duplicate = client.post("/api/ledger/supplement",
                            json={**body, "request_id": "req-b",
                                  "expected_version": 2})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "E_RUN_ACTIVE"
    entries = client.get("/api/ledger").json()["entries"]
    assert len([e for e in entries if e["kind"] == "supplement"]) == 1
    # 撤销之后允许重新补账
    client.post("/api/ledger/reverse",
                json={"request_id": "req-a", "expected_version": 2})
    redo = client.post("/api/ledger/supplement",
                       json={**body, "request_id": "req-b",
                             "expected_version": 3})
    assert redo.status_code == 200
    assert _amounts(redo.json()) == {("2026-01", "M0002"): "-0.87"}


def test_stale_decisions_rejected_on_close_and_supplement(ledger_client):
    client = ledger_client
    reread = _reread(client, REREAD_A)
    assert _version(client) == 0
    assert _close(client, "2026-01", 0).status_code == 200   # v0 → v1
    # 基于 v0 的补账决定已过期
    stale_sup = _supplement(client, reread["reread_id"], "2026-04", 0)
    assert stale_sup.status_code == 409
    assert stale_sup.json()["error"] == "E_LEDGER_STALE"
    # 基于 v0 的关账决定同样过期
    stale_close = _close(client, "2026-02", 0)
    assert stale_close.status_code == 409
    assert stale_close.json()["error"] == "E_LEDGER_STALE"
    # 反方向：先补账（v1 → v2），再用 v1 关账也被拒绝
    ok_sup = _supplement(client, reread["reread_id"], "2026-04", 1)
    assert ok_sup.status_code == 200
    late_close = _close(client, "2026-02", 1)
    assert late_close.status_code == 409
    assert late_close.json()["error"] == "E_LEDGER_STALE"
    # 撤销同样要带最新版本
    run = ok_sup.json()
    stale_rev = client.post("/api/ledger/reverse",
                            json={"request_id": run["request_id"],
                                  "expected_version": 1})
    assert stale_rev.status_code == 409


def test_version_required_for_decisions(ledger_client):
    client = ledger_client
    response = client.post("/api/ledger/close", json={"month": "2026-01"})
    assert response.status_code == 400
    assert response.json()["error"] == "E_LEDGER_VERSION_REQUIRED"
    reread = _reread(client, REREAD_A)
    response = client.post("/api/ledger/supplement",
                           json={"reread_id": reread["reread_id"],
                                 "target_month": "2026-04"})
    assert response.status_code == 400


def test_close_and_target_month_guards(ledger_client):
    client = ledger_client
    unknown = _close(client, "2027-01", 0)
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "E_MONTH_UNKNOWN"
    assert _close(client, "2026-01", 0).status_code == 200
    again = _close(client, "2026-01", 1)
    assert again.status_code == 409
    assert again.json()["error"] == "E_MONTH_CLOSED"
    reread = _reread(client, REREAD_A)
    # 已关账月不能作为补账目标月
    closed_target = _supplement(client, reread["reread_id"], "2026-01", 1)
    assert closed_target.status_code == 409
    assert closed_target.json()["error"] == "E_MONTH_CLOSED"
    # 非计费月不能作为目标月
    bad_target = _supplement(client, reread["reread_id"], "2027-01", 1)
    assert bad_target.status_code == 409
    assert bad_target.json()["error"] == "E_TARGET_MONTH_INVALID"


def test_supplement_requires_closed_month_and_known_reread(ledger_client):
    client = ledger_client
    reread = _reread(client, REREAD_A)
    no_closed = _supplement(client, reread["reread_id"], "2026-04", 0)
    assert no_closed.status_code == 409
    assert no_closed.json()["error"] == "E_MONTH_NOT_CLOSED"
    missing = _supplement(client, "rr-000000000000", "2026-04", 0)
    assert missing.status_code == 404
    assert missing.json()["error"] == "E_REREAD_NOT_FOUND"


def test_reread_invalid_rows_rejected(ledger_client):
    client = ledger_client
    unknown_meter = client.post("/api/ledger/reread", json={"readings": [
        {"meter_id": "M9999", "register": "TOTAL",
         "read_at": "2026-02-01T00:00:00+08:00", "reading": "1.0"}]})
    assert unknown_meter.status_code == 422
    assert unknown_meter.json()["error"] == "E_REREAD_INVALID"
    # 与既有读数同键不同值 → 冲突，整批拒绝
    conflict = client.post("/api/ledger/reread", json={"readings": [
        {"meter_id": "M0002", "register": "TOTAL",
         "read_at": "2026-04-01T00:00:00+08:00", "reading": "1003.5"}]})
    assert conflict.status_code == 422
    assert conflict.json()["error"] == "E_REREAD_INVALID"
    # 插入倒走读数 → 单调性拒绝
    reversed_row = client.post("/api/ledger/reread", json={"readings": [
        {"meter_id": "M0002", "register": "TOTAL",
         "read_at": "2026-02-01T00:00:00+08:00", "reading": "1005.0"}]})
    assert reversed_row.status_code == 422
    assert reversed_row.json()["error"] == "E_REREAD_INVALID"
    # 失败的补读不留批次
    assert client.get("/api/ledger").json()["rereads"] == []


def test_reread_idempotent_replay(ledger_client):
    client = ledger_client
    first = _reread(client, REREAD_A)
    second = _reread(client, REREAD_A)
    assert second["reread_id"] == first["reread_id"]
    assert second["idempotent_replay"] is True
    assert len(client.get("/api/ledger").json()["rereads"]) == 1


def test_reverse_guards(ledger_client):
    client = ledger_client
    missing = client.post("/api/ledger/reverse",
                          json={"request_id": "sr-none", "expected_version": 0})
    assert missing.status_code == 404
    assert missing.json()["error"] == "E_RUN_NOT_FOUND"
    assert _close(client, "2026-01", 0).status_code == 200
    assert _close(client, "2026-02", 1).status_code == 200
    assert _close(client, "2026-03", 2).status_code == 200
    reread = _reread(client, REREAD_C)
    run = _supplement(client, reread["reread_id"], "2026-04", 3,
                      crash_after=1).json()
    # 落账未完成不能撤销，先续跑
    not_done = client.post("/api/ledger/reverse",
                           json={"request_id": run["request_id"],
                                 "expected_version": 3})
    assert not_done.status_code == 409
    assert not_done.json()["error"] == "E_RUN_NOT_DONE"
    client.post("/api/ledger/supplement/resume",
                json={"request_id": run["request_id"]})
    done = client.post("/api/ledger/reverse",
                       json={"request_id": run["request_id"],
                             "expected_version": 4})
    assert done.status_code == 200
    # 重复撤销是幂等重放，不产生第二组反向分录
    replay = client.post("/api/ledger/reverse",
                         json={"request_id": run["request_id"],
                               "expected_version": 5})
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    entries = client.get("/api/ledger").json()["entries"]
    assert len([e for e in entries if e["kind"] == "reversal"]) == 3


def test_close_blocked_while_posting_in_flight(ledger_client):
    client = ledger_client
    assert _close(client, "2026-01", 0).status_code == 200
    assert _close(client, "2026-02", 1).status_code == 200
    reread = _reread(client, REREAD_C)
    run = _supplement(client, reread["reread_id"], "2026-04", 2,
                      crash_after=1).json()
    assert run["status"] == "posting"
    blocked = _close(client, "2026-04", 2)
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "E_POSTING_IN_FLIGHT"
    # 续跑完成后即可关账
    client.post("/api/ledger/supplement/resume",
                json={"request_id": run["request_id"]})
    assert _close(client, "2026-04", 3).status_code == 200


def test_reconcile_reports_pending_drift_before_posting(ledger_client):
    client = ledger_client
    assert _close(client, "2026-01", 0).status_code == 200
    reread = _reread(client, REREAD_A)
    mid = client.get("/api/ledger/reconcile").json()
    assert mid["latest_reread_id"] == reread["reread_id"]
    assert mid["all_balanced"] is False
    rows = {(r["month"], r["meter_id"]): r for r in mid["rows"]}
    assert rows[("2026-01", "M0002")]["drift"] == "-0.87"
    assert rows[("2026-01", "M0008")]["balanced"] is True
    _supplement(client, reread["reread_id"], "2026-04", 1)
    final = client.get("/api/ledger/reconcile").json()
    assert final["all_balanced"] is True
    assert final["drift_total"] == "0.00"
