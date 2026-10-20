"""R157: validator emulation gaps found by the remaining Legado-only rule rows (offline; patched fetches and synthetic pages only).

Validator-only fixes (both App profiles): searchExplore G1 (`<js>` segments + `||` precedence), G2 (`java.getString(rule, bool)`),
G3 (`sel[a:b]` list range), G4 (crypto routing incl. jsLib / source comment, randomUUID, `java.put` list, JSON Content-Type,
HTML4 unescape), G5 (explore search-rule fallback pre-check); tocContent A, D, E, F, G, K, C; js G2 / G3 (replay + frozen clock),
G7 (`data:` carrier), baidu (raw bytes + `bodyBase64`).
Profile 1.1.2 only (mirrors the Swift package E1): H, B, J, Jsoup shim extras, list helpers, getElements JSON objects,
`##` on JS rules, empty volume body; plus I (no static interaction pre-verdict).
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

import pytest
import requests

from validator import app_profiles
from validator import validate_source_packages as vsp

BASE = "https://example.test"
needs_quickjs = pytest.mark.skipif(vsp.quickjs is None, reason="QuickJS bridge only")
needs_node = pytest.mark.skipif(not vsp.node_binary_path(), reason="node is not available")


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


def _runtime(src: dict[str, Any] | None = None) -> vsp.RuleRuntime:
    return vsp.make_runtime(src or {"bookSourceName": "R157", "bookSourceUrl": BASE})


def _plain(items: list[str]) -> list[str]:
    return [re.sub(r"<[^>]+>", "", item).strip() for item in items]


# ------------------------------------------------------------------------------------------------ profile flags

NEW_FLAGS = (
    "search_item_fields_visible_to_book_url",
    "chapter_put_reaches_content",
    "js_chapter_url_empty_no_literal_fallback",
    "jsoup_shim_extras",
    "js_list_helpers_non_enumerable",
    "js_get_elements_json_objects",
    "js_rule_replace_suffix",
    "validation_content_sample_skips_volumes",
    "interaction_runtime_verdict_only",
)


@pytest.mark.parametrize("flag", NEW_FLAGS)
def test_new_profile_flags_are_off_for_1_1_1_and_on_for_1_1_2(flag: str) -> None:
    with app_profiles.app_profile_scope("1.1.1"):
        assert getattr(app_profiles.current_app_profile(), flag) is False
    with app_profiles.app_profile_scope("1.1.2"):
        assert getattr(app_profiles.current_app_profile(), flag) is True


# --------------------------------------------------------------------------- searchExplore G4: crypto engine routing


def test_g4_a_jslib_helper_that_uses_crypto_routes_the_script_to_node() -> None:
    runtime = _runtime()
    runtime.js_lib = "function encode(s) { return java.createSymmetricCrypto('AES/CBC/PKCS5Padding', 'k', 'iv').encryptBase64(s); }"
    assert vsp.js_requires_node_crypto("encode('x')") is False
    assert vsp._js_lib_crypto_needed("var b = encode('x'); b", runtime) is True
    assert vsp._js_lib_crypto_needed("var b = other('x'); b", runtime) is False  # not a jsLib helper
    assert vsp._js_lib_crypto_needed("obj.encode('x')", runtime) is False  # a method with the same name is not the helper
    assert vsp.js_needs_node_crypto_engine("encode('x')", runtime) is True
    runtime.js_lib = "function plain(s) { return s + '!'; }"
    assert vsp.js_needs_node_crypto_engine("plain('x')", runtime) is False


def test_g4_eval_of_the_source_comment_takes_the_crypto_markers_of_the_comment() -> None:
    src = {
        "bookSourceName": "bookld-like",
        "bookSourceUrl": BASE,
        "bookSourceComment": "md5 = String(java.md5Encode(jm));",
    }
    runtime = _runtime(src)
    code = "eval(String(source.bookSourceComment));"
    assert vsp.js_requires_node_crypto(code) is False
    assert vsp.js_needs_node_crypto_engine(code, runtime) is True
    assert vsp.js_needs_node_crypto_engine("var a = 1;", runtime) is False  # the comment is not read by this script
    plain_comment = _runtime({**src, "bookSourceComment": "just notes"})
    assert vsp.js_needs_node_crypto_engine(code, plain_comment) is False


# ------------------------------------------------------------------------ searchExplore G4: default Content-Type


class _Resp:
    def __init__(self) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {"Content-Type": "text/plain; charset=utf-8"}
        self.content = b"ok"
        self.encoding = "utf-8"
        self.url = BASE + "/p"
        self.text = "ok"
        self.cookies = requests.cookies.RequestsCookieJar()


def _post_headers(monkeypatch, body: str, headers: dict[str, str] | None = None, charset: str | None = None) -> tuple[dict, Any]:
    seen: dict[str, Any] = {}

    def fake_post(self, url, headers=None, data=None, **kwargs):
        seen["headers"] = dict(headers or {})
        seen["data"] = data
        return _Resp()

    monkeypatch.setattr(requests.Session, "post", fake_post)
    vsp.fetch_text(requests.Session(), BASE + "/p", method="POST", headers=headers, body=body, charset=charset)
    return seen["headers"], seen["data"]


def test_g4_a_json_body_without_content_type_is_posted_as_json(monkeypatch) -> None:
    headers, data = _post_headers(monkeypatch, '{"a": "中"}', charset="gbk")
    assert headers["Content-Type"] == "application/json; charset=UTF-8"
    assert data == '{"a": "中"}'.encode("utf-8")  # structured bodies are UTF-8 bytes (App NetworkManager postJson)


def test_g4_xml_and_array_bodies_are_structured_too_and_form_bodies_stay_forms(monkeypatch) -> None:
    assert _post_headers(monkeypatch, "<a>1</a>")[0]["Content-Type"] == "application/json; charset=UTF-8"
    assert _post_headers(monkeypatch, "[1, 2]")[0]["Content-Type"] == "application/json; charset=UTF-8"
    headers, data = _post_headers(monkeypatch, "a=1&b=2", charset="gbk")
    assert headers["Content-Type"] == "application/x-www-form-urlencoded; charset=gbk"
    assert data == b"a=1&b=2"


def test_g4_an_explicit_content_type_is_never_replaced(monkeypatch) -> None:
    headers, _data = _post_headers(monkeypatch, '{"a": 1}', headers={"content-type": "text/plain"})
    assert headers == {"content-type": "text/plain"}


# ---------------------------------------------------------------- tocContent K: form encoding for detail / toc / content


def test_k_a_post_form_body_is_field_encoded_for_detail_toc_and_content_urls() -> None:
    raw = 'https://example.test/api,{"method":"POST","body":"secret=a+b/c=&n=1"}'
    url, opts = vsp.resolve_url_with_options(raw, BASE, {})
    assert url == "https://example.test/api"
    assert opts["body"] == "secret=a%2Bb%2Fc%3D&n=1"  # Legado AnalyzeUrl.encodeParams: `+ / =` of a base64 value
    # without the header set (older callers) the body is untouched
    assert vsp.resolve_url_with_options(raw, BASE)[1]["body"] == "secret=a+b/c=&n=1"


def test_k_json_bodies_and_explicit_content_types_are_not_encoded() -> None:
    json_raw = 'https://example.test/api,{"method":"POST","body":"{\\"k\\":\\"a+b\\"}"}'
    assert vsp.resolve_url_with_options(json_raw, BASE, {})[1]["body"] == '{"k":"a+b"}'
    form_raw = 'https://example.test/api,{"method":"POST","body":"s=a+b"}'
    assert vsp.resolve_url_with_options(form_raw, BASE, {"Content-Type": "application/x-www-form-urlencoded"})[1]["body"] == "s=a+b"
    with_option_header = 'https://example.test/api,{"method":"POST","body":"s=a+b","headers":{"Content-Type":"text/plain"}}'
    assert vsp.resolve_url_with_options(with_option_header, BASE, {})[1]["body"] == "s=a+b"
    get_raw = 'https://example.test/api,{"body":"s=a+b"}'
    assert vsp.resolve_url_with_options(get_raw, BASE, {})[1]["body"] == "s=a+b"  # only POST bodies


# ----------------------------------------------------------------------- js G7 / tocContent D: `data:` request carrier


def test_g7_a_typed_data_carrier_url_is_plausible_and_keeps_its_options() -> None:
    carrier = 'data:;base64,eyJhIjoxfQ==,{"type":"mw"}'
    resolved = vsp.resolve_plausible_url_preserving_options(carrier, BASE, same_host_as="https://other.test/")
    assert resolved.startswith("data:;base64,eyJhIjoxfQ==")
    assert '"type":"mw"' in resolved
    # an ordinary non-http asset URL is still rejected
    assert vsp.resolve_plausible_url_preserving_options("javascript:void(0)", BASE) == ""


# --------------------------------------------------------------------------------- tocContent A: unescapeHtml4 semantics


def test_a_unescape_html4_needs_the_semicolon_and_only_knows_html4_names() -> None:
    assert vsp.legado_unescape_html4("a=1&timestamp=2&times=3") == "a=1&timestamp=2&times=3"
    assert vsp.legado_unescape_html4("1&times;2 &amp; &lt;b&gt; &quot;q&quot;") == '1×2 & <b> "q"'
    assert vsp.legado_unescape_html4("&#65;&#x42;&#X43;") == "ABC"
    assert vsp.legado_unescape_html4("&nbsp;&copy;&hellip;") == " ©…"
    assert vsp.legado_unescape_html4("&apos;&notanentity;&#99999999;") == "&apos;&notanentity;&#99999999;"  # not HTML 4 / out of range
    assert vsp.legado_get_string_unescape("x&sect=1") == "x&sect=1"
    assert vsp.legado_get_string_value(["a&para=1", "b&amp;c"]) == "a&para=1\nb&c"


def test_a_get_string_fields_do_not_decode_ampersand_names_in_urls() -> None:
    html = '<a href="/d?id=1&timestamp=2">x</a>'
    assert vsp.evaluate_get_string("a@href", html, _runtime(), "html") == "/d?id=1&timestamp=2"


# ------------------------------------------------------------------------------------------ tocContent G / F: rule forms


def test_g_xpath_double_slash_attribute_includes_the_element_itself() -> None:
    html = '<div><h2><a href="/x">x</a></h2><h2><a href="/y"><b>y</b></a></h2></div>'
    values = vsp.evaluate_rule("//h2/a//@href", html, _runtime(), "html")
    # `a//@href` = `a/descendant-or-self::node()/@href`: the `<a>` itself carries the href (the old `a//*` skipped it)
    assert [v.replace(BASE, "") for v in values] == ["/x", "/y"]


def test_f_a_rule_made_only_of_json_placeholders_is_a_template_result() -> None:
    item = json.dumps({"id": 5, "slug": "ab"})
    assert vsp.evaluate_rule("{{$.id}}", item, _runtime(), "json") == ["5"]
    assert vsp.evaluate_rule("/c/{{$.slug}}/{{$.id}}.html", item, _runtime(), "json") == ["/c/ab/5.html"]
    # a plain JSONPath is unaffected
    assert vsp.evaluate_rule("$.id", item, _runtime(), "json") == ["5"]


# ============================================================================================ searchExplore G1 / G3 lists


@needs_quickjs
def test_g1_a_js_segment_result_is_one_content_for_the_next_selector_not_one_item_per_line() -> None:
    """quanben: `<js>..</js>\n.book` - the JS returns the whole (multi-line) page; the selector runs on that one string."""
    html = "<div>page</div>"
    rule = "<js>'<div class=\"book\">a</div>\\n<div class=\"book\">b</div>\\n<div class=\"book\">c</div>'</js>\n.book"
    assert _plain(vsp.evaluate_book_list(rule, html, _runtime())) == ["a", "b", "c"]


@needs_quickjs
def test_g1_an_or_operator_inside_the_js_code_is_not_the_rule_operator() -> None:
    html = '<div class="book">only</div>'
    rule = "<js>\nvar x = 0 || 1;\nresult\n</js>\n.book"
    assert _plain(vsp.evaluate_book_list(rule, html, _runtime())) == ["only"]
    rule2 = "<js>var a = true && false; result</js>\n.book"
    assert _plain(vsp.evaluate_book_list(rule2, html, _runtime())) == ["only"]


@needs_quickjs
def test_g1_or_precedence_is_inside_the_selector_segment_and_the_js_tail_sees_the_whole_list() -> None:
    """jjwxc channel: `A||B\n<js>..</js>` - `||` picks the first non-empty branch, then ONE js over that list."""
    html = '<ul><li class="b">1</li><li class="b">2</li></ul>'
    rule = ".nope||.b\n<js>result.toArray().map(function (e) { return '<i>' + e.text() + '</i>'; })</js>"
    with app_profiles.app_profile_scope("1.1.2"):
        items = vsp.evaluate_book_list(rule, html, _runtime())
    assert _plain(items) == ["1", "2"]


@needs_quickjs
def test_g1_selector_js_selector_chain_runs_each_stage_on_the_previous_stage_result() -> None:
    """ndlib: JS -> `a||b` rule -> JS."""
    html = "<x/>"
    rule = (
        "<js>JSON.stringify({a: {items: [1, 2, 3]}})</js>\n$.b.items[*]||$.a.items[*]\n"
        "<js>result.toArray().map(function (v) { return 'n' + v; })</js>"
    )
    with app_profiles.app_profile_scope("1.1.2"):
        items = vsp.evaluate_book_list(rule, html, _runtime())
    assert [str(i) for i in items] == ["n1", "n2", "n3"]


@needs_quickjs
def test_g1_a_trailing_js_segment_runs_once_on_the_whole_list_only_on_1_1_2() -> None:
    """App 1.1.1 RuleType `.combine` evaluates `selector<js>` per item (`result` = the item string); Legado / 1.1.2 once on the list."""
    html = '<ul><li class="b">aa</li><li class="b">bbb</li></ul>'
    rule = ".b<js>String(result.length)</js>"
    with app_profiles.app_profile_scope("1.1.2"):
        # `result` is the list (2 items): ONE script run -> one string -> one item
        assert vsp.evaluate_book_list(rule, html, _runtime()) == ["2"]
    with app_profiles.app_profile_scope("1.1.1"):
        # per item: `result` is the item's outer HTML string, so there is one result per list item
        per_item = vsp.evaluate_book_list(rule, html, _runtime())
        assert per_item == [str(len('<li class="b">aa</li>')), str(len('<li class="b">bbb</li>'))]


@needs_quickjs
def test_g1_a_js_first_rule_keeps_its_or_precedence_on_both_profiles() -> None:
    html = '<div class="book">only</div>'
    rule = "<js>var x = 0 || 1; result</js>\n.book"
    for profile in ("1.1.1", "1.1.2"):
        with app_profiles.app_profile_scope(profile):
            assert _plain(vsp.evaluate_book_list(rule, html, _runtime())) == ["only"]


def test_g3_bare_selector_range_index_is_applied_to_the_list() -> None:
    html = "<ul>" + "".join(f'<li><h2>h{i}</h2><a href="/a{i}">a{i}</a></li>' for i in range(12)) + "</ul>"
    rt = _runtime()
    assert _plain(vsp.evaluate_book_list("ul li a[8:]", html, rt)) == ["a8", "a9", "a10", "a11"]
    assert _plain(vsp.evaluate_book_list("ul li a[1:3]", html, rt)) == ["a1", "a2", "a3"]
    assert _plain(vsp.evaluate_book_list("ul li a[-2:]", html, rt)) == ["a10", "a11"]
    assert _plain(vsp.evaluate_book_list("ul li a[0,2]", html, rt)) == ["a0", "a2"]
    assert _plain(vsp.evaluate_book_list("ul li a[!0:9]", html, rt)) == ["a10", "a11"]
    assert _plain(vsp.evaluate_book_list(".tb[1:]", '<div class="tb">x</div><div class="tb">y</div>', rt)) == ["y"]
    # `||` keeps the first non-empty branch, whose own range applies (zhonghuashici)
    assert len(vsp.evaluate_book_list("ul li h2||ul li a[8:]", html, rt)) == 12
    assert _plain(vsp.evaluate_book_list("ul li h3||ul li a[8:]", html, rt)) == ["a8", "a9", "a10", "a11"]


def test_g3_attribute_selectors_with_brackets_are_not_taken_for_index_ranges() -> None:
    html = '<a href="/x" class="k">x</a><a href="/y">y</a>'
    assert len(vsp.evaluate_book_list("a[href]", html, _runtime())) == 2


# ======================================================================================================= JS shim (G2 / G4)


@needs_quickjs
def test_g2_get_string_second_boolean_argument_is_unescape_not_content() -> None:
    """jjwxc: `java.getString(rule, true|false)` is AnalyzeRule.getString(ruleStr, unescape) (AnalyzeRule.kt:256)."""
    content = '{"a": "x&amp;y"}'
    rt = _runtime()
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js("java.getString('$.a', true)", content, rt) == "x&y"
        assert vsp.evaluate_js("java.getString('$.a', false)", content, rt) == "x&amp;y"
        assert vsp.evaluate_js("java.getString('$.a')", content, rt) == "x&y"
    # an explicit content argument still wins (both profiles)
    assert vsp.evaluate_js("java.getString('$.b', '{\"b\": \"z\"}')", content, rt) == "z"


@needs_quickjs
def test_g4_random_uuid_is_a_plain_string_and_put_stores_a_list_as_text() -> None:
    rt = _runtime()
    assert vsp.evaluate_js("typeof java.randomUUID() + ':' + java.randomUUID().replace(/-/g, '').length", "", rt) == "string:32"
    assert vsp.evaluate_js("java.put('k', ['a', 'b']); java.get('k')", "", rt) == "a,b"
    assert vsp.evaluate_js("java.put('n', 5); java.get('n')", "", rt) == "5"
    assert vsp.evaluate_js("java.put('s', 'x')", "", rt) == "x"  # returns the stored value like Legado


# ======================================================================================== searchExplore G5: explore fallback


def test_g5_a_blank_explore_book_list_falls_back_to_the_search_rule_in_the_pre_check() -> None:
    src = {"bookSourceUrl": BASE, "exploreUrl": "/list", "ruleSearch": {"bookList": ".b"}, "ruleExplore": {"bookList": ""}}
    assert vsp.has_effective_explore_book_list(src) is True
    assert vsp.has_effective_explore_book_list({**src, "ruleExplore": {"bookList": ".e"}, "ruleSearch": {}}) is True
    assert vsp.has_effective_explore_book_list({**src, "ruleExplore": {}, "ruleSearch": {"bookList": " "}}) is False
    assert vsp.has_effective_explore_book_list({"bookSourceUrl": BASE}) is False


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_g5_a_source_with_only_an_explore_url_and_a_search_book_list_is_not_rejected_as_missing_rules(monkeypatch, profile) -> None:
    monkeypatch.setattr(vsp, "fetch_text", Site({}))
    src = {
        "bookSourceName": "vista",
        "bookSourceUrl": BASE,
        "enabled": True,
        "exploreUrl": "/list",
        "ruleSearch": {"bookList": ".b", "name": "a@text", "bookUrl": "a@href"},
    }
    with app_profiles.app_profile_scope(profile):
        passed, _seed, detail = vsp.quick_scan_single_source(src)
    assert detail.get("failureReason") != "missing search/explore rule"


# ============================================================================= 1.1.2 JS shim: Jsoup extras / lists / getElements / `##`


@needs_quickjs
def test_jsoup_shim_extras_exist_only_on_1_1_2() -> None:
    code = (
        "var d = org.jsoup.Jsoup.parse('<div class=\"a b\"><p class=\"z\">x</p></div>');"
        "[typeof d.select('div').hasClass, typeof d.select('p').first, typeof d.select('p').parents].join()"
    )
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js(code, "", _runtime()) == "undefined,undefined,undefined"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(code, "", _runtime()) == "function,function,function"


@needs_quickjs
def test_jsoup_shim_extras_semantics_on_1_1_2() -> None:
    page = '<div class="a b"><p class="z">x</p><p>y</p></div>'
    with app_profiles.app_profile_scope("1.1.2"):
        rt = _runtime()
        js = "var d = org.jsoup.Jsoup.parse(%s);" % json.dumps(page)
        assert vsp.evaluate_js(js + "d.select('div').hasClass('a') + ':' + d.select('div').hasClass('q')", "", rt) == "true:false"
        assert vsp.evaluate_js(js + "d.select('p').first().text() + d.select('p').last().text()", "", rt) == "xy"
        assert vsp.evaluate_js(js + "String(d.select('nothing').first())", "", rt) == "null"
        assert vsp.evaluate_js(js + "d.select('p.z').first().parents().size() > 0", "", rt) == "true"
        assert vsp.evaluate_js(js + "d.select('p').contains(d.select('p').first())", "", rt) == "true"
        assert vsp.evaluate_js("typeof Packages.org.jsoup.Jsoup.parse + typeof org.jsoup.Jsoup.connect", "", rt) == "functionfunction"


@needs_quickjs
def test_list_helpers_are_not_enumerated_by_for_in_only_on_1_1_2() -> None:
    code = "var k = []; var arr = java.getElements('li'); for (var i in arr) k.push(i); k.join()"
    content = "<ul><li>1</li><li>2</li></ul>"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(code, content, _runtime()) == "0,1"
    with app_profiles.app_profile_scope("1.1.1"):
        assert "toArray" in vsp.evaluate_js(code, content, _runtime()).split(",")


@needs_quickjs
def test_get_elements_in_json_mode_returns_objects_only_on_1_1_2() -> None:
    code = "var r = java.getElements('$.a[*]'); r.length + ':' + typeof r[0] + ':' + (r[0] && r[0].id)"
    content = '{"a":[{"id":1},{"id":2}]}'
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(code, content, _runtime()) == "2:object:1"
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js(code, content, _runtime()).startswith("0:")


@needs_quickjs
def test_a_rule_replace_suffix_after_a_js_segment_applies_to_the_script_result_only_on_1_1_2() -> None:
    rt = _runtime()
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp._evaluate_legado_js_segment("'a、b'##、##,", False, "", rt, True) == ["a,b"]
        assert vsp._evaluate_legado_js_segment("'a、b'##、##,", True, "", rt, True) == ["a,b"]
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp._evaluate_legado_js_segment("'a、b'##、##,", False, "", rt, True) == []


def test_split_js_rule_replace_suffix_ignores_hashes_inside_literals_and_comments() -> None:
    split = vsp._split_js_rule_replace_suffix
    nl = chr(10)
    assert split("var a = 'a##b'; a##x##y") == ("var a = 'a##b'; a", "x##y")
    assert split("var a = `x##${b}`; a") is None
    assert split("/* ## */ a") is None
    assert split("var r = /##/.test(a); a // ## c" + nl + "##q##w") == ("var r = /##/.test(a); a // ## c", "q##w")
    assert split("a + b") is None
    assert split("##、##,") == ("", "、##,")


# ================================================================================================== tocContent H / B / J, volume, I


def test_h_search_item_kind_is_visible_to_the_book_url_rule_only_on_1_1_2() -> None:
    rule = {"kind": "span@text", "bookUrl": "a@href"}
    item = '<li><span>玄幻</span><a href="/b/1">x</a></li>'
    with app_profiles.app_profile_scope("1.1.2"):
        rt = _runtime()
        vsp.capture_search_item_rule_variables(rule, item, rt)
        assert rt.book_kind == "玄幻"
        assert "book.kind" not in (rt.variables or {})  # Legado's variable map has no such key
    with app_profiles.app_profile_scope("1.1.1"):
        rt = _runtime()
        vsp.capture_search_item_rule_variables(rule, item, rt)
        assert not rt.book_kind
        assert "book.kind" not in (rt.variables or {})


def _adopt_with_chapter_put() -> vsp.RuleRuntime:
    toc_runtime = _runtime()
    item_runtime = _runtime()
    item_runtime.variables = {"vid": "42", "__chapter.isVolume": False}
    target = _runtime()
    vsp._adopt_first_chapter_runtime(target, dict(toc_runtime.variables or {}), item_runtime, f"{BASE}/c/1", "第1章", 0, 1)
    return target


def test_b_chapter_put_variables_reach_the_content_runtime_only_on_1_1_2() -> None:
    with app_profiles.app_profile_scope("1.1.2"):
        variables = _adopt_with_chapter_put().variables or {}
        assert variables.get("vid") == "42"
        assert not any(str(key).startswith("__chapter.") for key in variables)
    with app_profiles.app_profile_scope("1.1.1"):
        assert "vid" not in (_adopt_with_chapter_put().variables or {})


def test_sfacg_an_empty_volume_body_passes_the_content_stage_only_on_1_1_2(monkeypatch) -> None:
    src = {"bookSourceName": "V", "bookSourceUrl": BASE, "ruleContent": {"content": "#c@text"}}
    monkeypatch.setattr(vsp, "fetch_text", Site({"/v/1": '<div id="c"></div>'}))

    def run(volume: bool) -> bool:
        rt = _runtime(src)
        rt.chapter_is_volume = volume
        return vsp.resolve_chapter_content(requests.Session(), src, rt, f"{BASE}/v/1", "第一卷", "书")[0]

    with app_profiles.app_profile_scope("1.1.2"):
        assert run(True) is True
        assert run(False) is False  # a normal chapter with an empty body still fails (BookContent.kt:149)
    with app_profiles.app_profile_scope("1.1.1"):
        assert run(True) is False


@pytest.mark.parametrize("profile,blocked", (("1.1.1", True), ("1.1.2", False)))
def test_i_no_static_interaction_verdict_from_rule_text_on_1_1_2(monkeypatch, profile: str, blocked: bool) -> None:
    monkeypatch.setattr(vsp, "fetch_text", Site({}))
    src = {
        "bookSourceName": "I",
        "bookSourceUrl": BASE,
        "enabled": True,
        "searchUrl": "/s?q={{key}}",
        "ruleSearch": {"bookList": ".b", "name": "a@text", "bookUrl": "a@href"},
        "ruleContent": {"content": "@js:if (!result) { java.getVerificationCode('x'); } result"},
    }
    with app_profiles.app_profile_scope(profile):
        _passed, _seed, detail = vsp.quick_scan_single_source(src)
    assert (detail.get("failureReason") == "interactive verification required") is blocked


# ============================================================================================ Node replay (js G3 / baidu)


def test_baidu_materialized_request_fixture_carries_the_raw_bytes_as_body_base64(monkeypatch) -> None:
    raw = bytes([0, 1, 2, 250, 251, 252])

    def fake_fetch(session, url, **kwargs):
        meta = kwargs.get("response_meta")
        if isinstance(meta, dict):
            meta["bodyBase64"] = base64.b64encode(raw).decode("ascii")
        return "garbled", url, 200

    monkeypatch.setattr(vsp, "fetch_text", fake_fetch)
    fixtures = vsp.materialize_js_request_log_fixtures([{"url": f"{BASE}/bin", "method": "GET"}], _runtime())
    assert fixtures[f"{BASE}/bin"]["bodyBase64"] == base64.b64encode(raw).decode("ascii")


def test_materialized_request_fixture_without_raw_bytes_has_no_body_base64(monkeypatch) -> None:
    monkeypatch.setattr(vsp, "fetch_text", lambda session, url, **kwargs: ("ok", url, 200))
    fixtures = vsp.materialize_js_request_log_fixtures([{"url": f"{BASE}/t", "method": "GET"}], _runtime())
    assert "bodyBase64" not in fixtures[f"{BASE}/t"]


@needs_node
def test_node_replay_freezes_the_clock_so_a_time_derived_url_is_stable() -> None:
    code = "var a = Date.now(); var b = new Date().getTime(); String(a === b)"
    rt = _runtime()
    assert vsp.evaluate_js_with_node(code, "", rt) == "true"


# ======================================================================================== js tadu / mingzw: result string methods, `title`


@needs_quickjs
@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_the_html_string_result_has_the_app_string_prototype_jsoup_methods(profile: str) -> None:
    """App JSEngine `String.prototype.select / text / attr / html / outerHtml / ownText` (both versions)."""
    item = '<a href="/c/1"><i class="vip">v</i>第1章</a>'
    with app_profiles.app_profile_scope(profile):
        rt = _runtime()
        assert vsp.evaluate_js("result.attr('href') + '|' + result.text() + '|' + result.select('i').text()", item, rt) == "/c/1|v 第1章|v"
        enumerated = vsp.evaluate_js("var s = []; for (var k in 'ab') s.push(k); s.join()", "", rt).split(",")
        assert not {"select", "text", "attr", "html", "outerHtml", "ownText"} & set(enumerated)


@needs_quickjs
def test_tadu_is_vip_rule_runs_hasclass_on_the_item_only_on_1_1_2() -> None:
    item = '<a href="/c/1"><i class="vip">v</i>第1章</a>'
    rule = "@js:result.select('i').hasClass('vip') "
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_rule(rule, item, _runtime(), "html") == ["true"]
    with app_profiles.app_profile_scope("1.1.1"):
        rt = _runtime()
        assert vsp.evaluate_rule(rule, item, rt, "html") == []
        assert "not a function" in str(rt.script_error)


@needs_quickjs
def test_mingzw_title_is_bound_in_a_chapter_context_like_legado_evaljs() -> None:
    rt = _runtime()
    rt.chapter_active = True
    rt.chapter_title = "第一章"
    assert vsp.evaluate_js("title", "", rt) == "第一章"
    assert vsp.evaluate_js("let title = 'z'; title", "", rt) == "z"  # still shadowable by the script


# ====================================================================================================== REV follow-ups


class PrefixSite:
    """Patched ``fetch_text`` answering by path prefix (first match); records every requested path."""

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


def _toc_walk(monkeypatch, body: str, rule: dict[str, Any], *, first_page_only: bool = False):
    src = {"bookSourceName": "REV", "bookSourceUrl": BASE, "ruleToc": rule}
    monkeypatch.setattr(vsp, "fetch_text", PrefixSite({"/toc": body}))
    return vsp.resolve_toc_source(
        requests.Session(), src, vsp.make_runtime(src), f"{BASE}/book", f"{BASE}/toc", first_page_only=first_page_only
    )


def test_p1_1_content_sample_dedups_the_first_16_chapters_by_url_only_on_1_1_2(monkeypatch) -> None:
    """App keepLastChapterPerURL == Legado BookChapterList.kt:119-128: of the sampled window only the LAST chapter per url stays, then
    the first remaining chapter is the sample. 1.1.1 takes chapters.first (xxsy: the placeholder row whose link rule was empty)."""
    rule = {"chapterList": "$.list[*]", "chapterName": "$.name", "chapterUrl": "$.url"}
    repeated = json.dumps({"list": [{"name": "序", "url": ""}, {"name": "第1章", "url": "/c/1"}, {"name": "尾", "url": ""}]})
    with app_profiles.app_profile_scope("1.1.2"):
        # the placeholder (baseUrl) repeats later in the window, so the first occurrence is dropped
        assert _toc_walk(monkeypatch, repeated, rule, first_page_only=True)[2] == f"{BASE}/c/1"
    with app_profiles.app_profile_scope("1.1.1"):
        assert _toc_walk(monkeypatch, repeated, rule, first_page_only=True)[2] == f"{BASE}/toc"
    single = json.dumps({"list": [{"name": "序", "url": ""}, {"name": "第1章", "url": "/c/1"}]})
    with app_profiles.app_profile_scope("1.1.2"):
        # a lone placeholder is not a duplicate: Legado / the App keep it and it is the first chapter
        assert _toc_walk(monkeypatch, single, rule, first_page_only=True)[2] == f"{BASE}/toc"
    dup_real = json.dumps({"list": [{"name": "A", "url": "/c/1"}, {"name": "B", "url": "/c/2"}, {"name": "A again", "url": "/c/1"}]})
    with app_profiles.app_profile_scope("1.1.2"):
        # keep-last: the first row's url repeats later, so the sample becomes the second row
        assert _toc_walk(monkeypatch, dup_real, rule, first_page_only=True)[2] == f"{BASE}/c/2"
    with app_profiles.app_profile_scope("1.1.1"):
        assert _toc_walk(monkeypatch, dup_real, rule, first_page_only=True)[2] == f"{BASE}/c/1"


def test_p2_3_a_js_chapter_url_that_returns_empty_does_not_fall_back_to_the_template_only_on_1_1_2(monkeypatch) -> None:
    """Swift `ruleContainsJavaScriptSegment` + blank result: `{{$.id}}@js:""` is an empty URL (-> toc page URL), not `1@js:""`."""
    body = json.dumps({"list": [{"name": "c1", "id": 1}]})
    for rule_text in ('{{$.id}}@js:""', '<js>""</js>{{$.id}}'):
        rule = {"chapterList": "$.list[*]", "chapterName": "$.name", "chapterUrl": rule_text}
        with app_profiles.app_profile_scope("1.1.2"):
            assert _toc_walk(monkeypatch, body, rule)[2] == f"{BASE}/toc"
        with app_profiles.app_profile_scope("1.1.1"):
            injected = _toc_walk(monkeypatch, body, rule)[2]
            assert injected != f"{BASE}/toc" and ("@js" in injected or "<js>" in injected)


def _author_source(explore: bool) -> dict[str, Any]:
    src: dict[str, Any] = {
        "bookSourceName": "A",
        "bookSourceUrl": BASE,
        "enabled": True,
        "searchUrl": "/s?q={{key}}",
        "ruleSearch": {"bookList": ".b", "name": "a@text", "author": ".au@text", "bookUrl": "a@href"},
        "ruleBookInfo": {"name": "h1@text"},
        "ruleToc": {"chapterList": ".toc li", "chapterName": "a@text", "chapterUrl": "a@href@js:result + '?w=' + book.author"},
        "ruleContent": {"content": "#c@text"},
    }
    if explore:
        src["exploreUrl"] = "分类::/e"
        src["ruleExplore"] = {"bookList": ".b", "name": "a@text", "author": ".au@text", "bookUrl": "a@href"}
    return src


def _author_pages(book: str, author: str) -> dict[str, Any]:
    return {
        f"/book/{book}": '<h1>书名</h1><div class="toc"></div>',
        f"/toc/{book}": f'<ul class="toc"><li><a href="/c/{book}">第一章</a></li></ul>',
        f"/c/{book}": f'<div id="c">{LONG_TEXT_R157}</div>',
    }


LONG_TEXT_R157 = "这是一段足够长的正文内容用于验证。这是一段足够长的正文内容用于验证。"


def test_p2_4_mowan_the_search_author_survives_a_detail_page_without_author(monkeypatch) -> None:
    """Legado BookInfo.kt:76-78 keeps the search author unless the detail author is non-empty (mowan)."""
    src = _author_source(explore=False)
    src["ruleBookInfo"] = {"name": "h1@text", "tocUrl": "/toc/1"}
    pages = {
        "/s?": '<div class="b"><a href="/book/1">书名</a><span class="au">搜索作者</span></div>',
        **_author_pages("1", "搜索作者"),
    }
    site = PrefixSite(pages)
    monkeypatch.setattr(vsp, "fetch_text", site)
    outcome, _record, detail = vsp.validate_single_source(src)
    assert detail["detailBookAuthor"] == "搜索作者"
    assert any(path.startswith("/c/1?w=") and "搜索作者" in requests.utils.unquote(path) for path in site.log), site.log


def test_p2_4_explore_chain_keeps_the_explore_author_for_the_chapter_stage(monkeypatch) -> None:
    src = _author_source(explore=True)
    src["ruleBookInfo"] = {"name": "h1@text", "tocUrl": "@js:baseUrl.replace('/book/', '/toc/')"}
    pages = {
        "/s?": '<div class="b"><a href="/book/1">书名</a><span class="au">搜索作者</span></div>',
        "/e": '<div class="b"><a href="/book/9">发现书</a><span class="au">发现作者</span></div>',
        "/book/9": '<h1>发现书</h1>',
        "/toc/9": '<ul class="toc"><li><a href="/c/9">第一章</a></li></ul>',
        "/c/9": f'<div id="c">{LONG_TEXT_R157}</div>',
        **_author_pages("1", "搜索作者"),
    }
    site = PrefixSite(pages)
    monkeypatch.setattr(vsp, "fetch_text", site)
    vsp.validate_single_source(src)
    assert any(path.startswith("/c/9?w=") and "发现作者" in requests.utils.unquote(path) for path in site.log), site.log


@needs_quickjs
@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_p2_4_jsoup_connect_body_as_bytes_on_a_plain_get_is_the_raw_payload(monkeypatch, profile: str) -> None:
    raw = bytes([0, 1, 200, 255])
    monkeypatch.setattr(vsp, "fetch_text", lambda session, url, **kwargs: ("lossy", url, 200))
    monkeypatch.setattr(vsp, "fetch_bytes", lambda session, url, headers=None, timeout=0: (raw, url, 200))
    with app_profiles.app_profile_scope(profile):
        out = vsp.evaluate_js(
            "org.jsoup.Jsoup.connect('" + BASE + "/bin').execute().bodyAsBytes().join(',')", "", _runtime()
        )
    assert out == "0,1,-56,-1"


def test_p2_4_xpath_double_slash_attribute_after_an_element_selects_every_attribute_node() -> None:
    """blxsw `//h2/a//@href`: Legado AnalyzeByXPath (JsoupXpath) evaluates `//@href` below the matched element; XPath 1.0 semantics
    `descendant-or-self::*/@href` - the element's own href is included."""
    html = '<div><h2><a href="/a1">x</a></h2><h2><a href="/a2">y</a></h2></div>'
    values = vsp.xpath_extract(html, "//h2/a//@href", "text", BASE)
    assert [v for v in values] == ["/a1", "/a2"] or [v for v in values] == [f"{BASE}/a1", f"{BASE}/a2"]


