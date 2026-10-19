"""R155: closing the rule-class parity gaps found by the Legado live reference (offline; patched fetches, synthetic pages).

I   search landing guard only after a real redirect (BookSourceEngine.swift:246-262; Legado has no such guard).
A   nextTocUrl batch semantics (Legado BookChapterList.kt:77-104 / :160-175; BookSourceEngine.swift:3326-3350).
B   `@get:{key}` expanded in JS rule segments and JS-produced URLs (AnalyzeRule.SourceRule.makeUpRule :659-705).
D   bare Jsoup selector + `!N[:M]` exclusion (AnalyzeByJSoup.ElementsSingle :303-322).
L   java.getElements / java.getElement take Legado rules (AnalyzeRule.getElements); M  `:lt/:gt/:eq` pseudo selectors.
H   a tocUrl RULE result is not run through the unusable / single-chapter heuristics (BookSourceEngine.swift:9120-9150).
G   `globalThis.headers/variables`: a source's own `let headers` is legal.
E   Java/ICU `.` does not match `\\r \\u0085 \\u2028 \\u2029`.
F   Jsoup-style attribute serialisation (always double quotes, `&quot;`, `&amp;`).
C/N App profile 1.1.2 only: list rule with trailing `@put:{}`, leading-dot JSON path = `$..` deep scan.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from validator import validate_source_packages as vsp

BASE = "https://example.test"
needs_quickjs = pytest.mark.skipif(vsp.quickjs is None, reason="QuickJS bridge only")
LONG_TEXT = "这是一段足够长的正文内容用于验证。这是一段足够长的正文内容用于验证。"


class Site:
    """Patched ``fetch_text``: answers by exact path (+query), optional redirects, records every request."""

    def __init__(self, pages: dict[str, Any], redirects: dict[str, str] | None = None) -> None:
        self.pages = dict(pages)
        self.redirects = dict(redirects or {})
        self.log: list[str] = []

    def __call__(self, session: requests.Session, url: str, **kwargs: Any) -> tuple[str, str, int]:
        path = url.replace(BASE, "")
        self.log.append(path)
        final_url = url
        if path in self.redirects:
            final_url = BASE + self.redirects[path]
            path = self.redirects[path]
        page = self.pages.get(path)
        if page is None:
            return "", final_url, 404
        body = page(url) if callable(page) else page
        return body, final_url, 200

    def requested(self, path: str) -> int:
        return self.log.count(path)


def _runtime(variables: dict[str, str] | None = None, base: str = f"{BASE}/") -> vsp.RuleRuntime:
    runtime = vsp.RuleRuntime(source_url=base, base_url=base, book_source_url=BASE, headers={})
    runtime.variables = dict(variables or {})
    return runtime


# ------------------------------------------------------------------------------------------------------------- I landing


def _landing_source() -> dict[str, Any]:
    return {
        "bookSourceName": "R155 I",
        "bookSourceUrl": BASE,
        "searchUrl": "/wap/index.php?keyword={{key}}",
        "ruleSearch": {"bookList": ".b", "name": "a@text", "bookUrl": "a@href"},
        "ruleBookInfo": {"name": "h1@text"},
        "ruleToc": {"chapterList": ".toc li", "chapterName": "a@text", "chapterUrl": "a@href"},
        "ruleContent": {"content": "#c@text"},
    }


def _landing_pages() -> dict[str, Any]:
    results = '<div class="b"><a href="/book/1">我的书</a></div>'
    return {
        "/wap/index.php?keyword=%E6%88%91%E7%9A%84": results,
        "/wap/index.php?keyword=我的": results,
        "/book/1": '<h1>我的书</h1><ul class="toc"><li><a href="/c/1">第一章</a></li></ul>',
        "/c/1": f'<div id="c">{LONG_TEXT}</div>',
        "/": "<p>home</p>",
    }


def test_i_search_url_that_only_looks_like_a_landing_page_is_not_dropped_without_a_redirect(monkeypatch) -> None:
    """`wap/index.php?keyword=..` (a root-index shaped path) answers with results directly: no redirect -> no guard."""
    site = Site(_landing_pages())
    monkeypatch.setattr(vsp, "fetch_text", site)
    src = _landing_source()
    urls, _base = vsp.fetch_book_candidates(requests.Session(), src, vsp.make_runtime(src))
    assert urls == [f"{BASE}/book/1"]
    outcome, record, detail = vsp.validate_single_source(src)
    assert outcome.passed and record is not None, detail


def test_i_a_real_redirect_to_the_source_home_page_is_still_a_landing(monkeypatch) -> None:
    pages = _landing_pages()
    pages["/"] = '<div class="b"><a href="/book/1">推荐</a></div>'
    site = Site(pages, redirects={"/wap/index.php?keyword=%E6%88%91%E7%9A%84": "/", "/wap/index.php?keyword=我的": "/"})
    monkeypatch.setattr(vsp, "fetch_text", site)
    src = _landing_source()
    urls, _base = vsp.fetch_book_candidates(requests.Session(), src, vsp.make_runtime(src))
    assert urls == []


def test_i_response_url_differs_from_request_ignores_requests_re_quoting() -> None:
    differs = vsp.response_url_differs_from_request
    assert differs("https://a.test/x?q=1", "https://a.test/x?q=1") is False
    assert differs("https://a.test/s?kw=我的", "https://a.test/s?kw=%E6%88%91%E7%9A%84") is False
    assert differs('https://a.test/s?kw=1,{"method":"GET"}', "https://a.test/s?kw=1") is False
    assert differs("https://a.test/s?kw=1", "https://a.test/") is True


# -------------------------------------------------------------------------------------------------------- A nextTocUrl


def _toc_source(next_rule: str = "a.n@href") -> dict[str, Any]:
    return {
        "bookSourceName": "R155 A",
        "bookSourceUrl": BASE,
        "ruleToc": {
            "chapterList": ".toc li",
            "chapterName": "a@text",
            "chapterUrl": "a@href",
            "nextTocUrl": next_rule,
        },
    }


def _toc_page(chapters: list[str], nexts: list[str]) -> str:
    items = "".join(f'<li><a href="/c/{name}">章{name}</a></li>' for name in chapters)
    links = "".join(f'<a class="n" href="{href}">下一页</a>' for href in nexts)
    return f'<ul class="toc">{items}</ul>{links}'


def _walk_toc(monkeypatch, pages: dict[str, Any], src: dict[str, Any] | None = None) -> tuple[Site, tuple, vsp.RuleRuntime]:
    site = Site(pages)
    monkeypatch.setattr(vsp, "fetch_text", site)
    source = src or _toc_source()
    runtime = vsp.make_runtime(source)
    result = vsp.resolve_toc_source(requests.Session(), source, runtime, f"{BASE}/book", f"{BASE}/toc1")
    return site, result, runtime


def test_a_several_next_urls_on_the_first_page_are_a_batch_and_the_batch_pages_never_chain(monkeypatch) -> None:
    pages = {
        "/toc1": _toc_page(["1"], ["/toc2", "/toc3"]),
        "/toc2": _toc_page(["2"], ["/toc4"]),  # a batch page's own nextTocUrl is NOT evaluated
        "/toc3": _toc_page(["3"], ["/toc5"]),
        "/toc4": _toc_page(["4"], []),
        "/toc5": _toc_page(["5"], []),
    }
    site, result, _runtime_after = _walk_toc(monkeypatch, pages)
    assert result[0] is True and result[4] == 3  # pages walked
    assert site.requested("/toc2") == 1 and site.requested("/toc3") == 1
    assert site.requested("/toc4") == 0 and site.requested("/toc5") == 0


def test_a_batch_pages_do_not_even_run_the_next_toc_rule(monkeypatch) -> None:
    """The 英语阅读 shape: the rule throws (`.match()[1]` of null) on a page without the marker. In a batch only the first
    page evaluates it, so no script error is recorded."""
    rule = r'@js:java.ajax("/count"); var m = String(result).match(/NEXT\[(.*?)\]/); m[1].split(",").join("\n")'
    pages = {
        "/toc1": _toc_page(["1"], []) + "NEXT[/toc2,/toc3]",
        "/toc2": _toc_page(["2"], []),
        "/toc3": _toc_page(["3"], []),
        "/count": "x",
    }
    site, result, runtime = _walk_toc(monkeypatch, pages, _toc_source(rule))
    assert result[0] is True and result[4] == 3
    assert site.requested("/toc2") == 1 and site.requested("/toc3") == 1
    assert site.requested("/count") == 1  # only the first page ran the nextTocUrl rule


def test_a_a_single_next_url_is_followed_serially_taking_only_the_first_url_of_each_page(monkeypatch) -> None:
    pages = {
        "/toc1": _toc_page(["1"], ["/toc2"]),
        "/toc2": _toc_page(["2"], ["/toc3", "/toc4"]),
        "/toc3": _toc_page(["3"], []),
        "/toc4": _toc_page(["4"], []),
    }
    site, result, _runtime_after = _walk_toc(monkeypatch, pages)
    assert result[0] is True and result[4] == 3
    assert site.requested("/toc3") == 1 and site.requested("/toc4") == 0


def test_a_a_next_url_equal_to_the_current_page_and_loops_are_dropped(monkeypatch) -> None:
    pages = {
        "/toc1": _toc_page(["1"], ["/toc1", "/toc2"]),
        "/toc2": _toc_page(["2"], ["/toc1"]),
    }
    site, result, _runtime_after = _walk_toc(monkeypatch, pages)
    assert result[0] is True and result[4] == 2
    assert site.requested("/toc1") == 1 and site.requested("/toc2") == 1


def test_a_the_80_page_cap_is_kept(monkeypatch) -> None:
    pages = {f"/toc{i}": _toc_page([str(i)], [f"/toc{i + 1}"]) for i in range(1, 200)}
    site, result, _runtime_after = _walk_toc(monkeypatch, pages)
    assert result[4] == 80 and len(site.log) == 80


# ---------------------------------------------------------------------------------------------------------- B @get


def test_b_get_variable_inside_a_js_tail_is_expanded_before_the_script_runs() -> None:
    runtime = _runtime({"book": "B42"})
    values = vsp.evaluate_rule("$.id@js:'/c?bookId=@get:{book}&id='+result", json.dumps({"id": 7}), runtime, "json")
    assert values == ["/c?bookId=B42&id=7"]
    values = vsp.evaluate_rule("@js:'x-@get:{book}-'+result", "R", _runtime({"book": "B42"}), "html")
    assert values == ["x-B42-R"]


@needs_quickjs
def test_b_get_in_the_script_of_a_pipeline_segment_does_not_stay_literal() -> None:
    runtime = _runtime({"p": "99"})
    item = json.dumps({"chapter_id": 5})
    values = vsp.evaluate_rule("$.chapter_id@js:'/api?comic_id=@get:{p}&chapter_id='+result", item, runtime, "json")
    assert values == ["/api?comic_id=99&chapter_id=5"]


def test_b_toc_chapter_url_from_a_js_rule_reaches_the_content_request_expanded(monkeypatch) -> None:
    pages = {
        "/toc1": '<ul class="toc"><li><a href="s1">第一章</a></li></ul>',
        "/c/BK/s1": f'<div id="c">{LONG_TEXT}</div>',
    }
    site = Site(pages)
    monkeypatch.setattr(vsp, "fetch_text", site)
    src = {
        "bookSourceName": "R155 B",
        "bookSourceUrl": BASE,
        "ruleToc": {
            "chapterList": ".toc li",
            "chapterName": "a@text",
            "chapterUrl": "a@href@js:'/c/@get:{book}/'+result",
        },
        "ruleContent": {"content": "#c@text"},
    }
    runtime = vsp.make_runtime(src)
    runtime.variables = {"book": "BK"}
    ok, _final, chapter_url, title, _pages, _first, _count = vsp.resolve_toc_source(
        requests.Session(), src, runtime, f"{BASE}/book", f"{BASE}/toc1"
    )
    assert ok and title == "第一章" and "@get" not in chapter_url
    content_ok, _text, chars = vsp.resolve_chapter_content(requests.Session(), src, runtime, chapter_url, title, "B")
    assert content_ok and chars > 0
    assert all("@get" not in path for path in site.log) and site.requested("/c/BK/s1") == 1


def test_b_content_request_url_expands_a_leftover_get(monkeypatch) -> None:
    site = Site({"/c/Z9": f'<div id="c">{LONG_TEXT}</div>'})
    monkeypatch.setattr(vsp, "fetch_text", site)
    src = {"bookSourceName": "x", "bookSourceUrl": BASE, "ruleContent": {"content": "#c@text"}}
    runtime = vsp.make_runtime(src)
    runtime.variables = {"k": "Z9"}
    ok, _text, _chars = vsp.resolve_chapter_content(requests.Session(), src, runtime, "/c/@get:{k}", "t", "b")
    assert ok and site.log == ["/c/Z9"]


# ------------------------------------------------------------------------------------------------------ D !N exclusion

TABLE = (
    "<table><tr><th>head</th></tr><tr><td>1</td></tr><tr><td>2</td></tr><tr><td>3</td></tr></table>"
)


@pytest.mark.parametrize(
    "rule,expected_count",
    [("tr!0", 3), ("tr!-1", 3), ("tr!0:3", 2), ("tr[!0, 1]", 2), ("table tr!0", 3), ("tr!9", 4)],
)
def test_d_bare_selector_with_exclusion_index(rule: str, expected_count: int) -> None:
    items = vsp.evaluate_book_list(rule, TABLE, _runtime())
    assert len(items) == expected_count


def test_d_exclusion_removes_the_listed_rows_not_the_whole_match() -> None:
    items = vsp.evaluate_book_list("tr!0", TABLE, _runtime())
    assert "head" not in "".join(items) and "<td>1</td>" in items[0]
    last_removed = vsp.evaluate_book_list("tr!-1", TABLE, _runtime())
    assert "<td>3</td>" not in "".join(last_removed) and "head" in last_removed[0]


def test_d_descendant_selector_with_leading_class_and_negative_index() -> None:
    html = '<div class="mySearch"><ul><li>a</li></ul><ul><li>b</li></ul><ul><li>c</li></ul></div>'
    items = vsp.evaluate_book_list(".mySearch ul!-1", html, _runtime())
    assert len(items) == 2 and "c" not in "".join(items).replace("<ul>", "").replace("</ul>", "").replace("<li>", "").replace("</li>", "")


def test_d_helper_only_accepts_single_step_bare_exclusion_rules() -> None:
    assert vsp.is_bare_css_exclusion_rule("tr!0")
    assert vsp.is_bare_css_exclusion_rule(".a ul!-1")
    assert not vsp.is_bare_css_exclusion_rule("tbody@tr!0")
    assert not vsp.is_bare_css_exclusion_rule("$.a[?(@.x!=1)]")
    assert not vsp.is_bare_css_exclusion_rule("tr.0")
    assert not vsp.is_bare_css_exclusion_rule("a[href!=1]")


# ------------------------------------------------------------------------------------------- M :lt / :gt / :eq pseudo


def test_m_jsoup_index_pseudo_selectors() -> None:
    html = "<table><tbody><tr><td>0</td></tr><tr><td>1</td></tr><tr><td>2</td></tr><tr><td>3</td></tr></tbody></table>"
    lt = vsp.css_extract(html, "tbody tr:lt(2)", "text", None, BASE)
    gt = vsp.css_extract(html, "tbody tr:gt(1)", "text", None, BASE)
    eq = vsp.css_extract(html, "tbody tr:eq(2)", "text", None, BASE)
    assert lt == ["0", "1"] and gt == ["2", "3"] and eq == ["2"]
    assert vsp.css_extract(html, "tbody tr:lt(0)", "text", None, BASE) == []
    assert vsp.normalize_selector("a:contains(x):lt(3)") == "a:-soup-contains(x):nth-child(-n+3)"


def test_m_css_rule_with_lt_in_a_list_rule() -> None:
    html = "<table>" + "".join(f"<tr><td>{i}</td></tr>" for i in range(10)) + "</table>"
    assert len(vsp.evaluate_book_list("@css:tbody tr:lt(5)", html, _runtime())) == 5


# ------------------------------------------------------------------------------------------------ L java.getElements

SFACG = (
    '<div class="mulu_list"><a href="/c/1">一</a><a href="/c/2">二</a></div>'
    '<div class="mulu_list"><a href="/c/3">三</a></div>'
)


def test_l_rule_detection() -> None:
    for rule in ("tag.a", "id.x", "class.mulu_list", "text.二", "children", "@@tag.a.1", "tag.a.1", "a@href", "li!0",
                 "a||b", "@css:a", "@xpath://a", "//a", "div[!0]"):
        assert vsp.is_legado_element_rule(rule), rule
    for rule in ("a", "div.item", ".mulu_list", "tr:lt(5)", "a[href]", "", "@js:1", "<js>1</js>"):
        assert not vsp.is_legado_element_rule(rule), rule


def test_l_select_elements_by_rule_json_rows() -> None:
    rows = json.loads(vsp.select_elements_by_rule_json("tag.a", SFACG, _runtime()))
    assert [row["text"] for row in rows] == ["一", "二", "三"] and rows[0]["href"] == "/c/1"
    assert json.loads(vsp.select_elements_by_rule_json("@@tag.a.1", SFACG, _runtime()))[0]["text"] == "二"
    assert json.loads(vsp.select_elements_by_rule_json("a", SFACG, _runtime())) is None  # plain CSS: JS keeps its path
    assert json.loads(vsp.select_elements_by_rule_json("class.mulu_list@tag.a.-1", SFACG, _runtime()))[0]["text"] == "三"
    assert json.loads(vsp.select_elements_by_rule_json("tag.zzz", SFACG, _runtime())) == []
    assert json.loads(vsp.select_elements_by_rule_json("tag.a", '{"a": 1}', _runtime())) is None


@needs_quickjs
def test_l_js_bridge_get_elements_and_get_element_use_legado_rules() -> None:
    runtime = _runtime()
    script = (
        "var n = java.getElements('tag.a', result).size();"
        "var s = java.getElement('@@tag.a.1').text();"
        "n + '|' + s"
    )
    assert vsp.evaluate_js(script, SFACG, runtime) == "3|二"


@needs_quickjs
def test_l_js_bridge_set_content_element_then_get_elements_chain() -> None:
    """The sfacg toc script: volumes -> setContent(volume) -> getElements(tag.a) of THAT volume."""
    script = (
        "var out = [];"
        "java.getElements('class.mulu_list').toArray().forEach(function(vol){"
        "  java.setContent(vol);"
        "  out.push(java.getElements('tag.a').toArray().map(function(a){ return a.text(); }).join(','));"
        "});"
        "out.join('|')"
    )
    assert vsp.evaluate_js(script, SFACG, _runtime()) == "一,二|三"


@needs_quickjs
def test_l_js_bridge_plain_css_selectors_keep_working_and_support_index_pseudo() -> None:
    runtime = _runtime()
    assert vsp.evaluate_js("java.getElements('a', result).size()", SFACG, runtime) == "3"
    assert vsp.evaluate_js("java.getElements('a:lt(1)', result).size()", SFACG, runtime) == "2"


# --------------------------------------------------------------------------------------------------------- H tocUrl


def test_h_toc_url_rule_result_is_not_filtered_by_the_unusable_toc_heuristics(monkeypatch) -> None:
    """ruleToc / tocUrl `text.more@href` yields a legal `/category/..` URL (ruarourou): with a tocUrl rule only the
    pagination-plausibility check applies; with neither a tocUrl nor a chapterList rule the heuristics stay."""
    pages = {
        "/book/1": '<h1>书</h1><a class="more" href="/category/list">更多</a>',
        "/category/list": '<ul class="toc"><li><a href="/c/1">第一章</a></li></ul>',
        "/c/1": f'<div id="c">{LONG_TEXT}</div>',
    }
    site = Site(pages)
    monkeypatch.setattr(vsp, "fetch_text", site)
    src = {
        "bookSourceName": "R155 H",
        "bookSourceUrl": BASE,
        "ruleBookInfo": {"name": "h1@text", "tocUrl": "a.more@href"},
        "ruleToc": {"chapterList": ".toc li", "chapterName": "a@text", "chapterUrl": "a@href"},
        "ruleContent": {"content": "#c@text"},
    }
    runtime = vsp.make_runtime(src)
    ok, name, toc_url, error = vsp._fetch_book_info(requests.Session(), src, runtime, f"{BASE}/book/1")
    assert ok, error
    assert name == "书" and toc_url == f"{BASE}/category/list"


# ----------------------------------------------------------------------------------------------------- G globalThis


@needs_quickjs
def test_g_a_script_can_declare_its_own_headers_and_variables() -> None:
    runtime = _runtime({"k": "v"})
    runtime.headers = {"X-A": "1"}
    assert vsp.evaluate_js("let headers = {a: 7}; String(headers.a)", "", runtime) == "7"
    assert vsp.evaluate_js("let variables = {b: 8}; String(variables.b)", "", runtime) == "8"
    # the injected objects are still there for a script that does not redeclare them
    assert vsp.evaluate_js("String(headers['X-A']) + String(variables.k)", "", runtime) == "1v"


# --------------------------------------------------------------------------------------------------------- E regex dot


def test_e_dot_translation_outside_classes_and_escapes() -> None:
    dot = "[^\\n\\r\\u0085\\u2028\\u2029]"
    translate = vsp.java_regex_dot_to_python
    assert translate("第(.*)页") == f"第({dot}*)页"
    assert translate("a\\.b") == "a\\.b"
    assert translate("[.a]") == "[.a]"
    assert translate("[^.]x.") == f"[^.]x{dot}"
    assert translate("[a[.]b].") == f"[a[.]b]{dot}"
    assert translate("[]\\].]x.") == f"[]\\].]x{dot}"
    assert translate("\\\\.") == f"\\\\{dot}"
    assert translate("(?s)a.b") == "(?s)a.b"
    assert translate("(?is)a.b") == "(?is)a.b"
    assert translate("abc") == "abc"


def test_e_replace_regex_dot_does_not_cross_a_lone_cr() -> None:
    text = "正文一\r第1页\r正文二"
    assert vsp.legado_replace_regex(text, "第(.*)页") == "正文一\r\r正文二"
    # the same pattern on a LF text behaves as before
    assert vsp.legado_replace_regex("正文一\n第1页\n正文二", "第(.*)页") == "正文一\n\n正文二"
    for terminator in ("\u0085", " ", " "):
        assert vsp.legado_replace_regex(f"a{terminator}b", "a(.*)b") == f"a{terminator}b"


def test_e_dotall_flag_keeps_python_dot_all_semantics() -> None:
    assert vsp.legado_replace_regex("x\ry", "(?s)x(.*)y") == ""


# ------------------------------------------------------------------------------------------------------ F outerHtml


def test_f_attributes_are_always_double_quoted_and_escaped_like_jsoup() -> None:
    html = '<div><a href=\'"/book/1"\' class=\'"g"\' title="a&amp;b">x</a></div>'
    outer = vsp.css_extract(html, "a", "outerhtml", None, BASE)[0]
    assert outer == '<a href="&quot;/book/1&quot;" class="&quot;g&quot;" title="a&amp;b">x</a>'
    assert vsp.css_extract(html, "div", "html", None, BASE)[0] == outer
    assert vsp._serialize_html(vsp.BeautifulSoup('<p data-x=\'say "hi"\'>t</p>', "html.parser").p) == '<p data-x="say &quot;hi&quot;">t</p>'


def test_f_source_attribute_order_is_kept() -> None:
    outer = vsp.css_extract('<a z="1" b="2" a="3">x</a>', "a", "outerhtml", None, BASE)[0]
    assert outer == '<a z="1" b="2" a="3">x</a>'


def test_f_element_rows_of_the_bridge_carry_the_jsoup_serialisation() -> None:
    row = json.loads(vsp.select_elements_json("a", '<a class=\'"g"\' href="/x">t</a>', BASE))[0]
    assert row["outerHtml"] == '<a class="&quot;g&quot;" href="/x">t</a>'


@needs_quickjs
def test_f_js_regex_on_serialised_elements_matches_like_the_52po_rule() -> None:
    html = '<div class="l"><a href=\'"/book/9"\' class=\'"g"\'>名</a></div>'
    script = (
        "var s = java.getElements('tag.a', result).toString();"
        "var m = s.match(/class=\"\\&quot;g\\&quot;\" ?/);"
        "m ? 'hit' : 'miss'"
    )
    assert vsp.evaluate_js(script, html, _runtime()) == "hit"


# ----------------------------------------------------------------------------------------------- C / N profile gated

SINA = json.dumps({"data": {"total_page": 3, "chapters": [{"t": "一"}, {"t": "二"}, {"t": "三"}]}}, ensure_ascii=False)


def test_c_list_rule_with_trailing_put_runs_the_put_on_the_whole_content_in_112_only() -> None:
    rule = "data.chapters@put:{total_page:data.total_page}"
    with vsp.app_profile_scope("1.1.2"):
        runtime = _runtime()
        items = vsp.evaluate_book_list(rule, SINA, runtime)
        assert len(items) == 3 and json.loads(items[0]) == {"t": "一"}
        assert runtime.variables.get("total_page") == "3"
    with vsp.app_profile_scope("1.1.1"):
        runtime = _runtime()
        items = vsp.evaluate_book_list(rule, SINA, runtime)
        assert len(items) == 1  # the App 1.1.1 behaviour (one bogus item) is unchanged


def test_c_trailing_put_split_only_accepts_a_tail_of_puts() -> None:
    assert vsp.trailing_put_split("data.chapters@put:{a:b}") == ("data.chapters", ["a:b"])
    assert vsp.trailing_put_split("x@put:{a:b}@put:{c:d}") == ("x", ["a:b", "c:d"])
    assert vsp.trailing_put_split("x@put:{a:b}y") is None
    assert vsp.trailing_put_split("@put:{a:b}") is None
    assert vsp.trailing_put_split("@js:x@put:{a:b}") is None
    assert vsp.trailing_put_split("data.chapters") is None


QQ = json.dumps({"code": 0, "data": {"bookList": [{"title": "甲"}, {"title": "乙"}]}}, ensure_ascii=False)


def test_n_leading_single_dot_json_path_is_a_deep_scan_in_112_only() -> None:
    with vsp.app_profile_scope("1.1.2"):
        assert len(vsp.evaluate_book_list(".bookList[*]", QQ, _runtime())) == 2
        assert len(vsp.evaluate_book_list("..bookList[*]", QQ, _runtime())) == 2
        assert len(vsp.evaluate_book_list("$.data.bookList[*]", QQ, _runtime())) == 2
        deep = json.dumps({"bookList": [{"a": 1}], "x": {"bookList": [{"a": 2}, {"a": 3}]}})
        assert len(vsp.evaluate_book_list(".bookList[*]", deep, _runtime())) == 3  # root level AND nested
    with vsp.app_profile_scope("1.1.1"):
        assert vsp.evaluate_book_list(".bookList[*]", QQ, _runtime()) == []  # unchanged: root-level key only
        assert len(vsp.evaluate_book_list("$.data.bookList[*]", QQ, _runtime())) == 2


def test_profile_flags_c_and_n() -> None:
    from validator import app_profiles

    assert app_profiles.APP_PROFILES["1.1.1"].list_put_on_whole_content is False
    assert app_profiles.APP_PROFILES["1.1.1"].json_leading_dot_deep_scan is False
    assert app_profiles.APP_PROFILES["1.1.2"].list_put_on_whole_content is True
    assert app_profiles.APP_PROFILES["1.1.2"].json_leading_dot_deep_scan is True


# ------------------------------------------------------- Q detail-field side effects / R lenient attribute value / S src


def test_q_detail_fields_other_than_name_kind_toc_url_run_their_put_side_effects(monkeypatch) -> None:
    """漫客栈: ruleBookInfo.wordCount `@put:{p:…}` feeds `@get:{p}` of the chapter URL (Legado BookInfo.kt:68-140 runs every field)."""
    site = Site({"/book/1": '<h1>书</h1><i class="id">777</i>'})
    monkeypatch.setattr(vsp, "fetch_text", site)
    src = {
        "bookSourceName": "R155 Q",
        "bookSourceUrl": BASE,
        "ruleBookInfo": {"name": "h1@text", "wordCount": "@put:{p:i.id@text}", "intro": "h1@text##x##y"},
    }
    runtime = vsp.make_runtime(src)
    ok, name, _toc, error = vsp._fetch_book_info(requests.Session(), src, runtime, f"{BASE}/book/1")
    assert ok, error
    assert name == "书" and (runtime.variables or {}).get("p") == "777"


def test_q_a_failing_side_effect_field_never_fails_the_detail_stage(monkeypatch) -> None:
    site = Site({"/book/1": "<h1>书</h1>"})
    monkeypatch.setattr(vsp, "fetch_text", site)
    src = {
        "bookSourceName": "R155 Q2",
        "bookSourceUrl": BASE,
        "ruleBookInfo": {"name": "h1@text", "lastChapter": "h1@text@put:{k:nothing}@js:throw new Error('x')"},
    }
    runtime = vsp.make_runtime(src)
    ok, name, _toc, error = vsp._fetch_book_info(requests.Session(), src, runtime, f"{BASE}/book/1")
    assert ok and name == "书", error
    assert runtime.script_error == ""


def test_r_jsoup_unquoted_attribute_value_selector() -> None:
    html = '<div style="text-indent: 2em;">正文</div><div style="color: red">other</div>'
    assert vsp.normalize_selector("div[style=text-indent: 2em;]") == 'div[style="text-indent: 2em;"]'
    assert vsp.normalize_selector('a[href="x y"]') == 'a[href="x y"]'
    assert vsp.normalize_selector("a[href^=http]") == "a[href^=http]"
    assert vsp.css_extract(html, "div[style=text-indent: 2em;]", "text", None, BASE) == ["正文"]
    assert vsp.evaluate_book_list("div[style=text-indent: 2em;]", html, _runtime()) == [
        '<div style="text-indent: 2em;">正文</div>'
    ]


@needs_quickjs
def test_s_src_is_the_rule_content_not_the_previous_segment_result() -> None:
    """Legado AnalyzeRule.evalJS binds `src` to the content; `result` is the chain value."""
    html = '<a href="/x" data-k="K1">t</a>'
    assert vsp.evaluate_rule("a@href@js:result + '|' + String(src).length", html, _runtime(), "html") == [
        "/x|" + str(len(html))
    ]
    assert vsp.evaluate_rule("a@href\n<js>String(src).indexOf('data-k') >= 0 ? 'has-src' : 'lost-src'</js>", html, _runtime(), "html") == [
        "has-src"
    ]
