"""R144a: the validator must not be looser than the Readori App (offline; no external network).

Goal: "the validator says pass  =>  the source works in the Readori App". Every test below pins one place where the
validator used to rescue a source with a fallback / heuristic / retry that the App (Swift engine,
Readori/Core/Engine/BookSourceEngine.swift + Readori/Services/BookSourceValidator.swift) does not have. "E:nnn" refers to
BookSourceEngine.swift line numbers. All network access is replaced by a monkeypatched `fetch_text` or by a fake Session.
"""
from __future__ import annotations

import json
import socket
from typing import Any

import pytest
import requests

from validator import validate_source_packages as validator

needs_js = pytest.mark.skipif(
    not validator.node_binary_path() and validator.quickjs is None,
    reason="no JS engine (node / quickjs) available",
)

LONG_TEXT = "这是一段足够长的章节正文，用来确认验证工具的判定和阅读器一致。" * 8
BASE = "https://example.test"


# ------------------------------------------------------------------------------------------------ helpers


class Fetch:
    """Replacement for validator.fetch_text: `responses` maps a URL (or URL suffix) to a body or (body, final_url, status)."""

    def __init__(self, responses: dict[str, Any] | None = None, default: Any = None) -> None:
        self.responses = responses or {}
        self.default = default
        self.calls: list[dict[str, Any]] = []

    def __call__(self, session: requests.Session, url: str, **kwargs: Any) -> tuple[str, str, int]:
        self.calls.append({
            "url": url,
            "session": session,
            "headers": dict(kwargs.get("headers") or {}),
            "method": kwargs.get("method", "GET"),
        })
        for key, value in self.responses.items():
            if url == key or url.endswith(key):
                if isinstance(value, tuple):
                    return value[0], value[1] if len(value) > 1 else url, value[2] if len(value) > 2 else 200
                return value, url, 200
        if self.default is not None:
            value = self.default
            if isinstance(value, tuple):
                return value[0], value[1] if len(value) > 1 else url, value[2] if len(value) > 2 else 200
            return value, url, 200
        raise AssertionError(f"unexpected URL: {url}")

    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


def install(monkeypatch, responses: dict[str, Any] | None = None, default: Any = None) -> Fetch:
    fetch = Fetch(responses, default)
    monkeypatch.setattr(validator, "fetch_text", fetch)
    return fetch


def _src(**overrides: Any) -> dict[str, Any]:
    src: dict[str, Any] = {"bookSourceName": "Alignment Source", "bookSourceUrl": BASE}
    src.update(overrides)
    return src


def _content(src: dict[str, Any], url: str = f"{BASE}/chapter/1.html") -> tuple[bool, str, int]:
    return validator.resolve_chapter_content(
        requests.Session(), src, validator.make_runtime(src), url, "第1章", "测试书"
    )


def _toc(src: dict[str, Any], toc_url: str = f"{BASE}/book/1/toc") -> tuple[Any, ...]:
    return validator.resolve_toc_source(
        requests.Session(), src, validator.make_runtime(src), f"{BASE}/book/1", toc_url
    )


def _detail(src: dict[str, Any], book_url: str = f"{BASE}/book/1") -> tuple[bool, str, str, str]:
    return validator.fetch_book_info(requests.Session(), src, validator.make_runtime(src), book_url)


def _candidates(src: dict[str, Any]) -> tuple[list[str], str]:
    return validator.fetch_book_candidates(requests.Session(), src, validator.make_runtime(src))


# ------------------------------------------------------------------------- 1  F1: content fallbacks need a blank rule


def test_item01_nonblank_content_rule_without_result_fails_instead_of_falling_back(monkeypatch) -> None:
    """App E:9723-9751: JSON key guesses / container fallbacks / whole-page text run only for `contentRuleIsBlank`."""
    html_page = f"<html><body><article>{LONG_TEXT}</article></body></html>"
    json_page = json.dumps({"data": {"content": LONG_TEXT}}, ensure_ascii=False)
    for rule, page in (("#missing@text", html_page), ("$.missing", json_page), ("class.nope@html", html_page)):
        install(monkeypatch, {f"{BASE}/chapter/1.html": page})
        ok, content, _pages = _content(_src(ruleContent={"content": rule}))
        assert not ok, rule
        assert content == "", rule
    # control: the whitespace-only rule is the App's blank rule and does reach the fallbacks
    for page in (html_page, json_page):
        install(monkeypatch, {f"{BASE}/chapter/1.html": page})
        ok, content, _pages = _content(_src(ruleContent={"content": " "}))
        assert ok
        assert "足够长的章节正文" in content


