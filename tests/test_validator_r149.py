"""R149: validator-stricter-than-App misjudgements found by the online re-validation (offline; patched fetches only).

One group of tests per root cause (B1-B10 of the R148 diagnosis). Every assertion states the App semantics it follows,
with the Swift location. B9 does not exist in the diagnosis; diagnosis item #22 (a rule starting with `.` is not
JSONPath in the App) is an App-side difference and deliberately left as is (see the last test).
"""
from __future__ import annotations

from typing import Any

import pytest
import requests
from bs4 import BeautifulSoup

from validator import validate_source_packages as vsp

BASE = "https://example.test"


class Fetch:
    """Replacement for ``vsp.fetch_text``: ``responses`` maps a URL (or suffix) to a body or ``(body, url, status)``."""

    def __init__(self, responses: dict[str, Any] | None = None, default: str | None = None) -> None:
        self.responses = responses or {}
        self.default = default
        self.urls: list[str] = []

    def __call__(self, session: requests.Session, url: str, **kwargs: Any) -> tuple[str, str, int]:
        self.urls.append(url)
        for key, value in self.responses.items():
            if url == key or url.endswith(key):
                if isinstance(value, tuple):
                    return value[0], value[1] if len(value) > 1 else url, value[2] if len(value) > 2 else 200
                return value, url, 200
        if self.default is not None:
            return self.default, url, 200
        raise AssertionError(f"unexpected URL: {url}")


def install(monkeypatch, responses: dict[str, Any] | None = None, default: str | None = None) -> Fetch:
    fetch = Fetch(responses, default)
    monkeypatch.setattr(vsp, "fetch_text", fetch)
    return fetch


def _runtime() -> vsp.RuleRuntime:
    return vsp.RuleRuntime(source_url=f"{BASE}/", base_url=f"{BASE}/", book_source_url=BASE, headers={})


LIST = "<html><body><div class='b'><a href='/book/{n}'>书{n}</a></div></body></html>"
EMPTY = "<html><body><p>nothing here</p></body></html>"
RULES = {"bookList": ".b", "name": "a@text", "bookUrl": "a@href"}


def _src(**overrides: Any) -> dict[str, Any]:
    src: dict[str, Any] = {"bookSourceName": "R149 Source", "bookSourceUrl": BASE}
    src.update(overrides)
    return src


def _candidates(src: dict[str, Any]) -> tuple[list[str], Any]:
    runtime = vsp.make_runtime(src)
    urls, _base = vsp.fetch_book_candidates(requests.Session(), src, runtime)
    return urls, runtime


# ------------------------------------------------------------------------------------------ B1  multi-token class


MULTI = (
    '<ul><li class="a b c"><a href="/whole">whole</a></li><li class="a b"><a href="/two">two</a></li>'
    '<li class="x a b c y"><a href="/inside">inside</a></li><li class="A B C"><a href="/upper">upper</a></li></ul>'
)


def test_b1_class_with_spaces_matches_the_whole_class_attribute_only() -> None:
    """RuleEvaluator.swift:1005-1010 getElementsByClass -> Jsoup/SwiftSoup hasClass: `class.a b c` is compared with the
    whole `class` attribute (case-insensitive equality), it is not split into tokens and not found inside a longer list."""
    hrefs = vsp.evaluate_css_chain_rule("class.a b c@a@href", MULTI, _runtime())
    assert hrefs == ["/whole", "/upper"]


def test_b1_single_token_class_still_matches_any_token_and_never_a_partial_name() -> None:
    hrefs = vsp.evaluate_css_chain_rule("class.b@a@href", MULTI, _runtime())
    assert hrefs == ["/whole", "/two", "/inside", "/upper"]
    assert vsp.evaluate_css_chain_rule("class.a b@a@href", MULTI, _runtime()) == ["/two"]
    assert vsp.evaluate_css_chain_rule("class.a c@a@href", MULTI, _runtime()) == []


def test_b1_class_chain_in_a_book_list_rule() -> None:
    items = vsp.evaluate_book_list("class.a b c", MULTI, _runtime())
    assert len(items) == 2 and all("<li" in item for item in items)


