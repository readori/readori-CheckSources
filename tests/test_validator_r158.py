"""R158: remaining rrs rule gaps (offline; patched fetches and synthetic pages only).

Validator-only fixes (both App profiles): B (ES5 completion value of a control-flow tail), D2 (`<js>` prefix + URL text), D1 (global `let` + `var`),
F (obfuscated script routed to node), H (search item kind reaches the Book of later stages), G6 (`java.getElement("$.x")` in JSON mode),
G7 (replay budget / node timeout), G8 (empty list -> detail page), `java.ajax` lenient options, Jsoup `:matches` / `:matchesOwn`.
Profile 1.1.2 only (mirrors the Swift package E1 / the App's own behaviour): E (`Array.prototype.toArray` hidden), whole-page toc dedup, RSA NoPadding,
jsLib parameter redeclaration, hutool Base64, StrResponse, JSON-object `result.get`, `java.setContent` keeps `result`, `$..[?()]` deep scan, fragment-only
redirect.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from typing import Any

import pytest
import requests

from validator import app_profiles
from validator import validate_source_packages as vsp

BASE = "https://example.test"
needs_quickjs = pytest.mark.skipif(vsp.quickjs is None, reason="QuickJS bridge only")
needs_node = pytest.mark.skipif(not vsp.node_binary_path(), reason="node is not available")


def _runtime(src: dict[str, Any] | None = None) -> vsp.RuleRuntime:
    return vsp.make_runtime(src or {"bookSourceName": "R158", "bookSourceUrl": BASE})


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


def _both_engines():
    """(label, evaluator) for every engine this environment has: QuickJS (what the shipped EXE uses) and node."""
    engines = []
    if vsp.quickjs is not None:
        engines.append(("quickjs", lambda code, content, runtime: vsp._evaluate_js_quickjs(
            code, content, runtime, None, json.loads(content) if vsp.is_legado_json_text(content) else None
        )))
    if vsp.node_binary_path():
        engines.append(("node", lambda code, content, runtime: vsp.evaluate_js_with_node(code, content, runtime)))
    return engines


ENGINES = _both_engines()
engine_param = pytest.mark.parametrize("engine", [e[1] for e in ENGINES], ids=[e[0] for e in ENGINES])


# ------------------------------------------------------------------------------------------------ profile flags

NEW_FLAGS = (
    "content_sample_dedups_whole_toc_page",
    "rsa_bare_public_decrypt_no_padding",
    "js_param_redeclare_relaxed",
    "js_hutool_base64_shim",
    "js_str_response_shim",
    "js_json_object_result_get",
    "set_content_keeps_result_binding",
    "jsonpath_deep_scan_filter",
    "redirect_guard_ignores_url_fragment",
    "js_get_element_json_whole_value",
)


@pytest.mark.parametrize("flag", NEW_FLAGS)
def test_new_profile_flags_are_off_for_1_1_1_and_on_for_1_1_2(flag: str) -> None:
    with app_profiles.app_profile_scope("1.1.1"):
        assert getattr(app_profiles.current_app_profile(), flag) is False
    with app_profiles.app_profile_scope("1.1.2"):
        assert getattr(app_profiles.current_app_profile(), flag) is True


# ------------------------------------------------------------- G: the content sample sees duplicates in the WHOLE first toc page


def _toc_walk(monkeypatch, body: str, rule: dict[str, Any]):
    src = {"bookSourceName": "G", "bookSourceUrl": BASE, "ruleToc": rule}
    site = PrefixSite({"/toc": body})
    monkeypatch.setattr(vsp, "fetch_text", site)
    out = vsp.resolve_toc_source(
        requests.Session(), src, vsp.make_runtime(src), f"{BASE}/book", f"{BASE}/toc", first_page_only=True
    )
    return out, site


def _pinned_toc(total: int, pinned_url: str = "/c/30") -> str:
    rows = [{"name": "latest", "url": pinned_url}] + [{"name": f"c{i}", "url": f"/c/{i}"} for i in range(1, total + 1)]
    return json.dumps({"list": rows})


TOC_RULE = {"chapterList": "$.list[*]", "chapterName": "$.name", "chapterUrl": "$.url"}


def test_g_a_pinned_latest_row_repeated_beyond_the_16_row_window_is_never_the_sample_on_1_1_2(monkeypatch) -> None:
    """Legado BookChapterList.kt:114-125 dedups the whole list keeping the last occurrence (ciyuanji: the pinned latest link repeats as the
    real last chapter ~140 rows later). 1.1.1 / the old approximation only deduped the 16-row window."""
    body = _pinned_toc(30)
    with app_profiles.app_profile_scope("1.1.2"):
        out, site = _toc_walk(monkeypatch, body, TOC_RULE)
        assert out[2] == f"{BASE}/c/1"
        assert site.log == ["/toc"]  # no extra request
    with app_profiles.app_profile_scope("1.1.1"):
        assert _toc_walk(monkeypatch, body, TOC_RULE)[0][2] == f"{BASE}/c/30"


def test_g_a_repeat_inside_the_window_and_a_unique_toc_behave_as_before(monkeypatch) -> None:
    with app_profiles.app_profile_scope("1.1.2"):
        assert _toc_walk(monkeypatch, _pinned_toc(5, "/c/5"), TOC_RULE)[0][2] == f"{BASE}/c/1"
        assert _toc_walk(monkeypatch, _pinned_toc(30, "/c/pinned-unique"), TOC_RULE)[0][2] == f"{BASE}/c/pinned-unique"


def test_g_js_rules_and_reversed_lists_keep_the_window_only_approximation(monkeypatch) -> None:
    body = _pinned_toc(30)  # the pinned "latest" row repeats as the real c/30 in the tail (row 31)
    with app_profiles.app_profile_scope("1.1.2"):
        assert _toc_walk(monkeypatch, body, TOC_RULE)[0][2] == f"{BASE}/c/1"  # control: the tail scan removes the pinned row
        # a JS chapterUrl rule or an inline {{java..}} script: nothing is run for the tail, the pinned row (unique in the window) is the sample
        assert _toc_walk(monkeypatch, body, {**TOC_RULE, "chapterUrl": "$.url@js:result"})[0][2] == f"{BASE}/c/30"
        rows = [{"name": "a", "url": "/a"}]
        for rule_text in ("$.url@js:result", "<js>result</js>", "{{java.put('a','b')}}$.url", "{{source.getKey()}}", "/c/{{$.url.trim()}}", "{{a;b}}"):
            assert _tail(rows, {**TOC_RULE, "chapterUrl": rule_text}) == set(), rule_text
        # a plain `{{$.id}}` template is not an inline script
        assert _tail([{"name": "a", "id": "7"}], {**TOC_RULE, "chapterUrl": "/c/{{$.id}}"}) == {f"{BASE}/c/7"}
        # a reversed list keeps the FIRST occurrence (BookChapterList.kt:119-128): the tail never removes a window row
        out = _toc_walk(monkeypatch, body, {**TOC_RULE, "chapterList": "-$.list[*]"})[0]
        assert out[0] is True and out[2] == f"{BASE}/c/30"


def test_g_row_cap_and_time_budget_keep_the_partial_set(monkeypatch) -> None:
    """App collectWindowOverflowChapterURLs `break`s on the row cap / the time budget and returns what it collected (it does not drop it)."""
    with app_profiles.app_profile_scope("1.1.2"):
        # the pinned duplicate (/c/17) is tail row 1 of 15: a cap of 5 still scans it -> removed from the window, the sample is c/1
        body = _pinned_toc(30, "/c/17")
        monkeypatch.setattr(vsp, "TOC_TAIL_URL_ROW_CAP", 5)
        assert _toc_walk(monkeypatch, body, TOC_RULE)[0][2] == f"{BASE}/c/1"
        # a duplicate beyond the cap is not seen (the old rows 'c30' sit at tail row 15 > 5)
        assert _toc_walk(monkeypatch, _pinned_toc(30, "/c/30"), TOC_RULE)[0][2] == f"{BASE}/c/30"
        monkeypatch.setattr(vsp, "TOC_TAIL_URL_ROW_CAP", 5000)
        # an exhausted budget before the first row: nothing collected, window-only
        monkeypatch.setattr(vsp, "TOC_TAIL_SECONDS", -1.0)
        assert _toc_walk(monkeypatch, body, TOC_RULE)[0][2] == f"{BASE}/c/17"


def test_g_the_cap_scans_the_first_rows_and_the_time_check_runs_every_64_rows(monkeypatch) -> None:
    rows = [{"name": f"t{i}", "url": f"/u/{i}"} for i in range(80)]
    with app_profiles.app_profile_scope("1.1.2"):
        monkeypatch.setattr(vsp, "TOC_TAIL_URL_ROW_CAP", 50)
        urls = _tail(rows, TOC_RULE)
        assert urls == {f"{BASE}/u/{i}" for i in range(50)}  # the FIRST 50 rows, a prefix - not an empty set
        monkeypatch.setattr(vsp, "TOC_TAIL_URL_ROW_CAP", 5000)
        # budget: in time while fewer than 64 rows were evaluated (two rule evaluations per row), over budget at the check before row 64
        real_evaluate = vsp.evaluate_rule
        calls = [0]

        def counting(*args: Any, **kwargs: Any) -> list[str]:
            calls[0] += 1
            return real_evaluate(*args, **kwargs)

        monkeypatch.setattr(vsp, "evaluate_rule", counting)
        monkeypatch.setattr(vsp.time, "monotonic", lambda: 100.0 if calls[0] >= 128 else 0.0)
        urls = _tail(rows, TOC_RULE)
        assert urls == {f"{BASE}/u/{i}" for i in range(64)}


def test_g_the_tail_scan_reraises_a_validation_deadline(monkeypatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> list[str]:
        raise vsp.SourceValidationDeadlineExceeded("deadline")

    with app_profiles.app_profile_scope("1.1.2"):
        monkeypatch.setattr(vsp, "evaluate_rule", boom)
        with pytest.raises(vsp.SourceValidationDeadlineExceeded):
            _tail([{"name": "a", "url": "/a"}], TOC_RULE)


def test_g_a_toc_page_that_lists_its_chapters_twice_falls_back_to_the_window_only_dedup(monkeypatch) -> None:
    """R158c dbxsd / dbxsn: `id.all-chapter@div@a` lists the chapters twice, so all 16 window rows repeat in the tail. The tail-aware dedup would
    empty the sample ("no usable chapter"); the App (`validationChapterSample`) falls back to the window-only dedup, whose first row is the sample
    (Legado keeps the repeated list whose page is the same). The fallback exists on 1.1.2 only; 1.1.1 never scans the tail."""
    rows = [{"name": f"c{i}", "url": f"/c/{i}"} for i in range(1, 17)] * 2
    body = json.dumps({"list": rows})
    with app_profiles.app_profile_scope("1.1.2"):
        out, site = _toc_walk(monkeypatch, body, TOC_RULE)
        assert out[0] is True and out[2] == f"{BASE}/c/1"
        assert site.log == ["/toc"]
    with app_profiles.app_profile_scope("1.1.1"):
        assert _toc_walk(monkeypatch, body, TOC_RULE)[0][2] == f"{BASE}/c/1"
    # the fallback only applies when the tail-aware sample is EMPTY: a pinned row that repeats (ciyuanji) is still removed
    with app_profiles.app_profile_scope("1.1.2"):
        assert _toc_walk(monkeypatch, _pinned_toc(30), TOC_RULE)[0][2] == f"{BASE}/c/1"
    # a window-only duplicate inside the fallback keeps the LAST occurrence of the url (window-only dedup)
    dup = [{"name": "pinned", "url": "/c/9"}] + [{"name": f"c{i}", "url": f"/c/{i}"} for i in range(1, 16)]
    with app_profiles.app_profile_scope("1.1.2"):
        out, _site = _toc_walk(monkeypatch, json.dumps({"list": dup + dup}), TOC_RULE)
        assert out[2] == f"{BASE}/c/1"  # the pinned /c/9 row is deduped by the window's own c9, not sampled


def _tail(rows: list[dict[str, Any]], rule: dict[str, Any], **kwargs: Any) -> set[str]:
    items = [json.dumps(row) for row in rows]
    options = {"reversed_list": False, "heuristic_toc": False, "name_rule_blank": False}
    options.update(kwargs)
    return vsp._toc_tail_chapter_urls(items, 16, rule, _runtime(), "json", f"{BASE}/toc", **options)


def test_g_tail_rows_pass_the_main_loop_filters() -> None:
    rule = {**TOC_RULE, "isVolume": "$.vol"}
    rows = [
        {"name": "a", "url": ""},              # empty url -> the page url
        {"name": "b", "url": "#"},             # hash link -> the page url
        {"name": "c", "url": "/c/9"},
        {"name": "", "url": "/c/untitled"},    # empty title is no chapter
        {"name": "V", "url": "", "vol": True},  # a volume with a synthetic url (title + index) is no chapter
        {"name": "W", "url": "/v/1", "vol": True},  # a volume with a real url stays
    ]
    assert _tail(rows, rule) == {f"{BASE}/toc", f"{BASE}/c/9", f"{BASE}/v/1"}
    assert _tail(rows, {**rule, "chapterUrl": "$.url@js:result"}) == set()
    assert _tail(rows, rule, reversed_list=True) == set()
    assert _tail(rows, rule, heuristic_toc=True) == set()  # a list-less toc is not scanned (hasChapterListRule precondition)


def test_g_tail_rows_pass_the_app_row_filters() -> None:
    """collectWindowOverflowChapterURLs (BookSourceEngine.swift ~3797): digest rows, option@ / @put / @get urls, unusable urls, name rules with JS."""
    rule = {**TOC_RULE}
    rows = [
        {"name": "a", "url": "/ok/1"},
        {"name": "b", "url": "/x?option@y"},                      # option@ / @put / @get urls are skipped
        {"name": "c", "url": "/x@put:{a:1}"},
        {"name": "d", "url": "ftp://h.test/file"},                # not a usable content page url
        {"name": "e", "url": "javascript:void(0)"},               # a javascript: link is the page url
        {"name": "f", "url": "data:;base64,QUJD"},                # a data: pseudo url is usable
    ]
    urls = _tail(rows, rule)
    assert urls == {f"{BASE}/ok/1", f"{BASE}/toc", "data:;base64,QUJD"}
    # an audio row without an url has no placeholder url: skipped
    assert _tail([{"name": "a", "url": ""}], rule, audio_source=True) == set()
    assert _tail([{"name": "a", "url": ""}], rule) == {f"{BASE}/toc"}
    # a name rule holding JS is not checkable: a row without a title still counts
    assert _tail([{"name": "", "url": "/n/1"}], {**rule, "chapterName": "$.name@js:result"}) == {f"{BASE}/n/1"}
    assert _tail([{"name": "", "url": "/n/1"}], rule) == set()
    # a redundant JSON digest row (type digest whose chapid is the id of a normal row) is skipped
    digest_rows = [
        {"id": "7", "type": "chapter", "name": "main", "url": "/d/main"},
        {"type": "digest", "attributes": {"chapid": "7"}, "name": "digest", "url": "/d/digest"},
        {"type": "digest", "attributes": {"chapid": "99"}, "name": "other", "url": "/d/other"},
    ]
    assert _tail(digest_rows, rule) == {f"{BASE}/d/main", f"{BASE}/d/other"}


# ------------------------------------------------------------------------------ B: ES5 completion value of a control-flow tail


COMPLETION_SCRIPT = "if('1'=='1'){result=result}else{result='nope'}\nif(java.get('type')=='4'){result='four'}"


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
@engine_param
def test_b_a_script_ending_in_a_not_taken_if_keeps_the_unchanged_result(engine, profile: str) -> None:
    """ndlib content rule: Rhino returns the value of the last executed expression statement (`result=result`); ES2015 engines `undefined`.
    App JSEngine falls back to the global `result` for an empty script value (no `!= input` guard)."""
    with app_profiles.app_profile_scope(profile):
        assert engine(COMPLETION_SCRIPT, "page-body", _runtime()) == "page-body"


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
@engine_param
def test_b_only_a_control_flow_tail_gets_the_fallback_and_a_throw_keeps_the_guard(engine, profile: str) -> None:
    with app_profiles.app_profile_scope(profile):
        # a script that merely returned "" (`{{java.get("k")}}`) still yields "" - the unguarded fallback would inject the page
        assert engine("java.get('missing')", "page-body", _runtime()) == ""
        assert engine("var a = 1;", "page-body", _runtime()) == ""
        # a script that threw keeps the `fallback != input` guard (App `hadException` branches)
        assert engine("throw new Error('x'); if(false){}", "page-body", _runtime()) == ""
        # a script that changed `result` and ended in a not-taken `if` yields the changed result
        assert engine("result = 'changed'; if(false){ result = 'x' }", "page-body", _runtime()) == "changed"


JJWXC_NAME_RULE = (
    '@js:if(baseUrl.match(/www/)){\njava.getString("@@tag.a.1@text",false)\n}else{\n'
    'a=String(result).match(/x/)?"y":"";\nString(java.getString("@@tag.a.0@text",false)).replace(/a/g,"")+a}'
)
JUNK_ROW = '<tr>\n<td height="10"></td>\n</tr>'


@engine_param
def test_b_a_defined_empty_completion_is_empty_on_1_1_2_and_falls_back_on_1_1_1(engine) -> None:
    """R158d (jjwxc explore): `if(c){java.getString(..)}` that finds nothing completes with a DEFINED "" - the junk first row of the explore
    table must stay empty (Legado skips it) instead of becoming the whole row as name / bookUrl. 1.1.1 App: `result.isEmpty` -> `result`."""
    runtime = _runtime()
    runtime.base_url = "https://www.jjwxc.net/topten.php"
    with app_profiles.app_profile_scope("1.1.2"):
        assert engine('if(true){ "" }', JUNK_ROW, _runtime()) == ""
        assert engine('if(false){ "x" }', JUNK_ROW, _runtime()) == JUNK_ROW  # undefined completion (not-taken branch): the ES5 `result` fallback stays
        assert engine("if(true){ result }", JUNK_ROW, _runtime()) == JUNK_ROW  # a non-empty completion is the value
    with app_profiles.app_profile_scope("1.1.1"):
        assert engine('if(true){ "" }', JUNK_ROW, _runtime()) == JUNK_ROW
        assert engine('if(false){ "x" }', JUNK_ROW, _runtime()) == JUNK_ROW


def test_b_the_jjwxc_explore_name_rule_skips_the_junk_row_on_1_1_2() -> None:
    runtime = vsp.make_runtime({"bookSourceUrl": "https://www.jjwxc.net", "bookSourceName": "jjwxc"})
    runtime.base_url = "https://www.jjwxc.net/topten.php?orderstr=19&t=0"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_rule(JJWXC_NAME_RULE, JUNK_ROW, runtime, "html") == []
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_rule(JJWXC_NAME_RULE, JUNK_ROW, runtime, "html") != []  # App 1.1.1 keeps the fallback


def test_b_js_tail_is_control_flow_scanner() -> None:
    f = vsp._js_tail_is_control_flow
    assert f("if(a){x=1}else{y=2}\nif(b){z}") is True
    assert f("for (var i=0;i<1;i++){}") is True
    assert f("try{a()}catch(e){}") is True
    assert f("var a = 1;") is False
    assert f("foo(function(){ return 1 })") is False
    assert f("function g(){ return 'if(a){}' }") is False
    assert f("var s = \"if(a){}\"; function h(){}") is False


# -------------------------------------------------------------------------------------- D2: `<js>..</js>` prefix + URL text


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_d2_a_js_prefix_followed_by_url_text_still_binds_the_keyword(profile: str) -> None:
    """Legado AnalyzeUrl.analyzeJs (:153-176) drops the JS value when URL text follows; `{{key}}` is evaluated afterwards (App
    legadoAnalyzeUrlRule). ndlib `.cn`: the keyword used to be lost (`search=type=0`)."""
    runtime = _runtime()
    template = "<js>java.log('x')</js>\nhttp://h.test/s?search={{key}}&type=0&page={{page}}"
    with app_profiles.app_profile_scope(profile):
        url, _opts = vsp._build_url_with_options(template, "关键", 1, runtime)
    assert url.startswith("http://h.test/s?search=") and "type=0&page=1" in url
    assert "search=&" not in url and "%E5%85%B3%E9%94%AE" in url


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_d2_a_whole_template_script_is_unchanged(profile: str) -> None:
    runtime = _runtime()
    with app_profiles.app_profile_scope(profile):
        url, _opts = vsp._build_url_with_options('<js>"http://h.test/q/" + key</js>', "abc", 1, runtime)
    assert url == "http://h.test/q/abc"


# ----------------------------------------------------------------------------- E: Array.prototype.toArray is not enumerable


@engine_param
def test_e_for_in_over_a_list_sees_only_the_indices_on_both_profiles(engine) -> None:
    """Rhino NativeJavaList enumerates indices only; the App installs toArray with _readoriDefineHidden (JSEngine.swift ~8686, already in HEAD / 1.1.1)."""
    code = "var keys = []; for (var k in [10, 20]) keys.push(k); keys.join(',')"
    with app_profiles.app_profile_scope("1.1.2"):
        assert engine(code, "", _runtime()) == "0,1"
    with app_profiles.app_profile_scope("1.1.1"):
        assert engine(code, "", _runtime()) == "0,1"  # was "0,1,toArray": stricter than HEAD, which already hides the helper


# --------------------------------------------------------------------- F: obfuscated script routed to the node engine


def test_f_a_long_mostly_non_ascii_script_without_a_java_marker_goes_to_node() -> None:
    padding = "".join(chr(0x4E00 + (i * 7) % 5000) for i in range(1800))
    hidden = "/*" + padding + "*/ var a = ['ja' + 'va'];"
    assert vsp._js_obfuscated_needs_node(hidden) is True
    assert vsp.js_needs_node_crypto_engine(hidden, _runtime()) is True
    assert vsp._js_obfuscated_needs_node("var a = 1; " * 400 + padding[:200]) is False  # not mostly non-ASCII
    assert vsp._js_obfuscated_needs_node("/*" + padding[:100] + "*/ var a = 1;") is False  # short
    assert vsp._js_obfuscated_needs_node(hidden + " java.md5Encode('x')") is False  # a literal java. is scanned by the normal routing


@needs_node
def test_f_an_obfuscated_hmac_call_computes_the_real_hmac() -> None:
    import hashlib
    import hmac

    padding = "".join(chr(0x4E00 + (i * 7) % 5000) for i in range(1800))
    code = "/*" + padding + "*/ eval('ja' + 'va.HMacBase64')('hello', 'HmacSHA256', 'skybook')"
    expected = base64.b64encode(hmac.new(b"skybook", b"hello", hashlib.sha256).digest()).decode()
    assert vsp.evaluate_js(code, "", _runtime()) == expected


# ------------------------------------------------------------------------------------------ D1: global let + var conflict


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
@engine_param
def test_d1_a_global_let_in_the_jslib_and_a_var_in_the_script_do_not_clash(engine, profile: str) -> None:
    """gongzicp: jsLib `let d;` + rule script `var d = ...`. Rhino and the App (hoistTopLevelLetConstToVar + function-scoped evaluation)
    accept it; a global script cannot."""
    runtime = _runtime()
    runtime.js_lib = "let d; function readD() { return d; }"
    with app_profiles.app_profile_scope(profile):
        assert engine("var d = 5; d + 1", "", runtime) == "6"


# ---------------------------------------------------------------- D1: jsLib parameter redeclaration (1.1.2 App mirror)


@engine_param
def test_d1_a_jslib_function_that_redeclares_a_parameter_loads_only_on_1_1_2(engine) -> None:
    runtime = _runtime()
    runtime.js_lib = "function AES(text, key, iv) { let key = key + '!'; let iv = 'v'; return text + key + iv; }"
    with app_profiles.app_profile_scope("1.1.2"):
        assert engine("AES('a', 'b', 'c')", "", runtime) == "ab!v"
    with app_profiles.app_profile_scope("1.1.1"):
        assert engine("AES('a', 'b', 'c')", "", runtime) == ""  # the whole jsLib fails to load (SyntaxError), like JavaScriptCore


def test_d1_relax_parameter_redeclarations_touches_only_the_function_body_depth() -> None:
    relax = vsp.relax_parameter_redeclarations
    # the keyword becomes `var` (not deleted): `var a = 1` re-declares the parameter legally and the other declarators stay function-local
    assert relax("function f(a, b) { let a = 1; const b = 2; return a + b; }") == "function f(a, b) { var a = 1; var b = 2; return a + b; }"
    assert relax("function f(key) { let key = 1, other = 2; return other; }") == "function f(key) { var key = 1, other = 2; return other; }"
    # block-scoped shadowing is legal; strings / templates / comments / regex literals are never scanned
    legal = "function f(a) { if (x) { let a = 1; } var s = 'let a = 2'; /* let a */ return `let a ${a}`; }"
    assert relax(legal) == legal
    # a non-parameter declaration, a `let` without a function, and plain text are returned untouched
    assert relax("function f(a) { let b = 1; }") == "function f(a) { let b = 1; }"
    assert relax("let a = 1;") == "let a = 1;"
    assert relax("var f = function(key) { let key = 'x'; return key; }") == "var f = function(key) { var key = 'x'; return key; }"
    # `i++ / 2` is a division: the `/ 2; let a = 1; ... /` pair must not be read as a regex literal that hides the redeclaration
    assert relax("function f(a) { var i = 1; i++ / 2; let a = 3; return i-- / 2 + a; }") == "function f(a) { var i = 1; i++ / 2; var a = 3; return i-- / 2 + a; }"
    # a default value in the parameter list is skipped, the name is still a parameter
    assert relax("function f(a = 1, b) { let a = 3; }") == "function f(a = 1, b) { var a = 3; }"


# --------------------------------------------------------------------------- 1.1.2 JS shims: hutool Base64, StrResponse, Map.get


@engine_param
def test_hutool_base64_is_available_only_on_1_1_2(engine) -> None:
    code = ("var b = Packages.cn.hutool.core.codec.Base64; "
            "[b.encode('你好'), b.encodeStr('ab'), b.encodeUrlSafe('a?>'), b.encodeWithoutPadding('a'), b.decodeStr('5L2g5aW9'), b.isBase64('QUI='), b.decode('QUI=').join(',')].join('|')")
    with app_profiles.app_profile_scope("1.1.2"):
        assert engine(code, "", _runtime()) == "5L2g5aW9|YWI=|YT8-|YQ|你好|true|65,66"
    with app_profiles.app_profile_scope("1.1.1"):
        assert engine(code, "", _runtime()) == ""


@engine_param
def test_str_response_constructor_accepts_a_response_object_and_keeps_the_namespace(engine) -> None:
    code = ("var first = Packages.io.legado.app.help.http.StrResponse('http://a.test/x', 'b1'); "
            "var second = Packages.io.legado.app.help.http.StrResponse(first, 'b2'); "
            "[second.body(), String(second.url()).indexOf('a.test/x') >= 0 ? 'u' : 'n', typeof Packages.cn.hutool.core.codec.Base64].join('|')")
    with app_profiles.app_profile_scope("1.1.2"):
        assert engine(code, "", _runtime()) == "b2|u|object"


@engine_param
def test_str_response_constructor_builds_a_response_only_on_1_1_2(engine) -> None:
    code = ("var r = Packages.io.legado.app.help.http.StrResponse('http://a.test/x', '{\"k\":1}'); "
            "[r.url ? 'u' : 'n', r.body(), r.code()].join('|')")
    with app_profiles.app_profile_scope("1.1.2"):
        assert engine(code, "", _runtime()) == "u|{\"k\":1}|200"
    with app_profiles.app_profile_scope("1.1.1"):
        assert engine(code, "", _runtime()) == ""


@engine_param
def test_a_json_object_result_has_the_java_map_get_only_on_1_1_2(engine) -> None:
    with app_profiles.app_profile_scope("1.1.2"):
        assert engine("[result.get('k'), String(result.get('missing')), Object.keys(result).join(',')].join('|')", '{"k": "v"}', _runtime()) == "v|null|k"
    with app_profiles.app_profile_scope("1.1.1"):
        assert engine("result.get('k')", '{"k": "v"}', _runtime()) == ""


# ------------------------------------------------------------------------------------ 5: java.setContent keeps `result`


@engine_param
def test_set_content_replaces_only_the_content_on_1_1_2(engine) -> None:
    """Legado AnalyzeRule.setContent (:84-96) replaces `content` only; the `result` / `src` bindings stay (wenxue88 bookList)."""
    code = "java.setContent('<b>x</b>'); [result, java.getString('b@text')].join('|')"
    with app_profiles.app_profile_scope("1.1.2"):
        assert engine(code, "<p>old</p>", _runtime()) == "<p>old</p>|x"
    with app_profiles.app_profile_scope("1.1.1"):
        assert engine(code, "<p>old</p>", _runtime()) == "<b>x</b>|x"


# ------------------------------------------------------------------ G6: java.getElement in JSON mode answers the value


@engine_param
def test_g6_get_element_in_json_mode_returns_the_value_on_1_1_2(engine) -> None:
    content = json.dumps({"a": {"content": "hello", "n": 3}})
    with app_profiles.app_profile_scope("1.1.2"):
        assert engine("java.getElement('$.a.content')", content, _runtime()) == "hello"
        assert engine("var o = java.getElement('$.a'); o.content + '|' + o.n", content, _runtime()) == "hello|3"


@engine_param
def test_g6_get_element_in_json_mode_keeps_the_old_row_behaviour_on_1_1_1(engine) -> None:
    content = json.dumps({"a": {"content": "hello", "n": 3}, "data": {"list": [{"t": "a"}]}})
    with app_profiles.app_profile_scope("1.1.1"):
        # 1.1.1 App getElementRawFn = resolveElementsRule(..).first: a Jsoup-style row, never the JSON value / the whole array
        assert engine("java.getElement('$.a.content')", content, _runtime()) == '{"_html":"","_innerHtml":"","_parent":null}'  # an empty element row
        assert engine("var o = java.getElement('$.a'); o.content + '|' + o.n", content, _runtime()) == "undefined|undefined"
        assert engine("var v = java.getElement('$.data.list'); String(Array.isArray(v))", content, _runtime()) == "false"


@engine_param
def test_g6_get_element_in_json_mode_returns_the_whole_value_not_the_first_row(engine) -> None:
    content = json.dumps({"data": {"list": [{"t": "a"}, {"t": "b"}], "n": 3, "name": "x"}})
    with app_profiles.app_profile_scope("1.1.2"):
        # a definite path whose value is an array answers the whole array (Legado getObject = ctx.read(rule)), not item 0
        assert engine("var v = java.getElement('$.data.list'); [Array.isArray(v), v.length, v[1].t].join('|')", content, _runtime()) == "true|2|b"
        # an indefinite path answers the array of every hit
        assert engine("var v = java.getElement('$.data.list[*].t'); [Array.isArray(v), v.join(',')].join('|')", content, _runtime()) == "true|a,b"
        # numbers / strings stay typed, a miss is the empty string
        assert engine("typeof java.getElement('$.data.n') + '|' + java.getElement('$.data.name') + '|[' + java.getElement('$.data.none') + ']'", content, _runtime()) == "number|x|[]"


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
@engine_param
def test_get_string_list_without_content_reads_the_set_content(engine, profile: str) -> None:
    with app_profiles.app_profile_scope(profile):
        code = "java.setContent(JSON.stringify({a: [1, 2, 3]})); java.getStringList('$.a[*]').join(',')"
        assert engine(code, "plain page", _runtime()) == "1,2,3"


# ---------------------------------------------------------------- G7: replay budget and node subprocess timeout match the App


def test_g7_the_replay_budget_is_the_apps_bulk_timeout_not_the_old_6_seconds() -> None:
    assert vsp.QUICKJS_NATIVE_REPLAY_SCRIPT_SECONDS == 15.0  # BookSourceValidator.swift bulkTimeoutCeiling (>= 100 sources)
    assert vsp.NODE_JS_SUBPROCESS_SECONDS == 15.0


@needs_node
def test_g7_the_node_subprocess_gets_the_app_timeout_clamped_by_the_source_budget(monkeypatch) -> None:
    seen: list[float] = []

    def fake_run(*args: Any, **kwargs: Any):
        seen.append(float(kwargs["timeout"]))
        return subprocess.CompletedProcess(args, 0, stdout='{"value": "ok", "variables": {}}', stderr="")

    monkeypatch.setattr(vsp.subprocess, "run", fake_run)
    assert vsp.evaluate_js_with_node("1 + 1", "", _runtime()) == "ok"
    assert seen and seen[0] == 15.0
    # the per-source deadline still clamps it
    vsp.begin_source_validation_deadline(3)
    try:
        seen.clear()
        vsp.evaluate_js_with_node("1 + 1", "", _runtime())
        assert seen and seen[0] <= 3.0
    finally:
        vsp.clear_source_validation_deadline() if hasattr(vsp, "clear_source_validation_deadline") else None


# -------------------------------------------------------------------------------- java.ajax lenient option parsing


@engine_param
def test_ajax_url_options_accept_the_lenient_json_the_app_reads(engine) -> None:
    """asgportal: `java.ajax(url, {"method": "post", body: "..."})` - bare keys / single quotes / trailing commas (UrlOption is Gson lenient)."""
    code = ("var p = __readoriParseUrlOptions(\"http://x.test/a,{\\\"method\\\": \\\"post\\\", body: 'a=1&b=\\\"2\\\"', 'headers': {k: 'v',},}\"); "
            "JSON.stringify([p.url, p.options.method, p.options.body, p.options.headers])")
    out = engine(code, "", _runtime())
    assert json.loads(out) == ["http://x.test/a", "post", 'a=1&b="2"', {"k": "v"}]
    assert json.loads(engine("JSON.stringify(__readoriParseUrlOptions('http://x.test/a'))", "", _runtime())) == {"url": "http://x.test/a", "options": {}}
    assert json.loads(engine("JSON.stringify(__readoriParseUrlOptions('http://x.test/a,{\"a\":1}'))", "", _runtime()))["options"] == {"a": 1}


# --------------------------------------------------------------------------------------- A: RSA bare transformation NoPadding


def _rsa_material():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    numbers = key.private_numbers()
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return key, numbers, json.dumps(public_pem)  # a JS string literal


def _private_raw(numbers, block: bytes) -> bytes:
    n = numbers.public_numbers.n
    return pow(int.from_bytes(block, "big"), numbers.d, n).to_bytes((n.bit_length() + 7) // 8, "big")


@needs_node
def test_a_bare_rsa_public_decrypt_reads_nopadding_blocks_only_on_1_1_2() -> None:
    """bookld: the server private-key encrypts WITHOUT padding. Legado JsEncodeUtils.kt:77 -> hutool AsymmetricCrypto("RSA") -> Cipher("RSA") (raw RSA,
    leading zero bytes dropped); PKCS1 type-1 unpadding rejects the block."""
    _key, numbers, public_b64 = _rsa_material()
    plaintext = "hello world 正文".encode()
    cipher_b64 = base64.b64encode(_private_raw(numbers, bytes(128 - len(plaintext)) + plaintext)).decode()
    code = f"java.createAsymmetricCrypto('RSA').setPublicKey({public_b64}).decryptStr('{cipher_b64}')"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(code, "", _runtime()) == "hello world 正文"
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_js(code, "", _runtime()) == ""


@needs_node
def test_a_pkcs1_blocks_and_explicit_transformations_are_unchanged() -> None:
    _key, numbers, public_b64 = _rsa_material()
    data = b"signed text"
    pkcs1 = b"\x00\x01" + b"\xff" * (128 - 3 - len(data)) + b"\x00" + data
    cipher_b64 = base64.b64encode(_private_raw(numbers, pkcs1)).decode()
    nopad = base64.b64encode(_private_raw(numbers, bytes(128 - len(data)) + data)).decode()
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.evaluate_js(f"java.createAsymmetricCrypto('RSA').setPublicKey({public_b64}).decryptStr('{cipher_b64}')", "", _runtime()) == "signed text"
        # a transformation with a `/` is explicit: no retry
        assert vsp.evaluate_js(
            f"java.createAsymmetricCrypto('RSA/ECB/PKCS1Padding').setPublicKey({public_b64}).decryptStr('{nopad}')", "", _runtime()
        ) == ""
        # the NoPadding transformation itself keeps working
        assert vsp.evaluate_js(
            f"java.createAsymmetricCrypto('RSA/ECB/NoPadding').setPublicKey({public_b64}).decryptStr('{nopad}')", "", _runtime()
        ).endswith("signed text")


# ------------------------------------------------------------------------------ 6: JSONPath deep-scan filter `$..[?()]`


def test_the_deep_scan_filter_returns_every_matching_object_only_on_1_1_2() -> None:
    """Jayway `$..[?(filter)]`: pre-order walk, every object node that passes the filter is a result (jjwxc `$..[?(@.novelName)]`)."""
    root = {"a": [{"novelName": "N1", "inner": {"novelName": "N2"}}, {"x": 1}], "novelName": "TOP", "b": {"c": [{"novelName": "N3"}]}}
    with app_profiles.app_profile_scope("1.1.2"):
        matches = vsp.evaluate_legado_jsonpath_filter(root, "$..[?(@.novelName)]")
        assert [m["novelName"] for m in matches] == ["TOP", "N1", "N2", "N3"]
        assert vsp.evaluate_legado_jsonpath_filter(root, "$..[?(@.novelName == 'N3')]") == [{"novelName": "N3"}]
        assert vsp.evaluate_legado_jsonpath_filter(root, "$..[?(@.novelName)].novelName") == ["TOP", "N1", "N2", "N3"]
        assert vsp.evaluate_legado_jsonpath_filter(root, "$.a..[?(@.novelName)]") == [root["a"][0], root["a"][0]["inner"]]
        assert vsp.evaluate_legado_jsonpath_filter(root, "$..[?(@.nothing)]") == []
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.evaluate_legado_jsonpath_filter(root, "$..[?(@.novelName)]") == []


def test_the_deep_scan_filter_reaches_a_book_list_rule() -> None:
    content = json.dumps({"data": {"rows": [{"novelName": "A", "id": 1}, {"novelName": "B", "id": 2}], "meta": {"novelName": "M"}}})
    with app_profiles.app_profile_scope("1.1.2"):
        items = vsp.evaluate_book_list("$..[?(@.novelName)]", content, _runtime())
        # Jayway pre-order walk: the `rows` elements come before the later sibling `meta`
        assert [json.loads(i)["novelName"] for i in items] == ["A", "B", "M"]


# ------------------------------------------------------------------------ G2: a fragment-only URL difference is no redirect


def test_g2_a_fragment_only_difference_is_not_a_redirect_on_1_1_2() -> None:
    request = "https://www.wenxue88.com#我的"
    final = "https://www.wenxue88.com/#%E6%88%91%E7%9A%84"
    with app_profiles.app_profile_scope("1.1.2"):
        assert vsp.response_url_differs_from_request(request, final) is False
        assert vsp.response_url_differs_from_request("https://h.test/s?q=1#x", "https://h.test/s?q=1") is False
        # a real redirect still is one, with or without a fragment
        assert vsp.response_url_differs_from_request(request, "https://www.wenxue88.com/index.php") is True
        assert vsp.response_url_differs_from_request("https://h.test/s#x", "https://h.test/home#x") is True
        # percent-encoding only (App 1.1.2 guard)
        assert vsp.response_url_differs_from_request("https://h.test/s?q=我的", "https://h.test/s?q=%E6%88%91%E7%9A%84") is False
    with app_profiles.app_profile_scope("1.1.1"):
        assert vsp.response_url_differs_from_request(request, final) is True


# ------------------------------------------------------------- G8: an empty list is parsed as a detail page (BookList.getInfoItem)


def _list_less_source(**over: Any) -> dict[str, Any]:
    src: dict[str, Any] = {
        "bookSourceName": "G8",
        "bookSourceUrl": BASE,
        "searchUrl": "/search?q={{key}}",
        "ruleSearch": {"bookList": ".none li", "name": "a@text", "bookUrl": "a@href"},
        "ruleBookInfo": {"name": "h1@text", "author": ".au@text"},
    }
    src.update(over)
    return src


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_g8_a_list_less_search_response_that_the_book_info_rules_read_is_a_book(monkeypatch, profile: str) -> None:
    """Legado BookList.kt:59-87 / App extractSearchBookFromDetailRules: no list, empty bookUrlPattern -> the response is a detail page."""
    page = '<h1>The Book</h1><p class="au">Someone</p>'
    monkeypatch.setattr(vsp, "fetch_text", PrefixSite({"/search": page}))
    src = _list_less_source()
    with app_profiles.app_profile_scope(profile):
        runtime = vsp.make_runtime(src)
        urls, base = vsp.fetch_book_candidates(requests.Session(), src, runtime, modes=("search",))
    assert urls == [f"{BASE}/search?q=%E6%88%91%E7%9A%84"] and base == urls[0]
    assert runtime.validation_book_author == "Someone"


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_g8_the_put_variables_of_the_info_rules_reach_the_later_stages(monkeypatch, profile: str) -> None:
    """App extractSearchBookFromDetailRules `capturedVariables = detailContext.serializedVariables()`: an `@put` in author / intro / kind is kept."""
    page = '<h1>The Book</h1><p class="au">Someone</p><i class="id">42</i>'
    monkeypatch.setattr(vsp, "fetch_text", PrefixSite({"/search": page}))
    src = _list_less_source(ruleBookInfo={
        "name": "h1@text",
        "author": ".au@text@put:{who:'.au@text'}",
        "intro": "p@text@put:{bid:'.id@text'}",
    })
    with app_profiles.app_profile_scope(profile):
        runtime = vsp.make_runtime(src)
        urls, _base = vsp.fetch_book_candidates(requests.Session(), src, runtime, modes=("search",))
    assert urls
    assert runtime.variables.get("bid") == "42"
    assert runtime.variables.get("who") == "Someone"


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_g8_no_book_info_name_a_book_url_pattern_or_an_unreadable_page_stay_failures(monkeypatch, profile: str) -> None:
    monkeypatch.setattr(vsp, "fetch_text", PrefixSite({"/search": "<p>nothing here</p>"}))
    with app_profiles.app_profile_scope(profile):
        for src in (
            _list_less_source(),  # the name rule finds nothing
            _list_less_source(bookUrlPattern=r"https://example\.test/book/\d+", ruleBookInfo={"name": "p@text"}),
            _list_less_source(ruleBookInfo={}),
        ):
            urls, _base = vsp.fetch_book_candidates(requests.Session(), src, vsp.make_runtime(src), modes=("search",))
            assert urls == []


def test_g8_the_explore_stage_takes_the_same_fallback(monkeypatch) -> None:
    monkeypatch.setattr(vsp, "fetch_text", PrefixSite({"/explore": '<h1>Explored</h1>'}))
    src = _list_less_source(exploreUrl="分类::/explore", ruleExplore={"bookList": ".none li", "name": "a@text", "bookUrl": "a@href"})
    del src["searchUrl"]
    urls, _base = vsp.fetch_book_candidates(requests.Session(), src, vsp.make_runtime(src), modes=("explore",))
    assert urls == [f"{BASE}/explore"]


# -------------------------------------------------------------------------- H: the search item kind reaches later stages


def _kind_source() -> dict[str, Any]:
    return {
        "bookSourceName": "H",
        "bookSourceUrl": BASE,
        "enabled": True,
        "searchUrl": "/s?q={{key}}",
        "ruleSearch": {"bookList": ".b", "name": "a@text", "kind": ".k@text", "bookUrl": "a@href"},
        "ruleBookInfo": {"name": "h1@text", "tocUrl": "@js:'/toc?k=' + book.kind"},
        "ruleToc": {"chapterList": ".toc li", "chapterName": "a@text", "chapterUrl": "a@href"},
        "ruleContent": {"content": "#c@text"},
    }


KIND_PAGES = {
    "/s?": '<div class="b"><a href="/book/1">N</a><i class="k">KIND9</i></div>',
    "/book/1": "<h1>Book</h1>",
    "/toc?k=KIND9": '<ul class="toc"><li><a href="/c/1">第一章</a></li></ul>',
    "/c/1": '<div id="c">这是一段足够长的正文内容用于验证。这是一段足够长的正文内容用于验证。</div>',
}


def test_h_a_search_item_kind_is_the_books_kind_in_the_detail_and_toc_stages(monkeypatch) -> None:
    """qq `novel.html5.qq.com`: `tocUrl = ...{{book.kind}}` works through the SEARCH item's kind (App SearchBook.kind -> Book.kind). The item
    runtime holds the kind from 1.1.2 on (`search_item_fields_visible_to_book_url`); the carrier is profile independent."""
    site = PrefixSite(KIND_PAGES)
    monkeypatch.setattr(vsp, "fetch_text", site)
    with app_profiles.app_profile_scope("1.1.2"):
        outcome, _record, detail = vsp.validate_single_source(_kind_source())
    assert outcome.passed, detail
    assert "/toc?k=KIND9" in site.log


def test_h_the_quick_scan_seed_carries_the_book_state_to_the_full_chain(monkeypatch) -> None:
    site = PrefixSite(KIND_PAGES)
    monkeypatch.setattr(vsp, "fetch_text", site)
    with app_profiles.app_profile_scope("1.1.2"):
        passed, seed, _detail = vsp.quick_scan_single_source(_kind_source())
        assert passed and seed is not None
        assert seed["bookState"]["book_kind"] == "KIND9"
        json.dumps(seed)
        site.log.clear()
        outcome, _record, detail = vsp.validate_single_source(_kind_source(), quick_seed=seed)
    assert outcome.passed, detail
    assert "/toc?k=KIND9" in site.log
    assert not any(path.startswith("/s?") for path in site.log)  # the seeded run does not search again


def test_h_apply_selected_book_state_splits_the_carrier_keys() -> None:
    runtime = _runtime()
    rest = vsp._apply_selected_book_state(runtime, {"__book_kind": "K", "__book_intro": "I", "tag": "T", "__other": "O"})
    assert (runtime.book_kind, runtime.book_intro) == ("K", "I")
    assert rest == {"tag": "T", "__other": "O"}


# -------------------------------------------------------------------------------- Jsoup :matches / :matchesOwn (SwiftSoup parity)


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_jsoup_matches_and_matches_own_select_by_text_regex(profile: str) -> None:
    """The App passes the selector to SwiftSoup (CSSEngine.swift `select`), which implements Evaluator.Matches / MatchesOwn."""
    html = '<ul><li>第1章 起</li><li>番外</li><li><b>第2章</b> 承 <i>x</i></li></ul>'
    with app_profiles.app_profile_scope(profile):
        rows = json.loads(vsp.select_elements_json("li:matches(^第\\d+章)", html, BASE))
        assert [re.sub(r"<[^>]+>", "", r.get("outerHtml") or r.get("html") or "") for r in rows] == ["第1章 起", "第2章 承 x"]
        own = json.loads(vsp.select_elements_json("li:matchesOwn(承)", html, BASE))
        assert len(own) == 1 and "第2章" in (own[0].get("outerHtml") or own[0].get("html") or "")
        none = json.loads(vsp.select_elements_json("li:matchesOwn(^第\\d+章$)", html, BASE))
        assert none == []
        # Jsoup text(): whitespace collapsed, blocks spaced, comments ignored; ownText() ignores the children
        spaced = "<div><p>a</p><p>b</p></div><div>x<!-- c -->\n   y</div><div><span>z</span> w</div>"
        assert len(json.loads(vsp.select_elements_json("div:matches(^a b$)", spaced, BASE))) == 1
        assert len(json.loads(vsp.select_elements_json("div:matches(^x y$)", spaced, BASE))) == 1
        assert len(json.loads(vsp.select_elements_json("div:matchesOwn(^w$)", spaced, BASE))) == 1
        assert len(json.loads(vsp.select_elements_json("div:matchesOwn(z)", spaced, BASE))) == 0


# ------------------------------------------- huarenv5: a bare `title` / `name` / `content` first segment of an `@` chain is a Jsoup select


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_a_chain_segment_named_like_an_attribute_is_a_selector_unless_it_is_the_last_one(profile: str) -> None:
    page = "<html><head><title>T 1 - site</title></head><body><div title='dt'>d</div><p>x</p></body></html>"
    with app_profiles.app_profile_scope(profile):
        rt = _runtime()
        # first segment = select("title"); the last segment is the result (AnalyzeByJSoup.getResultList / App RuleType.isSelectorLikeSegment)
        assert vsp.evaluate_rule("title@ownText", page, rt, "html") == ["T 1 - site"]
        assert vsp.evaluate_rule("title@text", page, rt, "html") == ["T 1 - site"]
        # a last segment `title` is the attribute
        assert vsp.evaluate_rule("div@title", page, rt, "html") == ["dt"]
        # a lone `title` is the current element's attribute (knownBareAttrs), not a selector
        assert vsp.evaluate_rule("title", "<div title='dt'>d</div>", rt, "html") == ["dt"]
        assert vsp.evaluate_rule("title", page, rt, "html") == []
        # the huarenv5 name rule: first branch empty, second branch `title@ownText` with the `##regex` replace chain
        chain = r".noveltitle@p.1@text||title@ownText##\s*-\s*site$"
        assert vsp.evaluate_rule(chain, page, rt, "html") == ["T 1"]


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_a_lone_js_init_replaces_the_body_with_its_whole_multi_line_string(profile: str) -> None:
    page = "<html>\n<head><title>T</title></head>\n<body>\n<p>x</p>\n</body></html>"
    with app_profiles.app_profile_scope(profile):
        rt = _runtime()
        out = vsp.evaluate_init_rule_nonthrowing("<js>\nvar y = 1;\nresult</js>", page, rt, "html")
        assert out == page  # App JSEngine.evaluateToArray -> [s]: not cut to the first line
        assert vsp.evaluate_init_rule_nonthrowing("@js:result.toUpperCase()", "a\nb", rt, "html") == "A\nB"
        assert vsp.evaluate_init_rule_nonthrowing("<js>''</js>", page, rt, "html") == ""


# ------------------------------------------------------------------------------- REV batch: own text, whitespace, blank url rule, toArray


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_matches_own_uses_jsoup_own_text_but_at_own_text_is_unchanged(profile: str) -> None:
    html = "<p>a<!--c-->b<i>x</i>d</p><p>q<br>r</p>"
    with app_profiles.app_profile_scope(profile):
        assert len(json.loads(vsp.select_elements_json("p:matchesOwn(^abd$)", html, BASE))) == 1  # direct text nodes, no separator, no comment
        assert len(json.loads(vsp.select_elements_json("p:matchesOwn(^q r$)", html, BASE))) == 1  # a direct <br> is a space
        assert len(json.loads(vsp.select_elements_json("p:matchesOwn(x)", html, BASE))) == 0
        soup = vsp.BeautifulSoup(html, "html.parser")
        assert vsp._jsoup_own_text(soup.find("p")) == "abd"
        assert vsp._css_own_text(soup.find("p")) != "abd"  # `@ownText` keeps its own behaviour


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_jsoup_text_collapses_only_jsoup_whitespace_so_ideographic_indents_survive(profile: str) -> None:
    indent = chr(0x3000) * 2
    nbsp = chr(0xA0)
    html = f"<div><p>{indent}Chapter</p><p>a{nbsp}{nbsp} \t\n b</p><p>x{chr(0x2003)}y</p></div>"
    with app_profiles.app_profile_scope(profile):
        soup = vsp.BeautifulSoup(html, "html.parser")
        paragraphs = soup.find_all("p")
        assert vsp._jsoup_text(paragraphs[0]) == f"{indent}Chapter"  # U+3000 is not whitespace to Jsoup
        assert vsp._jsoup_text(paragraphs[1]) == "a b"  # space, tab, newline and U+00A0 collapse
        assert vsp._jsoup_text(paragraphs[2]) == f"x{chr(0x2003)}y"  # the U+2000 series is not collapsed either
        assert vsp.evaluate_rule("p.0@text", html, _runtime(), "html") == [f"{indent}Chapter"]


def test_g_a_blank_chapter_url_rule_still_scans_and_every_row_is_the_page_url() -> None:
    """Swift collectWindowOverflowChapterURLs: `evaluateString("")` is "", so each row url is the source url (the page url)."""
    rule = {"chapterList": "$.list[*]", "chapterName": "$.name", "chapterUrl": ""}
    with app_profiles.app_profile_scope("1.1.2"):
        assert _tail([{"name": "a"}, {"name": "b"}], rule) == {f"{BASE}/toc"}
        assert _tail([{"name": ""}], rule) == set()  # an empty title is still no chapter


@pytest.mark.parametrize("profile", ("1.1.1", "1.1.2"))
def test_lone_js_init_needs_exactly_one_script_block(profile: str) -> None:
    with app_profiles.app_profile_scope(profile):
        rt = _runtime()
        # two `<js>` blocks are NOT one script: the generic segment pipeline runs them in turn
        assert vsp.evaluate_init_rule_nonthrowing("<js>result + '1'</js><js>result + '2'</js>", "x", rt, "html") == "x12"
        assert vsp.evaluate_init_rule_nonthrowing("<js>result + '1'</js>", "x", rt, "html") == "x1"


# ------------------------------------------------------------------------- R158c: regex prefilter and the GIL-free large-input path

HONGXIU_CHAIN = r"(\s|\S)*?阅读上一章|目录.*\s|-/-"


def _plain_replace(value: str, chain: str) -> str:
    """The pre-R158c semantics: plain `re` (Java dot conversion first), no prefilter, no `regex` module."""
    parts = chain.split("##")
    pattern, replacement = parts[0], parts[1] if len(parts) > 1 else ""
    compiled = re.compile(vsp.java_regex_dot_to_python(pattern))
    if len(parts) > 2:
        first = compiled.search(value)
        if first is None:
            return ""
        return compiled.sub(lambda m: vsp._java_regex_replacement(m, replacement), first.group(0), count=1)
    return compiled.sub(lambda m: vsp._java_regex_replacement(m, replacement), value)


def test_hongxiu_replace_regex_on_100k_text_without_the_literals_is_instant() -> None:
    import time

    text = ("这是一段没有任何标记的正文，只有普通文字。" * 6000)[:100_122]
    assert len(text) > 100_000 and not any(literal in text for literal in ("阅读上一章", "目录", "-/-"))
    started = time.monotonic()
    assert vsp.legado_replace_regex(text, HONGXIU_CHAIN + "##") == text
    assert vsp.legado_replace_regex(text, HONGXIU_CHAIN + "####") == ""  # replaceFirst: no match -> ""
    assert time.monotonic() - started < 1.0


def test_prefilter_leaves_results_identical_to_plain_re_with_and_without_the_literal() -> None:
    import random

    rng = random.Random(158)
    pieces = ["阅读上一章", "目录", "-/-", " ", "\n", "abc", "正文", "x", "ABC", "目", "录", "-", "/"]
    chains = [
        HONGXIU_CHAIN + "##",
        HONGXIU_CHAIN + "##[$]##",
        r"目录.*\s|-/-##<>",
        r"abc|xyz##[\0]",
        r"(abc)|(目录)##$1$2",
        r"ab(c)d|目录##$1",
        r"(?i)abc|XYZ##_",
        r"^目录|正文$###",
        r"a.c|-/-##",
    ]
    for _ in range(60):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 40)))
        for chain in chains:
            expected = _plain_replace(text, chain)
            assert vsp.legado_replace_regex(text, chain) == expected, (text, chain)


def test_prefilter_replace_first_ignore_case_and_group_branches() -> None:
    with_literal = "head -/- tail"
    # replaceFirst: no possible match -> "", a present literal still replaces only the first match
    assert vsp.legado_replace_regex("plain text", r"目录.*|-/-##X####") == ""
    assert vsp.legado_replace_regex(with_literal, r"目录.*|-/-##X####") == "X"
    # IGNORECASE: the literal is looked up case-insensitively, so a differently cased text still matches
    assert vsp.legado_replace_regex("say ABC now", r"(?i)abc|xyz##_") == "say _ now"
    assert vsp.legado_replace_regex("say nothing", r"(?i)abc|xyz##_") == "say nothing"
    # a surviving branch with groups keeps its numbering when the replacement uses `$1`
    assert vsp.legado_replace_regex("ab-c", r"(a)b|(x)y-c##[$1]") == "[a]-c"
    assert vsp.legado_replace_regex("xy-c ab", r"(a)b|(x)y-c##[$1][$2]") == "[][x] [a][]"


def test_prefilter_rebuilds_only_when_numbering_cannot_change() -> None:
    compiled = re.compile(r"abc|(目录)|zzz")
    can, runner = vsp.prefiltered_regex(compiled, "only abc here", False)
    assert can is True and runner is not compiled and runner.pattern == "abc"  # no `$n` in the replacement: surviving branches only
    can, runner = vsp.prefiltered_regex(compiled, "only abc here", True)
    assert can is True and runner is compiled  # `$n` with groups present: keep the original numbering
    can, _runner = vsp.prefiltered_regex(compiled, "nothing at all", False)
    assert can is False
    # patterns the analysis cannot reason about stay untouched
    backref = re.compile(r"(a)\1|bb")
    assert vsp.prefiltered_regex(backref, "no match here", False) == (True, backref)


def test_large_inputs_use_the_regex_module_when_it_is_installed(monkeypatch) -> None:
    pytest.importorskip("regex")
    text = ("段落" * 15000) + "-/-" + ("尾" * 100)
    assert len(text) > vsp.REGEX_LARGE_INPUT_CHARS
    compiled = re.compile(r"-/-")
    assert type(vsp.large_input_pattern(compiled, text)).__module__ in {"regex", "_regex"}
    assert vsp.large_input_pattern(compiled, "short") is compiled
    assert vsp.legado_replace_regex(text, "-/-##<>").count("<>") == 1
    assert vsp.regex_search(compiled, text).start() == 30000
    # without the module every input uses `re` as before
    monkeypatch.setattr(vsp, "_regex_module", None)
    assert vsp.large_input_pattern(compiled, text) is compiled
    assert vsp.legado_replace_regex(text, "-/-##<>").count("<>") == 1


def test_a_catastrophic_large_substitution_times_out_as_a_deadline_and_never_blocks_another_thread() -> None:
    pytest.importorskip("regex")
    import threading
    import time

    text = ("a" * 40_000) + "!"
    ticks: list[float] = []
    stop = threading.Event()

    def ticker() -> None:
        while not stop.is_set():
            ticks.append(time.monotonic())
            time.sleep(0.01)

    result: dict[str, Any] = {}

    def run() -> None:
        vsp.begin_source_validation_deadline(1.5)
        try:
            vsp.regex_sub(re.compile(r"(a+)+$b"), lambda m: "", text)
            result["outcome"] = "returned"
        except vsp.SourceValidationDeadlineExceeded:
            result["outcome"] = "deadline"
        finally:
            vsp.clear_source_validation_deadline()

    thread = threading.Thread(target=ticker, daemon=True)
    thread.start()
    worker = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(30)
    stop.set()
    assert result.get("outcome") == "deadline" and time.monotonic() - started < 10
    gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:])]
    assert gaps and max(gaps) < 0.5  # the GIL stayed free: the other thread kept ticking while the regex ran


def test_matches_pseudo_and_attribute_regex_use_the_same_search_helper() -> None:
    html = '<div id="a1"><p>x</p></div><div id="b2">y</div>'
    assert len(json.loads(vsp.select_elements_json("div[id~=^a\\d$]", html, BASE))) == 1
    assert len(json.loads(vsp.select_elements_json("div:matches(^y$)", html, BASE))) == 1
