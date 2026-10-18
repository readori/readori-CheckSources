"""R150: the last validator-vs-App differences found by the online re-validation (offline; patched fetches only).

B11   a rule holding `{{js}}` templates (newlines, nested `{}`, java.put, trailing `,{options}`) is a makeUpRule TEMPLATE;
      its expanded text is the rule result (RuleType.swift:413-416 evalPattern, :533-536 templateParts,
      RuleEvaluator.swift:665-722 expandTemplate). The tocUrl / bookUrl / nextTocUrl results then go through the URL
      options parser (`url,{method, body, headers}`).
EXP   the discovery request is an AnalyzeUrl request: `url,{options}` apply (BookSourceEngine+Flagship.swift:149-150, 195-200).
ERR   isLikelyDetailErrorJSON is narrower than the old port (BookSourceEngine.swift:1598-1636).

Every assertion states the App semantics it follows. The two source shapes below are the 创世中文网 / 爱下电子 tocUrl rules.
"""
from __future__ import annotations

import json
from typing import Any

import requests

from validator import validate_source_packages as vsp

BASE = "https://example.test"

# 创世中文网 tocUrl: a template literal with a newline, a tab and nested braces (`{"headers": {...} }`) inside one `{{…}}`.
CHUANGSHI_TOC = (
    '{{baseUrl.replace(/detail/,"chapter")+`,{\n\t"headers": {"User-Agent": "R150-UA/1.0"} }`}}'
)
# 爱下电子 tocUrl: `{{java.put…;"url"}}` (two statements, the last value is the text), literal `,{…}` options, and a second
# `{{…}}` inside the options body.
IXDZS_TOC = (
    '{{java.put("url",baseUrl);\n\t"https://ixdzs.tw/novel/clist/"}},{\n'
    '  "body": "bid={{baseUrl.match(/(\\d+).$/)[1]}}",\n  "method": "POST"\n}'
)


def _runtime(base: str = f"{BASE}/") -> vsp.RuleRuntime:
    return vsp.RuleRuntime(source_url=base, base_url=base, book_source_url=BASE, headers={})


class Fetch:
    """Replacement for ``vsp.fetch_text`` that records method / body / headers / charset and answers by URL substring."""

    def __init__(self, responses: dict[str, Any], default: str | None = None) -> None:
        self.responses = responses
        self.default = default
        self.calls: list[dict[str, Any]] = []

    def __call__(self, session: requests.Session, url: str, **kwargs: Any) -> tuple[str, str, int]:
        self.calls.append({
            "url": url,
            "method": str(kwargs.get("method") or "GET").upper(),
            "body": kwargs.get("body"),
            "headers": dict(kwargs.get("headers") or {}),
            "charset": kwargs.get("charset"),
        })
        for key, value in self.responses.items():
            if key in url:
                return value, url, 200
        if self.default is not None:
            return self.default, url, 200
        raise AssertionError(f"unexpected URL: {url}")


def _install(monkeypatch, responses: dict[str, Any], default: str | None = None) -> Fetch:
    fetch = Fetch(responses, default)
    monkeypatch.setattr(vsp, "fetch_text", fetch)
    return fetch


# ------------------------------------------------------------------------------------------- B11  template rules


def test_b11_template_with_newline_and_nested_braces_is_evaluated_to_url_plus_options() -> None:
    """evalPattern is `\\{\\{[\\w\\W]*?\\}\\}`: the inner text may contain newlines and single `}`; the expansion is the rule's
    result, `url,{options}` included (the old `[^}]+` stopped at the first `}` and left the template unevaluated)."""
    runtime = _runtime(f"{BASE}/detail/12345")
    values = vsp.evaluate_rule(CHUANGSHI_TOC, "<html></html>", runtime, "html")
    assert len(values) == 1
    url, options = vsp.parse_url_options(values[0])
    assert url == f"{BASE}/chapter/12345"
    assert options["headers"] == {"User-Agent": "R150-UA/1.0"}


def test_b11_java_put_template_followed_by_literal_options_and_a_second_template() -> None:
    """expandTemplate walks literal / expression parts in order; `java.put` stores the variable as a side effect and the
    LAST statement value is the expression's text. The `{{…}}` inside the literal options body expands as well."""
    runtime = _runtime("https://ixdzs.tw/read/12345/")
    values = vsp.evaluate_rule(IXDZS_TOC, "<html></html>", runtime, "html")
    assert values == ['https://ixdzs.tw/novel/clist/,{\n  "body": "bid=12345",\n  "method": "POST"\n}']
    assert (runtime.variables or {}).get("url") == "https://ixdzs.tw/read/12345/"
    url, options = vsp.parse_url_options(values[0])
    assert url == "https://ixdzs.tw/novel/clist/"
    assert options["method"] == "POST" and options["body"] == "bid=12345"