# ------------------------------------------------------------------------------------- B2  bare JSONPath vs selector


XBK = "<html><body><ul class='xbk'><li><a href='/b/1'>一</a></li><li><a href='/b/2'>二</a></li></ul></body></html>"


def test_b2_html_content_does_not_treat_ul_dot_class_as_jsonpath_in_a_list_rule() -> None:
    """RuleType.swift:911-925 `.contentDependent`: on HTML `ul.xbk` is a JSoup selector (RuleEvaluator.swift:1534-1539)."""
    assert vsp.looks_like_bare_jsonpath("ul.xbk")
    items = vsp.evaluate_book_list("ul.xbk", XBK, _runtime())
    assert len(items) == 1 and items[0].startswith("<ul class=\"xbk\">")
    items = vsp.evaluate_book_list("ul.xbk li", XBK, _runtime())
    assert len(items) == 2


def test_b2_html_content_runs_the_selector_for_field_rules_too() -> None:
    assert vsp.evaluate_rule("a.title", "<a class='title' href='/x'>T</a>", _runtime(), "html") == ["<a class=\"title\" href=\"/x\">T</a>"]


def test_b2_json_content_keeps_the_jsonpath_reading() -> None:
    body = '{"data": {"list": [{"n": 1}, {"n": 2}]}}'
    assert len(vsp.evaluate_book_list("data.list", body, _runtime())) == 2
    assert vsp.evaluate_rule("data.list[0].n", body, _runtime(), "json") == ["1"]


def test_b2_selector_that_finds_nothing_falls_back_to_jsonpath_on_markup_with_embedded_json() -> None:
    """The App keeps the old embedded-JSON fallback when the CSS reading of a bare path is empty (RuleEvaluator.swift:1537)."""
    page = '<html><body><script>var x = 1;</script><pre>{"data": {"list": [{"n": 1}]}}</pre></body></html>'
    assert not vsp.is_json_content(page)
    assert vsp.evaluate_book_list("data.list", page, _runtime()) == ['{"n": 1}']


# ---------------------------------------------------------------------------------------------- B3  select incl. self


def test_b3_select_includes_the_context_element_itself() -> None:
    """RuleEvaluator.swift:1034-1041: `Element.select(css)` collects from the element itself (66小说 `.item-link@a@href`
    where the list item IS the `<a class=item-link>`)."""
    item = '<a class="item-link" href="/chapter/1">第一章</a>'
    assert vsp.evaluate_css_chain_rule(".item-link@a@href", item, _runtime()) == ["/chapter/1"]
    assert vsp.evaluate_css_chain_rule(".item-link@a@text", item, _runtime()) == ["第一章"]


def test_b3_descendants_are_still_selected_and_not_duplicated() -> None:
    page = '<div class="x"><a class="x" href="/in">in</a></div>'
    assert vsp.evaluate_css_chain_rule("div@.x@href", page, _runtime()) == ["/in"]
    # first step runs on the document (not an element): the html wrapper is never "self"
    assert vsp.evaluate_css_chain_rule("a@href", "<a href='/only'>x</a>", _runtime()) == ["/only"]


def test_b3_self_match_respects_the_selector() -> None:
    item = '<a class="other" href="/nope">x</a>'
    assert vsp.evaluate_css_chain_rule("class.item-link@a@href", item, _runtime()) == []
    assert vsp.evaluate_css_chain_rule(".other@.item-link@href", item, _runtime()) == []


# ----------------------------------------------------------------------------------------------- B4  implicit tbody


TABLE = (
    "<table class='list'><thead><tr><th>h</th></tr></thead>"
    "<tr><td><a href='/1'>one</a></td></tr>\n<tr><td><a href='/2'>two</a></td></tr></table>"
)


