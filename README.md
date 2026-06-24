# 关键词接口监控爬虫

定时请求多个招标公告接口（JSON API / HTML 页面），当标题命中自定义关键词时，自动推送到企业微信群机器人。

## 项目结构

```
bid-monitor/
├── main.py                          # 主程序（唯一源代码，约 860 行）
├── config.yaml                      # 主配置文件（数据源、关键词、webhook 等）
├── config_test.yaml                 # 测试用最小配置（仅含三峡集团一个数据源）
├── requirements.txt                 # Python 依赖
├── seen.json                        # 去重持久化存储（已推送过的记录 ID）
├── crawler.log                      # 运行时日志（自动生成，已在 .gitignore 中）
├── README.md                        # 项目说明
├── .gitignore
├── .github/workflows/monitor.yml    # GitHub Actions 定时工作流
├── data.txt                         # 国家管网 API 参考文档
├── explain.txt                      # 需求规格说明（万项网接口文档）
├── addDataresource.txt              # 中国石油招标中心 / 招标网页面格式参考文档
├── addDataSource2.txt               # 中国政府采购网页面格式参考文档
└── Cwechat.txt                      # 企业微信 webhook URL（注意：包含敏感信息）
```

## 支持的数据源

### 当前激活的数据源（13 个）

| # | 数据源名称 | fetch_type | 网站 | 抓取方式 |
|---|-----------|------------|------|----------|
| 1 | 国家管网 | 默认（requests） | iscmdzzb.pipechina.com.cn | POST JSON API，按当天时间范围查询 |
| 2 | 万项网-推荐招标 | 默认（requests） | www.chinazbcg.com (tid=23) | GET JSON API |
| 3 | 万项网-招标公告 | 默认（requests） | www.chinazbcg.com (tid=2) | GET JSON API |
| 4 | 万项网-国际招标 | 默认（requests） | www.chinazbcg.com (tid=12) | GET JSON API |
| 5 | 招标网 | html | zb.zhaobiao.cn | Playwright 加载页面 + BeautifulSoup 解析表格 |
| 6 | 中国三峡集团 | html | eps.ctg.com.cn | Playwright 加载页面 + BeautifulSoup 解析列表 |
| 7 | 中国通用招标网 | html | cgci.china-tender.com.cn | Playwright 加载页面 + BeautifulSoup 解析列表 |
| 8 | 中招联合-工程招标 | html | www.365trade.com.cn (typeId=101) | Playwright 加载页面 + BeautifulSoup 解析列表 |
| 9 | 中招联合-货物招标 | html | www.365trade.com.cn (typeId=102) | Playwright 加载页面 + BeautifulSoup 解析列表 |
| 10 | 中招联合-服务招标 | html | www.365trade.com.cn (typeId=103) | Playwright 加载页面 + BeautifulSoup 解析列表 |
| 11 | 中国石油招标中心 | 默认（requests/curl） | www2.cnpcbidding.com | GET JSON API，macOS 下自动降级为 curl 请求 |
| 12 | 政府采购网-中央公告 | requests_html | www.ccgp.gov.cn/cggg/zygg/ | requests 获取静态 HTML + BeautifulSoup 解析 |
| 13 | 政府采购网-地方公告 | requests_html | www.ccgp.gov.cn/cggg/dfgg/ | requests 获取静态 HTML + BeautifulSoup 解析 |

### 已注释/暂不可用的数据源

| 数据源名称 | 原因 |
|-----------|------|
| 中石油-招标公告 | 加密接口，需 Playwright XHR 捕获，当前未启用 |
| 中石油-谈判采购 | 同上 |
| 招标采购导航网 (okcis.cn) | 站点需要人工打验证码，暂不可用 |

## 支持的数据抓取方式（fetch_type）

| fetch_type | 说明 | 适用场景 | 必需配置项 |
|------------|------|----------|-----------|
| 默认（不填） | `requests` 直接请求 JSON API | 普通免登录 JSON 接口 | `api_url` |
| `playwright` | Playwright 浏览器捕获 XHR 响应 | JS 加密接口（如中石油） | `playwright_url`, `api_url` |
| `html` | Playwright 加载页面 + BeautifulSoup 解析 DOM | 服务端渲染的 HTML 页面 | `playwright_url`, `html_parser` |
| `requests_html` | requests 直接获取静态 HTML + BeautifulSoup 解析 | 不需要 JS 的静态 HTML 页面 | `playwright_url`, `html_parser` |

### fetch_type 详解