def test_p2_6_detail_stage_binds_intro_last_chapter_cover_before_the_toc_url_rule_only_on_1_1_2(monkeypatch) -> None:
    src = {
        "bookSourceName": "D",
        "bookSourceUrl": BASE,
        "ruleBookInfo": {
            "name": "h1@text",
            "intro": ".intro@text",
            "lastChapter": ".last@text",
            "coverUrl": "img@src",
            "tocUrl": "@js:'/toc?i=' + book.intro + '&l=' + book.lastChapter + '&c=' + book.coverUrl",
        },
        "ruleToc": {"chapterList": ".toc li", "chapterName": "a@text", "chapterUrl": "a@href"},
    }
    detail_page = '<h1>Book</h1><p class="intro">introX</p><p class="last">lastY</p><img src="/cover.png">'
    toc_urls = {}
    for profile in ("1.1.1", "1.1.2"):
        monkeypatch.setattr(vsp, "fetch_text", PrefixSite({"/book": detail_page}))
        with app_profiles.app_profile_scope(profile):
            _ok, _name, toc_url, _err = vsp._run_detail_stage(
                requests.Session(), src, vsp.make_runtime(src), {"stages": {}}, f"{BASE}/book", stage="detail", label="x"
            )
        toc_urls[profile] = toc_url
    assert "i=introX" in toc_urls["1.1.2"] and "l=lastY" in toc_urls["1.1.2"] and "c=" + BASE + "/cover.png" in toc_urls["1.1.2"]
    assert "introX" not in toc_urls["1.1.1"] and "lastY" not in toc_urls["1.1.1"]


