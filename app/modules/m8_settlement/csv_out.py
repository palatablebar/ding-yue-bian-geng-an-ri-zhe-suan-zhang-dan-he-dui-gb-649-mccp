"""终止结算导出：结算单、明细行、运行清单。页面与导出读同一发布版本。"""
from __future__ import annotations

import csv
import io
import json

from ... import config

SETTLEMENT_HEADER = [
    "结算单号", "合同号", "订阅号", "账户", "合同起", "合同止", "停机日",
    "未履行区间", "期间天数", "已服务天数", "未履行天数", "合同承诺(元)",
    "已出账单(元)", "已出账单张数", "本期结算(元)", "最终应收(元)",
    "状态", "校验位", "标记",
]
LINE_HEADER = [
    "结算单号", "合同号", "序", "成分", "来源", "账期起", "账期止",
    "天数", "金额(元)", "标记",
]


def _writer() -> tuple[io.StringIO, "csv._writer"]:
    buffer = io.StringIO()
    return buffer, csv.writer(buffer, lineterminator="\n")


def flags_text(codes) -> str:
    return "|".join(codes or [])


def settlements_csv(rows: list[dict], check_digit: str) -> str:
    buffer, writer = _writer()
    writer.writerow(SETTLEMENT_HEADER)
    for row in rows:
        writer.writerow([
            row["settlement_id"], row["contract_id"], row["sub_id"],
            row["account"], row["contract_start"], row["contract_end"],
            row["stopped_on"], row["unserved_range"], row["period_days"],
            row["served_days"], row["unserved_days"], row["commitment"],
            row["issued"], row["issued_count"], row["payable"],
            row["final_receivable"],
            config.SETTLEMENT_STATUS_LABELS.get(row["status"], row["status"]),
            check_digit, flags_text(row["flags"]),
        ])
    return buffer.getvalue()


def settlement_lines_csv(rows: list[dict]) -> str:
    buffer, writer = _writer()
    writer.writerow(LINE_HEADER)
    for row in rows:
        writer.writerow([
            row["settlement_id"], row["contract_id"], row["seq"] + 1,
            "合同承诺" if row["kind"] == "commitment" else "已出账单冲抵",
            row["source"], row["period_start"], row["period_end"],
            row["days"], row["amount"], flags_text(row["flags"]),
        ])
    return buffer.getvalue()


def json_text(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def with_bom(text: str) -> str:
    return "﻿" + text
