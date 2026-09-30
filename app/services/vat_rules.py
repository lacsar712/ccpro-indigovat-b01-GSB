"""染缸状态与浸染登记业务规则。

浸染保存只有一条服务链 :func:`save_dip_lot`，展开区表单提交与直打 JSON
接口都必须走它，前端不得另设口径。
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DipLot, Vat


# 一个还原周期（自上次离开闲置起）内，累计布米相对缸容升数的占比上限
CLOTH_RATIO_CAP = Decimal("0.08")  # 即 8%
_CLOTH_QUANT = Decimal("0.01")
# 与 dip_lots.clothMeters Numeric(10,2) 容量对齐，超出按非法布米拒而非 500
_MAX_CLOTH_METERS = Decimal("99999999.99")

# 两条保存路径共用的同一句中文提示（不得各自改写）
ERR_METERS_POSITIVE = "布料米数必须为大于 0 的有限数值。"
ERR_VAT_IDLE = "该缸当前为闲置状态，离开闲置后才能登记浸染。"
ERR_CAP_EXCEEDED = "本还原周期累计布料米数已达缸容的 8% 上限，该笔浸染不予登记。"
ERR_LOT_TIME = "浸染时间无效，请重新选择。"


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class VatNotFoundError(Exception):
    pass


def cloth_cap(volume_l: Decimal) -> Decimal:
    """缸容（升）× 8% 换算成本周期累计布米上限（米，保留两位小数）。"""
    return (Decimal(volume_l) * CLOTH_RATIO_CAP).quantize(
        _CLOTH_QUANT, rounding=ROUND_HALF_UP
    )


def parse_meters(raw) -> Decimal:
    """把表单/接口入参解析成有限正数布米；非法即拒。"""
    if isinstance(raw, Decimal):
        meters = raw
    else:
        try:
            meters = Decimal(str(raw).strip())
        except (InvalidOperation, AttributeError, ValueError):
            raise VatRuleError(ERR_METERS_POSITIVE)
    if not meters.is_finite() or meters <= 0 or meters > _MAX_CLOTH_METERS:
        raise VatRuleError(ERR_METERS_POSITIVE)
    try:
        quantized = meters.quantize(_CLOTH_QUANT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        # 位数远超 Numeric(10,2) 的巨值同样按非法布米拒绝
        raise VatRuleError(ERR_METERS_POSITIVE)
    # 0.0001 之类量化后归零的输入不允许入库为 0 米残行
    if quantized <= 0:
        raise VatRuleError(ERR_METERS_POSITIVE)
    return quantized


def lock_vat(db: Session, vat_id: int) -> Vat:
    """锁住缸行：所有浸染写入在同一把行锁上串行化，杜绝双交双成功。"""
    vat = db.execute(
        select(Vat).where(Vat.id == vat_id).with_for_update()
    ).scalar_one_or_none()
    if vat is None:
        raise VatNotFoundError(f"vat {vat_id} not found")
    return vat


def cycle_used_meters(db: Session, vat: Vat) -> Decimal:
    """本还原周期（同缸同 cycleId）已入库的累计布米。

    窗口与「按缸列出的浸染合计」同源：展开区按缸列出的本周期批次求和即此值，
    不掺时间启发式，差恒为 0。
    """
    total = db.execute(
        select(func.coalesce(func.sum(DipLot.clothMeters), 0)).where(
            DipLot.vat_id == vat.id,
            DipLot.cycleId == vat.cycleSeq,
        )
    ).scalar_one()
    return Decimal(total)


def save_dip_lot(
    db: Session,
    *,
    vat_id: int,
    dipped_at: datetime,
    cloth_meters,
    redox_mv,
) -> DipLot:
    """登记一笔浸染（展开区表单与直打接口共用的唯一保存链）。

    - 单笔布米必须大于 0；
    - 闲置缸禁止新浸染；
    - 本周期累计布米 + 本笔不得超过缸容 × 8%；
    - 先取缸行级写锁再算累计：并发双交时后到者看到先到者已入库的批次，
      超限即整体回滚，不留残行、不做乐观插入。
    """
    meters = parse_meters(cloth_meters)
    redox = None
    if redox_mv is not None and str(redox_mv).strip() != "":
        try:
            redox = Decimal(str(redox_mv).strip())
        except (InvalidOperation, AttributeError, ValueError):
            redox = None
        else:
            if not redox.is_finite():
                redox = None
            else:
                try:
                    redox = redox.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                except InvalidOperation:
                    redox = None
    if not isinstance(dipped_at, datetime):
        raise VatRuleError(ERR_LOT_TIME)

    try:
        vat = lock_vat(db, vat_id)
        if vat.status == Vat.STATUS_IDLE:
            raise VatRuleError(ERR_VAT_IDLE)
        used = cycle_used_meters(db, vat)
        cap = cloth_cap(vat.volumeL)
        if used + meters > cap:
            raise VatRuleError(ERR_CAP_EXCEEDED)
        lot = DipLot(
            vat_id=vat.id,
            cycleId=vat.cycleSeq,
            dippedAt=dipped_at,
            clothMeters=meters,
            redoxMv=redox,
        )
        db.add(lot)
        db.commit()
    except VatRuleError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    return lot


def advance_cycle_on_leave_idle(db: Session, vat: Vat, new_status: str) -> None:
    """离开闲置（idle -> 非idle）即开启新还原周期，周期序号 +1。

    在已持有的缸行锁内调用；进入闲置（->idle）只是结束当前周期，
    再出来时累计窗口重新从 0 起算。
    """
    if vat.status == Vat.STATUS_IDLE and new_status != Vat.STATUS_IDLE:
        vat.cycleSeq += 1


def assert_can_mark_ready(latest: Optional[DipLot]) -> None:
    """不能将染缸标为 ready，除非最新浸染批次 redoxMv 已填且 <= -500。"""
    if latest is None or latest.redoxMv is None or Decimal(latest.redoxMv) > Decimal("-500"):
        raise VatRuleError(
            "无法设为可染色：最新浸染批次的氧化还原电位为空或高于 -500 mV。"
        )


def validate_vat_status_change(vat: Vat, new_status: str, latest: Optional[DipLot]) -> None:
    if new_status == Vat.STATUS_READY:
        assert_can_mark_ready(latest)