@needs_quickjs
def test_get_string_unescape_flag_is_honoured_only_on_1_1_2() -> None:
    content = '{"a": "x&amp;y"}'
    code = "java.getString('$.a', false)"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(code, content, _runtime()) == "x&amp;y"
        assert vsp.evaluate_js("java.getString('$.a', true)", content, _runtime()) == "x&y"
    with app_profiles.app_profile_scope("1.1.1"):
        # the old App ignores the boolean (it is still not the content) and always unescapes
        assert vsp.evaluate_js(code, content, _runtime()) == "x&y"
        assert vsp.evaluate_js("java.getString('$.a', true)", content, _runtime()) == "x&y"


@needs_node
def test_get_string_unescape_flag_on_the_node_engine() -> None:
    content = '{"a": "x&amp;y"}'
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js_with_node("java.getString('$.a', false)", content, _runtime()) == "x&amp;y"
        assert vsp.evaluate_js_with_node("java.getString('$.a', true)", content, _runtime()) == "x&y"
        assert vsp.evaluate_js_with_node("java.getString('$.a')", content, _runtime()) == "x&y"
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js_with_node("java.getString('$.a', false)", content, _runtime()) == "x&y"


@needs_quickjs
def test_a_js_list_rule_cuts_the_replace_suffix_off_the_script_only_on_1_1_2() -> None:
    """App RuleEvaluator `.javascript` list case (:1826): `@js:code##regex##repl` runs `code` only."""
    rule = "@js:['<i>a</i>', '<i>b</i>']##x##y"
    with app_profiles.app_profile_scope("1.1.2"):
        assert _plain(vsp.evaluate_book_list(rule, "<x/>", _runtime())) == ["a", "b"]
    with app_profiles.app_profile_scope("1.1.1"):
        assert _plain(vsp.evaluate_book_list(rule, "<x/>", _runtime())) == []


