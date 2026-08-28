"""仓库自带的零 Key 多引擎网页搜索。"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

DEFAULT_MAX_QUERIES = 4
DEFAULT_MAX_RESULTS = 8
DEFAULT_SEARCH_ENGINES = ("bing_rss", "baidu_html", "sogou_html", "duckduckgo")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class WebSearchError(RuntimeError):
    """搜索参数或搜索引擎执行失败。"""

    def __init__(self, message: str, code: str = "WEB_SEARCH_ERROR"):
        super().__init__(message)
        self.code = code


class EngineFailure(RuntimeError):
    """保留单个搜索引擎的失败类型，供 Agent 判断是否应该重试。"""

    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str
    source_engine: str
    fetched_at: str


@dataclass(frozen=True)
class SearchAttempt:
    query: str
    engine: str
    status: str
    result_count: int
    detail: str = ""


SearchFunction = Callable[[str, float, int], list[SearchResult]]


def execute_web_search(
    queries: list[str],
    *,
    timeout: float,
    engines: list[str] | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """并发调用仓库内置搜索引擎并转换为 Agent 工具结果。"""
    try:
        _validate_queries(queries, DEFAULT_MAX_QUERIES)
        selected_engines = list(dict.fromkeys(engines or DEFAULT_SEARCH_ENGINES))
        unknown_engines = [name for name in selected_engines if name not in ENGINE_SEARCHERS]
        if unknown_engines:
            raise WebSearchError(
                f"未知网页搜索引擎：{', '.join(unknown_engines)}。",
                "WEB_INVALID_ARGUMENT",
            )

        sources: list[dict[str, str]] = []
        attempts: list[SearchAttempt] = []
        seen_urls: set[str] = set()
        request_timeout = max(1.0, min(float(timeout), 10.0))
        for query in dict.fromkeys(item.strip() for item in queries):
            engine_results, query_attempts = _search_query(
                query,
                selected_engines,
                timeout=request_timeout,
                limit=max_results,
            )
            attempts.extend(query_attempts)
            for result in engine_results:
                normalized_url = _normalize_url(result.url)
                if not normalized_url or normalized_url in seen_urls:
                    continue
                seen_urls.add(normalized_url)
                source = asdict(result)
                source["url"] = normalized_url
                sources.append(source)
                if len(sources) >= max_results:
                    break
            if len(sources) >= max_results:
                break

        attempt_dicts = [asdict(attempt) for attempt in attempts]
        if sources:
            return _success_result(_format_sources(sources, attempts), sources, attempt_dicts)
        if attempts and all(attempt.status not in {"success", "empty"} for attempt in attempts):
            message = "网页搜索引擎均不可用。" + _format_diagnostics(attempts)
            return _error_result(message, "WEB_SEARCH_UNAVAILABLE", attempt_dicts)
        return _success_result(_format_sources([], attempts), [], attempt_dicts)
    except WebSearchError as error:
        return _error_result(str(error), error.code, [])
    except Exception as error:  # pragma: no cover - defensive host boundary
        return _error_result(f"网页搜索失败：{error}", "WEB_SEARCH_ERROR", [])


def _search_query(
    query: str,
    engines: list[str],
    *,
    timeout: float,
    limit: int,
) -> tuple[list[SearchResult], list[SearchAttempt]]:
    by_engine: dict[str, tuple[list[SearchResult], SearchAttempt]] = {}
    with ThreadPoolExecutor(max_workers=len(engines)) as executor:
        futures = {
            executor.submit(_run_engine, name, query, timeout, limit): name for name in engines
        }
        for future in as_completed(futures):
            name = futures[future]
            by_engine[name] = future.result()

    results: list[SearchResult] = []
    attempts: list[SearchAttempt] = []
    for name in engines:
        _, attempt = by_engine[name]
        attempts.append(attempt)
    # 按排名轮询各引擎，避免首个引擎独占返回条数。
    max_engine_results = max((len(by_engine[name][0]) for name in engines), default=0)
    for rank in range(max_engine_results):
        for name in engines:
            engine_results, _ = by_engine[name]
            if rank < len(engine_results):
                results.append(engine_results[rank])
    return results, attempts


def _run_engine(
    engine: str,
    query: str,
    timeout: float,
    limit: int,
) -> tuple[list[SearchResult], SearchAttempt]:
    try:
        results = ENGINE_SEARCHERS[engine](query, timeout, limit)
        status = "success" if results else "empty"
        detail = "" if results else "请求成功，但没有解析到结果"
        return results, SearchAttempt(query, engine, status, len(results), detail)
    except EngineFailure as error:
        return [], SearchAttempt(query, engine, error.status, 0, str(error))
    except Exception as error:  # pragma: no cover - isolate third-party endpoint changes
        return [], SearchAttempt(query, engine, "parse_error", 0, f"{type(error).__name__}: {error}")


def _search_bing_rss(query: str, timeout: float, limit: int) -> list[SearchResult]:
    url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "format": "rss", "setlang": "zh-Hans"}
    )
    payload = _http_get(url, timeout=timeout, accept="application/rss+xml,application/xml")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise EngineFailure("parse_error", f"RSS XML 无法解析：{error}") from error

    fetched_at = _now()
    results: list[SearchResult] = []
    for item in root.findall(".//item"):
        result_url = (item.findtext("link") or "").strip()
        title = _clean_text(item.findtext("title") or "")
        if not result_url or not title:
            continue
        results.append(
            SearchResult(
                url=result_url,
                title=title,
                snippet=_clean_text(item.findtext("description") or ""),
                source_engine="bing_rss",
                fetched_at=fetched_at,
            )
        )
        if len(results) >= limit:
            break
    return results


def _search_baidu_html(query: str, timeout: float, limit: int) -> list[SearchResult]:
    url = "https://www.baidu.com/s?" + urllib.parse.urlencode(
        {"wd": query, "rn": limit, "ie": "utf-8"}
    )
    payload = _http_get(url, timeout=timeout, referer="https://www.baidu.com/")
    pattern = re.compile(
        r'<h3[^>]*>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>\s*</h3>',
        re.DOTALL | re.IGNORECASE,
    )
    snippets = _extract_snippets(
        payload,
        r'<div[^>]+class=["\'][^"\']*(?:c-abstract|content-right_)[^"\']*["\'][^>]*>(.*?)</div>',
    )
    return _html_results(payload, pattern, snippets, "baidu_html", limit)


def _search_sogou_html(query: str, timeout: float, limit: int) -> list[SearchResult]:
    url = "https://www.sogou.com/web?" + urllib.parse.urlencode({"query": query})
    payload = _http_get(url, timeout=timeout, referer="https://www.sogou.com/")
    pattern = re.compile(
        r'<h3[^>]*class=["\'][^"\']*vr-title[^"\']*["\'][^>]*>\s*'
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    snippets = _extract_snippets(
        payload,
        r'<p[^>]+class=["\'][^"\']*(?:star-wiki|txt-info|space-txt)[^"\']*["\'][^>]*>(.*?)</p>',
    )
    return _html_results(payload, pattern, snippets, "sogou_html", limit)


def _search_duckduckgo(query: str, timeout: float, limit: int) -> list[SearchResult]:
    body = urllib.parse.urlencode({"q": query}).encode()
    payload = _http_get(
        "https://html.duckduckgo.com/html/",
        timeout=timeout,
        data=body,
        accept="text/html,application/xhtml+xml",
    )
    pattern = re.compile(
        r'<a[^>]+class=["\'][^"\']*result__a[^"\']*["\'][^>]+href=["\']([^"\']+)["\'][^>]*>'
        r"(.*?)</a>",
        re.DOTALL | re.IGNORECASE,
    )
    snippets = _extract_snippets(
        payload,
        r'<a[^>]+class=["\'][^"\']*result__snippet[^"\']*["\'][^>]*>(.*?)</a>',
    )
    results = _html_results(payload, pattern, snippets, "duckduckgo", limit)
    return [
        SearchResult(
            url=_resolve_duckduckgo_url(result.url),
            title=result.title,
            snippet=result.snippet,
            source_engine=result.source_engine,
            fetched_at=result.fetched_at,
        )
        for result in results
    ]


ENGINE_SEARCHERS: dict[str, SearchFunction] = {
    "bing_rss": _search_bing_rss,
    "baidu_html": _search_baidu_html,
    "sogou_html": _search_sogou_html,
    "duckduckgo": _search_duckduckgo,
}


def _http_get(
    url: str,
    *,
    timeout: float,
    data: bytes | None = None,
    accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    referer: str = "",
) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as error:
        status = "blocked" if error.code in {403, 429, 503} else "http_error"
        raise EngineFailure(status, f"HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise EngineFailure("network_error", str(reason)) from error

    text = raw.decode(charset, errors="replace")
    lower_text = text.lower()
    block_markers = ("captcha", "安全验证", "访问过于频繁", "unusual traffic", "verify you are human")
    if any(marker in lower_text for marker in block_markers):
        raise EngineFailure("blocked", "响应为验证或限流页面")
    return text


def _html_results(
    payload: str,
    pattern: re.Pattern[str],
    snippets: list[str],
    engine: str,
    limit: int,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    fetched_at = _now()
    for index, match in enumerate(pattern.finditer(payload)):
        if len(results) >= limit:
            break
        url = html.unescape(match.group(1)).strip()
        title = _clean_text(match.group(2))
        if not url or not title:
            continue
        if url.startswith("/"):
            host = {"baidu_html": "https://www.baidu.com", "sogou_html": "https://www.sogou.com"}.get(
                engine, ""
            )
            url = urllib.parse.urljoin(host, url)
        results.append(
            SearchResult(
                url=url,
                title=title,
                snippet=snippets[index] if index < len(snippets) else "",
                source_engine=engine,
                fetched_at=fetched_at,
            )
        )
    if not results and not _is_no_results_page(payload):
        raise EngineFailure("parse_error", "搜索页有响应，但结果结构无法解析")
    return results


def _extract_snippets(payload: str, pattern: str) -> list[str]:
    return [
        _clean_text(match.group(1))
        for match in re.finditer(pattern, payload, re.DOTALL | re.IGNORECASE)
    ]


def _clean_text(value: str) -> str:
    without_tags = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", value, flags=re.DOTALL | re.IGNORECASE)
    without_tags = re.sub(r"<[^>]+>", " ", without_tags)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _resolve_duckduckgo_url(url: str) -> str:
    parsed = urllib.parse.urlparse(html.unescape(url))
    target = urllib.parse.parse_qs(parsed.query).get("uddg")
    return target[0] if target else html.unescape(url)


def _normalize_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(html.unescape(url))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = urllib.parse.urlencode(
            [(key, value) for key, value in params if not key.startswith("utm_") and key not in {"ref", "source"}]
        )
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", query, "")
        )
    except ValueError:
        return ""


def _is_no_results_page(payload: str) -> bool:
    lower_text = _clean_text(payload).lower()
    markers = ("没有找到", "未找到相关结果", "no results", "did not match any documents")
    return any(marker in lower_text for marker in markers)


def _validate_queries(queries: list[str], max_queries: int) -> None:
    if not isinstance(queries, list) or not queries:
        raise WebSearchError("queries 必须是至少包含一个字符串的数组。", "WEB_INVALID_ARGUMENT")
    if len(queries) > max_queries:
        raise WebSearchError(f"queries 最多包含 {max_queries} 个查询。", "WEB_INVALID_ARGUMENT")
    if any(not isinstance(query, str) or not query.strip() for query in queries):
        raise WebSearchError("queries 中每一项都必须是非空字符串。", "WEB_INVALID_ARGUMENT")


def _format_sources(sources: list[dict[str, str]], attempts: list[SearchAttempt]) -> str:
    if not sources:
        return "未找到搜索结果。" + _format_diagnostics(attempts)
    lines = ["搜索结果："]
    for index, source in enumerate(sources, 1):
        title = source["title"] or source["url"]
        details = " | ".join(
            value
            for value in (source["snippet"], source["source_engine"], source["fetched_at"])
            if value
        )
        lines.append(f"{index}. [{title}]({source['url']})" + (f" - {details}" if details else ""))
    failed_attempts = [attempt for attempt in attempts if attempt.status not in {"success", "empty"}]
    if failed_attempts:
        lines.append(_format_diagnostics(failed_attempts).lstrip())
    lines.append("请在最终答复中引用相关 URL，并自行核验内容和发布时间。")
    return "\n".join(lines)


def _format_diagnostics(attempts: list[SearchAttempt]) -> str:
    if not attempts:
        return ""
    summaries = []
    for attempt in attempts:
        summary = f"{attempt.engine}={attempt.status}"
        if attempt.detail:
            summary += f"({attempt.detail})"
        summaries.append(summary)
    return " 引擎诊断：" + "；".join(summaries) + "。不要用相近关键词反复重试不可用的引擎。"


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _success_result(
    text: str,
    sources: list[dict[str, str]],
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "stdout": text,
        "stderr": "",
        "status": "success",
        "returncode": 0,
        "exit_code": 0,
        "timed_out": False,
        "signal": None,
        "termination": None,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "stdout_spill_path": None,
        "stderr_spill_path": None,
        "path": None,
        "operation": "web_search",
        "content_hash": None,
        "exception_info": "",
        "extra": {"sources": sources, "attempts": attempts},
    }


def _error_result(message: str, code: str, attempts: list[dict[str, Any]]) -> dict[str, Any]:
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
        "path": None,
        "operation": "web_search",
        "content_hash": None,
        "exception_info": message,
        "extra": {"error_code": code, "attempts": attempts},
    }
