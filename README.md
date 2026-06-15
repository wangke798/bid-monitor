# 关键词接口监控爬虫

定时请求国家管网免登录 JSON 接口，按当天发布时间范围抓取 `data.list`，当 `businessName` 命中自定义关键词时，自动推送到企业微信群机器人。

## 功能
- 使用免登录接口 `pageEs`，无需 Cookie 或 Authorization
- 每次请求自动设置当天 `releaseStartTime` 和 `releaseEndTime`
- 仅匹配 `businessName` 字段中的关键词
- 推送 `businessName`、`releaseTime`、`bidEndTime`、`tendererName`
- 按 `id` 去重，已推送内容记录在 `seen.json`
- 支持本地定时运行和 GitHub Actions 托管运行

## 安装
```powershell
pip install -r requirements.txt
```

## 配置
编辑 `config.yaml`：

1. **`keywords`**：需要监控的关键词，只会在 `businessName` 中搜索。
2. **`wechat_webhook`**：企业微信群机器人地址；部署到 GitHub Actions 时建议放到 Secret `BID_WEBHOOK`。
3. **`interval_minutes`**：本地运行时的检查间隔。
4. **`use_today_release_range`**：保持 `true`，程序会自动只抓当天发布内容。

当前接口不需要登录令牌，不需要配置 Cookie、Authorization 或 `BID_AUTHORIZATION`。

## 运行
```powershell
python main.py
```

启动后会立即检查一次，之后按设定间隔轮询。`Ctrl+C` 停止。

## GitHub Actions
仓库中已包含 `.github/workflows/monitor.yml`，每小时运行一次。只需要在仓库 Secrets 中配置：

| Name | Secret（值） |
|------|------|
| `BID_WEBHOOK` | 企业微信群机器人完整 Webhook 地址 |

运行后如有新增命中，会自动更新并提交 `seen.json` 用于跨次去重。
