import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def _database_url() -> str:
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "6120")
    name = os.environ.get("POSTGRES_DB", "indigovat")
    user = os.environ.get("POSTGRES_USER", "indigovat")
    password = os.environ.get("POSTGRES_PASSWORD", "indigovat")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


DATABASE_URL = os.environ.get("DATABASE_URL") or _database_url()

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def ensure_schema() -> None:
    """create_all 之外的幂等轻量迁移：补还原周期标记列并回填旧库。

    - vats.cycleStartedAt：本还原周期（上次离开闲置）起点，闲置为 NULL；
    - dip_lots.createdAt：入库时刻。旧行没有该值，用 dippedAt 回填，
      非闲置缸的周期起点回填为其最早批次时刻，保证累计窗口与既有行对得上。
    """
    inspector = inspect(engine)
    vat_cols = {c["name"] for c in inspector.get_columns("vats")}
    lot_cols = {c["name"] for c in inspector.get_columns("dip_lots")}
    with engine.begin() as conn:
        if "createdAt" not in lot_cols:
            conn.execute(
                text('ALTER TABLE dip_lots ADD COLUMN "createdAt" timestamp with time zone')
            )
            conn.execute(text('UPDATE dip_lots SET "createdAt" = "dippedAt" WHERE "createdAt" IS NULL'))
        if "cycleStartedAt" not in vat_cols:
            conn.execute(
                text('ALTER TABLE vats ADD COLUMN "cycleStartedAt" timestamp with time zone')
            )
            conn.execute(
                text(
                    'UPDATE vats SET "cycleStartedAt" = COALESCE(('
                    'SELECT MIN(l."dippedAt") FROM dip_lots l WHERE l.vat_id = vats.id'
                    "), now()) WHERE status <> 'idle'"
                )
            )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
