"""仓库自带的网页正文抓取能力。"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

MAX_FETCH_BYTES = 1_000_000
MAX_TEXT_CHARS = 12_000
MAX_URL_LENGTH = 2_000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class WebFetchError(RuntimeError):
    """网页地址、网络请求或正文解析失败。"""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def execute_web_fetch(url: str, *, timeout: float) -> dict[str, Any]:
    """抓取一个网页并提取标题、发布时间和正文文本。"""
    try:
        normalized_url = _validate_url(url)
        payload, content_type, charset = _http_get(normalized_url, timeout=max(1.0, min(float(timeout), 15.0)))
        parser = _PageParser()
        try:
            parser.feed(payload)
            parser.close()
        except Exception as parse_error:
            raise WebFetchError(f"网页正文解析失败：{parse_error}", "WEB_FETCH_PARSE_ERROR") from parse_error
        text = parser.text()
        if not text:
            raise WebFetchError("网页响应成功，但没有提取到可读正文。", "WEB_FETCH_EMPTY")
        result = {
            "url": normalized_url,
            "title": parser.title,
            "published_at": parser.published_at or _find_date(text),
            "content_type": content_type,
            "charset": charset,
            "content": text[:MAX_TEXT_CHARS],
            "content_truncated": len(text) > MAX_TEXT_CHARS,
        }
        return _success_result(result)
    except WebFetchError as error:
        return _error_result(str(error), error.code, url)
    except Exception as error:  # pragma: no cover - defensive host boundary
        return _error_result(f"网页抓取失败：{error}", "WEB_FETCH_ERROR", url)


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


def _http_get(url: str, *, timeout: float) -> tuple[str, str, str]:
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
            charset = response.headers.get_content_charset() or "utf-8"
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
    return raw.decode(charset, errors="replace"), content_type, charset


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self.title = ""
        self.published_at = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key in {
                "article:published_time",
                "article:published",
                "publishdate",
                "pubdate",
                "date",
                "datepublished",
                "publish_time",
            }:
                self.published_at = attributes.get("content", "").strip()
        if tag == "time" and not self.published_at:
            self.published_at = attributes.get("datetime", "").strip()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in {"script", "style", "noscript", "template", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        self.title = _clean_text(" ".join(self._title_parts))
        return _clean_text(" ".join(self._parts))


def _find_date(text: str) -> str:
    match = re.search(r"(?<!\d)(20\d{2}[-年/]\d{1,2}[-月/]\d{1,2}(?:日)?)(?!\d)", text)
    return match.group(1) if match else ""


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _success_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "stdout": result["content"],
        "stderr": "",
        "status": "success",
        "returncode": 0,
        "exit_code": 0,
        "timed_out": False,
        "signal": None,
        "termination": None,
        "stdout_truncated": result["content_truncated"],
        "stderr_truncated": False,
        "stdout_spill_path": None,
        "stderr_spill_path": None,
        "path": result["url"],
        "operation": "web_fetch",
        "content_hash": None,
        "exception_info": "",
        "extra": {"page": result},
    }


def _error_result(message: str, code: str, url: str) -> dict[str, Any]:
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
        "extra": {"error_code": code, "url": url},
    }