@pytest.mark.parametrize(
    "code,expected",
    [
        # regex after `}` is division in Swift's rule (`}` blocks a regex literal), so the `##` inside is a cut
        ("x = {}" + chr(10) + "/##/.test(y)", ("x = {}" + chr(10) + "/", "/.test(y)")),
        ("return /##/.test(s)", None),
        ("var a = /##/g; a##r##s", ("var a = /##/g; a", "r##s")),
        ("var t = `${ '##' + x }##`; t", None),
        ("var t = `${ a }`; t##q##w", ("var t = `${ a }`; t", "q##w")),
        ("var s = 'unterminated" + chr(10) + "x##y##z", ("var s = 'unterminated" + chr(10) + "x", "y##z")),
        ("x = a /b/ c ## d", ("x = a /b/ c", " d")),
        ("typeof /##/", None),
        ("a = (1) /2/ 3##x##y", ("a = (1) /2/ 3", "x##y")),
    ],
)
def test_split_js_rule_replace_suffix_matches_the_swift_scanner(code: str, expected) -> None:
    assert vsp._split_js_rule_replace_suffix(code) == expected


def test_response_raw_bytes_only_for_non_text_or_non_utf8_payloads() -> None:
    needs = vsp._response_needs_raw_bytes
    assert needs("application/octet-stream", b"plain ascii") is True
    assert needs("text/html; charset=utf-8", bytes([0, 1, 200, 255])) is True
    assert needs("text/html; charset=utf-8", "正文".encode("utf-8")) is False
    assert needs("application/json", b"{}") is False
    assert needs("image/png", b"x") is True


