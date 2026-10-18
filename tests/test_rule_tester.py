"""R146 WP-A: rule_tester (offline single-rule replay on saved response bodies)."""

from __future__ import annotations

import copy
import inspect
from types import SimpleNamespace

import pytest

import debug_fixture_site as fixture_site
from validator import debug_engine as de
from validator import rule_tester as rt

BASE = "http://rules.example"
APP = "1.1.1"

SEARCH_HTML = (
    '<html><body><ul class="result-list">'
    '<li><h3>我的老婆是女帝</h3><a href="/book/1">详情</a><span class="author">天命书生</span><img src="/static/cover1.jpg"></li>'
    '<li><h3>夜无疆</h3><a href="/book/2">详情</a><span class="author">远方客</span><img src="/static/cover2.jpg"></li>'
    "</ul></body></html>"
)
DETAIL_HTML = ('<html><body><h1>我的老婆是女帝</h1><span class="author">天命书生</span>'
               '<p class="intro">一个关于重生与权谋的故事。</p><a class="toc" href="/book/1/toc">章节目录</a></body></html>')
TOC_HTML = ('<html><body><ul id="list"><li><a href="/book/1/c1">第一章 重生</a></li><li><a href="/book/1/c2">第二章 起风</a></li>'
            '<li><a href="/book/1/c3">第三章 入局</a></li></ul><a class="next" href="/book/1/toc?p=2">下一页</a></body></html>')
CONTENT_HTML = ('<html><body><div id="content"><p>他睁开眼睛，发现自己躺在一间陌生的屋子里。</p>'
                "<p>天才一秒记住本站地址：www.fixture-site.com</p></div></body></html>")


@pytest.fixture()
def src() -> dict:
    return fixture_site.good_source(BASE)


def run(src: dict, field: str, body: str, **kwargs):
    kwargs.setdefault("base_url", BASE + "/search?kw=x")
    kwargs.setdefault("app_version", APP)
    return rt.test_rule(src, field, body=body, **kwargs)


def test_public_signature_matches_the_workbench_contract() -> None:
    names = set(inspect.signature(rt.test_rule).parameters)
    assert {"body", "base_url", "rule", "item_index", "app_version", "variables", "offline"} <= names
    result = run(fixture_site.good_source(BASE), "ruleSearch.bookList", SEARCH_HTML)
    for attribute in ("ok", "field", "rule", "kind", "values", "value", "count", "items_count", "elapsed_ms", "error",
                      "warnings", "data"):
        assert hasattr(result, attribute)
    assert result.to_dict()["field"] == "ruleSearch.bookList"


def test_stage_of_field_and_get_path() -> None:
    assert [rt.stage_of_field(p) for p in ("ruleSearch.name", "ruleExplore.bookUrl", "ruleBookInfo.tocUrl",
                                           "ruleToc.chapterUrl", "ruleContent.content", "x.y")] == [
        "search", "explore", "info", "toc", "content", ""]
    assert rt.get_path({"a": {"b": "c"}}, "a.b") == "c" and rt.get_path({"a": 1}, "a.b") is None
    assert rt.get_path({}, "a") is None


def test_list_rule(src: dict) -> None:
    result = run(src, "ruleSearch.bookList", SEARCH_HTML)
    assert result.ok and result.kind == "list" and result.items_count == 2 and result.count == 2
    assert "我的老婆是女帝" in result.values[0] and result.rule == "class.result-list@tag.li"
    bad = run(src, "ruleSearch.bookList", SEARCH_HTML, rule="class.nothing@tag.li")
    assert not bad.ok and bad.items_count == 0 and "没有匹配" in bad.error


@pytest.mark.parametrize("field, index, expected", [
    ("ruleSearch.name", 0, "我的老婆是女帝"),
    ("ruleSearch.name", 1, "夜无疆"),
    ("ruleSearch.author", 1, "远方客"),
    ("ruleSearch.bookUrl", 0, BASE + "/book/1"),
    ("ruleSearch.bookUrl", 1, BASE + "/book/2"),
    ("ruleSearch.coverUrl", 1, BASE + "/static/cover2.jpg"),
])
def test_search_item_fields(src: dict, field: str, index: int, expected: str) -> None:
    result = run(src, field, SEARCH_HTML, item_index=index)
    assert result.ok and result.value == expected and result.values == [expected] and result.count == 1
    assert result.items_count == 2 and result.data["itemIndex"] == index


