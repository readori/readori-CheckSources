"""R151: the discovery candidate's detail / TOC / content chain (App parity) and an `@js:` / `<js>` explore URL (offline).

E1  BookSourceValidator.doCheckSource / validateCandidate (BookSourceValidator.swift:2276-2550): the first search book and the
    first discovery book are BOTH candidates; every candidate runs detail -> first TOC page -> first chapter content and ANY
    failure fails the source. appendCandidate dedupes by the lower-cased trimmed URL part of `url,{options}`.
E2  BookSourceEngine+Flagship.swift:149-196 explore(): a built URL that is still `@js:` / `<js>…</js>` is evaluated locally
    (no request, no login check); a script error throws (js失效); a JSONP body is unwrapped before the list is parsed.

All pages are served by a patched ``vsp.fetch_text`` that records every request (the request log is asserted).
"""
from __future__ import annotations

import json
from typing import Any, Callable

import pytest
import requests

from validator import validate_source_packages as vsp

BASE = "https://example.test"
SEARCH_PATH = "/search?q=%E6%88%91%E7%9A%84"  # the App's default keyword 我的
LONG_TEXT = "这是一段足够长的正文内容用于验证。这是一段足够长的正文内容用于验证。"


class Site:
    """Patched ``fetch_text``: answers by exact path (+query); records every request; unknown paths are a 404."""

    def __init__(self, pages: dict[str, Any]) -> None:
        self.pages = dict(pages)
        self.log: list[str] = []

    def __call__(self, session: requests.Session, url: str, **kwargs: Any) -> tuple[str, str, int]:
        path = url.replace(BASE, "")
        self.log.append(path)
        page = self.pages.get(path)
        if page is None:
            return "", url, 404
        body = page(url) if callable(page) else page
        return body, url, 200

    def requested(self, path: str) -> int:
        return self.log.count(path)


def _book_page(title: str, chapter: str, with_toc: bool = True) -> str:
    toc = f'<ul class="toc"><li><a href="{chapter}">第一章</a></li></ul>' if with_toc else "<p>no chapters</p>"
    return f"<h1>{title}</h1>{toc}"


def _content_page(text: str = LONG_TEXT) -> str:
    return f'<div id="c">{text}</div>'


def _list_page(href: str, name: str, tag: str) -> str:
    return f'<div class="b"><a href="{href}">{name}</a><span class="t">{tag}</span></div>'


def _pages(**over: Any) -> dict[str, Any]:
    pages: dict[str, Any] = {
        SEARCH_PATH: _list_page("/book/s", "搜索书", "S-TAG"),
        "/explore": _list_page("/book/e", "发现书", "E-TAG"),
        "/book/s": _book_page("S", "/c/s1"),
        "/book/e": _book_page("E", "/c/e1"),
        "/c/s1": _content_page(),
        "/c/e1": _content_page(),
    }
    pages.update(over)
    return pages


def _source(**over: Any) -> dict[str, Any]:
    src: dict[str, Any] = {
        "bookSourceName": "R151",
        "bookSourceUrl": BASE,
        "searchUrl": "/search?q={{key}}",
        "exploreUrl": "热门::/explore",
        # `@put` stores the item's own tag; the detail name template reads it back (java.get) - so each candidate's
        # detailBookName shows which variables its chain ran with.
        "ruleSearch": {"bookList": ".b", "name": "a@text@put:{tag:span.t@text}", "bookUrl": "a@href"},
        "ruleExplore": {"bookList": ".b", "name": "a@text@put:{tag:span.t@text}", "bookUrl": "a@href"},
        "ruleBookInfo": {"name": '{{java.get("tag")}}'},
        "ruleToc": {"chapterList": ".toc li", "chapterName": "a@text", "chapterUrl": "a@href"},
        "ruleContent": {"content": "#c@text"},
    }
    src.update(over)
    return src


def _install(monkeypatch, **over: Any) -> Site:
    site = Site(_pages(**over))
    monkeypatch.setattr(vsp, "fetch_text", site)
    return site


def _validate(src: dict[str, Any], seed: dict[str, Any] | None = None):
    return vsp.validate_single_source(src, quick_seed=seed)