@needs_node
def test_node_frozen_clock_date_call_form_and_replay_only() -> None:
    code = "var a = Date.now(); var b = new Date().getTime(); var c = Date(); String(a === b) + ':' + (typeof c) + ':' + (c === new Date(a).toString())"
    assert vsp.evaluate_js_with_node(code, "", _runtime()) == "true:string:true"


# ============================================================================== REV round 2 (E1 final behaviour)


@needs_quickjs
def test_list_js_tail_result_is_an_array_with_helpers_and_the_return_is_the_new_list_on_1_1_2() -> None:
    html = '<ul><li class="b"><a href="/x">aa</a></li><li class="b"><a href="/y">bb</a></li></ul>'
    helpers = ".b<js>[result.length, result.first().indexOf('aa') > 0, result.last().indexOf('bb') > 0, result.text(), result.attr('class'), result.select('a').size()].join('|')</js>"
    with app_profiles.app_profile_scope("1.1.2"):
        out = vsp.evaluate_book_list(helpers, html, _runtime())
    assert out == ['2|true|true|aa bb|b|2']
    with app_profiles.app_profile_scope("1.1.2"):
        # an empty-array return -> empty list; an exception in the tail -> empty list (no per-item fallback)
        assert vsp.evaluate_book_list(".b<js>[]</js>", html, _runtime()) == []
        assert vsp.evaluate_book_list(".b<js>throw new Error('x')</js>", html, _runtime()) == []
        # selector segments after the JS segment still run per item of the returned list
        rows = vsp.evaluate_book_list(".b<js>result.toArray()</js>a", html, _runtime())
        assert _plain(rows) == ["aa", "bb"]


