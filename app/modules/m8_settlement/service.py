"""M8 合同终止结算：采信 → 计划 → 核对 → 原子发布。

三条硬约束都在这一层落地：

1. **唯一可复核**：一张结算单只有一个 `payable_cents`；页面、CSV、清单读同一个
   已发布版本，不各自重算。
2. **重跑不增单**：同两份输入命中 `batch_key` 复用批次；同一条终止指令即使输入变了，
   也只替换同一 `(合同号, 终止指令键)` 的 active 版本，结算单号按合同与停机月稳定生成。
3. **发布失败恢复旧版**：核对不通过时一行都不写；写库在单个事务内先作废旧版再写新版本，
   任一失败整体回滚，active 集仍是完整旧版。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from decimal import Decimal

from ... import config, manifest
from ...errors import Conflict
from ..m3_quality import quarantine
from ..m4_proration import rounding
from . import ingest, rules, settlement


def _now() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def _version_id(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "SV-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _inputs(contract_table, issued_table) -> list[dict]:
    return [
        {"name": contract_table.name, "encoding": contract_table.encoding,
         "bytes": contract_table.size, "sha256": contract_table.sha256,
         "rows": contract_table.data_rows, "header": contract_table.header},
        {"name": issued_table.name, "encoding": issued_table.encoding,
         "bytes": issued_table.size, "sha256": issued_table.sha256,
         "rows": issued_table.data_rows, "header": issued_table.header},
    ]


def batch_key(settings, contract_table, issued_table) -> str:
    return manifest.batch_key(settings.rule_version, [
        contract_table.name, contract_table.sha256,
        issued_table.name, issued_table.sha256,
    ])


def _counters(contracts: list[dict], contract_counts: dict, issued_counts: dict,
              duplicate_directives: int) -> dict:
    terminated = [row for row in contracts if row.get("stopped_on")]
    return {
        "rows_read": contract_counts["rows"] + issued_counts["rows"],
        "contract_rows": contract_counts["rows"],
        "issued_rows": issued_counts["rows"],
        "contracts_kept": contract_counts["kept"],
        "issued_kept": issued_counts["kept"],
        "terminated_contracts": len({row["contract_id"] for row in terminated}),
        "duplicate_directives": duplicate_directives,
        "quarantined_rows": contract_counts["quarantined_rows"]
                            + issued_counts["quarantined_rows"],
        "dropped_dup": contract_counts["dropped_dup"] + issued_counts["dropped_dup"],
        "blank_rows": contract_counts["blank_rows"] + issued_counts["blank_rows"],
        "repeated_headers": (contract_counts["repeated_headers"]
                             + issued_counts["repeated_headers"]),
        "truncated_rows": contract_counts["truncated"] + issued_counts["truncated"],
    }


def _plan_all(contracts: list[dict],
              issued_by_contract: dict[str, list[dict]]) -> list[settlement.SettlementPlan]:
    plans: list[settlement.SettlementPlan] = []
    for contract in contracts:
        plan = settlement.plan_for(contract,
                                   issued_by_contract.get(contract["contract_id"], []))
        if plan is not None:
            plans.append(plan)
    plans.sort(key=lambda item: (item.stop, item.contract["contract_id"]))
    return plans


def _detect_duplicate_settlements(plans: list[settlement.SettlementPlan]) -> list[dict]:
    seen: dict[str, str] = {}
    entries: list[dict] = []
    for plan in plans:
        if plan.settlement_id in seen:
            entries.append(quarantine.entry(
                file="settlement", row_no=plan.contract.get("row_no", 0),
                code="R_TERMINATION_DUP_GENERATED", field="结算单号",
                raw_value=plan.settlement_id,
                detail=f"与 {seen[plan.settlement_id]} 重复"))
        else:
            seen[plan.settlement_id] = plan.contract["contract_id"]
    return entries


def build_versions(plans: list[settlement.SettlementPlan],
                   run_id: str) -> list[dict]:
    """内容寻址：同一份结算内容永远得到同一个 version_id，跨批次可复用。

    `run_id` 只落在版本行的列上，不进 payload，避免「只是重跑一次」就把同一张
    结算单换成新内容指纹。
    """
    versions: list[dict] = []
    for plan in plans:
        payload = settlement.settlement_payload(plan)
        versions.append({
            "version_id": _version_id(payload),
            "contract_id": plan.contract["contract_id"],
            "termination_key": plan.termination_key,
            "run_id": run_id,
            "payload": payload,
        })
    return versions


def _assert_verifiable(versions: list[dict]) -> None:
    """发布前核对：任何一张结算单三方数字说不通就整体拒绝发布。"""
    problems: list[str] = []
    for version in versions:
        for problem in settlement.verify_payload(version["payload"]):
            problems.append(f"{version['payload']['settlement_id']}:{problem}")
    if problems:
        raise Conflict("终止结算核对未通过，未发布任何版本",
                       code="E_SETTLEMENT_VERIFY", detail={"problems": problems})


def _ledgers_from_payloads(payloads: list[dict], *,
                           scope_contracts: set[str] | None = None,
                           expected_settlements: int | None = None) -> list[dict]:
    """四本账按当前发布态现算，保证页面、导出、接口读的是同一组数字。

    `scope_contracts` / `expected_settlements` 只由展示层传入：把发布态限制在
    当批覆盖的合同内，再和当批结算单快照对账，能发现「发布态遗留了不属于本批的
    孤儿结算单」这种漂移。发布归档时两者都传 None，按全量对账。
    """
    scope = set(scope_contracts) if scope_contracts is not None else None
    scoped = [row for row in payloads
              if scope is None or row["contract_id"] in scope]
    commitment = sum(int(row["commitment_cents"]) for row in scoped)
    issued = sum(int(row["issued_cents"]) for row in scoped)
    payable = sum(int(row["payable_cents"]) for row in scoped)
    final = sum(int(row["final_receivable_cents"]) for row in scoped)
    keys = {(row["contract_id"], row["termination_key"]) for row in scoped}
    expected = len(scoped) if expected_settlements is None else expected_settlements
    rows = [
        {"seq": 0, "ledger": "settlements", "label": "结算单账",
         "left_text": str(len(scoped)), "right_text": str(expected),
         "diff_text": str(len(scoped) - expected),
         "status": "ok" if len(scoped) == expected else "mismatch",
         "note": "发布态结算单数 = 本批结算单快照数（重跑不新增）"},
        {"seq": 1, "ledger": "commitment", "label": "承诺账",
         "left_text": str(commitment), "right_text": str(issued + payable),
         "diff_text": str(commitment - issued - payable),
         "status": "ok" if commitment == issued + payable else "mismatch",
         "note": f"合同承诺 = 已出账单 + 本期终止结算；最终应收 {final}（分）"},
        {"seq": 2, "ledger": "issued", "label": "已出账单冲抵账",
         "left_text": str(issued),
         "right_text": str(commitment - payable),
         "diff_text": str(issued - (commitment - payable)),
         "status": "ok" if issued == commitment - payable else "mismatch",
         "note": "已出月份逐张冲抵，不重复收费"},
        {"seq": 3, "ledger": "directive", "label": "终止指令账",
         "left_text": str(len(scoped)), "right_text": str(len(keys)),
         "diff_text": str(len(scoped) - len(keys)),
         "status": "ok" if len(scoped) == len(keys) else "mismatch",
         "note": "结算单数 = 唯一终止指令数；重跑不新增"},
    ]
    return rows


def _assert_unique_contracts(plans: list[settlement.SettlementPlan]) -> None:
    """发布前自检：同一合同只能有一张结算单。放在写库之前，失败时库内毫无变化。"""
    counts: dict[str, int] = {}
    for plan in plans:
        counts[plan.contract["contract_id"]] = counts.get(
            plan.contract["contract_id"], 0) + 1
    repeated = sorted(contract_id for contract_id, count in counts.items() if count > 1)
    if repeated:
        raise Conflict("同一合同出现多张待发布结算单，拒绝发布",
                       code="E_SETTLEMENT_PUBLISH",
                       detail={"contracts": repeated})


def _rows_for_store(plans: list[settlement.SettlementPlan]) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    lines: list[dict] = []
    for plan in plans:
        payload = settlement.settlement_payload(plan)
        rows.append({
            "settlement_id": payload["settlement_id"],
            "termination_key": payload["termination_key"],
            "contract_id": payload["contract_id"],
            "sub_id": payload["sub_id"],
            "account": payload["account"],
            "contract_start": payload["contract_start"],
            "contract_end": payload["contract_end"],
            "stopped_on": payload["stopped_on"],
            "period_days": payload["period_days"],
            "served_days": payload["served_days"],
            "unserved_days": payload["unserved_days"],
            "commitment_cents": payload["commitment_cents"],
            "issued_cents": payload["issued_cents"],
            "issued_count": payload["issued_count"],
            "remainder_cents": payload["payable_cents"],
            "remainder_exact": str(Decimal(payload["payable_cents"]) / 100),
            "payable_cents": payload["payable_cents"],
            "status": payload["status"],
            "flags": payload["flags"],
        })
        lines.extend(payload["lines"])
    return rows, lines


def _totals(plans: list[settlement.SettlementPlan]) -> dict:
    return {
        "settlements": len(plans),
        "contracts": len({plan.contract["contract_id"] for plan in plans}),
        "commitment_cents": sum(plan.commitment_cents for plan in plans),
        "issued_cents": sum(plan.issued_cents for plan in plans),
        "payable_cents": sum(plan.payable_cents for plan in plans),
        "final_receivable_cents": sum(plan.final_receivable_cents for plan in plans),
        "served_days": sum(plan.served_days for plan in plans),
        "unserved_days": sum(plan.unserved_days for plan in plans),
        "period_days": sum(plan.period_days for plan in plans),
        "issued_rows": sum(len(plan.issued) for plan in plans),
        "duplicate_directives": 0,
        "money_unit": config.MONEY_UNIT,
        "policy": config.TERMINATION_POLICY,
    }


def latest_settlement_run(store) -> dict | None:
    """最近一批已完成的结算批次；没有则退回最近一批（便于展示失败原因）。"""
    return (store.latest_run(status="done", kind="settlement")
            or store.latest_run(kind="settlement"))


def summary(store, run: dict) -> dict:
    run_id = run["run_id"]
    data = store.get_manifest(run_id) or {}
    return {
        "run_id": run_id,
        "check_digit": data.get("check_digit") or manifest.check_digit(run_id),
        "batch_key": run["batch_key"],
        "status": run["status"],
        "kind": run.get("kind", "settlement"),
        "created_at": run["created_at"],
        "finished_at": run.get("finished_at"),
        "reused": False,
        "counters": run.get("counters", {}),
        "totals": store.list_totals(run_id),
        "ledgers": store.list_reconciliations(run_id),
        "inputs": store.list_inputs(run_id),
        "manifest": data,
    }


def run_rows(store, limit: int = 20) -> list[dict]:
    rows = []
    for run in store.list_runs(limit, kind="settlement"):
        totals = store.list_totals(run["run_id"])
        manifest_data = store.get_manifest(run["run_id"]) or {}
        rows.append({
            "run_id": run["run_id"],
            "check_digit": manifest_data.get("check_digit")
            or manifest.check_digit(run["run_id"]),
            "status": run["status"],
            "created_at": run["created_at"],
            "source": run["source"],
            "input_digest": run["input_digest"],
            "settlements": totals.get("settlements", "0"),
            "commitment_cents": totals.get("commitment_cents", "0"),
            "payable_cents": totals.get("payable_cents", "0"),
            "final_receivable_cents": totals.get("final_receivable_cents", "0"),
            "quarantined_rows": run["counters"].get("quarantined_rows", 0),
        })
    return rows


def import_sample(store, settings, contracts: str | None = None,
                  issued: str | None = None) -> dict:
    contract_name = contracts or settings.default_contracts
    issued_name = issued or settings.default_issued
    contract_table, issued_table = ingest.load_contract_tables(
        settings.sample_dir, contract_name, issued_name)
    key = batch_key(settings, contract_table, issued_table)
    existing = store.find_run_by_key(key, kind="settlement")
    if existing is not None:
        data = summary(store, existing)
        data["reused"] = True
        return data

    started_at = _now()
    clock = time.perf_counter()
    run_id = manifest.new_run_id(prefix="ST")
    store.create_run({
        "run_id": run_id,
        "batch_key": key,
        "status": "running",
        "source": f"{contract_table.name} + {issued_table.name}",
        "rule_version": settings.rule_version,
        "kind": "settlement",
        "created_at": started_at,
        "params": {"contracts": contract_table.name, "issued": issued_table.name,
                   "display_tz": settings.display_tz},
    })
    store.put_inputs(run_id, _inputs(contract_table, issued_table))

    try:
        return _run(store, settings, run_id, key, contract_table, issued_table,
                    started_at, clock)
    except Exception as exc:
        # 失败批次留档可查，但不动任何已发布版本；status=failed 不参与 batch_key 复用
        store.update_run(run_id, status="failed", finished_at=_now(),
                         note=f"{type(exc).__name__}: {exc}")
        raise


def _run(store, settings, run_id: str, key: str, contract_table, issued_table,
         started_at: str, clock: float) -> dict:
    """一次采信的主体：先算完、核对完，再落派生表，最后单事务发布。"""
    contracts_norm, contract_entries, contract_counts = ingest.normalize_contracts(
        contract_table, settings)
    issued_norm, issued_entries, issued_counts = ingest.normalize_issued(
        issued_table, contracts_norm, settings)

    winners, duplicates = settlement.pick_directives(contracts_norm)
    duplicate_entries = [
        quarantine.entry(file=contract_table.name, row_no=row.get("row_no", 0),
                         code="R_TERMINATION_DUP", field="终止指令",
                         raw_value=row.get("termination_id", "")
                         or row.get("stopped_on", ""),
                         detail=f"{row['contract_id']} 由较早的指令生效")
        for row in duplicates
    ]

    issued_by_contract: dict[str, list[dict]] = {}
    for row in issued_norm:
        if row["usable"]:
            issued_by_contract.setdefault(row["contract_id"], []).append(row)
    plans = _plan_all(winners, issued_by_contract)
    duplicate_settlements = _detect_duplicate_settlements(plans)

    entries = sorted(contract_entries + issued_entries + duplicate_entries
                     + duplicate_settlements,
                     key=lambda item: (item["file"], item["row_no"], item["code"]))
    counters = _counters(contracts_norm, contract_counts, issued_counts,
                         len(duplicates))
    counters["quarantine_entries"] = len(entries)
    counters["quarantine_by_level"] = {
        level: sum(1 for item in entries if item["level"] == level)
        for level in ("error", "warn", "info")
    }

    # ---- 先核对，再写任何派生行：失败时这一批判定为零 ----
    versions = build_versions(plans, run_id)
    _assert_unique_contracts(plans)
    _assert_verifiable(versions)

    store.put_contracts(run_id, contracts_norm)
    store.put_issued(run_id, issued_norm)
    store.put_quarantine(run_id, entries)
    store.update_run(run_id, counters_json=counters)
    rows, lines = _rows_for_store(plans)
    store.put_settlements(run_id, rows, lines)

    # ---- 单事务发布：旧版作废与新版写入同生共死 ----
    # 合同文件是完整快照：本次结果之外的 active 版本（合同行被删、指令被撤）
    # 在同一事务里一起撤下。
    published = store.publish_settlement_versions(versions, _now(), snapshot=True)
    active = store.active_versions()
    ledgers = _ledgers_from_payloads([
        version["payload"] for version in versions
        if (version["contract_id"], version["termination_key"]) in active])
    store.put_reconciliations(run_id, ledgers)

    totals = _totals(plans)
    totals["duplicate_directives"] = len(duplicates)
    store.put_totals(run_id, totals)

    digest = manifest.digest_parts([item["sha256"] for item in _inputs(
        contract_table, issued_table)])
    finished_at = _now()
    elapsed_ms = int((time.perf_counter() - clock) * 1000)
    store.update_run(run_id, status="done", finished_at=finished_at,
                     input_digest=digest)
    data = manifest.build(
        run_id=run_id, batch_key=key, settings=settings,
        inputs=_inputs(contract_table, issued_table),
        params={"display_tz": settings.display_tz,
                "contracts": contract_table.name, "issued": issued_table.name,
                "policy": config.TERMINATION_POLICY},
        started_at=started_at, finished_at=finished_at, elapsed_ms=elapsed_ms,
        stages=[{"stage": "settlement", "in_count": counters["rows_read"],
                 "out_count": len(plans),
                 "quarantine_count": counters["quarantined_rows"],
                 "note": f"发布 {len(published['published'])} 版，复用 {published['reused']} 版"}],
        counters=counters, totals=totals, reconciliations=ledgers,
    )
    data["publish"] = published
    data["settlement_version"] = {
        "active": len(active),
        "published": len(published["published"]),
        "reused": published["reused"],
        "withdrawn": len(published.get("withdrawn", [])),
    }
    store.put_manifest(run_id, data)

    run = store.get_run(run_id) or {"run_id": run_id, "batch_key": key,
                                    "status": "done", "kind": "settlement",
                                    "created_at": started_at,
                                    "counters": counters}
    return summary(store, run)


# ---------- 读取：页面与导出共用 ----------

def active_settlements(store) -> list[dict]:
    """当前发布态的全部结算单，按停机日与合同号排序。"""
    versions = store.active_versions()
    rows = [version["payload"] for version in versions.values()]
    rows.sort(key=lambda item: (item["stopped_on"], item["contract_id"]))
    return rows


def contract_views(store, run_id: str) -> list[dict]:
    """合同视图：合同定义 + 已出账单 + 当前发布结算（没有则为未终止）。

    同一合同的多条终止指令只显示生效（胜出）的那条，其余作为「留档指令」列出，
    与采信时的确定性选择保持一致。
    """
    active = {(payload["contract_id"], payload["termination_key"]): payload
              for payload in active_settlements(store)}
    issued_by_contract: dict[str, list[dict]] = {}
    for row in store.list_issued(run_id):
        issued_by_contract.setdefault(row["contract_id"], []).append(row)
    contracts = store.list_contracts(run_id)
    winners, duplicates = settlement.pick_directives(contracts)
    winner_ids = {(row["contract_id"], row.get("row_no")) for row in winners}
    superseded: dict[str, list[dict]] = {}
    for row in duplicates:
        superseded.setdefault(row["contract_id"], []).append(row)
    views = []
    for contract in contracts:
        if contract.get("stopped_on") and (
                contract["contract_id"], contract.get("row_no")) not in winner_ids:
            continue
        stop = contract.get("stopped_on") or ""
        payload = None
        if stop:
            key = rules.termination_key(contract["contract_id"],
                                        contract.get("termination_id", ""),
                                        dt.date.fromisoformat(stop))
            payload = active.get((contract["contract_id"], key))
        views.append({
            **contract,
            "issued": stamped_issued(
                issued_by_contract.get(contract["contract_id"], [])),
            # 只汇总参与冲抵的账单：被隔离的重叠账单不进这个数，与结算单一致
            "issued_cents": sum(
                int(row["amount_cents"])
                for row in issued_by_contract.get(contract["contract_id"], [])
                if row.get("usable")),
            "settlement": payload,
            "superseded_directives": superseded.get(contract["contract_id"], []),
            "final_receivable_cents": (
                payload["final_receivable_cents"] if payload
                else int(contract["commitment_cents"])),
        })
    return views


def stamped_issued(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        out.append({
            **row,
            "amount": rounding.cents_text(row["amount_cents"]),
            "range_label": f"[{row['period_start']}, {row['period_end']})",
            "credited": bool(row.get("usable", True)),
        })
    return out


def overview(store, run_id: str, *, scoped: bool = True) -> dict:
    """页面 / 接口的展示口径：active 集与当批快照一起核。

    `scoped=True`（默认）只统计当批合同覆盖范围内的 active 结算单，并和当批快照
    的结算单行数对账，防止旧批次遗留的孤儿结算单混进当前数字。
    """
    payloads = active_settlements(store)
    batch_rows = store.list_reconciliations(run_id) if run_id else []
    if scoped and run_id:
        contracts = store.list_contracts(run_id)
        scope = {row["contract_id"] for row in contracts}
        batch_snapshot = store.list_settlements(run_id)
        ledgers = _ledgers_from_payloads(payloads, scope_contracts=scope,
                                         expected_settlements=len(batch_snapshot))
    else:
        ledgers = _ledgers_from_payloads(payloads)
    return {
        "contracts": contract_views(store, run_id),
        "settlements": payloads,
        "ledgers": ledgers,
        "batch_ledgers": batch_rows,
        "totals": store.list_totals(run_id),
    }


def settlement_rows(store) -> list[dict]:
    rows = active_settlements(store)
    out = []
    for row in rows:
        out.append({
            **row,
            "commitment": rounding.cents_text(row["commitment_cents"]),
            "issued": rounding.cents_text(row["issued_cents"]),
            "payable": rounding.cents_text(row["payable_cents"]),
            "final_receivable": rounding.cents_text(
                row["final_receivable_cents"]),
            "unserved_range": f"[{row['unserved_start']}, {row['unserved_end']})",
        })
    return out


def line_rows(store) -> list[dict]:
    """发布版本里的成分行（逐张冲抵 + 承诺），导出与明细共用。"""
    rows: list[dict] = []
    for payload in active_settlements(store):
        for line in payload["lines"]:
            rows.append({
                **line,
                "contract_id": payload["contract_id"],
                "amount": rounding.cents_text(line["cents"]),
            })
    return rows


def detail(store, settlement_id: str) -> dict | None:
    for row in active_settlements(store):
        if row["settlement_id"] == settlement_id:
            data = dict(row)
            data["payable"] = rounding.cents_text(row["payable_cents"])
            data["commitment"] = rounding.cents_text(row["commitment_cents"])
            data["issued"] = rounding.cents_text(row["issued_cents"])
            data["final_receivable"] = rounding.cents_text(
                row["final_receivable_cents"])
            data["lines"] = [
                {**line,
                 "amount": rounding.cents_text(line["cents"])}
                for line in row["lines"]
            ]
            return data
    return None


def totals_view(store) -> dict:
    rows = active_settlements(store)
    return {
        "settlements": len(rows),
        "commitment_cents": sum(row["commitment_cents"] for row in rows),
        "issued_cents": sum(row["issued_cents"] for row in rows),
        "payable_cents": sum(row["payable_cents"] for row in rows),
        "final_receivable_cents": sum(row["final_receivable_cents"] for row in rows),
        "commitment": rounding.cents_text(
            sum(row["commitment_cents"] for row in rows)),
        "issued": rounding.cents_text(
            sum(row["issued_cents"] for row in rows)),
        "payable": rounding.cents_text(
            sum(row["payable_cents"] for row in rows)),
        "final_receivable": rounding.cents_text(
            sum(row["final_receivable_cents"] for row in rows)),
    }


__all__ = ["import_sample", "summary", "run_rows", "overview", "contract_views",
           "settlement_rows", "line_rows", "detail", "totals_view",
           "active_settlements", "build_versions", "batch_key"]
