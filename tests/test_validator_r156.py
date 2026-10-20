"""R156: validator emulation gaps found by the rrs batch (offline; patched fetches and synthetic pages only).

Validator-only fixes (both App profiles): network G1 / T, typed data: options, explore JSON, tocContent B / H / C / Q / F / N / JS##,
searchjs X / D / J / P / R / T, dedupe key.
Profile 1.1.2 only (mirror the Swift package E1): A, D, J (field order), G, E, U+200B, G2, page segment in a POST body.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from validator import app_profiles
from validator import validate_source_packages as vsp

BASE = "https://example.test"
needs_quickjs = pytest.mark.skipif(vsp.quickjs is None, reason="QuickJS bridge only")
needs_node = pytest.mark.skipif(not vsp.node_binary_path(), reason="node is not available")
LONG_TEXT = "这是一段足够长的正文内容用于验证。这是一段足够长的正文内容用于验证。"


class Site:
    """Patched ``fetch_text``: answers by exact path (+query), records every request."""

    def __init__(self, pages: dict[str, Any]) -> None:
        self.pages = dict(pages)
        self.log: list[str] = []

    def __call__(self, session: requests.Session, url: str, **kwargs: Any) -> tuple[str, str, int]:
        path = url.replace(BASE, "")
        self.log.append(path)
        page = self.pages.get(path)
        if page is None:
            return "", url, 404
        return (page(url) if callable(page) else page), url, 200

    def requested(self, path: str) -> int:
        return self.log.count(path)


def _runtime(src: dict[str, Any] | None = None) -> vsp.RuleRuntime:
    return vsp.make_runtime(src or {"bookSourceName": "R156", "bookSourceUrl": BASE})


def _walk(monkeypatch, pages: dict[str, Any], src: dict[str, Any], *, first_page_only: bool = False, toc_url: str = "/toc"):
    site = Site(pages)
    monkeypatch.setattr(vsp, "fetch_text", site)
    result = vsp.resolve_toc_source(
        requests.Session(), src, vsp.make_runtime(src), f"{BASE}/book", f"{BASE}{toc_url}", first_page_only=first_page_only
    )
    return site, result


def _toc_page(count: int, prefix: str = "c", start: int = 1) -> str:
    items = "".join(f'<li><a href="/{prefix}/{n}">第{n}章</a></li>' for n in range(start, start + count))
    return f'<ul class="toc">{items}</ul>'


def _toc_src(**toc: Any) -> dict[str, Any]:
    rule = {"chapterList": ".toc li", "chapterName": "a@text", "chapterUrl": "a@href"}
    rule.update(toc)
    return {"bookSourceName": "R156 toc", "bookSourceUrl": BASE, "ruleToc": rule}


# ------------------------------------------------------------------------------------------------ profile flags


NEW_FLAGS = (
    "txt_shortcut_only_without_toc_rule",
    "json_miss_runs_following_js",
    "list_item_legado_field_order",
    "get_element_returns_list",
    "empty_chapter_url_rule_uses_base_url",
    "js_strip_format_chars",
    "http_error_body_to_rules",
    "page_segment_in_post_body",
)


@pytest.mark.parametrize("flag", NEW_FLAGS)
def test_new_profile_flags_are_off_for_1_1_1_and_on_for_1_1_2(flag: str) -> None:
    with app_profiles.app_profile_scope("1.1.1"):
        assert getattr(app_profiles.current_app_profile(), flag) is False
    with app_profiles.app_profile_scope("1.1.2"):
        assert getattr(app_profiles.current_app_profile(), flag) is True


# ------------------------------------------------------------------------------------------- network G1 (guess)


def test_g1_a_failing_all_chapters_jump_page_restores_the_first_page_only_on_1_1_2(monkeypatch) -> None:
    """The jump page 404s. App 1.1.2 restores the first page's chapters; 1.1.1 (the App clears them for the jump and does not
    restore them on a request failure) fails the TOC."""
    src = {"bookSourceName": "G1", "bookSourceUrl": BASE, "ruleToc": {}}
    html = '<a href="/all.html">全部章节</a>' + _toc_page(3)
    with app_profiles.app_profile_scope("1.1.2"):
        site, result = _walk(monkeypatch, {"/toc": html}, src)
        assert result[0] is True, result
        assert site.requested("/all.html") >= 1  # the guess was tried ...
        assert result[1] == f"{BASE}/toc" and result[2] == f"{BASE}/c/1"  # ... and the first page's chapters were kept
        assert result[6] == 3
    with app_profiles.app_profile_scope("1.1.1"):
        site, result = _walk(monkeypatch, {"/toc": html}, src)
        assert result[0] is False, result


def test_g1_a_failing_range_option_page_keeps_the_first_page_on_both_profiles(monkeypatch) -> None:
    """The App keeps the first page's chapters when a range-option page fails (they are not cleared for it)."""
    html = (
        '<select><option value="/r1.html">第1-20章</option><option value="/r2.html">第21-40章</option></select>' + _toc_page(3)
    )
    src = _toc_src()
    for profile in ("1.1.1", "1.1.2"):
        with app_profiles.app_profile_scope(profile):
            site, result = _walk(monkeypatch, {"/toc": html}, src)
            assert result[0] is True, (profile, result)
            assert result[2] == f"{BASE}/c/1"