@needs_quickjs
def test_list_js_tail_json_items_arrive_as_objects_on_1_1_2() -> None:
    body = '{"a": [{"n": "x"}, {"n": "y"}]}'
    rule = "$.a[*]<js>result.toArray().map(function (o) { return typeof o + ':' + o.n; })</js>"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_book_list(rule, body, _runtime()) == ["object:x", "object:y"]


@needs_quickjs
def test_list_js_tail_inline_sort_yields_both_anchors_on_both_profiles_in_profile_order() -> None:
    """scripts/tests `test_toc_chapter_list_inline_js_receives_jsoup_array`: `.g<js>…join("")</js>tag.a`."""
    html = (
        '<ul><li class="g" data-order="2"><a href="/chapter/2.html">Second</a></li>'
        '<li class="g" data-order="1"><a href="/chapter/1.html">First</a></li></ul>'
    )
    rule = (
        ".g\n<js>\nlist = result.toArray();\nl = list.sort((a,b)=>a.attr(\"data-order\")-b.attr(\"data-order\"));\n"
        "l.join(\"\")\n</js>\ntag.a"
    )
    with app_profiles.app_profile_scope("1.1.2"):
        items = vsp.evaluate_book_list(rule, html, _runtime())
        assert len(items) == 2 and "/chapter/1.html" in items[0] and "/chapter/2.html" in items[1]
    with app_profiles.app_profile_scope("1.1.1"):
        # per item (old App): both anchors, the list order is not sorted
        items = vsp.evaluate_book_list(rule, html, _runtime())
        assert len(items) == 2 and "/chapter/2.html" in items[0]


