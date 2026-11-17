"""subbill 全局配置：列契约、编码链、状态与变更类型、Decimal 规则。

规则唯一来源，其他模块只读不写：
- 金额一律 Decimal，最小刻度 0.01 元，入库按「分」整数；
- 计费日边界取订阅时区的本地日 0 点，入库 UTC，展示固定 Asia/Shanghai；
- 期间一律半开区间 ``[start, end)``，天数为本地日历日差。
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UTC = dt.timezone.utc
DISPLAY_TZ = "Asia/Shanghai"

RULE_VERSION = "subbill/1.0.0"
CENT = Decimal("0.01")
DECIMAL_PREC = 34
MONEY_UNIT = "元"
DEFAULT_CURRENCY = "CNY"

# 编码回退链：先按 BOM，再严格 UTF-8，最后按 GB18030（GBK/GB2312 超集）
ENCODING_CHAIN = ("utf-8-sig", "utf-8", "gb18030")

# 空值哨兵：集中定义。None 哨兵 / 空串 / 0 三个语义分开
SENTINELS = frozenset(
    {"", "-", "--", "—", "——", "/", "n/a", "na", "null", "none", "nil",
     "未知", "无", "暂无", "待定", "不适用", "未提供", "?"}
)

# 订阅表列：规范键 → 中文列名 + 别名
SUB_COLUMNS = {
    "sub_id": ("订阅号", ("订阅号", "订阅ID", "订阅编号", "账号编号")),
    "account": ("账户", ("账户", "客户名称", "客户", "账户名")),
    "plan": ("套餐", ("套餐", "套餐名", "产品")),
    "price": ("月价(元)", ("月价(元)", "月价", "单价", "月价格")),
    "period_start": ("期间起", ("期间起", "计费起", "起租日")),
    "period_end": ("期间止", ("期间止", "计费止", "到期日")),
    "tz": ("时区", ("时区", "地区时区", "timezone")),
    "state": ("状态", ("状态", "订阅状态")),
}

CHANGE_COLUMNS = {
    "change_id": ("变更号", ("变更号", "变更ID", "变更编号")),
    "sub_id": ("订阅号", ("订阅号", "订阅ID", "订阅编号")),
    "effective": ("生效时间", ("生效时间", "生效日", "生效时刻")),
    "kind": ("类型", ("类型", "变更类型")),
    "new_plan": ("新套餐", ("新套餐", "目标套餐")),
    "new_price": ("新月价(元)", ("新月价(元)", "新月价", "新价格")),
}

# 年度合同：承诺总额是整份合同的应收，月价只作参考，不参与终止结算
CONTRACT_COLUMNS = {
    "contract_id": ("合同号", ("合同号", "合同ID", "合同编号")),
    "sub_id": ("订阅号", ("订阅号", "订阅ID", "订阅编号")),
    "account": ("账户", ("账户", "客户名称", "客户", "账户名")),
    "contract_start": ("合同起", ("合同起", "合同开始", "起租日", "服务起")),
    "contract_end": ("合同止", ("合同止", "合同结束", "到期日", "服务止")),
    "commitment": ("合同总额(元)", ("合同总额(元)", "合同总额", "合同承诺", "年度总额")),
    "stopped_on": ("停机日", ("停机日", "终止日", "停服日", "终止指令", "停机日期")),
    "termination_id": ("终止指令号", ("终止指令号", "终止号", "结算单号")),
    "state": ("状态", ("状态", "合同状态")),
}

# 已出账单：财务已经开票的历史月份，逐张冲抵，避免终止时重复收费
ISSUED_COLUMNS = {
    "contract_id": ("合同号", ("合同号", "合同ID", "合同编号")),
    "invoice_id": ("账单号", ("账单号", "发票号", "账单ID")),
    "period_start": ("账期起", ("账期起", "期间起", "计费起")),
    "period_end": ("账期止", ("账期止", "期间止", "计费止")),
    "amount": ("金额(元)", ("金额(元)", "金额", "账单额", "已出金额")),
}

STATE_LABELS = {
    "active": "生效中",
    "paused": "已暂停",
    "trial": "试用期",
    "cancelled": "已取消",
}
# 不计费状态：金额为 0，但天数计入「免费/暂停」一栏
ZERO_RATE_STATES = frozenset({"paused", "trial", "cancelled"})

CHANGE_KINDS = {
    "升配": "upgrade",
    "升级": "upgrade",
    "降配": "downgrade",
    "降级": "downgrade",
    "暂停": "pause",
    "挂起": "pause",
    "恢复": "resume",
    "取消": "cancel",
    "解约": "cancel",
}
KIND_LABELS = {
    "upgrade": "升配",
    "downgrade": "降配",
    "pause": "暂停",
    "resume": "恢复",
    "cancel": "取消",
}
KIND_EFFECT = {
    "upgrade": "active",
    "downgrade": "active",
    "resume": "active",
    "pause": "paused",
    "cancel": "cancelled",
}

# 流水线阶段：采信 → 期间 → 质量 → 变更 → 折算 → 账单
STAGES = ("ingest", "calendar", "quality", "changes", "proration", "invoices")
STAGE_LABELS = {
    "ingest": "采信",
    "calendar": "期间",
    "quality": "质量",
    "changes": "变更",
    "proration": "折算",
    "invoices": "账单",
}
STAGE_FLOW = "采信 → 期间 → 质量 → 变更 → 折算 → 账单"

INVOICE_STATUS_LABELS = {
    "ok": "已出账",
    "zero": "零金额",
    "conflict": "冲突",
}
RUN_STATUS_LABELS = {
    "done": "完成",
    "running": "运行中",
    "failed": "失败",
}

# ---- 年度合同终止结算 ----
# 结算单类型：本期新出的终止结算单；历史已出账单按原账期冲抵
SETTLEMENT_KIND = "termination"
SETTLEMENT_KIND_LABELS = {"termination": "终止结算", "period": "期间账单"}
SETTLEMENT_STATUS_LABELS = {
    "settled": "已结清",
    "zero": "零金额",
    "refund": "退款",
    "open": "未终止",
}
# 未履行部分处理方式：服务义务作废，但合同承诺作为债权在本期一次结清
TERMINATION_POLICY = "commitment"
TERMINATION_POLICY_LABELS = {
    "commitment": "未履行服务作废，合同承诺在本期一次结清",
}


@dataclass(frozen=True)
class Settings:
    """运行参数；测试用 replace() 覆盖，不读环境变量分支。"""

    db_path: str = str(PROJECT_ROOT / "var" / "subbill.sqlite")
    sample_dir: str = str(PROJECT_ROOT / "sample_data")
    display_tz: str = DISPLAY_TZ
    rule_version: str = RULE_VERSION
    default_subscriptions: str = "subscriptions_dirty.csv"
    default_changes: str = "changes_dirty.csv"
    default_contracts: str = "contracts_termination.csv"
    default_issued: str = "issued_invoices.csv"
    export_dir: str = str(PROJECT_ROOT / "var" / "export")

    @classmethod
    def from_env(cls) -> "Settings":
        base = cls()
        db_path = os.getenv("SUBBIL_DB") or base.db_path
        sample_dir = os.getenv("SUBBIL_SAMPLE_DIR") or base.sample_dir
        return replace(cls(), db_path=db_path, sample_dir=sample_dir)

    @property
    def data_dir(self) -> Path:
        return Path(self.db_path).resolve().parent