# ------------------------------------------------------------------------------------------------- E1 candidate chain


def test_both_chains_good_pass_and_each_candidate_runs_with_its_own_variables(monkeypatch) -> None:
    """validateCandidate runs for `.search` and `.discovery` with independent SearchBooks: the discovery book's `@put`
    value is what ITS detail sees, and the search candidate's value is unchanged."""
    site = _install(monkeypatch)
    outcome, record, detail = _validate(_source())
    assert outcome.passed and record is not None
    assert detail["stages"] == {"search": True, "detail": True, "toc": True, "content": True}
    assert detail["detailBookName"] == "S-TAG"
    explore = detail["exploreCandidate"]
    assert explore["bookUrl"] == f"{BASE}/book/e"
    assert explore["stages"] == {"detail": True, "toc": True, "content": True}
    assert explore["detailBookName"] == "E-TAG"
    assert explore["firstChapterTitle"] == "第一章" and explore["contentPreviewChars"] > 0
    assert explore["failureReason"] == ""
    assert "reused" not in explore
    assert site.requested("/c/e1") == 1 and site.requested("/c/s1") == 1
    json.dumps(detail["exploreCandidate"])  # serialisable (it ships in the report)


def test_discovery_toc_failure_fails_the_source_with_the_discovery_toc_group(monkeypatch) -> None:
    """App validateCandidate: an empty first TOC page of the discovery book -> `发现目录失效` (exploreTocFailed); the
    search candidate's `stages` stay all True."""
    _install(monkeypatch, **{"/book/e": _book_page("E", "/c/e1", with_toc=False)})
    outcome, record, detail = _validate(_source())
    assert not outcome.passed and record is None
    assert outcome.reason == "explore toc failed"
    assert detail["failureReason"] == "explore toc failed" and detail["failureGroup"] == "发现目录失效"
    assert detail["stages"] == {"search": True, "detail": True, "toc": True, "content": True}
    assert detail["exploreCandidate"]["stages"] == {"detail": True, "toc": False, "content": False}
    assert detail["exploreCandidate"]["failureReason"] == "explore toc failed"
    assert detail.get("ok") is not True


def test_discovery_content_failure_fails_the_source_with_the_discovery_content_group(monkeypatch) -> None:
    """Empty first chapter text of the discovery book -> `发现正文失效` (exploreContentFailed)."""
    _install(monkeypatch, **{"/c/e1": '<div id="c">   </div>'})
    outcome, record, detail = _validate(_source())
    assert not outcome.passed and record is None
    assert detail["failureReason"] == "explore content failed" and detail["failureGroup"] == "发现正文失效"
    assert detail["stages"]["content"] is True
    assert detail["exploreCandidate"]["stages"] == {"detail": True, "toc": True, "content": False}
    assert detail["exploreCandidate"]["firstChapterTitle"] == "第一章"
    assert detail["exploreCandidate"]["contentPreviewChars"] == 0


def test_discovery_detail_error_json_fails_the_source_as_a_site_failure(monkeypatch) -> None:
    """validateCandidate: a detail error is classifyError(error) - not a script error -> `网站失效`; the error text of the
    detail stage is kept."""
    body = json.dumps({"success": False, "msg": "denied"})
    _install(monkeypatch, **{"/book/e": body})
    outcome, record, detail = _validate(_source())
    assert not outcome.passed and record is None
    assert detail["failureReason"] == "explore detail failed" and detail["failureGroup"] == "网站失效"
    assert detail["error"] == "denied"
    assert detail["exploreCandidate"]["stages"] == {"detail": False, "toc": False, "content": False}
    assert detail["stages"]["content"] is True


def test_discovery_detail_script_error_is_a_js_failure(monkeypatch) -> None:
    """A source-script exception in the discovery book's detail rules is `js失效` (classifyError: RuleScriptError),
    reported against the explore chain stage."""
    _install(monkeypatch)
    src = _source()
    src["ruleBookInfo"] = {"name": '{{ java.get("tag") == "E-TAG" ? (function(){ throw new Error("explore-only") })() : java.get("tag") }}'}
    outcome, record, detail = _validate(src)
    assert not outcome.passed and record is None
    assert detail["failureReason"] == "js error" and detail["failureGroup"] == "js失效"
    assert detail["failedStage"] == "explore detail"
    assert "explore-only" in detail["error"]
    assert detail["stages"]["content"] is True  # the search candidate had passed


