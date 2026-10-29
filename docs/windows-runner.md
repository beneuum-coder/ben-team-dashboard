# 在常开 Windows 上运行 X5 刷新

适用：一台 24 小时不关机、出口 IP 固定的 Windows，用它替代 GitHub runner 执行
`抓 CRM → 写 Lark → 生成快照 → 推送发布`。

动机有三个，按重要性排序：

1. **固定出口 IP**：可以找 CRM 供应商做白名单，这是唯一能根治 Cloudflare 拦截的办法；
2. **定时准点**：GitHub 的 cron 实测迟到 3–21 小时，任务计划程序不会；
3. **会话可控**：有桌面浏览器，能人工过一次挑战，然后复用会话。

---

## 1. 一次性准备

### 1.1 软件

| 组件 | 要求 |
| --- | --- |
| Python | 3.11 或以上，安装时勾选 **Add python.exe to PATH** |
| Git for Windows | 需要 `git` 命令；推送走 HTTPS |
| 仓库 | 克隆到固定路径，例如 `C:\x5\ben-team-dashboard` |

```bat
cd C:\x5
git clone https://github.com/beneuum-coder/ben-team-dashboard.git
cd ben-team-dashboard
python -m pip install requests pyotp
```

### 1.2 凭证

把 `refresh.env.example` 复制成 `refresh.env`（与 `refresh.bat` 同目录），填入 8 个值：

```
CRM_USER / CRM_PASS / CRM_TOTP
CX_USER / CX_PASS
LARK_APP_ID / LARK_APP_SECRET / LARK_APP_TOKEN
```

`refresh.env` 已在 `.gitignore` 里，不会被提交。**不要把内容贴进任何聊天或文档。**

### 1.3 会话 Cookie（很可能需要）

CRM 登录页现在有 Cloudflare 人机挑战，脚本用密码直连大概率过不去。做法是人工登录一次、把会话交给脚本：

1. 在这台 Windows 的 Chrome 里登录 `https://admin.ultimarkets.com/`；
2. F12 → Application → Cookies → `https://admin.ultimarkets.com`；
3. 取 `JSESSIONID` 和 `jsId` 两个值，拼成一行：

```
JSESSIONID=xxxxx; jsId=yyyyy
```

4. 存到 `C:\x5\crm.cookie`（UTF-8、无换行问题），并在 `refresh.env` 里加：

```
CRM_COOKIE_FILE=C:\x5\crm.cookie
```

脚本会优先用这个会话；文件不存在时才走密码登录。会话过期后（症状：日志出现
`session_timeout` 或 `CRM Cloudflare security challenge`）重新执行 1.3 即可。

### 1.4 推送权限

最后一歩要 `git push` 到 `main`，需要一个能写仓库的 Personal Access Token：

- GitHub → Settings → Developer settings → Tokens → 生成，scope 至少 `repo`；
- 第一次 push 时按提示输入用户名和 token（Windows 凭据管理器会记住）；
- 或直接把远程地址改成带 token 的形式，但那样 token 会留在 `.git/config` 里，不推荐。

```bat
git config user.name  "X5 Dashboard Bot"
git config user.email "bot@your-domain"
```

---

## 2. 先手动跑一次

```bat
cd C:\x5\ben-team-dashboard
python scripts\run_refresh_all.py --attempts 2 --env-file refresh.env
```

它会依次执行四步（与 GitHub 工作流完全一致），然后提交并推送：

| 步骤 | 脚本 |
| --- | --- |
| 1 | `sync_x5_daily_gross.py --mode current --apply` |
| 2 | `sync_x5_channel_monthly.py --apply` |
| 3 | `export_dashboard_lark_data.py` |
| 4 | `publish_x5_dashboard_data.py` |
| 5 | `git commit` + `git push origin HEAD:main` |

通过标准：

- 命令退出码 0；
- `logs\refresh-*.log` 里四步都有输出，没有 `Traceback`；
- 日志里出现 `phase=Lark snapshot write skipped` 或 `create/update/unchanged` 计数；
- 页面 `rev=` 变了：`curl` 或直接刷新 `https://beneuum-coder.github.io/ben-team-dashboard/`；
- GitHub 上出现新的 bot 提交。

`--no-push` 可以只跑不发布，用来试跑。

---

## 3. 挂到任务计划程序

命令行方式（每 4 小时一次，从创建时刻起算）：