**1. 默认模式（requests）**
- 直接使用 `requests` 库发起 GET/POST 请求
- 支持自定义 headers、params（GET）和 body（POST）
- 国家管网会自动设置当天 `releaseStartTime` / `releaseEndTime`（毫秒时间戳）
- 适用于：国家管网、万项网

**2. Playwright 模式**
- 启动 Chromium 无头浏览器，注入 XHR monkey-patch 拦截页面 JS 发出的加密请求
- 捕获解密后的 JSON 响应，从 `window.__CAPTURED_XHR__` 读取
- 遇到验证码（`code: "511"`）会自动重试，可通过 `playwright_retries`（默认 3）和 `playwright_retry_delay`（默认 5 秒）调整
- 同一 `playwright_url` 在 30 秒内复用缓存，避免同轮多次 reload
- 适用于：中石油等 JS 加密站点

**3. HTML 模式**
- 使用 Playwright 加载页面，获取渲染后的 HTML
- 通过 `BeautifulSoup` 按预设解析器解析 DOM 元素
- 支持 `wait_selector` 等待目标元素加载完成
- 每次请求创建新页面以保证数据新鲜，但复用全局浏览器实例
- 内置解析器注册表：

| html_parser | 解析函数 | 目标网站 |
|-------------|----------|----------|
| `zhaobiao` | `_parse_zhaobiao()` | 招标网 zb.zhaobiao.cn |
| `ctg` | `_parse_ctg()` | 中国三峡集团 eps.ctg.com.cn |
| `tongyong` | `_parse_tongyong()` | 中国通用招标网 cgci.china-tender.com.cn |
| `okcis` | `_parse_okcis()` | 招标采购导航网 www.okcis.cn |
| `365trade` | `_parse_365trade()` | 中招联合招标采购网 www.365trade.com.cn |
| `ccgp` | `_parse_ccgp()` | 中国政府采购网 www.ccgp.gov.cn |

**4. requests_html 模式**
- 使用 `requests` 库直接发起 GET 请求获取服务端渲染的 HTML 页面
- 通过 `BeautifulSoup` 按预设解析器解析 DOM 元素
- 相比 `html` 模式，无需启动 Playwright 浏览器，资源开销更小、速度更快
- 页面内容由服务器直接返回，不依赖 JS 执行
- 适用于：中国政府采购网等纯静态 HTML 页面

> Playwright 模式下，浏览器和页面在同类数据源间复用，减少资源开销。HTML 模式下每个数据源使用独立页面以保证数据新鲜。requests_html 模式不依赖 Playwright，直接使用 requests 库获取页面。

## 配置说明

### 全局配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `keywords` | `list[str]` | — | 监控关键词列表，命中任意一个即触发推送 |
| `case_sensitive` | `bool` | `false` | 关键词匹配是否区分大小写 |
| `timeout` | `int` | `15` | HTTP 请求超时时间（秒） |
| `interval_minutes` | `int` | `10` | 本地循环运行时的检查间隔（分钟） |
| `seen_file` | `str` | `"seen.json"` | 去重记录文件路径 |
| `wechat_webhook` | `str` | `""` | 企业微信群机器人 webhook 地址（可通过 `BID_WEBHOOK` 环境变量覆盖） |
| `run_once` | `bool` | `false` | 设为 `true` 则运行一次后退出（等价于 `RUN_ONCE=1`） |

### 数据源配置项

每个数据源可配置以下字段，未配置项自动继承全局值：

