"""染缸业务规则与浸染保存服务。

展开区登记（表单）与直打保存（JSON 接口）都必须走 ``save_dip_lot``，
不得在调用方各自校验、也不得只在前端拦截。
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DipLot, Vat

# 单个还原周期内，累计布米相对缸容（升）的占比上限
CLOTH_RATIO_CAP = Decimal("0.08")

# 超限时展开区提交与直打保存接口共用的同一句中文
CLOTH_OVER_CAP_MESSAGE = "本缸本还原周期累计布料已达缸容的 8% 上限，本笔浸染被拒绝。"
CLOTH_NOT_POSITIVE_MESSAGE = "布料米数必须大于 0。"
VAT_IDLE_MESSAGE = "该缸处于闲置状态，禁止新浸染；请先将缸设为还原中。"
VAT_MISSING_MESSAGE = "染缸不存在。"


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def assert_can_mark_ready(latest: Optional[DipLot]) -> None:
    """不能将染缸标为 ready，除非最新浸染批次 redoxMv 已填且 <= -500。"""
    if latest is None or latest.redoxMv is None or Decimal(latest.redoxMv) > Decimal("-500"):
        raise VatRuleError(
            "无法设为可染色：最新浸染批次的氧化还原电位为空或高于 -500 mV。"
        )


def cloth_cap_liters(volume_liters: Decimal) -> Decimal:
    """缸容 8% 换算成的布米上限（升数 × 8%，按米记账）。"""
    return (Decimal(volume_liters) * CLOTH_RATIO_CAP).quantize(Decimal("0.01"))


def cycle_cloth_total(db: Session, vat: Vat) -> Decimal:
    """本缸自上次离开闲置以来的累计布米。

    口径：同缸 ``createdAt >= vat.cycleStartedAt`` 的全部浸染批次之和。
    按缸列出的浸染合计必须用同一口径，保证两边差为 0。
    """
    if vat.cycleStartedAt is None:
        return Decimal("0.00")
    total = (
        db.query(DipLot)
        .filter(
            DipLot.vat_id == vat.id,
            DipLot.createdAt >= vat.cycleStartedAt,
        )
        .with_entities(DipLot.clothMeters)
        .all()
    )
    amount = sum((row[0] for row in total), Decimal("0"))
    return amount.quantize(Decimal("0.01"))


def apply_vat_status_change(db: Session, vat: Vat, new_status: str) -> None:
    """改缸状态并维护还原周期起点。

    - idle → reducing/ready：开始一个新还原周期，记 cycleStartedAt（此时缸锁在手，
      不会与浸染提交并发交错）；
    - 任意状态 → idle：周期结束，清空 cycleStartedAt；
    - ready 仍受电位规则约束（见 assert_can_mark_ready）。
    """
    old_status = vat.status
    if new_status == Vat.STATUS_READY and old_status != Vat.STATUS_READY:
        latest = db.scalar(
            select(DipLot).where(DipLot.vat_id == vat.id).order_by(
                DipLot.dippedAt.desc(), DipLot.id.desc()
            ).limit(1)
        )
        assert_can_mark_ready(latest)
    if new_status != Vat.STATUS_IDLE and old_status == Vat.STATUS_IDLE:
        vat.cycleStartedAt = datetime.now(timezone.utc)
    if new_status == Vat.STATUS_IDLE and old_status != Vat.STATUS_IDLE:
        vat.cycleStartedAt = None
    vat.status = new_status


def save_dip_lot(
    db: Session,
    vat_id: int,
    dipped_at: datetime,
    cloth_meters: Decimal,
    redox_mv: Optional[Decimal] = None,
) -> DipLot:
    """展开区登记与直打保存共用的唯一入库入口。

    规则：
    1. 单笔布米必须大于 0；
    2. 闲置缸禁止新浸染；
    3. 本还原周期累计布米（含本笔）不得超过缸容升数的 8%。

    并发：整段在单事务内先对缸行加行锁（``SELECT ... FOR UPDATE``）再读累计、
    校验、插入。两人几乎同时往同一接近上限的缸提交时，第二个事务在锁上等待，
    拿到锁后看到第一笔已提交并重新累计——超限即拒绝并回滚，不乐观插入残行，
    故至多一笔入库。

    任何校验失败都抛 VatRuleError，由调用方回滚；本函数自身不 commit，
    提交/回滚由调用方在同一事务边界完成。
    """
    # 行级锁：把同一缸的并发提交串成一队；不同缸互不阻塞
    vat = db.scalar(select(Vat).where(Vat.id == vat_id).with_for_update())
    if vat is None:
        raise VatRuleError(VAT_MISSING_MESSAGE)

    try:
        meters = Decimal(cloth_meters).quantize(Decimal("0.01"))
    except Exception as exc:  # Decimal 自身对非法输入抛 InvalidOperation
        raise VatRuleError(f"布料米数无效：{cloth_meters!r}") from exc
    if meters <= 0:
        raise VatRuleError(CLOTH_NOT_POSITIVE_MESSAGE)

    if vat.status == Vat.STATUS_IDLE:
        raise VatRuleError(VAT_IDLE_MESSAGE)

    cap = cloth_cap_liters(vat.volumeL)
    already = cycle_cloth_total(db, vat)
    if already + meters > cap:
        raise VatRuleError(CLOTH_OVER_CAP_MESSAGE)

    lot = DipLot(
        vat_id=vat.id,
        dippedAt=dipped_at,
        clothMeters=meters,
        redoxMv=redox_mv,
    )
    db.add(lot)
    db.flush()  # 拿到 id/createdAt，同时让插入在锁保护下对后续事务可见
    return lot
