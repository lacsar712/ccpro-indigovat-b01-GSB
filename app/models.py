from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)


class Workshop(Base):
    __tablename__ = "workshops"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(80))
    notes: Mapped[str] = mapped_column(Text, default="")

    vats: Mapped[list["Vat"]] = relationship(back_populates="workshop")


class Vat(Base):
    __tablename__ = "vats"
    __table_args__ = (
        UniqueConstraint("workshop_id", "code", name="uniq_vat_code_per_workshop"),
    )

    STATUS_IDLE = "idle"
    STATUS_REDUCING = "reducing"
    STATUS_READY = "ready"

    id: Mapped[int] = mapped_column(primary_key=True)
    workshop_id: Mapped[int] = mapped_column(ForeignKey("workshops.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(40))
    dyeType: Mapped[str] = mapped_column(String(80))
    volumeL: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(20), default=STATUS_IDLE)
    # 还原周期序号：每次 idle -> 非 idle（离开闲置）+1。
    # 浸染累计窗口 = 该缸当前周期序号下的全部批次；闲置缸无进行中的周期。
    cycleSeq: Mapped[int] = mapped_column(default=1, server_default="1")

    workshop: Mapped["Workshop"] = relationship(back_populates="vats")
    lots: Mapped[list["DipLot"]] = relationship(back_populates="vat")

    def latest_lot(self) -> Optional["DipLot"]:
        if not self.lots:
            return None
        return sorted(self.lots, key=lambda x: (x.dippedAt, x.id), reverse=True)[0]


class DipLot(Base):
    __tablename__ = "dip_lots"
    __table_args__ = (
        Index("ix_dip_lots_vat_cycle", "vat_id", "cycleId"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vat_id: Mapped[int] = mapped_column(ForeignKey("vats.id", ondelete="CASCADE"))
    # 登记时所属还原周期（= Vat.cycleSeq）：累计窗口 = 同缸同周期的全部批次，
    # 因此「周期累计」与「按缸列出的浸染合计」永远同一口径，差为 0。
    cycleId: Mapped[int] = mapped_column(default=1, server_default="1")
    dippedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    clothMeters: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    redoxMv: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)

    vat: Mapped["Vat"] = relationship(back_populates="lots")