def test_item_index_out_of_range_and_missing_list(src: dict) -> None:
    result = run(src, "ruleSearch.name", SEARCH_HTML, item_index=5)
    assert not result.ok and result.error == "列表只有 2 项"
    empty = run(src, "ruleSearch.name", "<html><body></body></html>")
    assert not empty.ok and "没有匹配" in empty.error
    src["ruleSearch"]["bookList"] = ""
    assert "为空" in run(src, "ruleSearch.name", SEARCH_HTML).error


def test_rule_override_beats_the_source_rule(src: dict) -> None:
    result = run(src, "ruleSearch.name", SEARCH_HTML, rule="class.author@text", item_index=0)
    assert result.ok and result.value == "天命书生" and result.rule == "class.author@text"
    assert run(src, "ruleSearch.name", SEARCH_HTML, rule="class.nope@text").ok is False


def test_book_info_fields(src: dict) -> None:
    base = BASE + "/book/1"
    toc = run(src, "ruleBookInfo.tocUrl", DETAIL_HTML, base_url=base)
    assert toc.ok and toc.kind == "url" and toc.value == BASE + "/book/1/toc"
    assert run(src, "ruleBookInfo.name", DETAIL_HTML, base_url=base).value == "我的老婆是女帝"
    assert run(src, "ruleBookInfo.intro", DETAIL_HTML, base_url=base).value == "一个关于重生与权谋的故事。"
    missing = run(src, "ruleBookInfo.author", "<html><body></body></html>", base_url=base)
    assert not missing.ok and missing.error == "规则没有返回结果"


def test_toc_fields(src: dict) -> None:
    base = BASE + "/book/1/toc"
    lst = run(src, "ruleToc.chapterList", TOC_HTML, base_url=base)
    assert lst.ok and lst.items_count == 3
    assert run(src, "ruleToc.chapterName", TOC_HTML, base_url=base, item_index=2).value == "第三章 入局"
    assert run(src, "ruleToc.chapterUrl", TOC_HTML, base_url=base, item_index=1).value == BASE + "/book/1/c2"
    pager = run(src, "ruleToc.nextTocUrl", TOC_HTML, base_url=base)
    assert pager.ok and pager.kind == "urlList" and pager.values == [BASE + "/book/1/toc?p=2"] and pager.count == 1


def test_content_fields(src: dict) -> None:
    base = BASE + "/book/1/c1"
    content = run(src, "ruleContent.content", CONTENT_HTML, base_url=base)
    assert content.ok and content.kind == "content" and "他睁开眼睛" in content.value
    assert content.count == content.data["chars"] and content.count > 10
    src["ruleContent"]["replaceRegex"] = "##天才一秒记住本站地址：[^\\n]*##"
    replaced = run(src, "ruleContent.replaceRegex", CONTENT_HTML, base_url=base)
    assert replaced.ok and "天才一秒" not in replaced.value and "他睁开眼睛" in replaced.value
    assert replaced.data["after"] < replaced.data["before"] and replaced.data.get("preparedWithContentRule") is True
    untouched = run(src, "ruleContent.replaceRegex", CONTENT_HTML, base_url=base, rule="##不存在的广告##")
    assert untouched.ok and any("没有改变" in w for w in untouched.warnings)


def test_plain_text_body_for_replace_regex(src: dict) -> None:
    result = run(src, "ruleContent.replaceRegex", "第一行\n广告：买买买\n第二行", base_url=BASE,
                 rule="##广告：.*##")
    assert result.ok and "广告" not in result.value and "第二行" in result.value
    assert "preparedWithContentRule" not in result.data


def test_json_source_item_fields() -> None:
    src = fixture_site.sources(BASE)["api"]
    body = ('{"data":{"items":[{"name":"我的老婆是女帝","url":"/book/1","author":"天命书生"},'
            '{"name":"夜无疆","url":"/book/2","author":"远方客"}]}}')
    assert run(src, "ruleSearch.bookList", body).items_count == 2
    assert run(src, "ruleSearch.name", body, item_index=1).value == "夜无疆"
    assert run(src, "ruleSearch.bookUrl", body, item_index=0).value == BASE + "/book/1"


@pytest.mark.parametrize("field, error", [("ruleSearch.nonexistent", "不支持的字段"), ("foo", "不支持的字段")])
def test_unsupported_field(src: dict, field: str, error: str) -> None:
    result = run(src, field, SEARCH_HTML)
    assert not result.ok and error in result.error


def test_empty_rule(src: dict) -> None:
    src["ruleBookInfo"]["intro"] = ""
    result = run(src, "ruleBookInfo.intro", DETAIL_HTML)
    assert not result.ok and result.error == "规则为空"


