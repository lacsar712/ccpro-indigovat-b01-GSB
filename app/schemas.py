from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class WorkshopIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    region: str = Field(min_length=1, max_length=80)
    notes: str = ""


class VatIn(BaseModel):
    workshop_id: int
    code: str = Field(min_length=1, max_length=40)
    dyeType: str = Field(min_length=1, max_length=80)
    volumeL: Decimal
    status: str = "idle"


class DipLotIn(BaseModel):
    # vat_id 仅保留兼容；实际以 URL 路径中的缸号为准，防止越缸写入
    vat_id: Optional[int] = None
    dippedAt: datetime
    # 布米合法性（>0、有限数）由服务层 parse_meters 统一裁决，
    # 直打接口不得在入口以 422 提前打发，保证两条路径同一句中文
    clothMeters: Any
    redoxMv: Optional[Any] = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    detail: str