def test_b4_tbody_step_works_on_a_table_without_a_tbody_tag() -> None:
    """Jsoup/SwiftSoup's HTML5 tree builder wraps the `<tr>` children of a `<table>` in `<tbody>` (八块小说网
    `class.list@tag.tbody@tr`); html.parser does not. The thead row is not part of the implicit tbody."""
    rows = vsp.evaluate_css_chain_rule("class.list@tag.tbody@tr@td@text", TABLE, _runtime())
    assert rows == ["one", "two"]
    assert vsp.evaluate_css_chain_rule("class.list@tag.tbody@tr", TABLE, _runtime()) == [
        '<tr><td><a href="/1">one</a></td></tr>', '<tr><td><a href="/2">two</a></td></tr>',
    ]


def test_b4_tbody_in_a_css_selector_and_a_book_list() -> None:
    assert [h for h in vsp.css_extract(TABLE, "table.list > tbody > tr a", "href", None, BASE)] == ["/1", "/2"]
    items = vsp.evaluate_book_list("class.list@tag.tbody@tr", TABLE, _runtime())
    assert len(items) == 2


def test_b4_existing_tbody_is_left_alone_and_rules_without_tbody_do_not_pay() -> None:
    page = "<table><tbody><tr><td>a</td></tr></tbody></table>"
    soup = BeautifulSoup(page, "html.parser")
    vsp._insert_implicit_tbody(soup)
    assert len(soup.find_all("tbody")) == 1
    assert vsp.evaluate_css_chain_rule("tag.tbody@tr", page, _runtime()) == ["<tr><td>a</td></tr>"]
    # a rule that never mentions tbody leaves the table structure as parsed
    untouched = vsp.evaluate_css_chain_rule("class.list@outerHtml", TABLE, _runtime())
    assert len(untouched) == 1 and "<tbody" not in untouched[0]
    wrapped = vsp.evaluate_css_chain_rule("class.list@tag.tbody@outerHtml", TABLE, _runtime())
    assert len(wrapped) == 1 and wrapped[0].startswith("<tbody><tr><td>")


# ---------------------------------------------------------------------------------------------- B5  detail stage


def _toc_page() -> str:
    return "<html><body><ul class='toc'><li><a href='/c/1'>第一章 开端</a></li></ul></body></html>"


def _chapter_page() -> str:
    return "<html><body><div id='c'>" + "这是正文内容，用来证明目录和正文都能取到。" * 8 + "</div></body></html>"


def _detail_source(**info: Any) -> dict[str, Any]:
    return _src(
        searchUrl=f"{BASE}/search?q={{{{key}}}}", ruleSearch=dict(RULES),
        ruleBookInfo=dict(info),
        ruleToc={"chapterList": ".toc li", "chapterName": "a@text", "chapterUrl": "a@href"},
        ruleContent={"content": "#c@html"},
    )


def test_b5_detail_with_no_name_and_no_toc_url_is_not_a_detail_failure(monkeypatch) -> None:
    """BookSourceEngine.swift:9123-9150 (tocUrl falls back to bookUrl) + BookSourceValidator.swift:2455-2480: an unresolved
    name is only the info detail `（书名未解析）`; the detail stage fails on a request / script error."""
    install(monkeypatch, {"/search?q=%E6%88%91%E7%9A%84": LIST.format(n=1), "/book/1": _toc_page()})
    src = _detail_source(name=".no-such-node@text")
    ok, name, toc_url, error = vsp.fetch_book_info(requests.Session(), src, _runtime(), f"{BASE}/book/1")
    assert ok and name == "" and toc_url == "" and error == ""
    # a rule-less source and a book page the name heuristics cannot read behave the same
    ok, name, toc_url, error = vsp.fetch_book_info(
        requests.Session(), _src(ruleBookInfo={"name": ".gone@text", "tocUrl": ".gone@href"}), _runtime(), f"{BASE}/book/1")
    assert ok and name == "" and toc_url == ""


def test_b5_full_chain_continues_with_the_book_url_as_toc_url(monkeypatch) -> None:
    fetch = install(monkeypatch, {
        "/search?q=%E6%88%91%E7%9A%84": LIST.format(n=1),
        "/book/1": _toc_page(),
        "/c/1": _chapter_page(),
    })
    outcome, record, detail = vsp.validate_single_source(_detail_source(name=".no-such-node@text"))
    assert outcome.passed and record is not None, detail.get("failureReason")
    assert detail["stages"] == {"search": True, "detail": True, "toc": True, "content": True}
    assert f"{BASE}/book/1" in fetch.urls and f"{BASE}/c/1" in fetch.urls


