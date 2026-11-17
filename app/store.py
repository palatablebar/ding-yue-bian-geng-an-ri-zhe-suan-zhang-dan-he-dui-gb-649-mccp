"""SQLite 存储：批次、输入摘要、订阅、变更、隔离、分段、抵扣行、账单、对账、清单。

约定：
- 金额列一律 TEXT（Decimal 字符串）加一份「分」整数，不落 float；
- 一个批次的所有派生行在同一个事务里「先删后插」，失败整体回滚，不留半批；
- 页面只读本模块查询结果，不在模板里做业务计算。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'billing',
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input_digest TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    counters_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_key ON runs(batch_key, status);
CREATE INDEX IF NOT EXISTS idx_runs_kind ON runs(kind, status, created_at);

CREATE TABLE IF NOT EXISTS inputs (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    encoding TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    rows INTEGER NOT NULL,
    header_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS subscriptions (
    run_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    price_text TEXT NOT NULL DEFAULT '',
    monthly TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    tz TEXT NOT NULL,
    state TEXT NOT NULL,
    period_days INTEGER NOT NULL DEFAULT 0,
    leap INTEGER NOT NULL DEFAULT 0,
    source_file TEXT NOT NULL DEFAULT '',
    row_no INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS changes (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    change_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    effective_utc TEXT NOT NULL,
    effective_local TEXT NOT NULL,
    local_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    new_plan TEXT NOT NULL DEFAULT '',
    new_price_text TEXT NOT NULL DEFAULT '',
    new_price TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    row_no INTEGER NOT NULL DEFAULT 0,
    applied INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT NOT NULL DEFAULT '',
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS quarantine (
    run_id TEXT NOT NULL,
    file TEXT NOT NULL,
    row_no INTEGER NOT NULL,
    code TEXT NOT NULL,
    level TEXT NOT NULL,
    field TEXT NOT NULL DEFAULT '',
    raw_value TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_quarantine_run ON quarantine(run_id, code);

CREATE TABLE IF NOT EXISTS segments (
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    days INTEGER NOT NULL,
    plan TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active',
    monthly TEXT NOT NULL DEFAULT '',
    amount_exact TEXT NOT NULL DEFAULT '0',
    amount_cents INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS components (
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    days INTEGER NOT NULL DEFAULT 0,
    monthly TEXT NOT NULL DEFAULT '',
    exact_text TEXT NOT NULL DEFAULT '0',
    cents INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS invoices (
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_days INTEGER NOT NULL,
    billed_days INTEGER NOT NULL,
    free_days INTEGER NOT NULL,
    charge_cents INTEGER NOT NULL,
    credit_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    exact_total TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL DEFAULT 'ok',
    flags_json TEXT NOT NULL DEFAULT '[]',
    source_row INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stages (
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    in_count INTEGER NOT NULL DEFAULT 0,
    out_count INTEGER NOT NULL DEFAULT 0,
    quarantine_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'done',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS totals (
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliations (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ledger TEXT NOT NULL,
    label TEXT NOT NULL,
    left_text TEXT NOT NULL,
    right_text TEXT NOT NULL,
    diff_text TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS manifests (
    run_id TEXT PRIMARY KEY,
    json TEXT NOT NULL
);

-- ---- 年度合同终止结算 ----
-- 合同定义：承诺总额、起止；停机日与终止指令号来自同一条合同行
CREATE TABLE IF NOT EXISTS contracts (
    run_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    contract_start TEXT NOT NULL,
    contract_end TEXT NOT NULL,
    commitment_exact TEXT NOT NULL DEFAULT '0',
    commitment_cents INTEGER NOT NULL DEFAULT 0,
    stopped_on TEXT NOT NULL DEFAULT '',
    termination_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    row_no INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

-- 已出账单：账期与金额的原值，终止时逐张冲抵
CREATE TABLE IF NOT EXISTS issued_invoices (
    run_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    days INTEGER NOT NULL DEFAULT 0,
    amount_exact TEXT NOT NULL DEFAULT '0',
    amount_cents INTEGER NOT NULL DEFAULT 0,
    usable INTEGER NOT NULL DEFAULT 1,
    source_file TEXT NOT NULL DEFAULT '',
    row_no INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

-- 终止指令 → 结算单：一条指令只生成一张，重跑按 (contract_id, termination_key) 复用
CREATE TABLE IF NOT EXISTS settlements (
    run_id TEXT NOT NULL,
    settlement_id TEXT NOT NULL,
    termination_key TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    contract_start TEXT NOT NULL,
    contract_end TEXT NOT NULL,
    stopped_on TEXT NOT NULL,
    period_days INTEGER NOT NULL,
    served_days INTEGER NOT NULL,
    unserved_days INTEGER NOT NULL,
    commitment_cents INTEGER NOT NULL,
    issued_cents INTEGER NOT NULL,
    issued_count INTEGER NOT NULL,
    remainder_cents INTEGER NOT NULL,
    remainder_exact TEXT NOT NULL DEFAULT '0',
    payable_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'settled',
    flags_json TEXT NOT NULL DEFAULT '[]'
);

-- 结算单成分：已出月份逐张冲抵 + 未履行承诺一次结清
CREATE TABLE IF NOT EXISTS settlement_lines (
    run_id TEXT NOT NULL,
    settlement_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL DEFAULT '',
    days INTEGER NOT NULL DEFAULT 0,
    exact_text TEXT NOT NULL DEFAULT '0',
    cents INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

-- 发布态：每个合同只能有一张生效的终止结算单（一次终止只结算一次）
CREATE TABLE IF NOT EXISTS settlement_versions (
    version_id TEXT PRIMARY KEY NOT NULL,
    contract_id TEXT NOT NULL,
    termination_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    superseded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_versions_active
    ON settlement_versions(status, contract_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_single_active
    ON settlement_versions(contract_id)
    WHERE status = 'active';
"""


