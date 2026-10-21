"""R159: remaining rrs / full-package rule gaps (offline; patched fetches and synthetic pages only).

Profile 1.1.2 only (mirrors the Swift packages E1-JS / E1-RULE, new `AppProfile` flags): G1 option-js `java.url` / `java.headerMap`, G2 Java-string
`.length()`, xs8 `Elements.remove(int)` / `subList().clear()`, G3 `<?xml` content parsed as XML, yamibo JsoupXpath string operators
(`^=` `$=` `*=` `~=` `!~`), wenxue88 volume raw-url title prefix.
Validator-only (both profiles): G4 next-chapter boundary + `nextChapterUrl` binding, G5 explore transport errors +
`bookSourceType` in the report entry, G6 rate-limit pages (App `containsSearchBlockingPageSignals` AND `containsAntiBotOrRateLimitSignals`), czbooks
`java.t2s` speed, xs8 toc gaps (bracket-depth `~=`, complex `:has()`, hidden `_parent`).
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest
import requests
from bs4 import BeautifulSoup

from validator import app_profiles
from validator import validate_source_packages as vsp

BASE = "https://example.test"
needs_js = pytest.mark.skipif(vsp.quickjs is None and not vsp.node_binary_path(), reason="no JS engine (QuickJS bridge or node)")
needs_quickjs = pytest.mark.skipif(vsp.quickjs is None, reason="QuickJS bridge only")
LONG_TEXT = "这是一段足够长的正文内容。" * 30


class PrefixSite:
    """Patched ``fetch_text`` answering by path prefix (first match, query included); records every requested path."""

    def __init__(self, pages: dict[str, Any]) -> None:
        self.pages = dict(pages)
        self.log: list[str] = []

    def __call__(self, session: requests.Session, url: str, **kwargs: Any) -> tuple[str, str, int]:
        path = url.replace(BASE, "")
        self.log.append(path)
        for prefix, page in self.pages.items():
            if path.startswith(prefix):
                return (page(url) if callable(page) else page), url, 200
        return "", url, 404


@pytest.fixture(params=["quickjs", "node"])
def js_engine(request, monkeypatch) -> str:
    """Run the JS tests on QuickJS (what the shipped EXE uses) and on node (the fallback / crypto engine)."""
    if request.param == "quickjs" and vsp.quickjs is None:
        pytest.skip("QuickJS bridge only")
    if request.param == "node":
        if not vsp.node_binary_path():
            pytest.skip("node is not available")
        monkeypatch.setattr(vsp, "make_quickjs_context", lambda *args, **kwargs: None)  # evaluate_js falls back to the node engine
    return request.param


def _runtime(src: dict[str, Any] | None = None) -> vsp.RuleRuntime:
    return vsp.make_runtime(src or {"bookSourceName": "R159", "bookSourceUrl": BASE})


# ------------------------------------------------------------------------------------------------ profile flags

NEW_FLAGS = (
    "js_option_java_url_header_map",
    "js_string_length_method",
    "js_elements_remove_by_index",
    "xml_prefix_uses_xml_parser",
    "xpath_string_operators",
    "toc_volume_raw_url_title_prefix_filter",
)


@pytest.mark.parametrize("flag", NEW_FLAGS)
def test_new_profile_flags_are_off_for_1_1_1_and_on_for_1_1_2(flag: str) -> None:
    with app_profiles.app_profile_scope("1.1.1"):
        assert getattr(app_profiles.current_app_profile(), flag) is False
    with app_profiles.app_profile_scope("1.1.2"):
        assert getattr(app_profiles.current_app_profile(), flag) is True


# ------------------------------------------------------------------------------------------------ G1 option js: java.url / java.headerMap

OPTION_JS_TEMPLATE = (
    '/search?q={{key}},{"js":"java.headerMap.put(\'Cookie\', \'is_human=\' + java.url.length);'
    "java.headerMap.put('X-Seed', java.headerMap.get('Accept'));"
    "java.url + '&extra=1'\"}"
)


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g1_option_js_sees_the_request_url_and_a_writable_header_map_on_1_1_2() -> None:
    runtime = _runtime({"bookSourceName": "g1", "bookSourceUrl": BASE, "header": json.dumps({"Accept": "text/x"})})
    with app_profiles.app_profile_scope("1.1.2"):
        url, opts = vsp._build_url_with_options(OPTION_JS_TEMPLATE, "kw", 1, runtime)
    # java.url is the ABSOLUTE url before the options (Legado AnalyzeUrl.kt:218): https://example.test/search?q=kw (28 chars)
    assert url == f"{BASE}/search?q=kw&extra=1"
    assert opts["headers"]["Cookie"] == f"is_human={len(BASE + '/search?q=kw')}"
    assert opts["headers"]["X-Seed"] == "text/x"  # headerMap is seeded with the source headers
    assert not any(str(key).startswith("__optHdr_") for key in (runtime.variables or {}))  # bookkeeping variables are removed again


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g1_header_map_put_returns_the_previous_value_and_remove_works() -> None:
    template = (
        '/s,{"js":"var a = java.headerMap.put(\'K\', \'1\'); var b = java.headerMap.put(\'K\', \'2\');'
        "var c = java.headerMap.containsKey('K'); var d = java.headerMap.remove('K'); var e = java.headerMap.get('K');"
        "a + '|' + b + '|' + c + '|' + d + '|' + e\"}"
    )
    with app_profiles.app_profile_scope("1.1.2"):
        url, _opts = vsp._build_url_with_options(template, "x", 1, _runtime())
    assert url == "null|1|true|2|null"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g1_is_not_applied_on_1_1_1() -> None:
    runtime = _runtime()
    with app_profiles.app_profile_scope("1.1.1"):
        url, opts = vsp._build_url_with_options(OPTION_JS_TEMPLATE, "kw", 1, runtime)
    assert "Cookie" not in (opts.get("headers") or {})
    assert url != f"{BASE}/search?q=kw&extra=1"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g1_an_option_js_without_header_writes_leaves_the_headers_option_alone() -> None:
    template = '/s?k={{key}},{"headers":{"Referer":"https://r.test"},"js":"java.url + \'&p=2\'"}'
    with app_profiles.app_profile_scope("1.1.2"):
        url, opts = vsp._build_url_with_options(template, "q", 1, _runtime())
    assert url == f"{BASE}/s?k=q&p=2"
    assert opts["headers"] == {"Referer": "https://r.test"}


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g1_header_map_names_are_case_insensitive_like_the_app() -> None:
    template = (
        '/s,{"js":"var a = java.headerMap.put(\'cookie\', \'1\'); var b = java.headerMap.containsKey(\'COOKIE\');'
        "var c = java.headerMap.get('CoOkIe'); var d = java.headerMap.put('COOKIE', '2'); var e = java.headerMap.get('cookie');"
        "[a, b, c, d, e].join('|')\"}"
    )
    runtime = _runtime({"bookSourceName": "g1", "bookSourceUrl": BASE, "header": json.dumps({"Cookie": "seed"})})
    with app_profiles.app_profile_scope("1.1.2"):
        url, opts = vsp._build_url_with_options(template, "x", 1, runtime)
    assert url == "seed|true|1|1|2"  # put overwrites the existing name in place and returns the previous value
    assert opts["headers"] == {"Cookie": "2"}  # and no second `cookie` entry appears


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g1_a_removed_source_header_is_not_sent() -> None:
    template = '/s,{"js":"java.headerMap.remove(\'x-token\'); java.url"}'
    runtime = _runtime({"bookSourceName": "g1", "bookSourceUrl": BASE, "header": json.dumps({"X-Token": "t", "Accept": "a"})})
    with app_profiles.app_profile_scope("1.1.2"):
        _url, opts = vsp._build_url_with_options(template, "x", 1, runtime)
    merged = vsp.merge_request_headers(dict(runtime.headers or {}), opts.get("headers"))
    assert "X-Token" not in merged and merged["Accept"] == "a"


def test_g1_merge_request_headers_applies_none_as_a_case_insensitive_delete() -> None:
    assert vsp.merge_request_headers({"A": "1", "B": "2"}, {"a": None, "C": "3"}) == {"B": "2", "C": "3"}
    assert vsp.merge_request_headers({"A": "1"}, None) == {"A": "1"}


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g1_the_prelude_takes_the_url_and_headers_as_data_not_code() -> None:
    hostile = "__SEED__ __PREFIX__ ');java.put('pwn','1');('"
    runtime = _runtime({"bookSourceName": "g1", "bookSourceUrl": BASE, "header": json.dumps({"X": hostile})})
    template = '/s,{"js":"java.headerMap.get(\'X\') + \'|\' + java.get(\'pwn\')"}'
    with app_profiles.app_profile_scope("1.1.2"):
        url, _opts = vsp._build_url_with_options(template, "x", 1, runtime)
    assert url == hostile + "|"
    assert "pwn" not in (runtime.variables or {})


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g1_a_throwing_option_js_leaves_no_bookkeeping_variable_behind() -> None:
    template = '/s,{"js":"java.headerMap.put(\'K\', \'1\'); throw new Error(\'boom\')"}'
    runtime = _runtime()
    with app_profiles.app_profile_scope("1.1.2"):
        try:
            vsp._build_url_with_options(template, "x", 1, runtime)
        except Exception:
            pass
    assert not any(str(key).startswith("__optHdr") for key in (runtime.variables or {}))


# ------------------------------------------------------------------------------------------------ G2 Java String .length()


def test_g2_rewrite_only_touches_member_calls_in_code() -> None:
    rewrite = vsp.rewrite_java_string_length_calls
    helper = ".__readoriJavaLength()"
    assert rewrite("var n = k.length();") == "var n = k" + helper + ";"
    assert rewrite("a.length ( )") == "a" + helper
    assert rewrite("x.length + y.length") == "x.length + y.length"  # the property is untouched
    assert rewrite("var s = 'a.length()'; var t = \"b.length()\";") == "var s = 'a.length()'; var t = \"b.length()\";"
    assert rewrite("// k.length()\nvar z = 1;") == "// k.length()\nvar z = 1;"
    assert rewrite("var r = /a.length()/;") == "var r = /a.length()/;"
    assert rewrite("function length() {} length();") == "function length() {} length();"  # a bare call is not a member call
    assert rewrite("o.length(1)") == "o.length(1)"  # a call with arguments is not the String method


def test_g2_rewrite_reaches_the_code_inside_a_template_literal_interpolation() -> None:
    rewrite = vsp.rewrite_java_string_length_calls
    helper = ".__readoriJavaLength()"
    assert rewrite("`n=${k.length()}`") == "`n=${k" + helper + "}`"
    assert rewrite("`literal k.length() ${1}`") == "`literal k.length() ${1}`"  # the literal text of the template stays
    assert rewrite("`a${ {x: k.length()}.x }b${'q'.length()}`") == "`a${ {x: k" + helper + "}.x }b${'q'" + helper + "}`"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g2_string_length_method_works_on_1_1_2_and_the_property_still_does() -> None:
    runtime = _runtime()
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js("var k = 'abcd'; k.length() + ':' + k.length", "", runtime) == "4:4"
        assert vsp.evaluate_js("'x.length()' + ['a','b'].length", "", runtime) == "x.length()2"
        assert vsp.evaluate_js("var k = 'ab'; `n=${k.length()}|${'xyz'.length()}`", "", runtime) == "n=2|3"
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js("var k = 'abcd'; k.length()", "", _runtime()) == ""  # TypeError: not a function


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g2_an_object_with_its_own_length_method_keeps_it_on_1_1_2() -> None:
    """P1-1: Rhino calls the object's own `length()`; a blind `.length()` -> `.length` rewrite would return a function."""
    script = (
        "var o = { items: [1, 2, 3], length: function() { return this.items.length * 10; } };"
        "function Stack() { this.n = 4; } Stack.prototype.length = function() { return this.n; };"
        "[o.length(), new Stack().length(), 'abc'.length(), java.getFile('nothing-here.txt').length()].join('|')"
    )
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(script, "", _runtime()) == "30|4|3|0"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g2_anything_else_still_throws_like_before() -> None:
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js("try { [1, 2].length(); 'no' } catch (e) { 'threw' }", "", _runtime()) == "threw"
        assert vsp.evaluate_js("try { var n = 5; n.length(); 'no' } catch (e) { 'threw' }", "", _runtime()) == "threw"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g2_the_helper_is_not_enumerable_and_is_absent_on_1_1_1() -> None:
    script = "var seen = []; for (var key in {a: 1}) seen.push(key); seen.join(',') + '|' + (typeof Object.prototype.__readoriJavaLength)"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(script, "", _runtime()) == "a|function"
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js(script, "", _runtime()) == "a|undefined"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_g2_the_eval_ed_source_comment_is_rewritten_like_the_script() -> None:
    src = {"bookSourceName": "g2", "bookSourceUrl": BASE, "bookSourceComment": "var cm = 'abcde'; cm.length();"}
    with app_profiles.app_profile_scope("1.1.2"):
        runtime = vsp.make_runtime(src)
        assert vsp._source_comment_for_engine(runtime) == "var cm = 'abcde'; cm.__readoriJavaLength();"
        assert vsp.evaluate_js("eval(String(source.bookSourceComment))", "", runtime) == "5"
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp._source_comment_for_engine(vsp.make_runtime(src)) == src["bookSourceComment"]


