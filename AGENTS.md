# Dashboard V1 固定约束

本仓库的唯一正式产品是 **Dashboard V1**，后续所有 Work 线程和 feature branch 都必须遵守本文件。

## 正式基线

- 仓库：仓库根目录
- GitHub：`beneuum-coder/ben-team-dashboard`
- 原始 Dashboard 起点：`06b22cf` (`Initial static dashboard site`)
- 正式产品基线：`origin/main @ cf6c13f`

## 开发原则

本项目不是重新设计 Dashboard。所有工作必须在既有 V1 的基础上做增量增强。

必须保留既有 V1 的页面视觉、布局、导航、KPI 卡片、Chart.js 图表、客户池、成长曲线、问题日志、CSS 设计系统及既有交互。

CRM、Lark、X5、IB、CPA 和渠道分析优先按以下路径增加：

`数据层增强 → 快照增强 → 原页面模块增量增强`

不得用新页面替代 V1。

## V2 草稿边界

`dashboard-v2-preview.*`、`dashboard-channel-analytics.*` 及相关 V2 草稿仅为实验文件：

- 禁止将 V2 设置为正式入口。
- 禁止用 V2 替换现有 Dashboard。
- 禁止将 V2 作为正式开发主线。
- 未经用户明确批准，不得修改 `index.html` 指向 V2。
- 暂不删除 V2 文件；可复用功能必须提取并移植到 V1，而非由 V1 替换为 V2。

## Git 与验收

`origin/main` 是正式产品基线。专项工作可使用 feature branch 或 worktree，但任何拟合并成果必须：

1. 基于当时的 `origin/main`。
2. 不替换 V1 页面。
3. 通过数据 QA。
4. 通过 V1 页面回归测试。
5. 提供明确最终 diff。
6. 未经用户明确批准，不得 push 或 merge 到 `main`。