def test_g1_a_working_guessed_all_chapters_page_still_replaces_the_first_page(monkeypatch) -> None:
    src = {"bookSourceName": "G1", "bookSourceUrl": BASE, "ruleToc": {}}
    html = '<a href="/all.html">全部章节</a>' + _toc_page(3)
    site, result = _walk(monkeypatch, {"/toc": html, "/all.html": _toc_page(5, "y")}, src)
    assert result[0] is True
    assert result[1] == f"{BASE}/all.html" and result[2] == f"{BASE}/y/1"
    assert result[6] == 5


# ----------------------------------------- the App's all-chapters jump with a chapterList rule (Swift :3422-3440)


def test_h_the_all_chapters_jump_also_runs_with_a_chapter_list_rule_and_keeps_a_smaller_non_empty_result(monkeypatch) -> None:
    """App BookSourceEngine.swift:3422-3440 jumps with or without a chapterList rule; a jump page with fewer but >0 chapters
    keeps its result (:3357 drops the backup)."""
    html = '<a href="/all.html">全部章节</a>' + _toc_page(5)
    site, result = _walk(monkeypatch, {"/toc": html, "/all.html": _toc_page(2, "x")}, _toc_src())
    assert result[0] is True
    assert result[1] == f"{BASE}/all.html" and result[2] == f"{BASE}/x/1"
    assert result[6] == 2


def test_h_a_jump_page_without_any_usable_chapter_restores_the_first_page(monkeypatch) -> None:
    """tadu: the jump page lists nothing the rule can use -> the first page's chapters are restored (:2940 / :3546)."""
    html = '<a href="/all.html">全部章节</a>' + _toc_page(2)
    site, result = _walk(monkeypatch, {"/toc": html, "/all.html": "<p>no list</p>"}, _toc_src())
    assert result[0] is True
    assert site.requested("/all.html") == 1
    assert result[1] == f"{BASE}/toc" and result[2] == f"{BASE}/c/1"
    assert result[6] == 2


def test_h_a_jump_page_whose_items_yield_no_chapter_restores_the_first_page(monkeypatch) -> None:
    """The jump page matches the list rule but no item has a chapter URL: zero new chapters -> restore (:3546)."""
    html = '<a href="/all.html">全部章节</a>' + _toc_page(2)
    broken = '<ul class="toc"><li><span>no link</span></li></ul>'
    site, result = _walk(monkeypatch, {"/toc": html, "/all.html": broken}, _toc_src())
    assert result[0] is True
    assert result[1] == f"{BASE}/toc" and result[6] == 2


# --------------------------------------------------------------------------------------------- network T window


def test_t_the_batch_toc_stops_after_16_chapters_but_the_default_walk_keeps_going(monkeypatch) -> None:
    pages = {
        "/toc": _toc_page(10) + '<a class="n" href="/toc2">下一页</a>',
        "/toc2": _toc_page(10, start=11) + '<a class="n" href="/toc3">下一页</a>',
        "/toc3": _toc_page(10, start=21),
    }
    src = _toc_src(nextTocUrl="a.n@href")
    assert vsp.APP_VALIDATION_TOC_CHAPTER_LIMIT == 16
    site, windowed = _walk(monkeypatch, pages, src, first_page_only=True)
    assert windowed[0] is True
    assert site.requested("/toc3") == 0
    site, full = _walk(monkeypatch, pages, src, first_page_only=False)
    assert full[0] is True
    assert site.requested("/toc3") == 1


