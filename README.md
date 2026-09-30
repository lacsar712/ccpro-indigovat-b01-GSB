# IndigoVat-01 · 染缸还原台

FastAPI + PostgreSQL + Jinja2：主界面是横向**缸位条**（Alpine 反应式），不是工坊/染缸/批次三表导航。Session Cookie 登录；规则在 `app/services/vat_rules.py`。

## 技术栈

- FastAPI、SQLAlchemy 2、PostgreSQL
- 启动时 `create_all` + 幂等种子（蓝靛湾一号坊 / 清水江二号坊）
- Session Cookie 认证（Starlette SessionMiddleware）
- Jinja2 + Alpine.js + Pico（叠靛蓝水墨自定义样式）
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4720** |
| Postgres | **6120**（容器内 5432） |

数据库账号：`indigovat` / `indigovat` / 库名 `indigovat`

## 快速启动

```bash
cd IndigoVat/IndigoVat-01
docker compose up --build -d
```

浏览器打开：http://localhost:4720

演示账号（登录页已预填）：

- `admin` / `123456`
- `worker` / `123456`

## 交互（信息架构）

1. **染缸还原台**：横滑缸位条，每缸显示状态、最近电位与 redox sparkline
2. **工坊 chip**：仅作缸位筛选，无独立工坊 CRUD 页
3. **点缸展开**：同页内登记浸染批次、改状态、看近几笔；无平行「染缸表 / 批次表」

**业务规则**：
- 状态改为 `ready`（可染色）时，最新批次 `redoxMv` 须已填且 ≤ -500（见 `vat_rules.py`）。
- 浸染登记只有一条服务链 `save_dip_lot`，展开区表单 `POST /bay/vats/{id}/lots` 与直打接口 `POST /api/vats/{id}/lots` 共用：
  - 单笔布米必须 **大于 0**；
  - **闲置缸禁止新浸染**；
  - **缸容 8% 上限**：自上次离开闲置（idle → 非 idle）起的本还原周期内，累计布米 + 本笔不得超过 `缸容(升) × 8%`（米，两位小数）。例如 800 L 缸上限 64.00 m。超限两条路径返回同一句中文：`本还原周期累计布料米数已达缸容的 8% 上限，该笔浸染不予登记。`
- 周期窗口持久化在 `vats.cycleSeq` + `dip_lots.cycleId` 上：每次离开闲置周期序号 +1、累计归零；周期累计与按缸列出的当前周期批次合计同源，差恒为 0。
- 保存时先 `SELECT ... FOR UPDATE` 锁缸行再读累计：并发双交合计会超限时至多一笔入库，另一笔整体回滚，无残行、非乐观插入。

种子中 **V-01（800 L，还原中）当前周期 60.00 / 64.00 m**，是一口接近上限的缸；V-02 为闲置缸。

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
set POSTGRES_HOST=localhost
set POSTGRES_PORT=6120
uvicorn app.main:app --host 0.0.0.0 --port 4720 --reload
```

## 业务模型

1. **Workshop**：`name`、`region`、`notes`（UI 上仅为筛选片）
2. **Vat**：归属工坊、`code`、`dyeType`、`volumeL`、状态 `idle|reducing|ready`、`cycleSeq`（还原周期序号，离开闲置 +1）
3. **DipLot**：归属染缸、`cycleId`（登记时周期序号）、`dippedAt`、`clothMeters`、`redoxMv`（可空）

## 目录结构

```
IndigoVat-01/
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  requirements.txt
  app/
    main.py
    db.py
    models.py
    schemas.py
    auth.py
    seed.py
    routers/
    services/vat_rules.py
    templates/   # base / bay / login
```
