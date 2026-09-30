from datetime import datetime
from decimal import Decimal
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, Vat, Workshop
from app.schemas import DipLotIn
from app.services.vat_rules import (
    ERR_LOT_TIME,
    VatNotFoundError,
    VatRuleError,
    advance_cycle_on_leave_idle,
    cloth_cap,
    lock_vat,
    save_dip_lot,
    validate_vat_status_change,
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


def _vat_payload(vat: Vat) -> dict:
    lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id))
    chronological = lots
    latest = lots[-1] if lots else None
    recent = list(reversed(lots[-8:]))  # 展开区展示近几笔
    # 本周期累计与服务层同一口径（同缸同 cycleId 求和），保证与列表合计对得上
    cycle_meters = sum(
        (Decimal(l.clothMeters) for l in lots if l.cycleId == vat.cycleSeq),
        Decimal("0"),
    )
    cap = cloth_cap(vat.volumeL)
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "cycleId": vat.cycleSeq,
        "cycleMeters": float(cycle_meters),
        "cycleLimit": float(cap),
        "cycleRatioPct": float(cycle_meters / vat.volumeL * 100) if vat.volumeL else 0.0,
        "lastRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": latest.dippedAt.strftime("%Y-%m-%d %H:%M") if latest else None,
        "spark": _spark_points(chronological),
        "recentLots": [
            {
                "id": l.id,
                "cycleId": l.cycleId,
                "inCurrentCycle": l.cycleId == vat.cycleSeq,
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
        "vats": [_vat_payload(v) for v in vats],
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
    ws = int(workshop) if workshop.strip() else None
    error = None
    try:
        # 与浸染保存同一把缸行锁：状态切换（离开闲置开启新周期）与登记互斥
        item = lock_vat(db, pk)
        latest = (
            db.query(DipLot)
            .filter(DipLot.vat_id == pk)
            .order_by(DipLot.dippedAt.desc(), DipLot.id.desc())
            .first()
        )
        validate_vat_status_change(item, status, latest)
        advance_cycle_on_leave_idle(db, item, status)
        item.status = status
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except VatNotFoundError:
        db.rollback()
        return RedirectResponse("/", status_code=303)
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
    error = None
    try:
        dipped_at = datetime.fromisoformat(dippedAt)
    except ValueError:
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, ws, pk, ERR_LOT_TIME),
            status_code=400,
        )
    try:
        save_dip_lot(
            db,
            vat_id=pk,
            dipped_at=dipped_at,
            cloth_meters=clothMeters,
            redox_mv=redoxMv,
        )
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except VatNotFoundError:
        return RedirectResponse("/", status_code=303)
    except VatRuleError as exc:
        error = exc.message
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.post("/api/vats/{pk}/lots")
async def api_log_lot(
    pk: int,
    payload: DipLotIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """直打保存接口：与展开区表单共用 save_dip_lot，同规则、同一句中文。"""
    user = _need_login(request, db)
    if not user:
        return JSONResponse({"detail": "请先登录。"}, status_code=401)
    try:
        # 以路径 pk 为准，忽略 body 里的 vat_id，避免越缸写入
        lot = save_dip_lot(
            db,
            vat_id=pk,
            dipped_at=payload.dippedAt,
            cloth_meters=payload.clothMeters,
            redox_mv=payload.redoxMv,
        )
    except VatNotFoundError:
        return JSONResponse({"detail": "染缸不存在。"}, status_code=404)
    except VatRuleError as exc:
        return JSONResponse({"detail": exc.message}, status_code=400)
    return JSONResponse(
        {
            "id": lot.id,
            "vatId": lot.vat_id,
            "cycleId": lot.cycleId,
            "dippedAt": lot.dippedAt.isoformat(),
            "clothMeters": float(lot.clothMeters),
            "redoxMv": float(lot.redoxMv) if lot.redoxMv is not None else None,
        },
        status_code=201,
    )


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