def test_g2_the_shared_js_lib_is_rewritten_on_1_1_2_only() -> None:
    runtime = _runtime()
    runtime.js_lib = "function n(s) { return s.length(); }"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp._js_lib_for_engine(runtime) == "function n(s) { return s.__readoriJavaLength(); }"
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp._js_lib_for_engine(runtime) == "function n(s) { return s.length(); }"


# ------------------------------------------------------------------------------------------------ xs8 Elements.remove(int) / subList().clear()

LIST_HTML = "<ul><li>a</li><li>b</li><li>c</li><li>d</li></ul>"


def _jsoup_js(body: str) -> str:
    return "var d = org.jsoup.Jsoup.parse(%s); var l = d.select('li'); %s" % (json.dumps(LIST_HTML), body)


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_xs8_elements_remove_with_an_index_removes_from_the_list_on_1_1_2() -> None:
    script = _jsoup_js("var r = l.remove(1); r.text() + '|' + l.size() + '|' + l.eachText().join(',') + '|' + d.select('li').size()")
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(script, "", _runtime()) == "b|3|a,c,d|4"  # the DOM keeps all four


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_xs8_elements_remove_without_arguments_is_still_the_dom_removal() -> None:
    script = _jsoup_js("l.remove(); l.size() + '|' + d.select('li').size()")
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(script, "", _runtime()) == "0|0"
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js(script, "", _runtime()) == "0|0"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_xs8_elements_remove_with_an_index_is_the_dom_removal_on_1_1_1() -> None:
    script = _jsoup_js("l.remove(1); l.size()")
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js(script, "", _runtime()) == "0"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_xs8_elements_remove_with_an_index_out_of_range_throws() -> None:
    script = _jsoup_js("try { l.remove(9); 'no' } catch (e) { 'threw' }")
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(script, "", _runtime()) == "threw"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_xs8_sublist_clear_removes_the_range_from_the_list() -> None:
    script = _jsoup_js("var s = l.subList(1, 3); var n = s.length; s.clear(); n + '|' + l.size() + '|' + l.eachText().join(',')")
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(script, "", _runtime()) == "2|2|a,d"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_xs8_sublist_clamps_its_indexes_like_the_app() -> None:
    script = _jsoup_js("l.subList(-5, 99).clear(); l.size() + '|' + l.subList(3, 1).length")
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(script, "", _runtime()) == "0|0"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_xs8_sublist_does_not_exist_on_1_1_1() -> None:
    script = _jsoup_js("'[' + typeof l.subList + ']'")
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js(script, "", _runtime()) == "[undefined]"


