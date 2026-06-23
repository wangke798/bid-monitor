# 关键词接口监控爬虫

定时请求多个招标公告接口（JSON API / HTML 页面），当标题命中自定义关键词时，自动推送到企业微信群机器人。

当前内置数据源：
- **国家管网**：免登录接口 `pageEs`，按当天发布时间范围抓取 `data.list`，匹配 `businessName`。
- **万项网-推荐招标**（tid=23）、**万项网-招标公告**（tid=2）、**万项网-国际招标**（tid=12）：抓取 `result.list`，匹配 `title`。
- **中石油招标投标网**：通过 Playwright 浏览器捕获页面 JS 发出的加密 API，解析 `data.NOTICE_0001`（招标公告）和 `data.PURCHASE_1`（谈判采购公告），匹配 `title`。
- **招标网**（zb.zhaobiao.cn）：通过 Playwright 加载 HTML 页面，用 BeautifulSoup 解析 `<tbody>` 表格，提取标题/地区/发布时间，匹配 `title`。

## 支持的数据抓取方式（fetch_type）

| fetch_type | 说明 | 适用场景 | 必需配置项 |
|------------|------|----------|-----------|
| 默认（不填） | `requests` 直接请求 JSON API | 普通免登录 JSON 接口 | `api_url` |
| `playwright` | Playwright 浏览器捕获 XHR 响应 | JS 加密接口（如中石油） | `playwright_url`, `api_url` |
| `html` | Playwright 加载页面 + BeautifulSoup 解析 DOM | 服务端渲染的 HTML 页面（如招标网） | `playwright_url` |

> Playwright 模式遇到验证码（`code: "511"`）会自动重试，可在配置中通过 `playwright_retries`（默认 3）和 `playwright_retry_delay`（默认 5 秒）调整。

## 功能
- 支持多数据源、多抓取方式，统一关键词、统一去重、统一推送
- 国家管网每次请求自动设置当天 `releaseStartTime` 和 `releaseEndTime`
- Playwright 浏览器和页面在同类数据源间复用，减少资源开销
- 已推送过的公告按 ID 去重，不再重复推送
- 推送内容包含：关键词、项目名称、网站、类型、发布时间、推送时间、原文链接
- 去重记录保存在 `seen.json`，支持跨次运行累积去重
- 支持本地定时运行和 GitHub Actions / cron 托管运行（`RUN_ONCE=1` 环境变量）
- `wechat_webhook` 支持环境变量 `BID_WEBHOOK` 覆盖，避免明文入库

## 安装
```powershell
pip install -r requirements.txt
playwright install chromium
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
