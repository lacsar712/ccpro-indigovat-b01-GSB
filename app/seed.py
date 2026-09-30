import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import DipLot, User, Vat, Workshop

_PWD_SALT = os.environ.get("PWD_SALT", "indigovat-dev-salt").encode("utf-8")


def hash_password(password: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), _PWD_SALT, 120000
    )
    return digest.hex()


def verify_password(plain: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(plain), hashed)


def ensure_seed_data(db: Session) -> None:
    """幂等种子：账号 + 蓝靛湾/清水江样例缸位与电位序列。"""
    if not db.query(User).filter_by(username="admin").first():
        db.add(
            User(
                username="admin",
                password_hash=hash_password("123456"),
                is_superuser=True,
            )
        )
    if not db.query(User).filter_by(username="worker").first():
        db.add(
            User(
                username="worker",
                password_hash=hash_password("123456"),
                is_superuser=False,
            )
        )
    db.commit()

    if db.query(Workshop).first():
        return

    w1 = Workshop(name="蓝靛湾一号坊", region="黔东南", notes="晨露还原较快")
    w2 = Workshop(name="清水江二号坊", region="黔南", notes="缸体较深，保温好")
    db.add_all([w1, w2])
    db.flush()

    v1 = Vat(
        workshop_id=w1.id,
        code="V-01",
        dyeType="土靛",
        volumeL=Decimal("800.00"),
        status=Vat.STATUS_REDUCING,
        cycleSeq=2,  # 已离开闲置一次；当前周期 2 接近 8% 上限（64 m）
    )
    v2 = Vat(
        workshop_id=w1.id,
        code="V-02",
        dyeType="合成靛",
        volumeL=Decimal("600.00"),
        status=Vat.STATUS_IDLE,
        cycleSeq=2,  # 上一周期已结束、当前闲置，禁止新浸染
    )
    v3 = Vat(
        workshop_id=w2.id,
        code="V-11",
        dyeType="土靛",
        volumeL=Decimal("900.00"),
        status=Vat.STATUS_REDUCING,
        cycleSeq=2,
    )
    v4 = Vat(
        workshop_id=w2.id,
        code="V-12",
        dyeType="板蓝根靛",
        volumeL=Decimal("750.00"),
        status=Vat.STATUS_READY,
        cycleSeq=2,
    )
    db.add_all([v1, v2, v3, v4])
    db.flush()

    now = datetime.now(timezone.utc)

    def lots(vat_id: int, series, cycle_id: int = 1):
        """series: (hours_ago, meters, redox or None)；cycle_id 指定所属还原周期。"""
        rows = []
        for hours, meters, redox in series:
            rows.append(
                DipLot(
                    vat_id=vat_id,
                    cycleId=cycle_id,
                    dippedAt=now - timedelta(hours=hours),
                    clothMeters=Decimal(meters),
                    redoxMv=Decimal(redox) if redox is not None else None,
                )
            )
        return rows

    # V-01：上周期电位爬坡序列（cycle 1，不计入当前 8% 窗口）
    db.add_all(
        lots(
            v1.id,
            [
                (60, "18.00", "-410.00"),
                (52, "22.50", "-455.00"),
                (44, "30.00", "-490.00"),
            ],
            cycle_id=1,
        )
    )
    # V-01 当前周期累计 60.00 m / 上限 64.00 m（800 L × 8%）：
    # 各交一笔 3 m 时 60+3=63 可入、再 +3=66 超限，恰用于双交至多一成
    db.add_all(
        lots(
            v1.id,
            [
                (6, "30.00", "-505.00"),
                (2, "30.00", "-520.00"),
            ],
            cycle_id=2,
        )
    )
    # V-02 闲置：只剩上周期批次，本周期窗口为空且禁止登记
    db.add_all(
        lots(
            v2.id,
            [
                (30, "8.00", None),
                (25, "12.00", None),
            ],
            cycle_id=1,
        )
    )
    # V-11：上周期序列（cycle 1），当前周期 42.00 / 72.00 m
    db.add_all(
        lots(
            v3.id,
            [
                (72, "25.00", "-390.00"),
                (62, "35.00", "-430.00"),
                (54, "48.00", "-460.00"),
                (46, "60.00", "-480.00"),
            ],
            cycle_id=1,
        )
    )
    db.add_all(
        lots(
            v3.id,
            [
                (8, "22.00", "-495.00"),
                (3, "20.00", "-515.00"),
            ],
            cycle_id=2,
        )
    )
    # V-12 可染色：上周期序列（cycle 1），当前周期 38.50 / 60.00 m，
    # 最新批次电位 -530 mV，满足 ready 校验
    db.add_all(
        lots(
            v4.id,
            [
                (72, "20.00", "-420.00"),
                (56, "28.00", "-470.00"),
            ],
            cycle_id=1,
        )
    )
    db.add_all(
        lots(
            v4.id,
            [
                (9, "20.00", "-505.00"),
                (2, "18.50", "-530.00"),
            ],
            cycle_id=2,
        )
    )
    db.commit()
