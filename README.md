# 关键词接口监控爬虫

定时请求多个招标公告 JSON 接口，当标题命中自定义关键词时，自动推送到企业微信群机器人。

当前内置数据源：
- **国家管网**：免登录接口 `pageEs`，按当天发布时间范围抓取 `data.list`，匹配 `businessName`。
- **万项网-推荐招标**（tid=23）、**万项网-招标公告**（tid=2）、**万项网-国际招标**（tid=12）：抓取 `result.list`，匹配 `title`。

## 功能
- 支持多数据源，统一关键词、统一去重、统一推送
- 国家管网每次请求自动设置当天 `releaseStartTime` 和 `releaseEndTime`
- 万项网三个接口 `pageSize=50`，已推送过的公告不再重复推送
- 推送内容包含：关键词、项目名称、类型、发布时间、推送时间
- 按 `id` 去重，已推送内容记录在 `seen.json`
- 支持本地定时运行和 GitHub Actions 托管运行

## 安装
```powershell
pip install -r requirements.txt
```

## 配置
编辑 `config.yaml`：

1. **`keywords`**：需要监控的关键词（全局，所有数据源共用）。
2. **`wechat_webhook`**：企业微信群机器人地址；部署到 GitHub Actions 时建议放到 Secret `BID_WEBHOOK`。
3. **`interval_minutes`**：本地运行时的检查间隔。
4. **`sources`**：数据源列表。每个数据源可单独配置 `api_url`、`method`、`params`/`body`、`list_path`、`match_fields`、`title_field`、`type_label`、`start_time_field`、`time_format` 等，未配置项会继承全局值。

新增数据源时，只需在 `sources` 下追加一项即可，无需改动代码。

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