@pytest.mark.parametrize("variant", ["/Book/S", '/book/s,{"x":1}', "/BOOK/s,{\"method\":\"GET\"}"])
def test_discovery_book_equal_to_the_search_book_reuses_the_result(monkeypatch, variant: str) -> None:
    """appendCandidate key = lower-cased trimmed URL part: a duplicate discovery book gets no second chain and no request."""
    site = _install(monkeypatch, **{"/explore": f"<div class='b'><a href='{variant}'>同一本</a><span class='t'>X</span></div>"})
    outcome, record, detail = _validate(_source())
    assert outcome.passed and record is not None
    assert detail["exploreCandidate"]["reused"] is True
    assert set(detail["exploreCandidate"]) == {"bookUrl", "reused"}
    # one chain only: the search book's detail page is fetched by detail + toc (2), nothing for the discovery URL variant
    assert site.log.count("/book/s") == 2 and site.requested("/c/s1") == 1
    assert [path for path in site.log if path.lower().startswith("/book/") and path != "/book/s"] == []


def test_the_search_chain_failure_reasons_are_unchanged(monkeypatch) -> None:
    """The search candidate keeps its own reasons (`toc failed`), and the discovery chain is not reached."""
    site = _install(monkeypatch, **{"/book/s": _book_page("S", "/c/s1", with_toc=False)})
    outcome, _record, detail = _validate(_source())
    assert not outcome.passed and detail["failureReason"] == "toc failed"
    assert "exploreCandidate" not in detail and "/book/e" not in site.log


def test_failed_search_is_unchanged(monkeypatch) -> None:
    """A search that finds nothing already fails (`搜索失效`); a working discovery never starts the explore chain."""
    site = _install(monkeypatch, **{SEARCH_PATH: "<p>nothing</p>"})
    outcome, _record, detail = _validate(_source())
    assert not outcome.passed and detail["failureReason"] == "no search/explore result"
    assert "exploreCandidate" not in detail
    assert site.requested("/book/e") == 0 and site.requested("/c/e1") == 0


def test_explore_only_source_is_unchanged(monkeypatch) -> None:
    """No search: the discovery book already is the primary chain (one chain, no `exploreCandidate`)."""
    site = _install(monkeypatch)
    src = _source()
    for key in ("searchUrl", "ruleSearch"):
        src.pop(key)
    outcome, record, detail = _validate(src)
    assert outcome.passed and record is not None
    assert "exploreCandidate" not in detail
    assert detail["detailBookName"] == "E-TAG"
    assert site.requested("/c/e1") == 1 and site.requested("/book/s") == 0


def test_a_broken_discovery_book_does_not_matter_when_the_source_has_no_discovery(monkeypatch) -> None:
    """Only a source with both entries gets the discovery chain."""
    site = _install(monkeypatch, **{"/book/e": "<p>broken</p>"})
    src = _source()
    src.pop("exploreUrl")
    outcome, _record, detail = _validate(src)
    assert outcome.passed and "exploreCandidate" not in detail and site.requested("/explore") == 0


def test_fetch_book_candidates_keeps_its_signature_and_exposes_the_candidate_on_the_runtime(monkeypatch) -> None:
    _install(monkeypatch)
    src = _source()
    runtime = vsp.make_runtime(src)
    result = vsp.fetch_book_candidates(requests.Session(), src, runtime)
    assert isinstance(result, tuple) and len(result) == 2
    urls, base = result
    assert urls == [f"{BASE}/book/s"] and isinstance(base, str)
    candidate = runtime.explore_candidate
    assert candidate["bookUrl"] == f"{BASE}/book/e" and candidate["reused"] is False
    assert candidate["variables"].get("tag") == "E-TAG" and candidate["base"].startswith(BASE)
    # the search item's variables stay on the runtime, the discovery item's do not overwrite them
    assert (runtime.variables or {}).get("tag") == "S-TAG"
    # a second call starts clean (no stale candidate when the explore pass is not run)
    vsp.fetch_book_candidates(requests.Session(), src, runtime, modes=("search",))
    assert runtime.explore_candidate is None


