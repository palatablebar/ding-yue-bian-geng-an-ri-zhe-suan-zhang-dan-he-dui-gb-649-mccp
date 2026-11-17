"""M8 终止结算：停机日、承诺、已出账单三方互证的唯一结算。

覆盖四条验收：

1. 停机日决定未履行区间与天数，但不缩减承诺；
2. 最终应收 = 合同承诺 = 已出账单 + 本期结算，逐张、逐合同、合计三层成立；
3. 页面与导出显示同一个最终应收；
4. 同一终止指令重跑不新增结算单，发布失败恢复完整旧版。
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import create_app
from app.modules.m8_settlement import rules, service


@pytest.fixture()
def settle_client(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def seeded_settlement(settle_client) -> TestClient:
    response = settle_client.post("/api/settlements/import", json={})
    assert response.status_code == 200, response.text
    return settle_client


def _settlements(client) -> list[dict]:
    return client.get("/api/settlements").json()["settlements"]


def _by_contract(rows: list[dict]) -> dict[str, dict]:
    return {row["contract_id"]: row for row in rows}


# ---------- 规则层：单一公式 ----------

def test_stop_boundary_is_inclusive_and_does_not_shrink_commitment():
    start, end = dt.date(2026, 1, 1), dt.date(2027, 1, 1)
    stop = dt.date(2026, 4, 15)
    result = rules.derive(contract_start=start, contract_end=end, stop=stop,
                          commitment_cents=1_200_000, issued=[])
    # 停机日当天仍然服务：已服务 105 天，未履行 260 天
    assert (result.served_days, result.unserved_days, result.period_days) == (105, 260, 365)
    # 未履行天数只作解释，不按天折减承诺
    assert result.payable_cents == 1_200_000
    assert result.commitment_cents == 1_200_000


def test_stop_must_be_strictly_before_contract_end():
    start, end = dt.date(2026, 1, 1), dt.date(2027, 1, 1)
    assert rules.validate_stop(start, end, dt.date(2025, 12, 31)) == \
        "R_TERMINATION_BEFORE_START"
    assert rules.validate_stop(start, end, start) is None
    assert rules.validate_stop(start, end, end) == "R_TERMINATION_AFTER_END"
    # 到期日不终止，不需要结算单
    assert rules.validate_stop(start, end, dt.date(2026, 12, 31)) is None


def test_issued_and_payable_always_reconstruct_commitment():
    issued = [rules.IssuedRow("INV-1", dt.date(2026, 1, 1), dt.date(2026, 2, 1), 100_000),
              rules.IssuedRow("INV-2", dt.date(2026, 2, 1), dt.date(2026, 3, 1), 100_000)]
    result = rules.derive(contract_start=dt.date(2026, 1, 1),
                          contract_end=dt.date(2027, 1, 1),
                          stop=dt.date(2026, 4, 15),
                          commitment_cents=1_200_000, issued=issued)
    assert result.issued_cents + result.payable_cents == result.commitment_cents
    assert result.identity_ok and result.identity_diff == 0


def test_chained_overlap_keeps_first_and_drops_only_the_duplicate():
    # A[1/1,2/1) 与 C[2/1,3/1) 只是相接，B[1/15,2/15) 与两者都重叠
    rows = [
        rules.IssuedRow("A", dt.date(2026, 1, 1), dt.date(2026, 2, 1), 1),
        rules.IssuedRow("B", dt.date(2026, 1, 15), dt.date(2026, 2, 15), 1),
        rules.IssuedRow("C", dt.date(2026, 2, 1), dt.date(2026, 3, 1), 1),
    ]
    assert rules.issued_overlaps(rows) == [(0, 1)]
    assert rules.issued_overlaps(list(reversed(rows))) == [(2, 1)]


def test_directive_key_is_stable_and_content_addressed():
    stop = dt.date(2026, 4, 15)
    assert rules.termination_key("CT-1", "TERM-1", stop) == "CMD:CT-1:TERM-1"
    assert rules.termination_key("CT-1", "", stop) == "KEY:CT-1:2026-04-15"
    assert rules.settlement_id_for("CT-1", stop) == "ST-202604-CT-1"


# ---------- 集成：三方数字互证 ----------

def test_every_settlement_reconciles_three_ways(seeded_settlement):
    data = seeded_settlement.get("/api/settlements").json()
    assert data["policy"] == config.TERMINATION_POLICY
    for row in data["settlements"]:
        assert row["issued_cents"] + row["payable_cents"] == row["commitment_cents"], \
            row["settlement_id"]
        assert row["final_receivable_cents"] == row["commitment_cents"]
        assert row["served_days"] + row["unserved_days"] == row["period_days"]
        assert row["unserved_range"].startswith("[")
        assert row["identity_diff"] == 0
        assert sum(line["cents"] for line in row["lines"]) == row["payable_cents"]


def test_settlement_totals_and_ledgers_balance(seeded_settlement):
    data = seeded_settlement.get("/api/settlements").json()
    totals = data["totals"]
    assert totals["commitment_cents"] == totals["issued_cents"] + totals["payable_cents"]
    assert totals["final_receivable_cents"] == totals["commitment_cents"]
    assert [row["ledger"] for row in data["ledgers"]] == [
        "settlements", "commitment", "issued", "directive"]
    assert all(row["status"] == "ok" for row in data["ledgers"])


def test_terminated_contract_lines_credit_each_issued_invoice(seeded_settlement):
    row = _by_contract(_settlements(seeded_settlement))["CT-2026-001"]
    assert row["stopped_on"] == "2026-04-15"
    assert row["issued_count"] == 3
    credits = [line for line in row["lines"] if line["kind"] == "issued_credit"]
    assert [line["source"] for line in credits] == sorted(
        line["source"] for line in credits)
    assert -sum(line["cents"] for line in credits) == row["issued_cents"]
    commitment_line = row["lines"][0]
    assert commitment_line["kind"] == "commitment"
    # 停机日写进结算单，可复核
    assert row["unserved_start"] == "2026-04-16"


def test_overlapping_issued_invoice_is_not_credited(seeded_settlement):
    row = _by_contract(_settlements(seeded_settlement))["CT-2026-001"]
    # 重叠的 INV-202601-CT1B 被隔离，三张合法账单之外的第四张不参与冲抵
    assert row["issued_count"] == 3
    assert row["issued_cents"] == 300_000
    app = seeded_settlement.app
    run = app.state.store.latest_run(kind="settlement")
    codes = {entry["code"] for entry in app.state.store.list_quarantine(run["run_id"])}
    assert "R_ISSUED_OVERLAP" in codes
    assert "R_ISSUED_UNKNOWN_CONTRACT" in codes
    assert "R_TERMINATION_AFTER_END" in codes


def test_duplicate_directive_picks_earliest_and_records_the_other(seeded_settlement):
    contracts = {row["contract_id"]: row for row in
                 seeded_settlement.get("/api/contracts").json()["contracts"]}
    row = contracts["CT-2026-008"]
    assert row["stopped_on"] == "2026-02-10"
    assert [item["stopped_on"] for item in row["superseded_directives"]] == ["2026-03-01"]
    settlement_row = _by_contract(_settlements(seeded_settlement))["CT-2026-008"]
    assert settlement_row["settlement_id"] == "ST-202602-CT-2026-008"


def test_unterminated_contract_has_no_settlement(seeded_settlement):
    contracts = {row["contract_id"]: row for row in
                 seeded_settlement.get("/api/contracts").json()["contracts"]}
    open_row = contracts["CT-2026-003"]
    assert open_row["settlement"] is None
    assert open_row["final_receivable_cents"] == open_row["commitment_cents"]
    assert "CT-2026-003" not in _by_contract(_settlements(seeded_settlement))


# ---------- 页面 / 导出 / 清单同数 ----------

def test_page_shows_same_final_receivable_as_api(seeded_settlement):
    api = seeded_settlement.get("/api/settlements").json()["totals"]
    html = seeded_settlement.get("/settlements").text
    assert api["payable"] in html
    assert api["final_receivable"] in html
    assert api["commitment"] in html


def test_csv_export_matches_api_and_page(seeded_settlement):
    import csv
    import io

    api = seeded_settlement.get("/api/settlements").json()
    text = seeded_settlement.get("/api/settlements.csv").text
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    assert len(rows) == len(api["settlements"])
    assert seeded_settlement.get("/api/settlements.csv").headers["x-row-count"] == \
        str(len(api["settlements"]))
    for csv_row in rows:
        match = _by_contract(api["settlements"])[csv_row["合同号"]]
        assert csv_row["最终应收(元)"] == match["final_receivable"]
        assert csv_row["本期结算(元)"] == match["payable"]
        assert csv_row["合同承诺(元)"] == match["commitment"]
        assert csv_row["停机日"] == match["stopped_on"]
    total = sum((__import__("decimal").Decimal(row["最终应收(元)"]) for row in rows),
                __import__("decimal").Decimal("0"))
    assert str(total) == api["totals"]["final_receivable"]


def test_line_export_sums_to_payable(seeded_settlement):
    import csv
    import io
    from decimal import Decimal

    text = seeded_settlement.get("/api/settlements/detail.csv").text
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    api = seeded_settlement.get("/api/settlements").json()
    by_id = {row["settlement_id"]: Decimal(0) for row in api["settlements"]}
    for row in rows:
        by_id[row["结算单号"]] += Decimal(row["金额(元)"])
    for row in api["settlements"]:
        assert by_id[row["settlement_id"]] == Decimal(row["payable"])


def test_settlement_manifest_records_inputs_and_totals(seeded_settlement):
    response = seeded_settlement.get("/api/settlements/manifest.json")
    assert response.status_code == 200
    payload = json.loads(response.text)
    assert [item["name"] for item in payload["inputs"]] == [
        "contracts_termination.csv", "issued_invoices.csv"]
    assert payload["totals"]["final_receivable_cents"] == "8100000"
    assert payload["settlement_version"]["active"] == len(payload["publish"]["published"]) \
        or payload["settlement_version"]["active"] > 0


# ---------- 幂等：同一终止指令重跑不新增结算单 ----------

def test_rerun_same_input_reuses_batch(seeded_settlement):
    store = seeded_settlement.app.state.store
    first = seeded_settlement.get("/api/settlements").json()
    versions_before = store.version_count()
    again = seeded_settlement.post("/api/settlements/import", json={}).json()
    assert again["reused"] is True
    assert len(store.list_runs(kind="settlement")) == 1
    assert store.version_count() == versions_before
    assert seeded_settlement.get("/api/settlements").json()["totals"] == first["totals"]


def _variant_sample(tmp_path, *, issued_amount: str = "1800.00"):
    """复制主样例到临时目录，改一处已出账单金额，得到新的 batch_key。"""
    from pathlib import Path

    source = config.PROJECT_ROOT / "sample_data"
    (tmp_path / "contracts_termination.csv").write_text(
        (source / "contracts_termination.csv").read_text(encoding="utf-8"),
        encoding="utf-8")
    issued = (source / "issued_invoices.csv").read_text(encoding="utf-8")
    issued = issued.replace(
        "CT-2026-002,INV-202607-CT2,2026-07-01,2026-08-01,2000.00",
        f"CT-2026-002,INV-202607-CT2,2026-07-01,2026-08-01,{issued_amount}")
    (tmp_path / "issued_invoices.csv").write_text(issued, encoding="utf-8")
    assert Path(tmp_path / "issued_invoices.csv").is_file()
    return Path(tmp_path)


def test_same_directive_new_input_does_not_add_a_second_settlement(
        seeded_settlement, settings, tmp_path):
    """改一份已出账单金额（新 batch_key）：结算单号不变，版本被替换而不是新增。"""
    store = seeded_settlement.app.state.store
    before = seeded_settlement.get("/api/settlements").json()["settlements"]
    second = replace(settings, db_path=settings.db_path,
                     sample_dir=str(_variant_sample(tmp_path)))
    result = service.import_sample(store, second, "contracts_termination.csv",
                                   "issued_invoices.csv")
    assert result["reused"] is False
    after = seeded_settlement.get("/api/settlements").json()["settlements"]
    assert len(after) == len(before)
    ids = {row["contract_id"]: row["settlement_id"] for row in after}
    assert ids["CT-2026-002"] == "ST-202607-CT-2026-002"
    # 同一合同同一指令只有一条 active 版本
    versions = store.versions_for_contract("CT-2026-002")
    assert len(versions) == 2
    assert len([row for row in versions if row["status"] == "active"]) == 1
    changed = _by_contract(after)["CT-2026-002"]
    assert changed["issued_cents"] == 1_400_000 - 20_000


def test_publish_failure_restores_whole_previous_version(seeded_settlement, settings,
                                                         tmp_path, monkeypatch):
    """核对失败时一行都不写：active 集与结算单仍是完整旧版，批次记 failed。"""
    store = seeded_settlement.app.state.store
    before_rows = seeded_settlement.get("/api/settlements").json()["settlements"]
    before_versions = {key: value["version_id"]
                       for key, value in store.active_versions().items()}
    assert before_rows and before_versions

    from app.modules.m8_settlement import settlement as settlement_mod

    monkeypatch.setattr(settlement_mod, "verify_payload",
                        lambda payload: ["forced_failure"])
    second = replace(settings, db_path=settings.db_path,
                     sample_dir=str(_variant_sample(tmp_path)))
    from app.errors import Conflict

    with pytest.raises(Conflict) as excinfo:
        service.import_sample(store, second, "contracts_termination.csv",
                              "issued_invoices.csv")
    assert excinfo.value.code == "E_SETTLEMENT_VERIFY"
    after_versions = {key: value["version_id"]
                      for key, value in store.active_versions().items()}
    assert after_versions == before_versions
    after_rows = seeded_settlement.get("/api/settlements").json()["settlements"]
    assert after_rows == before_rows
    # 失败批次留档，但状态是 failed，后续重跑不会复用它
    failed = [run for run in store.list_runs(kind="settlement")
              if run["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["note"].startswith("Conflict:")


def test_retry_after_failure_recomputes_instead_of_reusing_failed_batch(
        seeded_settlement, settings, tmp_path, monkeypatch):
    from app.modules.m8_settlement import settlement as settlement_mod

    second = replace(settings, db_path=settings.db_path,
                     sample_dir=str(_variant_sample(tmp_path)))
    store = seeded_settlement.app.state.store
    with monkeypatch.context() as patch:
        patch.setattr(settlement_mod, "verify_payload", lambda payload: ["boom"])
        with pytest.raises(Exception):
            service.import_sample(store, second, "contracts_termination.csv",
                                  "issued_invoices.csv")
    result = service.import_sample(store, second, "contracts_termination.csv",
                                   "issued_invoices.csv")
    assert result["reused"] is False
    assert result["status"] == "done"


def test_store_publish_rolls_back_all_versions_on_failure(seeded_settlement):
    """发布事务中途失败：已作废的旧版回到 active，不出现半发布。"""
    store = seeded_settlement.app.state.store
    before = {key: value["version_id"]
              for key, value in store.active_versions().items()}
    active_rows = list(store.active_versions().values())
    first = active_rows[0]
    second = active_rows[1]

    def new_payload(row, bump: int) -> dict:
        payload = json.loads(json.dumps(row["payload"]))
        payload["payable_cents"] += bump
        payload["commitment_cents"] += bump
        payload["final_receivable_cents"] += bump
        payload["lines"][0]["cents"] += bump
        return payload

    versions = [
        {"version_id": "SV-FAIL-1", "contract_id": first["contract_id"],
         "termination_key": first["termination_key"], "run_id": "ST-TEST",
         "payload": new_payload(first, 1)},
        {"version_id": "SV-FAIL-2", "contract_id": second["contract_id"],
         "termination_key": second["termination_key"], "run_id": "ST-TEST",
         "payload": new_payload(second, 2)},
    ]
    # 用触发器让第二条 INSERT 失败：验证第一条与旧版作废一起回滚
    with store.connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_settlement_publish BEFORE INSERT ON "
            "settlement_versions WHEN NEW.version_id='SV-FAIL-2' "
            "BEGIN SELECT RAISE(ABORT, 'forced publish failure'); END")
    try:
        with pytest.raises(Exception):
            store.publish_settlement_versions(versions, "2026-11-17T00:00:00+00:00")
    finally:
        with store.connect() as conn:
            conn.execute("DROP TRIGGER IF EXISTS fail_settlement_publish")
    after = {key: value["version_id"]
             for key, value in store.active_versions().items()}
    assert after == before
    # 失败事务没有留下半条新版本
    assert store.get_version("SV-FAIL-1") is None


def test_store_publish_supersedes_previous_version_in_one_transaction(
        seeded_settlement):
    store = seeded_settlement.app.state.store
    row = list(store.active_versions().values())[0]
    payload = json.loads(json.dumps(row["payload"]))
    payload["payable_cents"] += 100
    payload["commitment_cents"] += 100
    payload["final_receivable_cents"] += 100
    payload["lines"][0]["cents"] += 100
    result = store.publish_settlement_versions([{
        "version_id": "SV-NEW-1", "contract_id": row["contract_id"],
        "termination_key": row["termination_key"], "run_id": "ST-TEST",
        "payload": payload}], "2026-11-17T00:00:00+00:00")
    assert result["published"]
    active = store.active_versions()
    assert (row["contract_id"], row["termination_key"]) in active
    assert active[(row["contract_id"], row["termination_key"])]["version_id"] == "SV-NEW-1"
    old = [item for item in store.versions_for_contract(row["contract_id"])
           if item["version_id"] != "SV-NEW-1"]
    assert old and all(item["status"] == "superseded" for item in old)


def test_quarantine_export_includes_termination_codes(seeded_settlement):
    store = seeded_settlement.app.state.store
    run = store.latest_run(kind="settlement")
    text = seeded_settlement.get(
        f"/api/quarantine.csv?run_id={run['run_id']}").text
    assert "R_TERMINATION_DUP" in text or "R_ISSUED_OVERLAP" in text
    assert "R_TERMINATION_AFTER_END" in text


def test_home_page_offers_settlement_entry(seeded_settlement):
    html = seeded_settlement.get("/").text
    assert "终止结算" in html
    assert "结算批次" in html


def test_billing_and_settlement_batches_stay_separate(seeded_settlement):
    """两类批次互不串台：计费批次列表只列计费，结算批次列表只列结算。"""
    client = seeded_settlement
    billing = client.post("/api/subscriptions/import", json={}).json()
    settlement = client.post("/api/settlements/import", json={}).json()
    store = client.app.state.store
    billing_runs = [run["run_id"] for run in store.list_runs(kind="billing")]
    settlement_runs = [run["run_id"] for run in store.list_runs(kind="settlement")]
    assert billing["run_id"] in billing_runs
    assert settlement["run_id"] in settlement_runs
    assert billing["run_id"] not in settlement_runs
    assert settlement["run_id"] not in billing_runs
    # 计费采信不会因为结算批次存在而复用，反之亦然
    again_billing = client.post("/api/subscriptions/import", json={}).json()
    again_settlement = client.post("/api/settlements/import", json={}).json()
    assert again_billing["run_id"] == billing["run_id"]
    assert again_settlement["run_id"] == settlement["run_id"]
    # 计费页仍按计费口径出账，不受结算影响
    assert client.get("/api/invoices").status_code == 200


# ---------- 边界：超额已出、零承诺、全无终止指令 ----------

def _custom_sample(tmp_path, contracts_text: str, issued_text: str):
    from pathlib import Path

    (tmp_path / "contracts_termination.csv").write_text(contracts_text,
                                                        encoding="utf-8")
    (tmp_path / "issued_invoices.csv").write_text(issued_text, encoding="utf-8")
    return Path(tmp_path)


def test_over_issued_settles_as_refund_and_still_balances(settings, tmp_path):
    sample = _custom_sample(
        tmp_path,
        "合同号,订阅号,账户,合同起,合同止,合同总额(元),停机日,终止指令号,状态\n"
        "C1,S1,甲,2026-01-01,2027-01-01,1000.00,2026-06-01,T1,active\n"
        "C2,S2,乙,2026-01-01,2027-01-01,0.00,2026-06-01,T2,active\n",
        "合同号,账单号,账期起,账期止,金额(元)\n"
        "C1,I1,2026-01-01,2026-04-01,1200.00\n"
        "C2,I2,2026-01-01,2026-02-01,100.00\n")
    second = replace(settings, db_path=settings.db_path, sample_dir=str(sample))
    app = create_app(second)
    with TestClient(app) as client:
        response = client.post("/api/settlements/import", json={})
        assert response.status_code == 200, response.text
        rows = _by_contract(client.get("/api/settlements").json()["settlements"])
        assert rows["C1"]["payable_cents"] == -20_000
        assert rows["C1"]["status"] == "refund"
        assert "R_ISSUED_OVER_COMMITMENT" in rows["C1"]["flags"]
        assert rows["C2"]["payable_cents"] == -10_000
        # 退款也满足恒等式：已出 + 结算 = 承诺；页面与结算单一致
        assert rows["C1"]["issued_cents"] + rows["C1"]["payable_cents"] == \
            rows["C1"]["commitment_cents"]
        ledgers = client.get("/api/settlements").json()["ledgers"]
        assert all(row["status"] == "ok" for row in ledgers)


def test_no_terminated_contract_produces_clean_empty_settlement(settings, tmp_path):
    sample = _custom_sample(
        tmp_path,
        "合同号,订阅号,账户,合同起,合同止,合同总额(元),停机日,终止指令号,状态\n"
        "C1,S1,甲,2026-01-01,2027-01-01,1000.00,,,active\n",
        "合同号,账单号,账期起,账期止,金额(元)\n")
    second = replace(settings, db_path=settings.db_path, sample_dir=str(sample))
    app = create_app(second)
    with TestClient(app) as client:
        response = client.post("/api/settlements/import", json={})
        assert response.status_code == 200
        data = client.get("/api/settlements").json()
        assert data["settlements"] == []
        assert data["totals"]["settlements"] == 0
        assert all(row["status"] == "ok" for row in data["ledgers"])
        contracts = client.get("/api/contracts").json()["contracts"]
        assert contracts[0]["settlement"] is None
        assert contracts[0]["final_receivable_cents"] == contracts[0]["commitment_cents"]


def test_termination_without_stop_date_is_quarantined(settings, tmp_path):
    sample = _custom_sample(
        tmp_path,
        "合同号,订阅号,账户,合同起,合同止,合同总额(元),停机日,终止指令号,状态\n"
        "C1,S1,甲,2026-01-01,2027-01-01,1000.00,,T9,active\n",
        "合同号,账单号,账期起,账期止,金额(元)\n")
    second = replace(settings, db_path=settings.db_path, sample_dir=str(sample))
    app = create_app(second)
    with TestClient(app) as client:
        response = client.post("/api/settlements/import", json={})
        assert response.status_code == 200
        store = app.state.store
        run = store.latest_run(kind="settlement")
        codes = {row["code"] for row in store.list_quarantine(run["run_id"])}
        assert "R_TERMINATION_NO_STOP" in codes
        assert client.get("/api/settlements").json()["settlements"] == []


def test_issued_invoice_before_contract_start_is_quarantined(settings, tmp_path):
    sample = _custom_sample(
        tmp_path,
        "合同号,订阅号,账户,合同起,合同止,合同总额(元),停机日,终止指令号,状态\n"
        "C1,S1,甲,2026-01-01,2027-01-01,1000.00,2026-06-01,T1,active\n",
        "合同号,账单号,账期起,账期止,金额(元)\n"
        "C1,I0,2025-12-01,2026-01-01,100.00\n")
    second = replace(settings, db_path=settings.db_path, sample_dir=str(sample))
    app = create_app(second)
    with TestClient(app) as client:
        response = client.post("/api/settlements/import", json={})
        assert response.status_code == 200
        store = app.state.store
        run = store.latest_run(kind="settlement")
        codes = {row["code"] for row in store.list_quarantine(run["run_id"])}
        assert "R_ISSUED_PERIOD" in codes
        row = client.get("/api/settlements").json()["settlements"][0]
        assert row["issued_cents"] == 0
        assert row["payable_cents"] == row["commitment_cents"]


def test_existing_database_upgrades_without_losing_runs(tmp_path):
    """初始快照的库没有 runs.kind 与 issued_invoices.usable，打开时要自动补列。"""
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE runs (
               run_id TEXT PRIMARY KEY, batch_key TEXT NOT NULL, status TEXT NOT NULL,
               source TEXT NOT NULL, rule_version TEXT NOT NULL, created_at TEXT NOT NULL,
               finished_at TEXT, input_digest TEXT NOT NULL DEFAULT '',
               params_json TEXT NOT NULL DEFAULT '{}',
               counters_json TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '');
           INSERT INTO runs (run_id, batch_key, status, source, rule_version, created_at)
           VALUES ('SB-OLD', 'key', 'done', 'legacy', 'subbill/1.0.0',
                   '2026-01-01T00:00:00+00:00');
        """)
    conn.commit()
    conn.close()

    from app.store import Store

    store = Store(str(path))
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "SB-OLD"
    # 旧批次按计费口径归类，不会被结算页误读
    assert runs[0]["kind"] == "billing"
    assert store.latest_run(kind="settlement") is None
    # 再打开一次仍然幂等
    assert Store(str(path)).version_count() == 0


def test_contract_removed_from_snapshot_withdraws_its_settlement(
        seeded_settlement, settings, tmp_path):
    """合同文件是完整快照：整行删掉后，旧结算单必须从发布态撤下。"""
    client = seeded_settlement
    store = client.app.state.store
    before = [row["contract_id"] for row in
              client.get("/api/settlements").json()["settlements"]]
    assert "CT-2026-007" in before

    sample = _variant_sample(tmp_path)
    contracts_path = sample / "contracts_termination.csv"
    kept = [line for line in contracts_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("CT-2026-007")]
    contracts_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    second = replace(settings, db_path=settings.db_path, sample_dir=str(sample))
    result = service.import_sample(store, second, "contracts_termination.csv",
                                   "issued_invoices.csv")
    assert result["status"] == "done"
    after = [row["contract_id"] for row in
             client.get("/api/settlements").json()["settlements"]]
    assert "CT-2026-007" not in after
    assert len(after) == len(before) - 1
    # 发布清单记下撤下数量，可复核
    manifest_data = client.get("/api/settlements/manifest.json").json()
    assert manifest_data["settlement_version"]["withdrawn"] >= 1
    assert all(row["status"] == "ok"
               for row in client.get("/api/settlements").json()["ledgers"])
