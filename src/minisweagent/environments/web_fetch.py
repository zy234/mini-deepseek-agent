"""分层网页正文抓取：HTTP 快路径，必要时使用 Playwright 渲染。"""

from __future__ import annotations

import atexit
import html
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from minisweagent.environments.web_time import cutoff_status, parse_web_time, require_cutoff

MAX_FETCH_BYTES = 1_000_000
MAX_TEXT_CHARS = 12_000
MAX_URL_LENGTH = 2_000
MIN_USEFUL_CONTENT_CHARS = 180
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SOFT_BLOCK_MARKERS = (
    "captcha",
    "verify you are human",
    "unusual traffic",
    "访问过于频繁",
    "安全验证",
    "请输入验证码",
    "系统检测到异常访问",
    "内容审核中",
)
CONTENT_HINTS = (
    "article",
    "artibody",
    "contentbody",
    "article-content",
    "article_content",
    "articlebody",
    "post-content",
    "news-content",
    "detailcontent",
    "g-article-content",
)
ARTICLE_SELECTOR = "article, #ContentBody, #artibody, .article-content, main"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class WebFetchError(RuntimeError):
    """网页地址、网络请求或正文解析失败。"""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def execute_web_fetch(
    url: str,
    *,
    timeout: float,
    as_of: str = "",
    browser_enabled: bool = False,
) -> dict[str, Any]:
    """抓取单篇正文，并在配置截止时间时由宿主执行点时过滤。"""
    try:
        normalized_url = _validate_url(url)
        cutoff = require_cutoff(as_of) if as_of else None
    except ValueError as error:
        return _error_result(str(error), "WEB_INVALID_CUTOFF", url, [])
    except WebFetchError as error:
        return _error_result(str(error), error.code, url, [])

    attempts: list[dict[str, str]] = []
    page: dict[str, Any] | None = None
    http_error: WebFetchError | None = None
    try:
        http_result = _http_get(
            normalized_url,
            timeout=max(1.0, min(float(timeout), 15.0)),
        )
        payload, content_type, charset = http_result[:3]
        final_url = http_result[3] if len(http_result) == 4 else normalized_url
        page = _extract_page(final_url, payload, content_type, charset, "http")
        attempts.append({"engine": "http", "status": "success", "detail": _quality_detail(page)})
    except WebFetchError as error:
        http_error = error
        attempts.append({"engine": "http", "status": _attempt_status(error.code), "detail": str(error)})

    if browser_enabled and (page is None or not page["data_quality"]["usable"]):
        try:
            payload, final_url = _browser_get(normalized_url, timeout=max(1.0, min(float(timeout), 30.0)))
            browser_page = _extract_page(final_url, payload, "text/html", "utf-8", "playwright")
            attempts.append(
                {"engine": "playwright", "status": "success", "detail": _quality_detail(browser_page)}
            )
            if page is not None:
                _merge_page_metadata(browser_page, page)
            if page is None or browser_page["data_quality"]["score"] > page["data_quality"]["score"]:
                page = browser_page
        except WebFetchError as error:
            attempts.append(
                {"engine": "playwright", "status": _attempt_status(error.code), "detail": str(error)}
            )

    if page is None:
        error = http_error or WebFetchError("所有网页抓取引擎均失败。", "WEB_FETCH_UNAVAILABLE")
        return _error_result(str(error), error.code, normalized_url, attempts)
    page["attempts"] = attempts

    published = parse_web_time(page["published_at"])
    if published is not None:
        page["published_at"] = published.isoformat()
        page["published_at_precision"] = published.precision
    else:
        page["published_at_precision"] = "unknown"

    if cutoff is not None:
        status = cutoff_status(published, cutoff)
        page["as_of"] = cutoff.isoformat()
        page["as_of_status"] = status
        if status != "accepted":
            messages = {
                "future": "网页发布时间晚于模拟截止时间，正文已由宿主隐藏。",
                "unknown": "网页缺少可靠发布时间，正文不能进入点时模拟。",
                "ambiguous": "网页只有日期、没有具体时间，无法证明其在盘中截止时间前发布。",
            }
            codes = {
                "future": "WEB_FETCH_AFTER_CUTOFF",
                "unknown": "WEB_FETCH_TIME_UNKNOWN",
                "ambiguous": "WEB_FETCH_TIME_AMBIGUOUS",
            }
            return _error_result(messages[status], codes[status], normalized_url, attempts, page=page)
    else:
        page["as_of"] = ""
        page["as_of_status"] = "not_configured"

    if not page["data_quality"]["usable"] and (browser_enabled or cutoff is not None):
        return _error_result(
            "网页已响应，但正文质量不足，不能作为研究证据。",
            "WEB_FETCH_LOW_QUALITY",
            normalized_url,
            attempts,
            page=page,
        )
    return _success_result(page)


