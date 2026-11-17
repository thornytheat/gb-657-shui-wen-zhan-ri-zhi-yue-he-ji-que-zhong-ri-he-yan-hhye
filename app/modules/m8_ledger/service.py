"""M8 编排：关账、回读、补账落账、续跑、撤销与核对。

账本不变量：
- 已关账月的应收 = 原账快照 + 该月已入账净补账，原账一行不改；
- 关账 / 补账 / 撤销都是「决定」，必须携带 expected_version，过期决定拒绝；
- 落账按分录逐笔入账可中断，续跑从 pending 继续，同笔分录重试不重复入账。
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal

from ... import errors
from ... import store as store_mod
from ..m2_contract import contract as contract_mod
from ..m2_contract import service as m2_service
from ..m3_quality import service as m3_service
from ..m4_intervals import restore as restore_mod
from ..m5_months import allocate as allocate_mod
from ..m6_batches import history, resume as resume_mod
from . import ledger

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
REREAD_CELLS = ("meter_id", "register", "read_at", "reading", "unit", "read_type",
                "note")


def _now() -> str:
    return history.iso(history.now_utc())


def _require_version(expected_version) -> int:
    if expected_version is None:
        raise errors.BadRequest(
            "缺少 expected_version：关账与补账决定必须携带账本版本",
            code="E_LEDGER_VERSION_REQUIRED")
    try:
        return int(expected_version)
    except (TypeError, ValueError) as exc:
        raise errors.BadRequest("expected_version 必须是整数") from exc


def _valid_month(month, label: str = "月份") -> str:
    text = str(month or "")
    if not MONTH_RE.match(text):
        raise errors.BadRequest(f"{label}格式应为 YYYY-MM：{text!r}")
    return text


def _latest_done(store) -> dict:
    batch = store.latest_batch(status="done")
    if batch is None:
        raise errors.NotFound("还没有完成的批次", code="E_BATCH_NOT_FOUND")
    return batch


def _base_batch(store, batch_id: str | None) -> dict:
    if batch_id:
        return resume_mod.require_batch(store, batch_id)
    return _latest_done(store)


def _crash_limit(crash_after) -> int | None:
    if crash_after is None:
        return None
    try:
        value = int(crash_after)
    except (TypeError, ValueError) as exc:
        raise errors.BadRequest("crash_after 必须是非负整数") from exc
    if value < 0:
        raise errors.BadRequest("crash_after 必须是非负整数")
    return value


# ---------- 关账 ----------
def close(settings, store, *, month: str, expected_version, batch_id=None) -> dict:
    version = _require_version(expected_version)
    month = _valid_month(month)
    batch = _base_batch(store, batch_id)
    bills = [item for item in store.load_bills(batch["batch_id"])
             if item["month"] == month]
    if not bills:
        raise errors.NotFound(f"{month} 在批次 {batch['batch_id']} 没有账单，不能关账",
                              code="E_MONTH_UNKNOWN")
    amount = sum((item["amount_yuan"] for item in bills), Decimal("0"))
    snapshot = [{
        "meter_id": item["meter_id"],
        "usage_kwh": str(item["usage_kwh"]),
        "amount_yuan": str(item["amount_yuan"]),
        "components": item["components"],
    } for item in bills]
    try:
        store.close_month(month=month, batch_id=batch["batch_id"], closed_at=_now(),
                          amount_yuan=amount, bills=snapshot, expected_version=version)
    except store_mod.StaleWrite as exc:
        raise errors.Conflict(str(exc), code="E_LEDGER_STALE") from exc
    except store_mod.MonthClosed as exc:
        raise errors.Conflict(str(exc), code="E_MONTH_CLOSED") from exc
    except store_mod.PostingInFlight as exc:
        raise errors.Conflict(str(exc), code="E_POSTING_IN_FLIGHT") from exc
    return {"month": month, "batch_id": batch["batch_id"], "meters": len(bills),
            "amount_yuan": str(amount), "version": store.ledger_version()}


# ---------- 回读（补读批次） ----------
def _cell(raw: dict, name: str) -> str:
    value = raw.get(name)
    return "" if value is None else str(value)


def _parse_reread_rows(readings: list[dict], contracts) -> tuple[list[dict], list[dict]]:
    rows = []
    for index, raw in enumerate(readings, start=1):
        if not isinstance(raw, dict):
            raise errors.BadRequest(f"补读第 {index} 行不是对象")
        rows.append({
            "lineno": 1_000_000 + index,
            "segment": 1,
            "cells": {name: _cell(raw, name) for name in REREAD_CELLS},
            "raw": json.dumps(raw, ensure_ascii=False),
        })
    records, quarantine, _ = m2_service.interpret_rows(rows, contracts)
    return records, quarantine


def _canonical(records: list[dict]) -> str:
    keys = sorted((item["meter_id"], item["register"], item["read_at"],
                   str(item["reading"]), item["read_type"]) for item in records)
    return json.dumps(keys, ensure_ascii=False)


def _base_records(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        if row.get("reading_norm") is None:
            continue
        out.append({
            "lineno": row["lineno"], "segment": row["segment"],
            "meter_id": row["meter_id"], "register": row["register"],
            "read_at": row["read_at"], "reading": Decimal(str(row["reading_norm"])),
            "reading_text": row["reading_text"], "read_type": row["read_type"],
            "unit_norm": row.get("unit_norm"), "flags": list(row.get("flags") or []),
            "note": row.get("note", ""),
        })
    return out


def reread(settings, store, *, readings: list[dict], batch_id=None,
           note: str = "") -> dict:
    """回读：补读行并入基准批次重算最新应收，原批次数据一行不动。"""
    if not readings:
        raise errors.BadRequest("补读行为空")
    batch = _base_batch(store, batch_id)
    if batch["status"] != "done":
        raise errors.BadRequest("基准批次未完成，不能作为补读基准",
                                code="E_BATCH_NOT_DONE")
    contracts = contract_mod.load(settings)
    prices = contract_mod.load_prices(settings)
    records, invalid = _parse_reread_rows(readings, contracts)
    if invalid:
        raise errors.UnprocessableInput(
            "补读行未通过契约校验", code="E_REREAD_INVALID",
            detail={"rows": invalid})
    digest = hashlib.sha256(
        (batch["batch_id"] + "\n" + _canonical(records)).encode("utf-8")).hexdigest()
    reread_id = "rr-" + digest[:12]
    existing = store.get_reread(reread_id)
    if existing is not None:
        return _reread_payload(store, existing, idempotent=True)
    merged = _base_records(store.load_readings(batch["batch_id"])) + records
    quality = m3_service.evaluate(merged, contracts)
    if quality["quarantine"]:
        raise errors.UnprocessableInput(
            "补读行与既有读数冲突或造成倒走", code="E_REREAD_INVALID",
            detail={"rows": quality["quarantine"]})
    intervals = restore_mod.build_intervals(quality["valid"], quality["estimate"],
                                            contracts)
    allocated = allocate_mod.allocate(intervals, contracts, prices)
    row = {"reread_id": reread_id, "base_batch_id": batch["batch_id"],
           "created_at": _now(), "note": note or "",
           "readings_json": json.dumps(readings, ensure_ascii=False), "digest": digest}
    store.save_reread(reread=row, bills=allocated["bills"])
    return _reread_payload(store, store.get_reread(reread_id), idempotent=False)


def _reread_payload(store, reread: dict, *, idempotent: bool) -> dict:
    bills = store.load_reread_bills(reread["reread_id"])
    latest = ledger.bill_map(bills)
    posted = ledger.net_posted(store.list_accounted_entries())
    diffs = []
    for closure in store.list_closures():
        month = closure["month"]
        closed_bills = ledger.closure_bill_map(closure)
        meters = (set(closed_bills)
                  | {meter for (mo, meter) in latest if mo == month}
                  | {meter for (mo, meter) in posted if mo == month})
        for meter_id in sorted(meters):
            closed = closed_bills.get(meter_id) or ledger.zero_bill()
            done = posted.get((month, meter_id),
                              {"amount": ledger.ZERO_MONEY})
            new = latest.get((month, meter_id)) or ledger.zero_bill()
            diffs.append({
                "month": month, "meter_id": meter_id,
                "closed_amount": str(closed["amount_yuan"]),
                "latest_amount": str(new["amount_yuan"]),
                "posted_net": str(done["amount"]),
                "unposted_diff": str(Decimal(str(new["amount_yuan"]))
                                     - Decimal(str(closed["amount_yuan"]))
                                     - done["amount"]),
            })
    return {
        "reread_id": reread["reread_id"],
        "base_batch_id": reread["base_batch_id"],
        "seq": reread["seq"],
        "idempotent_replay": idempotent,
        "readings_accepted": len(json.loads(reread["readings_json"])),
        "latest_bills": [{
            "month": bill["month"], "meter_id": bill["meter_id"],
            "usage_kwh": str(bill["usage_kwh"]),
            "amount_yuan": str(bill["amount_yuan"]),
        } for bill in bills],
        "closed_month_diffs": diffs,
        "version": store.ledger_version(),
    }


# ---------- 补账（落账，可中断续跑） ----------
def supplement(settings, store, *, reread_id: str, target_month: str,
               expected_version, request_id=None, crash_after=None) -> dict:
    version = _require_version(expected_version)
    limit = _crash_limit(crash_after)
    target_month = _valid_month(target_month, "目标月")
    reread = store.get_reread(reread_id)
    if reread is None:
        raise errors.NotFound(f"补读批次 {reread_id} 不存在", code="E_REREAD_NOT_FOUND")
    request_id = request_id or f"sr-{reread_id}-{target_month}-v{version}"
    existing = store.get_run(request_id)
    if existing is not None:
        # 同一决定的重复提交：幂等键优先于版本，重试绝不重复入账
        if (existing["reread_id"], existing["target_month"]) != (reread_id, target_month):
            raise errors.Conflict(
                f"request_id {request_id} 已属于另一笔补账决定",
                code="E_RUN_MISMATCH")
        if existing["status"] == "posting":
            return _post_pending(store, existing, crash_limit=limit)
        return _run_payload(store, existing, idempotent=True)
    closures = store.list_closures()
    if not closures:
        raise errors.Conflict("没有已关账月份，无需补账", code="E_MONTH_NOT_CLOSED")
    if any(item["month"] == target_month for item in closures):
        raise errors.Conflict(f"目标月 {target_month} 已关账，差额只能记入开放月",
                              code="E_MONTH_CLOSED")
    base_months = {bill["month"]
                   for bill in store.load_bills(reread["base_batch_id"])}
    if target_month not in base_months:
        raise errors.Conflict(f"目标月 {target_month} 不是本账本的计费月",
                              code="E_TARGET_MONTH_INVALID")
    entries = ledger.plan_entries(
        request_id=request_id, reread_id=reread_id, target_month=target_month,
        closures=closures, latest_bills=store.load_reread_bills(reread_id),
        accounted=store.list_accounted_entries())
    run = {"request_id": request_id, "reread_id": reread_id,
           "target_month": target_month, "status": "posting",
           "created_at": _now(), "finished_at": None, "base_version": version}
    try:
        store.create_run(run=run, entries=entries, expected_version=version)
    except store_mod.StaleWrite as exc:
        raise errors.Conflict(str(exc), code="E_LEDGER_STALE") from exc
    except store_mod.MonthClosed as exc:
        raise errors.Conflict(str(exc), code="E_MONTH_CLOSED") from exc
    except store_mod.RunStateConflict as exc:
        raise errors.Conflict(str(exc), code="E_RUN_ACTIVE") from exc
    return _post_pending(store, store.get_run(request_id), crash_limit=limit)


def _post_pending(store, run: dict, *, crash_limit: int | None) -> dict:
    """逐笔入账；crash_limit 是测试钩子，模拟落账中断。"""
    request_id = run["request_id"]
    posted_now = 0
    interrupted = False
    for entry in store.pending_entries(request_id):
        if crash_limit is not None and posted_now >= crash_limit:
            interrupted = True
            break
        store.post_entry(entry["entry_id"], _now())
        posted_now += 1
    if not interrupted:
        store.finish_run(request_id, _now())
    return _run_payload(store, store.get_run(request_id),
                        posted_now=posted_now, interrupted=interrupted)


def resume(settings, store, *, request_id: str, crash_after=None) -> dict:
    limit = _crash_limit(crash_after)
    run = store.get_run(request_id)
    if run is None:
        raise errors.NotFound(f"补账批次 {request_id} 不存在", code="E_RUN_NOT_FOUND")
    if run["status"] != "posting":
        return _run_payload(store, run, idempotent=True)
    if store.get_closure(run["target_month"]):
        raise errors.Conflict(f"目标月 {run['target_month']} 已关账，落账无法续跑",
                              code="E_MONTH_CLOSED")
    return _post_pending(store, run, crash_limit=limit)


# ---------- 撤销补账（反向分录） ----------
def reverse(settings, store, *, request_id: str, expected_version) -> dict:
    version = _require_version(expected_version)
    run = store.get_run(request_id)
    if run is None:
        raise errors.NotFound(f"补账批次 {request_id} 不存在", code="E_RUN_NOT_FOUND")
    if run["status"] == "reversed":
        return _reverse_payload(store, run, idempotent=True)
    if run["status"] != "done":
        raise errors.Conflict("落账未完成，先续跑再撤销", code="E_RUN_NOT_DONE")
    now = _now()
    reversals = []
    for entry in store.list_entries(request_id=request_id):
        if entry["kind"] != "supplement" or entry["status"] != "posted":
            continue
        reversals.append({
            "entry_id": entry["entry_id"] + ":rev",
            "request_id": request_id,
            "reread_id": entry["reread_id"],
            "target_month": entry["target_month"],
            "source_month": entry["source_month"],
            "meter_id": entry["meter_id"],
            "usage_delta": -Decimal(str(entry["usage_delta"])),
            "amount_delta": -Decimal(str(entry["amount_delta"])),
            "components": [{
                "register": comp["register"],
                "usage": str(-Decimal(str(comp["usage"]))),
                "price": str(comp["price"]),
                "amount": str(-Decimal(str(comp["amount"]))),
            } for comp in entry["components"]],
            "kind": "reversal",
            "reversal_of": entry["entry_id"],
            "status": "posted",
            "posted_at": now,
            "seq": entry["seq"],
        })
    try:
        store.reverse_run(request_id=request_id, reversal_entries=reversals,
                          finished_at=now, expected_version=version)
    except store_mod.StaleWrite as exc:
        raise errors.Conflict(str(exc), code="E_LEDGER_STALE") from exc
    except store_mod.RunStateConflict as exc:
        raise errors.Conflict(str(exc), code="E_RUN_NOT_DONE") from exc
    return _reverse_payload(store, store.get_run(request_id), idempotent=False)


def _reverse_payload(store, run: dict, *, idempotent: bool) -> dict:
    entries = store.list_entries(request_id=run["request_id"])
    return {
        "request_id": run["request_id"],
        "status": run["status"],
        "idempotent_replay": idempotent,
        "reversals": [_entry_payload(item) for item in entries
                      if item["kind"] == "reversal"],
        "version": store.ledger_version(),
    }


# ---------- 核对与总览 ----------
def reconcile(settings, store) -> dict:
    latest = store.latest_reread()
    latest_bills = store.load_reread_bills(latest["reread_id"]) if latest else []
    rows = ledger.reconcile_rows(closures=store.list_closures(),
                                 latest_bills=latest_bills,
                                 accounted=store.list_accounted_entries())
    drift_total = sum((row["drift"] for row in rows), Decimal("0"))
    return {
        "latest_reread_id": latest["reread_id"] if latest else None,
        "rows": [{
            "month": row["month"], "meter_id": row["meter_id"],
            "original": str(row["original"]),
            "net_supplement": str(row["net_supplement"]),
            "latest": str(row["latest"]),
            "drift": str(row["drift"]),
            "balanced": row["balanced"],
        } for row in rows],
        "all_balanced": all(row["balanced"] for row in rows),
        "drift_total": str(drift_total),
    }


def overview(settings, store) -> dict:
    latest = store.latest_reread()
    ref_batch_id = (latest["base_batch_id"] if latest
                    else (store.latest_batch() or {}).get("batch_id"))
    closures = store.list_closures()
    open_months: list[str] = []
    if ref_batch_id:
        closed = {item["month"] for item in closures}
        open_months = sorted({bill["month"]
                              for bill in store.load_bills(ref_batch_id)} - closed)
    return {
        "version": store.ledger_version(),
        "closed_months": [{
            "month": item["month"], "batch_id": item["batch_id"],
            "amount_yuan": str(item["amount_yuan"]), "closed_at": item["closed_at"],
            "meters": len(item["bills"]),
        } for item in closures],
        "open_months": open_months,
        "rereads": [{
            "reread_id": item["reread_id"], "base_batch_id": item["base_batch_id"],
            "seq": item["seq"], "created_at": item["created_at"], "note": item["note"],
        } for item in store.list_rereads()],
        "runs": store.list_runs(),
        "entries": [_entry_payload(item) for item in store.list_entries()],
    }


def _entry_payload(entry: dict) -> dict:
    amount = Decimal(str(entry["amount_delta"]))
    return {
        "entry_id": entry["entry_id"],
        "request_id": entry["request_id"],
        "reread_id": entry["reread_id"],
        "kind": entry["kind"],
        "reversal_of": entry.get("reversal_of"),
        "source_month": entry["source_month"],
        "target_month": entry["target_month"],
        "meter_id": entry["meter_id"],
        "usage_delta": str(entry["usage_delta"]),
        "amount_delta": str(entry["amount_delta"]),
        "direction": "charge" if amount > 0 else ("refund" if amount < 0 else "zero"),
        "components": [{
            "register": comp["register"], "usage": str(comp["usage"]),
            "price": str(comp["price"]), "amount": str(comp["amount"]),
        } for comp in entry["components"]],
        "status": entry["status"],
        "posted_at": entry.get("posted_at"),
    }


def _run_payload(store, run: dict, *, idempotent: bool = False,
                 posted_now: int = 0, interrupted: bool = False) -> dict:
    entries = store.list_entries(request_id=run["request_id"])
    posted = [item for item in entries if item["status"] == "posted"]
    pending = [item for item in entries if item["status"] == "pending"]
    return {
        "request_id": run["request_id"],
        "reread_id": run["reread_id"],
        "target_month": run["target_month"],
        "status": run["status"],
        "interrupted": interrupted,
        "idempotent_replay": idempotent,
        "posted_now": posted_now,
        "posted_total": len(posted),
        "pending": len(pending),
        "entries": [_entry_payload(item) for item in entries],
        "version": store.ledger_version(),
    }
