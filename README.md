# 团队管理看板

这是一个可直接发布到 GitHub Pages 的静态站点。

## 发布

1. 在 GitHub 新建仓库后，在此目录执行 `git init`、提交并推送到 `main` 分支。
2. 在仓库 **Settings → Pages** 中选择：
   - Branch：`main`
   - Folder：`/(root)`
3. GitHub Pages 的入口是根目录 `index.html`，它会以相对路径打开 `outputs/团队扩编与筛选体系框架-可视化编辑版.html`。

## 数据说明

- `outputs/lark-dashboard-data.js` 是当前看板使用的只读数据快照。
- `work/` 和本地 Lark 刷新工具不会被提交。
- GitHub Pages 通常公开访问。发布前请确认客户联系方式、跟进记录及业绩快照适合公开给拥有页面链接的人查看。

## X5 Gross 自动刷新

`.github/workflows/refresh-x5-dashboard.yml` 每小时运行一次，也可以在 GitHub 的 **Actions → Refresh X5 dashboard data → Run workflow** 手动运行。它会先使用现有 X5 CRM 统计逻辑同步到 Lark 的 `X5日度Gross`，在 Lark 读回成功后才更新并推送 `outputs/lark-dashboard-data.js`。任何一步失败都不会提交新的线上数据。

在仓库 **Settings → Secrets and variables → Actions** 中设置以下 Secrets：

- `CRM_USER`
- `CRM_PASS`
- `CRM_TOTP`
- `LARK_APP_ID`
- `LARK_APP_SECRET`
- `LARK_APP_TOKEN`

这些值只在 Actions 运行时使用，不写入仓库或网页文件。