def _extract_page(url: str, payload: str, content_type: str, charset: str, engine: str) -> dict[str, Any]:
    parser = _PageParser()
    try:
        parser.feed(payload)
        parser.close()
    except Exception as error:
        raise WebFetchError(f"网页正文解析失败：{error}", "WEB_FETCH_PARSE_ERROR") from error
    title, text, published_at, author, extraction_method = parser.result()
    issues = _quality_issues(title, text, extraction_method)
    score = _quality_score(title, text, issues, extraction_method)
    return {
        "url": url,
        "source": urllib.parse.urlsplit(url).netloc.lower(),
        "title": title,
        "published_at": published_at,
        "published_at_source": parser.published_at_source,
        "author": author,
        "content_type": content_type,
        "charset": charset,
        "content": text[:MAX_TEXT_CHARS],
        "content_truncated": len(text) > MAX_TEXT_CHARS,
        "engine_used": engine,
        "extraction_method": extraction_method,
        "data_quality": {
            "usable": not issues,
            "score": score,
            "issues": issues,
            "content_chars": len(text),
        },
    }


def _validate_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise WebFetchError("url 必须是非空字符串。", "WEB_INVALID_ARGUMENT")
    if len(url.strip()) > MAX_URL_LENGTH:
        raise WebFetchError(f"url 长度不能超过 {MAX_URL_LENGTH} 个字符。", "WEB_INVALID_ARGUMENT")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WebFetchError("url 必须是完整的 http 或 https 地址。", "WEB_INVALID_ARGUMENT")
    if parsed.username or parsed.password:
        raise WebFetchError("url 不允许包含用户名或密码。", "WEB_INVALID_ARGUMENT")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _http_get(url: str, *, timeout: float) -> tuple[str, str, str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_FETCH_BYTES + 1)
            content_type = response.headers.get_content_type()
            declared_charset = response.headers.get_content_charset()
            final_url = response.geturl()
    except urllib.error.HTTPError as error:
        code = "WEB_FETCH_BLOCKED" if error.code in {403, 429, 451, 503} else "WEB_FETCH_HTTP_ERROR"
        raise WebFetchError(f"网页返回 HTTP {error.code}。", code) from error
    except (urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise WebFetchError(f"网页网络请求失败：{reason}", "WEB_FETCH_NETWORK_ERROR") from error
    if len(raw) > MAX_FETCH_BYTES:
        raise WebFetchError(
            f"网页响应超过 {MAX_FETCH_BYTES} 字节上限，未提取正文。",
            "WEB_FETCH_TOO_LARGE",
        )
    charset = _detect_charset(raw, declared_charset)
    return raw.decode(charset, errors="replace"), content_type, charset, final_url


def _detect_charset(raw: bytes, declared: str | None) -> str:
    candidates = [declared, _charset_from_html(raw), "utf-8", "gb18030", "gbk"]
    for charset in dict.fromkeys(item.lower() for item in candidates if item):
        try:
            raw.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
        return charset
    return declared or "utf-8"


def _charset_from_html(raw: bytes) -> str:
    head = raw[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", head, re.IGNORECASE)
    return match.group(1) if match else ""


def _browser_get(url: str, *, timeout: float) -> tuple[str, str]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise WebFetchError(
            "Playwright 未安装；可安装 browser 可选依赖后启用动态页面降级。",
            "WEB_FETCH_BROWSER_UNAVAILABLE",
        ) from error

    with _BROWSER_LOCK:
        context = None
        try:
            global _BROWSER_RUNTIME, _BROWSER
            if _BROWSER is None:
                _BROWSER_RUNTIME = sync_playwright().start()
                _BROWSER = _BROWSER_RUNTIME.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            context = _BROWSER.new_context(user_agent=USER_AGENT, locale="zh-CN")
            page = context.new_page()
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_(),
            )
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            try:
                page.wait_for_selector(ARTICLE_SELECTOR, timeout=2000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(500)
            payload = page.content()
            final_url = page.url
            if len(payload.encode("utf-8")) > MAX_FETCH_BYTES:
                raise WebFetchError(
                    f"浏览器渲染结果超过 {MAX_FETCH_BYTES} 字节上限，未提取正文。",
                    "WEB_FETCH_TOO_LARGE",
                )
            return payload, final_url
        except (PlaywrightError, PlaywrightTimeoutError) as error:
            _stop_browser_unlocked()
            raise WebFetchError(f"浏览器抓取失败：{error}", "WEB_FETCH_BROWSER_ERROR") from error
        finally:
            if context is not None:
                try:
                    context.close()
                except PlaywrightError:
                    pass


def _close_browser() -> None:
    with _BROWSER_LOCK:
        _stop_browser_unlocked()


def _stop_browser_unlocked() -> None:
    global _BROWSER_RUNTIME, _BROWSER
    if _BROWSER is not None:
        try:
            _BROWSER.close()
        except Exception:
            pass
        _BROWSER = None
    if _BROWSER_RUNTIME is not None:
        try:
            _BROWSER_RUNTIME.stop()
        except Exception:
            pass
        _BROWSER_RUNTIME = None


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._main_depth = 0
        self._open_tags: list[tuple[str, bool]] = []
        self._all_parts: list[str] = []
        self._main_parts: list[str] = []
        self._title_parts: list[str] = []
        self._h1_parts: list[str] = []
        self._json_ld_parts: list[str] = []
        self._in_title = False
        self._in_h1 = False
        self._in_json_ld = False
        self.published_at = ""
        self.published_at_source = ""
        self.author = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag == "script" and attributes.get("type", "").lower() == "application/ld+json":
            self._in_json_ld = True
        elif tag in {"script", "style", "noscript", "template", "svg", "nav", "footer", "aside"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self._in_h1 = True
        enters_main = _is_main_container(tag, attributes)
        if tag not in VOID_TAGS:
            self._open_tags.append((tag, enters_main))
        if enters_main:
            self._main_depth += 1
        if tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content", "").strip()
            if key in {
                "article:published_time",
                "article:published",
                "publishdate",
                "pubdate",
                "date",
                "datepublished",
                "publish_time",
                "weibo:article:create_at",
            } and not self.published_at:
                self.published_at = content
                self.published_at_source = f"meta:{key}"
            if key in {"author", "article:author", "byl"} and not self.author:
                self.author = content
        if tag == "time" and not self.published_at:
            self.published_at = attributes.get("datetime", "").strip()
            if self.published_at:
                self.published_at_source = "time:datetime"
        if tag in {"p", "br", "div", "section"}:
            self._append_break()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag == "h1":
            self._in_h1 = False
        if tag == "script" and self._in_json_ld:
            self._in_json_ld = False
        elif tag in {"script", "style", "noscript", "template", "svg", "nav", "footer", "aside"} and self._skip_depth:
            self._skip_depth -= 1
        for index in range(len(self._open_tags) - 1, -1, -1):
            if self._open_tags[index][0] != tag:
                continue
            closed = self._open_tags[index:]
            del self._open_tags[index:]
            self._main_depth -= sum(1 for _name, entered_main in closed if entered_main)
            break
        if tag in {"p", "br", "div", "section"}:
            self._append_break()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_ld_parts.append(data)
            return
        if self._in_title:
            self._title_parts.append(data)
        if self._in_h1:
            self._h1_parts.append(data)
        if self._skip_depth:
            return
        self._all_parts.append(data)
        if self._main_depth:
            self._main_parts.append(data)

    def result(self) -> tuple[str, str, str, str, str]:
        self._read_json_ld()
        title = _clean_text(" ".join(self._h1_parts)) or _clean_text(" ".join(self._title_parts))
        main_text = _clean_text(" ".join(self._main_parts))
        all_text = _clean_text(" ".join(self._all_parts))
        has_article_body = len(main_text) >= MIN_USEFUL_CONTENT_CHARS
        text = main_text if has_article_body else all_text
        if not self.published_at:
            self.published_at = _find_published_date(all_text[:2000])
            if self.published_at:
                self.published_at_source = "text:publication_label"
        extraction_method = "article_container" if has_article_body else "full_page"
        return title, text, self.published_at, self.author, extraction_method

    def _read_json_ld(self) -> None:
        for raw in self._json_ld_parts:
            try:
                data = json.loads(raw.strip())
            except (json.JSONDecodeError, TypeError):
                continue
            for item in _json_objects(data):
                if not self.published_at and isinstance(item.get("datePublished"), str):
                    self.published_at = item["datePublished"].strip()
                    self.published_at_source = "json_ld:datePublished"
                if not self.author:
                    author = item.get("author")
                    if isinstance(author, dict):
                        self.author = str(author.get("name") or "").strip()
                    elif isinstance(author, str):
                        self.author = author.strip()

    def _append_break(self) -> None:
        if not self._skip_depth:
            self._all_parts.append("\n")
            if self._main_depth:
                self._main_parts.append("\n")


def _json_objects(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        graph = value.get("@graph")
        return [value, *[item for item in graph or [] if isinstance(item, dict)]]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _is_main_container(tag: str, attrs: dict[str, str]) -> bool:
    if tag in {"article", "main"}:
        return True
    identity = f"{attrs.get('id', '')} {attrs.get('class', '')}".lower()
    return tag in {"div", "section"} and any(hint in identity for hint in CONTENT_HINTS)


def _find_published_date(text: str) -> str:
    date_pattern = (
        r"(?<!\d)(20\d{2}[-年/]\d{1,2}[-月/]\d{1,2}(?:日)?"
        r"(?:[ T]+\d{1,2}:\d{2}(?::\d{2})?)?)(?!\d)"
    )
    for pattern in (
        rf"(?:发布时间|发布日期|发布于|时间)[：:\s]*{date_pattern}",
        rf"{date_pattern}(?=\s*(?:来源|作者|编辑)[：:])",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def _quality_issues(title: str, text: str, extraction_method: str) -> list[str]:
    issues: list[str] = []
    lowered = f"{title} {text[:1000]}".lower()
    if any(marker in lowered for marker in SOFT_BLOCK_MARKERS):
        issues.append("soft_blocked")
    if not title:
        issues.append("missing_title")
    if len(text) < MIN_USEFUL_CONTENT_CHARS:
        issues.append("content_too_short")
    if extraction_method == "full_page":
        issues.append("missing_article_body")
    replacement_ratio = text.count("�") / max(1, len(text))
    if replacement_ratio > 0.01:
        issues.append("encoding_corruption")
    return issues


def _quality_score(title: str, text: str, issues: list[str], extraction_method: str) -> float:
    score = 0.2 if title else 0.0
    score += min(len(text) / 2000, 1.0) * 0.45
    paragraph_count = text.count("\n")
    score += min(paragraph_count / 8, 1.0) * 0.1
    chinese_ratio = len(re.findall(r"[\u4e00-\u9fff]", text)) / max(1, len(text))
    score += min(chinese_ratio / 0.5, 1.0) * 0.15
    if extraction_method == "article_container":
        score += 0.1
    if issues:
        score -= 0.35
    return round(max(0.0, min(score, 1.0)), 3)


def _merge_page_metadata(target: dict[str, Any], source: dict[str, Any]) -> None:
    """浏览器用于补正文，HTTP 解析出的原始页面元数据优先保留。"""
    for key in ("published_at", "published_at_source", "author"):
        if source.get(key):
            target[key] = source[key]


def _quality_detail(page: dict[str, Any]) -> str:
    quality = page["data_quality"]
    issues = ",".join(quality["issues"]) or "none"
    return f"score={quality['score']}; chars={quality['content_chars']}; issues={issues}"


def _clean_text(value: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in html.unescape(value).splitlines()]
    return "\n".join(line for line in lines if line)


def _attempt_status(code: str) -> str:
    if code in {"WEB_FETCH_BLOCKED", "WEB_FETCH_BROWSER_UNAVAILABLE"}:
        return "blocked"
    if code in {"WEB_FETCH_NETWORK_ERROR", "WEB_FETCH_BROWSER_ERROR"}:
        return "network_error"
    if code == "WEB_FETCH_PARSE_ERROR":
        return "parse_error"
    return "http_error"


def _format_observation(page: dict[str, Any]) -> str:
    metadata = [
        f"标题：{page['title'] or 'unknown'}",
        f"来源：{page['source'] or 'unknown'}",
        f"发布时间：{page['published_at'] or 'unknown'}",
        f"抓取引擎：{page['engine_used']}",
        f"内容质量：{page['data_quality']['score']}",
    ]
    return "\n".join(metadata) + "\n\n正文：\n" + page["content"]


def _success_result(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "stdout": _format_observation(page),
        "stderr": "",
        "status": "success",
        "returncode": 0,
        "exit_code": 0,
        "timed_out": False,
        "signal": None,
        "termination": None,
        "stdout_truncated": page["content_truncated"],
        "stderr_truncated": False,
        "stdout_spill_path": None,
        "stderr_spill_path": None,
        "path": page["url"],
        "operation": "web_fetch",
        "content_hash": None,
        "exception_info": "",
        "extra": {"page": page},
    }


def _error_result(
    message: str,
    code: str,
    url: str,
    attempts: list[dict[str, str]],
    *,
    page: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extra: dict[str, Any] = {"error_code": code, "url": url, "attempts": attempts}
    if page is not None:
        if code in {
            "WEB_FETCH_AFTER_CUTOFF",
            "WEB_FETCH_TIME_UNKNOWN",
            "WEB_FETCH_TIME_AMBIGUOUS",
        }:
            allowed = {
                "source",
                "published_at",
                "published_at_precision",
                "published_at_source",
                "as_of",
                "as_of_status",
                "engine_used",
                "extraction_method",
                "data_quality",
                "attempts",
            }
            safe_page = {key: value for key, value in page.items() if key in allowed}
        else:
            safe_page = {key: value for key, value in page.items() if key != "content"}
        extra["page"] = safe_page
    return {
        "stdout": "",
        "stderr": message,
        "status": "error",
        "returncode": -1,
        "exit_code": None,
        "timed_out": False,
        "signal": None,
        "termination": None,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "stdout_spill_path": None,
        "stderr_spill_path": None,
        "path": url,
        "operation": "web_fetch",
        "content_hash": None,
        "exception_info": message,
        "extra": extra,
    }


_BROWSER_LOCK = threading.Lock()
_BROWSER_RUNTIME: Any = None
_BROWSER: Any = None
atexit.register(_close_browser)
