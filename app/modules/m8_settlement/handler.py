"""M8 路由：合同终止结算的采信、列表、明细、导出。

静态路径（`/settlements.csv`、`/settlements/detail.csv`、`/settlements/manifest.json`）
必须声明在动态路径 `/settlements/{id}` 之前，否则会被当成结算单号。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request, Response

from ... import config, manifest
from ...errors import BadRequest, NotFound
from . import csv_out, service

router = APIRouter(prefix="/api", tags=["M8 终止结算"])


def _run_or_404(request: Request) -> dict:
    """结算批次：显式 run_id 优先，否则最近一批已完成的结算；不是结算批次就 404。"""
    ctx = request.app.state.ctx
    run_id = request.query_params.get("run_id")
    run = (ctx.require_run(run_id) if run_id
           else request.app.state.store.latest_run(status="done", kind="settlement"))
    if run is None or run.get("kind") != "settlement":
        raise NotFound("还没有终止结算批次，先采信合同与已出账单",
                       code="E_NO_SETTLEMENT_RUN")
    return run


def _check_of(request: Request, run: dict | None) -> str:
    if run is None:
        return ""
    data = request.app.state.store.get_manifest(run["run_id"]) or {}
    return data.get("check_digit") or manifest.check_digit(run["run_id"])


@router.post("/settlements/import")
def import_sample(request: Request,
                  payload: dict[str, Any] | None = Body(default=None)) -> dict:
    settings = request.app.state.settings
    store = request.app.state.store
    body = payload or {}
    contracts = body.get("contracts")
    issued = body.get("issued")
    for value in (contracts, issued):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise BadRequest("文件名不能为空", code="E_BAD_FILE_NAME")
    data = service.import_sample(store, settings, contracts, issued)
    data["check_digit"] = data.get("check_digit") or ""
    return data


@router.get("/settlements")
def settlements(request: Request) -> dict:
    store = request.app.state.store
    run = service.latest_settlement_run(store)
    if request.query_params.get("run_id"):
        run = _run_or_404(request)
    run_id = run["run_id"] if run else ""
    data = service.overview(store, run_id)
    return {
        "run_id": run_id,
        "check_digit": _check_of(request, run),
        "totals": service.totals_view(store),
        "settlements": service.settlement_rows(store),
        "ledgers": data["ledgers"],
        "batch_ledgers": data["batch_ledgers"],
        "policy": config.TERMINATION_POLICY,
        "policy_label": config.TERMINATION_POLICY_LABELS[config.TERMINATION_POLICY],
    }


@router.get("/settlements.csv")
def settlements_csv(request: Request) -> Response:
    store = request.app.state.store
    run = service.latest_settlement_run(store)
    if request.query_params.get("run_id"):
        run = _run_or_404(request)
    rows = service.settlement_rows(store)
    text = csv_out.settlements_csv(rows, _check_of(request, run))
    return Response(
        content=csv_out.with_bom(text), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="settlements-{run["run_id"] if run else "none"}.csv"',
                 "X-Row-Count": str(len(rows))},
    )


@router.get("/settlements/detail.csv")
def settlement_lines_csv(request: Request) -> Response:
    store = request.app.state.store
    run = service.latest_settlement_run(store)
    if request.query_params.get("run_id"):
        run = _run_or_404(request)
    rows = service.line_rows(store)
    return Response(
        content=csv_out.with_bom(csv_out.settlement_lines_csv(rows)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="settlement-lines-{run["run_id"] if run else "none"}.csv"',
                 "X-Row-Count": str(len(rows))},
    )


@router.get("/settlements/manifest.json")
def settlement_manifest(request: Request) -> Response:
    run = _run_or_404(request)
    data = request.app.state.store.get_manifest(run["run_id"]) or {}
    return Response(
        content=csv_out.json_text(data),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="settlement-manifest-{run["run_id"]}.json"'},
    )


@router.get("/settlements/{settlement_id}")
def settlement_detail(request: Request, settlement_id: str) -> dict:
    data = service.detail(request.app.state.store, settlement_id)
    if data is None:
        raise NotFound(f"结算单不存在：{settlement_id}", code="E_NO_SETTLEMENT")
    return data


@router.get("/contracts")
def contracts(request: Request) -> dict:
    run = _run_or_404(request)
    store = request.app.state.store
    return {"run_id": run["run_id"],
            "contracts": service.contract_views(store, run["run_id"]),
            "totals": service.totals_view(store)}
