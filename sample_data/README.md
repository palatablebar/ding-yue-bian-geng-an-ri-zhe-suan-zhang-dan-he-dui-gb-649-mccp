# 样例数据

计费期间一律 `[期间起, 期间止)` 半开区间，日期按订阅时区的本地日；金额单位元，入库 Decimal。

| 文件 | 编码 | 用途 |
|---|---|---|
| `subscriptions_dirty.csv` | UTF-8 带 BOM | 主样例：千分位、全角数字、三位小数、期间倒置、多一列、重复键冲突、未知时区、未知状态、缺价、空行 |
| `changes_dirty.csv` | UTF-8 | 变更主样例：Z 与 +08:00 混用、夏令时重叠段、同日两次变更、同一时刻两条、乱序迟到、期间外变更、非零点生效、未知订阅 |
| `subscriptions_gbk.csv` | GB18030 | 编码回退链：同一套代码路径读国标编码 |
| `subscriptions_header_only.csv` | UTF-8 | 空态：仅表头 |
| `subscriptions_blank.csv` | UTF-8 | 空态：全空行 |
| `subscriptions_quoted.csv` | UTF-8 | 引号内含换行：逻辑行数 = 记录数 |
| `subscriptions_truncated.csv` | UTF-8 | 截断尾行：引号未闭合 |
| `contracts_termination.csv` | UTF-8 | 年度合同 + 终止指令：千分位承诺、重复指令、停机日晚于合同止 |
| `issued_invoices.csv` | UTF-8 | 已出月份：链式账期重叠、未知合同号 |

主样例里有 16 条订阅记录（其中 6 条会被隔离）、14 条变更记录（其中 1 条未知订阅被隔离）。
合同样例 10 行：7 份终止合同、1 份未终止、1 份到期日停机（隔离）、1 份重复终止指令（留档）。
已出账单样例 13 行：11 张参与冲抵、1 张账期重叠（隔离不冲抵）、1 张未知合同（隔离）。
