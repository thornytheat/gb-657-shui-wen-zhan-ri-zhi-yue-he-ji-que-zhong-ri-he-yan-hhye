"""SQLite 存储：批次、表计契约、读数、区间、月度分摊、账单、隔离、阶段、合计、清单。

约定：
- 用量与金额一律 TEXT（Decimal 字符串），不落 float；
- 一次运行的所有行在同一个事务里「先删后插」，失败整体回滚，不留半批数据；
- 页面永远只展示一个批次，按 batch_id 过滤。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sample TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input_digest TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_key ON batches(batch_key, status);
CREATE TABLE IF NOT EXISTS meters (
    batch_id TEXT NOT NULL, meter_id TEXT NOT NULL, name TEXT NOT NULL,
    capacity TEXT NOT NULL, multiplier TEXT NOT NULL, unit TEXT NOT NULL,
    registers TEXT NOT NULL, replaced_at TEXT, replaced_by TEXT, tariff TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    batch_id TEXT NOT NULL, lineno INTEGER NOT NULL, segment INTEGER NOT NULL,
    meter_id TEXT NOT NULL, register TEXT NOT NULL, read_at TEXT NOT NULL,
    reading_text TEXT NOT NULL, unit_raw TEXT NOT NULL, reading_norm TEXT,
    unit_norm TEXT, read_type TEXT NOT NULL, status TEXT NOT NULL,
    code TEXT, flags_json TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS intervals (
    batch_id TEXT NOT NULL, interval_id TEXT NOT NULL, meter_id TEXT NOT NULL,
    register TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
    prev_reading TEXT NOT NULL, cur_reading TEXT NOT NULL, delta TEXT NOT NULL,
    multiplier TEXT NOT NULL, unit TEXT NOT NULL, usage_kwh TEXT NOT NULL,
    days TEXT NOT NULL, method TEXT NOT NULL, flags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS allocations (
    batch_id TEXT NOT NULL, month TEXT NOT NULL, meter_id TEXT NOT NULL,
    register TEXT NOT NULL, usage_kwh TEXT NOT NULL, exact_share TEXT NOT NULL,
    tou_adjusted INTEGER NOT NULL DEFAULT 0, sources_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS bills (
    batch_id TEXT NOT NULL, month TEXT NOT NULL, meter_id TEXT NOT NULL,
    usage_kwh TEXT NOT NULL, amount_yuan TEXT NOT NULL,
    components_json TEXT NOT NULL, adjust_cents INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quarantine (
    batch_id TEXT NOT NULL, lineno INTEGER NOT NULL, segment INTEGER NOT NULL,
    code TEXT NOT NULL, stage TEXT NOT NULL, level TEXT NOT NULL,
    field TEXT, raw_value TEXT, meter_id TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS stages (
    batch_id TEXT NOT NULL, stage TEXT NOT NULL, in_count INTEGER NOT NULL,
    out_count INTEGER NOT NULL, quarantine_count INTEGER NOT NULL,
    status TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS totals (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
    batch_id TEXT PRIMARY KEY, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS month_closures (
    month TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    amount_yuan TEXT NOT NULL,
    bills_json TEXT NOT NULL,
    base_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rereads (
    reread_id TEXT PRIMARY KEY,
    base_batch_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    readings_json TEXT NOT NULL,
    digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reread_bills (
    reread_id TEXT NOT NULL, month TEXT NOT NULL, meter_id TEXT NOT NULL,
    usage_kwh TEXT NOT NULL, amount_yuan TEXT NOT NULL,
    components_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supplement_runs (
    request_id TEXT PRIMARY KEY,
    reread_id TEXT NOT NULL,
    target_month TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    base_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    reread_id TEXT NOT NULL,
    target_month TEXT NOT NULL,
    source_month TEXT NOT NULL,
    meter_id TEXT NOT NULL,
    usage_delta TEXT NOT NULL,
    amount_delta TEXT NOT NULL,
    components_json TEXT NOT NULL,
    kind TEXT NOT NULL,
    reversal_of TEXT,
    status TEXT NOT NULL,
    posted_at TEXT,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_source ON ledger_entries(source_month, meter_id);
CREATE INDEX IF NOT EXISTS idx_entries_request ON ledger_entries(request_id, status);
"""