def test_b5_a_real_detail_failure_still_fails(monkeypatch) -> None:
    install(monkeypatch, {"/search?q=%E6%88%91%E7%9A%84": LIST.format(n=1), "/book/1": ("", f"{BASE}/book/1", 200)})
    outcome, record, detail = vsp.validate_single_source(_detail_source(name=".no-such-node@text"))
    assert not outcome.passed and detail["failureReason"] == "detail failed"
    assert detail["error"] == "detail empty response"
    # and a broken toc after the unresolved name is judged by the toc stage
    install(monkeypatch, {"/search?q=%E6%88%91%E7%9A%84": LIST.format(n=1), "/book/1": EMPTY})
    outcome, record, detail = vsp.validate_single_source(_detail_source(name=".no-such-node@text"))
    assert not outcome.passed and detail["failureReason"] == "toc failed"


# ---------------------------------------------------------------------------------------- B6  discovery categories


def test_b6_explore_url_is_split_on_double_ampersand_and_newlines() -> None:
    """BookSource.swift:1190-1196 splitExploreUrlEntries: newline first, then `&&`."""
    entries = vsp.parse_explore_urls("玄幻::/xuanhuan/{{page}}.html&&都市::/dushi/{{page}}.html\n历史::/lishi", "S")
    assert entries == [("玄幻", "/xuanhuan/{{page}}.html"), ("都市", "/dushi/{{page}}.html"), ("历史", "/lishi")] or [
        url for _title, url in entries] == ["/xuanhuan/{{page}}.html", "/dushi/{{page}}.html", "/lishi"]
    assert vsp.split_explore_url_entries(" a && b \n\n c&&&&d ") == ["a", "b", "c", "d"]
    runtime = _runtime()
    assert vsp.build_first_explore_url("玄幻::/xuanhuan/{{page}}.html&&都市::/dushi/1.html", runtime) == f"{BASE}/xuanhuan/1.html"


def test_b6_first_category_skips_action_buttons_and_title_only_headers_for_scripts() -> None:
    """BookSourceValidator.swift:2357-2360: the first category whose url is non-empty and not an action."""
    runtime = _runtime()
    script = "@js:JSON.stringify([{title:'设置',url:'',action:'openSetting()'},{title:'分组'},{title:'热门',url:'/hot/{{page}}.html'}])"
    assert vsp.build_first_explore_url(script, runtime, resolve_categories=True) == f"{BASE}/hot/1.html"
    assert vsp.build_first_explore_url("@js:'分组\\n男频::/m/1&&女频::/f/1'", runtime, resolve_categories=True) == f"{BASE}/m/1"
    # `{{js}}` actions are never categories, even in a static field
    assert vsp.build_first_explore_url("设置::{{open()}}&&榜单::/rank", runtime) == f"{BASE}/rank"


def test_b6_title_only_first_line_is_a_header_not_a_url_when_validating() -> None:
    """BookSource.parseExploreUrlField (BookSource.swift:1140-1175): a segment without `::` is a title-only header
    (潇湘书院 starts with the line `排行`; the old reading requested `GET /排行`, a 404)."""
    runtime = _runtime()
    text = "                       排行      \n潇湘票榜::/rank/xxyuepiao\n新书榜::/rank/new"
    assert vsp.build_first_explore_url(text, runtime, resolve_categories=True) == f"{BASE}/rank/xxyuepiao"
    assert vsp.build_first_explore_url("排行", runtime, resolve_categories=True) is None
    # static checks keep the lenient reading (source_has_absolute_entry_point / fallback_base_url_from_entry_point)
    assert vsp.build_first_explore_url("https://x.test/list", runtime) == "https://x.test/list"