| 配置项 | 必需 | 说明 |
|--------|------|------|
| `name` | 是 | 数据源名称（用于日志标识和推送消息中的网站字段） |
| `site` | 否 | 推送消息中展示的网站名称 |
| `fetch_type` | 否 | 抓取方式：`"playwright"` / `"html"` / `"requests_html"` / 不填（默认 requests） |
| `api_url` | 视模式 | 目标 API 地址（默认和 playwright 模式必需） |
| `method` | 否 | HTTP 方法：`"GET"` / `"POST"`，默认 `"GET"` |
| `params` | 否 | GET 请求的 URL 参数（dict） |
| `body` | 否 | POST 请求的 JSON body（dict） |
| `headers` | 否 | 自定义请求头（dict） |
| `cookie` | 否 | Cookie 字符串（自动添加到 headers） |
| `use_today_release_range` | 否 | 是否自动设置当天发布时间范围（默认 `true`，用于国家管网） |
| `list_path` | 是 | 返回 JSON 中列表数据的路径，如 `"data.list"`、`"result.list"` |
| `match_fields` | 是 | 需要进行关键词匹配的字段列表 |
| `id_field` | 否 | 唯一 ID 字段名（用于去重，无则自动用 match_fields 拼接做 MD5） |
| `title_field` | 否 | 标题字段名（用于推送消息和日志展示） |
| `type_label` | 否 | 公告类型标签，推送消息中显示 |
| `start_time_field` | 否 | 发布时间的字段名 |
| `time_format` | 否 | 时间格式：`"ms"`（毫秒时间戳）或 `"date"`（日期字符串如 "2026-06-22"） |
| `deadline_field` | 否 | 开标/截止时间字段名（毫秒时间戳），用于过滤已过期公告 |
| `detail_url_template` | 否 | 详情链接模板，如 `"{detail_url}"` 或 `"https://example.com/{id}"`。模板中的 `{字段名}` 会被自动 URL 编码（非 HTTP 字符串值） |
| `ssl_verify` | 否 | 设为 `false` 时跳过 SSL 证书验证。macOS 下自动使用 curl 子进程替代 Python requests（LibreSSL 与部分服务器 TLS 不兼容） |

**HTML 模式专属配置项：**

| 配置项 | 必需 | 说明 |
|--------|------|------|
| `playwright_url` | 是 | 要加载的页面 URL |
| `html_parser` | 是 | 解析器名称，可选值见上文解析器注册表 |
| `wait_selector` | 否 | CSS 选择器，等待该元素出现后再解析（超时 15 秒） |

### 配置示例

完整示例参见 `config.yaml`（含 13 个数据源的完整配置），最小测试示例参见 `config_test.yaml`（仅含三峡集团一个数据源）。

## 环境变量

| 变量名 | 说明 |
|--------|------|
| `BID_WEBHOOK` | 企业微信 webhook 地址，会覆盖 `config.yaml` 中的 `wechat_webhook`。用于 GitHub Actions / 服务器部署时避免明文入库 |
| `RUN_ONCE` | 设为 `1` 或 `true` 则单次运行后退出（用于 cron / GitHub Actions 定时触发） |

## 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

## 运行

### 本地持续监控

```bash
python main.py
```

启动后立即检查一次，之后按 `interval_minutes` 设定的间隔循环轮询。`Ctrl+C` 停止。

### 单次运行（测试 / cron）

```bash
RUN_ONCE=1 python main.py
```

或在 `config.yaml` 中设置 `run_once: true`。

## 功能

- 支持多数据源、多抓取方式，统一关键词、统一去重、统一推送
- 国家管网每次请求自动设置当天 `releaseStartTime` 和 `releaseEndTime`
- Playwright 浏览器和页面在同类数据源间复用，减少资源开销
- 已推送过的公告按 ID 去重，不再重复推送
- 推送内容包含：关键词、项目名称、网站、类型、发布时间、推送时间、原文链接
- 去重记录保存在 `seen.json`，支持跨次运行累积去重
- HTML 模式的数据源直接从页面解析链接，无需额外配置 `detail_url_template`
- requests_html 模式使用轻量级 requests 获取静态 HTML 页面，无需 Playwright
- `detail_url_template` 中的 `{字段名}` 自动 URL 编码，解决时间等字段含空格/特殊字符导致链接断裂的问题
- macOS 下 SSL 不兼容时自动降级为 curl 请求（`ssl_verify: false`）
- 支持本地定时运行和 GitHub Actions / cron 托管运行（`RUN_ONCE=1` 环境变量）
- `wechat_webhook` 支持环境变量 `BID_WEBHOOK` 覆盖，避免明文入库
- 已过期公告（开标时间早于当前时间）自动跳过，不推送

## 添加新数据源

### JSON API 数据源

在 `config.yaml` 的 `sources` 列表中添加：

```yaml
- name: "新网站名称"
  site: "网站简称"
  api_url: "https://example.com/api/list"
  method: "GET"                    # 或 "POST"
  params:                          # GET 参数（可选）
    pageSize: 50
    pageNo: 1
  # body: {}                       # POST body（可选）
  list_path: "data.list"           # 响应中列表数据的路径
  match_fields:                    # 要匹配关键词的字段
    - "title"
  id_field: "id"                   # 去重 ID 字段
  title_field: "title"             # 标题字段
  type_label: "招标公告"
  start_time_field: "publishTime"
  time_format: "ms"                # "ms" 或 "date"
  detail_url_template: "https://example.com/detail/{id}"
```

### HTML 页面数据源

