"""补账纯函数：差额计划、已入账净额、核对行。

约定：
- 差额 = 最新应收 −（原账快照 + 已入账净额），正数补收、负数退补，均按原价；
- 已入账净额 = 已入账补账分录（含被撤销的）+ 已入账反向分录，pending 不计；
- 分量差额 = 最新分量 − 原账分量 − 已入账分量差额，合计与分录额逐分相等。
"""
from __future__ import annotations

from decimal import Decimal

ZERO_MONEY = Decimal("0.00")
ZERO_USAGE = Decimal("0.000")


def zero_bill() -> dict:
    return {"usage_kwh": ZERO_USAGE, "amount_yuan": ZERO_MONEY, "components": []}


def bill_map(bills: list[dict]) -> dict[tuple[str, str], dict]:
    return {(bill["month"], bill["meter_id"]): bill for bill in bills}


def closure_bill_map(closure: dict) -> dict[str, dict]:
    """关账快照里的原账：meter_id → 账单。"""
    return {bill["meter_id"]: bill for bill in closure["bills"]}


def _component_map(components: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for comp in components:
        out[comp["register"]] = {
            "usage": Decimal(str(comp["usage"])),
            "amount": Decimal(str(comp["amount"])),
            "price": str(comp["price"]),
        }
    return out


def net_posted(entries: list[dict]) -> dict[tuple[str, str], dict]:
    """(原月, 表号) → 已入账净额（用量 / 金额 / 分量）。"""
    out: dict[tuple[str, str], dict] = {}
    for entry in entries:
        key = (entry["source_month"], entry["meter_id"])
        bucket = out.setdefault(key, {"usage": ZERO_USAGE, "amount": ZERO_MONEY,
                                      "components": {}})
        bucket["usage"] += Decimal(str(entry["usage_delta"]))
        bucket["amount"] += Decimal(str(entry["amount_delta"]))
        for comp in entry.get("components") or []:
            slot = bucket["components"].setdefault(
                comp["register"], {"usage": ZERO_USAGE, "amount": ZERO_MONEY,
                                   "price": str(comp["price"])})
            slot["usage"] += Decimal(str(comp["usage"]))
            slot["amount"] += Decimal(str(comp["amount"]))
    return out


def _diff_components(latest: dict, closed: dict, posted: dict) -> list[dict]:
    latest_map = _component_map(latest.get("components") or [])
    closed_map = _component_map(closed.get("components") or [])
    posted_map = posted.get("components") or {}
    registers = sorted(set(latest_map) | set(closed_map) | set(posted_map))
    out = []
    for register in registers:
        new = latest_map.get(register, {"usage": ZERO_USAGE, "amount": ZERO_MONEY})
        old = closed_map.get(register, {"usage": ZERO_USAGE, "amount": ZERO_MONEY})
        done = posted_map.get(register, {"usage": ZERO_USAGE, "amount": ZERO_MONEY})
        usage = new["usage"] - old["usage"] - done["usage"]
        amount = new["amount"] - old["amount"] - done["amount"]
        if usage == 0 and amount == 0:
            continue
        price = (latest_map.get(register) or closed_map.get(register)
                 or posted_map.get(register) or {}).get("price", "")
        out.append({"register": register, "usage": usage, "price": price,
                    "amount": amount})
    return out


def plan_entries(*, request_id: str, reread_id: str, target_month: str,
                 closures: list[dict], latest_bills: list[dict],
                 accounted: list[dict]) -> list[dict]:
    """对每个已关账月 × 表算差额，生成待入账分录（金额为 0 不落分录）。"""
    latest = bill_map(latest_bills)
    posted = net_posted(accounted)
    entries: list[dict] = []
    seq = 0
    for closure in sorted(closures, key=lambda item: item["month"]):
        month = closure["month"]
        closed_bills = closure_bill_map(closure)
        meters = (set(closed_bills)
                  | {meter for (mo, meter) in latest if mo == month}
                  | {meter for (mo, meter) in posted if mo == month})
        for meter_id in sorted(meters):
            closed = closed_bills.get(meter_id) or zero_bill()
            closed_usage = Decimal(str(closed["usage_kwh"]))
            closed_amount = Decimal(str(closed["amount_yuan"]))
            done = posted.get((month, meter_id),
                              {"usage": ZERO_USAGE, "amount": ZERO_MONEY,
                               "components": {}})
            new = latest.get((month, meter_id)) or zero_bill()
            amount_delta = Decimal(str(new["amount_yuan"])) - closed_amount - done["amount"]
            usage_delta = Decimal(str(new["usage_kwh"])) - closed_usage - done["usage"]
            if amount_delta == 0:
                continue
            seq += 1
            entries.append({
                "entry_id": f"{request_id}:{month}:{meter_id}",
                "request_id": request_id,
                "reread_id": reread_id,
                "target_month": target_month,
                "source_month": month,
                "meter_id": meter_id,
                "usage_delta": usage_delta,
                "amount_delta": amount_delta,
                "components": _diff_components(new, closed, done),
                "kind": "supplement",
                "reversal_of": None,
                "status": "pending",
                "posted_at": None,
                "seq": seq,
            })
    return entries


def reconcile_rows(*, closures: list[dict], latest_bills: list[dict],
                   accounted: list[dict]) -> list[dict]:
    """核对：原账 + 净补账 = 最新应收，逐 (已关账月, 表号) 给差额。"""
    latest = bill_map(latest_bills)
    posted = net_posted(accounted)
    rows: list[dict] = []
    for closure in sorted(closures, key=lambda item: item["month"]):
        month = closure["month"]
        closed_bills = closure_bill_map(closure)
        meters = (set(closed_bills)
                  | {meter for (mo, meter) in latest if mo == month}
                  | {meter for (mo, meter) in posted if mo == month})
        for meter_id in sorted(meters):
            closed = closed_bills.get(meter_id) or zero_bill()
            original = Decimal(str(closed["amount_yuan"]))
            done = posted.get((month, meter_id),
                              {"usage": ZERO_USAGE, "amount": ZERO_MONEY})
            new = latest.get((month, meter_id)) or zero_bill()
            latest_amount = Decimal(str(new["amount_yuan"]))
            drift = latest_amount - (original + done["amount"])
            rows.append({
                "month": month,
                "meter_id": meter_id,
                "original": original,
                "net_supplement": done["amount"],
                "latest": latest_amount,
                "drift": drift,
                "balanced": drift == 0,
            })
    return rows
