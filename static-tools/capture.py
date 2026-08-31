import argparse
import html as html_lib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

try:
    import requests
except ModuleNotFoundError:
    requests = None

try:
    from bs4 import BeautifulSoup
except ModuleNotFoundError:
    BeautifulSoup = None


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

DEFAULT_PUBLIC_SOURCE_URLS = [
    "https://legado.aoaostar.com/sources/71e56d4f.json",
    "https://legado.aoaostar.com/sources/2a1f129b.json",
    "http://www.yckceo.com/yuedu/shuyuans/json/id/728.json",
    "http://sy.legado1.top/sy.php/806fd615465bd0445ef77246911d1b0b.json",
]

DEFAULT_PUBLIC_SOURCE_INDEX_URLS = [
    "https://legado.aoaostar.com/",
    "https://legado.aoaostar.com/sources/",
    "http://www.yckceo.com/yuedu/shuyuan/index.html",
]

ESSENTIAL_RULE_KEYS = ("ruleSearch", "ruleBookInfo", "ruleToc", "ruleContent")
SOURCE_LIST_KEYS = ("sources", "bookSources", "passedSources", "list", "data")
RULE_GROUP_KEYS = ("ruleSearch", "ruleExplore", "ruleBookInfo", "ruleToc", "ruleContent")
PLACEHOLDER_RULE_TEXT = {
    "book list rule",
    "book name rule",
    "author rule",
    "cover rule",
    "detail url rule",
    "intro rule",
    "kind rule",
    "latest chapter rule",
    "word count rule",
    "toc url rule",
    "chapter list rule",
    "chapter name rule",
    "chapter url rule",
    "next toc rule",
    "content rule",
    "next content rule",
    "search url rule",
    "书籍列表规则",
    "书名规则",
    "作者规则",
    "封面规则",
    "详情页URL规则",
    "简介规则",
    "分类规则",
    "最新章节规则",
    "字数规则",
    "目录URL规则",
    "章节列表规则",
    "章节名称规则",
    "章节URL规则",
    "目录下一页规则",
    "正文内容规则",
    "正文下一页规则",
    "搜索URL规则",
}
RISKY_RULE_MARKERS = ("<js>", "@js:", "##", "{'webView': true}", '{"webView": true}', "java.")


@dataclass
class SearchRule:
    init: Optional[str] = None
    bookList: Optional[str] = None
    name: Optional[str] = None
    author: Optional[str] = None
    intro: Optional[str] = None
    kind: Optional[str] = None
    lastChapter: Optional[str] = None
    updateTime: Optional[str] = None
    bookUrl: Optional[str] = None
    coverUrl: Optional[str] = None
    wordCount: Optional[str] = None
    checkKeyWord: Optional[str] = None


@dataclass
class ExploreRule:
    bookList: Optional[str] = None
    name: Optional[str] = None
    author: Optional[str] = None
    intro: Optional[str] = None
    kind: Optional[str] = None
    lastChapter: Optional[str] = None
    updateTime: Optional[str] = None
    bookUrl: Optional[str] = None
    coverUrl: Optional[str] = None
    wordCount: Optional[str] = None


@dataclass
class BookInfoRule:
    init: Optional[str] = None
    name: Optional[str] = None
    author: Optional[str] = None
    intro: Optional[str] = None
    kind: Optional[str] = None
    lastChapter: Optional[str] = None
    updateTime: Optional[str] = None
    coverUrl: Optional[str] = None
    tocUrl: Optional[str] = None
    wordCount: Optional[str] = None
    isEnd: Optional[str] = None
    canReToc: Optional[str] = None
    canReName: Optional[str] = None


@dataclass
class TocRule:
    chapterList: Optional[str] = None
    chapterName: Optional[str] = None
    chapterUrl: Optional[str] = None
    isVolume: Optional[str] = None
    isVip: Optional[str] = None
    isPay: Optional[str] = None
    updateTime: Optional[str] = None
    formatJs: Optional[str] = None
    preUpdateJs: Optional[str] = None
    nextTocUrl: Optional[str] = None


@dataclass
class ContentRule:
    content: Optional[str] = None
    title: Optional[str] = None
    nextContentUrl: Optional[str] = None
    webJs: Optional[str] = None
    sourceRegex: Optional[str] = None
    replaceRegex: Optional[str] = None
    imageStyle: Optional[str] = None
    imageDecode: Optional[str] = None
    payAction: Optional[str] = None


@dataclass
class BookSource:
    bookSourceUrl: str
    bookSourceName: str
    bookSourceType: int = 0
    enabled: bool = True
    enabledExplore: bool = False
    searchUrl: Optional[str] = None
    exploreUrl: Optional[str] = None
    ruleSearch: Optional[SearchRule] = None
    ruleExplore: Optional[ExploreRule] = None
    ruleBookInfo: Optional[BookInfoRule] = None
    ruleToc: Optional[TocRule] = None
    ruleContent: Optional[ContentRule] = None
    bookSourceComment: Optional[str] = None


@dataclass
class StageLog:
    stage: str
    url: str
    attempts: int
    success: bool
    statusCode: Optional[int] = None
    durationMs: int = 0
    error: Optional[str] = None