def test_item01_is_meaningful_content_is_the_apps_function() -> None:
    """App isMeaningfulContent (E:6955-6978): >= 80 chars, signal >= 40, signal > 1.2 x digits, punctuation density for CJK text."""
    sentence = "这是一个用于测试的段落，"  # 12 characters, 1 punctuation mark
    assert validator.is_meaningful_content(sentence * 8)
    assert not validator.is_meaningful_content(sentence * 6)  # 72 characters
    assert not validator.is_meaningful_content("章节标题" * 30)  # 120 CJK characters, no punctuation (menus / tag lists)
    assert not validator.is_meaningful_content("1234567890" * 12)  # digits only
    assert not validator.is_meaningful_content("")
    # navigation words are stripped before measuring, but tags are not
    assert not validator.is_meaningful_content("上一页下一页返回目录" * 20)


# --------------------------------------------------------------------------- 2  F2/F15: TOC rules are never replaced


TOC_PAGE = """
<html><body>
  <ul id="toc">
    <li><a href="/book/1/1.html">第1章 起始</a></li>
    <li><a href="/book/1/2.html">第2章 继续</a></li>
    <li><a href="/book/1/3.html">第3章 前行</a></li>
  </ul>
</body></html>
"""


def test_item02_nonblank_chapter_list_rule_is_never_replaced_by_raw_links(monkeypatch) -> None:
    """App E:2933-2985: raw-link / dynamic-catalog fallbacks exist only when there is NO chapterList rule."""
    install(monkeypatch, {f"{BASE}/book/1/toc": TOC_PAGE, f"{BASE}/book/1": TOC_PAGE})  # bookUrl: the App retries it when the catalog is empty (E:3488-3500)
    ok, *_rest = _toc(_src(ruleToc={"chapterList": ".missing li", "chapterName": "a@text", "chapterUrl": "a@href"}))
    assert not ok
    # control: a rule that matches is used as-is
    ok, _final, chapter_url, title, _pages, first_count, count = _toc(
        _src(ruleToc={"chapterList": "#toc li", "chapterName": "a@text", "chapterUrl": "a@href"})
    )
    assert ok and (chapter_url, title, first_count, count) == (f"{BASE}/book/1/1.html", "第1章 起始", 3, 3)


@needs_js
def test_item02_chapter_list_script_exception_fails_the_toc(monkeypatch) -> None:
    """App E:2930-2934: a chapterList script error fails the whole catalog; it is not rescued by raw links."""
    install(monkeypatch, {f"{BASE}/book/1/toc": TOC_PAGE, f"{BASE}/book/1": TOC_PAGE})  # bookUrl: the App retries it when the catalog is empty (E:3488-3500)
    src = _src(ruleToc={"chapterList": "<js>throw new Error('boom');</js>", "chapterName": "a@text", "chapterUrl": "a@href"})
    ok, *_rest = _toc(src)
    assert not ok


def test_item02_missing_chapter_list_rule_uses_the_apps_raw_link_fallback(monkeypatch) -> None:
    """App E:2935-2945 / tocLacksRule: without a chapterList rule the page is read by the raw-link heuristics (no early failure)."""
    install(monkeypatch, {f"{BASE}/book/1/toc": TOC_PAGE, f"{BASE}/book/1": TOC_PAGE})  # bookUrl: the App retries it when the catalog is empty (E:3488-3500)
    ok, _final, chapter_url, title, _pages, first_count, count = _toc(_src(ruleToc={"chapterName": "a@text"}))
    assert ok
    assert (chapter_url, title) == (f"{BASE}/book/1/1.html", "第1章 起始")
    assert first_count == count == 3
    # no ruleToc object at all is the App's noTocRule error
    ok, *_rest = _toc(_src())
    assert not ok