def test_quick_scan_does_not_run_the_chain_and_the_seed_carries_the_candidate(monkeypatch) -> None:
    """Quick scan only does the entry checks; its seed carries the discovery candidate (serialisable), so the full
    validation runs the explore chain without fetching the discovery list or the search again."""
    site = _install(monkeypatch)
    src = _source()
    passed, seed, quick_detail = vsp.quick_scan_single_source(src)
    assert passed and seed is not None
    assert site.requested("/book/e") == 0 and site.requested("/c/e1") == 0 and site.requested("/book/s") == 0
    assert seed["exploreCandidate"]["bookUrl"] == f"{BASE}/book/e"
    assert seed["exploreCandidate"]["variables"]["tag"] == "E-TAG"
    json.dumps(seed["exploreCandidate"])

    site.log.clear()
    outcome, record, detail = _validate(src, seed)
    assert outcome.passed and record is not None
    assert site.requested("/explore") == 0 and site.requested(SEARCH_PATH) == 0
    assert site.requested("/c/e1") == 1 and site.requested("/c/s1") == 1
    assert detail["exploreCandidate"]["detailBookName"] == "E-TAG"
    assert detail["detailBookName"] == "S-TAG"


def test_seeded_validation_reports_a_discovery_toc_failure(monkeypatch) -> None:
    site = _install(monkeypatch, **{"/book/e": _book_page("E", "/c/e1", with_toc=False)})
    src = _source()
    passed, seed, _detail = vsp.quick_scan_single_source(src)
    assert passed and seed is not None
    outcome, record, detail = _validate(src, seed)
    assert not outcome.passed and record is None
    assert detail["failureReason"] == "explore toc failed" and detail["failureGroup"] == "发现目录失效"
    assert site.requested(SEARCH_PATH) == 1  # only the quick scan searched


def test_seeded_validation_of_a_seed_without_a_candidate_has_no_discovery_chain(monkeypatch) -> None:
    site = _install(monkeypatch)
    src = _source()
    passed, seed, _detail = vsp.quick_scan_single_source(src)
    assert passed and seed is not None
    seed = {**seed, "exploreCandidate": None}
    outcome, _record, detail = _validate(src, seed)
    assert outcome.passed and "exploreCandidate" not in detail and site.requested("/book/e") == 0


def test_stability_rounds_run_the_chain_without_a_seed_every_time(monkeypatch) -> None:
    """validate_group (the stability round path) validates without a seed: the discovery chain runs on every round."""
    site = _install(monkeypatch)
    src = _source()
    for round_number in range(1, 3):
        _url, record, detail = vsp.validate_group(BASE, [src])
        assert record is not None and detail["exploreCandidate"]["stages"]["content"] is True
        assert site.requested("/c/e1") == round_number


def test_explore_chain_network_error_replaces_the_reason_like_the_search_chain(monkeypatch) -> None:
    """A transport error of the discovery book's request is a `network error: …` (site failure) - not the discovery TOC
    group; the failure group falls back to the derivation (`网站失效`)."""
    site = _install(monkeypatch)
    real = site.__call__

    def broken(session: requests.Session, url: str, **kwargs: Any):
        if url.endswith("/book/e"):
            vsp.record_network_event(url, "ConnectionError: refused")
            return "", url, 0
        return real(session, url, **kwargs)

    monkeypatch.setattr(vsp, "fetch_text", broken)
    outcome, _record, detail = _validate(_source())
    assert not outcome.passed
    assert detail["failureReason"].startswith("network error:")
    assert "failureGroup" not in detail
    assert detail["networkError"]["stage"] == "explore detail"
    assert detail["exploreCandidate"]["failureReason"] == detail["failureReason"]


# ------------------------------------------------------------------------------------------- E2  @js: / <js> explore URL