def test_b11_a_rule_with_a_template_is_literal_text_never_a_selector() -> None:
    """RuleType.swift:533-536: a SourceRule with evalPattern matches is `.template` — literal text plus expression values,
    and that string is the result. It is not handed to the CSS / XPath engines afterwards."""
    runtime = _runtime()
    page = "<html><body><p class='x'>hello</p></body></html>"
    assert vsp.evaluate_rule("{{1+1}}-x", page, runtime, "html") == ["2-x"]
    assert vsp.evaluate_rule("{{ 'a' + 'b' }}{{1+2}}", page, runtime, "html") == ["ab3"]
    # two templates in one rule end at their own first `}}`
    assert vsp.evaluate_rule("{{'a'}}/{{'b'}}", page, runtime, "html") == ["a/b"]
    # an ordinary selector without any `{{…}}` is still a selector
    assert vsp.evaluate_rule("p.x@text", page, runtime, "html") == ["hello"]


def test_b11_result_placeholder_still_reads_the_previous_segment() -> None:
    """RuleEvaluator.swift:689-692: `{{result}}` is the template's input."""
    item = json.dumps({"bid": "50765373"})
    rule = "$.bid\n<js>1100000000+parseInt(result)</js>\nhttps://example.test/book?bookid={{result}}"
    assert vsp.evaluate_rule(rule, item, _runtime(), "json") == ["https://example.test/book?bookid=1150765373"]


def test_b11_book_url_and_chapter_url_templates_keep_their_options() -> None:
    """The same expansion serves bookUrl (search item) and chapterUrl rules; the option text survives to the request."""
    runtime = _runtime(f"{BASE}/")
    src = {"bookSourceName": "R150", "bookSourceUrl": BASE}
    rule = '{{"/book/"+JSON.parse(result).id+`,{\n"method": "POST", "body": "id=` + JSON.parse(result).id + `"}`}}'
    book_url = vsp.first_result_to_book_url(src, json.dumps({"id": 7}), rule, runtime)
    url, options = vsp.parse_url_options(book_url)
    assert url == f"{BASE}/book/7"
    assert options["method"] == "POST" and options["body"] == "id=7"


def _toc_source(toc_rule: str, **toc: Any) -> dict[str, Any]:
    return {
        "bookSourceName": "R150 toc",
        "bookSourceUrl": BASE,
        "ruleBookInfo": {"name": "h1@text", "tocUrl": toc_rule},
        "ruleToc": {"chapterList": ".toc li", "chapterName": "a@text", "chapterUrl": "a@href", **toc},
    }


def test_b11_detail_toc_url_template_reaches_the_toc_request_with_its_options(monkeypatch) -> None:
    """BookSourceEngine.swift:9123-9150 evaluates the tocUrl rule through the evaluator and keeps `url,{options}`; the
    chapter list is then requested with those options (POST body, headers) — both sample sources."""
    fetch = _install(monkeypatch, {
        "/novel/clist/": '<ul class="toc"><li><a href="/c/1">第一章</a></li></ul>',
        "/chapter/": '<ul class="toc"><li><a href="/c/1">第一章</a></li></ul>',
    }, default="<html><body><h1>测试书</h1></body></html>")

    src = _toc_source(IXDZS_TOC)
    runtime = vsp.make_runtime(src)
    ok, name, toc_url, error = vsp.fetch_book_info(requests.Session(), src, runtime, "https://ixdzs.tw/read/12345/")
    assert ok and name == "测试书" and error == ""
    assert toc_url.startswith("https://ixdzs.tw/novel/clist/,{")
    result = vsp.resolve_toc_source(requests.Session(), src, runtime, "https://ixdzs.tw/read/12345/", toc_url)
    assert result[0], result
    toc_call = [c for c in fetch.calls if "/novel/clist/" in c["url"]][-1]
    assert toc_call["method"] == "POST" and toc_call["body"] == "bid=12345" and "," not in toc_call["url"].rsplit("/", 1)[-1]

    fetch.calls.clear()
    src = _toc_source(CHUANGSHI_TOC)
    runtime = vsp.make_runtime(src)
    book = f"{BASE}/detail/12345"
    ok, name, toc_url, error = vsp.fetch_book_info(requests.Session(), src, runtime, book)
    assert ok and toc_url.startswith(f"{BASE}/chapter/12345,{{")
    result = vsp.resolve_toc_source(requests.Session(), src, runtime, book, toc_url)
    assert result[0], result
    toc_call = [c for c in fetch.calls if "/chapter/12345" in c["url"]][-1]
    assert toc_call["method"] == "GET" and toc_call["url"] == f"{BASE}/chapter/12345"
    assert toc_call["headers"].get("User-Agent") == "R150-UA/1.0"


