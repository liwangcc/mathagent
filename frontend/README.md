# MathAgent 前端

Vue 3 + Vite + Element Plus 前端，对接仓库根目录的 FastAPI 后端。

## 启动

```bash
cd frontend
npm install
npm run dev
```

默认地址为 `http://localhost:5173`；开发服务器将 `/api`、`/uploads` 代理到 `http://127.0.0.1:8080`。

## 常用命令

```bash
npm run test
npm run build
npm run preview
```

## 目录

- `src/main.js`：入口与全局组件注册
- `src/router/`：路由与登录守卫
- `src/stores/auth.js`：登录状态
- `src/api/index.js`：带认证的 HTTP 客户端
- `src/views/`：登录、批阅、模板、学生、报告、题库、统计、课件等页面
- `src/components/`：共享组件

`tests/*.browser.mjs` 是不在 `npm run test` 中的浏览器测试，运行方式见项目内部文档《项目总览》第 9 节（该文档不随开源仓库发布）。

前端只负责展示和提交用户操作；权限、归属、合同和成绩计算以服务端为准。真实登录、上传、SSE 和权限流程需要在浏览器中另行验收。