def test_item02_empty_chapter_url_becomes_the_toc_page_url(monkeypatch) -> None:
    """App E:3203-3226: a chapter whose chapterUrl rule yields nothing gets the TOC page URL (Legado BookChapterList)."""
    page = "<html><body><ul id='toc'><li><b>第1章 无链接章节</b></li></ul></body></html>"
    install(monkeypatch, {f"{BASE}/book/1/toc": page})
    src = _src(ruleToc={"chapterList": "#toc li", "chapterName": "b@text", "chapterUrl": "a@href"})
    ok, final_url, chapter_url, title, _pages, first_count, _count = _toc(src)
    assert ok and title == "第1章 无链接章节" and first_count == 1
    assert chapter_url == final_url == f"{BASE}/book/1/toc"


# --------------------------------------------------------------------------------- 3  F3: detail name is never invented


def test_item03_detail_name_rule_result_is_final_and_book_source_name_is_never_used(monkeypatch) -> None:
    """App E:8800-9120: name heuristics only for a blank name rule; there is no bookSourceName fallback anywhere."""
    page = (
        "<html><head><title>页面标题_某站</title><meta property='og:novel:book_name' content='Meta Name'></head>"
        "<body><a id='toc' href='/book/1/catalog'>目录</a></body></html>"
    )
    install(monkeypatch, {f"{BASE}/book/1": page})
    ok, name, toc_url, _reason = _detail(_src(bookSourceName="My Book Source", ruleBookInfo={"name": ".missing@text", "tocUrl": "#toc@href"}))
    assert ok
    assert name == ""  # non-blank rule, empty result: no <title> / og guess
    assert toc_url == f"{BASE}/book/1/catalog"
    # blank rule: the <title> / og heuristics stay (App `lacksRule`)
    ok, name, _toc_url, _reason = _detail(_src(bookSourceName="My Book Source", ruleBookInfo={"tocUrl": "#toc@href"}))
    assert ok and name == "Meta Name"
    # nothing to find at all: still never the source's own name
    install(monkeypatch, {f"{BASE}/book/1": "<html><body><p>x</p></body></html>"})
    ok, name, _toc_url, _reason = _detail(_src(bookSourceName="My Book Source", ruleBookInfo={"name": ".missing@text"}))
    assert name != "My Book Source"
    assert not ok or name == ""


# ------------------------------------------------------------------------------- 4  F4: search has no extra heuristics


SEARCH_PAGE = """
<html><body>
  <div class="recommend"><a href="/book/9">推荐小说一</a><a href="/book/10">推荐小说二</a></div>
  <div class="hit"><span class="t">命中</span></div>
</body></html>
"""


def test_item04_search_without_rule_result_has_no_wrapper_or_html_heuristics(monkeypatch) -> None:
    """App usesHeuristicBookList (blank bookList only): a rule bookList matching nothing is an empty search. The validator
    no longer recovers nested JSON lists / wrapper noise, nor builds items from the page's links."""
    nested_json = json.dumps({"data": {"list": [{"bookName": "凡人修仙传", "bookUrl": "/book/1"}]}}, ensure_ascii=False)
    link_list = "<html><body><ul><li><a href='/book/5'>凡人修仙传</a><span>忘语</span></li><li><a href='/book/6'>仙逆</a><span>耳根</span></li></ul></body></html>"
    for body, rule in ((nested_json, {"bookList": "$.missing[*]", "name": "bookName", "bookUrl": "bookUrl"}),
                       (link_list, {"bookList": ".nothing", "name": "a@text", "bookUrl": "a@href"})):
        install(monkeypatch, default=body)
        urls, base = _candidates(_src(searchUrl=f"{BASE}/search?q={{{{key}}}}", ruleSearch=rule))
        assert urls == [] and base == ""
    # control: a blank bookList is the App's heuristic mode, but without a searchUrl+bookList pair there is no search at all
    install(monkeypatch, default=nested_json)
    assert _candidates(_src(searchUrl=f"{BASE}/search?q={{{{key}}}}", ruleSearch={"bookList": "$.data.list[*]", "name": "bookName", "bookUrl": "bookUrl"}))[0] == [f"{BASE}/book/1"]


