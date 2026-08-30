# Frontend Architecture

前端位于 `apps/web/`，使用 React + TypeScript + Vite，提供上传、目标输入、任务状态、澄清、计划确认、结果查看、异常复核和产物下载。

## 技术栈

- React
- TypeScript
- Vite
- React Router
- 普通 CSS
- 轻量 fetch API client

当前不引入重型 UI 框架，也不引入复杂 monorepo 工具链。

## 目录

```text
apps/web/src/
  main.tsx
  App.tsx
  api/
    client.ts
    jobs.ts
    types.ts
  routes/
    UploadPage.tsx
    DiscoveryPage.tsx
    PlanReviewPage.tsx
    PlanConfirmationPage.tsx
    ClarificationPage.tsx
    JobDetailPage.tsx
    ResultDashboardPage.tsx
    ExceptionReviewPage.tsx
    SettingsPage.tsx
  components/
    layout/
    upload/
    job/
    report/
    table/
    common/
  hooks/
  styles/
```

## 页面职责

| 页面 | 职责 |
| --- | --- |
| UploadPage | 上传多文件、输入 goal、选择 discover/plan/answer、提交任务 |
| DiscoveryPage | 展示文件、表、key 候选和初步质量问题 |
| PlanReviewPage | 用业务语言展示主表、预计行数/字段影响、处理步骤、交付内容、规则和风险 |
| JobDetailPage | 作为任务总览，展示目标、状态行动区、处理阶段、关键结果和任务内入口 |
| ResultDashboardPage | 按结论、可用性、指标、结果预览和产物说明组织；技术依据默认折叠 |
| ExceptionReviewPage | 展示复核进度、状态筛选、真实影响字段和值，并保存三态业务判断 |
| ClarificationPage | 展示受控多轮澄清问题并恢复任务 |
| PlanConfirmationPage | 展示目标理解、业务步骤、交付内容，以及高移除比例/无法估算/模糊匹配等确认原因 |
| SettingsPage | 展示服务状态、数据保护等级、上传限制和结果交付方式 |

## API 联调

开发环境默认通过 Vite proxy 访问后端：

```text
/api -> http://127.0.0.1:8000
```

可通过 `.env` 修改：

```text
VITE_API_BASE_URL=http://127.0.0.1:8000
```

## 当前边界

- 上传任务、状态轮询、发现、计划、业务结果和复核均使用真实 Job API。
- 任务页面统一使用 `/jobs/:jobId/...`，URL 中的 `jobId` 是页面数据的唯一主键；
  `GET /api/v1/jobs` 是任务切换真源，浏览器最近任务缓存只用于快捷恢复。
- 桌面侧栏持续展示任务总览、数据概览、处理方案、处理结果和异常复核；
  移动端转换为横向任务标签，不把任务入口藏在内容卡片中。
- 最近任务已被删除或超出 TTL 时自动清理缓存，并明确展示“任务已不存在”及新建入口。
- 发现、计划或复核尚未生成时，后端返回 404，页面展示“生成中”，不把空对象解释成无问题。
- 每个资源 Hook 与当前 URL 的 `jobId` 绑定；任务切换时立即清空旧资源，并忽略过期响应。
- 澄清与计划确认支持暂停、刷新后恢复和继续执行。确认页保存 GET 返回的
  `plan_id + plan_hash` 并随 POST 原样回传；过期页面收到 409 后不会启动任务。
- 计划确认页直接翻译 ExecutionStep 的 `confirmation_reasons`，不在前端重新计算
  风险阈值；页面展示的是后端已经写入快照的判定结果。
- 深链接会从 URL 恢复当前任务上下文；浏览器同时保存最近一个任务入口。
- 上传区支持鼠标、拖放和键盘触发，并可逐个移除已选文件。
- TaskSession 保存 ConversationTurn；前端只展示与当前数据任务有关的受控澄清，不提供无限聊天窗口。
- 复核清单支持分页加载和逐条自动保存；同一记录的快速连续操作按顺序提交。
- 复核不会覆盖原始结果；每次应用判断都生成新版本。结果页可选择历史版本、撤销到父
  版本或从任意版本创建分支，也可把成功任务保存为 Recipe。
- 生产任务历史、Recipe、语义模型和版本图由 PostgreSQL 持久化并按租户隔离；本地开发
  保留文件后端。直接编辑单元格修正值仍属于后续能力。
- 新建任务可选择租户内 Recipe 或业务语义模型；两者互斥，Recipe 会锁定已验收目标。