def test_b11_next_toc_url_template_with_nested_braces_is_followed(monkeypatch) -> None:
    """nextTocUrl is evaluated like any other rule (RuleEvaluator `.template`): the second page is requested with the
    options of the template result."""
    rule = '{{baseUrl.indexOf("p2") < 0 ? baseUrl + `p2,{"headers": {"X-Page": "2"} }` : ""}}'
    fetch = _install(monkeypatch, {
        "p2": '<ul class="toc"><li><a href="/c/2">第二章</a></li></ul>',
    }, default='<ul class="toc"><li><a href="/c/1">第一章</a></li></ul>')
    src = _toc_source("", nextTocUrl=rule)
    src["ruleBookInfo"] = {"name": "h1@text"}
    runtime = vsp.make_runtime(src)
    result = vsp.resolve_toc_source(requests.Session(), src, runtime, f"{BASE}/book/1/", f"{BASE}/book/1/")
    assert result[0], result
    second = [c for c in fetch.calls if c["url"].endswith("p2")]
    assert second and second[0]["headers"].get("X-Page") == "2"


# ------------------------------------------------------------------------------------------- EXP  discovery request


EXPLORE_LIST = "<html><body><div class='b'><a href='/book/{n}'>书{n}</a></div></body></html>"


def _explore_source(explore_url: str) -> dict[str, Any]:
    return {
        "bookSourceName": "R150 explore",
        "bookSourceUrl": BASE,
        "exploreUrl": explore_url,
        "ruleExplore": {"bookList": ".b", "name": "a@text", "bookUrl": "a@href"},
    }


def _explore(monkeypatch, explore_text: str, **kwargs: Any) -> tuple[list[str], Fetch]:
    fetch = _install(monkeypatch, {}, default=EXPLORE_LIST.format(n=1))
    src = _explore_source(explore_text)
    runtime = vsp.make_runtime(src)
    urls, _base = vsp.fetch_book_candidates(requests.Session(), src, runtime, modes=("explore",), **kwargs)
    return urls, fetch


def test_explore_applies_method_body_and_headers_options(monkeypatch) -> None:
    """BookSourceEngine+Flagship.swift:149-150 + 195-200: the category is an AnalyzeUrl template — `url,{method, body,
    headers}` are applied and the options text is not part of the requested URL."""
    template = '热门::/rank?t=1,{"method": "POST", "body": "type=hot&page={{page}}", "headers": {"X-Test": "explore"}}'
    urls, fetch = _explore(monkeypatch, template)
    assert urls == [f"{BASE}/book/1"]
    call = fetch.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE}/rank?t=1"
    assert call["body"] == "type=hot&page=1"
    assert call["headers"].get("X-Test") == "explore"


def test_explore_passes_charset_and_resolves_page_templates_in_the_url(monkeypatch) -> None:
    template = '玄幻::/list/{{page}}.html,{"charset": "gbk"}'
    urls, fetch = _explore(monkeypatch, template)
    assert urls == [f"{BASE}/book/1"]
    assert fetch.calls[0]["url"] == f"{BASE}/list/1.html"
    assert fetch.calls[0]["charset"] == "gbk" and fetch.calls[0]["method"] == "GET"


def test_explore_plain_category_is_still_a_get(monkeypatch) -> None:
    urls, fetch = _explore(monkeypatch, "首页::/\n玄幻::/xuanhuan")
    assert urls == [f"{BASE}/book/1"]
    assert [c["method"] for c in fetch.calls] == ["GET"] and fetch.calls[0]["url"] == f"{BASE}/"