JS_LIST = 'JSON.stringify({list:[{n:"JS书",u:"/book/j"}]})'
JSON_RULE = {"bookList": "$.list[*]", "name": "$.n", "bookUrl": "$.u"}


def _explore_only(entry: str, rule: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"bookSourceName": "R151 js", "bookSourceUrl": BASE, "exploreUrl": entry, "ruleExplore": rule or JSON_RULE}


def _explore(src: dict[str, Any]) -> tuple[list[str], str, vsp.RuleRuntime, list[Any]]:
    runtime = vsp.make_runtime(src)
    sink = vsp.begin_script_error_stage(runtime)
    urls, base = vsp.fetch_book_candidates(requests.Session(), src, runtime, modes=("explore",))
    runtime.script_error_sink = None
    return urls, base, runtime, sink


def test_explore_url_still_js_after_the_build_is_evaluated_without_a_request(monkeypatch) -> None:
    """Flagship.swift:151-196: the template `@js:'@js:…'` builds to the text `@js:JSON.stringify(…)` which is still a
    script: it is run locally (input "", response = output / effective base URL / 200) and no request is made."""
    site = _install(monkeypatch)
    urls, base, _runtime, sink = _explore(_explore_only(f"热门::@js:'@js:{JS_LIST}'"))
    assert urls == [f"{BASE}/book/j"]
    assert base == BASE
    assert site.log == []
    assert not [entry for entry in sink if entry.get("kind") == "error"]


def test_explore_js_url_script_error_is_a_js_failure_of_the_explore_stage(monkeypatch) -> None:
    """The script's exception is not swallowed into an empty list: the explore stage fails like the App's RuleScriptError."""
    site = _install(monkeypatch)
    src = _explore_only("热门::@js:'@js:throw new Error(\"boom\")'")
    runtime = vsp.make_runtime(src)
    sink = vsp.begin_script_error_stage(runtime)
    urls, _base = vsp.fetch_book_candidates(requests.Session(), src, runtime, modes=("explore",))
    assert urls == [] and site.log == []
    with pytest.raises(vsp.RuleScriptError, match="boom"):
        vsp.finish_script_error_stage(runtime, sink, {}, "search")
    # the batch result of the full validation: js error / js失效
    outcome, record, detail = _validate(src)
    assert not outcome.passed and record is None
    assert detail["failureReason"] == "js error" and detail["failureGroup"] == "js失效"
    assert "boom" in detail["error"]


def test_explore_js_url_script_error_after_a_passing_search_is_js_failure_in_quick_scan_and_full(monkeypatch) -> None:
    """Search passed, the discovery entry's script throws: the App adds `js失效` (not `发现失效`)."""
    _install(monkeypatch)
    src = _source(exploreUrl="热门::@js:'@js:throw new Error(\"boom\")'")
    passed, seed, quick_detail = vsp.quick_scan_single_source(src)
    assert not passed and seed is None
    assert quick_detail["failureReason"] == "js error" and quick_detail["failureGroup"] == "js失效"
    outcome, _record, detail = _validate(src)
    assert not outcome.passed and detail["failureReason"] == "js error" and detail["failureGroup"] == "js失效"


def test_explore_js_variant_with_a_js_tag_block_is_evaluated_locally(monkeypatch) -> None:
    """`<js>…</js>` (hasPrefix / hasSuffix, case-sensitive) takes the same branch. The URL builder strips `<…>` page
    segments from a template output (as the App's applyLegadoPageSegments does), so the built value is injected here."""
    site = _install(monkeypatch)
    real = vsp.request_search_or_explore

    def built_js_block(session, src, runtime, keyword, page=1, mode="search", template=None):
        request = real(session, src, runtime, keyword, page=page, mode=mode, template=template)
        if mode != "explore" or request is None:
            return request
        return f"<js>{JS_LIST}</js>", request[1], request[2]

    monkeypatch.setattr(vsp, "request_search_or_explore", built_js_block)
    urls, base, _runtime, _sink = _explore(_explore_only("热门::/placeholder"))
    assert urls == [f"{BASE}/book/j"] and base == BASE
    assert site.log == []