def test_b6_script_exploreurl_runs_the_script_and_uses_its_first_category() -> None:
    """BookSourceEngine+Flagship.swift:32-94 resolveExploreCategories: the script OUTPUT is the category list."""
    runtime = _runtime()
    array = "<js>var r=[{title:'A',url:'/a/1.html'},{title:'B',url:'/b/1.html'}]; JSON.stringify(r)</js>"
    assert vsp.build_first_explore_url(array, runtime, resolve_categories=True) == f"{BASE}/a/1.html"
    # without the opt-in (static checks such as source_has_absolute_entry_point) the script is not executed
    assert vsp.build_first_explore_url(array, runtime) == array


def test_b6_script_error_is_an_error_header_not_a_failure_and_empty_output_falls_back_to_the_script() -> None:
    """Flagship.swift:67-76: a throwing category script becomes an unclickable `ERROR:` header (-> `发现规则为空`, info)."""
    runtime = _runtime()
    sink = vsp.begin_script_error_stage(runtime)
    assert vsp.build_first_explore_url("@js:throw new Error('boom')", runtime, resolve_categories=True) is None
    assert not [entry for entry in sink if entry.get("kind") == "error"] and runtime.script_error == ""
    # a script returning only plain lines yields headers (no `::`) -> no usable category
    assert vsp.build_first_explore_url("@js:'https://x.test/a'", runtime, resolve_categories=True) is None
    # an empty output keeps the script itself as the category (App: generatedCategories.isEmpty -> staticCategories)
    assert vsp.build_first_explore_url("@js:''", runtime, resolve_categories=True) == "@js:''"


def test_b6_discovery_runs_the_scripts_first_category_end_to_end(monkeypatch) -> None:
    fetch = install(monkeypatch, {"/hot/1.html": LIST.format(n=7)}, default=LIST.format(n=1))
    script = "@js:JSON.stringify([{title:'热门',url:'/hot/{{page}}.html'}])"
    src = _src(exploreUrl=script, ruleExplore=dict(RULES))
    urls, runtime = _candidates(src)
    assert urls == [f"{BASE}/book/7"]
    assert f"{BASE}/hot/1.html" in fetch.urls
    # `A&&B` static field: the first segment is requested
    fetch = install(monkeypatch, {"/a/1": LIST.format(n=3)})
    urls, _runtime_ = _candidates(_src(exploreUrl="甲::/a/1&&乙::/b/1", ruleExplore=dict(RULES)))
    assert urls == [f"{BASE}/book/3"] and fetch.urls == [f"{BASE}/a/1"]


# ------------------------------------------------------------------------------------------ B7  explore landing guard


def test_b7_explore_category_that_is_the_home_page_is_judged_by_the_rule(monkeypatch) -> None:
    """BookSourceEngine+Flagship.swift:100-263 explore() has no landing-page guard (it only exists for search,
    BookSourceEngine.swift:250-268): category `首页::/` returns the books its rule extracts."""
    install(monkeypatch, {f"{BASE}/": LIST.format(n=5), f"{BASE}": LIST.format(n=5)})
    urls, runtime = _candidates(_src(exploreUrl="首页::/", ruleExplore=dict(RULES)))
    assert urls == [f"{BASE}/book/5"]
    assert (runtime.variables or {}).get("__exploreFailed") is None


def test_b7_search_keeps_its_landing_page_guard(monkeypatch) -> None:
    install(monkeypatch, {"/search?q=%E6%88%91%E7%9A%84": (LIST.format(n=5), f"{BASE}/", 200)})
    urls, _runtime_ = _candidates(_src(searchUrl=f"{BASE}/search?q={{{{key}}}}", ruleSearch=dict(RULES)))
    assert urls == []


def test_b7_explore_category_without_books_still_fails_the_source(monkeypatch) -> None:
    install(monkeypatch, {"/search?q=%E6%88%91%E7%9A%84": LIST.format(n=1), f"{BASE}/": EMPTY}, default=EMPTY)
    src = _src(searchUrl=f"{BASE}/search?q={{{{key}}}}", exploreUrl="首页::/", ruleSearch=dict(RULES), ruleExplore=dict(RULES))
    urls, runtime = _candidates(src)
    assert urls and (runtime.variables or {}).get("__exploreFailed") == "1"