# ------------------------------------------------------------------------------------------------ G3 `<?xml` content is parsed as XML

RSS = (
    '<?xml version="1.0" encoding="UTF-8"?><rss><channel><title>T</title>'
    "<item><title>One</title><link>https://a.example/p/1</link></item>"
    "<item><title>Two</title><link>https://a.example/p/2</link></item></channel></rss>"
)


def test_g3_rss_link_keeps_its_text_on_1_1_2() -> None:
    runtime = _runtime()
    with app_profiles.app_profile_scope("1.1.2"):
        items = vsp.evaluate_css_chain_rule("tag.channel@tag.item", RSS, runtime)
        assert len(items) == 2
        assert [vsp.evaluate_css_chain_rule("tag.link@text", item, runtime) for item in items] == [
            ["https://a.example/p/1"], ["https://a.example/p/2"]
        ]
        # the prefix test is case-insensitive
        assert vsp.evaluate_css_chain_rule("tag.link@text", RSS.replace("<?xml", "<?XML", 1), runtime) == [
            "https://a.example/p/1", "https://a.example/p/2"
        ]
        # Legado AnalyzeByJSoup.kt:31 tests the raw string: no whitespace / BOM trimming
        assert vsp.evaluate_css_chain_rule("tag.link@text", "\ufeff" + RSS, runtime) == []
        assert vsp.evaluate_css_chain_rule("tag.link@text", "\n  " + RSS, runtime) == []


def test_g3_the_html_parser_is_kept_on_1_1_1_and_for_other_content() -> None:
    runtime = _runtime()
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_css_chain_rule("tag.link@text", RSS, runtime) == []  # void `<link>`: the url is a sibling text node
    with app_profiles.app_profile_scope("1.1.2"):
        html = '<html><head><link href="/x.css"></head><body><div class="a">hi</div></body></html>'
        assert vsp.evaluate_css_chain_rule("tag.link@text", html, runtime) == []
        assert vsp.evaluate_css_chain_rule(".a@text", html, runtime) == ["hi"]
        # only a leading `<?xml` triggers the XML mode, not a declaration somewhere inside
        inner = "<div><?xml version='1.0'?><link>kept</link></div>"
        assert vsp.evaluate_css_chain_rule("tag.link@text", inner, runtime) == []


def test_g3_items_are_plain_outer_html_but_their_field_rules_still_parse_xml() -> None:
    with app_profiles.app_profile_scope("1.1.2"):
        items = vsp.evaluate_css_chain_rule("tag.item", RSS, _runtime())
        # no injected declaration: a JS / regex `result` sees the plain outerHtml
        assert items[0] == "<item><title>One</title><link>https://a.example/p/1</link></item>"
        assert vsp.evaluate_css_chain_rule("tag.link@text", items[0], _runtime()) == ["https://a.example/p/1"]
        assert vsp.evaluate_css_chain_rule("tag.title@text", RSS, _runtime()) == ["T", "One", "Two"]
        # an identical-looking string that did NOT come from an XML document is parsed as HTML
        assert vsp.evaluate_css_chain_rule("tag.link@text", "<item><link>x</link></item>", _runtime()) == []