def test_t_node_binary_path_is_cached() -> None:
    first = vsp.node_binary_path()
    assert vsp.node_binary_path() == first
    assert vsp._NODE_BINARY_CACHE  # populated after the first lookup


def test_t_first_candidate_only_context_limits_candidate_collection(monkeypatch) -> None:
    src = {
        "bookSourceName": "T",
        "bookSourceUrl": BASE,
        "ruleSearch": {"bookList": ".b", "name": "a@text", "bookUrl": "a@href"},
    }
    runtime = _runtime(src)
    items = [f'<div class="b"><a href="/book/{n}">书{n}</a></div>' for n in range(1, 6)]
    all_urls, _vars, _err = vsp.collect_app_book_candidates(src, src["ruleSearch"], items, runtime)
    first_urls, _vars, _err = vsp.collect_app_book_candidates(src, src["ruleSearch"], items, runtime, first_only=True)
    assert len(all_urls) == 5
    assert len(first_urls) == 1


# ------------------------------------------------------------------------------------- network: options / explore


def test_data_url_option_types_are_preserved_and_keyword_is_prebound(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch(session, url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return "<html></html>", url, 200

    monkeypatch.setattr(vsp, "fetch_text", fake_fetch)
    src = {"bookSourceName": "opts", "bookSourceUrl": BASE, "searchUrl": '/s,{"method":"POST","body":"kw={{key}}&p={{page}}"}'}
    runtime = _runtime(src)
    url, options = vsp._build_url_with_options(src["searchUrl"], "我的", 1, runtime)
    assert url.endswith("/s")
    assert options.get("method") == "POST"
    assert "kw=" in str(options.get("body")) and "p=1" in str(options.get("body"))


def test_explore_json_with_literal_newlines_in_strings_parses_like_gson() -> None:
    text = '[{"title":"分类","url":"/x?a=1\nb"}]'
    kinds = vsp.parse_explore_urls(text, "src")
    assert kinds and kinds[0][0] == "分类"
    parsed = vsp.parse_explore_kinds_app('[{"title":"a\nb","url":"/x"}]', "src")
    assert parsed


def test_page_segment_helper_expands_the_page_variants() -> None:
    assert vsp.apply_legado_page_segments("a<1,2>b", 1) == "a1b"
    assert vsp.apply_legado_page_segments("a<1,2>b", 2) == "a2b"
    assert vsp.apply_legado_page_segments("a<1,2>b", 5) == "a2b"
    assert vsp.apply_legado_page_segments("kw=1&<,&page=2>", 3, True) == "kw=1&&page=2"
    assert vsp.apply_legado_page_segments("kw=1&<,&page=2>", 1, True) == "kw=1&"


def test_page_segment_in_post_body_is_profile_1_1_2_only() -> None:
    src = {"bookSourceName": "pg", "bookSourceUrl": BASE}
    template = '/s,{"method":"POST","body":"kw={{key}}<,&page={{page}}>"}'
    runtime = _runtime(src)
    with app_profiles.app_profile_scope("1.1.2"):
        _url, options = vsp._build_url_with_options(template, "ab", 2, runtime)
        assert options.get("body") == "kw=ab&page=2"
        _url, first = vsp._build_url_with_options(template, "ab", 1, runtime)
        assert first.get("body") == "kw=ab"
    with app_profiles.app_profile_scope("1.1.1"):
        _url, options = vsp._build_url_with_options(template, "ab", 2, runtime)
        assert options.get("body") != "kw=ab&page=2"


# ------------------------------------------------------------------------------------------- tocContent B / N / ##


def test_b_xpath_attribute_only_rule_selects_every_element_with_the_attribute() -> None:
    runtime = _runtime()
    html = '<ul><li><a href="/1">a</a></li><li><a href="/2">b</a></li></ul>'
    assert vsp.evaluate_xpath_rule("//@href", html, runtime, "html") == ["/1", "/2"]
    assert vsp.evaluate_xpath_rule("//li/@id", '<ul><li id="7">a</li></ul>', runtime, "html") == ["7"]


def test_n_json_prefix_in_front_of_a_literal_template_is_stripped() -> None:
    runtime = _runtime()
    assert vsp.evaluate_rule("@Json:{$.a}|{$.b}", '{"a":"x","b":"y"}', runtime) == ["x|y"]
    assert vsp.evaluate_rule("@json:https://e.test/{{$.a}}", '{"a":"v"}', runtime) == ["https://e.test/v"]


@needs_quickjs
def test_js_rule_splits_the_trailing_regex_replace_off_the_script() -> None:
    """Legado AnalyzeRule.kt:708-719: `@js:code##regex##repl` -> the replace applies to the script's result."""
    runtime = _runtime()
    assert vsp.evaluate_rule('@js:"abxcx"##x##-', "<a>hi</a>", runtime) == ["ab-c-"]


# ----------------------------------------------------------------------------------------------- tocContent F / E


def test_f_jsoup_regex_attribute_selector_matches_by_regex() -> None:
    runtime = _runtime()
    html = '<div class="x article">hello</div><div class="y">no</div>'
    assert vsp.evaluate_rule("div[class~=content|article]@text", html, runtime) == ["hello"]
    html = '<table><tr bgcolor="#eefaee"><td>a</td></tr><tr bgcolor="#fff"><td>b</td></tr></table>'
    assert vsp.evaluate_rule("tr[bgcolor~=eefaee]@text", html, runtime) == ["a"]


# ------------------------------------------------------------------------------------------------ tocContent Q


@pytest.mark.parametrize(
    "code, deferred",
    [
        ('java.startBrowserAwait(u,"t")', True),
        ('if (a) { java.startBrowserAwait(u,"t"); }', False),
        ('if (a) java.startBrowserAwait(u,"t")', False),
        ('if (a) { x() } else { startBrowser(u,"t") }', False),
        ('var s="{"; java.startBrowserAwait(u,"t");', True),  # a brace inside a string is not a block
        ('// if (x) {\njava.startBrowserAwait(u,"t")', True),  # nor is one in a comment
    ],
)
def test_q_browser_call_guard_uses_a_string_and_comment_free_brace_stack(code: str, deferred: bool) -> None:
    src = {"ruleContent": {"content": "@js:" + code}}
    assert vsp.is_browser_deferred_content_source(src) is deferred


# ------------------------------------------------------------------------------------------------ tocContent C


def test_c_any_non_blank_text_passes_when_the_content_rule_is_non_empty(monkeypatch) -> None:
    src = {
        "bookSourceName": "C",
        "bookSourceUrl": BASE,
        "ruleContent": {"content": "#c@text"},
    }
    site = Site({"/c/1": '<div id="c">短</div>'})
    monkeypatch.setattr(vsp, "fetch_text", site)
    ok, text, _count = vsp.resolve_chapter_content(requests.Session(), src, _runtime(src), f"{BASE}/c/1", "第一章", "书")
    assert ok is True
    assert "短" in text


def test_c_a_blank_result_still_fails(monkeypatch) -> None:
    src = {"bookSourceName": "C", "bookSourceUrl": BASE, "ruleContent": {"content": "#c@text"}}
    site = Site({"/c/1": '<div id="c">   </div>'})
    monkeypatch.setattr(vsp, "fetch_text", site)
    ok, _text, _count = vsp.resolve_chapter_content(requests.Session(), src, _runtime(src), f"{BASE}/c/1", "第一章", "书")
    assert ok is False


# ------------------------------------------------------------------------------------------------- searchjs X


def test_x_list_item_xpath_with_a_single_slash_is_relative_to_the_fragment_root() -> None:
    html = "<div><a><p>one</p><p>two</p></a></div>"
    assert vsp.xpath_extract(html, "/a/p[1]/text()", None, BASE) == ["one"]
    full = "<html><body><div><a>x</a></div></body></html>"
    assert vsp.xpath_extract(full, "/a", None, BASE) == []  # a full document keeps the absolute meaning


# ------------------------------------------------------------------------------------------------- searchjs D / P


def test_d_jsoup_shim_element_is_not_serialized_as_json() -> None:
    assert vsp.json_container_to_string({"_html": "<p>x</p>"}) == "<p>x</p>"


def test_p_exclusion_inside_a_chain_is_not_a_css_selector_override() -> None:
    runtime = _runtime()
    html = "<div><p>a</p><p>b</p><p>c</p></div>"
    assert vsp.looks_like_css_selector("a!1@text") is True
    assert vsp.evaluate_rule("p!1@text", html, runtime) == ["a", "c"]


# ------------------------------------------------------------------------------------------------- searchjs R / T


def test_t_lenient_header_option_text_never_raises() -> None:
    assert vsp._lenient_header_pairs("User-Agent: x\nAccept: y") == {"User-Agent": "x", "Accept": "y"}
    assert "User-Agent" in vsp._lenient_header_pairs("{'User-Agent': 'x', bad}")
    options = vsp.normalize_url_options({"headers": "{'A':'b'"})
    assert isinstance(options["headers"], dict)


def test_dedupe_fingerprint_separates_disabled_from_enabled_copies() -> None:
    enabled = {"bookSourceUrl": BASE, "searchUrl": "/s?q={{key}}"}
    disabled = dict(enabled, enabled=False)
    assert vsp.source_rule_fingerprint(enabled) != vsp.source_rule_fingerprint(disabled)
    assert vsp.source_rule_fingerprint(enabled) == vsp.source_rule_fingerprint(dict(enabled, enabled=True))


# ------------------------------------------------------------------------------- 1.1.2: D (JSON miss runs the JS)


def test_d_json_miss_followed_by_js_runs_the_js_only_on_1_1_2() -> None:
    runtime = _runtime()
    content = '{"a":1}'
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_rule('$.missing@js:"X"', content, runtime) == []
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_rule('$.missing@js:"X"', content, runtime) == ["X"]


def test_d_a_jsoup_miss_in_a_non_url_string_field_skips_the_js_but_a_list_does_not() -> None:
    """Legado getString -> null on a Jsoup miss (JS skipped); getStringList / isUrl getString -> "" (JS runs)."""
    runtime = _runtime()
    html = "<div><p>a</p></div>"
    rule = '.nothing@text@js:"X"'
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_rule(rule, html, runtime) == ["X"]
        with vsp._evaluating_non_url_string_field():
            assert vsp.evaluate_rule(rule, html, runtime) == []
        # a JSON miss still runs the JS in a non-URL string field (JsonPath answers "")
        with vsp._evaluating_non_url_string_field():
            assert vsp.evaluate_rule('$.missing@js:"X"', '{"a":1}', runtime) == ["X"]


@pytest.mark.parametrize(
    "segment, json_content, list_mode, expected",
    [
        ("$.a", False, False, True),  # JSON path: "" on a miss
        ("@json:$.a", False, False, True),
        ("@xpath://a", False, False, True),
        ("//a", False, False, True),
        ("@css:.a", True, False, False),  # explicit css = Jsoup even on JSON: getString answers null
        ("@css:.a", True, True, True),  # ... but the list evaluator answers []
        (".a@text", True, False, False),  # bare CSS on JSON in getString mode: the App stays conservative
        (".a@text", False, True, True),
        ("data.list", True, False, True),  # bare JSONPath shape: content dependent
        ("data.list", False, False, False),
        ("data.list", False, True, True),
        ("href", False, False, False),  # bare attribute rule = css
        ("@get:{k}", True, True, False),
        (r":(\d+)", True, True, False),  # regex / text / template segments never
        ("{{$.a}}", True, True, False),
        ("$.a##x##y", False, False, True),  # the `##` replace belongs to the base rule
    ],
)
def test_d_segment_classification_mirrors_the_app(segment: str, json_content: bool, list_mode: bool, expected: bool) -> None:
    assert vsp._app_segment_yields_empty_for_next_js(segment, json_content, list_mode) is expected


# -------------------------------------------------------------------------------- 1.1.2: A / E (TOC shortcuts)


def test_e_an_empty_chapter_url_rule_uses_the_toc_page_url_only_on_1_1_2(monkeypatch) -> None:
    src = _toc_src(chapterUrl="")
    pages = {"/toc": '<ul class="toc"><li><a href="/c/1">第1章</a></li><li><a href="/c/2">第2章</a></li></ul>'}
    with app_profiles.app_profile_scope("1.1.2"):
        site, result = _walk(monkeypatch, pages, src)
        assert result[0] is True, result
        assert result[2] == f"{BASE}/toc"
    with app_profiles.app_profile_scope("1.1.1"):
        site, result = _walk(monkeypatch, pages, src)
        assert result[2] != f"{BASE}/toc" or result[0] is False


# ----------------------------------------------------------------------------------- 1.1.2: U+200B in JS


def test_zero_width_characters_are_stripped_outside_strings_only() -> None:
    strip = vsp.strip_js_format_characters_outside_strings
    assert strip("a​.b('x​y')") == "a.b('x​y')"
    assert strip("var x = /a​b/;​ y") == "var x = /a​b/; y"
    assert strip("// c​\nz​") == "// c​\nz"
    assert strip("`t​${a​}`") == "`t​${a​}`"  # template text and expression are copied untouched
    assert strip("plain") == "plain"
    assert strip("﻿a") == "﻿a"  # BOM is whitespace to the lexer and stays


@needs_quickjs
def test_a_script_with_a_zero_width_space_runs_only_on_1_1_2() -> None:
    runtime = _runtime()
    script = "var r = 1​ + 2; r"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(script, "", runtime).strip() == "3"


# ----------------------------------------------------------------------------------------- 1.1.2: G2 HTTP bodies


def test_g2_search_404_error_pages_reach_the_rules_only_on_1_1_2() -> None:
    """An error page (`404 Not Found` title) is refused by the F3 rule on 1.1.1; 1.1.2 hands it to the search / explore rules."""
    body = "<html><title>404 Not Found</title><body>Not Found</body></html>"
    assert vsp.contains_http_error_page_signals(body)
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.app_http_failure(f"{BASE}/s", 404, body, "search", None) is True
        assert vsp.app_http_failure(f"{BASE}/e", 410, body, "explore", None) is True
        assert vsp.app_http_failure(f"{BASE}/t", 404, body, "toc", None) is True
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.app_http_failure(f"{BASE}/s", 404, body, "search", None) is False
        assert vsp.app_http_failure(f"{BASE}/e", 410, body, "explore", None) is False
        assert vsp.app_http_failure(f"{BASE}/t", 404, body, "toc", None) is True  # toc is not a search / explore request
        assert vsp.app_http_failure(f"{BASE}/s", 404, "", "search", None) is True  # an empty body stays an error


@pytest.mark.parametrize("status", [401, 403, 407, 408, 429, 500, 502, 503])
def test_g2_status_codes_that_stay_errors(status: int) -> None:
    pure = {"ruleToc": {"chapterList": '@js:"[]"', "chapterName": '@js:"a"', "chapterUrl": '@js:"b"'}}
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.app_http_failure(f"{BASE}/t", status, "<html>x</html>", "toc", pure) is True


@pytest.mark.parametrize(
    "marker",
    ["cf-chl-bypass", "cf_chl_opt", "cf-turnstile", "challenge-platform", "challenges.cloudflare.com", "Just a moment...",
     "Verifying you are human", "ddos-guard", "hcaptcha", "cf-browser-verification"],
)
def test_g2_anti_bot_pages_are_never_passed_to_the_rules(marker: str) -> None:
    """The R156 layer itself (the F3 rule has its own, older error-page signal list)."""
    page = f"<html>{marker}</html>"
    for pure in (False, True):
        assert vsp.app_http_error_passes_to_rules_legado_parity(404, page, "search", pure) is False
    assert vsp.app_http_error_passes_to_rules_legado_parity(404, "<html>plain 404 page</html>", "search", False) is True
    assert vsp.app_http_error_passes_to_rules_legado_parity(404, "<html>plain 404 page</html>", "toc", False) is False
    assert vsp.app_http_error_passes_to_rules_legado_parity(404, "", "search", False) is False
    # a 404 with an anti-bot page on a toc request stays a failure end to end
    pure_toc = {"ruleToc": {"chapterList": '@js:"[]"', "chapterName": '@js:"a"', "chapterUrl": '@js:"b"'}}
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.app_http_failure(f"{BASE}/t", 404, page, "toc", pure_toc) is True


def test_g2_pure_javascript_rules_accept_any_other_4xx_but_not_when_they_read_the_response() -> None:
    pure = {"ruleToc": {"chapterList": '@js:"[]"', "chapterName": '@js:"a"', "chapterUrl": "<js>'b'</js>"}}
    reading = {"ruleToc": {"chapterList": "@js:result", "chapterName": '@js:"a"', "chapterUrl": '@js:"b"'}}
    mixed = {"ruleToc": {"chapterList": '@js:"[]"', "chapterName": "a@text", "chapterUrl": '@js:"b"'}}
    assert vsp.pure_javascript_rules_for_operation(pure, "toc") is True
    assert vsp.pure_javascript_rules_for_operation(reading, "toc") is False
    assert vsp.pure_javascript_rules_for_operation(mixed, "toc") is False
    assert vsp.pure_javascript_rules_for_operation(None, "toc") is False
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.app_http_failure(f"{BASE}/t", 418, "<html>x</html>", "toc", pure) is False
        assert vsp.app_http_failure(f"{BASE}/t", 418, "<html>x</html>", "toc", reading) is True
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.app_http_failure(f"{BASE}/t", 418, "<html>x</html>", "toc", pure) is True


# -------------------------------------------------------------------------------------- 1.1.2: J (field order)


def _captured_field_order(profile: str) -> list[str]:
    seen: list[str] = []
    original = vsp.evaluate_rule

    def fake_evaluate(rule, item, runtime, *args, **kwargs):
        seen.append(rule)
        return ["v"]

    rule = {
        "name": "N", "author": "A", "kind": "K", "wordCount": "W", "lastChapter": "L", "intro": "I", "coverUrl": "C", "bookUrl": "B",
    }
    try:
        vsp.evaluate_rule = fake_evaluate  # type: ignore[assignment]
        with app_profiles.app_profile_scope(profile):
            vsp.capture_search_item_rule_variables(rule, "<div></div>", _runtime())
    finally:
        vsp.evaluate_rule = original  # type: ignore[assignment]
    return seen


def test_j_list_item_side_effect_fields_follow_legado_book_list_order_only_on_1_1_2() -> None:
    """BookList.kt:225-269: author, kind, wordCount, lastChapter, intro, coverUrl (name first and bookUrl last are evaluated by
    the caller)."""
    assert _captured_field_order("1.1.2") == ["A", "K", "W", "L", "I", "C"]
    assert _captured_field_order("1.1.1") == ["A", "C", "I", "K", "L", "W"]


# ------------------------------------------------------------------------------------ remaining runtime checks


def test_typed_data_url_options_survive_the_request_builder() -> None:
    src = {"bookSourceName": "d", "bookSourceUrl": BASE}
    url, method, _opts = vsp.request_search_or_explore(
        requests.Session(), src, _runtime(src), "kw", 1, "search", template='data:,{{key}},{"type":"json"}'
    )
    assert url == 'data:,kw,{"type":"json"}'
    assert method == "GET"


def test_r_empty_template_expression_is_an_empty_string_and_source_url_is_bound() -> None:
    runtime = _runtime()
    assert vsp._resolve_js_template_expressions('{{ "" }}ab', runtime) == "ab"
    assert vsp.resolve_template(r"{{bookSourceUrl.match(/\/\/(\w+)/)[1]}}-z", runtime, rule_template=True) == "example-z"


@needs_quickjs
def test_g_java_get_element_returns_a_list_only_on_1_1_2() -> None:
    runtime = _runtime()
    html = '<div class="a"><p>1</p></div><div class="a"><p>2</p></div>'
    script = 'String(Array.isArray(java.getElement(".a")))'
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js(script, html, runtime) == "false"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(script, html, runtime) == "true"


@needs_quickjs
def test_zero_width_space_in_code_fails_on_1_1_1_and_runs_on_1_1_2() -> None:
    runtime = _runtime()
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js("1​+2", "", runtime) == ""
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js("1​+2", "", runtime) == "3"


@needs_quickjs
def test_j_a_jsonp_wrapper_is_not_auto_objectified_but_whole_json_is() -> None:
    runtime = _runtime()
    assert vsp.evaluate_js("typeof result", 'cb({"a":1})', runtime) == "string"
    assert vsp.evaluate_js("typeof result", '{"a":1}', runtime) == "object"


def test_a_txt_book_url_shortcut_is_skipped_when_a_chapter_list_rule_exists(monkeypatch) -> None:
    """App profile 1.1.2: the `.txt` single-chapter shortcut applies only without a chapterList rule (Legado has none)."""
    src = _toc_src()
    pages = {"/book.txt": _toc_page(3)}
    site = Site(pages)
    monkeypatch.setattr(vsp, "fetch_text", site)
    with app_profiles.app_profile_scope("1.1.2"):
        result = vsp.resolve_toc_source(requests.Session(), src, vsp.make_runtime(src), f"{BASE}/book.txt", f"{BASE}/book.txt")
    assert result[0] is True
    assert site.requested("/book.txt") >= 1
    assert result[2] == f"{BASE}/c/1"  # the rule ran on the page instead of collapsing to one synthetic chapter


def test_explicit_css_rule_accepts_the_camel_case_last_rule_keywords() -> None:
    """sudugu: `@css:div.con@textNodes` returned nothing because the keyword was compared lower-cased only."""
    runtime = _runtime()
    html = '<div class="con" data-k="v">\n&emsp;&emsp;民国<br />\n<br />\n&emsp;&emsp;木森<br />\n</div>'
    assert vsp.evaluate_rule("@css:div.con@textNodes", html, runtime) == ["民国\n木森"]
    assert vsp.evaluate_rule("@css:div.con@data-k", html, runtime) == ["v"]


def test_page_segment_in_a_post_body_skips_xml_markup() -> None:
    """App applyLegadoPageSegments(requiresComma:): `</x,y>`, `<?xml a,b?>` and `<root a="x,y">` are XML, not page segments."""
    seg = vsp.apply_legado_page_segments
    assert seg("</x,y>", 2, True) == "</x,y>"
    assert seg("<?xml a,b?>", 2, True) == "<?xml a,b?>"
    assert seg('<root a="x,y">', 2, True) == '<root a="x,y">'
    assert seg("<!--a,b-->", 2, True) == "<!--a,b-->"
    assert seg("<search>kw</search>", 2, True) == "<search>kw</search>"  # no comma
    assert seg("&<,page=2>", 2, True) == "&page=2"
    assert seg("<x,y>", 2, False) == "y"  # the URL itself keeps the permissive rule


def test_pure_javascript_search_rules_read_the_init_key_and_the_old_alias() -> None:
    reading = {"ruleSearch": {"init": "@js:result", "bookList": '@js:"[]"', "name": '@js:"a"', "bookUrl": '@js:"b"'}}
    alias = {"ruleSearch": {"initJs": "@js:result", "bookList": '@js:"[]"', "name": '@js:"a"', "bookUrl": '@js:"b"'}}
    pure = {"ruleSearch": {"init": '@js:"x"', "bookList": '@js:"[]"', "name": '@js:"a"', "bookUrl": '@js:"b"'}}
    assert vsp.pure_javascript_rules_for_operation(reading, "search") is False
    assert vsp.pure_javascript_rules_for_operation(alias, "search") is False
    assert vsp.pure_javascript_rules_for_operation(pure, "search") is True


def test_js_lib_loses_format_characters_only_on_1_1_2() -> None:
    src = {"bookSourceName": "lib", "bookSourceUrl": BASE, "jsLib": "function f()​{ return 1; }"}
    runtime = vsp.make_runtime(src)
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp._js_lib_for_engine(runtime) == src["jsLib"]
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp._js_lib_for_engine(runtime) == "function f(){ return 1; }"


def test_toc_window_stops_after_the_first_page_with_any_chapter_and_drops_synthetic_volumes(monkeypatch) -> None:
    """App firstPageOnly stops after the first page that has any chapter (volume rows count); BookSourceValidator then drops volumes
    whose URL is the synthetic title + index. A page of volumes only therefore fails the TOC instead of walking on."""
    rule = {"chapterList": ".toc li", "chapterName": "a@text", "chapterUrl": "", "isVolume": "@js:'true'", "nextTocUrl": "a.n@href"}
    src = _toc_src(**rule)
    pages = {
        "/toc": '<ul class="toc"><li><a>卷一</a></li></ul><a class="n" href="/toc2">下一页</a>',
        "/toc2": _toc_page(3),
    }
    with app_profiles.app_profile_scope("1.1.1"):
        site, windowed = _walk(monkeypatch, pages, src, first_page_only=True)
        assert windowed[0] is False
        assert site.requested("/toc2") == 0


def test_all_chapters_guess_waits_for_pagination_candidates(monkeypatch) -> None:
    """App order: range options, then (>= 15 items) the pagination list, and only without any candidate the all-chapters link."""
    html = (
        '<a href="/all.html">全部章节</a><div class="pages"><a href="/toc_2.html">2</a></div>' + _toc_page(16)
    )
    src = _toc_src()
    site, result = _walk(monkeypatch, {"/toc": html, "/toc_2.html": _toc_page(4, start=17), "/all.html": _toc_page(3, "z")}, src)
    assert result[0] is True
    assert site.requested("/all.html") == 0
    assert site.requested("/toc_2.html") == 1
    # fewer than 15 items: the pagination list is not consulted, the all-chapters link is
    short = '<a href="/all.html">全部章节</a><div class="pages"><a href="/toc_2.html">2</a></div>' + _toc_page(3)
    site, result = _walk(monkeypatch, {"/toc": short, "/toc_2.html": _toc_page(4, start=17), "/all.html": _toc_page(5, "z")}, src)
    assert result[1] == f"{BASE}/all.html" and result[6] == 5