# ------------------------------------------------------------------------------------------- B8  home page probe


def test_b8_quick_scan_and_full_validation_do_not_probe_the_home_page(monkeypatch) -> None:
    """The App's batch check (BookSourceValidator.doCheckSource) never requests the home page. The old probe could eat the
    8s quick-scan quota and failed a source whose search works (`connectivity failed`)."""
    probes: list[str] = []

    def probe(self, url, **kwargs):  # noqa: ANN001
        probes.append(url)
        raise requests.ConnectionError("home page unreachable")

    monkeypatch.setattr(requests.Session, "get", probe)
    install(monkeypatch, {"/search?q=%E6%88%91%E7%9A%84": LIST.format(n=1), "/book/1": _toc_page(), "/c/1": _chapter_page()})
    src = _detail_source(name=".no-such-node@text")
    passed, seed, detail = vsp.quick_scan_single_source(src)
    assert passed and seed is not None and detail["quickScan"]["connectivity"] is True
    outcome, record, detail = vsp.validate_single_source(src)
    assert outcome.passed and record is not None, detail.get("failureReason")
    assert probes == []


def test_b8_unreachable_site_is_reported_by_the_search_stage(monkeypatch) -> None:
    def fail(session, url, **kwargs):  # noqa: ANN001
        return "", url, 0

    monkeypatch.setattr(vsp, "fetch_text", fail)
    passed, seed, detail = vsp.quick_scan_single_source(_detail_source(name=".x@text"))
    assert not passed and seed is None
    assert detail["failureReason"] == "no search/explore result"


# ------------------------------------------------------------------------------------ B10  attribute entity decoding


def test_b10_semicolonless_named_entity_in_an_attribute_is_not_decoded() -> None:
    """Jsoup/SwiftSoup Tokeniser.consumeCharacterReference(inAttribute: true): `&sect` followed by a letter, digit, `=`,
    `-` or `_` stays literal (包子漫画 `...&section_slot=0` became `§ion_slot=0` and 404)."""
    soup = BeautifulSoup('<a href="/c?id=1&section_slot=0&copy2=1&amp=2&lt-x&para_x">t</a>', "html.parser")
    assert soup.a["href"] == "/c?id=1&section_slot=0&copy2=1&amp=2&lt-x&para_x"
    chapter = '<a class="ch" href="/comic/chapter/1?chapter_slot=1&section_slot=0">第1话</a>'
    assert vsp.evaluate_css_chain_rule(".ch@href", chapter, _runtime()) == ["/comic/chapter/1?chapter_slot=1&section_slot=0"]
    assert vsp.evaluate_css_chain_rule("a@href", chapter, _runtime()) == ["/comic/chapter/1?chapter_slot=1&section_slot=0"]


def test_b10_terminated_numeric_and_standalone_references_are_still_decoded() -> None:
    soup = BeautifulSoup('<a href="/x?a=1&amp;b=2&sect;&#167;&#xA7;&copy 1" title="&lt;&gt;&quot;">t&sect;&amp;</a>', "html.parser")
    assert soup.a["href"] == "/x?a=1&b=2§§§© 1"
    assert soup.a["title"] == '<>"'
    # text nodes are not touched by the attribute rule
    assert soup.a.get_text() == "t§&"
    assert BeautifulSoup("<p>&sect and &copy</p>", "html.parser").p.get_text() == "§ and ©"
    assert vsp.jsoup_unescape_attribute("plain") == "plain"
    assert vsp.jsoup_unescape_attribute("a&b") == "a&b"


# ------------------------------------------------------------------------------------------------- not changed


def test_diagnosis_22_dot_prefixed_rule_is_not_jsonpath_like_the_app() -> None:
    """RuleType.swift:913: a rule starting with `.` is never a bare JSONPath (Legado would prepend `$.`); the validator
    stays equal to the App (the classification is unchanged by R149; reported as an App-side gap)."""
    assert not vsp.looks_like_bare_jsonpath(".bookList[*]")
    assert not vsp.looks_like_bare_jsonpath("#list.items")
    assert vsp.looks_like_bare_jsonpath("data.list")
