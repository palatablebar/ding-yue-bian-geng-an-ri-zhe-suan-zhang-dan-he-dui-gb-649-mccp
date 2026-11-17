"""合同与已出账单的采信：列契约、字段归一、行级隔离、证据保留。

采信规则与订阅/变更表一致：编码回退链、重复表头、列数不齐、重复键。
不同点：

- 合同表一行就是「一条合同定义 + 可能的一条终止指令」。合同定义相同但停机日
  不同的多行是**多条终止指令**，不是重复键冲突；由 `settlement.pick_directives`
  确定性挑一条生效，其余留档。
- 空停机日表示未终止，是合法状态；只有「填了终止指令号却没有停机日」才判缺失。
- 已出账单账期重叠即重复收费：重叠行进隔离区且**不参与**结算冲抵，避免多抵一次。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import config, reasons
from ...reasons import level_of
from ..m1_ingest import columns as column_mod
from ..m1_ingest import reader
from ..m3_quality import quarantine, rules
from ..m4_proration import rounding
from . import rules as settle_rules

FATAL_STATES = ("R_EMPTY_FILE", "R_HEADER_ONLY", "R_BLANK_ROWS")


def _entry(file: str, row_no: int, code: str, *, field: str = "", raw: str = "",
           detail: str = "") -> dict:
    return quarantine.entry(file=file, row_no=row_no, code=code, field=field,
                            raw_value=raw, detail=detail)


def _file_entries(table, mapping, required: tuple[str, ...]) -> list[dict]:
    entries: list[dict] = []
    if table.state_code:
        entries.append(_entry(table.name, 0, table.state_code,
                              detail=f"编码 {table.encoding}，字节 {table.size}"))
        return entries
    missing = column_mod.required_missing(mapping, required)
    if missing:
        entries.append(_entry(table.name, 0, "R_HEADER_MISSING",
                              field=",".join(missing), raw=",".join(table.header)))
        return entries
    if mapping.unknown:
        entries.append(_entry(table.name, 0, "R_HEADER_UNKNOWN",
                              field=",".join(mapping.unknown),
                              detail="未登记列已忽略"))
    if table.repeated_headers:
        entries.append(_entry(table.name, 0, "R_HEADER_REPEAT",
                              detail=f"重复表头 {table.repeated_headers} 行已剔除"))
    if table.truncated_rows:
        entries.append(_entry(table.name, table.truncated_row_no, "R_ROW_TRUNCATED",
                              detail="引号未闭合，半行不参与计算"))
    if table.blank_rows and table.rows:
        entries.append(_entry(table.name, 0, "R_BLANK_ROWS",
                              detail=f"空白行 {table.blank_rows} 行"))
    return entries


def _span_days(start: dt.date, end: dt.date) -> int:
    delta = (end - start).days
    return delta if delta > 0 else 0


def definition_signature(record: dict) -> tuple:
    """合同定义：决定承诺与期间；指令不同不影响定义。"""
    return (record["sub_id"], record["account"], record["contract_start"],
            record["contract_end"], str(record["commitment_exact"]))


def directive_signature(record: dict) -> tuple:
    return (record["stopped_on"], record["termination_id"])


def normalize_contracts(table, settings) -> tuple[list[dict], list[dict], dict]:
    mapping = column_mod.contract_header(table.header)
    entries = _file_entries(table, mapping, column_mod.CONTRACT_REQUIRED)
    counts = {"rows": table.data_rows, "kept": 0, "quarantined_rows": 0,
              "dropped_dup": 0, "blank_rows": table.blank_rows,
              "repeated_headers": table.repeated_headers,
              "truncated": table.truncated_rows}
    if table.state_code in FATAL_STATES or not mapping.complete:
        counts["quarantined_rows"] = len(table.rows) + table.truncated_rows
        return [], entries, counts

    header_len = len(table.header)
    records: list[dict] = []
    first_by_contract: dict[str, dict] = {}
    seen_directives: dict[tuple[str, str], dict] = {}
    error_rows: set[int] = set()

    def reject(row_no: int, code: str, field: str = "", raw: str = "",
               detail: str = "") -> None:
        entries.append(_entry(table.name, row_no, code, field=field,
                              raw=raw, detail=detail))
        error_rows.add(row_no)

    for row in table.rows:
        if not column_mod.column_count_ok(row.fields, header_len):
            reject(row.row_no, "R_COLUMN_COUNT", field="列数",
                   raw=f"{len(row.fields)}列/表头{header_len}列")
            continue
        contract_id = mapping.value(row.fields, "contract_id").strip()
        if not contract_id:
            reject(row.row_no, "R_CONTRACT_KEY_MISSING", field="合同号", raw=row.raw)
            continue
        start = rules.normalize_date(mapping.value(row.fields, "contract_start"))
        end = rules.normalize_date(mapping.value(row.fields, "contract_end"))
        if start.code or end.code:
            reject(row.row_no, "R_CONTRACT_DATE", field="合同起止",
                   raw=f"{mapping.value(row.fields, 'contract_start')}~"
                       f"{mapping.value(row.fields, 'contract_end')}")
            continue
        if start.value is None or end.value is None or end.value <= start.value:
            reject(row.row_no, "R_CONTRACT_PERIOD", field="合同起止",
                   raw=f"{start.value}~{end.value}")
            continue
        commitment = rules.normalize_money(mapping.value(row.fields, "commitment"))
        if commitment.code or commitment.value is None:
            reject(row.row_no, "R_CONTRACT_COMMITMENT", field="合同总额",
                   raw=mapping.value(row.fields, "commitment"),
                   detail=commitment.code or "合同总额不可用")
            continue
        if commitment.value < 0:
            reject(row.row_no, "R_CONTRACT_COMMITMENT", field="合同总额",
                   raw=mapping.value(row.fields, "commitment"), detail="合同总额为负")
            continue

        stop_parsed = rules.normalize_date(mapping.value(row.fields, "stopped_on"))
        if stop_parsed.code:
            reject(row.row_no, stop_parsed.code, field="停机日",
                   raw=mapping.value(row.fields, "stopped_on"))
            continue
        stop = stop_parsed.value
        termination_id = mapping.value(row.fields, "termination_id").strip()
        if stop is None and termination_id:
            reject(row.row_no, "R_TERMINATION_NO_STOP", field="停机日",
                   raw=termination_id)
            continue
        flags: list[str] = list(commitment.flags)
        if stop is not None:
            invalid = settle_rules.validate_stop(start.value, end.value, stop)
            if invalid:
                reject(row.row_no, invalid, field="停机日", raw=stop.isoformat())
                continue
            if not termination_id:
                flags.append("R_TERMINATION_ID_DERIVED")

        record = {
            "contract_id": contract_id,
            "sub_id": mapping.value(row.fields, "sub_id").strip(),
            "account": mapping.value(row.fields, "account").strip(),
            "contract_start": start.value.isoformat(),
            "contract_end": end.value.isoformat(),
            "commitment_exact": str(commitment.value),
            "commitment_cents": rounding.to_cents(commitment.value),
            "stopped_on": stop.isoformat() if stop else "",
            "termination_id": termination_id,
            "state": mapping.value(row.fields, "state").strip().lower(),
            "source_file": table.name,
            "row_no": row.row_no,
            "flags": flags,
            "usable": True,
        }

        first = first_by_contract.get(contract_id)
        if first is not None and definition_signature(first) != definition_signature(
                record):
            reject(row.row_no, "R_CONTRACT_DUP_KEY", field="合同号",
                   raw=contract_id,
                   detail=f"与第 {first['row_no']} 行合同定义冲突，保留前者")
            if "R_CONTRACT_DUP_KEY" not in first["flags"]:
                first["flags"].append("R_CONTRACT_DUP_KEY")
            continue
        directive_key = (contract_id, f"{record['stopped_on']}|{termination_id}")
        if directive_key in seen_directives:
            kept = seen_directives[directive_key]
            entries.append(_entry(table.name, row.row_no, "R_CONTRACT_DUP_ROW",
                                  field="合同号", raw=contract_id,
                                  detail=f"与第 {kept['row_no']} 行同一终止指令，丢弃"))
            counts["dropped_dup"] += 1
            continue
        seen_directives[directive_key] = record
        first_by_contract.setdefault(contract_id, record)
        records.append(record)

    counts["kept"] = len(records)
    counts["quarantined_rows"] = len(error_rows)
    return records, entries, counts


def issued_signature(record: dict) -> tuple:
    return (record["invoice_id"], record["period_start"], record["period_end"],
            str(record["amount_exact"]))


def normalize_issued(table, contracts: list[dict],
                     settings) -> tuple[list[dict], list[dict], dict]:
    mapping = column_mod.issued_header(table.header)
    entries = _file_entries(table, mapping, column_mod.ISSUED_REQUIRED)
    counts = {"rows": table.data_rows, "kept": 0, "quarantined_rows": 0,
              "dropped_dup": 0, "blank_rows": table.blank_rows,
              "repeated_headers": table.repeated_headers,
              "truncated": table.truncated_rows, "unusable": 0}
    if table.state_code in FATAL_STATES or not mapping.complete:
        counts["quarantined_rows"] = len(table.rows) + table.truncated_rows
        return [], entries, counts

    header_len = len(table.header)
    definitions: dict[str, dict] = {}
    stop_by_contract: dict[str, str] = {}
    for contract in contracts:
        definitions.setdefault(contract["contract_id"], contract)
        if contract.get("stopped_on"):
            stop_by_contract.setdefault(contract["contract_id"], contract["stopped_on"])
    records: list[dict] = []
    error_rows: set[int] = set()
    seen: dict[str, dict] = {}

    def reject(row_no: int, code: str, field: str = "", raw: str = "",
               detail: str = "") -> None:
        entries.append(_entry(table.name, row_no, code, field=field,
                              raw=raw, detail=detail))
        error_rows.add(row_no)

    for row in table.rows:
        if not column_mod.column_count_ok(row.fields, header_len):
            reject(row.row_no, "R_COLUMN_COUNT", field="列数",
                   raw=f"{len(row.fields)}列/表头{header_len}列")
            continue
        contract_id = mapping.value(row.fields, "contract_id").strip()
        invoice_id = mapping.value(row.fields, "invoice_id").strip()
        if not contract_id or not invoice_id:
            reject(row.row_no, "R_KEY_MISSING", field="合同号/账单号", raw=row.raw)
            continue
        contract = definitions.get(contract_id)
        if contract is None:
            reject(row.row_no, "R_ISSUED_UNKNOWN_CONTRACT", field="合同号",
                   raw=contract_id)
            continue
        start = rules.normalize_date(mapping.value(row.fields, "period_start"))
        end = rules.normalize_date(mapping.value(row.fields, "period_end"))
        if start.code or end.code or start.value is None or end.value is None \
                or end.value <= start.value:
            reject(row.row_no, "R_ISSUED_PERIOD", field="账期",
                   raw=f"{mapping.value(row.fields, 'period_start')}~"
                       f"{mapping.value(row.fields, 'period_end')}")
            continue
        contract_start = dt.date.fromisoformat(contract["contract_start"])
        contract_end = dt.date.fromisoformat(contract["contract_end"])
        if start.value < contract_start or end.value > contract_end:
            reject(row.row_no, "R_ISSUED_PERIOD", field="账期",
                   raw=f"{start.value}~{end.value}",
                   detail=f"合同 {contract_start}~{contract_end}")
            continue
        amount = rules.normalize_money(mapping.value(row.fields, "amount"))
        if amount.code or amount.value is None:
            reject(row.row_no, "R_ISSUED_AMOUNT", field="金额",
                   raw=mapping.value(row.fields, "amount"))
            continue

        flags: list[str] = list(amount.flags)
        stop_text = stop_by_contract.get(contract_id, "")
        if stop_text and settle_rules.crosses_stop(
                start.value, end.value, dt.date.fromisoformat(stop_text)):
            flags.append("R_ISSUED_AFTER_STOP")
        record = {
            "contract_id": contract_id,
            "invoice_id": invoice_id,
            "period_start": start.value.isoformat(),
            "period_end": end.value.isoformat(),
            "days": _span_days(start.value, end.value),
            "amount_exact": str(amount.value),
            "amount_cents": rounding.to_cents(amount.value),
            "source_file": table.name,
            "row_no": row.row_no,
            "flags": flags,
            "usable": True,
        }
        if invoice_id in seen:
            kept = seen[invoice_id]
            if issued_signature(kept) == issued_signature(record):
                entries.append(_entry(table.name, row.row_no, "R_DUP_INVOICE_ROW",
                                      field="账单号", raw=invoice_id,
                                      detail="同一账单号重复且取值相同"))
                counts["dropped_dup"] += 1
            else:
                entries.append(_entry(table.name, row.row_no, "R_DUP_INVOICE",
                                      field="账单号", raw=invoice_id,
                                      detail=f"与第 {kept['row_no']} 行取值冲突"))
                error_rows.add(row.row_no)
            continue
        seen[invoice_id] = record
        records.append(record)

    # 账期重叠：同一合同两张账单覆盖同一天 = 重复收费。重叠行隔离且不参与冲抵。
    by_contract: dict[str, list[dict]] = {}
    for record in records:
        by_contract.setdefault(record["contract_id"], []).append(record)
    for contract_id, rows in by_contract.items():
        parsed = [
            settle_rules.IssuedRow(
                invoice_id=item["invoice_id"],
                period_start=dt.date.fromisoformat(item["period_start"]),
                period_end=dt.date.fromisoformat(item["period_end"]),
                amount_cents=int(item["amount_cents"]),
            )
            for item in rows
        ]
        for left, right in settle_rules.issued_overlaps(parsed):
            bad = rows[right]
            entries.append(_entry(
                table.name, bad["row_no"], "R_ISSUED_OVERLAP", field="账期",
                raw=f"{bad['period_start']}~{bad['period_end']}",
                detail=f"{contract_id} 与 {rows[left]['invoice_id']} 重叠，不参与冲抵"))
            if "R_ISSUED_OVERLAP" not in bad["flags"]:
                bad["flags"].append("R_ISSUED_OVERLAP")
            bad["usable"] = False
            error_rows.add(bad["row_no"])

    counts["quarantined_rows"] = len(error_rows)
    counts["kept"] = sum(1 for item in records if item["usable"])
    counts["unusable"] = sum(1 for item in records if not item["usable"])
    return records, entries, counts


def load_contract_tables(sample_dir: str, contract_name: str,
                         issued_name: str) -> tuple[reader.Table, reader.Table]:
    return (reader.load(sample_dir, contract_name),
            reader.load(sample_dir, issued_name))


def reason_level(code: str) -> str:
    return reasons.level_of(code) if code in reasons.REASONS else level_of(code)


__all__ = ["normalize_contracts", "normalize_issued", "load_contract_tables",
           "reason_level", "Decimal", "config"]