def test_never_raises(src: dict) -> None:
    assert run(src, "ruleSearch.name", None).ok is False  # type: ignore[arg-type]
    broken = run({"ruleSearch": "not a dict"}, "ruleSearch.bookList", SEARCH_HTML)
    assert not broken.ok and broken.error
    assert not run(src, "ruleSearch.name", SEARCH_HTML, item_index="x").ok or True  # type: ignore[arg-type]


def test_both_app_versions_agree_on_plain_rules(src: dict) -> None:
    for version in ("1.1.1", "1.1.2"):
        assert run(src, "ruleSearch.name", SEARCH_HTML, app_version=version).value == "我的老婆是女帝"


def test_variables_reach_the_rule(src: dict) -> None:
    src["ruleBookInfo"]["intro"] = "@get:{greeting}"
    result = run(src, "ruleBookInfo.intro", DETAIL_HTML, variables={"greeting": "你好"})
    assert result.ok and result.value == "你好"


def test_does_not_modify_the_source_or_the_variables(src: dict) -> None:
    snapshot = copy.deepcopy(src)
    variables = {"a": "1"}
    run(src, "ruleSearch.name", SEARCH_HTML, variables=variables)
    assert src == snapshot and variables == {"a": "1"}


def test_offline_blocks_rule_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK", "1")
    base, _alt, stop = fixture_site.start()
    try:
        src = fixture_site.good_source(base)
        src["ruleBookInfo"]["intro"] = f"@js:java.ajax('{base}/book/1')"
        fixture_site.REQUEST_LOG.clear()
        result = rt.test_rule(src, "ruleBookInfo.intro", body=DETAIL_HTML, base_url=base + "/book/1", app_version=APP,
                              offline=True)
        assert fixture_site.REQUEST_LOG == []
        assert any("网络请求" in w for w in result.warnings) and result.data.get("blockedRequests") == [base + "/book/1"]
        online = rt.test_rule(src, "ruleBookInfo.intro", body=DETAIL_HTML, base_url=base + "/book/1", app_version=APP,
                              offline=False)
        assert fixture_site.REQUEST_LOG and online.ok and "我的老婆是女帝" in online.value
    finally:
        stop()


# ---------------------------------------------------------------------------
# on a debug session
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def run_session():
    patch = pytest.MonkeyPatch()
    patch.setenv("READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK", "1")
    base, _alt, stop = fixture_site.start()
    try:
        source = fixture_site.good_source(base)
        result = de.debug_source(copy.deepcopy(source), "我的", None, app_version=APP)
        assert result.status == "pass"
        yield SimpleNamespace(src=source, session=result.session, base=base)
    finally:
        stop()
        patch.undo()


def test_test_rule_on_session_uses_the_saved_response_of_the_stage(run_session: SimpleNamespace) -> None:
    session, src = run_session.session, run_session.src
    name = rt.test_rule_on_session(src, session, "ruleSearch.name", item_index=1, app_version=APP)
    assert name.ok and name.value == "夜无疆"
    chapter = rt.test_rule_on_session(src, session, "ruleToc.chapterUrl", item_index=0, app_version=APP)
    # latest("toc") is the last saved toc page (page 2 of the fixture): item 0 there is chapter four
    assert chapter.ok and chapter.value == run_session.base + "/book/1/c4"
    content = rt.test_rule_on_session(src, session, "ruleContent.content", app_version=APP)
    assert content.ok and "他睁开眼睛" in content.value
    tweaked = rt.test_rule_on_session(src, session, "ruleSearch.name", rule="class.author@text", app_version=APP)
    assert tweaked.value == "天命书生"


def test_test_rule_on_session_without_a_saved_stage(run_session: SimpleNamespace) -> None:
    empty = de.DebugSession(app_version=APP)
    result = rt.test_rule_on_session(run_session.src, empty, "ruleToc.chapterName", app_version=APP)
    assert not result.ok and "没有保存" in result.error and "toc" in result.error
    # explore falls back to the saved search response
    explore = rt.test_rule_on_session(run_session.src, run_session.session, "ruleExplore.name", app_version=APP)
    assert explore.ok and explore.value == "我的老婆是女帝"


def test_evaluate_field_is_offline(run_session: SimpleNamespace) -> None:
    body = run_session.session.raw.latest("info").body
    result = rt.evaluate_field(run_session.src, "ruleBookInfo.intro", body=body, base_url=run_session.base + "/book/1",
                               app_version=APP)
    assert result.ok and result.value == "一个关于重生与权谋的故事。"