def test_explore_js_url_prefixes_are_case_sensitive_like_the_app(monkeypatch) -> None:
    assert vsp.explore_js_url_script("  @js:1+1 ") == "1+1"
    assert vsp.explore_js_url_script("<js>1+1</js>") == "1+1"
    assert vsp.explore_js_url_script("@JS:1+1") is None
    assert vsp.explore_js_url_script("<JS>1+1</JS>") is None
    assert vsp.explore_js_url_script("<js>1+1") is None
    assert vsp.explore_js_url_script("/rank") is None


def test_explore_js_url_puts_variables_for_the_list_parse(monkeypatch) -> None:
    """The script runs in the request context (same RuleData as AnalyzeUrl): `java.put` stays visible afterwards."""
    site = _install(monkeypatch)
    script = 'java.put("k","v"); JSON.stringify({list:[{n:java.get("k"),u:"/book/j"}]})'
    urls, _base, runtime, _sink = _explore(_explore_only(f"热门::@js:'@js:{script}'"))
    assert urls == [f"{BASE}/book/j"] and site.log == []
    assert (runtime.variables or {}).get("k") == "v"
    response = vsp.evaluate_explore_js_url(runtime, "'x' + 1", {"X-A": "1"})
    assert response == ("x1", BASE, 200)


def test_explore_js_url_response_skips_the_http_and_login_checks(monkeypatch) -> None:
    """No request -> `app_http_failure` / `loginCheckJs` do not run for the scripted response."""
    _install(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(vsp, "app_http_failure", lambda *args, **kwargs: calls.append("http") or True)
    monkeypatch.setattr(vsp, "apply_login_check_js", lambda *args, **kwargs: calls.append("login") or (False, "", ""))
    urls, _base, _runtime, _sink = _explore(_explore_only(f"热门::@js:'@js:{JS_LIST}'"))
    assert urls == [f"{BASE}/book/j"] and calls == []


def test_explore_jsonp_body_is_unwrapped_for_the_request_branch(monkeypatch) -> None:
    """Flagship.swift:224-227: detectContentType == .json (JSONP included) -> extractJSONPBody before the list rule."""
    _install(monkeypatch, **{"/rank": 'cb({"list":[{"n":"JP书","u":"/book/p"}]});'})
    urls, _base, _runtime, _sink = _explore(_explore_only("热门::/rank"))
    assert urls == [f"{BASE}/book/p"]


def test_explore_jsonp_output_is_unwrapped_for_the_js_branch(monkeypatch) -> None:
    site = _install(monkeypatch)
    script = '"cb("+JSON.stringify({list:[{n:"JP书",u:"/book/p"}]})+")"'
    urls, _base, _runtime, _sink = _explore(_explore_only(f"热门::@js:'@js:{script}'"))
    assert urls == [f"{BASE}/book/p"] and site.log == []


def test_extract_jsonp_body_and_content_detection_follow_the_app() -> None:
    assert vsp.extract_jsonp_body(' cb({"a":1}) ') == '{"a":1}'
    assert vsp.extract_jsonp_body("cb([1,2]);") == "[1,2]"
    assert vsp.extract_jsonp_body('{"a":1}') is None  # already plain JSON
    assert vsp.extract_jsonp_body("cb(not json)") is None
    assert vsp.extract_jsonp_body(")(") is None  # last `)` before the first `(` must not raise
    assert vsp.detect_content_is_json('﻿ {"a":1}')
    assert vsp.detect_content_is_json('cb ( {"a":1})') and vsp.detect_content_is_json("x([1])")
    assert not vsp.detect_content_is_json("<html></html>") and not vsp.detect_content_is_json("cb(1)")
    assert not vsp.detect_content_is_json("")


def test_a_plain_json_explore_response_is_not_touched(monkeypatch) -> None:
    _install(monkeypatch, **{"/rank": '{"list":[{"n":"书","u":"/book/q"}]}'})
    urls, _base, _runtime, _sink = _explore(_explore_only("热门::/rank"))
    assert urls == [f"{BASE}/book/q"]
