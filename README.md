# 关键词接口监控爬虫

定时请求一个**需登录的 JSON 接口**，遍历返回的 list，当出现包含**自定义关键词**的新记录时，自动推送到**企业微信群机器人**。

## 原理
网站登录后通过接口（POST）返回 JSON 数据。爬虫携带浏览器登录后的 **Cookie** 请求同一接口，拿到 `data.list`，逐条检查指定字段是否包含关键词，命中且未推送过的就发到企业微信。

## 功能
- 定时轮询 JSON 接口（GET/POST，带 Cookie 登录）
- 按点号路径定位 list（如 `data.list`）
- 多字段、多关键词匹配（支持是否区分大小写）
- 按唯一字段去重，已推送不重复发送（记录在 `seen.json`）
- 命中后通过企业微信群机器人推送 Markdown 消息
- 运行日志输出到 `crawler.log`

## 安装
```powershell
pip install -r requirements.txt
```

## 配置
编辑 `config.yaml`：

1. **`api_url`**：F12 → Network → 找到返回 list 的请求 → 复制 Request URL。
2. **`method`**：`POST`（本站）。
3. **`body`**：POST 请求体，把 F12 里看到的参数（分页、查询条件等）填进来。
4. **`cookie`**：登录后 F12 → Network → 点该接口 → Request Headers → 复制整段 `Cookie` 粘贴。
   > ⚠️ Cookie 会过期，失效后日志会提示"返回不是合法 JSON"，重新复制即可。
5. **`list_path`**：list 在 JSON 里的路径，本站为 `data.list`。
6. **`match_fields`**：要检查关键词的字段（`bidSectionName`、`noticeName`、`tendererName`）。
7. **`id_field`**：去重唯一字段（`id`）。
8. **`keywords`**：监控的关键词。
9. **`detail_url_template`**（可选）：详情页链接模板，用 `{字段名}` 占位，如 `https://xxx/detail?id={bidSectionId}`。
10. **`wechat_webhook`**：企业微信群机器人地址（群聊 → `...` → 添加群机器人 → 复制 Webhook）。
11. **`interval_minutes`**：检查间隔（分钟）。

## 运行
```powershell
python main.py
```
启动后会立即检查一次，之后按设定间隔轮询。`Ctrl+C` 停止。

## 关于 Cookie 过期
手动复制 Cookie 最简单稳妥，适合监控类任务。若想全自动免维护，可改为代码模拟登录，但需处理验证码/加密参数，复杂度高很多——通常没必要。
