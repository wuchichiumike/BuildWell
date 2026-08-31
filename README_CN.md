# 筑福链

面向建筑施工项目的物料与工人保障协同管理 MVP。业务事实、库存余额和审计记录由 FastAPI 后端管理；前端提供项目工作台；链上部分默认使用明确标注的 Mock 证据适配器。

## 本地运行

```bash
make install
make backend     # http://localhost:8000/docs
make frontend    # http://localhost:5173
```

`make` 必须在项目目录运行。若终端当前不在项目目录，可直接使用：

```bash
make -C "{项目文件夹地址}" dev
```

后端默认使用本地 SQLite 演示库，启动时会加载 `P001` 的可重复演示数据。当前仓储层是 SQLite demo adapter；生产部署需要替换为 PostgreSQL/SQLAlchemy adapter，并将文件存储/证据适配器替换为对应实现。

```bash
make test
make reset-demo   # 仅在 DEMO_MODE=true 下清理演示命名空间
```

API 前缀为 `/api/v1`。演示登录通过 `X-Demo-User` 或 `X-User-Id` 请求头选择固定用户（`owner@example.test`、`procurement@example.test`、`supplier@example.test`、`site@example.test`、`inspector@example.test`、`auditor@example.test`）；服务端会忽略请求中伪造的角色字段。实际部署应接入 OIDC/Bearer 会话。所有写请求都需要 `Idempotency-Key`；确认动作还需要一次性 `confirmation_token`。

前端默认使用业务 API；只有显式设置 `VITE_USE_MOCK=true` 时才读取 `frontend/src/data.ts` 的离线夹具。离线模式会在页面顶部标明且不会执行业务写入。文件下载链接由后端签名并带过期时间，生产环境请设置独立的 `DOCUMENT_SIGNING_SECRET`。

## 演示验收路径

默认 `P001` 有一笔部分发货订单和待确认的验收、交接记录。切换页面右上角的角色后可按真实状态机完成流程：

1. `供应商` 在“采购协同 → 订单与发货”中提交订单剩余发货。
2. `现场管理员` 在同一订单详情中登记到货，并使用系统返回的一次性 token 确认。
3. `验收监理` 登记通过/不通过数量并确认；只有通过量会生成入库流水，不通过量会进入隔离。
4. `项目负责人` 与 `供应商` 分别在“证据追溯”的交接单中确认本方；第二次确认后才生成调拨流水和交接证据事件。

AI 现场助手会按当前账号恢复持久化线程；“清空当前线程”仅清空会话视图，运行、工具调用和审计记录仍可在后端保留。日报中的每个来源条目可跳转到业务记录、库存或计算结果。

## 范围边界

AI 只通过受控工具读取和计算，并生成待确认草稿；正式采购、到货、验收和证据锚定仍由有权限人员确认。数据库保存完整业务记录，证据适配器只提交摘要和 SHA-256；演示环境不会把它伪装成真实 Fabric 网络。
