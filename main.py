"""
关键词接口监控爬虫
- 定时请求免登录 JSON 接口 / HTML 页面
- 支持多种抓取方式: requests（JSON API）、playwright（JS 加密接口）、
  html（Playwright + DOM 解析）、requests_html（requests + 静态 HTML 解析）
- macOS SSL 不兼容时自动降级为 curl 请求
- 遍历返回 list，命中自定义关键词的新记录推送到企业微信群机器人
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin

from typing import Optional, Dict, Set

import requests
import schedule
import yaml
from playwright.sync_api import sync_playwright, Page, Browser, Playwright


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

# ── Playwright 浏览器管理 ──────────────────────────────────────────────
_pw: Optional[Playwright] = None
_browser: Optional[Browser] = None
_page_cache: Dict[str, Page] = {}

def _get_page(url: str) -> Page:
    """获取或创建指定 base URL 的浏览器 Page（复用，避免反复启动浏览器）。

    首次创建时注入 XHR monkey-patch，使页面发出的 axios 请求在解密后
    被自动捕获到 window.__CAPTURED_XHR__ 中。
    """
    global _pw, _browser, _page_cache
    if _pw is None:
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=True)
        log.info("Playwright 浏览器已启动")
    if url not in _page_cache:
        page = _browser.new_page()
        # 在页面 JS 加载前注入 XHR monkey-patch
        page.add_init_script("""
            (function() {
                const OrigXHR = window.XMLHttpRequest;
                window.XMLHttpRequest = function() {
                    const xhr = new OrigXHR();
                    const origOpen = xhr.open;
                    const origSend = xhr.send;
                    let _url, _method, _body, _startTime;

                    xhr.open = function(method, url, ...args) {
                        _url = url;
                        _method = method;
                        _startTime = Date.now();
                        return origOpen.apply(this, [method, url, ...args]);
                    };

                    xhr.send = function(body) {
                        _body = body;
                        xhr.addEventListener('load', function() {
                            try {
                                const resp = {
                                    url: _url,
                                    method: _method,
                                    requestBody: _body,
                                    responseText: xhr.responseText,
                                    responseJSON: null,
                                    status: xhr.status,
                                    elapsed: Date.now() - _startTime,
                                };
                                // 尝试解析 JSON 响应
                                try {
                                    resp.responseJSON = JSON.parse(xhr.responseText);
                                } catch (_) {}
                                window.__CAPTURED_XHR__ = window.__CAPTURED_XHR__ || [];
                                window.__CAPTURED_XHR__.push(resp);
                            } catch (_) {}
                        });
                        return origSend.apply(this, [body]);
                    };
                    return xhr;
                };
                window.XMLHttpRequest.prototype = OrigXHR.prototype;
            })();
        """)
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(6000)  # 等待 JS 完全初始化并发起首轮 API 请求
        _page_cache[url] = page
        log.info("Playwright 页面已就绪: %s", url)
    return _page_cache[url]

def _cleanup_browser() -> None:
    """关闭 Playwright 浏览器，释放资源。"""
    global _pw, _browser, _page_cache
    for page in list(_page_cache.values()):
        try:
            page.close()
        except Exception:
            pass
    _page_cache.clear()
    if _browser:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None
    if _pw:
        try:
            _pw.stop()
        except Exception:
            pass
        _pw = None
    log.info("Playwright 浏览器已关闭")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 环境变量覆盖企业微信 Webhook(用于 GitHub Actions / 服务器部署,避免明文入库)
    webhook = os.environ.get("BID_WEBHOOK")
    if webhook:
        cfg["wechat_webhook"] = webhook
    return cfg


def iter_sources(cfg: dict):
    """遍历所有数据源。

    若配置中存在 sources 列表,则把每个数据源与全局配置(keywords/webhook 等)
    合并后逐个产出(数据源字段优先);否则把顶层配置作为单一数据源(兼容旧配置)。
    """
    sources = cfg.get("sources")
    if sources:
        base = {k: v for k, v in cfg.items() if k != "sources"}
        for src in sources:
            merged = {**base, **(src or {})}
            yield merged
    else:
        yield cfg


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


# Playwright 接口响应缓存: 同一轮 run_all 中多个数据源共享同一页面,避免重复 reload
_pw_resp_cache: Dict[str, tuple[float, dict]] = {}

def _is_valid_api_response(captured: dict) -> bool:
    """检查 Playwright 捕获的 API 响应是否为有效数据。

    正常响应 code 为 "0000"，data 为含各板块列表的 dict；
    触发验证码时 code 为 "511"，data 为 base64 图片字符串。
    """
    return isinstance(captured, dict) and isinstance(captured.get("data"), dict)


def fetch_playwright(cfg: dict) -> dict:
    """通过 Playwright 请求加密接口。

    页面首次加载时已通过 add_init_script 注入了 XHR monkey-patch,
    可捕获页面 axios 发出的加密请求及其明文 JSON 响应。
    每次调用 reload 页面以触发新一轮 API 请求,再通过 window.__CAPTURED_XHR__
    读取 axios 解密后的响应数据。

    同一 playwright_url 在 30 秒内复用缓存的响应,避免同一轮中多个数据源重复 reload。
    遇到验证码等异常响应时自动重试(默认最多 3 次)。
    """
    page = _get_page(cfg["playwright_url"])
    api_url = cfg.get("api_url", "/cms/frontPage/init")

    # 短时缓存: 30 秒内同一页面不重复 reload
    cache_key = cfg["playwright_url"]
    cache_entry = _pw_resp_cache.get(cache_key)
    if cache_entry is not None:
        cached_time, cached_data = cache_entry
        if time.time() - cached_time < 30:
            log.info("服用 Playwright 缓存响应 (%d 秒前)", int(time.time() - cached_time))
            return cached_data

    max_retries = cfg.get("playwright_retries", 3)
    retry_delay = cfg.get("playwright_retry_delay", 5)
    url_hint = api_url.rsplit("/", 1)[-1]

    for attempt in range(1, max_retries + 1):
        # 清空之前捕获的数据并 reload 触发新一轮 API 请求
        page.evaluate("() => { window.__CAPTURED_XHR__ = []; }")
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(6000)  # 等待 JS 初始化完成并发出 API 请求

        # 从捕获的 XHR 响应中找到目标 API 的明文 JSON
        captured = page.evaluate(
            """
            (hint) => {
                const all = window.__CAPTURED_XHR__ || [];
                for (const entry of all) {
                    if (entry.url && entry.url.includes(hint)) {
                        return entry.responseJSON || JSON.parse(entry.responseText || 'null');
                    }
                }
                return null;
            }
            """,
            url_hint,
        )

        if captured is None:
            xhr_count = page.evaluate("() => (window.__CAPTURED_XHR__ || []).length")
            raise ValueError(f"未捕获到 {api_url} 的 API 响应，共捕获 {xhr_count} 个 XHR 请求")

        if _is_valid_api_response(captured):
            log.info("Playwright 响应有效 (第 %d 次尝试)", attempt)
            _pw_resp_cache[cache_key] = (time.time(), captured)
            return captured

        code = captured.get("code", "?")
        data_type = type(captured.get("data")).__name__
        if attempt < max_retries:
            log.warning(
                "Playwright 响应异常 code=%s data 类型=%s，%d 秒后重试 (%d/%d)",
                code, data_type, retry_delay, attempt, max_retries,
            )
            time.sleep(retry_delay)
        else:
            raise ValueError(
                f"Playwright 重试 {max_retries} 次后仍未获取到有效响应 "
                f"(code={code}，data 类型={data_type})，可能触发了验证码"
            )

    # 理论上不会走到这里，仅为完整性
    raise RuntimeError("fetch_playwright 意外退出")


def _parse_zhaobiao(soup: "BeautifulSoup", cfg: dict) -> list[dict]:
    """解析 招标网 zb.zhaobiao.cn 的投标列表。"""
    tbody = soup.find("tbody", id="datatbody")
    if not tbody:
        raise ValueError("未在页面中找到 datatbody 表格")
    items = []
    for tr in tbody.find_all("tr", class_="datatr"):
        a_tag = tr.find("a", href=True)
        if not a_tag:
            continue
        title = a_tag.get("title", "").strip()
        link = a_tag["href"]
        if not link.startswith("http"):
            link = "https://zb.zhaobiao.cn" + link
        tds = tr.find_all("td")
        province = tds[1].get_text(strip=True) if len(tds) > 1 else ""
        publish_time = tds[2].get_text(strip=True) if len(tds) > 2 else ""
        items.append({
            "id": hashlib.md5(link.encode("utf-8")).hexdigest(),
            "title": title,
            "detail_url": link,
            "province": province,
            "publishedTime": publish_time,
        })
    return items


def _parse_ctg(soup: "BeautifulSoup", cfg: dict) -> list[dict]:
    """解析 中国三峡集团 eps.ctg.com.cn 的投标列表。"""
    ul = soup.find("ul", id="list1")
    if not ul:
        raise ValueError("未在页面中找到 ul#list1")
    items = []
    for li in ul.find_all("li", attrs={"name": "li_name"}):
        a_tag = li.find("a", href=True)
        if not a_tag:
            continue
        title = a_tag.get("title", "").strip()
        link = a_tag["href"]
        if not link.startswith("http"):
            link = "https://eps.ctg.com.cn" + link
        # 日期在 <a> 内的最后一个 <em>
        ems = a_tag.find_all("em")
        publish_time = ems[-1].get_text(strip=True) if ems else ""
        items.append({
            "id": hashlib.md5(link.encode("utf-8")).hexdigest(),
            "title": title,
            "detail_url": link,
            "publishedTime": publish_time,
        })
    return items


def _parse_tongyong(soup: "BeautifulSoup", cfg: dict) -> list[dict]:
    """解析 中国通用招标网 cgci.china-tender.com.cn 的投标列表。"""
    container = soup.find("div", class_="List2")
    if not container:
        raise ValueError("未在页面中找到 div.List2")
    items = []
    for li in container.find_all("li"):
        a_tag = li.find("a", href=True)
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        link = a_tag["href"]
        if not link.startswith("http"):
            link = "https://cgci.china-tender.com.cn" + link
        # 日期: <span class="Gray Right">发布时间：2026-06-23</span>
        date_span = li.find("span", class_="Gray")
        publish_time = ""
        if date_span:
            text = date_span.get_text(strip=True)
            publish_time = text.replace("发布时间：", "").strip()
        items.append({
            "id": hashlib.md5(link.encode("utf-8")).hexdigest(),
            "title": title,
            "detail_url": link,
            "publishedTime": publish_time,
        })
    return items


def _parse_okcis(soup: "BeautifulSoup", cfg: dict) -> list[dict]:
    """解析 招标采购导航网 www.okcis.cn/bn/ 的投标列表。"""
    container = soup.find("div", class_="tybl_list")
    if not container:
        raise ValueError("未在页面中找到 div.tybl_list")
    items = []
    for ul in container.find_all("ul"):
        # 标题在第一个 <li> 的 <a> 中
        first_li = ul.find("li")
        if not first_li:
            continue
        a_tag = first_li.find("a", href=True, title=True)
        if not a_tag:
            continue
        title = a_tag.get("title", "").strip()
        link = a_tag["href"]
        if not link.startswith("http"):
            link = "https://www.okcis.cn" + link
        # 日期在第二个 <li> 的 <b style="float: right"> 中，格式 "浙江-衢州 &nbsp; 2026-06-23"
        lis = ul.find_all("li")
        publish_time = ""
        if len(lis) > 1:
            date_b = lis[1].find("b", style=lambda v: v and "float: right" in v if v else False)
            if date_b:
                text = date_b.get_text(" ", strip=True)
                if "\xa0" in text or "&nbsp;" in text:
                    parts = text.replace("\xa0", " ").split()
                    publish_time = parts[-1] if parts else ""
                else:
                    parts = text.rsplit(" ", 1)
                    publish_time = parts[-1] if len(parts) > 1 else text
        items.append({
            "id": hashlib.md5(link.encode("utf-8")).hexdigest(),
            "title": title,
            "detail_url": link,
            "publishedTime": publish_time,
        })
    return items


def _parse_ccgp(soup: "BeautifulSoup", cfg: dict) -> list[dict]:
    """解析 中国政府采购网 www.ccgp.gov.cn 的中央/地方公告列表。"""
    ul = soup.find("ul", class_="c_list_bid")
    if not ul:
        raise ValueError("未在页面中找到 ul.c_list_bid")
    base_url = cfg.get("playwright_url", "https://www.ccgp.gov.cn/")
    items = []
    for li in ul.find_all("li"):
        a_tag = li.find("a", href=True)
        if not a_tag:
            continue
        title = a_tag.get("title", "").strip() or a_tag.get_text(strip=True)
        link = urljoin(base_url, a_tag["href"])
        # 发布时间在没有 rel 属性的 <em> 中，格式 "2026-06-24 13:52"
        publish_time = ""
        for em in li.find_all("em"):
            if em.get("rel"):
                continue
            text = em.get_text(strip=True)
            if len(text) >= 10 and text[4] == "-" and text[7] == "-":
                publish_time = text[:10]
                break
        items.append({
            "id": hashlib.md5(link.encode("utf-8")).hexdigest(),
            "title": title,
            "detail_url": link,
            "publishedTime": publish_time,
        })
    return items


def _parse_365trade(soup: "BeautifulSoup", cfg: dict) -> list[dict]:
    """解析 中招联合招标采购网 www.365trade.com.cn 的投标列表。"""
    ul = soup.find("ul", class_="searchList")
    if not ul:
        raise ValueError("未在页面中找到 ul.searchList")
    items = []
    for li in ul.find_all("li"):
        a_tag = li.find("a", href=True)
        if not a_tag:
            continue
        # 标题在 <span class="span_hover" title="...">
        span = a_tag.find("span", class_="span_hover")
        if span:
            title = span.get("title", "").strip()
        else:
            title = a_tag.get_text(strip=True)
        link = a_tag["href"]
        if not link.startswith("http"):
            link = "http://www.365trade.com.cn" + link
        # 日期: <i class="fl dis_block release_date">发布日期：2026-06-23</i>
        date_i = a_tag.find("i", class_="release_date")
        publish_time = ""
        if date_i:
            text = date_i.get_text(strip=True)
            publish_time = text.replace("发布日期：", "").strip()
        items.append({
            "id": hashlib.md5(link.encode("utf-8")).hexdigest(),
            "title": title,
            "detail_url": link,
            "publishedTime": publish_time,
        })
    return items


# 解析器注册表
_HTML_PARSERS = {
    "zhaobiao": _parse_zhaobiao,
    "ctg": _parse_ctg,
    "tongyong": _parse_tongyong,
    "okcis": _parse_okcis,
    "365trade": _parse_365trade,
    "ccgp": _parse_ccgp,
}


def fetch_html(cfg: dict) -> dict:
    """通过 Playwright 加载 HTML 页面，根据 html_parser 配置选择解析策略。

    与 fetch_playwright 不同，HTML 模式不需要 XHR monkey-patch，
    使用 domcontentloaded 即可，避免 networkidle 在某些站点超时。
    每个 HTML 数据源使用独立的非缓存页面以保证数据新鲜。

    支持的 html_parser:
      zhaobiao - 招标网 zb.zhaobiao.cn
      ctg      - 中国三峡集团 eps.ctg.com.cn
      tongyong - 中国通用招标网 cgci.china-tender.com.cn
      okcis    - 招标采购导航网 www.okcis.cn
      365trade - 中招联合招标采购网 www.365trade.com.cn
      ccgp     - 中国政府采购网 www.ccgp.gov.cn
    """
    from bs4 import BeautifulSoup

    global _pw, _browser

    parser_name = cfg.get("html_parser", "zhaobiao")
    parser = _HTML_PARSERS.get(parser_name)
    if parser is None:
        raise ValueError(
            f"未知的 html_parser: {parser_name}，可选值: {list(_HTML_PARSERS.keys())}"
        )

    # 确保浏览器已启动（复用全局实例）
    if _pw is None:
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=True)
        log.info("Playwright 浏览器已启动")

    wait_selector = cfg.get("wait_selector")
    page_url = cfg["playwright_url"]

    page = _browser.new_page()
    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        if wait_selector:
            page.wait_for_selector(wait_selector, timeout=15000)
        page.wait_for_timeout(2000)

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        items = parser(soup, cfg)

        log.info("HTML[%s] 解析完成，获取到 %d 条记录", parser_name, len(items))
        return {"list": items}
    finally:
        page.close()


def fetch_requests_html(cfg: dict) -> dict:
    """通过 requests 直接获取服务端渲染的 HTML 页面，适用于不需要 JS 的静态页面。"""
    from bs4 import BeautifulSoup
    url = cfg.get("playwright_url") or cfg["api_url"]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Referer": cfg.get("referer", ""),
    }
    headers.update(build_headers(cfg))
    resp = requests.get(url, headers=headers, timeout=cfg.get("timeout", 15))
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    parser_name = cfg.get("html_parser", "")
    parser = _HTML_PARSERS.get(parser_name)
    if parser is None:
        raise ValueError(f"未知的 html_parser: {parser_name}，可选值: {list(_HTML_PARSERS.keys())}")
    soup = BeautifulSoup(resp.text, "html.parser")
    items = parser(soup, cfg)
    log.info("HTML[%s] 解析完成，获取到 %d 条记录", parser_name, len(items))
    return {"list": items}

def _fetch_via_curl(cfg: dict) -> dict:
    """通过 curl 子进程请求 JSON，用于 Python SSL 栈（LibreSSL）无法完成 TLS 握手的数据源。
    在 GitHub Actions（OpenSSL）上不会走此路径，因为 ssl_verify: false 只在本地 config.yaml 设置。
    """
    url = cfg["api_url"]
    params = cfg.get("params") or {}
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    headers = build_headers(cfg)
    args = ["curl", "-s", "-k", url]
    for k, v in headers.items():
        args += ["-H", f"{k}: {v}"]
    timeout = cfg.get("timeout", 15)
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 5)
    if result.returncode != 0:
        raise RuntimeError(f"curl 请求失败: {result.stderr.strip()}")
    return json.loads(result.stdout)


def fetch(cfg: dict) -> dict:
    """请求接口,返回解析后的 JSON 字典。
    根据 fetch_type 自动选择 requests、Playwright 或 HTML 解析方式。
    """
    # Playwright 模式: 由浏览器 JS 处理加解密
    if cfg.get("fetch_type") == "playwright":
        return fetch_playwright(cfg)
    # HTML 解析模式: 使用 Playwright 加载页面后直接解析 DOM
    if cfg.get("fetch_type") == "html":
        return fetch_html(cfg)
    # requests 直接获取 HTML 模式: 适用于服务端渲染的静态页面
    if cfg.get("fetch_type") == "requests_html":
        return fetch_requests_html(cfg)
    # curl 回退: macOS LibreSSL 2.8.3 与部分服务器 TLS 握手不兼容，GitHub Actions 上使用正常 requests 路径
    if cfg.get("ssl_verify") is False:
        return _fetch_via_curl(cfg)
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
        # URL-encode string values that are not themselves URLs (e.g. datetime fields with spaces)
        encoded = {
            k: quote(str(v), safe="") if isinstance(v, str) and not str(v).startswith("http") else v
            for k, v in item.items()
        }
        return template.format(**encoded)
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


def format_release_time(item: dict, cfg: dict) -> str:
    """返回发布时间的展示字符串。

    time_format 为 'date' 时,字段本身就是日期字符串(如 '2026-06-22'),直接展示;
    否则按毫秒级时间戳解析。
    """
    field = cfg.get("start_time_field") or "releaseTime"
    val = item.get(field)
    if val in (None, ""):
        return ""
    if cfg.get("time_format", "ms") == "date":
        return str(val)
    dt = parse_ts(item, field)
    return f"{dt:%Y-%m-%d %H:%M}" if dt else ""


def build_message(item: dict, hits: list[str], cfg: dict) -> str:
    title = str(item.get(cfg.get("title_field") or "") or get_title(item, cfg))
    release_time = format_release_time(item, cfg)
    bid_end_time = parse_ts(item, "bidEndTime")
    tenderer_name = str(item.get("tendererName") or "")
    type_label = str(cfg.get("type_label") or item.get("tname") or "")
    site = str(cfg.get("site") or "")
    link = build_link(item, cfg)
    kw_str = "、".join(hits)
    lines = [
        "**🔔 命中关键词监控**",
        f"> 关键词: `{kw_str}`",
        f"> 项目名称: {title}",
    ]
    if site:
        lines.append(f"> 网站: {site}")
    if type_label:
        lines.append(f"> 类型: {type_label}")
    if release_time:
        lines.append(f"> 发布时间: {release_time}")
    if bid_end_time:
        lines.append(f"> 开标时间: {bid_end_time:%Y-%m-%d %H:%M}")
    if tenderer_name:
        lines.append(f"> 招标人: {tenderer_name}")
    if link:
        lines.append(f"> 链接: [点击查看]({link})")
    lines.append(f"> 推送时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    return "\n".join(lines)


def run_once(cfg: dict, seen: set) -> int:
    """检查单个数据源,返回本轮新推送条数。不负责写入 seen 文件。"""
    source_name = cfg.get("name") or cfg["api_url"]
    log.info("开始检查数据源: %s", source_name)
    try:
        data = fetch(cfg)
    except (requests.RequestException, ValueError) as e:
        log.error("[%s] 请求失败: %s", source_name, e)
        return 0
    except Exception as e:
        # Playwright 等非 requests 的异常也统一捕获
        log.error("[%s] 抓取异常 (%s): %s", source_name, type(e).__name__, e)
        return 0

    # 请求成功,重置过期告警状态
    _alert_state["token_alerted"] = False

    items = dig(data, cfg.get("list_path", "data.list"))
    if not isinstance(items, list):
        log.error("[%s] 未在路径 '%s' 找到 list,请检查 list_path 配置。返回顶层键: %s",
                  source_name, cfg.get("list_path"),
                  list(data.keys()) if isinstance(data, dict) else type(data))
        return 0
    log.info("[%s] 获取到 %d 条记录", source_name, len(items))

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
        log.info("[%s] 命中: %s (关键词: %s)", source_name, get_title(item, cfg), hits)
        # 仅在推送成功后才标记为已读,避免推送失败造成漏推
        if push_wechat(webhook, build_message(item, hits, cfg)):
            seen.add(rid)
            new_hits += 1

    log.info("[%s] 完成,新推送 %d 条,跳过已过期 %d 条", source_name, new_hits, expired)
    return new_hits


def run_all(cfg: dict, seen: set, seen_path: Path) -> None:
    """遍历所有数据源各检查一次,共享去重集合,最后统一落盘。"""
    total_new = 0
    for src in iter_sources(cfg):
        total_new += run_once(src, seen)
    if total_new:
        save_seen(seen_path, seen)
    log.info("本轮全部数据源完成,合计新推送 %d 条", total_new)


def main() -> None:
    cfg = load_config()
    seen_path = BASE_DIR / cfg.get("seen_file", "seen.json")
    seen = load_seen(seen_path)

    # 单次运行模式(GitHub Actions / cron 定时触发时使用): 跑一次就退出
    run_once_mode = os.environ.get("RUN_ONCE", "").strip() in ("1", "true", "True") \
        or bool(cfg.get("run_once"))
    if run_once_mode:
        log.info("单次运行模式")
        run_all(cfg, seen, seen_path)
        return

    interval = cfg.get("interval_minutes", 10)
    log.info("启动监控,每 %d 分钟检查一次", interval)

    run_all(cfg, seen, seen_path)  # 启动立即执行一次
    schedule.every(interval).minutes.do(run_all, cfg, seen, seen_path)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("已手动停止")
    finally:
        _cleanup_browser()