@needs_quickjs
def test_get_elements_json_mode_detection_follows_legado_source_rule_init_on_1_1_2() -> None:
    content = '{"a": [{"n": "x"}]}'
    with app_profiles.app_profile_scope("1.1.2"):
        rt = _runtime()
        assert vsp.evaluate_js("var r = java.getElements('$.a[*]', '" + content + "'); typeof r[0] + ':' + r[0].n", "", rt) == "object:x"
        # explicit CSS / `@@` / XPath / `/`-prefixed rules never are JSON mode, even on JSON content
        # a bare rule on JSON content is JSON mode (a CSS reading of `a` would find nothing)
        assert vsp.evaluate_js("var r = java.getElements('a', '" + content + "'); typeof r[0]", "", rt) == "object"
        for rule in ("@css:a", "@@a", "@xpath://a", "//a"):
            assert vsp.evaluate_js("var r = java.getElements(" + json.dumps(rule) + ", '" + content + "'); typeof r[0]", "", rt) != "object"
        # a scalar string that merely starts with `{` is not parsed outside JSON mode
        html = '<p>{"k": 1}</p>'
        assert vsp.evaluate_js("var r = java.getElements('p', " + json.dumps(html) + "); r[0].text()", "", rt) == '{"k": 1}'


@needs_quickjs
def test_jsoup_parents_is_empty_without_a_document_root_and_nearest_first_with_one() -> None:
    with app_profiles.app_profile_scope("1.1.2"):
        rt = _runtime()
        html = '<div id="o"><section id="m"><p id="p">x</p></section></div>'
        nearest = vsp.evaluate_js(
            "var d = org.jsoup.Jsoup.parse(" + json.dumps(html) + "); d.select('p').first().parents()[0].attr('id')", "", rt
        )
        assert nearest == "m"
        # a getElements item has no Document root in the shim: no ancestors
        assert vsp.evaluate_js("java.getElements('p', " + json.dumps(html) + ").first().parents().size()", "", rt) == "0"