def test_g3_the_xml_item_registry_is_per_source_and_capped_like_the_app() -> None:
    with app_profiles.app_profile_scope("1.1.2"):
        vsp.begin_xml_element_scope()
        items = vsp.evaluate_css_chain_rule("tag.item", RSS, _runtime())
        assert items and all(item in vsp._xml_element_strings() for item in items)
        vsp.begin_xml_element_scope()  # the next source starts: nothing carries over
        assert not vsp._xml_element_strings()
        assert vsp.evaluate_css_chain_rule("tag.link@text", items[0], _runtime()) == []
        # an item larger than 256 KB UTF-8 is never registered (App CSSEngine.xmlItemMarkerMaxUTF8)
        big = "<item><title>" + "x" * (256 * 1024) + "</title></item>"
        vsp._note_xml_element_strings([big, "<item><link>small</link></item>"])
        assert big not in vsp._xml_element_strings() and "<item><link>small</link></item>" in vsp._xml_element_strings()
        assert vsp.evaluate_css_chain_rule("tag.link@text", big, _runtime()) == []
        vsp.begin_xml_element_scope()


def test_g3_the_xml_item_registry_is_cleared_when_the_app_capacity_is_reached() -> None:
    vsp.begin_xml_element_scope()
    vsp._note_xml_element_strings([f"<i>{n}</i>" for n in range(vsp.XML_ELEMENT_STRING_CAP)])
    assert len(vsp._xml_element_strings()) == vsp.XML_ELEMENT_STRING_CAP
    vsp._note_xml_element_strings(["<i>again</i>"])
    assert vsp._xml_element_strings() == {"<i>again</i>"}
    vsp.begin_xml_element_scope()


# ------------------------------------------------------------------------------------------------ yamibo JsoupXpath string operators

XPATH_HTML = (
    '<ul><li class="list a"><a href="/t1.html">One</a></li><li class="b"><a href="/b/12">Two</a></li>'
    '<li><a href="/t3.html">Three</a></li></ul>'
)


def _xpath(rule: str, html: str = XPATH_HTML) -> list[str]:
    return vsp._xpath_extract_document(html, rule, None, BASE)


def test_xpath_string_operators_rewrite_to_string_functions() -> None:
    rewrite = vsp.jsoupxpath_rewrite_string_operators
    assert rewrite("//li[class^=list]/a/@href") == "//li[starts-with(string(class), string(list))]/a/@href"
    assert rewrite("//a[@href$='.html']") == "//a[(substring(string(@href), string-length(string(@href)) - string-length('.html') + 1) = '.html')]"
    assert rewrite("//a[@href*=\"/b/\"]") == '//a[contains(string(@href), "/b/")]'
    assert rewrite("//a[@href~='^/b/\\d+']") == "//a[re:test(string(@href), '^/b/\\d+')]"
    assert rewrite("//a[@href!~'^/b/']") == "//a[not(re:test(string(@href), '^/b/'))]"
    # quoted operators are literals; text without an operator is returned as it is
    assert rewrite("//a[@title='x^=y']") == "//a[@title='x^=y']"
    assert rewrite("//a[contains(@class,'z') and @id='q']") == "//a[contains(@class,'z') and @id='q']"
    assert rewrite("//a[a/b^='x' and @c*='y']") == "//a[starts-with(string(a/b), 'x') and contains(string(@c), 'y')]"


def test_xpath_string_operators_evaluate_on_1_1_2() -> None:
    with app_profiles.app_profile_scope("1.1.2"):
        # yamibo `li[class^=list]`: a bare name is a child-element test, both sides are the empty string -> every li matches
        assert _xpath("//li[class^=list]/a/@href") == ["/t1.html", "/b/12", "/t3.html"]
        assert _xpath("//li[@class^='list']/a/@href") == ["/t1.html"]
        assert _xpath("//a[@href$='.html']/@href") == ["/t1.html", "/t3.html"]
        assert _xpath("//li[@class*='is']/a/@href") == ["/t1.html"]
        assert _xpath("//li[@class*='b']/a/@href") == ["/b/12"]
        assert _xpath("//a[@href~='^/b/\\d+$']/@href") == ["/b/12"]
        assert _xpath("//a[@href!~'^/b/']/@href") == ["/t1.html", "/t3.html"]


def test_xpath_star_equals_is_an_operator_only_at_operator_position() -> None:
    rewrite = vsp.jsoupxpath_rewrite_string_operators
    # `*=` right after `[` / `@` is a wildcard step compared with `=`, not the contains operator
    assert rewrite("//li[*='Two']") == "//li[*='Two']"
    assert rewrite("//li[@*='b']") == "//li[@*='b']"
    with app_profiles.app_profile_scope("1.1.2"):
        assert _xpath("//li[*='Two']/a/@href") == ["/b/12"]
        assert _xpath("//li[@*='b']/a/@href") == ["/b/12"]
        assert _xpath("//li[@class*='is']/a/@href") == ["/t1.html"]  # the real operator still works


def test_xpath_regex_operators_with_an_invalid_pattern_are_false_and_do_not_throw() -> None:
    with app_profiles.app_profile_scope("1.1.2"):
        assert _xpath("//a[@href~='[']/@href") == []
        assert _xpath("//a[@href!~'(']/@href") == []
        assert _xpath("//a[@href~='^/b/']/@href") == ["/b/12"]  # a valid pattern is unaffected


def test_xpath_string_operators_are_not_available_on_1_1_1() -> None:
    with app_profiles.app_profile_scope("1.1.1"):
        assert _xpath("//li[class^=list]/a/@href") == []
        assert _xpath("//a[@href$='.html']/@href") == []


# ------------------------------------------------------------------------------------------------ wenxue88 volume raw url


def _volume_toc(monkeypatch, rows: list[dict[str, Any]]):
    src = {
        "bookSourceName": "wx",
        "bookSourceUrl": BASE,
        "ruleToc": {"chapterList": "$.list[*]", "chapterName": "$.name", "chapterUrl": "$.url", "isVolume": "$.vol"},
    }
    site = PrefixSite({"/toc": json.dumps({"list": rows})})
    monkeypatch.setattr(vsp, "fetch_text", site)
    return vsp.resolve_toc_source(requests.Session(), src, vsp.make_runtime(src), f"{BASE}/book", f"{BASE}/toc", first_page_only=True)


