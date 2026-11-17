"""M8 入口：关账、回读、补账落账、续跑、撤销、核对与账本总览。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...web import Ctx, get_ctx
from . import service

router = APIRouter(tags=["M8 关账与补账"])


@router.get("/api/ledger")
def api_ledger(ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.overview(ctx.settings, ctx.store)


@router.get("/api/ledger/reconcile")
def api_ledger_reconcile(ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.reconcile(ctx.settings, ctx.store)


@router.post("/api/ledger/close")
def api_close(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    return service.close(
        ctx.settings, ctx.store,
        month=str(body.get("month") or ""),
        expected_version=body.get("expected_version"),
        batch_id=body.get("batch_id"))


@router.post("/api/ledger/reread")
def api_reread(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    return service.reread(
        ctx.settings, ctx.store,
        readings=body.get("readings") or [],
        batch_id=body.get("batch_id"),
        note=str(body.get("note") or ""))


@router.post("/api/ledger/supplement")
def api_supplement(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    return service.supplement(
        ctx.settings, ctx.store,
        reread_id=str(body.get("reread_id") or ""),
        target_month=str(body.get("target_month") or ""),
        expected_version=body.get("expected_version"),
        request_id=body.get("request_id"),
        crash_after=body.get("crash_after"))


@router.post("/api/ledger/supplement/resume")
def api_resume(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    return service.resume(
        ctx.settings, ctx.store,
        request_id=str(body.get("request_id") or ""),
        crash_after=body.get("crash_after"))


@router.post("/api/ledger/reverse")
def api_reverse(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    return service.reverse(
        ctx.settings, ctx.store,
        request_id=str(body.get("request_id") or ""),
        expected_version=body.get("expected_version"))