def _loads(value: str, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self._migrate(conn)
            conn.executescript(SCHEMA)

    @staticmethod
    def _migrate(conn) -> None:
        """把旧库补到当前列集：只增列、不删不改，已有数据原样保留。

        必须在 `executescript(SCHEMA)` 之前跑：SCHEMA 里对新列建了索引，
        旧表缺列时先建索引会直接抛 `no such column`。
        """
        additions = {
            "runs": {"kind": "TEXT NOT NULL DEFAULT 'billing'"},
            "issued_invoices": {"usable": "INTEGER NOT NULL DEFAULT 1"},
        }
        for table, columns in additions.items():
            existing = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if existing is None:
                continue
            present = {row["name"] for row in
                       conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in columns.items():
                if name not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

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
    def create_run(self, run: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO runs (run_id, batch_key, status, source, rule_version,
                        kind, created_at, finished_at, input_digest, params_json,
                        counters_json, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     input_digest=excluded.input_digest,
                     counters_json=excluded.counters_json""",
                (run["run_id"], run["batch_key"], run["status"], run["source"],
                 run["rule_version"], run.get("kind", "billing"), run["created_at"],
                 run.get("finished_at"), run.get("input_digest", ""),
                 json.dumps(run.get("params", {}), ensure_ascii=False),
                 json.dumps(run.get("counters", {}), ensure_ascii=False),
                 run.get("note", "")),
            )

    def update_run(self, run_id: str, **fields) -> None:
        allowed = {"status", "finished_at", "input_digest", "counters_json", "note"}
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "counters_json" and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        values.append(run_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", values)

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def find_run_by_key(self, batch_key: str, status: str = "done",
                        kind: str = "billing") -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE batch_key=? AND status=? AND kind=? "
                "ORDER BY created_at LIMIT 1",
                (batch_key, status, kind)).fetchone()
        return self._run_row(row) if row else None

    def latest_run(self, status: str | None = None,
                   kind: str = "billing") -> dict | None:
        with self.connect() as conn:
            if status:
                row = conn.execute(
                    "SELECT * FROM runs WHERE status=? AND kind=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (status, kind)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM runs WHERE kind=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1", (kind,)
                ).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, limit: int = 30, kind: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE kind=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (kind, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (limit,)).fetchall()
        return [self._run_row(row) for row in rows]

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["params"] = _loads(data.pop("params_json", "{}"), {})
        data["counters"] = _loads(data.pop("counters_json", "{}"), {})
        return data

    # ---------- 输入摘要 ----------
    def put_inputs(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM inputs WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO inputs (run_id, name, encoding, bytes, sha256, rows,
                        header_json, note) VALUES (?,?,?,?,?,?,?,?)""",
                [(run_id, item["name"], item["encoding"], int(item["bytes"]),
                  item["sha256"], int(item["rows"]),
                  json.dumps(item.get("header", []), ensure_ascii=False),
                  item.get("note", "")) for item in items],
            )

    def list_inputs(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM inputs WHERE run_id=? ORDER BY name", (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["header"] = _loads(data.pop("header_json", "[]"), [])
            out.append(data)
        return out

    # ---------- 订阅 ----------
    def put_subscriptions(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM subscriptions WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO subscriptions (run_id, sub_id, account, plan, price_text,
                        monthly, period_start, period_end, tz, state, period_days, leap,
                        source_file, row_no, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["sub_id"], item["account"], item["plan"],
                  item["price_text"], str(item["monthly"]), item["period_start"],
                  item["period_end"], item["tz"], item["state"], int(item["period_days"]),
                  int(item["leap"]), item.get("source_file", ""), int(item.get("row_no", 0)),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_subscriptions(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE run_id=? ORDER BY sub_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    # ---------- 变更 ----------
    def put_changes(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM changes WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO changes (run_id, seq, change_id, sub_id, effective_utc,
                        effective_local, local_date, kind, new_plan, new_price_text,
                        new_price, source_file, row_no, applied, superseded_by, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, int(item["seq"]), item["change_id"], item["sub_id"],
                  item["effective_utc"], item["effective_local"], item["local_date"],
                  item["kind"], item.get("new_plan", ""), item.get("new_price_text", ""),
                  str(item.get("new_price", "")), item.get("source_file", ""),
                  int(item.get("row_no", 0)), int(item.get("applied", 1)),
                  item.get("superseded_by", ""),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_changes(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM changes WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            data["applied"] = bool(data["applied"])
            out.append(data)
        return out

    # ---------- 隔离 ----------
    def put_quarantine(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM quarantine WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO quarantine (run_id, file, row_no, code, level, field,
                        raw_value, detail) VALUES (?,?,?,?,?,?,?,?)""",
                [(run_id, item["file"], int(item["row_no"]), item["code"], item["level"],
                  item.get("field", ""), item.get("raw_value", ""),
                  item.get("detail", "")) for item in items],
            )

    def list_quarantine(self, run_id: str, limit: int | None = None) -> list[dict]:
        sql = ("SELECT * FROM quarantine WHERE run_id=? ORDER BY file, row_no, code")
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.connect() as conn:
            rows = conn.execute(sql, (run_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---------- 分段 ----------
    def put_segments(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM segments WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO segments (run_id, invoice_id, sub_id, seq, start_date,
                        end_date, days, plan, state, monthly, amount_exact, amount_cents,
                        flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["invoice_id"], item["sub_id"], int(item["seq"]),
                  item["start_date"], item["end_date"], int(item["days"]), item["plan"],
                  item["state"], str(item.get("monthly", "")),
                  str(item.get("amount_exact", "0")), int(item.get("amount_cents", 0)),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_segments(self, run_id: str, invoice_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if invoice_id:
                rows = conn.execute(
                    "SELECT * FROM segments WHERE run_id=? AND invoice_id=? "
                    "ORDER BY invoice_id, seq", (run_id, invoice_id)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM segments WHERE run_id=? ORDER BY invoice_id, seq",
                    (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    def update_segment_amounts(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.executemany(
                "UPDATE segments SET amount_exact=?, amount_cents=? "
                "WHERE run_id=? AND invoice_id=? AND seq=?",
                [(str(item["amount_exact"]), int(item["amount_cents"]), run_id,
                  item["invoice_id"], int(item["seq"])) for item in items],
            )

    # ---------- 抵扣行 ----------
    def put_components(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM components WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO components (run_id, invoice_id, sub_id, seq, kind, source,
                        label, days, monthly, exact_text, cents, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["invoice_id"], item["sub_id"], int(item["seq"]),
                  item["kind"], item.get("source", ""), item.get("label", ""),
                  int(item.get("days", 0)), str(item.get("monthly", "")),
                  str(item["exact"]), int(item["cents"]),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_components(self, run_id: str, invoice_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if invoice_id:
                rows = conn.execute(
                    "SELECT * FROM components WHERE run_id=? AND invoice_id=? "
                    "ORDER BY invoice_id, seq", (run_id, invoice_id)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM components WHERE run_id=? ORDER BY invoice_id, seq",
                    (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    # ---------- 账单 ----------
    def put_invoices(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM invoices WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO invoices (run_id, invoice_id, sub_id, account, period_start,
                        period_end, period_days, billed_days, free_days, charge_cents,
                        credit_cents, total_cents, exact_total, status, flags_json, source_row)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["invoice_id"], item["sub_id"], item.get("account", ""),
                  item["period_start"], item["period_end"], int(item["period_days"]),
                  int(item["billed_days"]), int(item["free_days"]),
                  int(item["charge_cents"]), int(item["credit_cents"]),
                  int(item["total_cents"]), str(item.get("exact_total", "0")),
                  item.get("status", "ok"),
                  json.dumps(item.get("flags", []), ensure_ascii=False),
                  int(item.get("source_row", 0))) for item in items],
            )

    def list_invoices(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invoices WHERE run_id=? ORDER BY invoice_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    def get_invoice(self, run_id: str, invoice_id: str) -> dict | None:
        for invoice in self.list_invoices(run_id):
            if invoice["invoice_id"] == invoice_id:
                return invoice
        return None

    # ---------- 阶段 / 合计 / 对账 ----------
    def put_stages(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM stages WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO stages (run_id, stage, in_count, out_count,
                        quarantine_count, status, note) VALUES (?,?,?,?,?,?,?)""",
                [(run_id, item["stage"], int(item["in_count"]), int(item["out_count"]),
                  int(item["quarantine_count"]), item.get("status", "done"),
                  item.get("note", "")) for item in items],
            )

    def list_stages(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM stages WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def put_totals(self, run_id: str, totals: dict) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM totals WHERE run_id=?", (run_id,))
            conn.executemany(
                "INSERT INTO totals (run_id, key, value) VALUES (?,?,?)",
                [(run_id, key, str(value)) for key, value in sorted(totals.items())],
            )

    def list_totals(self, run_id: str) -> dict:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM totals WHERE run_id=? ORDER BY key", (run_id,)
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def put_reconciliations(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM reconciliations WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO reconciliations (run_id, seq, ledger, label, left_text,
                        right_text, diff_text, status, note) VALUES (?,?,?,?,?,?,?,?,?)""",
                [(run_id, int(item["seq"]), item["ledger"], item["label"],
                  item["left_text"], item["right_text"], item["diff_text"],
                  item["status"], item.get("note", "")) for item in items],
            )

    def list_reconciliations(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reconciliations WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 清单 ----------
    def put_manifest(self, run_id: str, data: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO manifests (run_id, json) VALUES (?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET json=excluded.json",
                (run_id, json.dumps(data, ensure_ascii=False)),
            )

    def get_manifest(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT json FROM manifests WHERE run_id=?", (run_id,)).fetchone()
        return _loads(row["json"], {}) if row else None

    # ---------- 整体重算 ----------
    def clear_derived(self, run_id: str) -> None:
        """重算前清派生表；订阅、变更、隔离、输入摘要保留。"""
        with self.connect() as conn:
            for table in ("segments", "components", "invoices", "stages", "totals",
                          "reconciliations"):
                conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))

    # ---------- 年度合同终止结算 ----------
    def put_contracts(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM contracts WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO contracts (run_id, contract_id, sub_id, account,
                        contract_start, contract_end, commitment_exact, commitment_cents,
                        stopped_on, termination_id, state, source_file, row_no, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["contract_id"], item["sub_id"], item.get("account", ""),
                  item["contract_start"], item["contract_end"],
                  str(item.get("commitment_exact", "0")),
                  int(item.get("commitment_cents", 0)), item.get("stopped_on", ""),
                  item.get("termination_id", ""), item.get("state", ""),
                  item.get("source_file", ""), int(item.get("row_no", 0)),
                  json.dumps(item.get("flags", []), ensure_ascii=False))
                 for item in items],
            )

    def list_contracts(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM contracts WHERE run_id=? ORDER BY contract_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    def put_issued(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM issued_invoices WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO issued_invoices (run_id, contract_id, invoice_id,
                        period_start, period_end, days, amount_exact, amount_cents,
                        usable, source_file, row_no, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["contract_id"], item["invoice_id"],
                  item["period_start"], item["period_end"], int(item.get("days", 0)),
                  str(item.get("amount_exact", "0")), int(item.get("amount_cents", 0)),
                  int(item.get("usable", 1)),
                  item.get("source_file", ""), int(item.get("row_no", 0)),
                  json.dumps(item.get("flags", []), ensure_ascii=False))
                 for item in items],
            )

    def list_issued(self, run_id: str, contract_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if contract_id:
                rows = conn.execute(
                    "SELECT * FROM issued_invoices WHERE run_id=? AND contract_id=? "
                    "ORDER BY period_start, invoice_id", (run_id, contract_id)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM issued_invoices WHERE run_id=? "
                    "ORDER BY contract_id, period_start, invoice_id", (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            data["usable"] = bool(data.get("usable", 1))
            out.append(data)
        return out

    def put_settlements(self, run_id: str, items: list[dict],
                        lines: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM settlements WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM settlement_lines WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO settlements (run_id, settlement_id, termination_key,
                        contract_id, sub_id, account, contract_start, contract_end,
                        stopped_on, period_days, served_days, unserved_days,
                        commitment_cents, issued_cents, issued_count, remainder_cents,
                        remainder_exact, payable_cents, status, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["settlement_id"], item["termination_key"],
                  item["contract_id"], item["sub_id"], item.get("account", ""),
                  item["contract_start"], item["contract_end"], item["stopped_on"],
                  int(item["period_days"]), int(item["served_days"]),
                  int(item["unserved_days"]), int(item["commitment_cents"]),
                  int(item["issued_cents"]), int(item["issued_count"]),
                  int(item["remainder_cents"]), str(item.get("remainder_exact", "0")),
                  int(item["payable_cents"]), item.get("status", "settled"),
                  json.dumps(item.get("flags", []), ensure_ascii=False))
                 for item in items],
            )
            conn.executemany(
                """INSERT INTO settlement_lines (run_id, settlement_id, seq, kind,
                        source, label, period_start, period_end, days, exact_text,
                        cents, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["settlement_id"], int(item["seq"]), item["kind"],
                  item.get("source", ""), item.get("label", ""),
                  item.get("period_start", ""), item.get("period_end", ""),
                  int(item.get("days", 0)), str(item.get("exact_text", "0")),
                  int(item["cents"]),
                  json.dumps(item.get("flags", []), ensure_ascii=False))
                 for item in lines],
            )

    def list_settlements(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM settlements WHERE run_id=? "
                "ORDER BY stopped_on, contract_id", (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    def list_settlement_lines(self, run_id: str,
                              settlement_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if settlement_id:
                rows = conn.execute(
                    "SELECT * FROM settlement_lines WHERE run_id=? AND settlement_id=? "
                    "ORDER BY seq", (run_id, settlement_id)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM settlement_lines WHERE run_id=? "
                    "ORDER BY settlement_id, seq", (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    def get_settlement(self, run_id: str, settlement_id: str) -> dict | None:
        for row in self.list_settlements(run_id):
            if row["settlement_id"] == settlement_id:
                return row
        return None

    # ---------- 结算发布态（原子切换） ----------
    def active_versions(self) -> dict[tuple[str, str], dict]:
        """当前发布的结算版本，按 (合同号, 终止指令键) 索引；只读 active。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM settlement_versions WHERE status='active' "
                "ORDER BY contract_id, termination_key").fetchall()
        return {(row["contract_id"], row["termination_key"]):
                self._version_row(row) for row in rows}

    def versions_for_contract(self, contract_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM settlement_versions WHERE contract_id=? "
                "ORDER BY created_at DESC, rowid DESC", (contract_id,)).fetchall()
        return [self._version_row(row) for row in rows]

    @staticmethod
    def _version_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["payload"] = _loads(data.pop("payload_json", "{}"), {})
        return data

    def get_version(self, version_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM settlement_versions WHERE version_id=?",
                (version_id,)).fetchone()
        return self._version_row(row) if row else None

    def publish_settlement_versions(self, versions: list[dict],
                                    created_at: str,
                                    snapshot: bool = False) -> dict:
        """原子发布：同一事务内完成「旧版作废 → 新版写入 → 快照外撤下」。

        - 一个合同只允许一张生效结算单，旧指令不论叫什么都会先整体作废；
        - `snapshot=True` 表示本次输入是完整快照：不在本次结果里的 active 版本
          在同一事务里被撤下（合同行被删除、终止指令被撤销都靠这条路径收口）；
        - 任一步失败（含唯一索引冲突）整体回滚，active 集保持发布前的完整旧版，
          不允许出现「一半新、一半旧」的发布态。
        """
        published, reused, withdrawn = [], [], []
        with self.connect() as conn:
            keep: set[tuple[str, str]] = set()
            for version in versions:
                payload = json.dumps(version["payload"], ensure_ascii=False,
                                     sort_keys=True)
                current = conn.execute(
                    "SELECT * FROM settlement_versions WHERE status='active' "
                    "AND contract_id=?", (version["contract_id"],)).fetchone()
                if (current is not None
                        and current["termination_key"] == version["termination_key"]
                        and current["payload_json"] == payload):
                    reused.append(self._version_row(current))
                    keep.add((current["contract_id"], current["termination_key"]))
                    continue
                conn.execute(
                    "UPDATE settlement_versions SET status='superseded', "
                    "superseded_at=? WHERE status='active' AND contract_id=?",
                    (created_at, version["contract_id"]))
                previous = conn.execute(
                    "SELECT * FROM settlement_versions WHERE version_id=?",
                    (version["version_id"],)).fetchone()
                if previous is not None:
                    conn.execute(
                        "UPDATE settlement_versions SET status='active', "
                        "superseded_at=NULL, run_id=?, payload_json=? "
                        "WHERE version_id=?",
                        (version["run_id"], payload, version["version_id"]))
                else:
                    conn.execute(
                        """INSERT INTO settlement_versions (version_id, contract_id,
                                termination_key, run_id, payload_json, status, created_at)
                           VALUES (?,?,?,?,?,'active',?)""",
                        (version["version_id"], version["contract_id"],
                         version["termination_key"], version["run_id"], payload,
                         created_at))
                published.append({"version_id": version["version_id"],
                                  "contract_id": version["contract_id"],
                                  "termination_key": version["termination_key"]})
                keep.add((version["contract_id"], version["termination_key"]))
            if snapshot:
                rows = conn.execute(
                    "SELECT version_id, contract_id, termination_key "
                    "FROM settlement_versions WHERE status='active'").fetchall()
                for row in rows:
                    if (row["contract_id"], row["termination_key"]) in keep:
                        continue
                    conn.execute(
                        "UPDATE settlement_versions SET status='superseded', "
                        "superseded_at=? WHERE version_id=?",
                        (created_at, row["version_id"]))
                    withdrawn.append(row["version_id"])
        return {"published": published, "reused": len(reused),
                "withdrawn": withdrawn}

    def rollback_publish(self, version_ids: list[str],
                         created_at: str) -> None:
        """发布后人工回退：把指定 active 版本作废，保留记录以便复核。

        正常发布是单事务，失败自动回滚，不需要调用。
        """
        with self.connect() as conn:
            for version_id in version_ids:
                conn.execute(
                    "UPDATE settlement_versions SET status='superseded', "
                    "superseded_at=? WHERE version_id=? AND status='active'",
                    (created_at, version_id))

    def version_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM settlement_versions").fetchone()
        return int(row["n"])
