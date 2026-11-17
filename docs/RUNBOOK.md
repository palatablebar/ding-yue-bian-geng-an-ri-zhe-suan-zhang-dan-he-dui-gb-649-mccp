# 运行手册 subbill

## 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -s http://127.0.0.1:18208/api/health
```

宿主机端口 18208 → 容器 8000。数据库在命名卷 `subbill-data` 的 `subbill.sqlite`。

## 主流程

```bash
# 1 采信自带脏样例（订阅 + 变更），返回 run_id 与四本账
curl -s -X POST http://127.0.0.1:18208/api/subscriptions/import \
  -H 'Content-Type: application/json' -d '{}'

# 2 周期诊断
curl -s http://127.0.0.1:18208/api/calendar | head -c 400

# 3 隔离区与原因码
curl -s http://127.0.0.1:18208/api/quarantine | head -c 400
curl -s -o /tmp/quarantine.csv http://127.0.0.1:18208/api/quarantine.csv

# 4 折算与变更
curl -s http://127.0.0.1:18208/api/proration | head -c 300
curl -s -X POST http://127.0.0.1:18208/api/changes | head -c 300

# 5 账单与对账
curl -s http://127.0.0.1:18208/api/invoices | head -c 400
curl -s http://127.0.0.1:18208/api/reconcile

# 6 导出
curl -s -o /tmp/invoices.csv http://127.0.0.1:18208/api/export/invoices.csv
curl -s -o /tmp/manifest.json http://127.0.0.1:18208/api/export/manifest.json
```

## 年度合同终止结算

```bash
# 采信合同与已出账单（样例自带 3 条隔离：重叠账期、未知合同、到期日停机）
curl -s -X POST http://127.0.0.1:18208/api/settlements/import \
  -H 'Content-Type: application/json' -d '{}'

# 结算单与合计（页面 /settlements 读同一组数字）
curl -s http://127.0.0.1:18208/api/settlements
curl -s -o /tmp/settlements.csv http://127.0.0.1:18208/api/settlements.csv
curl -s -o /tmp/settlement-lines.csv http://127.0.0.1:18208/api/settlements/detail.csv
curl -s -o /tmp/settlement-manifest.json http://127.0.0.1:18208/api/settlements/manifest.json
curl -s http://127.0.0.1:18208/api/contracts
```

合同表必需列：`合同号`、`合同起`、`合同止`、`合同总额(元)`；可选 `订阅号`、`账户`、`停机日`、`终止指令号`、`状态`。
已出账单表必需列：`合同号`、`账单号`、`账期起`、`账期止`、`金额(元)`。

复核顺序建议：

1. 看 `/settlements` 顶部四个数：承诺 = 已出 + 本期结算 = 最终应收；
2. 看「合同与已出账单」逐份合同：停机日、未履行区间、已出月份是否齐全，重叠账单标「未冲抵」；
3. 看「结算对账」四本账是否全平；
4. 点结算单看成分：第一行承诺，其后每张已出账单一行负数。

**重跑与修正**

- 同两份文件再采信：命中 `batch_key`，直接返回原批次，结算单不新增。
- 改了输入（金额或停机日）：新批次会替换同一 `(合同号, 指令键)` 的生效版本，结算单号不变。
- 删掉某份合同的停机日：该合同旧结算单在同一次发布事务里撤下，页面不再出现。
- 发布核对失败返回 409 `E_SETTLEMENT_VERIFY`，库内 `active` 集不动；批次记 `failed` 可查原因。

## 换成自己的数据

样例目录挂载在容器 `/app/sample_data`，采信时传文件名即可（只接受纯文件名）：

```bash
curl -s -X POST http://127.0.0.1:18208/api/subscriptions/import \
  -H 'Content-Type: application/json' \
  -d '{"subscriptions":"subscriptions_gbk.csv","changes":"changes_dirty.csv"}'
```

两份文件的列名会被映射到规范字段：订阅表需要 `订阅号`、`月价(元)`、`期间起`、`期间止`、`状态`；变更表需要 `变更号`、`订阅号`、`生效时间`、`类型`。缺必需列会整体隔离并给出 `R_HEADER_MISSING`。

## 重算与幂等

- 同一份输入再采信一次：命中 `batch_key`，直接返回原批次，行数与合计不变。
- 只重算金额：`POST /api/proration`；重算分段与抵扣：`POST /api/changes`。两者都会重新出账并刷新四本账。
- 想强制新建批次：改任一输入文件的内容（`batch_key` 由输入 sha256 决定）。

## 重建数据库

```bash
docker compose down -v          # 删掉命名卷，下次启动会重建空库
docker compose up -d --build
```

## 排错

| 现象 | 检查 |
|---|---|
| 首页 200 但表格为空 | 还没有批次，点「采信」；或 `/api/runs` 看是否有 `done` 批次 |
| 采信返回 400 `E_FILE_MISSING` | 文件名不在 `sample_data/`；只接受纯文件名 |
| 采信返回 400 `E_BAD_FILE_NAME` | 传了路径分隔符或空串 |
| 账单为 0 | 期间内全是暂停/试用，或月价为 0；看账单标记 `R_INVOICE_ZERO` |
| 对账不平 | 看 `/api/reconcile` 哪一本账 `status=mismatch`，再看对应明细 |
| 导出 404 `E_NO_RUN` | 还没批次；先采信 |
| 容器内 pytest 失败 | `docker compose exec -T app python -m pytest -q`，先看第一个失败 |

## 测试

```bash
docker compose exec -T app python -m pytest -q
docker compose exec -T app python -m pytest tests/test_invariants.py -q
```

## 日志

```bash
docker compose logs -f app
docker compose exec -T app ls -l /data
```