```bat
schtasks /Create /TN "X5 Dashboard Refresh" /TR "C:\x5\ben-team-dashboard\refresh.bat" /SC HOURLY /MO 4 /RL HIGHEST /F
```

图形界面方式（推荐，能勾上补跑选项）：

1. 任务计划程序 → 创建任务；
2. **常规**：勾选“不管用户是否登录都要运行”、“使用最高权限运行”；
3. **触发器**：每天，重复任务间隔 4 小时，持续时间“无限期”；勾选
   **“如果错过计划开始时间，请尽快启动任务”**；
4. **操作**：启动程序 `C:\x5\ben-team-dashboard\refresh.bat`，起始于
   `C:\x5\ben-team-dashboard`；
5. **条件**：取消“只有在计算机使用交流电源时才启动此任务”；
6. **设置**：勾选“如果任务失败，按以下频率重新启动”，间隔 30 分钟，尝试 3 次。

另外把电源计划设成**从不睡眠**，否则任务会静默不跑。

`refresh.bat` 会把每次的退出码追加到 `logs\scheduler.log`，便于事后核对。

---

## 4. 与 GitHub 定时二选一（重要）

切到本机后，必须停掉 GitHub 侧的 4 小时定时，否则两边都写 Lark、互相覆盖：

- 编辑 `.github/workflows/refresh-x5-dashboard-retry.yml`，注释掉 `schedule:` 段，
  保留 `workflow_dispatch`（需要时仍可手动触发）；
- 或者直接 `gh workflow disable "Refresh X5 dashboard (retry)"`。

保留 `refresh-x5-dashboard.yml` 不动——它只是被调用的那半边，没有定时。

---

## 4.5 并发：锁与公共状态文件

`run_refresh_all.py` 现在一次只允许跑一轮，两个窗口不会同时写 Lark / push：

- 锁文件是 `work\.refresh.lock`（`work/` 已被 gitignore），内容是
  `{"pid", "started", "phase", "log"}`。第二个实例启动会立刻退出，并打印
  持有者的 pid、阶段和日志名。
- 判定标准是**持有进程是否还活着**，不是文件在不在：进程被杀掉留下的锁，
  下一次运行会自动接管，不会永久卡死。
- 半写或损坏的锁在 2 分钟内不会被抢占（躲开写入瞬间），超过 2 分钟才当陈旧锁处理。
- 解锁的正确姿势：确认锁里的 pid 已经不在，再删 `work\.refresh.lock`。

每次运行还会维护仓库上一级的 `REFRESH-STATUS.md`（本机即 `C:\x5\REFRESH-STATUS.md`）：

- 顶部“当前”区域：`running` / `idle`、pid、阶段、日志文件；
- 下面“运行记录”表：开始、结束、结果、提交、数据时间，一轮一行，只追加。

两个窗口开工前先读这个文件；状态是 `running` 就别起第二轮。想换位置可以设
环境变量 `X5_REFRESH_STATUS`。

---

## 5. 排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `CRM Cloudflare security challenge` | 密码登录被挑战 | 配 `CRM_COOKIE_FILE`（1.3），或找 CRM 加白名单 |
| `CRM read retry ... reason=ReadTimeout` | CRM 接口慢 | 脚本已带 3 次退避；持续出现要查 CRM 侧负载 |
| `session_timeout` | 会话过期 | 重做 1.3 |
| push 失败 `403`/`Authentication failed` | PAT 过期或无权限 | 重新生成 token |
| 日志里 `unchanged: N` 且没有 `write` | 数据没变，正常 | 无需处理 |
| 任务计划显示上次结果 `0x1` | 某一步失败 | 看 `logs\refresh-*.log` 的 `=== ... failed ===` 段落 |

想要失败通知，可以在 `refresh.bat` 末尾根据 `%ERRORLEVEL%` 调用企业微信机器人或发邮件；
这是 GitHub 侧原本自带的告警，本机需要自己补。

---

## 6. 已知差异

- **没有 Actions 的日志界面**：只有 `logs\` 下的文本；
- **提交作者**是你配置的 `user.name`，不再是 `github-actions[bot]`；
- **Windows Update 重启会中断**：建议设置活动时间，或配置自动登录 + “错过补跑”；
- **本机是单点**：机器挂了就没人刷新，GitHub 侧那套至少是托管环境。
