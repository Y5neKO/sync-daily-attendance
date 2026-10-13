# SYNC PROJECT（同调计划）每日自动签到

参考 [skland-daily-attendance](https://github.com/Y5neKO/skland-daily-attendance) 的机制实现：
GitHub Actions 定时任务 + Secrets 凭证注入 + 失败通知 + 多账号。

每天 **北京时间 09:00 / 21:00** 自动执行：
拉取全部好友 → 逐个打招呼（`POST /api/friends/ping`，每次 +10 经验）。

## 原理

- 签名算法（客户端硬编码）：`authorization: {MD5(微秒时间戳 + 盐)},{微秒}`，盐为 App 内置字符串
- 身份凭证：`cookie: Zwin-ArkRadar=<UUID>`（登录会话）
- 每次打招呼对应服务端 `ACT_PING` 经验事件（每日有配额，配额内无冷却）

## 部署（GitHub Actions）

1. Fork / 推送本仓库到你的 GitHub
2. `Settings → Secrets and variables → Actions → New repository secret`：

   | Secret | 说明 | 必填 |
   |--------|------|------|
   | `SYNC_COOKIES` | 会话 cookie（UUID），多账号用英文逗号分隔 | ✅ |
   | `SYNC_FEISHU_WEBHOOK` | 飞书自定义机器人 webhook（`open.feishu.cn/open-apis/bot/v2/hook/xxx`） | 可选 |
   | `SYNC_FEISHU_APP_ID` / `SYNC_FEISHU_APP_SECRET` | 飞书自建应用凭证，配置后推送改为**图片报表**模式（需应用有 `im:resource` 权限并发布） | 可选 |
   | `SYNC_MAX_RETRIES` | 重试次数，默认 3 | 可选 |
   | `SYNC_DELAY` | 打招呼间隔秒数，默认 1.5 | 可选 |

3. `Actions` 页面手动 Run workflow 验证一次
4. 之后每天 09:00 / 21:00 自动执行（GitHub cron 可能有 5~30 分钟延迟）

## 会话 cookie 获取方式

1. 手机/模拟器打开 App 登录
2. 抓包任一请求的 `cookie: Zwin-ArkRadar=<UUID>` 请求头，取 UUID 部分
3. 填入 `SYNC_COOKIES`

> 会话有效期由服务端控制（厂商可强制下线）。失效时脚本会通过飞书 webhook 通知，
> 按上面步骤重新抓取更新 Secret 即可。

## 本地运行

```bash
export SYNC_COOKIES="<UUID>"
export SYNC_FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/<xxx>"  # 可选
python3 checkin.py
```

## 定时（本机 launchd 示例，替代 GitHub Actions）

```xml
<!-- ~/Library/LaunchAgents/cn.sync.checkin.plist -->
<plist version="1.0"><dict>
  <key>Label</key><string>cn.sync.checkin</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>/path/to/checkin.py</string></array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>SYNC_COOKIES</key><string>&lt;UUID&gt;</string></dict>
</dict></plist>
```

`launchctl load ~/Library/LaunchAgents/cn.sync.checkin.plist`

## 文件

```
checkin.py                    # 主脚本（多账号/重试/通知）
.github/workflows/checkin.yml # GitHub Actions 定时（UTC 01:00 / 13:00）
```
