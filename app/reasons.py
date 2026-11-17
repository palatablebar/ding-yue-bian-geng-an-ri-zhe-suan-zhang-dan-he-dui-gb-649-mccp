"""原因码唯一来源：每条隔离记录与每个标记都必须来自这里。

三个级别：
- ``error``：行不可用，进隔离区；
- ``warn``：行可用但存在需要人工复核的事实（重叠、乱序、夏令时等）；
- ``info``：可用且已自动归一（千分位、全角、三位小数等）。
"""
from __future__ import annotations

# code → (level, 中文说明)
REASONS: dict[str, tuple[str, str]] = {
    # ---- 采信与列契约 ----
    "R_EMPTY_FILE": ("error", "文件没有数据行"),
    "R_HEADER_ONLY": ("error", "文件只有表头"),
    "R_BLANK_ROWS": ("warn", "文件含空白行"),
    "R_COLUMN_COUNT": ("error", "列数与表头不一致"),
    "R_KEY_MISSING": ("error", "业务键缺失"),
    "R_HEADER_UNKNOWN": ("warn", "出现未登记列名"),
    "R_HEADER_MISSING": ("error", "缺少必需列"),
    "R_ROW_TRUNCATED": ("error", "末行引号未闭合"),
    "R_HEADER_REPEAT": ("warn", "分段导出重复表头"),
    # ---- 字段归一 ----
    "R_PRICE_FORMAT": ("error", "月价格式无法解析"),
    "R_PRICE_MISSING": ("error", "月价缺失"),
    "R_PRICE_PRECISION": ("info", "月价超过两位小数"),
    "R_PRICE_THOUSANDS": ("info", "月价含千分位"),
    "R_PRICE_FULLWIDTH": ("info", "月价含全角字符"),
    "R_DATE_FORMAT": ("error", "日期格式无法解析"),
    "R_PERIOD_MISSING": ("error", "期间起止缺失"),
    "R_PERIOD_REVERSED": ("error", "期间止早于期间起"),
    "R_PERIOD_ZERO": ("error", "期间天数为零"),
    "R_TZ_UNKNOWN": ("error", "时区无法解析"),
    "R_STATE_UNKNOWN": ("error", "状态无法识别"),
    # ---- 订阅键 ----
    "R_DUP_SUB_KEY": ("error", "订阅号重复且取值冲突"),
    "R_DUP_SUB_ROW": ("info", "订阅号重复且取值相同"),
    # ---- 变更 ----
    "R_CHANGE_UNKNOWN_SUB": ("error", "变更引用未知订阅"),
    "R_CHANGE_KIND": ("error", "变更类型无法识别"),
    "R_CHANGE_OVERLAP": ("warn", "多条变更生效时刻相同"),
    "R_SAME_DAY_MULTI": ("warn", "同一订阅同日多次变更"),
    "R_LATE_CHANGE": ("warn", "变更行乱序到达"),
    "R_TIME_TRUNCATED": ("warn", "生效时刻按本地日取整"),
    "R_DST_AMBIGUOUS": ("warn", "本地时刻落在夏令时重叠段"),
    "R_DST_GAP": ("warn", "本地时刻落在夏令时跳过段"),
    "R_CHANGE_OUT_OF_PERIOD": ("warn", "变更生效日在账单期间外"),
    "R_CHANGE_ON_PERIOD_START": ("warn", "变更生效日等于期间首日"),
    # ---- 账单 ----
    "R_DUP_INVOICE": ("error", "同一订阅同一期间出现两张账单"),
    "R_INVOICE_ZERO": ("info", "账单金额为零"),
    "R_ROUND_ADJUST": ("info", "尾差按最大余额分配到分"),
    # ---- 年度合同终止结算 ----
    # 合同定义（承诺、起止）无法采信
    "R_CONTRACT_KEY_MISSING": ("error", "合同号缺失"),
    "R_CONTRACT_PERIOD": ("error", "合同起止无法解析或止早于起"),
    "R_CONTRACT_DATE": ("error", "合同起止日期格式无法解析"),
    "R_CONTRACT_COMMITMENT": ("error", "合同总额缺失、无法解析或为负"),
    "R_CONTRACT_DUP_KEY": ("error", "合同号重复且取值冲突"),
    "R_CONTRACT_DUP_ROW": ("info", "合同号重复且取值相同"),
    # 终止指令
    "R_TERMINATION_UNKNOWN_CONTRACT": ("error", "终止指令引用未知合同"),
    "R_TERMINATION_BEFORE_START": ("error", "停机日早于合同起"),
    "R_TERMINATION_AFTER_END": ("error", "停机日不早于合同止"),
    "R_TERMINATION_NO_STOP": ("warn", "合同标记终止但缺停机日"),
    "R_TERMINATION_ID_DERIVED": ("info", "终止指令号缺失，按合同号与停机日派生"),
    "R_TERMINATION_DUP": ("warn", "同一合同出现多条不同终止指令"),
    "R_TERMINATION_REUSED": ("info", "同一终止指令重跑，复用原结算单"),
    "R_TERMINATION_DUP_GENERATED": ("error", "同一合同重复生成终止结算单"),
    "R_TERMINATION_MISSING_REMAINDER": ("error", "终止结算单缺少未履行承诺"),
    # 已出账单
    "R_ISSUED_UNKNOWN_CONTRACT": ("error", "已出账单引用未知合同"),
    "R_ISSUED_PERIOD": ("error", "已出账期无法解析或落在合同外"),
    "R_ISSUED_AMOUNT": ("error", "已出账单金额无法解析或为负"),
    "R_ISSUED_OVERLAP": ("error", "已出账单账期重叠，存在重复收费"),
    "R_DUP_INVOICE_ROW": ("info", "已出账单重复且取值相同"),
    "R_ISSUED_AFTER_STOP": ("warn", "已出账单账期跨过或晚于停机日"),
    "R_ISSUED_OVER_COMMITMENT": ("warn", "已出账单超过合同承诺，结算为退款"),
}

LEVEL_LABELS = {"error": "隔离", "warn": "复核", "info": "归一"}


def level_of(code: str) -> str:
    return REASONS.get(code, ("error", "未登记原因码"))[0]


def label_of(code: str) -> str:
    return REASONS.get(code, ("error", "未登记原因码"))[1]


def catalog() -> list[dict]:
    """按 code 排序的原因码目录，页面与导出共用。"""
    return [
        {"code": code, "level": level, "level_label": LEVEL_LABELS[level], "label": label}
        for code, (level, label) in sorted(REASONS.items())
    ]


def codes_by_level(level: str) -> list[str]:
    return sorted(code for code, (lv, _) in REASONS.items() if lv == level)
