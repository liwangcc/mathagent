# MathAgent Showcase

独立的 MathAgent 产品展示站，不依赖现有 `frontend/` 业务应用。

## 本地开发

```powershell
cd D:\MathAgent\showcase
npm install
npm run dev -- --host 127.0.0.1 --port 4174
```

## 配置

复制 `.env.example` 为 `.env.local` 后按部署环境配置：

- `VITE_WEB_APP_URL`：网页版入口，默认 `https://mathagent.com.cn`
- `VITE_DOWNLOAD_URL`：可选的 Windows 安装包地址；未设置时使用站内 `/downloads/MathAgent_0.1.10_x64-setup.exe`
- `VITE_BETA_DOWNLOAD_URL`：可选的未签名内测包地址；页面会显示未知发布者、SmartScreen 和 SHA-256 提示。正式签名版发布后应移除

安装包由 Nginx/CDN 从 `/downloads/` 独立提供，不复制进源码仓库；发布新版本时同步更新页面默认文件名和校验值。

当前 0.1.10 安装包尚未数字签名，下载页明确提示未知发布者及设备安全策略限制，不应对外宣称为数字签名版。

## 设计与动画

- 视觉沿用教师工作台的“纸与苔”令牌（`frontend/src/styles/global.css`）：暖纸底、墨色衬线标题、苔绿强调，支持浅色 / 深色 / 跟随系统与宋 · 青 · 黛三种配色。
- 不加载第三方字体或视频；首屏批阅演示与“夜晚”插画均为页面内的 SVG / CSS 动画，可暂停，并在“减少动态效果”设置下显示静态终态。
- “夜晚”插画与“墨迹星图”随滚动推进，固定在视口后也会自己播放（页面按固定速度滚过该段，`src/lib/scrollPlayer.ts`）：滚轮、触摸、方向键或拖动滚动条随时接管，前后都能拨动；停手约 1.2 秒后从当前位置继续，到段尾停下、不越过本段。两段各有“暂停”按钮；“减少动态效果”下不自动播放。
- “墨迹星图”（`InkAtlas`）用 WebGL 实时绘制 30,000 个墨点：一份作答从手写、转录、判定，到全班错题星图与按题目指纹归并。知识点名取自 `references/math_curricula/senior.v1.json`，错题分布为示意数据；离开视口或页面隐藏时停止，“减少动态效果”下只显示各段终态，不支持 WebGL 时退回纯文字。
- 页面中的学生、成绩与统计均为脱敏示意数据；`tests/` 中的契约测试锁定关键文案与数据自洽。

## 构建

```powershell
npm run typecheck
npm run build
```

构建输出位于 `showcase/dist/`，可部署到 `www.mathagent.com.cn`。
