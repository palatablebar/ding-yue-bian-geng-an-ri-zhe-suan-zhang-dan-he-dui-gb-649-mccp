"""年度合同终止结算的规则与金额推导（纯函数 + 单一公式）。

业务约定（本模块是唯一来源）：

1. **合同承诺不可撤销**。年度合同签的是整份承诺 `C`；中途终止只改变服务截止日，
   不改变应收本金。未履行部分在运营口径下「作废」，但财务口径下要在本期一次结清，
   两者不矛盾——作废的是服务义务，不是收款权利。
2. **已出账单不得重复收费**。财务已经按月份开过的账单 `I₁…Iₙ` 逐张冲抵，
   终止结算单只收「承诺 − 已出账单」，不再按剩余天数重新计价。
3. **停机日只解释、不缩减**。停机日 `stop`（含当天服务）决定：

   - 已服务天数 = `[合同起, 停机日 + 1天)` 的本地日历日数；
   - 未履行天数 = `合同总天数 − 已服务天数`；
   - 未履行区间 = `[停机日 + 1天, 合同止)`，半开区间。

   金额不再按天数二次折算，天数只用来说明「承诺里有多少天没有服务」。
4. **唯一最终应收**：`最终应收 = 合同承诺`；
   `本期终止结算 = 合同承诺 − Σ已出账单`。
   页面、导出、清单读同一个 `payable_cents`，不各算一遍。
5. **停机日边界**：停机日必须 `合同起 <= 停机日 < 合同止`；
   停机日等于合同起表示一天都没服务（仍按承诺结清），停机日等于合同止
   表示服务到期，不需要终止结算，按 `R_TERMINATION_AFTER_END` 拒绝。

金额全程走「分」整数；精确额只用于复核差额，最终以整数分比较。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from ..m2_calendar import days as days_mod

ONE_DAY = dt.timedelta(days=1)


@dataclass
class IssuedRow:
    """已出账单：账期半开区间 + 金额。"""

    invoice_id: str
    period_start: dt.date
    period_end: dt.date
    amount_cents: int
    amount_exact: Decimal = Decimal("0")


@dataclass
class Reconciliation:
    """终止结算的核对结果：三方数字互相说明。"""

    commitment_cents: int
    issued_cents: int
    payable_cents: int
    remainder_cents: int
    served_days: int
    unserved_days: int
    period_days: int
    identity_ok: bool
    identity_diff: int
    flags: list[str] = field(default_factory=list)


def local_days(start: dt.date, end: dt.date) -> int:
    return days_mod.days(start, end)


def served_through(stop: dt.date) -> dt.date:
    """停机日含当天服务，服务截止到次日 0 点；返回半开区间右端。"""
    return stop + ONE_DAY


def unserved_range(stop: dt.date, contract_end: dt.date) -> tuple[dt.date, dt.date]:
    return served_through(stop), contract_end


def validate_stop(contract_start: dt.date, contract_end: dt.date,
                  stop: dt.date) -> str | None:
    """返回原因码；合法返回 None。"""
    if stop < contract_start:
        return "R_TERMINATION_BEFORE_START"
    if stop >= contract_end:
        return "R_TERMINATION_AFTER_END"
    return None


def derive(*, contract_start: dt.date, contract_end: dt.date, stop: dt.date,
           commitment_cents: int, issued: list[IssuedRow]) -> Reconciliation:
    """按唯一公式推导结算：承诺 − 已出 = 本期应收。"""
    period_days = local_days(contract_start, contract_end)
    served = local_days(contract_start, served_through(stop))
    unserved = max(period_days - served, 0)
    issued_cents = sum(row.amount_cents for row in issued)
    remainder = commitment_cents - issued_cents
    payable = commitment_cents - issued_cents
    flags: list[str] = []
    if remainder < 0:
        flags.append("R_ISSUED_OVER_COMMITMENT")
    return Reconciliation(
        commitment_cents=commitment_cents,
        issued_cents=issued_cents,
        payable_cents=payable,
        remainder_cents=remainder,
        served_days=served,
        unserved_days=unserved,
        period_days=period_days,
        identity_ok=(issued_cents + payable == commitment_cents),
        identity_diff=issued_cents + payable - commitment_cents,
        flags=flags,
    )


def issued_overlaps(issued: list[IssuedRow]) -> list[tuple[int, int]]:
    """已出账单账期重叠的 (保留, 重复) 下标对；重叠即重复收费，必须隔离复核。

    保留规则是确定的：按 (账期起, 账单号) 升序，先到先得；后到的账单只要与已保留
    的任一张有交集就算重复。这样一条链式重叠（A∩B、B∩C，但 A 与 C 相接不相交）
    只会隔离 B，不会把首尾两张本来合法的账单一起废掉。
    """
    pairs: list[tuple[int, int]] = []
    order = sorted(range(len(issued)),
                   key=lambda index: (issued[index].period_start,
                                      issued[index].period_end,
                                      issued[index].invoice_id))
    kept: list[int] = []
    for index in order:
        current = issued[index]
        culprit = next(
            (owner for owner in kept
             if current.period_start < issued[owner].period_end
             and issued[owner].period_start < current.period_end),
            None)
        if culprit is not None:
            pairs.append((culprit, index))
            continue
        kept.append(index)
    return pairs


def crosses_stop(period_start: dt.date, period_end: dt.date, stop: dt.date) -> bool:
    """账期右端超过停机日次日，说明跨过了停机日。"""
    return period_end > served_through(stop)


def termination_key(contract_id: str, termination_id: str, stop: dt.date) -> str:
    """终止指令的幂等键：显式指令号优先，否则按合同号 + 停机日派生。"""
    if termination_id:
        return f"CMD:{contract_id}:{termination_id}"
    return f"KEY:{contract_id}:{stop.isoformat()}"


def settlement_id_for(contract_id: str, stop: dt.date) -> str:
    """结算单号稳定可复算：ST-<停机年月>-<合同号>。"""
    return f"ST-{stop.strftime('%Y%m')}-{contract_id}"
