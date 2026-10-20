"""R155b: detail-error heuristic relaxed to Legado behaviour, volume-key fallback, REV follow-ups from R155 (offline).

J    error-shaped JSON detail body fails only when nothing usable was extracted (App profile 1.1.2; 1.1.1 unchanged).
V    toc JSON fallback for `isVolume` does not read the key `volume` (a volume number in many APIs; App profile 1.1.2).
P2-3 Jsoup-style text escaping in element outerHtml (`&` `<` `>` U+00A0).
P3-3 leading single `.` field rule on JSON: deep scan, with the `..` / `.[` / `.` / `.*` exclusions (App profile 1.1.2).
P3-5 `http://host` vs `http://host/` is not a redirect.
P3-8 `{{` template rules are not Legado element rules.
P3-9 book-info `@put` evaluation order = Legado BookInfo order, each field once.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from validator import app_profiles
from validator import validate_source_packages as vsp

BASE = "https://example.test"


def _runtime(base: str = f"{BASE}/") -> vsp.RuleRuntime:
    return vsp.RuleRuntime(source_url=base, base_url=base, book_source_url=BASE, headers={})


class Site:
    """Patched ``fetch_text``: answers by exact path, 404 for anything else."""

    def __init__(self, pages: dict[str, Any]) -> None:
        self.pages = dict(pages)
        self.log: list[str] = []

    def __call__(self, session: requests.Session, url: str, **kwargs: Any) -> tuple[str, str, int]:
        path = url.replace(BASE, "")
        self.log.append(path)
        page = self.pages.get(path)
        if page is None:
            return "", url, 404
        return page, url, 200


def _fetch_info(monkeypatch, body: str, rule: dict[str, Any], version: str) -> tuple[bool, str, str, str]:
    monkeypatch.setattr(vsp, "fetch_text", Site({"/book/1": body}))
    src = {"bookSourceName": "R155b J", "bookSourceUrl": BASE, "ruleBookInfo": rule}
    with vsp.app_profile_scope(version):
        return vsp.fetch_book_info(requests.Session(), src, _runtime(), f"{BASE}/book/1")


# ------------------------------------------------------------------------------------------------------------------- J


def test_j_error_json_with_an_extracted_name_is_a_detail_in_112_and_still_an_error_in_111(monkeypatch) -> None:
    body = json.dumps({"success": False, "name": "错误页上的名字", "msg": "denied"}, ensure_ascii=False)
    ok, name, _toc, error = _fetch_info(monkeypatch, body, {"name": "$.name"}, "1.1.2")
    assert ok and name == "错误页上的名字" and error == ""
    ok, _name, _toc, error = _fetch_info(monkeypatch, body, {"name": "$.name"}, "1.1.1")
    assert not ok and error == "denied"


def test_j_error_json_with_a_toc_url_distinct_from_the_book_url_is_a_detail_in_112(monkeypatch) -> None:
    body = json.dumps({"success": False, "toc": "/toc/1", "msg": "denied"})
    rule = {"name": "$.missing", "tocUrl": "$.toc"}
    ok, _name, toc_url, error = _fetch_info(monkeypatch, body, rule, "1.1.2")
    assert ok and toc_url == f"{BASE}/toc/1" and error == ""
    ok, _name, _toc, error = _fetch_info(monkeypatch, body, rule, "1.1.1")
    assert not ok and error == "denied"


def test_j_error_json_with_nothing_extractable_still_fails_with_the_same_message_in_both_profiles(monkeypatch) -> None:
    body = json.dumps({"success": False, "msg": "denied"})
    for version in ("1.1.1", "1.1.2"):
        ok, name, toc_url, error = _fetch_info(monkeypatch, body, {"name": "$.missing"}, version)
        assert not ok and error == "denied", version
        assert name == "" and toc_url == ""
        ok, _name, _toc, error = _fetch_info(monkeypatch, json.dumps({"success": False}), {"name": "$.missing"}, version)
        assert not ok and error == "detail api error", version


def test_j_toc_url_equal_to_the_book_url_is_not_usable_info(monkeypatch) -> None:
    body = json.dumps({"success": False, "toc": "/book/1", "msg": "denied"})
    ok, _name, _toc, error = _fetch_info(monkeypatch, body, {"name": "$.missing", "tocUrl": "$.toc"}, "1.1.2")
    assert not ok and error == "denied"


def test_j_a_success_body_is_a_normal_detail_in_both_profiles(monkeypatch) -> None:
    body = json.dumps({"success": True, "name": "好书"}, ensure_ascii=False)
    for version in ("1.1.1", "1.1.2"):
        ok, name, _toc, error = _fetch_info(monkeypatch, body, {"name": "$.name"}, version)
        assert ok and name == "好书" and error == "", version


# ------------------------------------------------------------------------------------------------------------------- V


def _volume_toc(monkeypatch, item: dict[str, Any], version: str, extra_rule: dict[str, Any] | None = None) -> tuple:
    body = json.dumps({"data": [item]}, ensure_ascii=False)
    monkeypatch.setattr(vsp, "fetch_text", Site({"/toc": body}))
    toc_rule = {"chapterList": "$.data[*]", "chapterName": "$.title", "chapterUrl": "$.url"}
    toc_rule.update(extra_rule or {})
    src = {"bookSourceName": "R155b V", "bookSourceUrl": BASE, "ruleToc": toc_rule}
    with vsp.app_profile_scope(version):
        return vsp.resolve_toc_source(requests.Session(), src, vsp.make_runtime(src), f"{BASE}/book", f"{BASE}/toc")


def test_v_volume_number_key_is_not_a_volume_flag_in_112_only(monkeypatch) -> None:
    item = {"title": "第一章", "url": "/c/1", "volume": "1"}
    result = _volume_toc(monkeypatch, item, "1.1.2")
    assert result[0] is True and result[3] == "第一章"
    result = _volume_toc(monkeypatch, item, "1.1.1")
    assert result[0] is False  # unchanged: every chapter looks like a volume, the TOC ends up empty


def test_v_boolean_named_keys_still_mark_a_volume(monkeypatch) -> None:
    for key in ("isVolume", "is_volume", "isPart"):
        result = _volume_toc(monkeypatch, {"title": "卷一", "url": "/c/1", key: True}, "1.1.2")
        assert result[0] is False, key


def test_v_an_explicit_is_volume_rule_is_unaffected(monkeypatch) -> None:
    result = _volume_toc(monkeypatch, {"title": "卷一", "url": "/c/1", "kind": 1}, "1.1.2", {"isVolume": "$.kind"})
    assert result[0] is False


def test_profile_flags_j_and_v() -> None:
    assert app_profiles.APP_PROFILES["1.1.1"].detail_error_json_requires_empty_info is False
    assert app_profiles.APP_PROFILES["1.1.1"].toc_volume_key_excluded is False
    assert app_profiles.APP_PROFILES["1.1.2"].detail_error_json_requires_empty_info is True
    assert app_profiles.APP_PROFILES["1.1.2"].toc_volume_key_excluded is True


# --------------------------------------------------------------------------------------------------------------- P2-3


def test_p23_outer_html_escapes_text_nodes_like_jsoup() -> None:
    html = '<div class="x"><p>a &amp; b &lt;i&gt; c d</p></div>'
    assert vsp.evaluate_book_list("div.x", html, _runtime()) == ['<div class="x"><p>a &amp; b &lt;i&gt; c&nbsp;d</p></div>']
    # attribute values keep the R155 F behaviour
    quoted = '<a title=\'say "hi" & bye\'>t</a>'
    assert vsp.evaluate_book_list("a", quoted, _runtime()) == ['<a title="say &quot;hi&quot; &amp; bye">t</a>']


def test_p23_script_and_style_text_stay_raw() -> None:
    html = '<div class="x"><script>if (a < b && c > d) {}</script><style>a > b {}</style></div>'
    out = vsp.evaluate_book_list("div.x", html, _runtime())
    assert out == ['<div class="x"><script>if (a < b && c > d) {}</script><style>a > b {}</style></div>']


def test_p23_inner_html_text_is_escaped_too() -> None:
    html = "<div class=\"x\">1 &lt; 2 &amp; 3</div>"
    assert vsp.css_extract(html, "div.x", "html", None, BASE) == ["1 &lt; 2 &amp; 3"]


# --------------------------------------------------------------------------------------------------------------- P3-3

DEEP = json.dumps({"title": "X", "data": {"title": "Y", "list": [10, 20]}}, ensure_ascii=False)


def test_p33_leading_single_dot_field_rule_deep_scans_json_in_112() -> None:
    with vsp.app_profile_scope("1.1.2"):
        values = vsp.evaluate_json_rule(".title", DEEP, _runtime())
        assert values == ["X", "Y"]
        # field exclusions keep their own meaning: `..x` deep scan, `.[` root array, `.*` direct children
        assert vsp.evaluate_json_rule("..title", DEEP, _runtime()) == ["X", "Y"]
        assert vsp.evaluate_json_rule(".[0]", "[5, 6]", _runtime()) == ["5"]
        assert "X" in vsp.evaluate_json_rule(".*", DEEP, _runtime())


def test_p33_111_keeps_its_current_field_behaviour() -> None:
    with vsp.app_profile_scope("1.1.1"):
        assert vsp.evaluate_json_rule(".title", DEEP, _runtime()) == ["X", "Y"]


# --------------------------------------------------------------------------------------------------------------- P3-5


def test_p35_trailing_slash_is_not_a_redirect() -> None:
    differs = vsp.response_url_differs_from_request
    assert differs("http://host", "http://host/") is False
    assert differs("http://host/", "http://host") is False
    assert differs("http://host/a/", "http://host/a") is False
    assert differs("http://host/a", "http://host/b") is True
    assert differs("http://host/s?q=1", "http://host/") is True


# --------------------------------------------------------------------------------------------------------------- P3-8


def test_p38_template_rules_are_not_legado_element_rules() -> None:
    assert vsp.is_legado_element_rule("class.a@tag.b") is True
    assert vsp.is_legado_element_rule("{{baseUrl}}a@href") is False
    assert vsp.is_legado_element_rule("tag.a{{1+1}}") is False
    assert vsp.is_legado_element_rule("div.x") is False


# --------------------------------------------------------------------------------------------------------------- P3-9

ORDER_PAGE = '<h1>书</h1><i class="a">A</i><i class="k">K</i><i class="w">W</i>'


def _order_source(rule: dict[str, Any]) -> dict[str, Any]:
    return {"bookSourceName": "R155b P39", "bookSourceUrl": BASE, "ruleBookInfo": {"name": "h1@text", **rule}}


def test_p39_author_put_runs_before_kind_and_kind_before_word_count(monkeypatch) -> None:
    monkeypatch.setattr(vsp, "fetch_text", Site({"/book/1": ORDER_PAGE}))
    src = _order_source({
        "author": "@put:{seq:i.a@text}",
        "kind": "@put:{seq:i.k@text}",
        "wordCount": "@put:{seq:i.w@text}",
    })
    runtime = vsp.make_runtime(src)
    ok, _name, _toc, error = vsp._fetch_book_info(requests.Session(), src, runtime, f"{BASE}/book/1")
    assert ok, error
    assert (runtime.variables or {}).get("seq") == "W"
    # author and kind only: Legado order name, author, kind -> the kind put wins
    src = _order_source({"author": "@put:{seq:i.a@text}", "kind": "@put:{seq:i.k@text}"})
    runtime = vsp.make_runtime(src)
    ok, _name, _toc, error = vsp._fetch_book_info(requests.Session(), src, runtime, f"{BASE}/book/1")
    assert ok, error
    assert (runtime.variables or {}).get("seq") == "K"


def test_p39_a_side_effect_field_is_evaluated_once(monkeypatch) -> None:
    monkeypatch.setattr(vsp, "fetch_text", Site({"/book/1": ORDER_PAGE}))
    rules = {"author": "@put:{a:i.a@text}", "wordCount": "@put:{w:i.w@text}", "intro": "@put:{n:i.k@text}"}
    src = _order_source(rules)
    seen: list[str] = []
    real = vsp.evaluate_get_string

    def spy(rule: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(rule)
        return real(rule, *args, **kwargs)

    monkeypatch.setattr(vsp, "evaluate_get_string", spy)
    runtime = vsp.make_runtime(src)
    ok, _name, _toc, error = vsp._fetch_book_info(requests.Session(), src, runtime, f"{BASE}/book/1")
    assert ok, error
    for rule in rules.values():
        assert seen.count(rule) == 1, rule
    assert [r for r in seen if r in rules.values()] == [rules["author"], rules["wordCount"], rules["intro"]]
