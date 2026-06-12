# 部署到 GitHub Actions（免费托管，电脑可关机）

让招标监控在 GitHub 的服务器上每小时自动运行，无需你的电脑常开。

## 一、准备：创建一个**私有**仓库

> ⚠️ 一定要用**私有(Private)仓库**。虽然令牌放在 Secrets 里，但私有仓库更安全。

1. 登录 GitHub → 右上角 `+` → `New repository`
2. 填仓库名（如 `bid-monitor`）→ 选 **Private** → `Create repository`

## 二、上传代码

需要上传这些文件/文件夹：
- `main.py`
- `config.yaml`（已清空令牌和 webhook，安全）
- `requirements.txt`
- `.github/`（含定时任务）
- `.gitignore`

**不要**上传 `release/`、`seen.json`（首次没有也没关系）、`crawler.log`。

### 上传方式（二选一）

**方式 A：网页拖拽（最简单）**
1. 进入刚建的仓库 → `uploading an existing file`
2. 把上述文件拖进去 → `Commit changes`
3. 注意：`.github` 文件夹需要保持目录结构，网页拖拽文件夹即可。

**方式 B：用 Git 命令**（你电脑需装 Git）
```powershell
cd d:\python
git init
git add main.py config.yaml requirements.txt .github .gitignore
git commit -m "init"
git branch -M main
git remote add origin https://github.com/你的用户名/bid-monitor.git
git push -u origin main
```

## 三、设置密钥（Secrets）

把真实令牌和 webhook 存进仓库密钥，不会泄露：

1. 仓库页面 → `Settings` → 左侧 `Secrets and variables` → `Actions`
2. 点 `New repository secret`，添加两个：

| Name | Secret（值） |
|------|------|
| `BID_AUTHORIZATION` | 你的令牌，如 `d9c41fa2-9d72-4997-83b1-bf295e0dd8a3`（不用加 Bearer，程序会自动加） |
| `BID_WEBHOOK` | 企业微信群机器人完整地址 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx` |

## 四、开启并测试

1. 仓库页面 → `Actions` 标签 → 如提示启用工作流，点启用
2. 左侧选 `招标关键词监控` → 右侧 `Run workflow` 手动跑一次测试
3. 看运行日志是否成功、企业微信是否收到消息

成功后，它会**每小时自动运行**（无需你管）。

## 五、令牌过期了怎么办

令牌失效时，程序会推一条企业微信告警。更新步骤：
1. 浏览器登录招标网站 → F12 复制最新令牌
2. 仓库 → `Settings` → `Secrets` → 点 `BID_AUTHORIZATION` → `Update secret` → 粘贴新令牌保存
3. 下个整点会自动用新令牌运行（或去 Actions 手动 Run 一次）

## 常见问题

**Q：定时不准，没有准点运行？**
A：GitHub 定时任务在高峰期可能延迟几分钟到十几分钟，属正常现象，对每小时检查无影响。

**Q：会重复推送同一条吗？**
A：不会。每次运行后 `seen.json` 会自动提交回仓库，下次运行读取它去重。

**Q：免费额度够用吗？**
A：私有仓库每月有充足的免费分钟数，这个任务每次只跑几十秒，远用不完。
