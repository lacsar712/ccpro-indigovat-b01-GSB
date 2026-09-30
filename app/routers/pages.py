from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, Vat, Workshop
from app.schemas import DipLotIn
from app.services.vat_rules import (
    CLOTH_OVER_CAP_MESSAGE,
    VatRuleError,
    apply_vat_status_change,
    cloth_cap_liters,
    cycle_cloth_total,
    save_dip_lot,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False))


templates.env.filters["tojson"] = _tojson

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _spark_points(lots: list[DipLot], width: int = 72, height: int = 28) -> list[dict]:
    """把 redox 序列压成 sparkline 坐标（无有效读数则空）。"""
    vals = [float(l.redoxMv) for l in lots if l.redoxMv is not None]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 0 if n == 1 else round(i * (width - 1) / (n - 1), 2)
        y = round(height - 1 - ((v - lo) / span) * (height - 1), 2)
        pts.append({"x": x, "y": y})
    return pts


def _vat_payload(db: Session, vat: Vat) -> dict:
    all_lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id))
    # 列出口径与累计口径完全相同：仅列本还原周期（自上次离开闲置）以来的批次，
    # 这样「按缸列出的浸染合计」与周期累计天然对平，差为 0
    if vat.cycleStartedAt is not None:
        lots = [l for l in all_lots if l.createdAt >= vat.cycleStartedAt]
    else:
        lots = []
    latest = all_lots[-1] if all_lots else None
    recent = list(reversed(lots[-8:]))  # 展开区展示本周期近几笔
    cap = cloth_cap_liters(vat.volumeL)
    # 累计口径直接取服务层函数：与保存校验、按缸合计是同一窗口，差为 0
    used = cycle_cloth_total(db, vat)
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "lastRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": latest.dippedAt.strftime("%Y-%m-%d %H:%M") if latest else None,
        "cycleStartedAt": vat.cycleStartedAt.strftime("%Y-%m-%d %H:%M") if vat.cycleStartedAt else None,
        "cycleUsedMeters": float(used),
        "cycleCapMeters": float(cap),
        "cycleRemainingMeters": float(max(Decimal("0.00"), cap - used)),
        "spark": _spark_points(all_lots),
        "recentLots": [
            {
                "id": l.id,
                "dippedAt": l.dippedAt.strftime("%Y-%m-%d %H:%M"),
                "clothMeters": float(l.clothMeters),
                "redoxMv": float(l.redoxMv) if l.redoxMv is not None else None,
            }
            for l in recent
        ],
    }


def _bay_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
):
    # 始终下发全部缸位；工坊仅作前端 chip 筛选，避免切回「全部」时缺数据
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.code)
        .all()
    )
    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "vats": [_vat_payload(db, v) for v in vats],
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
    }


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[int] = None,
    vat: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "bay.html", _bay_context(request, db, user, workshop, vat))


@router.post("/bay/vats/{pk}/status", response_class=HTMLResponse)
async def bay_vat_status(
    pk: int,
    request: Request,
    status: str = Form(...),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    # 锁缸行：与并发浸染提交互斥，避免「离开/回到闲置」与累计窗口交错
    item = db.scalar(select(Vat).where(Vat.id == pk).with_for_update())
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        apply_vat_status_change(db, item, status)
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.post("/bay/vats/{pk}/lots", response_class=HTMLResponse)
async def bay_log_lot(
    pk: int,
    request: Request,
    dippedAt: str = Form(...),
    clothMeters: str = Form(...),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    ws = int(workshop) if workshop.strip() else None
    if not db.get(Vat, pk):
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        # 唯一保存链：单笔>0、闲置禁染、周期累计缸容8%上限都在此校验，
        # 与直打接口共用，前端 min/提示不算数
        save_dip_lot(
            db,
            vat_id=pk,
            dipped_at=datetime.fromisoformat(dippedAt),
            cloth_meters=Decimal(clothMeters),
            redox_mv=Decimal(redoxMv) if redoxMv.strip() else None,
        )
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except (ValueError, InvalidOperation) as exc:
        error = f"浸染记录无效：{exc}"
        db.rollback()
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)


@router.post("/api/vats/{pk}/lots")
async def api_log_lot(
    pk: int,
    payload: DipLotIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """直打保存接口（JSON）。

    与展开区表单是同一个保存入口 save_dip_lot：单笔布米 > 0、闲置缸禁染、
    周期累计缸容 8% 上限，超限返回的中文文案与展开区完全相同（400 + detail）。
    """
    user = _need_login(request, db)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "未登录。"})
    if payload.vat_id != pk:
        return JSONResponse(status_code=400, content={"detail": "缸号与路径不一致。"})
    try:
        lot = save_dip_lot(
            db,
            vat_id=pk,
            dipped_at=payload.dippedAt,
            cloth_meters=payload.clothMeters,
            redox_mv=payload.redoxMv,
        )
        db.commit()
    except VatRuleError as exc:
        db.rollback()
        return JSONResponse(status_code=400, content={"detail": exc.message})
    return JSONResponse(
        status_code=201,
        content={
            "detail": "已写入本缸。",
            "lot": {
                "id": lot.id,
                "vat_id": lot.vat_id,
                "dippedAt": lot.dippedAt.isoformat(),
                "clothMeters": float(lot.clothMeters),
                "redoxMv": float(lot.redoxMv) if lot.redoxMv is not None else None,
                "createdAt": lot.createdAt.isoformat(),
            },
        },
    )