def test_explore_url_override_also_gets_its_options(monkeypatch) -> None:
    """The debug `explore_url` replaces the first category; it is the same AnalyzeUrl template."""
    urls, fetch = _explore(
        monkeypatch, "占位::/unused", explore_url='/rank,{"method": "POST", "body": "a=1"}')
    assert urls == [f"{BASE}/book/1"]
    assert fetch.calls[0]["method"] == "POST" and fetch.calls[0]["url"] == f"{BASE}/rank" and fetch.calls[0]["body"] == "a=1"


def test_explore_js_template_url_is_evaluated_before_the_options_are_split(monkeypatch) -> None:
    template = '榜单::{{"/rank/" + (1+1)}},{"headers": {"X-A": "b"} }'
    urls, fetch = _explore(monkeypatch, template)
    assert urls == [f"{BASE}/book/1"]
    assert fetch.calls[0]["url"] == f"{BASE}/rank/2" and fetch.calls[0]["headers"].get("X-A") == "b"


def test_first_explore_url_default_form_is_unchanged_for_static_checks() -> None:
    """`build_first_explore_url` without `raw_template` still returns the resolved absolute URL; the raw form returns the
    template untouched (the request path now resolves it through build_url_with_options)."""
    runtime = _runtime()
    text = "玄幻::/x/{{page}}.html,{\"method\": \"POST\"}"
    assert vsp.build_first_explore_url(text, runtime, resolve_categories=True).startswith(f"{BASE}/x/1.html")
    assert vsp.build_first_explore_url(text, runtime, resolve_categories=True, raw_template=True) == (
        '/x/{{page}}.html,{"method": "POST"}')


# ------------------------------------------------------------------------------------- ERR  detail error JSON


def test_error_json_code_alone_is_not_an_error() -> None:
    """BookSourceEngine.swift:1608-1615 comment: a business code outside 0 / 1 / 200 (10000, "OK" …) is not by itself an
    error; the old port flagged it."""
    assert not vsp.is_likely_detail_error_json({"code": 10000, "title": "t"})
    assert not vsp.is_likely_detail_error_json({"code": 500, "message": "something happened"})
    assert not vsp.is_likely_detail_error_json({"status": "weird", "msg": ""})


def test_error_json_bare_message_marker_without_a_code_is_not_an_error() -> None:
    """:1617-1631 the explicit failure message only counts together with an error-looking code (a book blurb containing
    "login" / "error" / "token" must not fail the detail stage; those loose markers existed only in the old port)."""
    assert not vsp.is_likely_detail_error_json({"msg": "login error token failed"})
    assert not vsp.is_likely_detail_error_json({"message": "未登录"})
    assert not vsp.is_likely_detail_error_json({"msg": "error", "code": 200})


def test_error_json_explicit_message_plus_error_code_is_an_error() -> None:
    assert vsp.is_likely_detail_error_json({"code": 401, "msg": "未登录"})
    assert vsp.is_likely_detail_error_json({"status": "fail", "message": "Token失效"})
    assert vsp.is_likely_detail_error_json({"errCode": 403, "error": "Forbidden"})
    assert vsp.is_likely_detail_error_json({"code": "E100", "reason": "请先登录"})
    # success codes (0 / 1 / 200 / "ok" / "success") never turn a message into an error
    for code in (0, 1, 200, "0", "1", "200", "OK", " success "):
        assert not vsp.is_likely_detail_error_json({"code": code, "msg": "未授权"}), code
    # bool is an NSNumber: true -> 1 (success), false -> 0 (success)
    assert not vsp.is_likely_detail_error_json({"code": True, "msg": "未登录"})


def test_error_json_success_false_is_an_error_regardless_of_message() -> None:
    assert vsp.is_likely_detail_error_json({"success": False})
    assert vsp.is_likely_detail_error_json({"success": False, "msg": "anything"})
    assert vsp.is_likely_detail_error_json({"success": 0})  # Swift `as? Bool` bridges the number 0
    assert not vsp.is_likely_detail_error_json({"success": "false"})
    # `success: true` does not exempt a body: the message + error-code pair still decides (the App has no success short-cut)
    assert vsp.is_likely_detail_error_json({"success": True, "msg": "未登录", "code": 500})