class StaleWrite(Exception):
    """事务内版本校验失败：决定基于的账本状态已过期。"""


class MonthClosed(Exception):
    """目标月已关账。"""


class PostingInFlight(Exception):
    """该月存在进行中的落账，不能关账。"""


class RunStateConflict(Exception):
    """补账批次状态不允许该操作。"""


def _dec(text):
    return None if text is None else Decimal(str(text))


def _components_json(components: list[dict]) -> str:
    """分量落库一律字符串：用量/金额不落 float，也不落 Decimal 对象。"""
    normalized = [{key: (str(value) if isinstance(value, Decimal) else value)
                   for key, value in comp.items()} for comp in components]
    return json.dumps(normalized, ensure_ascii=False)


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in
                       conn.execute("PRAGMA table_info(batches)").fetchall()}
            if "updated_at" not in columns:
                conn.execute("ALTER TABLE batches ADD COLUMN updated_at TEXT")
            conn.execute(
                "INSERT OR IGNORE INTO ledger_state (id, version) VALUES (1, 0)")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 批次 ----------
    def get_batch(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return dict(row) if row else None

    def find_done_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? AND status='done' "
                "ORDER BY created_at LIMIT 1", (batch_key,)).fetchone()
        return dict(row) if row else None

    def find_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? ORDER BY created_at DESC LIMIT 1",
                (batch_key,)).fetchone()
        return dict(row) if row else None

    def latest_batch(self, status: str = "done") -> dict | None:
        with self.connect() as conn:
            order = "COALESCE(updated_at, created_at) DESC, rowid DESC"
            row = conn.execute(
                f"SELECT * FROM batches WHERE status=? ORDER BY {order} LIMIT 1",
                (status,)).fetchone()
            if row is None:
                row = conn.execute(
                    f"SELECT * FROM batches ORDER BY {order} LIMIT 1").fetchone()
        return dict(row) if row else None

    def list_batches(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY COALESCE(updated_at, created_at) DESC, "
                "rowid DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def save_attempt(self, *, batch: dict, stages: list[dict], note: str = "") -> None:
        """失败尝试：只写批次与阶段，不动已有批次的数据。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,sample,status,created_at,
                        finished_at,input_digest,rule_version,params_json,note,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     note=excluded.note, run_id=excluded.run_id,
                     updated_at=excluded.updated_at""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["sample"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch["rule_version"],
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 note or batch.get("note", ""), batch.get("updated_at")))
            conn.execute("DELETE FROM stages WHERE batch_id=?", (batch["batch_id"],))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,in_count,out_count,
                        quarantine_count,status,elapsed_ms,note) VALUES (?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], s["stage"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"],
                     s.get("note", "")))

    def touch_batch(self, batch_id: str, stamp: str) -> None:
        """命中已有批次时刷新「最近一次运行」，页面才会停在这次看的结果上。"""
        with self.connect() as conn:
            conn.execute("UPDATE batches SET updated_at=? WHERE batch_id=?",
                         (stamp, batch_id))

    # ---------- 一次运行整体落库 ----------
    def save_run(self, *, batch: dict, meters: list[dict], readings: list[dict],
                 intervals: list[dict], allocations: list[dict], bills: list[dict],
                 quarantine: list[dict], stages: list[dict], totals: dict,
                 manifest_json: dict) -> None:
        b = batch["batch_id"]
        with self.connect() as conn:
            for table in ("meters", "readings", "intervals", "allocations", "bills",
                          "quarantine", "stages", "totals"):
                conn.execute(f"DELETE FROM {table} WHERE batch_id=?", (b,))
            for m in meters:
                conn.execute(
                    """INSERT INTO meters (batch_id,meter_id,name,capacity,multiplier,unit,
                        registers,replaced_at,replaced_by,tariff)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (b, m["meter_id"], m["name"], str(m["capacity"]),
                     str(m["multiplier"]), m["unit"], ";".join(m["registers"]),
                     m.get("replaced_at"), m.get("replaced_by"), m["tariff"]))
            for r in readings:
                conn.execute(
                    """INSERT INTO readings (batch_id,lineno,segment,meter_id,register,
                        read_at,reading_text,unit_raw,reading_norm,unit_norm,read_type,
                        status,code,flags_json,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (b, r["lineno"], r["segment"], r["meter_id"], r["register"],
                     r["read_at"], r["reading_text"], r["unit_raw"],
                     None if r.get("reading_norm") is None else str(r["reading_norm"]),
                     r.get("unit_norm"), r["read_type"], r["status"], r.get("code"),
                     json.dumps(r.get("flags", []), ensure_ascii=False),
                     r.get("note", "")))
            for i in intervals:
                conn.execute(
                    """INSERT INTO intervals (batch_id,interval_id,meter_id,register,
                        start_at,end_at,prev_reading,cur_reading,delta,multiplier,unit,
                        usage_kwh,days,method,flags_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (b, i["interval_id"], i["meter_id"], i["register"], i["start_at"],
                     i["end_at"], str(i["prev_reading"]), str(i["cur_reading"]),
                     str(i["delta"]), str(i["multiplier"]), i["unit"],
                     str(i["usage_kwh"]), str(i["days"]), i["method"],
                     json.dumps(i.get("flags", []), ensure_ascii=False)))
            for a in allocations:
                conn.execute(
                    """INSERT INTO allocations (batch_id,month,meter_id,register,usage_kwh,
                        exact_share,tou_adjusted,sources_json)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (b, a["month"], a["meter_id"], a["register"], str(a["usage_kwh"]),
                     str(a["exact_share"]), 1 if a.get("tou_adjusted") else 0,
                     json.dumps(a.get("sources", []), ensure_ascii=False)))
            for bill in bills:
                conn.execute(
                    """INSERT INTO bills (batch_id,month,meter_id,usage_kwh,amount_yuan,
                        components_json,adjust_cents) VALUES (?,?,?,?,?,?,?)""",
                    (b, bill["month"], bill["meter_id"], str(bill["usage_kwh"]),
                     str(bill["amount_yuan"]),
                     json.dumps(bill["components"], ensure_ascii=False),
                     int(bill.get("adjust_cents", 0))))
            for q in quarantine:
                conn.execute(
                    """INSERT INTO quarantine (batch_id,lineno,segment,code,stage,level,
                        field,raw_value,meter_id,detail) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (b, q["lineno"], q.get("segment", 1), q["code"], q["stage"],
                     q["level"], q.get("field"), q.get("raw_value"),
                     q.get("meter_id"), q.get("detail")))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,in_count,out_count,
                        quarantine_count,status,elapsed_ms,note) VALUES (?,?,?,?,?,?,?,?)""",
                    (b, s["stage"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"],
                     s.get("note", "")))
            for key, value in totals.items():
                conn.execute("INSERT INTO totals (batch_id,key,value) VALUES (?,?,?)",
                             (b, key, str(value)))
            conn.execute(
                "INSERT INTO manifests (batch_id,json) VALUES (?,?) "
                "ON CONFLICT(batch_id) DO UPDATE SET json=excluded.json",
                (b, json.dumps(manifest_json, ensure_ascii=False)))
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,sample,status,created_at,
                        finished_at,input_digest,rule_version,params_json,note,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     run_id=excluded.run_id, note=excluded.note,
                     updated_at=excluded.updated_at""",
                (b, batch["batch_key"], batch["run_id"], batch["sample"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch["rule_version"],
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 batch.get("note", ""), batch.get("updated_at")))

    # ---------- 读取 ----------
    def load_meters(self, batch_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM meters WHERE batch_id=? ORDER BY rowid", (batch_id,))
        for r in rows:
            r["capacity"] = _dec(r["capacity"])
            r["multiplier"] = _dec(r["multiplier"])
            r["registers"] = r["registers"].split(";")
        return rows

    def load_readings(self, batch_id: str, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM readings WHERE batch_id=?"
        args: list = [batch_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY lineno"
        rows = self._all(sql, tuple(args))
        for r in rows:
            r["flags"] = json.loads(r.pop("flags_json") or "[]")
            r["reading_norm"] = _dec(r["reading_norm"])
        return rows

    def load_intervals(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM intervals WHERE batch_id=? ORDER BY meter_id, register, start_at",
            (batch_id,))
        for r in rows:
            r["flags"] = json.loads(r.pop("flags_json") or "[]")
            for key in ("prev_reading", "cur_reading", "delta", "multiplier", "usage_kwh",
                        "days"):
                r[key] = _dec(r[key])
        return rows

    def load_allocations(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM allocations WHERE batch_id=? ORDER BY month, meter_id, register",
            (batch_id,))
        for r in rows:
            r["usage_kwh"] = _dec(r["usage_kwh"])
            r["exact_share"] = _dec(r["exact_share"])
            r["sources"] = json.loads(r.pop("sources_json") or "[]")
            r["tou_adjusted"] = bool(r["tou_adjusted"])
        return rows

    def load_bills(self, batch_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM bills WHERE batch_id=? ORDER BY month, meter_id",
                         (batch_id,))
        for r in rows:
            r["usage_kwh"] = _dec(r["usage_kwh"])
            r["amount_yuan"] = _dec(r["amount_yuan"])
            r["components"] = json.loads(r.pop("components_json") or "[]")
        return rows

    def load_quarantine(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM quarantine WHERE batch_id=? ORDER BY lineno", (batch_id,))

    def load_stages(self, batch_id: str) -> list[dict]:
        from . import config
        rows = self._all("SELECT * FROM stages WHERE batch_id=? ORDER BY rowid", (batch_id,))
        for row in rows:
            row["label"] = config.STAGE_LABELS.get(row["stage"], row["stage"])
        return rows

    def load_totals(self, batch_id: str) -> dict:
        rows = self._all("SELECT key, value FROM totals WHERE batch_id=?", (batch_id,))
        return {r["key"]: r["value"] for r in rows}

    def load_manifest(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT json FROM manifests WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    def count_rows(self, batch_id: str) -> dict:
        tables = ("meters", "readings", "intervals", "allocations", "bills", "quarantine")
        with self.connect() as conn:
            return {t: conn.execute(
                f"SELECT COUNT(*) AS n FROM {t} WHERE batch_id=?", (batch_id,)
            ).fetchone()["n"] for t in tables}

    def _all(self, sql: str, args: tuple) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # ---------- 账本：关账 / 回读 / 补账 ----------
    @staticmethod
    def _version(conn) -> int:
        row = conn.execute("SELECT version FROM ledger_state WHERE id=1").fetchone()
        return int(row["version"]) if row else 0

    @staticmethod
    def _check_version(conn, expected_version: int) -> None:
        current = Store._version(conn)
        if current != expected_version:
            raise StaleWrite(
                f"账本版本已变化：决定基于 v{expected_version}，当前 v{current}")

    def ledger_version(self) -> int:
        with self.connect() as conn:
            return self._version(conn)

    # ----- 关账 -----
    def get_closure(self, month: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM month_closures WHERE month=?",
                               (month,)).fetchone()
        return self._closure(row) if row else None

    def list_closures(self) -> list[dict]:
        rows = self._all("SELECT * FROM month_closures ORDER BY month", ())
        return [self._closure(row) for row in rows]

    @staticmethod
    def _closure(row: dict) -> dict:
        out = dict(row)
        out["bills"] = json.loads(out.pop("bills_json"))
        out["amount_yuan"] = _dec(out["amount_yuan"])
        return out

    def close_month(self, *, month: str, batch_id: str, closed_at: str,
                    amount_yuan, bills: list[dict], expected_version: int) -> None:
        """关账：快照原账并推进账本版本，版本校验与写在同一事务。"""
        with self.connect() as conn:
            self._check_version(conn, expected_version)
            if conn.execute("SELECT 1 FROM month_closures WHERE month=?",
                            (month,)).fetchone():
                raise MonthClosed(f"月份 {month} 已关账")
            if conn.execute(
                    "SELECT 1 FROM supplement_runs WHERE target_month=? "
                    "AND status='posting'", (month,)).fetchone():
                raise PostingInFlight(f"月份 {month} 有进行中的落账，先续跑完成")
            conn.execute(
                """INSERT INTO month_closures (month,batch_id,closed_at,amount_yuan,
                        bills_json,base_version) VALUES (?,?,?,?,?,?)""",
                (month, batch_id, closed_at, str(amount_yuan),
                 json.dumps(bills, ensure_ascii=False), expected_version))
            conn.execute("UPDATE ledger_state SET version=version+1 WHERE id=1")

    # ----- 回读（补读批次） -----
    def save_reread(self, *, reread: dict, bills: list[dict]) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM rereads"
                               ).fetchone()
            seq = int(row["seq"])
            conn.execute(
                """INSERT INTO rereads (reread_id,base_batch_id,seq,created_at,note,
                        readings_json,digest) VALUES (?,?,?,?,?,?,?)""",
                (reread["reread_id"], reread["base_batch_id"], seq,
                 reread["created_at"], reread.get("note", ""),
                 reread["readings_json"], reread["digest"]))
            for bill in bills:
                conn.execute(
                    """INSERT INTO reread_bills (reread_id,month,meter_id,usage_kwh,
                            amount_yuan,components_json) VALUES (?,?,?,?,?,?)""",
                    (reread["reread_id"], bill["month"], bill["meter_id"],
                     str(bill["usage_kwh"]), str(bill["amount_yuan"]),
                     json.dumps(bill["components"], ensure_ascii=False)))

    def get_reread(self, reread_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM rereads WHERE reread_id=?",
                               (reread_id,)).fetchone()
        return dict(row) if row else None

    def latest_reread(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rereads ORDER BY seq DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def list_rereads(self) -> list[dict]:
        return self._all("SELECT * FROM rereads ORDER BY seq", ())

    def load_reread_bills(self, reread_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM reread_bills WHERE reread_id=? ORDER BY month, meter_id",
            (reread_id,))
        for r in rows:
            r["usage_kwh"] = _dec(r["usage_kwh"])
            r["amount_yuan"] = _dec(r["amount_yuan"])
            r["components"] = json.loads(r.pop("components_json") or "[]")
        return rows

    # ----- 补账批次与分录 -----
    def get_run(self, request_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM supplement_runs WHERE request_id=?",
                               (request_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50) -> list[dict]:
        return self._all(
            "SELECT * FROM supplement_runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,))

    def create_run(self, *, run: dict, entries: list[dict],
                   expected_version: int) -> None:
        """落账计划入库：版本校验 + 目标月开放校验与写在同一事务。"""
        with self.connect() as conn:
            self._check_version(conn, expected_version)
            if conn.execute("SELECT 1 FROM month_closures WHERE month=?",
                            (run["target_month"],)).fetchone():
                raise MonthClosed(f"目标月 {run['target_month']} 已关账")
            if conn.execute(
                    "SELECT 1 FROM supplement_runs WHERE reread_id=? "
                    "AND target_month=? AND status IN ('posting','done')",
                    (run["reread_id"], run["target_month"])).fetchone():
                raise RunStateConflict(
                    f"补读 {run['reread_id']} 到 {run['target_month']} "
                    "已有补账批次，如需调整请先撤销")
            conn.execute(
                """INSERT INTO supplement_runs (request_id,reread_id,target_month,
                        status,created_at,finished_at,base_version)
                   VALUES (?,?,?,?,?,?,?)""",
                (run["request_id"], run["reread_id"], run["target_month"],
                 run["status"], run["created_at"], run.get("finished_at"),
                 run["base_version"]))
            for e in entries:
                conn.execute(
                    """INSERT INTO ledger_entries (entry_id,request_id,reread_id,
                            target_month,source_month,meter_id,usage_delta,
                            amount_delta,components_json,kind,reversal_of,status,
                            posted_at,seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (e["entry_id"], e["request_id"], e["reread_id"], e["target_month"],
                     e["source_month"], e["meter_id"], str(e["usage_delta"]),
                     str(e["amount_delta"]),
                     _components_json(e["components"]), e["kind"],
                     e.get("reversal_of"), e["status"], e.get("posted_at"), e["seq"]))

    def pending_entries(self, request_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM ledger_entries WHERE request_id=? AND status='pending' "
            "ORDER BY seq", (request_id,))
        return [self._entry(row) for row in rows]

    def post_entry(self, entry_id: str, posted_at: str) -> bool:
        """单笔入账：只从 pending 翻转，重试同一笔不会重复入账。"""
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE ledger_entries SET status='posted', posted_at=? "
                "WHERE entry_id=? AND status='pending'", (posted_at, entry_id))
            return cursor.rowcount > 0

    def finish_run(self, request_id: str, finished_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE supplement_runs SET status='done', finished_at=? "
                "WHERE request_id=?", (finished_at, request_id))
            conn.execute("UPDATE ledger_state SET version=version+1 WHERE id=1")

    def reverse_run(self, *, request_id: str, reversal_entries: list[dict],
                    finished_at: str, expected_version: int) -> None:
        """撤销补账：反向分录入账，原分录标记 reversed，旧账一行不删。"""
        with self.connect() as conn:
            self._check_version(conn, expected_version)
            row = conn.execute(
                "SELECT status FROM supplement_runs WHERE request_id=?",
                (request_id,)).fetchone()
            if row is None or row["status"] != "done":
                raise RunStateConflict(f"补账批次 {request_id} 不在可撤销状态")
            for e in reversal_entries:
                conn.execute(
                    """INSERT INTO ledger_entries (entry_id,request_id,reread_id,
                            target_month,source_month,meter_id,usage_delta,
                            amount_delta,components_json,kind,reversal_of,status,
                            posted_at,seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (e["entry_id"], e["request_id"], e["reread_id"], e["target_month"],
                     e["source_month"], e["meter_id"], str(e["usage_delta"]),
                     str(e["amount_delta"]),
                     _components_json(e["components"]), e["kind"],
                     e.get("reversal_of"), e["status"], e.get("posted_at"), e["seq"]))
            conn.execute(
                "UPDATE ledger_entries SET status='reversed' WHERE request_id=? "
                "AND kind='supplement' AND status='posted'", (request_id,))
            conn.execute(
                "UPDATE supplement_runs SET status='reversed', finished_at=? "
                "WHERE request_id=?", (finished_at, request_id))
            conn.execute("UPDATE ledger_state SET version=version+1 WHERE id=1")

    def list_entries(self, request_id: str | None = None,
                     limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM ledger_entries"
        args: list = []
        if request_id:
            sql += " WHERE request_id=?"
            args.append(request_id)
        sql += " ORDER BY rowid LIMIT ?"
        args.append(limit)
        return [self._entry(row) for row in self._all(sql, tuple(args))]

    def list_accounted_entries(self) -> list[dict]:
        """计入「已入账净额」的分录：已入账补账（含被撤销的）+ 已入账反向分录。

        被撤销的原分录与它的反向分录同时在账，净额归零；pending 从不计入。
        """
        rows = self._all(
            "SELECT * FROM ledger_entries WHERE "
            "(kind='supplement' AND status IN ('posted','reversed')) OR "
            "(kind='reversal' AND status='posted') ORDER BY rowid", ())
        return [self._entry(row) for row in rows]

    @staticmethod
    def _entry(row: dict) -> dict:
        out = dict(row)
        out["usage_delta"] = _dec(out["usage_delta"])
        out["amount_delta"] = _dec(out["amount_delta"])
        out["components"] = json.loads(out.pop("components_json") or "[]")
        return out