VOLUME_ROWS = [
    {"name": "序章", "url": "序章", "vol": True},
    {"name": "第一章", "url": "/c/1", "vol": False},
]


def test_volume_whose_raw_url_starts_with_its_title_is_not_the_sample_on_1_1_2(monkeypatch) -> None:
    with app_profiles.app_profile_scope("1.1.2"):
        out = _volume_toc(monkeypatch, VOLUME_ROWS)
    assert out[0] is True and out[2] == f"{BASE}/c/1" and out[3] == "第一章"


def test_volume_raw_url_prefix_filter_is_off_on_1_1_1(monkeypatch) -> None:
    with app_profiles.app_profile_scope("1.1.1"):
        out = _volume_toc(monkeypatch, VOLUME_ROWS)
    assert out[2] == f"{BASE}/序章"  # absolutized, no longer starts with the title: the volume row is the sample


def test_volume_with_a_real_url_stays_a_chapter(monkeypatch) -> None:
    rows = [{"name": "卷一", "url": "/v/1", "vol": True}, {"name": "第一章", "url": "/c/1", "vol": False}]
    with app_profiles.app_profile_scope("1.1.2"):
        assert _volume_toc(monkeypatch, rows)[2] == f"{BASE}/v/1"


# ------------------------------------------------------------------------------------------------ G4 rwsk2: next chapter boundary

CHAPTER_PAGES = {
    "/toc": (
        '<ul class="toc"><li><a href="/txtread/166_1.html">第一章</a></li>'
        '<li><a href="/txtread/166_2.html">第二章</a></li></ul>'
    ),
    "/txtread/166_1.html": f'<div id="c">{LONG_TEXT}</div><a class="next" href="/txtread/166_2.html">下一页</a>',
    "/txtread/166_2.html": f'<div id="c">{LONG_TEXT}</div><a class="next" href="/txtread/166_3.html">下一页</a>',
}


def _chapter_source() -> dict[str, Any]:
    return {
        "bookSourceName": "rwsk2",
        "bookSourceUrl": BASE,
        "ruleToc": {"chapterList": ".toc li", "chapterName": "a@text", "chapterUrl": "a@href"},
        "ruleContent": {"content": "#c@text", "nextContentUrl": "a.next@href"},
    }


def test_toc_stage_records_the_next_chapter_url_for_the_content_stage(monkeypatch) -> None:
    src = _chapter_source()
    monkeypatch.setattr(vsp, "fetch_text", PrefixSite(CHAPTER_PAGES))
    for profile in ("1.1.1", "1.1.2"):
        with app_profiles.app_profile_scope(profile):
            runtime = vsp.make_runtime(src)
            vsp.resolve_toc_source(requests.Session(), src, runtime, f"{BASE}/book", f"{BASE}/toc", first_page_only=True)
            assert runtime.next_chapter_url == f"{BASE}/txtread/166_2.html"


def test_toc_with_a_single_chapter_uses_the_chapter_itself_as_next_chapter(monkeypatch) -> None:
    src = _chapter_source()
    pages = {**CHAPTER_PAGES, "/toc": '<ul class="toc"><li><a href="/txtread/166_1.html">第一章</a></li></ul>'}
    monkeypatch.setattr(vsp, "fetch_text", PrefixSite(pages))
    runtime = vsp.make_runtime(src)
    vsp.resolve_toc_source(requests.Session(), src, runtime, f"{BASE}/book", f"{BASE}/toc", first_page_only=True)
    assert runtime.next_chapter_url == f"{BASE}/txtread/166_1.html"  # CheckSourceService.kt:218 `toc.getOrNull(1)?.url ?: toc.first().url`


def test_content_paging_stops_at_the_next_chapter_url(monkeypatch) -> None:
    """Legado BookContent.kt:50-55 / :87-91: the `下一页` link of the last page is the next chapter (same numeric stem) - not a content page."""
    site = PrefixSite(CHAPTER_PAGES)
    monkeypatch.setattr(vsp, "fetch_text", site)
    src = _chapter_source()
    runtime = vsp.make_runtime(src)
    runtime.next_chapter_url = f"{BASE}/txtread/166_2.html"
    ok, _text, chars = vsp.resolve_chapter_content(requests.Session(), src, runtime, f"{BASE}/txtread/166_1.html", "第一章", "B")
    assert ok and chars > 0
    assert site.log == ["/txtread/166_1.html"]


@pytest.mark.parametrize("href", [
    "/TXTREAD/166_2.html",  # case
    "/txtread/166_2.html#top",  # fragment
    "/txtread/166_2.html/",  # trailing slash
    "/txtread/166_2.html,{\"method\":\"GET\"}",  # url options
])
def test_the_next_chapter_boundary_is_compared_like_the_app(monkeypatch, href: str) -> None:
    """App `normalizedBookCandidateURLForComparison`: options / fragment / trailing slash / case do not make the next chapter a content page."""
    pages = {**CHAPTER_PAGES, "/txtread/166_1.html": f'<div id="c">{LONG_TEXT}</div><a class="next" href=\'{href}\'>下一页</a>'}
    site = PrefixSite(pages)
    monkeypatch.setattr(vsp, "fetch_text", site)
    src = _chapter_source()
    runtime = vsp.make_runtime(src)
    runtime.next_chapter_url = f"{BASE}/txtread/166_2.html"
    ok, _text, _chars = vsp.resolve_chapter_content(requests.Session(), src, runtime, f"{BASE}/txtread/166_1.html", "第一章", "B")
    assert ok and site.log == ["/txtread/166_1.html"]


def test_the_boundary_also_stops_the_derived_next_page_when_there_is_no_next_content_rule(monkeypatch) -> None:
    site = PrefixSite(CHAPTER_PAGES)
    monkeypatch.setattr(vsp, "fetch_text", site)
    src = _chapter_source()
    src["ruleContent"] = {"content": "#c@text"}
    runtime = vsp.make_runtime(src)
    runtime.next_chapter_url = f"{BASE}/txtread/166_2.html/"
    ok, _text, _chars = vsp.resolve_chapter_content(requests.Session(), src, runtime, f"{BASE}/txtread/166_1.html", "第一章", "B")
    assert ok and "/txtread/166_2.html" not in site.log