def test_error_json_non_empty_payload_wins_and_blank_strings_are_empty() -> None:
    """isEmptyJSONPayload trims strings; any of data / result / book / novel / info / item that is non-empty ends the check."""
    assert not vsp.is_likely_detail_error_json({"success": False, "data": {"name": "x"}})
    assert not vsp.is_likely_detail_error_json({"success": False, "item": [1]})
    assert vsp.is_likely_detail_error_json({"success": False, "data": "  \n", "result": {}, "book": None, "info": []})
    assert vsp.is_empty_json_payload("  \t") and vsp.is_empty_json_payload({}) and vsp.is_empty_json_payload(None)
    assert not vsp.is_empty_json_payload(0) and not vsp.is_empty_json_payload(False)


def test_error_json_code_and_message_lookup_follow_the_app() -> None:
    """`json["code"] ?? json["status"] ?? …` takes the first key that is PRESENT (a JSON null stops the chain and is not an
    error); jsonSearchFallbackString reads a string, a number, or the first element of an array, and skips objects."""
    assert not vsp.is_likely_detail_error_json({"code": None, "status": 500, "msg": "未登录"})
    assert vsp.is_likely_detail_error_json({"status": 500, "msg": "未登录"})
    assert vsp.is_likely_detail_error_json({"code": 401, "message": ["未授权", "x"]})
    assert not vsp.is_likely_detail_error_json({"code": 401, "message": {"text": "未授权"}})
    assert not vsp.is_likely_detail_error_json({"code": 401, "message": []})
    # error key order: msg, message, error, errorMsg, reason — the first NON-BLANK one is the message
    assert vsp.is_likely_detail_error_json({"code": 401, "msg": "  ", "error": "unauthorized"})
    assert not vsp.is_likely_detail_error_json(["not", "an", "object"])


def test_content_error_json_uses_the_same_detail_check() -> None:
    """BookSourceEngine.swift:4510-4532 isLikelyContentErrorJSON ends in isLikelyDetailErrorJSON."""
    assert vsp.is_likely_content_error_json('{"error": "boom"}')
    assert not vsp.is_likely_content_error_json('{"code": 500, "title": "x"}')
    assert vsp.is_likely_content_error_json('{"code": 401, "msg": "未登录"}')


def test_detail_error_json_fails_the_detail_stage_even_when_a_rule_extracts_a_name(monkeypatch) -> None:
    """BookSourceEngine.swift:8895-8898: the App throws for any JSON detail response that isLikelyDetailErrorJSON, before
    the rules run — the old gate (name AND tocUrl both empty) let it pass when a rule pulled a value out of the error."""
    body = json.dumps({"success": False, "name": "错误页上的名字", "msg": "denied"})
    _install(monkeypatch, {}, default=body)
    src = {"bookSourceName": "R150", "bookSourceUrl": BASE, "ruleBookInfo": {"name": "$.name"}}
    ok, name, toc_url, error = vsp.fetch_book_info(requests.Session(), src, _runtime(), f"{BASE}/book/1")
    assert not ok and error == "denied"
    # no message: the App's fallback text
    _install(monkeypatch, {}, default=json.dumps({"success": False}))
    ok, _name, _toc, error = vsp.fetch_book_info(requests.Session(), src, _runtime(), f"{BASE}/book/1")
    assert not ok and error == "detail api error"
    # a success=true body with the same rule is a normal detail
    _install(monkeypatch, {}, default=json.dumps({"success": True, "name": "好书"}))
    ok, name, _toc, error = vsp.fetch_book_info(requests.Session(), src, _runtime(), f"{BASE}/book/1")
    assert ok and name == "好书"


def test_detail_html_page_with_embedded_error_json_is_not_a_json_response(monkeypatch) -> None:
    page = '<html><head><title>x</title></head><body><h1 class="t">书名</h1><script>var s={"success":false};</script></body></html>'
    _install(monkeypatch, {}, default=page)
    src = {"bookSourceName": "R150", "bookSourceUrl": BASE, "ruleBookInfo": {"name": ".t@text"}}
    ok, name, _toc, error = vsp.fetch_book_info(requests.Session(), src, _runtime(), f"{BASE}/book/1")
    assert ok and name == "书名" and error == ""


# ---------------------------------------------------------------------------------------------- dead code removal


def test_dead_helpers_are_gone_and_the_live_connect_timeout_remains() -> None:
    """R149 removed the last callers of the login-title guess (the App has no such check) and nothing reads CONNECT_TIMEOUT;
    REQUEST_CONNECT_TIMEOUT is the one in use."""
    assert not hasattr(vsp, "is_likely_detail_login_page")
    assert not hasattr(vsp, "CONNECT_TIMEOUT")
    assert vsp.REQUEST_CONNECT_TIMEOUT > 0