class AutoBuilder:
    def __init__(self, timeout: int = 20, retries: int = 2, retry_backoff: float = 0.7) -> None:
        if requests is None or BeautifulSoup is None:
            raise RuntimeError("network capture requires dependencies from requirements.txt: requests, beautifulsoup4, lxml")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})
        self.timeout = timeout
        self.retries = max(0, retries)
        self.retry_backoff = max(0.1, retry_backoff)
        self.stage_logs: List[StageLog] = []
        self.site_patterns: Dict[str, Dict[str, str]] = {
            "biquge": {
                "ruleToc.chapterList": "div#list dl dd, .listmain dd, .chapterlist li",
                "ruleContent.content": "div#content@html"
            },
            "qidian": {
                "ruleToc.chapterList": "ul.cf li, .catalog-content-wrap li",
                "ruleContent.content": ".read-content@html"
            },
            "17k": {
                "ruleToc.chapterList": ".Volume a, .chapter ul li",
                "ruleContent.content": ".p@html, .readAreaBox@html"
            }
        }

    def reset_stage_logs(self) -> None:
        self.stage_logs = []

    def fetch(self, url: str, stage: str) -> str:
        return self.fetch_request(url=url, stage=stage)

    def fetch_request(
        self,
        url: str,
        stage: str,
        method: str = "GET",
        body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        charset: Optional[str] = None,
    ) -> str:
        last_error = None
        attempts = self.retries + 1
        start = time.time()
        method = (method or "GET").upper()
        for i in range(attempts):
            try:
                resp = self.session.request(
                    method,
                    url,
                    data=body if method != "GET" else None,
                    headers=headers or None,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                resp.encoding = charset or resp.apparent_encoding or resp.encoding or "utf-8"
                duration_ms = int((time.time() - start) * 1000)
                self.stage_logs.append(
                    StageLog(
                        stage=stage,
                        url=url,
                        attempts=i + 1,
                        success=True,
                        statusCode=resp.status_code,
                        durationMs=duration_ms,
                    )
                )
                return resp.text
            except Exception as e:
                last_error = e
                if i < attempts - 1:
                    time.sleep(self.retry_backoff * (i + 1))

        duration_ms = int((time.time() - start) * 1000)
        status_code = None
        if isinstance(last_error, requests.HTTPError) and getattr(last_error, "response", None) is not None:
            status_code = last_error.response.status_code
        self.stage_logs.append(
            StageLog(
                stage=stage,
                url=url,
                attempts=attempts,
                success=False,
                statusCode=status_code,
                durationMs=duration_ms,
                error=str(last_error) if last_error else "unknown error",
            )
        )
        raise last_error if last_error else RuntimeError("fetch failed")

    def split_legado_url_options(self, value: str) -> Tuple[str, Dict[str, Any]]:
        raw = (value or "").strip()
        marker = raw.rfind(",{")
        if marker < 0:
            return raw, {}
        url_part = raw[:marker].strip()
        options_part = raw[marker + 1 :].strip()
        try:
            options = json.loads(options_part)
        except json.JSONDecodeError:
            return raw, {}
        return url_part, options if isinstance(options, dict) else {}

    def fetch_legado_rule_url(self, rule_url: str, stage: str, keyword: str, page: str = "1") -> Tuple[str, str]:
        preview = build_search_preview(rule_url, keyword=keyword, page=page)
        url, options = self.split_legado_url_options(preview)
        method = str(options.get("method") or "GET").upper()
        body: Optional[Any] = options.get("body")
        headers = options.get("headers") if isinstance(options.get("headers"), dict) else None
        charset = str(options.get("charset") or "") or None

        if isinstance(body, dict):
            body = urlencode(body)
        elif isinstance(body, list):
            body = urlencode(body)
        elif isinstance(body, str) and method == "GET" and body:
            query = urlencode(parse_qsl(body, keep_blank_values=True))
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{query}"
            body = None

        return url, self.fetch_request(url=url, stage=stage, method=method, body=body, headers=headers, charset=charset)

    def normalize_base(self, raw: str) -> str:
        value = raw.strip()
        if not value.startswith("http://") and not value.startswith("https://"):
            value = f"https://{value}"
        p = urlparse(value)
        if not p.scheme or not p.netloc:
            raise ValueError("invalid base url")
        return f"{p.scheme}://{p.netloc}"

    def abs_url(self, base: str, maybe_rel: str) -> str:
        return urljoin(base, maybe_rel)

    def infer_search_url(self, home_html: str, base: str) -> str:
        soup = BeautifulSoup(home_html, "lxml")
        forms = soup.select("form")
        for form in forms:
            text = form.get_text(" ", strip=True).lower()
            has_search_input = False
            hidden_params: List[str] = []
            qname = "key"
            for inp in form.select("input"):
                name = (inp.get("name") or "").strip()
                if not name:
                    continue
                input_type = (inp.get("type") or "text").lower()
                value = (inp.get("value") or "").strip()
                if input_type == "hidden":
                    hidden_params.append(f"{name}={value}")
                elif re.search(r"(search|keyword|key|wd|q)", name, re.I):
                    has_search_input = True
                    qname = name

            if "search" in text or "搜索" in text or has_search_input:
                action = form.get("action") or "/search"
                method = (form.get("method") or "GET").upper()
                action_url = self.abs_url(base, action)
                body_parts = [f"{qname}={{key}}"] + hidden_params
                if method == "POST":
                    body = "&".join(body_parts)
                    return f'{action_url},{{"method":"POST","body":"{body}"}}'
                sep = "&" if "?" in action_url else "?"
                query = "&".join(body_parts + ["page={{page}}"])
                return f"{action_url}{sep}{query}"
        return f"{base}/search?keyword={{key}}&page={{page}}"

    def detect_js_redirect(self, html: str, base_url: str) -> Optional[str]:
        if not html or len(html) > 2500:
            return None
        patterns = [
            r"window\\.location(?:\\.href)?\\s*=\\s*['\"]([^'\"]+)['\"]",
            r"location\\.replace\\(\\s*['\"]([^'\"]+)['\"]\\s*\\)",
        ]
        for pattern in patterns:
            m = re.search(pattern, html, re.I)
            if not m:
                continue
            redirect_path = m.group(1).strip()
            if not redirect_path:
                continue
            return self.abs_url(base_url, redirect_path)
        return None

    # ---------- TOC page detection ----------

    def is_toc_page(self, html: str) -> bool:
        """Return True if html looks like a chapter-list (TOC) page."""
        soup = BeautifulSoup(html, "lxml")

        # 1. Count chapter-like links (第X章 / 第X话 / Chapter N)
        chapter_link_count = sum(
            1
            for a in soup.select("a[href]")
            if re.search(r"第.{0,6}[章话节]|chapter\s*\d", a.get_text(" ", strip=True), re.I)
        )
        if chapter_link_count >= 10:
            return True

        # 2. Pagination signals → current page is already listing chapters
        pagination_selectors = [
            "[class*=pagination]",
            "[class*=page-link]",
            "[aria-label=Next]",
            "[class*=next]",
            "[class*=page]",
        ]
        for sel in pagination_selectors:
            if soup.select_one(sel):
                # also check there are at least a few links to avoid header pagers
                all_links = soup.select("a[href]")
                if len(all_links) >= 8:
                    return True
                break

        # 3. Dense link block — any container with ≥20 <a> tags is likely a list
        for container in soup.select("ul, ol, dl, div, section"):
            links = container.find_all("a", href=True, recursive=False)
            if len(links) >= 20:
                return True

        return False

    def find_toc_url_in_detail(self, html: str, base: str) -> Optional[str]:
        """From a detail page, try to find the link to the TOC page."""
        soup = BeautifulSoup(html, "lxml")
        toc_keywords = ["查看完整目录", "完整目录", "全部目录", "章节目录", "全本目录", "目录", "点击阅读", "开始阅读"]
        for kw in toc_keywords:
            tag = soup.find("a", string=re.compile(kw))
            if tag is None:
                tag = soup.find("a", title=re.compile(kw))
            if tag and tag.get("href"):
                return self.abs_url(base, tag["href"])
        # fallback: look for href patterns like /chapters/ /catalog/ /dir/
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if re.search(r"/(chapter|catalog|dir|toc|mulu|list|chapterlist)", href, re.I):
                return self.abs_url(base, href)
        return None

    def find_first_detail_url(self, html: str, base: str) -> Optional[str]:
        soup = BeautifulSoup(html, "lxml")
        base_host = urlparse(base).netloc.lower()
        best_url: Optional[str] = None
        best_score = 0
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            text = a.get_text(" ", strip=True)
            if not href or href.startswith("#") or href.lower().startswith(("javascript:", "mailto:")):
                continue
            absolute = self.abs_url(base, href)
            parsed = urlparse(absolute)
            path = parsed.path.lower()
            text_lower = text.lower()
            score = 0
            if parsed.netloc.lower() != base_host:
                score -= 8
            if re.search(r"/(book|novel|info|detail|xiaoshuo|xs|b)/?\d*", path, re.I):
                score += 8
            if re.search(r"\d{2,}", path):
                score += 2
            if re.search(r"(author|intro|summary|book|novel|\u4f5c\u8005|\u7b80\u4ecb|\u4e66\u540d|\u5c0f\u8bf4)", a.parent.get_text(" ", strip=True) if a.parent else "", re.I):
                score += 3
            if len(text) >= 2 and not re.search(r"(search|login|register|catalog|chapter|\u76ee\u5f55|\u7ae0|\u641c\u7d22|\u767b\u5f55)", text_lower, re.I):
                score += 3
            if re.search(r"(chapter|catalog|mulu|list|read|reader|search|login|register)", path, re.I):
                score -= 5
            if absolute.rstrip("/") == base.rstrip("/"):
                score -= 10
            if score > best_score:
                best_score = score
                best_url = absolute
        return best_url if best_score >= 5 else None

    def find_first_chapter_url(self, html: str, base: str) -> Optional[str]:
        soup = BeautifulSoup(html, "lxml")
        best_url: Optional[str] = None
        best_score = 0
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            text = a.get_text(" ", strip=True)
            if not href or href.startswith("#") or href.lower().startswith(("javascript:", "mailto:")):
                continue
            absolute = self.abs_url(base, href)
            path = urlparse(absolute).path.lower()
            score = 0
            if re.search(r"(\u7b2c.{0,8}[\u7ae0\u8bdd\u8282]|chapter\s*\d+|chapter|read|reader)", text, re.I):
                score += 8
            if re.search(r"(chapter|read|reader|html/\d+|\d+\.html$)", path, re.I):
                score += 5
            if re.search(r"(catalog|mulu|list|search|login|register|book$|novel$)", path, re.I):
                score -= 4
            if len(text) > 80:
                score -= 2
            if score > best_score:
                best_score = score
                best_url = absolute
        return best_url if best_score >= 5 else None

    # ---------- heuristic selectors ----------

    def infer_list_selector(self, soup: BeautifulSoup, candidates: List[str]) -> str:
        best = candidates[0]
        best_score = -1
        for sel in candidates:
            nodes = soup.select(sel)
            if not nodes:
                continue
            a_count = sum(1 for n in nodes if n.select_one("a[href]") is not None)
            score = a_count * 10 + min(len(nodes), 30)
            if score > best_score:
                best_score = score
                best = sel
        return best

    def infer_search_rule(self, html: str) -> SearchRule:
        soup = BeautifulSoup(html, "lxml")
        list_sel = self.infer_list_selector(
            soup,
            [
                ".search-book",
                ".book-item",
                ".bookbox",
                ".novel-item",
                ".result-item",
                ".book-list li",
                "article",
                "li",
            ],
        )
        return SearchRule(
            bookList=list_sel,
            name="a@text",
            bookUrl="a@href",
            author=".author@text",
            intro=".intro@text",
            kind=".tag@text",
            coverUrl="img@src",
        )

    def infer_info_rule(self, html: str) -> BookInfoRule:
        return BookInfoRule(
            name=".book-title@text||h1@text",
            author=".author@text||[class*=author]@text",
            intro=".intro@text||.summary@text",
            kind=".book-tag@text||.category@text",
            coverUrl=".book-cover img@src||img@src",
            tocUrl="a:contains(目录)@href||a[href*=chapter]@href",
        )

    def infer_toc_rule(self, html: str) -> TocRule:
        soup = BeautifulSoup(html, "lxml")
        list_sel = self.infer_list_selector(
            soup,
            [
                ".chapter-list li",
                ".listmain dd",
                ".volume li",
                ".chapter li",
                "dd",
                "li",
            ],
        )
        return TocRule(
            chapterList=list_sel,
            chapterName="a@text",
            chapterUrl="a@href",
            nextTocUrl="a:contains(下一页)@href||a.next@href",
        )

    def infer_content_rule(self, html: str) -> ContentRule:
        soup = BeautifulSoup(html, "lxml")
        candidates = ["#content", ".content", ".article-content", ".chapter-content", ".read-content", "article", "main"]
        best = "#content"
        best_len = -1
        for sel in candidates:
            node = soup.select_one(sel)
            if node is None:
                continue
            l = len(node.get_text(" ", strip=True))
            if l > best_len:
                best_len = l
                best = sel
        return ContentRule(content=f"{best}@html", nextContentUrl="a:contains(下一页)@href||a.next@href")

    def build(
        self,
        base_url: str,
        name: Optional[str] = None,
        search_url: Optional[str] = None,
        detail_url: Optional[str] = None,
        toc_url: Optional[str] = None,
        chapter_url: Optional[str] = None,
        search_keyword: str = "test",
        auto_follow_samples: bool = False,
    ) -> dict:
        self.reset_stage_logs()
        base = self.normalize_base(base_url)
        host = urlparse(base).netloc
        source_name = name or host

        notes: List[str] = []
        home = self.fetch(base, stage="home")
        redirect = self.detect_js_redirect(home, base)
        if redirect:
            try:
                home = self.fetch(redirect, stage="home_js_redirect")
                notes.append(f"首页检测到JS跳转并已跟随: {redirect}")
            except Exception:
                notes.append(f"首页检测到JS跳转但跟随失败: {redirect}")

        inferred_search_url = self.infer_search_url(home, base)
        final_search_url = inferred_search_url
        if search_url:
            search_html = self.fetch(self.abs_url(base, search_url), stage="search_sample")
            notes.append("使用示例搜索页推断")
            final_search_url = self.abs_url(base, search_url)
        elif auto_follow_samples:
            try:
                sample_search_url, search_html = self.fetch_legado_rule_url(
                    inferred_search_url,
                    stage="search_autofollow",
                    keyword=search_keyword,
                    page="1",
                )
                notes.append(f"自动执行搜索样例并推断: {sample_search_url}")
            except Exception as exc:
                search_html = home
                notes.append(f"自动搜索样例失败，使用首页推断: {exc}")
        else:
            search_html = home
            notes.append("未提供示例搜索页，使用首页与表单推断")

        search_rule = self.infer_search_rule(search_html)

        detail_html: Optional[str] = None
        detail_abs: Optional[str] = None
        if detail_url:
            detail_abs = self.abs_url(base, detail_url)
            detail_html = self.fetch(detail_abs, stage="detail_sample")
            info_rule = self.infer_info_rule(detail_html)
            notes.append("使用示例详情页推断")
        elif auto_follow_samples:
            found_detail_url = self.find_first_detail_url(search_html, base)
            if found_detail_url:
                try:
                    detail_abs = found_detail_url
                    detail_html = self.fetch(found_detail_url, stage="detail_autofollow")
                    info_rule = self.infer_info_rule(detail_html)
                    notes.append(f"从搜索结果自动跟随详情页: {found_detail_url}")
                except Exception as exc:
                    info_rule = self.infer_info_rule(home)
                    notes.append(f"详情页自动跟随失败，使用保守详情规则: {exc}")
            else:
                info_rule = self.infer_info_rule(home)
                notes.append("搜索结果未识别到可跟随详情链接，使用保守详情规则")
        else:
            info_rule = self.infer_info_rule(home)
            notes.append("未提供示例详情页，使用保守详情规则")

        toc_html: Optional[str] = None
        toc_abs: Optional[str] = None
        if toc_url:
            toc_abs = self.abs_url(base, toc_url)
            toc_html = self.fetch(toc_abs, stage="toc_sample")
            toc_rule = self.infer_toc_rule(toc_html)
            notes.append("使用示例目录页推断")
        elif detail_html is not None:
            # Auto-detect: check whether detail page IS the toc page, or has a toc link
            detail_html_for_toc = detail_html
            if self.is_toc_page(detail_html_for_toc):
                toc_html = detail_html_for_toc
                toc_abs = detail_abs
                toc_rule = self.infer_toc_rule(detail_html_for_toc)
                notes.append("详情页同时是目录页，直接用于推断目录规则")
            else:
                found_toc_url = self.find_toc_url_in_detail(detail_html_for_toc, base)
                if found_toc_url:
                    try:
                        toc_abs = found_toc_url
                        toc_html = self.fetch(found_toc_url, stage="toc_followlink")
                        toc_rule = self.infer_toc_rule(toc_html)
                        notes.append(f"从详情页跟随目录链接推断: {found_toc_url}")
                    except Exception:
                        toc_rule = TocRule(chapterList=".chapter-list li, .listmain dd, li", chapterName="a@text", chapterUrl="a@href")
                        notes.append(f"目录链接跟随失败，使用保守规则: {found_toc_url}")
                else:
                    toc_rule = TocRule(chapterList=".chapter-list li, .listmain dd, li", chapterName="a@text", chapterUrl="a@href")
                    notes.append("详情页未发现目录链接，使用保守目录规则")
        else:
            toc_rule = TocRule(chapterList=".chapter-list li, .listmain dd, li", chapterName="a@text", chapterUrl="a@href")
            notes.append("未提供示例目录页，使用保守目录规则")

        if chapter_url:
            chapter_html = self.fetch(self.abs_url(base, chapter_url), stage="chapter_sample")
            content_rule = self.infer_content_rule(chapter_html)
            notes.append("使用示例正文页推断")
        elif auto_follow_samples:
            chapter_base = toc_abs or detail_abs or base
            found_chapter_url = self.find_first_chapter_url(toc_html or detail_html or search_html, chapter_base)
            if found_chapter_url:
                try:
                    chapter_html = self.fetch(found_chapter_url, stage="chapter_autofollow")
                    content_rule = self.infer_content_rule(chapter_html)
                    notes.append(f"从目录/详情页自动跟随正文页: {found_chapter_url}")
                except Exception as exc:
                    content_rule = ContentRule(content="#content@html")
                    notes.append(f"正文页自动跟随失败，使用保守正文规则: {exc}")
            else:
                content_rule = ContentRule(content="#content@html")
                notes.append("未识别到可跟随正文链接，使用保守正文规则")
        else:
            content_rule = ContentRule(content="#content@html")
            notes.append("未提供示例正文页，使用保守正文规则")

        source = BookSource(
            bookSourceUrl=base,
            bookSourceName=source_name,
            searchUrl=final_search_url,
            ruleSearch=search_rule,
            ruleBookInfo=info_rule,
            ruleToc=toc_rule,
            ruleContent=content_rule,
            bookSourceComment="auto-generated by booksource-auto-builder",
        )

        self.apply_site_patterns(base, source, notes)

        data = asdict(source)
        data["notes"] = notes
        data["diagnostics"] = [asdict(s) for s in self.stage_logs]
        return data

    def apply_site_patterns(self, base: str, source: BookSource, notes: List[str]) -> None:
        host = (urlparse(base).netloc or "").lower()
        for key, mapping in self.site_patterns.items():
            if key not in host:
                continue
            for field, value in mapping.items():
                if field == "ruleToc.chapterList":
                    if source.ruleToc is None:
                        source.ruleToc = TocRule()
                    source.ruleToc.chapterList = value
                elif field == "ruleContent.content":
                    if source.ruleContent is None:
                        source.ruleContent = ContentRule()
                    source.ruleContent.content = value
            notes.append(f"应用站点特征规则库: {key}")
            break


def remove_none(d):
    if isinstance(d, dict):
        return {k: remove_none(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [remove_none(i) for i in d]
    return d


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_json(path: str, payload: Any) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def write_text(path: str, content: str) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def split_generated_result(result: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], List[Dict[str, Any]]]:
    notes = list(result.get("notes") or [])
    diagnostics = list(result.get("diagnostics") or [])
    source = {k: v for k, v in result.items() if k not in {"notes", "diagnostics"}}
    return remove_none(source), notes, diagnostics


def render_assessment(source: Dict[str, Any], notes: List[str], diagnostics: List[Dict[str, Any]]) -> str:
    issues = source_static_issues(source)
    failed_stages = [d for d in diagnostics if not d.get("success")]
    rating = "directly generatable" if not issues and not failed_stages else "generatable with risk"
    lines = [
        "# Assessment",
        "",
        f"- Site: {source.get('bookSourceName', '')}",
        f"- Base URL: {source.get('bookSourceUrl', '')}",
        f"- Rating: {rating}",
        f"- Static issues: {', '.join(issues) if issues else 'none'}",
        f"- Failed fetch stages: {len(failed_stages)}",
        "",
        "## Notes",
    ]
    lines.extend([f"- {note}" for note in notes] or ["- none"])
    lines.extend(
        [
            "",
            "## Decision",
            "- This file is an automated first-pass assessment.",
            "- Import only after reviewing the generated source and running Readori or Legado validation.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_analysis(source: Dict[str, Any], notes: List[str], diagnostics: List[Dict[str, Any]]) -> str:
    lines = [
        "# Analysis",
        "",
        "## Chain Coverage",
        "",
        "| Stage | URL | Success | Status | Duration ms |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in diagnostics:
        lines.append(
            "| {stage} | {url} | {success} | {status} | {duration} |".format(
                stage=item.get("stage", ""),
                url=str(item.get("url", "")).replace("|", "%7C"),
                success=item.get("success", ""),
                status=item.get("statusCode", ""),
                duration=item.get("durationMs", ""),
            )
        )
    lines.extend(
        [
            "",
            "## Generated Rules",
            "",
            f"- Search URL: `{source.get('searchUrl', '')}`",
            f"- Search list: `{(source.get('ruleSearch') or {}).get('bookList', '')}`",
            f"- Detail name: `{(source.get('ruleBookInfo') or {}).get('name', '')}`",
            f"- TOC list: `{(source.get('ruleToc') or {}).get('chapterList', '')}`",
            f"- Content: `{(source.get('ruleContent') or {}).get('content', '')}`",
            "",
            "## Notes",
        ]
    )
    lines.extend([f"- {note}" for note in notes] or ["- none"])
    return "\n".join(lines) + "\n"


def render_validation_checklist(source: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Validation Checklist",
            "",
            f"- Source: {source.get('bookSourceName', '')}",
            f"- Base URL: {source.get('bookSourceUrl', '')}",
            "",
            "## Import",
            "- [ ] `book-source.json` imports into Readori.",
            "- [ ] Source appears enabled after import.",
            "",
            "## Runtime Validation",
            "- [ ] Search finds the target sample book.",
            "- [ ] Book detail shows name, author or intro, and TOC entry.",
            "- [ ] TOC loads the expected first page of chapters.",
            "- [ ] At least two chapter contents open successfully.",
            "- [ ] If TOC has pagination, late chapters are reachable.",
            "- [ ] Reopening a chapter uses cache or loads substantially faster.",
            "",
            "## Failure Evidence",
            "- [ ] If any step fails, capture the failing URL, source JSON, stage name, and Readori diagnostic log.",
            "",
        ]
    )


def write_site_output_bundle(output_dir: str, source: Dict[str, Any], notes: List[str], diagnostics: List[Dict[str, Any]]) -> Dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    files = {
        "bookSource": os.path.join(output_dir, "book-source.json"),
        "assessment": os.path.join(output_dir, "assessment.md"),
        "analysis": os.path.join(output_dir, "analysis.md"),
        "validationChecklist": os.path.join(output_dir, "validation-checklist.md"),
        "report": os.path.join(output_dir, "report.json"),
    }
    write_json(files["bookSource"], [source])
    write_text(files["assessment"], render_assessment(source, notes, diagnostics))
    write_text(files["analysis"], render_analysis(source, notes, diagnostics))
    write_text(files["validationChecklist"], render_validation_checklist(source))
    return files


def derive_site_slug(site_url: str) -> str:
    try:
        host = urlparse(site_url).netloc or site_url
    except ValueError:
        host = site_url
    host = host.strip().lower()
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    return slug or "site"


def scaffold_output_bundle(root_dir: str, site_url: str) -> Dict[str, str]:
    bundle_dir = os.path.join(root_dir, derive_site_slug(site_url))
    os.makedirs(bundle_dir, exist_ok=True)
    files = {
        "bookSource": os.path.join(bundle_dir, "book-source.json"),
        "assessment": os.path.join(bundle_dir, "assessment.md"),
        "analysis": os.path.join(bundle_dir, "analysis.md"),
        "validationChecklist": os.path.join(bundle_dir, "validation-checklist.md"),
    }
    templates = {
        files["bookSource"]: "[]\n",
        files["assessment"]: "\n".join(
            [
                "# Site Generatability Assessment",
                "",
                f"- Target site: {site_url}",
                "- Login dependency: ",
                "- Search availability: ",
                "- Detail availability: ",
                "- TOC availability: ",
                "- Content availability: ",
                "- Anti-crawler/signature/encryption risk: ",
                "- Rating: ",
                "- Continue generation: ",
                "- Continue/stop reason: ",
                "",
            ]
        ),
        files["analysis"]: "\n".join(
            [
                "# Site Analysis",
                "",
                "## Search",
                "- Entry or trigger: ",
                "- Request/API source: ",
                "- Stable extraction evidence: ",
                "- Risk: ",
                "- Legado rule recommendation: ",
                "",
                "## Detail",
                "- Entry or trigger: ",
                "- Request/API source: ",
                "- Stable extraction evidence: ",
                "- Risk: ",
                "- Legado rule recommendation: ",
                "",
                "## TOC",
                "- Entry or trigger: ",
                "- Request/API source: ",
                "- Stable extraction evidence: ",
                "- Risk: ",
                "- Legado rule recommendation: ",
                "",
                "## Content",
                "- Entry or trigger: ",
                "- Request/API source: ",
                "- Stable extraction evidence: ",
                "- Risk: ",
                "- Legado rule recommendation: ",
                "",
            ]
        ),
        files["validationChecklist"]: "\n".join(
            [
                "# Validation Checklist",
                "",
                "- [ ] `book-source.json` imports into Readori.",
                "- [ ] Search returns the target sample book.",
                "- [ ] Detail page shows metadata.",
                "- [ ] TOC loads enough chapters, including late chapters when known.",
                "- [ ] At least two chapter contents open.",
                "- [ ] Reopening content hits cache or loads faster.",
                "- [ ] Failures include source JSON, failing URL, stage, and Readori diagnostics.",
                "",
            ]
        ),
    }
    for path, content in templates.items():
        if not os.path.exists(path):
            write_text(path, content)
    return files


def collect_embedded_js_snippets(value: Any) -> List[str]:
    if not isinstance(value, str):
        return []
    snippets: List[str] = []
    for match in re.finditer(r"<js>\s*([\s\S]*?)\s*</js>", value, re.I):
        snippets.append(match.group(1))
    js_index = value.find("@js:")
    if js_index >= 0:
        snippets.append(value[js_index + 4 :])
    return snippets


def check_js_syntax(snippet: str) -> Optional[str]:
    node = shutil.which("node")
    if not node:
        return None
    checker = (
        "const fs=require('fs');"
        "const src=fs.readFileSync(0,'utf8');"
        "try{new Function(src);}"
        "catch(e){console.error(e.message);process.exit(1);}"
    )
    try:
        proc = subprocess.run(
            [node, "-e", checker],
            input=snippet,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return f"node_check_failed: {exc}"
    if proc.returncode == 0:
        return None
    return (proc.stderr or proc.stdout or "syntax error").strip()


def collect_js_syntax_errors(source: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    for group_name in RULE_GROUP_KEYS:
        group = normalize_rule_dict(source.get(group_name))
        for field_name, field_value in group.items():
            for snippet in collect_embedded_js_snippets(field_value):
                message = check_js_syntax(str(snippet))
                if message:
                    errors.append(f"{group_name}.{field_name}: {message}")
                    break
    return errors


def is_risky_rule_value(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith(":") or any(marker in stripped for marker in RISKY_RULE_MARKERS)


def build_search_preview(search_url: Any, keyword: str = "测试", page: str = "1") -> str:
    if not isinstance(search_url, str) or not search_url:
        return ""
    preview = search_url
    replacements = {
        "{{key}}": keyword,
        "{{keyword}}": keyword,
        "{{page}}": str(page),
        "{{searchPage}}": str(page),
        "{key}": keyword,
        "{page}": str(page),
    }
    for old, new in replacements.items():
        preview = preview.replace(old, new)
    return preview


def audit_source_rules(source: Dict[str, Any], keyword: str = "测试", page: str = "1") -> Dict[str, Any]:
    sections: Dict[str, Any] = {}
    for group_name in RULE_GROUP_KEYS:
        group = normalize_rule_dict(source.get(group_name))
        placeholder_fields: List[str] = []
        risky_fields: List[str] = []
        for field_name, field_value in group.items():
            if not isinstance(field_value, str):
                continue
            normalized = field_value.strip()
            if not normalized:
                continue
            if normalized in PLACEHOLDER_RULE_TEXT or normalized.lower() in PLACEHOLDER_RULE_TEXT:
                placeholder_fields.append(field_name)
            if is_risky_rule_value(normalized):
                risky_fields.append(field_name)

        notes: List[str] = []
        if placeholder_fields:
            notes.append("placeholder_rules_present")
        if risky_fields:
            notes.append("js_regex_webview_or_java_rules_need_runtime_validation")
        sections[group_name] = {
            "totalFields": len(group),
            "placeholderFields": placeholder_fields,
            "riskyFields": risky_fields,
            "notes": notes,
        }

    return {
        "bookSourceName": source.get("bookSourceName", ""),
        "bookSourceUrl": source.get("bookSourceUrl", ""),
        "staticIssues": source_static_issues(source),
        "loginConfigured": bool(source.get("loginUrl")),
        "exploreConfigured": bool(source.get("enabledExplore") or source.get("exploreUrl")),
        "searchPreview": build_search_preview(source.get("searchUrl"), keyword=keyword, page=page),
        "jsSyntaxErrors": collect_js_syntax_errors(source),
        "sections": sections,
    }


def format_audit_report(source: Dict[str, Any], audit: Dict[str, Any]) -> str:
    lines = [
        f"Source: {source.get('bookSourceName') or 'Unknown'}",
        f"Site: {source.get('bookSourceUrl') or 'Unknown'}",
        f"Login configured: {'yes' if audit.get('loginConfigured') else 'no'}",
        f"Explore configured: {'yes' if audit.get('exploreConfigured') else 'no'}",
        f"Static issues: {', '.join(audit.get('staticIssues') or []) if audit.get('staticIssues') else 'none'}",
    ]
    if audit.get("searchPreview"):
        lines.append(f"Search preview: {audit['searchPreview']}")
    js_errors = audit.get("jsSyntaxErrors") or []
    lines.append(f"JS syntax check: {', '.join(js_errors) if js_errors else 'no syntax errors detected or node unavailable'}")

    for group_name, section in (audit.get("sections") or {}).items():
        lines.extend(
            [
                "",
                f"{group_name}:",
                f"  fields: {section.get('totalFields', 0)}",
                f"  placeholders: {', '.join(section.get('placeholderFields') or []) if section.get('placeholderFields') else 'none'}",
                f"  risky: {', '.join(section.get('riskyFields') or []) if section.get('riskyFields') else 'none'}",
            ]
        )
        for note in section.get("notes") or []:
            lines.append(f"  note: {note}")

    lines.extend(
        [
            "",
            "Note: this is a static audit. It does not simulate the full Legado/Readori runtime.",
            "Runtime validation still requires search/detail/TOC/content network checks.",
        ]
    )
    return "\n".join(lines)


def first_absolute_url(text: Optional[str]) -> str:
    raw = text or ""
    match = re.search(r"https?://[^\s\"'<>]+", raw)
    return match.group(0).rstrip(",)}]\"") if match else ""


def normalize_source_package_link(value: Any, base_url: str = "") -> str:
    raw = html_lib.unescape(str(value or "")).strip()
    if not raw:
        return ""
    raw = raw.strip(" \t\r\n\"'<>),;")
    if raw.startswith("//"):
        base_scheme = urlparse(base_url).scheme or "https"
        raw = f"{base_scheme}:{raw}"
    if base_url:
        raw = urljoin(base_url, raw)
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path_lower = parsed.path.lower()
    if not path_lower.endswith(".json"):
        return ""
    if path_lower.endswith(("/package.json", "/manifest.json", "/tsconfig.json")):
        return ""
    return parsed._replace(fragment="").geturl()


def extract_source_package_urls(text: str, base_url: str = "") -> List[str]:
    urls: List[str] = []
    seen = set()

    def add(candidate: Any) -> None:
        normalized = normalize_source_package_link(candidate, base_url=base_url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)

    if BeautifulSoup is not None:
        soup = BeautifulSoup(text or "", "lxml")
        for tag in soup.select("[href], [src], [data-url], [data-src], [data-href]"):
            for attr in ("href", "src", "data-url", "data-src", "data-href"):
                if tag.has_attr(attr):
                    add(tag.get(attr))

    for match in re.finditer(r"https?://[^\s\"'<>]+?\.json(?:\?[^\s\"'<>]*)?", text or "", re.I):
        add(match.group(0))
    for match in re.finditer(r"['\"]([^'\"]+?\.json(?:\?[^'\"]*)?)['\"]", text or "", re.I):
        add(match.group(1))
    return urls


def discover_public_source_urls(
    builder: AutoBuilder,
    seed_urls: Optional[List[str]] = None,
    limit: int = 80,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    seeds = list(seed_urls or []) or list(DEFAULT_PUBLIC_SOURCE_INDEX_URLS)
    discovered: List[str] = []
    seen = set()
    reports: List[Dict[str, Any]] = []

    def add(url: str) -> None:
        if url and url not in seen and len(discovered) < limit:
            seen.add(url)
            discovered.append(url)

    for seed in seeds:
        if len(discovered) >= limit:
            break
        direct = normalize_source_package_link(seed)
        if direct:
            add(direct)
            reports.append({"url": seed, "success": True, "directPackage": True, "discovered": 1})
            continue
        try:
            page = builder.fetch(seed, stage="source_index")
            found = extract_source_package_urls(page, base_url=seed)
            for url in found:
                add(url)
            reports.append({"url": seed, "success": True, "directPackage": False, "discovered": len(found)})
        except Exception as exc:
            reports.append({"url": seed, "success": False, "directPackage": False, "discovered": 0, "error": str(exc)})
    return discovered, reports


def normalize_source_url(value: str) -> str:
    url = (value or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return url
    netloc = parsed.netloc
    if parsed.scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif parsed.scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    elif netloc.endswith(":"):
        netloc = netloc[:-1]
    path = parsed.path.rstrip("/") if parsed.path == "/" else parsed.path
    return parsed._replace(netloc=netloc, path=path).geturl()


def dedupe_key_for_source(item: Dict[str, Any]) -> str:
    normalized = normalize_source_url(str(item.get("bookSourceUrl") or "")).lower().rstrip("/")
    if normalized:
        return normalized
    payload = json.dumps(item, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_source_record(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if str(obj.get("bookSourceUrl") or "").strip():
        return True
    return any(isinstance(obj.get(key), dict) for key in ESSENTIAL_RULE_KEYS)


def load_source_records_from_payload(payload: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            if is_source_record(item):
                records.append(item)
            elif isinstance(item, (dict, list)):
                records.extend(load_source_records_from_payload(item))
        return records
    if isinstance(payload, dict):
        if is_source_record(payload):
            records.append(payload)
        for key in SOURCE_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, (dict, list)):
                records.extend(load_source_records_from_payload(value))
    return records


def load_source_records_from_file(path: str) -> List[Dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return load_source_records_from_payload(payload)


def derive_valid_source_url(item: Dict[str, Any]) -> str:
    current = normalize_source_url(str(item.get("bookSourceUrl") or ""))
    try:
        parsed = urlparse(current) if current else None
    except ValueError:
        parsed = None
    if parsed and parsed.scheme in {"http", "https"} and parsed.netloc:
        return current

    candidates = [
        first_absolute_url(str(item.get("searchUrl") or "")),
        first_absolute_url(str(item.get("exploreUrl") or "")),
    ]
    for key in ESSENTIAL_RULE_KEYS:
        value = item.get(key)
        if isinstance(value, dict):
            candidates.append(first_absolute_url(json.dumps(value, ensure_ascii=False)))

    for candidate in candidates:
        normalized = normalize_source_url(candidate)
        try:
            parsed_candidate = urlparse(normalized) if normalized else None
        except ValueError:
            parsed_candidate = None
        if parsed_candidate and parsed_candidate.scheme in {"http", "https"} and parsed_candidate.netloc:
            return f"{parsed_candidate.scheme}://{parsed_candidate.netloc}"
    return current


def normalize_rule_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def repair_source_record(item: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    repaired = dict(item)
    changes: List[str] = []

    for tool_key in ("diagnostics", "notes"):
        if tool_key in repaired:
            repaired.pop(tool_key, None)
            changes.append(f"removed_tool_field:{tool_key}")

    source_url = derive_valid_source_url(repaired)
    if source_url != repaired.get("bookSourceUrl"):
        repaired["bookSourceUrl"] = source_url
        changes.append("repaired_bookSourceUrl")

    if not str(repaired.get("bookSourceName") or "").strip():
        host = urlparse(str(repaired.get("bookSourceUrl") or "")).netloc or "Unnamed Source"
        repaired["bookSourceName"] = host
        changes.append("filled_bookSourceName")

    if "bookSourceType" not in repaired:
        repaired["bookSourceType"] = 0
        changes.append("filled_bookSourceType")
    if "enabled" not in repaired:
        repaired["enabled"] = True
        changes.append("filled_enabled")
    if "enabledExplore" not in repaired:
        repaired["enabledExplore"] = bool(str(repaired.get("exploreUrl") or "").strip())
        changes.append("filled_enabledExplore")
    if "enabledCookieJar" not in repaired:
        repaired["enabledCookieJar"] = True
        changes.append("filled_enabledCookieJar")

    for key in ("ruleSearch", "ruleExplore", "ruleBookInfo", "ruleToc", "ruleContent"):
        if key in repaired and repaired[key] is None:
            repaired[key] = {}
            changes.append(f"normalized_empty_{key}")
        elif key in repaired and not isinstance(repaired[key], dict):
            repaired[key] = {}
            changes.append(f"dropped_invalid_{key}")

    repaired = remove_none(repaired)
    return repaired, changes


def source_static_issues(item: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    try:
        parsed = urlparse(str(item.get("bookSourceUrl") or ""))
    except ValueError:
        parsed = urlparse("")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        issues.append("invalid_bookSourceUrl")
    if not str(item.get("bookSourceName") or "").strip():
        issues.append("missing_bookSourceName")

    rule_search = normalize_rule_dict(item.get("ruleSearch"))
    rule_explore = normalize_rule_dict(item.get("ruleExplore"))
    has_search = bool(str(item.get("searchUrl") or "").strip()) and bool(str(rule_search.get("bookList") or "").strip())
    has_explore = bool(str(item.get("exploreUrl") or "").strip()) and bool(str(rule_explore.get("bookList") or "").strip())
    if not has_search and not has_explore:
        issues.append("missing_search_or_explore")

    if not normalize_rule_dict(item.get("ruleBookInfo")):
        issues.append("missing_ruleBookInfo")

    rule_toc = normalize_rule_dict(item.get("ruleToc"))
    if not str(rule_toc.get("chapterList") or "").strip():
        issues.append("missing_ruleToc.chapterList")
    if not str(rule_toc.get("chapterName") or "").strip():
        issues.append("missing_ruleToc.chapterName")
    if not str(rule_toc.get("chapterUrl") or "").strip():
        issues.append("missing_ruleToc.chapterUrl")

    rule_content = normalize_rule_dict(item.get("ruleContent"))
    if not str(rule_content.get("content") or "").strip():
        issues.append("missing_ruleContent.content")

    return issues


def score_source_record(item: Dict[str, Any]) -> int:
    score = 0
    if item.get("enabled", True):
        score += 10
    for key in ESSENTIAL_RULE_KEYS:
        if isinstance(item.get(key), dict):
            score += 6
    if str(item.get("searchUrl") or "").strip():
        score += 5
    if str(item.get("exploreUrl") or "").strip():
        score += 2
    score -= len(source_static_issues(item)) * 4
    score += len([k for k, v in item.items() if v not in ("", None, [], {})])
    return score


def repair_and_validate_sources(records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    best_by_key: Dict[str, Dict[str, Any]] = {}
    best_score: Dict[str, int] = {}
    detail: List[Dict[str, Any]] = []
    raw_count = len(records)

    for index, record in enumerate(records):
        repaired, changes = repair_source_record(record)
        key = dedupe_key_for_source(repaired)
        score = score_source_record(repaired)
        issues = source_static_issues(repaired)
        detail.append(
            {
                "index": index,
                "bookSourceName": repaired.get("bookSourceName", ""),
                "bookSourceUrl": repaired.get("bookSourceUrl", ""),
                "score": score,
                "issues": issues,
                "changes": changes,
            }
        )
        if key not in best_by_key or score > best_score.get(key, -10_000):
            best_by_key[key] = repaired
            best_score[key] = score

    repaired_sources = sorted(
        best_by_key.values(),
        key=lambda item: (normalize_source_url(str(item.get("bookSourceUrl") or "")).lower(), str(item.get("bookSourceName") or "")),
    )
    usable = [item for item in repaired_sources if not source_static_issues(item)]
    report = {
        "generatedAtUTC": datetime.now(timezone.utc).isoformat(),
        "rawRecords": raw_count,
        "dedupedRecords": len(repaired_sources),
        "usableRecords": len(usable),
        "invalidRecords": len(repaired_sources) - len(usable),
        "checks": [
            "bookSourceUrl",
            "bookSourceName",
            "search_or_explore",
            "ruleBookInfo",
            "ruleToc.chapterList",
            "ruleToc.chapterName",
            "ruleToc.chapterUrl",
            "ruleContent.content",
        ],
        "details": detail,
    }
    return repaired_sources, report


def fetch_source_package(url: str, builder: AutoBuilder) -> List[Dict[str, Any]]:
    html = builder.fetch(url, stage="source_package")
    payload = json.loads(html)
    return load_source_records_from_payload(payload)


def run_repair_package(args: argparse.Namespace, builder: AutoBuilder) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []
    inputs: List[Dict[str, Any]] = []

    for path in args.repair_file or []:
        loaded = load_source_records_from_file(path)
        records.extend(loaded)
        inputs.append({"type": "file", "path": path, "records": len(loaded)})

    urls = list(args.source_url or [])
    if args.public_sources:
        urls.extend(DEFAULT_PUBLIC_SOURCE_URLS)
    if args.discover_public_sources:
        discovered, discovery_reports = discover_public_source_urls(
            builder,
            seed_urls=args.public_source_index_url,
            limit=max(1, int(args.discover_limit or 1)),
        )
        urls.extend(DEFAULT_PUBLIC_SOURCE_URLS)
        urls.extend(discovered)
        inputs.append(
            {
                "type": "public-discovery",
                "seedUrls": list(args.public_source_index_url or DEFAULT_PUBLIC_SOURCE_INDEX_URLS),
                "records": 0,
                "discoveredUrls": len(discovered),
                "success": any(item.get("success") for item in discovery_reports),
                "details": discovery_reports,
            }
        )

    seen_urls = set()
    for url in urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            loaded = fetch_source_package(url, builder)
            records.extend(loaded)
            inputs.append({"type": "url", "url": url, "records": len(loaded), "success": True})
        except Exception as exc:
            inputs.append({"type": "url", "url": url, "records": 0, "success": False, "error": str(exc)})

    repaired, report = repair_and_validate_sources(records)
    report["mode"] = "repair-package"
    report["inputs"] = inputs
    report["diagnostics"] = [asdict(s) for s in builder.stage_logs]

    output_sources = repaired
    if args.usable_only:
        output_sources = [item for item in repaired if not source_static_issues(item)]
    report["outputRecords"] = len(output_sources)
    report["outputFilter"] = "usable-static" if args.usable_only else "all-repaired"

    output = args.package_output or "booksource-package.repaired.json"
    report_path = args.report or f"{output}.report.json"
    write_json(output, output_sources)
    write_json(report_path, report)

    print(f"OK: repaired {len(repaired)} sources from {len(records)} raw records")
    print(f"OK: output {len(output_sources)} sources ({report['outputFilter']})")
    print(f"OK: usable static sources {report['usableRecords']}")
    print(f"OK: wrote {output}")
    print(f"OK: wrote report {report_path}")
    return report


def run_scaffold(args: argparse.Namespace) -> Dict[str, Any]:
    site_url = args.scaffold_site_url or args.base
    if not site_url:
        raise ValueError("--scaffold-site-url or --base is required with --scaffold-output-dir")
    files = scaffold_output_bundle(args.scaffold_output_dir, site_url)
    report = {
        "mode": "scaffold",
        "generatedAtUTC": datetime.now(timezone.utc).isoformat(),
        "siteUrl": site_url,
        "files": files,
    }
    if args.report:
        write_json(args.report, report)
    print(f"OK: scaffolded {os.path.dirname(files['bookSource'])}")
    for label, path in files.items():
        print(f"OK: {label} -> {path}")
    return report


def run_audit(args: argparse.Namespace) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []
    inputs: List[Dict[str, Any]] = []
    for path in args.audit_file or []:
        loaded = load_source_records_from_file(path)
        records.extend(loaded)
        inputs.append({"path": path, "records": len(loaded)})

    reports: List[Dict[str, Any]] = []
    rendered: List[str] = []
    for index, source in enumerate(records):
        audit = audit_source_rules(source, keyword=args.audit_keyword, page=str(args.audit_page))
        reports.append({"index": index, **audit})
        header = f"# Source {index + 1}" if len(records) > 1 else "# Source Audit"
        rendered.append(f"{header}\n\n{format_audit_report(source, audit)}")

    output_text = "\n\n".join(rendered) + ("\n" if rendered else "")
    summary = {
        "mode": "audit",
        "generatedAtUTC": datetime.now(timezone.utc).isoformat(),
        "inputs": inputs,
        "records": len(records),
        "reports": reports,
    }
    if args.audit_output:
        write_text(args.audit_output, output_text)
        print(f"OK: wrote audit {args.audit_output}")
    else:
        print(output_text.rstrip())
    if args.report:
        write_json(args.report, summary)
        print(f"OK: wrote report {args.report}")
    return summary


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", name.strip())
    return cleaned.strip("_") or "source"


def run_single(args: argparse.Namespace, builder: AutoBuilder) -> Dict[str, Any]:
    result = builder.build(
        base_url=args.base,
        name=args.name,
        search_url=args.search_url,
        detail_url=args.detail_url,
        toc_url=args.toc_url,
        chapter_url=args.chapter_url,
        search_keyword=args.search_keyword,
        auto_follow_samples=args.auto_follow_samples,
    )
    source, notes, diagnostics = split_generated_result(remove_none(result))
    output_path = args.output
    if args.output_dir:
        output_path = os.path.join(args.output_dir, "book-source.json")
    write_json(output_path, [source])

    report = {
        "mode": "single",
        "generatedAtUTC": datetime.now(timezone.utc).isoformat(),
        "success": True,
        "output": output_path,
        "diagnostics": diagnostics,
        "notes": notes,
        "staticIssues": source_static_issues(source),
    }
    if args.output_dir:
        files = write_site_output_bundle(args.output_dir, source, notes, diagnostics)
        report_path = args.report or files["report"]
    else:
        report_path = args.report or f"{output_path}.report.json"
    write_json(report_path, report)

    print(f"OK: wrote {output_path}")
    print(f"OK: wrote report {report_path}")
    if args.output_dir:
        print(f"OK: wrote output bundle {args.output_dir}")
    return report


def run_batch(args: argparse.Namespace, builder: AutoBuilder) -> Dict[str, Any]:
    with open(args.batch_file, "r", encoding="utf-8") as f:
        batch_items = json.load(f)

    if not isinstance(batch_items, list):
        raise ValueError("batch file must be a JSON array")

    output_dir = args.batch_output_dir or "batch-output"
    os.makedirs(output_dir, exist_ok=True)

    tasks_report: List[Dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    for idx, item in enumerate(batch_items, start=1):
        if not isinstance(item, dict):
            fail_count += 1
            tasks_report.append(
                {
                    "index": idx,
                    "success": False,
                    "error": "task item must be object",
                }
            )
            continue

        task_base = item.get("base")
        task_name = item.get("name")
        if not task_base:
            fail_count += 1
            tasks_report.append(
                {
                    "index": idx,
                    "name": task_name,
                    "success": False,
                    "error": "missing required field: base",
                }
            )
            continue

        task_id = safe_name(str(task_name or task_base))
        output_path = item.get("output") or os.path.join(output_dir, f"{idx:03d}-{task_id}.source.json")
        try:
            result = builder.build(
                base_url=task_base,
                name=task_name,
                search_url=item.get("search_url"),
                detail_url=item.get("detail_url"),
                toc_url=item.get("toc_url"),
                chapter_url=item.get("chapter_url"),
                search_keyword=str(item.get("search_keyword") or args.search_keyword),
                auto_follow_samples=bool(item.get("auto_follow_samples", args.auto_follow_samples)),
            )
            source, notes, diagnostics = split_generated_result(remove_none(result))
            write_json(output_path, [source])
            success_count += 1
            tasks_report.append(
                {
                    "index": idx,
                    "name": task_name or task_base,
                    "base": task_base,
                    "success": True,
                    "output": output_path,
                    "notes": notes,
                    "diagnostics": diagnostics,
                    "staticIssues": source_static_issues(source),
                }
            )
            print(f"[{idx}/{len(batch_items)}] OK: {task_name or task_base} -> {output_path}")
        except Exception as e:
            fail_count += 1
            tasks_report.append(
                {
                    "index": idx,
                    "name": task_name or task_base,
                    "base": task_base,
                    "success": False,
                    "error": str(e),
                    "diagnostics": [asdict(s) for s in builder.stage_logs],
                }
            )
            print(f"[{idx}/{len(batch_items)}] FAIL: {task_name or task_base} -> {e}")

    summary = {
        "mode": "batch",
        "generatedAtUTC": datetime.now(timezone.utc).isoformat(),
        "total": len(batch_items),
        "success": success_count,
        "failed": fail_count,
        "tasks": tasks_report,
    }

    report_path = args.report or os.path.join(output_dir, "batch.report.json")
    write_json(report_path, summary)
    print(f"OK: wrote batch report {report_path}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Book source auto builder for Readori/Legado style JSON")
    parser.add_argument("--base", help="base site url, e.g. https://example.com")
    parser.add_argument("--name", help="book source name")
    parser.add_argument("--search-url", help="sample search page url (absolute or relative)")
    parser.add_argument("--detail-url", help="sample detail page url (absolute or relative)")
    parser.add_argument("--toc-url", help="sample toc page url (absolute or relative)")
    parser.add_argument("--chapter-url", help="sample chapter page url (absolute or relative)")
    parser.add_argument("--search-keyword", default="test", help="sample keyword for --auto-follow-samples")
    parser.add_argument("--auto-follow-samples", action="store_true", help="single/batch mode: execute inferred search and follow detail/TOC/chapter samples automatically")
    parser.add_argument("--output", default="output.source.json", help="output json path")
    parser.add_argument("--report", help="diagnostics report path")
    parser.add_argument("--timeout", type=int, default=20, help="request timeout seconds")
    parser.add_argument("--retries", type=int, default=2, help="retry times for each fetch")
    parser.add_argument("--retry-backoff", type=float, default=0.7, help="retry backoff base seconds")
    parser.add_argument("--batch-file", help="batch task json file path")
    parser.add_argument("--batch-output-dir", help="batch output directory, default batch-output")
    parser.add_argument("--output-dir", help="write a skill-style single-site bundle: book-source.json, assessment.md, analysis.md, validation-checklist.md, report.json")
    parser.add_argument("--scaffold-output-dir", help="create an empty skill-style bundle under this root without fetching the website")
    parser.add_argument("--scaffold-site-url", help="site URL for --scaffold-output-dir; defaults to --base")
    parser.add_argument("--repair-file", action="append", help="Legado/Readori source JSON file to repair and statically validate; repeatable")
    parser.add_argument("--source-url", action="append", help="remote Legado source package URL to fetch, repair, merge, and statically validate; repeatable")
    parser.add_argument("--public-sources", action="store_true", help="fetch the built-in public source package URLs discovered from current public Legado ecosystem")
    parser.add_argument("--discover-public-sources", action="store_true", help="crawl public source index pages for Legado JSON package URLs before repair")
    parser.add_argument("--public-source-index-url", action="append", help="public index page URL used by --discover-public-sources; repeatable")
    parser.add_argument("--discover-limit", type=int, default=80, help="maximum source package URLs discovered from public index pages")
    parser.add_argument("--package-output", help="output path for repaired/merged source package")
    parser.add_argument("--usable-only", action="store_true", help="package mode only: output records with no static validation issues")
    parser.add_argument("--audit-file", action="append", help="Legado/Readori source JSON file to statically audit without repair; repeatable")
    parser.add_argument("--audit-output", help="markdown output path for --audit-file")
    parser.add_argument("--audit-keyword", default="测试", help="keyword used when rendering search URL previews")
    parser.add_argument("--audit-page", default="1", help="page value used when rendering search URL previews")

    args = parser.parse_args()

    if args.scaffold_output_dir:
        run_scaffold(args)
        return

    if args.audit_file:
        run_audit(args)
        return

    builder = AutoBuilder(timeout=args.timeout, retries=args.retries, retry_backoff=args.retry_backoff)
    if args.repair_file or args.source_url or args.public_sources or args.discover_public_sources:
        run_repair_package(args, builder)
        return

    if args.batch_file:
        run_batch(args, builder)
        return

    if not args.base:
        raise ValueError("--base is required in single mode (without --batch-file)")

    run_single(args, builder)


if __name__ == "__main__":
    main()