def test_normalized_chapter_url_for_comparison() -> None:
    norm = vsp.normalized_chapter_url_for_comparison
    assert norm("HTTPS://A.test/B/C.html/#frag") == "https://a.test/b/c.html"
    assert norm('https://a.test/x,{"headers":{}}') == "https://a.test/x"
    assert norm("") == "" and norm("  ") == "" and norm("{}") == "" and norm("[1]") == ""


def test_content_paging_follows_the_link_when_no_boundary_is_known(monkeypatch) -> None:
    site = PrefixSite(CHAPTER_PAGES)
    monkeypatch.setattr(vsp, "fetch_text", site)
    src = _chapter_source()
    runtime = vsp.make_runtime(src)
    ok, _text, _chars = vsp.resolve_chapter_content(requests.Session(), src, runtime, f"{BASE}/txtread/166_1.html", "第一章", "B")
    assert ok and "/txtread/166_2.html" in site.log  # control: the stem heuristic alone cannot tell


@needs_js
@pytest.mark.usefixtures("js_engine")
@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_next_chapter_url_is_bound_in_the_js_context(profile: str) -> None:
    runtime = _runtime()
    runtime.next_chapter_url = f"{BASE}/c/2"
    with app_profiles.app_profile_scope(profile):
        assert vsp.evaluate_js("'[' + nextChapterUrl + ']'", "", runtime) == f"[{BASE}/c/2]"
        assert vsp.evaluate_js("'[' + nextChapterUrl + ']'", "", _runtime()) == "[]"


def test_request_timeout_is_unchanged_at_20_seconds() -> None:
    assert vsp.REQUEST_TIMEOUT == 20  # never above the App (45 s) / Legado (60 s) read timeouts; R159 does not widen it


# ------------------------------------------------------------------------------------------------ G5 explore transport errors / bookSourceType


def test_explore_failed_after_a_tls_error_is_a_network_failure() -> None:
    vsp.begin_network_stage()
    vsp.record_network_event("https://m.iyouman.com/x", "SSLError: certificate verify failed: certificate has expired")
    detail: dict[str, Any] = {}
    vsp._mark_explore_failed(detail)
    assert detail["failureReason"] == "network error: tls"
    assert detail["networkError"]["category"] == "tls" and detail["networkError"]["stage"] == "explore"
    assert detail["failureGroup"] == "发现失效"  # still the App's discovery failure
    vsp.begin_network_stage()


@pytest.mark.parametrize("message,category", [
    ("Connection reset by peer", "connection reset"),
    ("Read timed out.", "timed out"),
    ("getaddrinfo failed", "dns"),
])
def test_explore_failed_after_other_transport_errors(message: str, category: str) -> None:
    vsp.begin_network_stage()
    vsp.record_network_event("https://x.test/", message)
    detail: dict[str, Any] = {}
    vsp._mark_explore_failed(detail)
    assert detail["failureReason"] == f"network error: {category}"
    vsp.begin_network_stage()


def test_explore_failed_after_an_http_404_probe_stays_a_rule_failure() -> None:
    """ndlib's explore GET answers 404 before the successful POST: an HTTP status must not relabel a later rule failure."""
    vsp.begin_network_stage()
    vsp.record_http_status_event("https://m.ndlib.cn/findBookPart.html", 404)
    detail: dict[str, Any] = {}
    vsp._mark_explore_failed(detail)
    assert detail["failureReason"] == "explore failed" and "networkError" not in detail
    vsp.begin_network_stage()


def test_explore_failed_after_a_404_and_a_tls_error_reports_the_tls_error() -> None:
    vsp.begin_network_stage()
    vsp.record_http_status_event("https://a.test/1", 404)
    vsp.record_network_event("https://a.test/2", "SSLError: certificate verify failed")
    detail: dict[str, Any] = {}
    vsp._mark_explore_failed(detail)
    assert detail["failureReason"] == "network error: tls"
    vsp.begin_network_stage()


def test_explore_failed_ignores_transport_errors_from_before_the_discovery_started() -> None:
    """A TLS / timeout error of the SEARCH pass that was retried successfully must not be pinned on a later discovery rule failure."""
    vsp.begin_network_stage()
    vsp.record_network_event("https://a.test/search", "SSLError: certificate verify failed")
    vsp.mark_explore_network_events()  # discovery starts here
    detail: dict[str, Any] = {}
    vsp._mark_explore_failed(detail)
    assert detail["failureReason"] == "explore failed" and "networkError" not in detail
    vsp.record_network_event("https://a.test/explore", "Read timed out.")
    detail = {}
    vsp._mark_explore_failed(detail)
    assert detail["failureReason"] == "network error: timed out" and detail["networkError"]["url"] == "https://a.test/explore"
    vsp.begin_network_stage()


def test_the_report_entry_carries_the_book_source_type() -> None:
    assert vsp._empty_detail({"bookSourceName": "f", "bookSourceUrl": BASE, "bookSourceType": 3})["bookSourceType"] == 3
    assert vsp._empty_detail({"bookSourceName": "f", "bookSourceUrl": BASE})["bookSourceType"] == 0


@pytest.mark.parametrize("raw,expected", [
    (3, 3), ("3", 3), (" 1 ", 1), (None, 0), ("", 0), ("abc", 0), ("1.5", 0), (2.0, 2), (True, 1), ([1], 0), ({"a": 1}, 0),
])
def test_book_source_type_parsing_never_raises(raw: Any, expected: int) -> None:
    src = {"bookSourceName": "f", "bookSourceUrl": BASE, "bookSourceType": raw}
    assert vsp.safe_book_source_type(src) == expected
    assert vsp._empty_detail(src)["bookSourceType"] == expected


def test_a_file_type_report_entry_is_classified_unsupported_source_type() -> None:
    from legado_reference import compare_parity

    detail = vsp._empty_detail({"bookSourceName": "f", "bookSourceUrl": BASE, "bookSourceType": 3})
    detail["failureReason"] = "no search/explore result"  # the throttled search of freexiaoshuo
    assert compare_parity.classify_readori_failure(detail) == "unsupportedSourceType"


# ------------------------------------------------------------------------------------------------ G6 rate-limit pages