def test_item04_item_name_and_book_url_follow_the_app(monkeypatch) -> None:
    """App parseSearchResult (E:931-1085): a non-blank name rule that yields nothing skips the item (no placeholder name);
    an empty bookUrl rule result is the response URL; a usable rule bookUrl is kept as is."""
    page = "<html><body><div class='b'><i class='n'>书甲</i><a href='/book/1'>x</a></div><div class='b'><a href='/book/2'>x</a></div></body></html>"
    install(monkeypatch, default=page)
    src = _src(searchUrl=f"{BASE}/search?q={{{{key}}}}", ruleSearch={"bookList": ".b", "name": ".n@text", "bookUrl": "a@href"})
    urls, _base = _candidates(src)
    assert urls == [f"{BASE}/book/1"]  # the second item has no name -> skipped (old code invented the name "x")
    src = _src(searchUrl=f"{BASE}/search?q={{{{key}}}}", ruleSearch={"bookList": ".b", "name": ".n@text", "bookUrl": ".missing@href"})
    urls, base = _candidates(src)
    assert urls == [base]  # empty bookUrl result = the search response URL (Legado isUrl fallback)


def test_item04_search_failure_is_not_rescued_by_a_working_explore(monkeypatch) -> None:
    """App BookSourceValidator.doCheckSource (2226-2317): search and explore are independent checks; an empty search is
    `搜索失效` even if explore works."""
    explore_page = "<html><body><div class='b'><a href='/book/7'>探索书</a></div></body></html>"
    install(
        monkeypatch,
        {f"{BASE}/explore": explore_page},
        default="<html><body><p>nothing</p></body></html>",
    )
    src = _src(
        searchUrl=f"{BASE}/search?q={{{{key}}}}",
        exploreUrl=f"分类::{BASE}/explore",
        ruleSearch={"bookList": ".b", "name": "a@text", "bookUrl": "a@href"},
        ruleExplore={"bookList": ".b", "name": "a@text", "bookUrl": "a@href"},
    )
    runtime = validator.make_runtime(src)
    urls, _base = validator.fetch_book_candidates(requests.Session(), src, runtime)
    assert urls == []
    assert (runtime.variables or {}).get("__exploreOnlyRescue") == "1"
    outcome, _record, detail = validator.validate_single_source(src)
    assert not outcome.passed
    assert detail["stages"]["search"] is False
    assert "搜索失效" in str(detail.get("error") or "")
    # a source that has no search at all is judged by its explore
    explore_only = _src(exploreUrl=f"分类::{BASE}/explore", ruleExplore={"bookList": ".b", "name": "a@text", "bookUrl": "a@href"})
    urls, _base = _candidates(explore_only)
    assert urls == [f"{BASE}/book/7"]


# ----------------------------------------------------------------------------------- 5  F7: empty content rule


def test_item05_empty_content_rule_fails_text_sources_but_passes_audio_and_image(monkeypatch) -> None:
    """App getContent (E:9420-9436) returns the chapter URL as the text. User decision: a text source with that is a failure;
    audio (1) / image (2) sources whose chapter URL is the content still pass."""
    install(monkeypatch)  # no request may happen
    chapter_url = f"{BASE}/book/1/2.html"
    for source_type, rule in ((0, {}), (0, None), (0, {"content": ""}), (0, "not-a-dict"), (1, {}), (2, None), (1, {"content": ""})):
        src = _src(bookSourceType=source_type, ruleContent=rule)
        ok, content, pages = _content(src, chapter_url)
        assert ok is (source_type in (1, 2)), (source_type, rule)
        assert content == chapter_url and pages == 0


# --------------------------------------------------------------------------------------- 6  F8: replaceRegex applies


