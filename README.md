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

1. 状态改为 `ready`（可染色）时，最新批次 `redoxMv` 须已填且 ≤ -500（见 `vat_rules.py`）。
2. **浸染布米上限（展开区登记与直打保存共用同一条后端校验链 `save_dip_lot`，不接受只拦前端）**：
   - 单笔布料米数必须大于 0；
   - 闲置（`idle`）缸禁止新浸染，须先改为还原中；
   - 该缸**自上次离开闲置以来**的累计布米，不得超过缸容升数的 **8%**
     （例如 800 L 缸上限 64.00 m；换算即 `缸容L × 8%`）；
   - 累计窗口 = 同缸 `createdAt（入库时刻） ≥ cycleStartedAt（离开闲置时刻）`，
     回到闲置清零、再离开闲置重新起算；该口径与按缸列出的浸染合计完全一致（差为 0）；
   - 超限（含并发双交导致合计超限）时，展开区表单与直打接口
     `POST /api/vats/{id}/lots` 返回同一句中文：
     `本缸本还原周期累计布料已达缸容的 8% 上限，本笔浸染被拒绝。`
   - 保存事务对缸行加行锁（`SELECT … FOR UPDATE`）：两人几乎同时提交时第二笔在锁上等待、
     按最新累计复核，超限即回滚，至多一笔入库，不乐观插入残行。

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
2. **Vat**：归属工坊、`code`、`dyeType`、`volumeL`、状态 `idle|reducing|ready`
3. **DipLot**：归属染缸、`dippedAt`、`clothMeters`、`redoxMv`（可空）

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