1. 在 `config.yaml` 的 `sources` 列表中添加配置：

```yaml
- name: "新HTML网站"
  site: "网站简称"
  fetch_type: "html"
  html_parser: "myparser"
  playwright_url: "https://example.com/bid-list.html"
  wait_selector: "table#bidTable"   # 可选，CSS 选择器
  list_path: "list"
  match_fields:
    - "title"
  id_field: "id"
  title_field: "title"
  type_label: "招标公告"
  start_time_field: "publishedTime"
  time_format: "date"
  detail_url_template: "{detail_url}"
```

2. 在 `main.py` 中编写解析函数并注册：

```python
def _parse_myparser(soup: "BeautifulSoup", cfg: dict) -> list[dict]:
    """解析 新网站 的投标列表。"""
    # 使用 BeautifulSoup 解析页面，返回 list[dict]
    # 每个 dict 至少包含：id, title, detail_url, publishedTime
    ...

# 在 _HTML_PARSERS 字典中注册
_HTML_PARSERS = {
    ...
    "myparser": _parse_myparser,
}
```

## 技术栈

- **Python 3.12**
- **requests** — HTTP 请求（默认 fetch_type 和 requests_html fetch_type）
- **playwright** — 无头浏览器（playwright 和 html fetch_type）
- **beautifulsoup4** — HTML 解析（html 和 requests_html fetch_type）
- **PyYAML** — 配置文件解析
- **schedule** — 本地定时调度
- **curl** — SSL 回退方案（macOS LibreSSL 与部分服务器 TLS 握手不兼容时自动使用）

## GitHub Actions

仓库中已包含 `.github/workflows/monitor.yml`，每小时第 5 分钟触发一次（UTC 时间）。

只需要在仓库 Secrets 中配置：

| Name | Secret（值） |
|------|------|
| `BID_WEBHOOK` | 企业微信群机器人完整 Webhook 地址 |

运行后如有新增命中，工作流会自动更新并提交 `seen.json` 用于跨次去重。

> **注意：** 工作流已设置 `concurrency` 为 `bid-monitor`，同一时间只允许一个实例运行，避免 `seen.json` 并发写入冲突。

## 安全注意事项

1. **不要将 webhook URL 写入 `config.yaml` 并提交到仓库。** 使用环境变量 `BID_WEBHOOK` 代替。仓库中的 `Cwechat.txt` 包含真实 webhook URL，也应避免提交。
2. `config.yaml` 中的 `wechat_webhook` 留空，由环境变量在运行时注入。
3. GitHub Actions 通过 `secrets.BID_WEBHOOK` 安全注入。
4. `seen.json` 和 `config.yaml` 不在 `.gitignore` 中，因为它们需要被版本控制跟踪（用于跨运行持久化）。

## 常见问题

**Q: 运行报错 `playwright` 未安装？**
```bash
playwright install chromium
```

**Q: HTML 模式抓取不到数据？**
- 检查目标网站是否改版，页面结构可能已变化
- 查看 `crawler.log` 中的 `HTML[parser_name] 解析完成，获取到 X 条记录` 日志
- 确认 `wait_selector` 中指定的 CSS 选择器是否正确

**Q: macOS 上报 SSL 错误（如 `SSLError` / `TLS 握手失败`）？**
在数据源配置中设置 `ssl_verify: false`，程序会自动使用 curl 子进程替代 Python requests 发起请求。这是因为 macOS 自带的 LibreSSL 与部分服务器使用的 TLS 版本不兼容，而 GitHub Actions（使用 OpenSSL）不受此影响。

**Q: requests_html 和 html 模式有什么区别？**
- `html` 模式使用 Playwright 加载页面后再解析 DOM，适用于需要 JS 渲染的页面
- `requests_html` 模式直接用 `requests` 库获取服务端返回的 HTML，适用于纯静态页面，无需 Playwright 浏览器，资源开销更小、速度更快

**Q: 如何测试单个数据源？**
使用 `config_test.yaml`，将其重命名为 `config.yaml`（备份原文件），或修改代码中 `CONFIG_PATH` 指向测试配置，然后 `RUN_ONCE=1 python main.py`。

**Q: 推送消息格式是怎样的？**
```markdown
🔔 命中关键词监控
> 关键词: 网络安全
> 项目名称: xxx项目招标公告
> 网站: 万项网招标
> 类型: 招标公告
> 发布时间: 2026-06-22
> 链接: [点击查看](https://...)
> 推送时间: 2026-06-23 13:38:19
```
