"""
关键词接口监控爬虫
- 定时请求免登录 JSON 接口
- 遍历返回 list,命中自定义关键词的新记录推送到企业微信群机器人
"""
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import schedule
import yaml


def app_dir() -> Path:
    """返回程序所在目录: 打包成 exe 时为 exe 所在目录,否则为脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


BASE_DIR = app_dir()
CONFIG_PATH = BASE_DIR / "config.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "crawler.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# 运行期告警状态(保留兼容旧配置)
_alert_state = {"token_alerted": False}


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 环境变量覆盖企业微信 Webhook(用于 GitHub Actions / 服务器部署,避免明文入库)
    webhook = os.environ.get("BID_WEBHOOK")
    if webhook:
        cfg["wechat_webhook"] = webhook
    return cfg


def load_seen(seen_path: Path) -> set:
    if seen_path.exists():
        try:
            return set(json.loads(seen_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            log.warning("seen 文件读取失败,重新开始去重记录")
    return set()


def save_seen(seen_path: Path, seen: set) -> None:
    seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def build_headers(cfg: dict) -> dict:
    headers = dict(cfg.get("headers") or {})
    cookie = (cfg.get("cookie") or "").strip()
    if cookie and cookie != "在这里粘贴你的Cookie":
        headers["Cookie"] = cookie
    return headers


class TokenExpired(Exception):
    """兼容旧版本配置的异常类型。"""


def fetch(cfg: dict) -> dict:
    """请求接口,返回解析后的 JSON 字典。"""
    method = cfg.get("method", "GET").upper()
    headers = build_headers(cfg)
    kwargs = {
        "headers": headers,
        "params": cfg.get("params") or {},
        "timeout": cfg.get("timeout", 15),
    }
    if method == "POST":
        kwargs["json"] = build_body(cfg)
        log.info("发布时间范围: %s - %s", kwargs["json"].get("releaseStartTime"), kwargs["json"].get("releaseEndTime"))
        resp = requests.post(cfg["api_url"], **kwargs)
    else:
        resp = requests.get(cfg["api_url"], **kwargs)
    resp.raise_for_status()
    return resp.json()


def build_body(cfg: dict) -> dict:
    """构造请求体,每次运行动态写入当天发布时间范围。"""
    body = dict(cfg.get("body") or {})
    if cfg.get("use_today_release_range", True):
        start_ms, end_ms = today_range_ms()
        body["releaseStartTime"] = start_ms
        body["releaseEndTime"] = end_ms
    return body


def today_range_ms() -> tuple[int, int]:
    """返回本地当天 00:00:00.000 到 23:59:59.999 的毫秒时间戳。"""
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def dig(data, path: str):
    """按点号路径取值,如 'data.list'。取不到返回 None。"""
    cur = data
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def match_keywords(text: str, keywords: list[str], case_sensitive: bool) -> list[str]:
    hay = text if case_sensitive else text.lower()
    hits = []
    for kw in keywords:
        needle = kw if case_sensitive else kw.lower()
        if needle in hay:
            hits.append(kw)
    return hits


def record_id(item: dict, cfg: dict) -> str:
    id_field = cfg.get("id_field", "")
    if id_field and item.get(id_field) is not None:
        return str(item[id_field])
    # 没有唯一字段时,用匹配字段拼接做哈希
    raw = "|".join(str(item.get(f, "")) for f in cfg.get("match_fields", []))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def build_link(item: dict, cfg: dict) -> str:
    template = cfg.get("detail_url_template") or ""
    if not template:
        return ""
    try:
        return template.format(**item)
    except (KeyError, IndexError):
        return ""


def get_title(item: dict, cfg: dict) -> str:
    title_field = cfg.get("title_field") or ""
    if title_field and item.get(title_field):
        return str(item[title_field])
    for f in cfg.get("match_fields", []):
        if item.get(f):
            return str(item[f])
    return "(无标题)"


def parse_ts(item: dict, field: str):
    """把记录中指定字段(毫秒级时间戳)解析为 datetime。取不到返回 None。"""
    if not field:
        return None
    ts = item.get(field)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def get_deadline(item: dict, cfg: dict):
    """返回开标时间(datetime)。字段为毫秒级时间戳。取不到返回 None。"""
    return parse_ts(item, cfg.get("deadline_field") or "")


def push_wechat(webhook: str, content: str) -> bool:
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        resp = requests.post(webhook, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            log.info("已推送企业微信")
            return True
        log.error("企业微信推送失败: %s", data)
        return False
    except (requests.RequestException, ValueError) as e:
        log.error("企业微信推送异常: %s", e)
        return False


def build_message(item: dict, hits: list[str], cfg: dict) -> str:
    business_name = str(item.get("businessName") or get_title(item, cfg))
    release_time = parse_ts(item, "releaseTime")
    bid_end_time = parse_ts(item, "bidEndTime")
    tenderer_name = str(item.get("tendererName") or "")
    link = build_link(item, cfg)
    kw_str = "、".join(hits)
    lines = [
        "**🔔 命中关键词监控**",
        f"> 关键词: `{kw_str}`",
        f"> 项目名称: {business_name}",
    ]
    if release_time:
        lines.append(f"> 发布时间: {release_time:%Y-%m-%d %H:%M}")
    if bid_end_time:
        lines.append(f"> 开标时间: {bid_end_time:%Y-%m-%d %H:%M}")
    if tenderer_name:
        lines.append(f"> 招标人: {tenderer_name}")
    if link:
        lines.append(f"> 链接: [点击查看]({link})")
    lines.append(f"> 推送时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    return "\n".join(lines)


def run_once(cfg: dict, seen: set, seen_path: Path) -> None:
    log.info("开始检查接口: %s", cfg["api_url"])
    try:
        data = fetch(cfg)
    except requests.RequestException as e:
        log.error("请求失败: %s", e)
        return
    except ValueError as e:
        log.error("返回内容不是合法 JSON: %s", e)
        return

    # 请求成功,重置过期告警状态
    _alert_state["token_alerted"] = False

    items = dig(data, cfg.get("list_path", "data.list"))
    if not isinstance(items, list):
        log.error("未在路径 '%s' 找到 list,请检查 list_path 配置。返回顶层键: %s",
                  cfg.get("list_path"), list(data.keys()) if isinstance(data, dict) else type(data))
        return
    log.info("获取到 %d 条记录", len(items))

    keywords = cfg.get("keywords", [])
    case_sensitive = cfg.get("case_sensitive", False)
    match_fields = cfg.get("match_fields", [])
    webhook = cfg["wechat_webhook"]
    now = datetime.now()

    new_hits = 0
    expired = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        # 把所有要检查的字段拼成一段文本统一匹配
        text = " ".join(str(item.get(f, "")) for f in match_fields)
        hits = match_keywords(text, keywords, case_sensitive)
        if not hits:
            continue
        # 开标时间过滤: 已过开标时间的跳过
        deadline = get_deadline(item, cfg)
        if deadline is not None and deadline < now:
            expired += 1
            continue
        rid = record_id(item, cfg)
        if rid in seen:
            continue
        log.info("命中: %s (关键词: %s)", get_title(item, cfg), hits)
        # 仅在推送成功后才标记为已读,避免推送失败造成漏推
        if push_wechat(webhook, build_message(item, hits, cfg)):
            seen.add(rid)
            new_hits += 1

    if new_hits:
        save_seen(seen_path, seen)
    log.info("本轮完成,新推送 %d 条,跳过已过期 %d 条", new_hits, expired)


def main() -> None:
    cfg = load_config()
    seen_path = BASE_DIR / cfg.get("seen_file", "seen.json")
    seen = load_seen(seen_path)

    # 单次运行模式(GitHub Actions / cron 定时触发时使用): 跑一次就退出
    run_once_mode = os.environ.get("RUN_ONCE", "").strip() in ("1", "true", "True") \
        or bool(cfg.get("run_once"))
    if run_once_mode:
        log.info("单次运行模式")
        run_once(cfg, seen, seen_path)
        return

    interval = cfg.get("interval_minutes", 10)
    log.info("启动监控,每 %d 分钟检查一次", interval)

    run_once(cfg, seen, seen_path)  # 启动立即执行一次
    schedule.every(interval).minutes.do(run_once, cfg, seen, seen_path)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("已手动停止")
