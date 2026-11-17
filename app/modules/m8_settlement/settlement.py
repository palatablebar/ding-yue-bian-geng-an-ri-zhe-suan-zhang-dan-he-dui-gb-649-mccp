"""终止结算的推导、核对与结算单构造。

一张结算单由两部分组成，两者相加恰好等于本期应收：

    合同承诺（未履行部分一次结清）      +C
    已出账单冲抵（逐张，负号）          −I₁ … −Iₙ
    ────────────────────────────────────────
    本期终止结算应收                    C − ΣI

于是「最终应收」仍然只有一个：`Σ已出 + 本期结算 = C`。
停机日决定未履行区间与天数，写进结算单作为解释，不参与金额二次折算。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from ... import config
from . import rules


@dataclass
class SettlementPlan:
    contract: dict
    stop: dt.date
    period_days: int
    served_days: int
    unserved_days: int
    commitment_cents: int
    issued: list[dict]
    issued_cents: int
    payable_cents: int
    final_receivable_cents: int
    identity_diff: int
    settlement_id: str
    termination_key: str
    flags: list[str] = field(default_factory=list)

    @property
    def identity_ok(self) -> bool:
        return self.identity_diff == 0


def plan_for(contract: dict, issued: list[dict]) -> SettlementPlan | None:
    """未终止合同返回 None；终止合同返回结算计划。"""
    stop_text = contract.get("stopped_on") or ""
    if not stop_text:
        return None
    contract_start = dt.date.fromisoformat(contract["contract_start"])
    contract_end = dt.date.fromisoformat(contract["contract_end"])
    stop = dt.date.fromisoformat(stop_text)
    commitment_cents = int(contract["commitment_cents"])
    issued_rows = [
        rules.IssuedRow(
            invoice_id=row["invoice_id"],
            period_start=dt.date.fromisoformat(row["period_start"]),
            period_end=dt.date.fromisoformat(row["period_end"]),
            amount_cents=int(row["amount_cents"]),
            amount_exact=Decimal(row["amount_exact"]),
        )
        for row in issued
    ]
    derived = rules.derive(
        contract_start=contract_start, contract_end=contract_end, stop=stop,
        commitment_cents=commitment_cents, issued=issued_rows)
    flags = list(derived.flags)
    return SettlementPlan(
        contract=contract,
        stop=stop,
        period_days=derived.period_days,
        served_days=derived.served_days,
        unserved_days=derived.unserved_days,
        commitment_cents=commitment_cents,
        issued=issued,
        issued_cents=derived.issued_cents,
        payable_cents=derived.payable_cents,
        final_receivable_cents=int(contract["commitment_cents"]),
        identity_diff=derived.identity_diff,
        settlement_id=rules.settlement_id_for(contract["contract_id"], stop),
        termination_key=rules.termination_key(
            contract["contract_id"], contract.get("termination_id", ""), stop),
        flags=flags,
    )


def settlement_lines(plan: SettlementPlan) -> list[dict]:
    """结算单成分：先记承诺，再逐张冲抵已出账单；合计 = 本期应收。"""
    rows: list[dict] = [{
        "settlement_id": plan.settlement_id,
        "seq": 0,
        "kind": "commitment",
        "source": "contract",
        "label": "合同承诺（未履行部分一次结清）",
        "period_start": plan.contract["contract_start"],
        "period_end": plan.contract["contract_end"],
        "days": plan.period_days,
        "exact_text": str(Decimal(plan.commitment_cents) / 100),
        "cents": plan.commitment_cents,
        "flags": list(plan.flags),
    }]
    ordered = sorted(plan.issued, key=lambda row: (row["period_start"], row["invoice_id"]))
    for row in ordered:
        rows.append({
            "settlement_id": plan.settlement_id,
            "seq": len(rows),
            "kind": "issued_credit",
            "source": row["invoice_id"],
            "label": f"已出账单冲抵 {row['invoice_id']}",
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "days": int(row.get("days", 0)),
            "exact_text": str(-Decimal(row["amount_exact"])),
            "cents": -int(row["amount_cents"]),
            "flags": list(row.get("flags", [])),
        })
    return rows


def settlement_payload(plan: SettlementPlan) -> dict:
    """发布版本的唯一内容：页面、导出、清单都读它。"""
    return {
        "settlement_id": plan.settlement_id,
        "termination_key": plan.termination_key,
        "contract_id": plan.contract["contract_id"],
        "sub_id": plan.contract.get("sub_id", ""),
        "account": plan.contract.get("account", ""),
        "contract_start": plan.contract["contract_start"],
        "contract_end": plan.contract["contract_end"],
        "stopped_on": plan.stop.isoformat(),
        "unserved_start": rules.served_through(plan.stop).isoformat(),
        "unserved_end": plan.contract["contract_end"],
        "period_days": plan.period_days,
        "served_days": plan.served_days,
        "unserved_days": plan.unserved_days,
        "commitment_cents": plan.commitment_cents,
        "issued_cents": plan.issued_cents,
        "issued_count": len(plan.issued),
        "payable_cents": plan.payable_cents,
        "final_receivable_cents": plan.final_receivable_cents,
        "identity_diff": plan.identity_diff,
        "policy": config.TERMINATION_POLICY,
        "status": ("refund" if plan.payable_cents < 0
                   else "zero" if plan.payable_cents == 0 else "settled"),
        "flags": list(plan.flags),
        "lines": settlement_lines(plan),
    }


def verify_payload(payload: dict) -> list[str]:
    """发布前核对：三类数字必须能互相解释；返回问题列表。"""
    problems: list[str] = []
    commitment = int(payload["commitment_cents"])
    issued = int(payload["issued_cents"])
    payable = int(payload["payable_cents"])
    final = int(payload["final_receivable_cents"])
    lines = payload.get("lines", [])
    line_sum = sum(int(row["cents"]) for row in lines)
    if issued + payable != commitment:
        problems.append("issued_plus_payable_ne_commitment")
    if final != commitment:
        problems.append("final_receivable_ne_commitment")
    if line_sum != payable:
        problems.append("lines_ne_payable")
    credit_sum = -sum(int(row["cents"]) for row in lines
                      if row["kind"] == "issued_credit")
    if credit_sum != issued:
        problems.append("credit_lines_ne_issued")
    if int(payload["identity_diff"]) != 0:
        problems.append("identity_diff_nonzero")
    served = int(payload["served_days"])
    unserved = int(payload["unserved_days"])
    if served + unserved != int(payload["period_days"]):
        problems.append("days_not_conserved")
    if not payload["stopped_on"]:
        problems.append("missing_stop")
    return problems


def pick_directives(contracts: list[dict]) -> tuple[list[dict], list[dict]]:
    """同一合同多条终止指令：确定性取一条，其余留档不发布。

    胜者按 (停机日, 指令键) 最小；结果只由输入内容决定，与文件行序无关。
    """
    grouped: dict[str, list[dict]] = {}
    for contract in contracts:
        if contract.get("stopped_on"):
            grouped.setdefault(contract["contract_id"], []).append(contract)
    winners: list[dict] = []
    duplicates: list[dict] = []
    for contract_id in sorted(grouped):
        group = grouped[contract_id]
        if len(group) == 1:
            winners.append(group[0])
            continue
        ordered = sorted(group, key=lambda row: (
            row["stopped_on"],
            rules.termination_key(contract_id, row.get("termination_id", ""),
                                  dt.date.fromisoformat(row["stopped_on"])),
            row.get("row_no", 0),
        ))
        winner = ordered[0]
        winner = {**winner, "flags": [*winner.get("flags", []),
                                      "R_TERMINATION_DUP"]}
        winners.append(winner)
        for extra in ordered[1:]:
            duplicates.append({
                **extra,
                "superseded_by": winner["termination_id"]
                or winner["stopped_on"],
            })
    return winners, duplicates


__all__ = ["SettlementPlan", "plan_for", "settlement_lines", "settlement_payload",
           "verify_payload", "pick_directives"]