def test_a_js_chapter_url_rule_with_a_template_never_takes_the_literal_branch_on_1_1_2(monkeypatch) -> None:
    """E1 final: a rule containing JS skips the whole literal template-expansion branch (not only on a blank result)."""
    body = json.dumps({"list": [{"name": "c1", "id": 1}]})
    rule = {"chapterList": "$.list[*]", "chapterName": "$.name", "chapterUrl": '{{$.id}}/q@js:"/p/" + "{{" + "$.id}}"'}
    with app_profiles.app_profile_scope("1.1.2"):
        assert _toc_walk(monkeypatch, body, rule)[2] == f"{BASE}/p/1"


@needs_quickjs
def test_list_js_tail_addendum_empty_list_runs_the_tail_and_empty_returns_are_empty_lists() -> None:
    html = '<ul><li class="b"><a href="/x">aa</a></li></ul>'
    with app_profiles.app_profile_scope("1.1.2"):
        # (a) the tail still runs when the previous segment found nothing (`result` is the empty list)
        assert vsp.evaluate_book_list(".nomatch<js>'size=' + result.length</js>", html, _runtime()) == ["size=0"]
        # (d) undefined / null / "" -> empty list (never the bound list); null / undefined array elements are skipped
        for body in ("undefined", "null", "''", "var z = 1;", "[null, undefined]"):
            assert vsp.evaluate_book_list(".b<js>" + body + "</js>", html, _runtime()) == [], body
        assert vsp.evaluate_book_list(".b<js>['k', null, undefined, 'm']</js>", html, _runtime()) == ["k", "m"]


@needs_quickjs
def test_list_js_tail_addendum_to_string_forms_and_elements_methods() -> None:
    html = '<ul><li class="b c"><a href="/x">aa</a></li><li class="b"><a href="/y">bb</a></li></ul>'
    with app_profiles.app_profile_scope("1.1.2"):
        # (c) HTML mode: Elements methods; toString joins outerHTML by a newline
        probe = (
            ".b<js>[String(result).split('\\n').length, result.eachText().join('+'), result.eq(1).text(), result.hasClass('c'), "
            "result.hasClass('zz'), result.attr('class'), result.html().indexOf('<a href') >= 0, result.outerHtml().indexOf('<li') >= 0, "
            "typeof result.remove()].join('|')</js>"
        )
        assert vsp.evaluate_book_list(probe, html, _runtime()) == ["2|aa+bb|bb|true|false|b c|true|true|object"]
        # (b) JSON mode: `[` + raw JSON joined by `,` + `]`, and each object's toString is its raw JSON
        body = '{"a": [{"n": 1}, {"n": 2}]}'
        out = vsp.evaluate_book_list("$.a[*]<js>[String(result), result[0].toString(), result.toString()].join('|')</js>", body, _runtime())
        assert out == ['[{"n": 1},{"n": 2}]|{"n": 1}|[{"n": 1},{"n": 2}]']


@needs_quickjs
def test_list_js_tail_addendum_only_container_items_are_parsed() -> None:
    body = '{"a": ["x", {"n": 1}, 5]}'
    rule = "$.a[*]<js>result.toArray().map(function (v) { return typeof v; })</js>"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_book_list(rule, body, _runtime()) == ["string", "object", "string"]


@needs_quickjs
def test_list_js_tail_blank_script_keeps_the_list_and_blank_results_are_dropped() -> None:
    html = '<ul><li class="b"><a href="/x">aa</a></li></ul>'
    with app_profiles.app_profile_scope("1.1.2"):
        assert len(vsp.evaluate_book_list(".b<js>   </js>", html, _runtime())) == 1
        assert vsp.evaluate_book_list(".b<js>['k', '  ', 'm']</js>", html, _runtime()) == ["k", "m"]


@needs_quickjs
def test_list_js_tail_empty_list_prints_per_the_page_mode_json_array_or_empty_html() -> None:
    """Swift `listResultIsJSON: isJSONContent(html)`: the PAGE decides the mode, so an empty list in a JSON page is `[]`
    (ndlib `result != "[]" ? result : fallback`), and an empty Elements list in an HTML page is the empty string."""
    with app_profiles.app_profile_scope("1.1.2"):
        json_rule = "$.none[*]<js>'<' + String(result) + '>'</js>"
        assert vsp.evaluate_book_list(json_rule, '{"a": [1]}', _runtime()) == ["<[]>"]
        assert vsp.evaluate_book_list(".nomatch<js>'<' + String(result) + '>'</js>", "<p>x</p>", _runtime()) == ["<>"]


@needs_quickjs
def test_segment_after_a_js_segment_runs_on_the_whole_list_in_json_mode_on_1_1_2() -> None:
    """E1 final / Legado getElements: `$.a<js>result</js>$.[*]` on `{"a":[{"n":1},{"n":2}]}` gives the two items (not the fields of each)."""
    body = '{"a": [{"n": 1}, {"n": 2}]}'
    rule = "$.a<js>result</js>$.[*]"
    with app_profiles.app_profile_scope("1.1.2"):
        rows = vsp.evaluate_book_list(rule, body, _runtime())
        assert [json.loads(row) for row in rows] == [{"n": 1}, {"n": 2}]
    with app_profiles.app_profile_scope("1.1.1"):
        # old App: per item, so each object is searched alone and yields its field values
        assert vsp.evaluate_book_list(rule, body, _runtime()) != rows


@needs_quickjs
def test_segment_after_a_js_segment_runs_on_the_joined_outer_html_in_html_mode_on_1_1_2() -> None:
    html = '<ul><li class="b"><a href="/x">aa</a></li><li class="b"><a href="/y">bb</a></li></ul>'
    with app_profiles.app_profile_scope("1.1.2"):
        rows = vsp.evaluate_book_list(".b<js>result</js>a", html, _runtime())
        assert _plain(rows) == ["aa", "bb"]
        # a selector -> selector chain that never passed through JS stays per item
        assert _plain(vsp.evaluate_book_list(".b@a", html, _runtime())) == ["aa", "bb"]