THROTTLE_PAGE = "<script>alert('搜索间隔15秒,请稍后重新搜索！');history.back();</script>"
# a blocking page (App `containsSearchBlockingPageSignals`: here the `网址已失效` marker) that also carries a rate-limit keyword
BLOCKED_THROTTLE_PAGE = "<p>网址已失效，请求过于频繁，请稍后再试</p>"


@pytest.mark.parametrize("page", [
    BLOCKED_THROTTLE_PAGE,
    "<p>文件不存在，请稍后再试</p>",
    "<p>cf-turnstile 请稍后再试</p>",
    "<p>just a moment... cloudflare</p>",
    "<p>页面未找到 访问频率超限</p>",
    "<p>hcaptcha</p>",
    "<p>not found</title> 操作过于频繁</p>",
])
def test_rate_limit_needs_a_blocking_page_and_an_anti_bot_keyword(page: str) -> None:
    assert vsp.contains_rate_limit_page_signals(page, vsp.contains_http_error_page_signals(page)) is True


@pytest.mark.parametrize("page", [
    THROTTLE_PAGE,  # biquge365: no blocking marker, and `搜索间隔` is not in the App keyword list
    "<p>系统限制的搜索间隔</p>",  # keyword only
    "<p>请求过于频繁，请稍后再试</p>",  # keyword only
    "<p>操作过于频繁</p>",
    "<p>404 not found</p>",  # blocking marker only
    "<div class='b'>no result</div>",
    "",
])
def test_a_keyword_or_a_blocking_marker_alone_is_not_a_rate_limit_page(page: str) -> None:
    assert vsp.contains_rate_limit_page_signals(page, vsp.contains_http_error_page_signals(page)) is False


def test_rate_limit_has_no_size_cap_like_the_app() -> None:
    assert vsp.contains_rate_limit_page_signals(BLOCKED_THROTTLE_PAGE + "x" * 200000) is True
    assert vsp.contains_rate_limit_page_signals("<p>网址已失效 访问限制</p>" + "x" * 200000) is True
    assert not hasattr(vsp, "RATE_LIMIT_PAGE_MAX_CHARS")


def _search_source() -> dict[str, Any]:
    return {
        "bookSourceName": "biquge365",
        "bookSourceUrl": BASE,
        "enabled": True,
        "searchUrl": "/s.php?q={{key}}",
        "ruleSearch": {"bookList": ".b", "name": "a@text", "bookUrl": "a@href"},
    }


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_a_throttled_search_is_a_network_failure_and_is_not_retried(monkeypatch, profile: str) -> None:
    site = PrefixSite({"/s.php": BLOCKED_THROTTLE_PAGE})
    monkeypatch.setattr(vsp, "fetch_text", site)
    with app_profiles.app_profile_scope(profile):
        passed, _seed, detail = vsp.quick_scan_single_source(_search_source())
    assert not passed
    assert detail["failureReason"] == "network error: rate limited"
    assert detail["networkError"]["category"] == "rate limited" and detail["networkError"]["stage"] == "search"
    keywords = [path for path in site.log if path.startswith("/s.php")]
    assert len(keywords) == len(set(keywords))  # no request is repeated for a retry


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_a_bare_throttle_phrase_without_a_blocking_page_stays_a_rule_failure(monkeypatch, profile: str) -> None:
    """App BookSourceEngine.swift:867-876: keyword alone (biquge365 `搜索间隔15秒`) is not enough, the page must also be a blocking page."""
    monkeypatch.setattr(vsp, "fetch_text", PrefixSite({"/s.php": THROTTLE_PAGE}))
    with app_profiles.app_profile_scope(profile):
        passed, _seed, detail = vsp.quick_scan_single_source(_search_source())
    assert not passed and detail["failureReason"] == "no search/explore result" and "networkError" not in detail


def test_the_rate_limit_verdict_is_decided_per_keyword(monkeypatch) -> None:
    """The first keyword hits the throttle page, the next one an ordinary empty page: the last request decides (no stale flag)."""
    calls: list[str] = []

    def fetch(session, url, **kwargs):
        calls.append(url)
        return (BLOCKED_THROTTLE_PAGE if len(calls) == 1 else "<p>没有结果</p>"), url, 200

    monkeypatch.setattr(vsp, "fetch_text", fetch)
    src = _search_source()
    runtime = vsp.make_runtime(src)
    book_urls, _base = vsp.fetch_book_candidates(requests.Session(), src, runtime, keywords=["a", "b"], modes=("search",))
    assert book_urls == [] and len(calls) == 2
    assert (runtime.variables or {}).get("__searchRateLimited") != "1"
    detail: dict[str, Any] = {}
    assert vsp.attach_rate_limited_error(detail, runtime, "search") is False


def test_a_json_search_body_is_never_a_rate_limit_page(monkeypatch) -> None:
    body = '{"msg": "404 not found 请求过于频繁"}'
    monkeypatch.setattr(vsp, "fetch_text", PrefixSite({"/s.php": body}))
    passed, _seed, detail = vsp.quick_scan_single_source(_search_source())
    assert not passed and detail["failureReason"] != "network error: rate limited"


def test_an_empty_search_without_a_throttle_notice_stays_a_rule_failure(monkeypatch) -> None:
    monkeypatch.setattr(vsp, "fetch_text", PrefixSite({"/s.php": "<p>没有结果</p>"}))
    passed, _seed, detail = vsp.quick_scan_single_source(_search_source())
    assert not passed and detail["failureReason"] == "no search/explore result" and "networkError" not in detail


def test_a_search_with_results_is_not_affected_by_a_throttle_word_in_the_page(monkeypatch) -> None:
    page = "<div class='b'><a href='/book/1'>书</a></div><p>请求过于频繁将被限制</p>"
    monkeypatch.setattr(vsp, "fetch_text", PrefixSite({"/s.php": page, "/book/1": "<p>x</p>"}))
    passed, _seed, detail = vsp.quick_scan_single_source(_search_source())
    assert detail.get("failureReason") != "network error: rate limited"
    assert detail["quickScan"]["searchEntry"] is True


# ------------------------------------------------------------------------------------------------ czbooks: java.t2s / s2t


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_t2s_and_s2t_convert_character_by_character() -> None:
    runtime = _runtime()
    assert vsp.evaluate_js("java.t2s('劍來與龍門，abc 123')", "", runtime) == "剑来与龙门，abc 123"
    assert vsp.evaluate_js("java.s2t('剑来与龙门')", "", runtime) == "劍來與龍門"
    assert vsp.evaluate_js("java.t2s('搜尋')", "", runtime) == "搜尋"  # single-character mapping only (the old loop): no multi-character phrase table
    assert vsp.evaluate_js("java.t2s('')", "", runtime) == ""
    assert vsp.evaluate_js("java.t2s(null) + '|' + java.s2t(undefined)", "", runtime) == "|"