def test_item06_replace_regex_that_empties_the_content_is_a_failure(monkeypatch) -> None:
    """App E:9947-9952 + E:7008-7016: the replaceRegex / inline `##` result is always what the reader shows."""
    page = f"<html><body><div id='c'>{LONG_TEXT}</div></body></html>"
    install(monkeypatch, {f"{BASE}/chapter/1.html": page})
    ok, content, _pages = _content(_src(ruleContent={"content": "#c@text", "replaceRegex": "##.*"}))
    assert not ok and content == ""
    ok, content, _pages = _content(_src(ruleContent={"content": "#c@text##[^，。]+"}))  # inline replace removes the text, punctuation stays
    assert "足够长" not in content
    assert not validator.is_usable_chapter_text(content)
    # a replacement that keeps readable text still passes
    ok, content, _pages = _content(_src(ruleContent={"content": "#c@text", "replaceRegex": "##验证工具##阅读器"}))
    assert ok and "阅读器" in content and "验证工具" not in content


# ---------------------------------------------------------------------- 7  F9: no rescue retries the App does not have


class _HeaderSensitiveSession:
    """Answers an error page unless a User-Agent is sent - the case the old second attempt rescued."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def get(self, url: str, headers: dict[str, str] | None = None, **_kwargs: Any):
        self.calls.append(dict(headers or {}))
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp._content = (
            b"<html><head><title>OK</title></head><body>ok</body></html>"
            if "User-Agent" in (headers or {})
            else "<html><head><title>页面未找到</title></head><body>404</body></html>".encode("utf-8")
        )
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp


def test_item07_fetch_text_makes_a_single_attempt_with_the_source_headers() -> None:
    """The App only repeats the identical request (executeRequestWithRetry); it never swaps in browser default headers."""
    session = _HeaderSensitiveSession()
    body, _url, status = validator.fetch_text(session, f"{BASE}/book/1")
    assert status == 200 and "页面未找到" in body
    assert len(session.calls) == 1 and "User-Agent" not in session.calls[0]


def test_item07_cookie_free_session_retries_only_for_blank_rules_and_enabled_cookie_jar(monkeypatch) -> None:
    """App E:8759-8773 (detail), E:2784 (toc), E:9536 (content): the empty-body cookie-free retry is a blank-rule heuristic
    and also needs `source.cookieJarEnabled`."""
    body = f"<html><head><meta property='og:novel:book_name' content='重试书'></head><body><h1 class='t'>重试书</h1><div id='c'>{LONG_TEXT}</div><dl class='l'><dd><a href='/b/1.html'>第1章</a></dd></dl></body></html>"

    def empty_then_body(session, url, **_kwargs):
        calls.append(session)
        return ("", url, 0) if len(calls) == 1 else (body, url, 200)

    def run(call, src) -> int:
        """Number of distinct Sessions used: the cookie-free retry is the only one that switches to a new Session."""
        calls.clear()
        monkeypatch.setattr(validator, "fetch_text", empty_then_body)
        call(src)
        return len({id(session) for session in calls})

    calls: list[requests.Session] = []
    # detail
    assert run(_detail, _src(ruleBookInfo={"name": ""})) == 2
    assert run(_detail, _src(ruleBookInfo={"name": ".t@text"})) == 1
    assert run(_detail, _src(ruleBookInfo={"name": ""}, enabledCookieJar=False)) == 1
    # toc
    assert run(_toc, _src(ruleToc={"chapterName": "a@text"})) == 2
    assert run(_toc, _src(ruleToc={"chapterList": ".l dd", "chapterName": "a@text", "chapterUrl": "a@href"})) == 1
    # content
    assert run(_content, _src(ruleContent={"content": " "})) == 2
    assert run(_content, _src(ruleContent={"content": "#c@text"})) == 1


def test_item07_referer_retry_is_kept_but_only_for_pages_the_app_lets_through(monkeypatch) -> None:
    """App E:9583-9600: hot-link-protection pages (`ref=blocked` URL / error <title>) are retried once with the parent
    directory as Referer. A real 404 is thrown before that by executeRequestWithRetry, so it is a failure."""
    blocked = "<html><head><title>404</title></head><body>ref=blocked</body></html>"
    good = f"<html><body><div id='c'>{LONG_TEXT}</div></body></html>"
    seen: list[dict[str, str]] = []

    def make(blocked_status: int):
        def fetch(_session, url, **kwargs):
            headers = dict(kwargs.get("headers") or {})
            seen.append(headers)
            if headers.get("Referer") == f"{BASE}/book/1/":
                return good, url, 200
            return blocked, f"{url}?ref=blocked", blocked_status

        return fetch

    src = _src(ruleContent={"content": "#c@text"})
    monkeypatch.setattr(validator, "fetch_text", make(200))
    ok, content, _pages = _content(src, f"{BASE}/book/1/16.html")
    assert ok and "足够长的章节正文" in content and len(seen) == 2 and seen[1]["Referer"] == f"{BASE}/book/1/"
    seen.clear()
    monkeypatch.setattr(validator, "fetch_text", make(404))
    ok, _content_text, _pages = _content(src, f"{BASE}/book/1/16.html")
    assert not ok and len(seen) == 1


# ------------------------------------------------------------------------------------- 8  F10: JSON field guesses


def test_item08_json_guesses_run_only_for_blank_rules(monkeypatch) -> None:
    """App: kind / chapter title / chapter URL / content guesses are gated by `lacksRule` / `tocLacksRule` / `contentRuleIsBlank`."""
    # detail kind
    install(monkeypatch, {f"{BASE}/book/1": json.dumps({"name": "书", "kind": "玄幻,热血"}, ensure_ascii=False)})
    detail_ok, detail_name, _toc_url, _reason = _detail(_src(ruleBookInfo={"name": "$.name", "kind": "$.missingKind"}))
    assert detail_ok and detail_name == "书"  # (kind itself is not returned; a non-blank kind rule must not crash or invent text)
    # toc chapter title / url
    toc_json = json.dumps({"list": [{"title": "第一章", "url": "/c/1"}, {"title": "第二章", "url": "/c/2"}]}, ensure_ascii=False)
    install(monkeypatch, {f"{BASE}/book/1/toc": toc_json, f"{BASE}/book/1": toc_json})
    ok, *_rest = _toc(_src(ruleToc={"chapterList": "$.list[*]", "chapterName": "$.missingTitle", "chapterUrl": "$.url"}))
    assert not ok  # every title is empty -> every chapter is skipped (App `guard !title.isEmpty`)
    ok, _final, chapter_url, title, *_rest = _toc(_src(ruleToc={"chapterList": "$.list[*]", "chapterName": "", "chapterUrl": ""}))
    assert ok and (title, chapter_url) == ("第一章", f"{BASE}/c/1")  # blank rules: the key guesses apply
    # content
    install(monkeypatch, {f"{BASE}/chapter/1.html": json.dumps({"content": LONG_TEXT}, ensure_ascii=False)})
    ok, _text, _pages = _content(_src(ruleContent={"content": "$.body"}))
    assert not ok
    ok, text, _pages = _content(_src(ruleContent={"content": " "}))
    assert ok and "足够长的章节正文" in text


# ------------------------------------------------------------------------------------------ 9  F11: cookies / sessions


class _FakeCookieResponse:
    """What http.cookiejar needs from a response: `info()` returning a message with Set-Cookie headers."""

    def __init__(self) -> None:
        import email.message

        self._msg = email.message.Message()
        self._msg["Set-Cookie"] = "sid=abc; Path=/"

    def info(self):
        return self._msg


def test_item09_enabled_cookie_jar_false_stores_no_cookie_and_every_source_gets_its_own_session() -> None:
    """App: `enabledCookieJar` only controls whether Set-Cookie is saved (E:520-591), and cookies are per source."""
    import urllib.request

    keep = validator.make_source_session(_src())
    drop = validator.make_source_session(_src(enabledCookieJar=False))
    for session in (keep, drop):
        session.cookies.extract_cookies(_FakeCookieResponse(), urllib.request.Request(BASE + "/"))
    assert keep.cookies.get("sid") == "abc"
    assert drop.cookies.get("sid") is None
    # cookies the source sends itself (explicit header / jar entry) are untouched by the policy
    drop.cookies.set("manual", "1", domain="example.test")
    assert drop.cookies.get("manual") == "1"
    # a new source never inherits the cookies of an earlier one
    assert validator.make_source_session(_src()) is not keep
    assert not validator.make_source_session(_src()).cookies.get_dict()


def test_item09_validation_never_reuses_a_session_between_sources(monkeypatch) -> None:
    seen: list[requests.Session] = []  # keep the objects alive so ids cannot be recycled

    def fetch(session, url, **_kwargs):
        seen.append(session)
        return "", url, 0

    monkeypatch.setattr(validator, "fetch_text", fetch)
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("offline")))
    src = _src(searchUrl=f"{BASE}/s?q={{{{key}}}}", ruleSearch={"bookList": ".b", "name": "a@text", "bookUrl": "a@href"})
    validator.validate_single_source(src)
    first = list(seen)
    seen.clear()
    validator.validate_single_source(src)
    second = list(seen)
    assert first and second
    assert not ({id(item) for item in first} & {id(item) for item in second})


# ------------------------------------------------------------------------------------------- 10  F14: `/xpath##replace`


def test_item10_xpath_rule_with_replace_chain_applies_the_replacement() -> None:
    """RuleType.parseSourceRule splits `##` before choosing the mode, so `/xpath##regex##to` replaces in the XPath result."""
    html_page = "<html><body><div id='a'>Hello World</div></body></html>"
    runtime = validator.make_runtime(_src())
    rule = '//div[@id="a"]/text()##World##Readori'
    assert validator.evaluate_rule(rule, html_page, runtime) == ["Hello Readori"]
    assert validator.evaluate_single_rule(rule, html_page, runtime) == ["Hello Readori"]
    assert validator.evaluate_single_rule('//div[@id="a"]/text()', html_page, runtime) == ["Hello World"]


# ------------------------------------------------------------------------------------------- 11  F16: network errors


class _RaisingSession:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def get(self, *_args: Any, **_kwargs: Any):
        raise self.exc

    post = head = get


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (requests.exceptions.ConnectionError(socket.gaierror(11001, "getaddrinfo failed")), "dns"),
        (requests.exceptions.SSLError("certificate verify failed: unable to get local issuer certificate"), "tls"),
        (requests.exceptions.ConnectTimeout("Connection to example.test timed out"), "timed out"),
        (requests.exceptions.ReadTimeout("Read timed out."), "timed out"),
        (requests.exceptions.ConnectionError("[WinError 10061] connection refused by the target machine"), "connection refused"),
        (requests.exceptions.ConnectionError("Connection reset by peer"), "connection reset"),
        (requests.exceptions.TooManyRedirects("Exceeded 30 redirects."), "too many redirects"),
    ],
)
def test_item11_transport_failures_are_recorded_with_a_distinguishable_category(exc: BaseException, category: str) -> None:
    validator.begin_network_stage()
    assert validator.fetch_text(_RaisingSession(exc), f"{BASE}/x") == ("", f"{BASE}/x", 0)  # verdict unchanged: empty + status 0
    event = validator.first_network_event()
    assert event is not None and event["category"] == category and event["url"] == f"{BASE}/x"
    detail: dict[str, Any] = {"failureReason": "no search/explore result"}
    assert validator.attach_network_error(detail, "search")
    assert detail["failureReason"] == f"network error: {category}"
    assert detail["networkError"]["stage"] == "search" and detail["networkError"]["message"]


def test_item11_validation_reports_the_network_category_and_http_status(monkeypatch) -> None:
    src = _src(
        searchUrl=f"{BASE}/s?q={{{{key}}}}",
        ruleSearch={"bookList": "$.books[*]", "name": "$.name", "bookUrl": "$.url"},
        ruleBookInfo={"name": "$.name"},
        ruleToc={"chapterList": "$.list[*]", "chapterName": "$.title", "chapterUrl": "$.url"},
        ruleContent={"content": "$.content"},
    )
    # (a) DNS failure on every request
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kwargs: (_ for _ in ()).throw(
        requests.exceptions.ConnectionError(socket.gaierror(11001, "getaddrinfo failed"))))
    outcome, _record, detail = validator.validate_single_source(src)
    assert not outcome.passed
    assert detail["failureReason"] == "network error: dns" == outcome.reason
    assert detail["networkError"]["category"] == "dns"
    # (b) the search works but the detail answers 503
    books = json.dumps({"books": [{"name": "书", "url": "/book/1"}]}, ensure_ascii=False)
    install(monkeypatch, {f"{BASE}/book/1": ("<html>busy</html>", f"{BASE}/book/1", 503)}, default=(books, "", 200))
    outcome, _record, detail = validator.validate_single_source(src)
    assert not outcome.passed
    assert detail["stages"]["search"] is True and detail["stages"]["detail"] is False
    assert detail["failureReason"] == "network error: http 503"
    assert detail["networkError"]["status"] == 503 and detail["networkError"]["stage"] == "detail"


def test_item11_http_status_semantics_follow_the_app() -> None:
    """E:10353 shouldPassHTTPErrorBodyToRules: non-2xx throws, except loginCheckJs sources and passable 4xx JSON bodies."""
    blocks = validator.app_http_error_blocks_rules
    assert not blocks(200, "x", "toc") and not blocks(0, "", "toc") and not blocks(10, "", "toc")  # stub / transport statuses
    assert blocks(500, '{"a":1}', "content") and blocks(403, '{"a":1}', "content") and blocks(429, '{"a":1}', "search")
    assert blocks(404, "", "content") and blocks(404, "<title>404 Not Found</title>", "search")
    assert not blocks(404, '{"data":[1]}', "content")  # passable JSON body
    assert not blocks(500, "x", "content", has_login_check=True)
    assert blocks(400, "<div>plain</div>", "content") and not blocks(400, "<div>plain</div>", "search")  # HTML passes for search / explore only


# ------------------------------------------------------------------------------------------ 12  ruleSearch.init


@needs_js
def test_item12_rule_search_init_is_executed_like_the_app(monkeypatch) -> None:
    """App BookSourceEngine.swift:236-247: the search response goes through ruleSearch.init before bookList is evaluated."""
    raw = json.dumps({"payload": {"books": [{"name": "初始化书", "url": "/book/1"}]}}, ensure_ascii=False)
    install(monkeypatch, default=raw)
    rule = {"bookList": "$.books[*]", "name": "$.name", "bookUrl": "$.url"}
    without_init = _src(searchUrl=f"{BASE}/s?q={{{{key}}}}", ruleSearch=dict(rule))
    assert _candidates(without_init)[0] == []  # $.books[*] does not match the raw wrapper
    with_init = _src(searchUrl=f"{BASE}/s?q={{{{key}}}}", ruleSearch=dict(rule, init="<js>JSON.stringify(JSON.parse(result).payload)</js>"))
    assert _candidates(with_init)[0] == [f"{BASE}/book/1"]
    # a throwing init does not abort the search (App evaluates it non-throwing) - the raw body is used
    broken = _src(searchUrl=f"{BASE}/s?q={{{{key}}}}", ruleSearch=dict(rule, init="<js>throw new Error('x')</js>"))
    assert _candidates(broken)[0] == []


# ------------------------------------------------------------------------------- 13  unrecognised rules are not literals


def test_item13_unrecognised_rules_are_never_returned_as_literal_text() -> None:
    """RuleEvaluator `.contentDependent`: 字面量形态绝不把规则文本当结果. On HTML a rule that matches nothing is empty;
    JSON content keeps the `.text` / bare-field semantics."""
    runtime = validator.make_runtime(_src())
    html_page = "<html><body><p>正文</p></body></html>"
    for rule in ("nonexistentword", "some words here", "https://example.test/path", "/nope/at/all.html", "foo\nbar"):
        assert validator.evaluate_single_rule(rule, html_page, runtime) in ([], ), rule
    json_page = json.dumps({"name": "书名", "list": [1]}, ensure_ascii=False)
    assert validator.evaluate_single_rule("name", json_page, runtime) == ["书名"]  # bare field -> JSONPath
    assert validator.evaluate_single_rule("missing", json_page, runtime) == []
    # a rule the engine understands is still evaluated
    assert validator.evaluate_single_rule("p@text", html_page, runtime) == ["正文"]
    # makeUpRule templates (`@get:{key}`, `{{...}}`) are the one literal form the App keeps (differential fixture rule.template.get.variable)
    runtime.variables = {"k": "v"}
    assert validator.evaluate_single_rule("x@get:{k}", html_page, runtime) == ["xv"]
