# Web Console

前端产品结构和页面职责以根目录
[README.md](../../README.md) 与
[docs/FRONTEND_ARCHITECTURE.md](../../docs/FRONTEND_ARCHITECTURE.md)
为准。本目录只保留运行说明。

## 环境要求

- Node.js 20.19+ 或 22.12+
- 后端默认运行在 `http://127.0.0.1:8000`

## 本地启动

```bash
cd apps/web
npm install
npm run dev
```

访问 `http://127.0.0.1:5173`。Vite 默认把 `/api` 代理到本地后端。

如需直连其他后端，创建 `.env`：

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000
```

## 生产构建

```bash
npm run build
npm run preview
```

前端全部页面使用真实 Job API，不包含 mock 数据兜底。