@needs_js
@pytest.mark.usefixtures("js_engine")
@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_t2s_and_s2t_output_is_identical_to_the_old_per_character_loop(profile: str) -> None:
    """The single-pass speed-up must not change a byte: compare with the previous loop implementation on every key of both maps plus noise."""
    script = (
        "function oldConvert(value, map) { var text = String(value === undefined || value === null ? '' : value); var out = '';"
        " for (var i = 0; i < text.length; i++) { var ch = text.charAt(i); out += Object.prototype.hasOwnProperty.call(map, ch) ? map[ch] : ch; }"
        " return out; }"
        "var sample = ''; [__readoriT2SMap, __readoriS2TMap].forEach(function(m) { Object.keys(m).forEach(function(k) { sample += k + m[k] + 'a1 '; }); });"
        "sample += String.fromCharCode(0x3000, 10, 0xd83d, 0xde00, 0x3400, 0x9fff, 0xf900, 0xfaff) + '<p>x</p>';"
        "[java.t2s(sample) === oldConvert(sample, __readoriT2SMap), java.s2t(sample) === oldConvert(sample, __readoriS2TMap)].join('|')"
    )
    with app_profiles.app_profile_scope(profile):
        assert vsp.evaluate_js(script, "", _runtime()) == "true|true"


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_t2s_of_a_large_page_finishes_well_inside_the_script_time_limit() -> None:
    """czbooks: 262 KB took ~4 s with the per-character `out +=` loop (over QuickJS's 2 s limit, the interrupted script came back as "")."""
    text = ("劍來與龍門 abc <p>書閱雲</p>\n" * 12000)
    runtime = _runtime()
    started = time.monotonic()
    out = vsp.evaluate_js("java.t2s(result).length + ':' + java.t2s(result).slice(0, 5)", text, runtime)
    assert out.endswith(":剑来与龙门") and int(out.split(":")[0]) == len(text)
    assert time.monotonic() - started < 3.0


# ------------------------------------------------------------------------------------------------ xs8 toc gaps


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_removing_markup_leaves_the_surrounding_whitespace_untouched() -> None:
    """No post-remove whitespace collapse (the App has none): the gaps around the removed element stay as they were."""
    page = "<div><p>keep</p>" + " " * 50 + "<span>drop</span>" + " " * 50 + "<i>keep2</i></div>"
    script = "var d = org.jsoup.Jsoup.parse(%s); d.select('span').remove(); d.html()" % json.dumps(page)
    out = vsp.evaluate_js(script, "", _runtime())
    assert "<span>" not in out and "<p>keep</p>" in out and "<i>keep2</i>" in out
    assert " " * 100 in out


@pytest.mark.parametrize("selector,expected", [
    ("a[href~=chapter[/_-]?list]", ["/chapterlist/1"]),
    ("a[href~=^/chapter[a-z]+/\\d+$]", ["/chapterlist/1"]),
    ('a[href~="chapter[/_-]?list"]', ["/chapterlist/1"]),
    ("a[href~=(foo|bar)]", ["/foo"]),
])
def test_regex_attribute_value_with_nested_brackets_is_taken_whole(selector: str, expected: list[str]) -> None:
    html = '<a href="/chapterlist/1">x</a><a href="/other/2">y</a><a href="/foo">z</a>'
    soup = BeautifulSoup(html, "html.parser")
    normalized = vsp.normalize_selector(selector)
    rewritten, marked = vsp._jsoup_regex_attr_prepare(soup, normalized)
    try:
        assert [node["href"] for node in soup.select(rewritten)] == expected
    finally:
        vsp._jsoup_regex_attr_cleanup(marked)


def test_regex_attribute_clause_scan_handles_several_clauses_and_unbalanced_text() -> None:
    seen: list[tuple[str, str, str]] = []
    out = vsp._jsoup_sub_regex_attr_clauses(
        "a[href~=x[a-z]][title~='q]r'] b[c~=d", lambda name, raw, original: seen.append((name, raw, original)) or "[m]"
    )
    assert seen == [("href", "x[a-z]", "[href~=x[a-z]]"), ("title", "'q]r'", "[title~='q]r']")]
    assert out == "a[m][m] b[c~=d"  # an unbalanced clause is left to the selector engine


@pytest.mark.parametrize("selector,expected", [
    ("div:has(div + div)", "div:has(:is(div + div))"),
    ("a:has(ul li, p.x) b", "a:has(:is(ul li), p.x) b"),
    ("div:has(> a)", "div:has(> a)"),
    ("x:has(a:not(.b))", "x:has(a:not(.b))"),
    ("li:has(span:has(i b))", "li:has(span:has(:is(i b)))"),
    ("p:has(a[title='a b'])", "p:has(a[title='a b'])"),
])
def test_complex_has_arguments_are_wrapped_for_soupsieve(selector: str, expected: str) -> None:
    assert vsp.normalize_selector(selector) == expected


def test_a_complex_has_selector_selects_like_jsoup() -> None:
    soup = BeautifulSoup("<div id=a><div>1</div><div>2</div></div><div id=b><p>x</p></div>", "html.parser")
    assert [node["id"] for node in soup.select(vsp.normalize_selector("div:has(div + div)"))] == ["a"]


@needs_js
@pytest.mark.usefixtures("js_engine")
def test_the_parent_link_of_a_jsoup_element_is_not_enumerable() -> None:
    script = (
        "var d = org.jsoup.Jsoup.parse('<ul><li>a</li><li>b</li></ul>'); var l = d.select('li'); var e = l.get(0);"
        "Object.keys(e).filter(function(k) { return typeof e[k] !== 'function'; }).join(',')"
        " + '|' + (JSON.stringify(e).indexOf('_parent') >= 0) + '|' + (e._parent === d)"
    )
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(script, "", _runtime()) == "_html,_innerHtml|false|true"  # still reachable for remove(), hidden from enumeration / JSON
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js(script, "", _runtime()) == "_html,_innerHtml,_parent|true|true"  # 1.1.1 output is unchanged
