"""R146 WP-B: fix suggestion rules F1..F8 on hand-built debug sessions (offline, no network).

Sessions are faked (events + raw responses); online previews use a stub ``debug_engine``.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from validator import fix_suggester as fs
from validator import source_edit as se

OLD = "http://old.example.com"
NEW = "http://new.example.org"
PAD = "<p>" + "这是一段用来凑够可见文字数量的说明文字。" * 30 + "</p>"  # > 500 visible chars


# -- fake session -----------------------------------------------------------------------------

@dataclass
class FakeRaw:
    entries: list[dict] = field(default_factory=list)

    def get(self, raw_id: int) -> dict | None:
        return next((e for e in self.entries if e["raw_id"] == raw_id), None)

    def latest(self, stage: str) -> dict | None:
        best = None
        for entry in self.entries:
            if entry["stage"] == stage and 200 <= entry["status"] < 400 and entry["body"]:
                best = entry
        return best


@dataclass
class FakeSession:
    events: list[dict] = field(default_factory=list)
    raw: FakeRaw = field(default_factory=FakeRaw)
    variables: dict[str, str] = field(default_factory=dict)
    key: str = ""

    def add(self, kind: str, stage: str = "", **fields: Any) -> dict:
        event = {"seq": len(self.events), "stage": stage, "kind": kind, "state": 1, "data": {}}
        event.update(fields)
        self.events.append(event)
        return event

    def fetch(self, stage: str, url: str, *, status: int = 200, body: str = "", attempt: int = 0,
              history: list | None = None, resp_headers: dict | None = None, charset: str = "utf-8",
              method: str = "GET", final_url: str | None = None, req_body: str = "") -> dict:
        raw_id = len(self.raw.entries) + 1
        self.raw.entries.append({
            "raw_id": raw_id, "stage": stage, "url": url, "final_url": final_url or url, "method": method,
            "status": status, "request_headers": {}, "request_body": req_body,
            "response_headers": resp_headers or {}, "history": history or [], "charset": charset, "body": body,
        })
        return self.add("fetch", stage, url=url, method=method, attempt=attempt, status=status,
                        final_url=final_url or url, raw_id=raw_id, history=history or [], charset=charset)

    def fetch_error(self, stage: str, url: str, error: str) -> dict:
        return self.add("fetch_error", stage, url=url, method="GET", attempt=0, error=error)

    def list_event(self, stage: str, count: int, field_name: str = "bookList") -> dict:
        return self.add("list", stage, field=field_name, count=count)


def base_src() -> dict:
    return {
        "bookSourceUrl": OLD,
        "bookSourceName": "示例站",
        "searchUrl": "/search?q={{key}}",
        "header": '{"Referer":"http://old.example.com/"}',
        "ruleSearch": {"bookList": "class.result-list@tag.li", "name": "h3@text", "bookUrl": "a@href",
                       "coverUrl": "img@src"},
        "ruleBookInfo": {"name": "h1@text", "tocUrl": "a.toc@href"},
        "ruleToc": {"chapterList": "id.list@tag.dd", "chapterName": "a@text", "chapterUrl": "a@href"},
        "ruleContent": {"content": "id.content@html"},
    }


def codes(suggestions: list[fs.Suggestion]) -> list[str]:
    return [s.id for s in suggestions]


def by_code(suggestions: list[fs.Suggestion], code: str) -> fs.Suggestion:
    return next(s for s in suggestions if s.code == code)


@pytest.fixture(params=["fallback", "rule_tester"])
def tester_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Offline previews run once on the built-in replay and, once WP-A has merged, on ``rule_tester``."""
    if request.param == "fallback":
        monkeypatch.setattr(fs, "USE_RULE_TESTER", False)
    else:
        pytest.importorskip("validator.rule_tester")
        monkeypatch.setattr(fs, "USE_RULE_TESTER", True)
    return request.param


# -- F1 -------------------------------------------------------------------------------------

def _migration_session(count: int = 2, final_url: str = NEW + "/search?q=1", status: int = 200) -> FakeSession:
    s = FakeSession()
    for i in range(count):
        s.fetch("search" if i == 0 else "info", f"{OLD}/search?q={i}", status=status, body="<html>ok</html>",
                history=[(301, f"{OLD}/search?q={i}")], final_url=final_url.replace("q=1", f"q={i}"))
    return s


def test_f1_domain_migration_patch_lists_every_hit_path() -> None:
    src = base_src()
    src["ruleSearch"]["coverUrl"] = "img@src##//old.example.com/x"
    src["exploreUrl"] = "[\"//old.example.com/rank\", \"https://old.example.com/new\", \"https://sub.old.example.com/keep\"]"
    suggestions = fs.suggest(src, _migration_session())
    f1 = by_code(suggestions, "F1")
    assert f1.id == "F1" and f1.confidence == "high" and f1.preview_kind == "online" and f1.apply_allowed
    paths = [op["path"] for op in f1.patch]
    assert sorted(paths) == sorted(["bookSourceUrl", "header", "exploreUrl", "ruleSearch.coverUrl"])
    doc = se.SourceDocument(src)
    fs.apply_suggestion(doc, f1)
    assert doc.working["bookSourceUrl"] == NEW
    assert doc.working["header"] == '{"Referer":"http://new.example.org/"}'
    assert doc.working["ruleSearch"]["coverUrl"] == "img@src##//new.example.org/x"
    # protocol-relative and https variants move; a different sub-domain does not
    assert doc.working["exploreUrl"] == ('["//new.example.org/rank", "http://new.example.org/new", '
                                         '"https://sub.old.example.com/keep"]')
    assert doc.change_codes() == ["F1"]


def test_f1_medium_for_a_single_request_and_keep_source_url() -> None:
    suggestions = fs.suggest(base_src(), _migration_session(count=1), keep_source_url=True)
    f1 = by_code(suggestions, "F1")
    assert f1.confidence == "medium"
    assert "bookSourceUrl" not in [op["path"] for op in f1.patch]
    assert f1.meta["keep_source_url"] is True and f1.meta["changes_primary_key"] is False
    default = by_code(fs.suggest(base_src(), _migration_session(count=1)), "F1")
    assert default.meta["changes_primary_key"] is True


def test_f1_ignores_www_prefix_and_other_subdomain_hops() -> None:
    s = FakeSession()
    s.fetch("search", "http://example.com/s", body="x", history=[(301, "http://example.com/s")],
            final_url="http://www.example.com/s")
    s.fetch("search", "http://m.example.com/s", body="x", history=[(302, "http://m.example.com/s")],
            final_url="http://wap.example.com/s")
    assert fs.suggest(base_src(), s) == []


def test_f1_ignores_non_permanent_status_landing_page_and_failed_final() -> None:
    non_redirect = FakeSession()
    non_redirect.fetch("search", f"{OLD}/s", body="x", history=[(200, f"{OLD}/s")], final_url=NEW + "/s")
    assert fs.suggest(base_src(), non_redirect) == []
    landing = FakeSession()  # every URL ends on the new site's home page: a parked domain, not a migration
    landing.fetch("search", f"{OLD}/search?q=1", body="home", history=[(301, f"{OLD}/search?q=1")],
                  final_url=NEW + "/")
    assert fs.suggest(base_src(), landing) == []
    assert "F1" not in codes(fs.suggest(base_src(), _migration_session(status=403)))


def test_f1b_http_to_https_same_host() -> None:
    s = FakeSession()
    s.fetch("search", f"{OLD}/search?q=1", body="x", history=[(301, f"{OLD}/search?q=1")],
            final_url="https://old.example.com/search?q=1")
    suggestions = fs.suggest(base_src(), s)
    assert codes(suggestions) == ["F1b"]
    f1b = suggestions[0]
    assert f1b.code == "F1b"
    doc = se.SourceDocument(base_src())
    fs.apply_suggestion(doc, f1b)
    assert doc.working["bookSourceUrl"] == "https://old.example.com"
    assert doc.working["header"] == '{"Referer":"https://old.example.com/"}'


def test_f1_dead_domain_is_hint_only() -> None:
    s = FakeSession()
    s.fetch_error("search", f"{OLD}/search?q=1", "ConnectionError: NameResolutionError(getaddrinfo failed)")
    (hint,) = fs.suggest(base_src(), s)
    assert hint.id == "F1-dead" and hint.patch == [] and not hint.apply_allowed and hint.preview_kind == "none"
    assert "无法推断新域名" in hint.reason
    # a host that also answered successfully is not dead
    s.fetch("search", f"{OLD}/other", body="ok")
    assert fs.suggest(base_src(), s) == []


def test_f1_outranks_f2_and_f3() -> None:
    s = _migration_session()
    s.fetch("search", f"{OLD}/x", status=403, body="no", attempt=0)
    s.fetch("search", f"{OLD}/x", status=200, body="<html>yes</html>", attempt=1)
    result = fs.suggest(base_src(), s)
    assert "F3" not in codes(result) and "F1" in codes(result)


# -- F2 -------------------------------------------------------------------------------------

def _gbk_session(url: str = "http://old.example.com/search?q=%E6%88%91%E7%9A%84", body: str | None = None,
                 charset: str = "gbk", keyword: str = "我的", count: int = 0) -> FakeSession:
    s = FakeSession()
    s.add("keyword", "search", value=keyword)
    s.fetch("search", url, body=body if body is not None else "<html><meta charset=gbk>乱码</html>", charset=charset)
    s.list_event("search", count)
    return s


def test_f2_adds_charset_option_to_plain_search_url() -> None:
    (f2,) = fs.suggest(base_src(), _gbk_session())
    assert f2.code == "F2" and f2.preview_kind == "online" and f2.confidence == "high" and f2.apply_allowed
    assert f2.patch == [{"op": "set", "path": "searchUrl", "old": "/search?q={{key}}",
                         "new": '/search?q={{key}},{"charset":"gbk"}'}]
    assert f2.meta["charset"] == "gbk"


def test_f2_merges_into_existing_options_and_big5() -> None:
    src = base_src()
    src["searchUrl"] = '/search,{"method":"POST","body":"q={{key}}"}'
    s = _gbk_session(url="http://old.example.com/search", charset="big5")
    s.raw.entries[0]["request_body"] = "q=%E6%88%91%E7%9A%84"
    s.events[1]["method"] = "POST"
    (f2,) = fs.suggest(src, s)
    assert json.loads(f2.patch[0]["new"].split(",", 1)[1]) == {"method": "POST", "body": "q={{key}}", "charset": "big5"}


def test_f2_detects_charset_from_header_and_meta() -> None:
    s = _gbk_session(charset="utf-8", body="<html>乱码</html>")
    s.raw.entries[0]["response_headers"] = {"Content-Type": "text/html; charset=GB2312"}
    assert [x.code for x in fs.suggest(base_src(), s)] == ["F2"]
    s2 = _gbk_session(charset="", body='<meta http-equiv="Content-Type" content="text/html; charset=gb18030">')
    assert [x.code for x in fs.suggest(base_src(), s2)] == ["F2"]


def test_f2_negatives() -> None:
    assert fs.suggest(base_src(), _gbk_session(charset="utf-8", body="<html>干净</html>")) == []
    assert fs.suggest(base_src(), _gbk_session(keyword="abc", url="http://old.example.com/search?q=abc")) == []
    already = base_src()
    already["searchUrl"] = '/search?q={{key}},{"charset":"gbk"}'
    assert fs.suggest(already, _gbk_session()) == []
    ok_page = _gbk_session(body="<html><meta charset=gbk>我的 书</html>", count=3)
    assert fs.suggest(base_src(), ok_page) == []
    not_encoded = _gbk_session(url="http://old.example.com/search?q=%CE%D2%B5%C4")  # request already GBK-encoded
    assert fs.suggest(base_src(), not_encoded) == []


def test_f2_script_search_url_is_hint_only() -> None:
    src = base_src()
    src["searchUrl"] = "@js:'/search?q='+key"
    (f2,) = fs.suggest(src, _gbk_session())
    assert f2.patch == [] and not f2.apply_allowed and "charset" in f2.reason


def test_f2_lenient_options_keep_text() -> None:
    src = base_src()
    src["searchUrl"] = "/search?q={{key}},{'method':'GET'}"
    (f2,) = fs.suggest(src, _gbk_session())
    assert f2.patch[0]["new"] == "/search?q={{key}},{\"charset\":\"gbk\",'method':'GET'}"


# -- F3 -------------------------------------------------------------------------------------

def _ua_session(stage: str = "search", url: str = "http://old.example.com/search?q=1") -> FakeSession:
    s = FakeSession()
    s.fetch(stage, url, status=403, body="Forbidden", attempt=0)
    s.fetch(stage, url, status=200, body="<html><ul><li>ok</li></ul></html>", attempt=1)
    return s


def test_f3_retry_with_browser_headers_adds_user_agent() -> None:
    src = base_src()
    src["header"] = None
    del src["header"]
    (f3,) = fs.suggest(src, _ua_session())
    assert f3.code == "F3" and f3.confidence == "high" and f3.preview_kind == "online"
    new = json.loads(f3.patch[0]["new"])
    assert new["User-Agent"].startswith("Mozilla/5.0")
    assert f3.patch[0]["path"] == "header" and f3.patch[0]["old"] is None


def test_f3_replaces_non_browser_user_agent_and_keeps_dict_header() -> None:
    src = base_src()
    src["header"] = {"User-Agent": "python-requests/2", "X-Token": "abc"}
    (f3,) = fs.suggest(src, _ua_session())
    new = f3.patch[0]["new"]
    assert isinstance(new, dict) and new["X-Token"] == "abc" and new["User-Agent"].startswith("Mozilla/5.0")
    assert f3.patch[0]["old"] == {"User-Agent": "python-requests/2", "X-Token": "abc"}


def test_f3_adds_referer_for_forbidden_detail_page() -> None:
    src = base_src()
    src["header"] = '{"User-Agent":"Mozilla/5.0 X"}'
    (f3,) = fs.suggest(src, _ua_session(stage="toc", url="http://old.example.com/book/1/"))
    new = json.loads(f3.patch[0]["new"])
    assert new["Referer"] == "http://old.example.com/" and new["User-Agent"] == "Mozilla/5.0 X"


def test_f3_normalizes_lowercase_header_keys_on_403() -> None:
    src = base_src()
    src["header"] = '{"user-agent":"Mozilla/5.0 X","referer":"http://old.example.com/"}'
    s = FakeSession()
    s.fetch("search", "http://old.example.com/search?q=1", status=403, body="no")
    (f3,) = fs.suggest(src, s)
    assert f3.confidence == "medium"
    assert json.loads(f3.patch[0]["new"]) == {"User-Agent": "Mozilla/5.0 X", "Referer": "http://old.example.com/"}


def test_f3_weak_signal_adds_user_agent_when_header_has_none() -> None:
    src = base_src()
    del src["header"]
    s = FakeSession()
    s.fetch("search", "http://old.example.com/search?q=1", status=200, body="  ")
    (f3,) = fs.suggest(src, s)
    assert f3.confidence == "medium" and "User-Agent" in json.loads(f3.patch[0]["new"])


def test_f3_negatives() -> None:
    healthy = FakeSession()
    healthy.fetch("search", "http://old.example.com/search?q=1", body="<html>ok</html>")
    assert fs.suggest(base_src(), healthy) == []
    src = base_src()
    src["header"] = '{"User-Agent":"Mozilla/5.0 X"}'
    s = FakeSession()
    s.fetch("search", "http://old.example.com/search?q=1", status=403, body="no")  # UA fine, search stage: nothing to add
    assert fs.suggest(src, s) == []


def test_f3_script_header_is_hint_only() -> None:
    src = base_src()
    src["header"] = "@js:JSON.stringify({a:1})"
    (f3,) = fs.suggest(src, _ua_session())
    assert f3.patch == [] and not f3.apply_allowed and f3.preview_kind == "none"


# -- F8 -------------------------------------------------------------------------------------

def _cf_session(stage: str = "search") -> FakeSession:
    s = FakeSession()
    s.fetch(stage, "http://old.example.com/search?q=1", status=503, body="<title>Just a moment...</title>",
            resp_headers={"Server": "cloudflare", "CF-RAY": "abc-LAX"})
    return s


def test_f8_cloudflare_challenge_is_reported_never_patched() -> None:
    (f8,) = fs.suggest(base_src(), _cf_session())
    assert f8.code == "F8" and f8.patch == [] and f8.preview_kind == "none" and not f8.apply_allowed
    assert f8.confidence == "high" and "过盾" in f8.reason


def test_f8_challenge_variable_and_event() -> None:
    s = FakeSession()
    s.add("info", "search", code="challenge", url="http://old.example.com/s")
    (f8,) = fs.suggest(base_src(), s)
    assert f8.code == "F8" and f8.confidence == "medium" and f8.stage == "search"
    s2 = FakeSession(variables={"__searchChallenge": "1"})
    s2.add("begin", "search")
    assert [x.code for x in fs.suggest(base_src(), s2)] == ["F8"]


def test_f8_requires_cloudflare_evidence() -> None:
    s = FakeSession()
    s.fetch("search", "http://old.example.com/s", status=403, body="<title>Just a moment...</title>")  # no cf headers
    assert "F8" not in codes(fs.suggest(base_src(), s))
    s2 = FakeSession()
    s2.fetch("search", "http://old.example.com/s", status=403, body="denied", resp_headers={"server": "cloudflare"})
    assert "F8" not in codes(fs.suggest(base_src(), s2))


def test_f8_suppresses_f3_and_f4_in_its_stage() -> None:
    s = _cf_session()
    s.fetch("search", "http://old.example.com/s2", status=403, body="no", attempt=0)
    s.fetch("search", "http://old.example.com/s2", status=200, body="<html>ok</html>", attempt=1)
    s.list_event("search", 0)
    s.fetch("search", "http://old.example.com/s3", body=PAD + _result_list_html(5))
    assert codes(fs.suggest(base_src(), s)) == ["F8"]


def test_f8_does_not_block_other_stages() -> None:
    s = _cf_session(stage="search")
    s.fetch("info", "http://old.example.com/b/1", status=403, body="no", attempt=0)
    s.fetch("info", "http://old.example.com/b/1", status=200, body="<html>ok</html>", attempt=1)
    assert set(codes(fs.suggest(base_src(), s))) == {"F8", "F3"}


# -- F4 -------------------------------------------------------------------------------------

def _result_list_html(n: int, *, cls: str = "books", keyword: str = "我的") -> str:
    rows = "".join(
        f'<li class="book"><h3><a href="/book/{i}">{keyword}小说{i}</a></h3><span>作者{i}</span></li>' for i in range(n))
    return f'<div class="wrap"><ul class="{cls}">{rows}</ul></div>'


def test_f4_html_candidates_are_ranked_and_verified_offline(tester_mode: str) -> None:
    s = FakeSession()
    s.add("keyword", "search", value="我的")
    s.fetch("search", f"{OLD}/search?q=1", body=PAD + _result_list_html(6))
    s.list_event("search", 0)
    f4 = by_code(fs.suggest(base_src(), s), "F4")
    assert f4.id == "F4-search" and f4.preview_kind == "offline" and f4.apply_allowed
    assert f4.confidence == "medium"  # keyword found in the candidate's items
    new = f4.patch[0]["new"]
    assert f4.patch[0]["path"] == "ruleSearch.bookList" and f4.patch[0]["old"] == "class.result-list@tag.li"
    cands = f4.meta["candidates"]
    assert 1 <= len(cands) <= 5 and cands[0]["rule"] == new and cands[0]["count"] == 6
    assert {c["syntax"] for c in cands} >= {"legado", "css"}
    assert any(c["rule"].startswith("@CSS:") for c in cands)
    doc = se.SourceDocument(base_src())
    fs.apply_suggestion(doc, f4)
    assert doc.working["ruleSearch"]["bookList"] == new
    # preview proves it: count and the first title through the existing name rule
    outcome = fs.preview(base_src(), f4, s)
    assert outcome["ok"] is True and "6" in outcome["summary"] and "我的小说0" in outcome["summary"]


def test_f4_with_candidate_switches_rule_and_requires_new_preview() -> None:
    s = FakeSession()
    s.add("keyword", "search", value="我的")
    s.fetch("search", f"{OLD}/search?q=1", body=PAD + _result_list_html(6))
    s.list_event("search", 0)
    f4 = by_code(fs.suggest(base_src(), s), "F4")
    alt = fs.with_candidate(f4, len(f4.meta["candidates"]) - 1)
    assert alt.patch[0]["new"] == f4.meta["candidates"][-1]["rule"] and not alt.apply_allowed
    assert f4.patch[0]["new"] == f4.meta["candidates"][0]["rule"]  # original untouched
    with pytest.raises(IndexError):
        fs.with_candidate(f4, 99)


def test_f4_json_array_candidate() -> None:
    body = json.dumps({"data": {"items": [{"name": f"书{i}", "url": f"/b/{i}", "intro": "x" * 40} for i in range(5)]},
                       "pad": "说明" * 300}, ensure_ascii=False)
    s = FakeSession()
    s.fetch("search", f"{OLD}/api/search", body=body)
    s.list_event("search", 0)
    f4 = by_code(fs.suggest(base_src(), s), "F4")
    assert f4.patch[0]["new"] == "$.data.items[*]"
    assert f4.meta["candidates"][0]["keys"].startswith("name,url")
    assert f4.apply_allowed


def test_f4_toc_candidate_prefers_chapter_titles() -> None:
    rows = "".join(f'<dd><a href="/c/{i}.html">第{i}章 标题</a></dd>' for i in range(1, 8))
    nav = "".join(f'<li><a href="/n/{i}">导航{i}</a></li>' for i in range(5))
    body = PAD + f'<ul class="nav">{nav}</ul><div id="chapters">{rows}</div>'
    s = FakeSession()
    s.fetch("toc", f"{OLD}/book/1/", body=body)
    s.list_event("toc", 0, "chapterList")
    f4 = by_code(fs.suggest(base_src(), s), "F4")
    assert f4.id == "F4-toc" and f4.patch[0]["path"] == "ruleToc.chapterList"
    best = f4.meta["candidates"][0]
    assert best["count"] == 7 and best["hit"] == 1.0


def test_f4_negatives() -> None:
    short = FakeSession()
    short.fetch("search", f"{OLD}/s", body="<ul>" + "<li><a href='/a'>a</a></li>" * 5 + "</ul>")
    short.list_event("search", 0)
    assert fs.suggest(base_src(), short) == []
    works = FakeSession()
    works.fetch("search", f"{OLD}/s", body=PAD + _result_list_html(5))
    works.list_event("search", 0)
    works.list_event("search", 5)  # a later keyword produced a list
    assert fs.suggest(base_src(), works) == []
    err = FakeSession()
    err.fetch("search", f"{OLD}/s", body="<html><title>404 Not Found</title>" + PAD + _result_list_html(5) + "</html>")
    err.list_event("search", 0)
    assert fs.suggest(base_src(), err) == []
    nothing = FakeSession()
    nothing.fetch("search", f"{OLD}/s", body=PAD + "<div>没有任何重复结构</div>")
    nothing.list_event("search", 0)
    assert fs.suggest(base_src(), nothing) == []


# -- F5 -------------------------------------------------------------------------------------

def _cover_page(n: int = 3, lazy_items: int | None = None, attr: str = "data-original") -> str:
    lazy_items = n if lazy_items is None else lazy_items
    rows = ""
    for i in range(n):
        extra = f' {attr}="http://img.example.com/c{i}.jpg"' if i < lazy_items else ""
        rows += (f'<li><h3><a href="/book/{i}">书{i}</a></h3><img src="/images/loading.gif"{extra}></li>')
    return f'<div class="result-list"><ul>{rows}</ul></div>'


def _search_session(body: str) -> FakeSession:
    s = FakeSession()
    s.add("keyword", "search", value="书")
    s.fetch("search", f"{OLD}/search?q=1", body=body)
    s.list_event("search", 3)
    return s


def test_f5_lazy_cover_swaps_src_for_data_original(tester_mode: str) -> None:
    src = base_src()
    s = _search_session(_cover_page())
    f5 = by_code(fs.suggest(src, s), "F5")
    assert f5.preview_kind == "offline" and f5.apply_allowed and f5.confidence == "high"
    assert f5.patch == [{"op": "set", "path": "ruleSearch.coverUrl", "old": "img@src", "new": "img@data-original"}]
    outcome = fs.preview(src, f5, s)
    assert outcome["ok"] is True and outcome["after"] == ["http://img.example.com/c0.jpg"]
    assert outcome["before"][0].endswith("loading.gif") or "loading" in outcome["before"][0]


def test_f5_falls_back_to_or_rule_when_some_items_lack_the_attribute() -> None:
    f5 = by_code(fs.suggest(base_src(), _search_session(_cover_page(n=4, lazy_items=2))), "F5")
    assert f5.patch[0]["new"] == "img@data-original||img@src"


def test_f5_other_lazy_attributes_and_xpath_suffix() -> None:
    src = base_src()
    src["ruleSearch"]["coverUrl"] = "//img/@src"
    f5 = by_code(fs.suggest(src, _search_session(_cover_page(attr="data-src"))), "F5")
    assert f5.patch[0]["new"] == "//img/@data-src"


def test_f5_negatives() -> None:
    real = '<div class="result-list"><ul>' + "".join(
        f'<li><h3><a href="/b/{i}">书{i}</a></h3><img src="http://img.example.com/{i}.jpg"></li>' for i in range(3)
    ) + "</ul></div>"
    assert "F5" not in codes(fs.suggest(base_src(), _search_session(real)))
    no_lazy = '<div class="result-list"><ul>' + "".join(
        f'<li><h3><a href="/b/{i}">书{i}</a></h3><img src="/images/loading.gif"></li>' for i in range(3)) + "</ul></div>"
    assert "F5" not in codes(fs.suggest(base_src(), _search_session(no_lazy)))
    other_rule = base_src()
    other_rule["ruleSearch"]["coverUrl"] = "img@data-original"
    assert "F5" not in codes(fs.suggest(other_rule, _search_session(_cover_page())))
    js_rule = base_src()
    js_rule["ruleSearch"]["coverUrl"] = "img@src||@js:result"
    assert "F5" not in codes(fs.suggest(js_rule, _search_session(_cover_page())))


# -- F6 -------------------------------------------------------------------------------------

def _toc_page(chapters: int, pager: str) -> str:
    rows = "".join(f'<dd><a href="/c/{i}.html">第{i}章</a></dd>' for i in range(1, chapters + 1))
    return f'<div id="list">{rows}</div>{pager}'


def _toc_session(body: str, chapters: int, final_url: str = f"{OLD}/book/1/") -> FakeSession:
    s = FakeSession()
    s.fetch("toc", final_url, body=body, final_url=final_url)
    s.list_event("toc", chapters, "chapterList")
    for i in range(chapters):
        s.add("item", "toc", index=i, value=f"{OLD}/c/{i + 1}.html")
    return s


def test_f6_link_pager_adds_next_toc_url(tester_mode: str) -> None:
    body = _toc_page(20, '<div class="pg"><a href="/book/1/?p=2">下一页</a></div>')
    s = _toc_session(body, 20)
    f6 = by_code(fs.suggest(base_src(), s), "F6")
    assert f6.patch == [{"op": "set", "path": "ruleToc.nextTocUrl", "old": None, "new": "text.下一页@href"}]
    assert f6.preview_kind == "offline" and f6.apply_allowed and f6.confidence == "medium"
    assert "App" in f6.reason  # the validator guesses paging, the App does not
    outcome = fs.preview(base_src(), f6, s)
    assert outcome["ok"] and outcome["urls"] == [f"{OLD}/book/1/?p=2"]


def test_f6_select_pager_uses_css_option_values(tester_mode: str) -> None:
    pager = ('<select id="pageSel"><option value="/book/1/?p=1">第1-20章</option>'
             '<option value="/book/1/?p=2">第21-40章</option><option value="/book/1/?p=3">第41-60章</option></select>')
    s = _toc_session(_toc_page(20, pager), 20)
    f6 = by_code(fs.suggest(base_src(), s), "F6")
    assert f6.patch[0]["new"] == "@CSS:#pageSel option@value" and f6.meta["kind"] == "select"
    outcome = fs.preview(base_src(), f6, s)
    assert outcome["ok"] and len(outcome["urls"]) == 3


def test_f6_negatives() -> None:
    body = _toc_page(20, '<a href="/book/1/?p=2">下一页</a>')
    has_rule = base_src()
    has_rule["ruleToc"]["nextTocUrl"] = "text.下一页@href"
    assert "F6" not in codes(fs.suggest(has_rule, _toc_session(body, 20)))
    many = _toc_session(_toc_page(40, '<a href="/book/1/?p=2">下一页</a>'), 20)
    for i in range(20, 40):  # the validator already followed pages: plenty of unique chapters
        many.add("item", "toc", index=i, value=f"{OLD}/c/{i + 1}.html")
    assert "F6" not in codes(fs.suggest(base_src(), many))
    assert "F6" not in codes(fs.suggest(base_src(), _toc_session(_toc_page(20, "<p>没有分页</p>"), 20)))
    foreign = _toc_page(20, '<a href="http://other.example.net/p2">下一页</a>')
    assert "F6" not in codes(fs.suggest(base_src(), _toc_session(foreign, 20)))
    self_link = _toc_page(20, f'<a href="{OLD}/book/1/">下一页</a>')
    assert "F6" not in codes(fs.suggest(base_src(), _toc_session(self_link, 20)))


# -- F7 -------------------------------------------------------------------------------------

AD_TEXT = "第一段正文内容很长很长，" * 6 + "\n" + "天才一秒记住本站地址：www.abcde.com" + "\n" + "第二段正文内容很长很长，" * 6


def _content_session(text: str, pages: list[str] | None = None) -> FakeSession:
    s = FakeSession()
    body = f'<div id="content">{text.replace(chr(10), "<br>")}</div>'
    s.fetch("content", f"{OLD}/c/1.html", body=body)
    for page in pages or [text]:
        s.add("field", "content", field="content", value=page, count=len(page))
    return s


def test_f7_new_replace_regex_removes_ads(tester_mode: str) -> None:
    s = _content_session(AD_TEXT)
    f7 = by_code(fs.suggest(base_src(), s), "F7")
    assert f7.preview_kind == "offline" and f7.apply_allowed and f7.meta["target"] == "replaceRegex"
    op = f7.patch[0]
    assert op["path"] == "ruleContent.replaceRegex" and op["old"] is None and op["new"].startswith("##")
    assert "天才一秒记住" in op["new"] and "www" in op["new"]
    outcome = fs.preview(base_src(), f7, s)
    assert outcome["ok"] and outcome["after"]["chars"] < outcome["before"]["chars"]
    assert outcome["after"]["hits"] >= 2 and outcome["after"]["deletedRatio"] < 0.3


def test_f7_existing_replace_regex_appends_to_content_rule(tester_mode: str) -> None:
    src = base_src()
    src["ruleContent"]["replaceRegex"] = "##旧规则"
    s = _content_session(AD_TEXT)
    f7 = by_code(fs.suggest(src, s), "F7")
    assert f7.meta["target"] == "content"
    assert f7.patch[0]["path"] == "ruleContent.content" and f7.patch[0]["new"].startswith("id.content@html##")
    assert f7.apply_allowed
    # existing `##regex` content rule: alternate, do not stack a second `##`
    src["ruleContent"]["content"] = "id.content@html##旧"
    f7b = by_code(fs.suggest(src, s), "F7")
    assert f7b.patch[0]["new"].startswith("id.content@html##旧|") and f7b.patch[0]["new"].count("##") == 1


def test_f7_complex_content_rule_is_hint_only() -> None:
    src = base_src()
    src["ruleContent"]["replaceRegex"] = "##旧规则"
    src["ruleContent"]["content"] = "id.content@html##a##b"
    (f7,) = [x for x in fs.suggest(src, _content_session(AD_TEXT)) if x.code == "F7"]
    assert f7.patch == [] and f7.preview_kind == "none" and not f7.apply_allowed and f7.meta["target"] == "hint"
    src["ruleContent"]["content"] = "@js:result"
    (f7,) = [x for x in fs.suggest(src, _content_session(AD_TEXT)) if x.code == "F7"]
    assert f7.patch == []


def test_f7_refuses_when_more_than_30_percent_would_be_deleted() -> None:
    text = "短正文。\n天才一秒记住" + "字" * 50
    s = _content_session(text)
    f7 = by_code(fs.suggest(base_src(), s), "F7")
    assert not f7.apply_allowed
    outcome = fs.preview(base_src(), f7, s)
    assert outcome["apply_allowed"] is False and "不允许应用" in outcome["summary"]


def test_f7_repeated_lines_across_pages() -> None:
    page1 = "正文一\n" * 3 + "请收藏本站以便下次阅读哦亲\n" + "内容甲乙丙丁戊己庚辛" * 8
    page2 = "正文二\n" * 3 + "请收藏本站以便下次阅读哦亲\n" + "内容壬癸子丑寅卯辰巳" * 8
    s = _content_session(page1 + "\n" + page2, pages=[page1, page2])
    f7 = by_code(fs.suggest(base_src(), s), "F7")
    assert "重复行" in f7.meta["names"] and "请收藏本站以便下次阅读哦亲" in f7.patch[0]["new"]


def test_f7_negatives() -> None:
    assert "F7" not in codes(fs.suggest(base_src(), _content_session("干净的正文。" * 40)))
    single_repeat = _content_session("干净的正文。\n" * 5)  # one page: repeated lines are not evidence
    assert "F7" not in codes(fs.suggest(base_src(), single_repeat))
    s = FakeSession()
    s.add("begin", "content")
    assert fs.suggest(base_src(), s) == []


# -- suggest() as a whole -----------------------------------------------------------------------

def test_suggest_empty_session_and_ordering() -> None:
    assert fs.suggest(base_src(), FakeSession()) == []
    s = _content_session(AD_TEXT)
    s.fetch("toc", f"{OLD}/book/1/", body=_toc_page(20, '<a href="/book/1/?p=2">下一页</a>'))
    s.list_event("toc", 20, "chapterList")
    s.fetch("search", f"{OLD}/search?q=2", body="<html>x</html>", status=403, attempt=0)
    s.fetch("search", f"{OLD}/search?q=2", body="<html>ok</html>", status=200, attempt=1)
    s.fetch("search", f"{OLD}/search?q=1", body=_cover_page())  # latest successful search response
    s.list_event("search", 3)
    result = fs.suggest(base_src(), s)
    assert codes(result) == ["F3", "F5", "F6", "F7"]
    assert all(isinstance(json.dumps(x.to_dict(), ensure_ascii=False), str) for x in result)


def test_suggest_never_mutates_the_source_or_the_session() -> None:
    src = base_src()
    s = _content_session(AD_TEXT)
    before_src, before_events = copy.deepcopy(src), copy.deepcopy(s.events)
    fs.suggest(src, s)
    assert src == before_src and s.events == before_events


def test_one_broken_rule_does_not_hide_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(ctx: Any) -> list:
        raise RuntimeError("boom")

    monkeypatch.setattr(fs, "_f5", boom)
    s = _content_session(AD_TEXT)
    assert "F7" in codes(fs.suggest(base_src(), s))


def test_is_safe_means_offline_verified_with_a_patch() -> None:
    s = _content_session(AD_TEXT)
    f7 = by_code(fs.suggest(base_src(), s), "F7")
    assert fs.is_safe(f7)
    f7.apply_allowed = False
    assert not fs.is_safe(f7)
    assert not fs.is_safe(by_code(fs.suggest(base_src(), _cf_session()), "F8"))


def test_apply_suggestion_is_one_undo_unit_and_rejects_hints() -> None:
    src = base_src()
    s = _search_session(_cover_page())
    f5 = by_code(fs.suggest(src, s), "F5")
    doc = se.SourceDocument(src)
    fs.apply_suggestion(doc, f5)
    assert doc.changes()[0]["origin"] == "suggestion:F5" and doc.changes()[0]["reason"] == f5.title
    assert doc.undo() and doc.working == doc.original
    (f8,) = fs.suggest(src, _cf_session())
    with pytest.raises(ValueError):
        fs.apply_suggestion(doc, f8)


def test_apply_suggestion_detects_a_stale_patch() -> None:
    src = base_src()
    f5 = by_code(fs.suggest(src, _search_session(_cover_page())), "F5")
    edited = se.SourceDocument(src)
    edited.set("ruleSearch.coverUrl", "img@data-x")
    with pytest.raises(se.PatchConflict):
        fs.apply_suggestion(edited, f5)


def test_preview_without_patch_or_none_kind() -> None:
    (f8,) = fs.suggest(base_src(), _cf_session())
    outcome = fs.preview(base_src(), f8, _cf_session())
    assert outcome["ok"] is False and outcome["apply_allowed"] is False and "仅提示" in outcome["summary"]


# -- online previews (stub debug engine) ----------------------------------------------------------

class StubEngine:
    """Replays a canned run depending on what the patched source looks like."""

    def __init__(self, decide: Any) -> None:
        self.decide = decide
        self.calls: list[tuple[dict, str, dict]] = []

    def debug_source(self, src: dict, key: str, on_event: Any, **kwargs: Any) -> None:
        self.calls.append((src, key, kwargs))
        try:
            for event in self.decide(src, key):
                on_event(event)
        except BaseException as exc:  # the real engine ends the run on DebugCancelled (R146 WP-A)
            if type(exc).__name__ != "DebugCancelled":
                raise


def _search_run(count: int) -> list[dict]:
    return [{"kind": "list", "stage": "search", "field": "bookList", "count": count, "data": {}, "state": 1}]


def _auto_run(passed: bool) -> list[dict]:
    return [{"kind": "verdict", "stage": "", "state": 1000 if passed else -1, "data": {}}]


def test_f2_online_preview_reruns_with_patched_source(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = StubEngine(lambda src, key: _search_run(5 if '"charset":"gbk"' in src["searchUrl"] else 0))
    monkeypatch.setattr(fs, "_load_debug_engine", lambda: engine)
    s = _gbk_session()
    (f2,) = fs.suggest(base_src(), s)
    outcome = fs.preview(base_src(), f2, s, online=True, app_version="1.1.2")
    assert outcome["ok"] is True and outcome["apply_allowed"] is True
    assert outcome["before"]["bookListCount"] == 0 and outcome["after"]["bookListCount"] == 5
    (patched, key, kwargs), = engine.calls
    assert patched["searchUrl"].endswith('{"charset":"gbk"}') and key == "我的"
    assert kwargs["app_version"] == "1.1.2"
    assert base_src()["searchUrl"] == "/search?q={{key}}"


def test_online_preview_reports_failure_when_patch_does_not_help(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "_load_debug_engine", lambda: StubEngine(lambda src, key: _search_run(0)))
    s = _gbk_session()
    (f2,) = fs.suggest(base_src(), s)
    outcome = fs.preview(base_src(), f2, s, online=True)
    assert outcome["ok"] is False and outcome["apply_allowed"] is False


def test_online_preview_without_flag_or_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _gbk_session()
    (f2,) = fs.suggest(base_src(), s)
    assert fs.preview(base_src(), f2, s)["ok"] is None
    assert "联网" in fs.preview(base_src(), f2, s)["summary"]
    monkeypatch.setattr(fs, "_load_debug_engine", lambda: None)
    outcome = fs.preview(base_src(), f2, s, online=True)
    assert outcome["ok"] is None and "不可用" in outcome["summary"]


def test_f3_and_f1_online_preview_use_the_full_pipeline_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    def decide(src: dict, key: str) -> list[dict]:
        header = src.get("header")
        has_ua = isinstance(header, str) and "Mozilla" in header
        return _auto_run(has_ua)

    engine = StubEngine(decide)
    monkeypatch.setattr(fs, "_load_debug_engine", lambda: engine)
    src = base_src()
    del src["header"]
    s = _ua_session(stage="info", url=f"{OLD}/book/1")
    s.add("verdict", "", state=-1)
    (f3,) = fs.suggest(src, s)
    outcome = fs.preview(src, f3, s, online=True)
    assert outcome["ok"] is True and engine.calls[0][1] == ""  # auto key
    assert outcome["before"]["outcome"] == "fail" and outcome["after"]["outcome"] == "pass"

    def decide_f1(src_: dict, key: str) -> list[dict]:
        return _auto_run("new.example.org" in src_["bookSourceUrl"])

    monkeypatch.setattr(fs, "_load_debug_engine", lambda: StubEngine(decide_f1))
    s1 = _migration_session()
    f1 = by_code(fs.suggest(base_src(), s1), "F1")
    assert fs.preview(base_src(), f1, s1, online=True)["ok"] is True


def test_online_preview_survives_engine_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def debug_source(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("engine down")

    monkeypatch.setattr(fs, "_load_debug_engine", lambda: Broken())
    s = _gbk_session()
    (f2,) = fs.suggest(base_src(), s)
    outcome = fs.preview(base_src(), f2, s, online=True)
    assert outcome["ok"] is False and "engine down" in outcome["summary"]


# -- session adapter ---------------------------------------------------------------------------------

def test_session_adapter_reads_fields_from_data_and_raw_entry_objects() -> None:
    """Event fields may live in ``data``; the raw store may hand back objects instead of dicts."""

    @dataclass
    class Entry:
        raw_id: int
        stage: str
        url: str
        final_url: str
        method: str
        status: int
        request_headers: dict
        request_body: str
        response_headers: dict
        history: list
        charset: str
        body: str

    class Store:
        def __init__(self) -> None:
            self.entries = [Entry(1, "search", f"{OLD}/s", NEW + "/s", "GET", 200, {}, "", {}, [(301, f"{OLD}/s")],
                                  "utf-8", "<html>ok</html>"),
                            Entry(2, "info", f"{OLD}/b", NEW + "/b", "GET", 200, {}, "", {}, [{"status": 302, "url": f"{OLD}/b"}],
                                  "utf-8", "<html>ok</html>")]

        def get(self, raw_id: int) -> Entry | None:
            return next((e for e in self.entries if e.raw_id == raw_id), None)

        def latest(self, stage: str) -> Entry | None:
            return next((e for e in reversed(self.entries) if e.stage == stage), None)

    class Session:
        events = [
            {"seq": 0, "stage": "search", "kind": "fetch", "raw_id": 1, "data": {"attempt": 0, "method": "GET"}},
            {"seq": 1, "stage": "info", "kind": "fetch", "raw_id": 2, "data": {"attempt": 0, "method": "GET"}},
        ]
        raw = Store()

    f1 = by_code(fs.suggest(base_src(), Session()), "F1")
    assert f1.confidence == "high" and f1.meta["requests"] == 2


def test_rule_tester_result_objects_are_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    """When WP-A's ``rule_tester`` is present its dataclass result is read through ``getattr``."""

    @dataclasses.dataclass
    class Result:
        ok: bool = True
        field: str = "ruleToc.nextTocUrl"
        rule: str = "r"
        kind: str = "urlList"
        values: list = dataclasses.field(default_factory=lambda: ["http://old.example.com/book/1/?p=2"])
        value: str = "http://old.example.com/book/1/?p=2"
        count: int = 1
        items_count: int | None = None
        elapsed_ms: int = 3
        error: str = ""
        warnings: list = dataclasses.field(default_factory=list)

    class Tester:
        calls: list[dict] = []

        @staticmethod
        def test_rule(src: dict, field_path: str, **kwargs: Any) -> Result:
            Tester.calls.append({"field": field_path, **kwargs})
            return Result()

    monkeypatch.setattr(fs, "USE_RULE_TESTER", True)
    monkeypatch.setattr(fs, "_load_rule_tester", lambda: Tester)
    out = fs._run_rule(base_src(), "ruleToc.nextTocUrl", body="<a/>", base_url=f"{OLD}/book/1/", rule="r",
                       app_version="1.1.1")
    assert out["ok"] and out["values"] == ["http://old.example.com/book/1/?p=2"] and out["count"] == 1
    call = Tester.calls[0]
    assert call["offline"] is True and call["rule"] == "r" and call["app_version"] == "1.1.1" and call["item_index"] == 0


def test_run_rule_never_raises() -> None:
    out = fs._run_rule(base_src(), "ruleSearch.bookList", body="<html/>", base_url=OLD, rule="@js:throw 1")
    assert out["ok"] is False and isinstance(out["error"], str)
    unsupported = fs._fallback_rule_test(base_src(), "ruleFoo.bar", body="x", base_url=OLD, rule="a",
                                         item_index=0, variables=None)
    assert unsupported["ok"] is False and "不支持" in unsupported["error"]


# -- end to end: real debug engine against the local fixture site (WP-A) ---------------------------

@pytest.fixture(scope="module")
def fixture_site_env() -> Any:
    import debug_fixture_site as site

    patch = pytest.MonkeyPatch()
    patch.setenv("READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK", "1")
    base, alt, stop = site.start()
    try:
        yield dataclasses.make_dataclass("Env", ["base", "alt", "sources"])(base, alt, site.sources(base))
    finally:
        stop()
        patch.undo()


def _real_run(src: dict, key: str, version: str = "1.1.1") -> Any:
    from validator import debug_engine

    return debug_engine.debug_source(copy.deepcopy(src), key, None, app_version=version)


def _validates(src: dict, version: str = "1.1.1") -> bool:
    from validator import debug_engine

    return bool(debug_engine.validate_one(copy.deepcopy(src), app_version=version)["passed"])


def _apply(src: dict, suggestion: fs.Suggestion) -> dict:
    doc = se.SourceDocument(copy.deepcopy(src))
    fs.apply_suggestion(doc, suggestion)
    return doc.working


def test_f1_domain_migration_end_to_end(fixture_site_env: Any) -> None:
    src = fixture_site_env.sources["moved"]
    run = _real_run(src, "我的")
    assert run.status == "pass"  # the old address redirects, the chain still works
    f1 = by_code(fs.suggest(src, run.session), "F1")
    assert f1.preview_kind == "online" and f1.patch
    outcome = fs.preview(src, f1, run.session, online=True)
    assert outcome["ok"] is True and outcome["apply_allowed"] is True
    assert (outcome["after"]["bookListCount"] or 0) > 0
    assert _validates(_apply(src, f1))


def test_f2_gbk_charset_end_to_end(fixture_site_env: Any) -> None:
    src = fixture_site_env.sources["gbk_missing_charset"]
    run = _real_run(src, "我的")
    assert run.status == "fail" and run.message == "︽未获取到书籍"
    (f2,) = fs.suggest(src, run.session)
    assert f2.code == "F2" and f2.confidence == "high" and f2.patch[0]["new"].endswith('{"charset":"gbk"}')
    outcome = fs.preview(src, f2, run.session, online=True)
    assert outcome["ok"] is True and outcome["before"]["bookListCount"] == 0 and outcome["after"]["bookListCount"] == 2
    assert _validates(_apply(src, f2))
    assert not _validates(src)  # the unpatched source really is broken


def test_f3_referer_end_to_end(fixture_site_env: Any) -> None:
    src = fixture_site_env.sources["referer_missing"]
    run = _real_run(src, "我的")
    assert run.status == "fail"
    f3 = by_code(fs.suggest(src, run.session), "F3")
    assert f3.preview_kind == "online" and "Referer" in f3.patch[0]["new"]
    outcome = fs.preview(src, f3, run.session, online=True)
    assert outcome["ok"] is True and outcome["before"]["outcome"] == "fail" and outcome["after"]["outcome"] == "pass"
    assert _validates(_apply(src, f3))


def test_online_preview_does_not_bless_a_patch_that_does_not_help(fixture_site_env: Any) -> None:
    src = fixture_site_env.sources["ua_missing"]  # lower-case "user-agent" with a UA the site refuses
    run = _real_run(src, "我的")
    suggestions = [s for s in fs.suggest(src, run.session) if s.code == "F3"]
    assert suggestions, "F3 should notice the 403 and the lower-case key"
    outcome = fs.preview(src, suggestions[0], run.session, online=True)
    assert outcome["ok"] is False and outcome["apply_allowed"] is False


def test_online_preview_search_key_stops_after_the_list(fixture_site_env: Any) -> None:
    """The preview raises DebugCancelled from on_event at the first bookList event: the engine must return a result."""
    from validator import debug_engine, debug_trace

    src = fixture_site_env.sources["good"]
    seen: list[dict] = []

    def on_event(event: dict) -> None:
        seen.append(event)
        if event.get("kind") == "list" and event.get("field") == "bookList":
            raise debug_trace.DebugCancelled()

    result = debug_engine.debug_source(copy.deepcopy(src), "我的", on_event, app_version="1.1.1", timeout=30)
    assert result.status == "cancelled" and result.message == "■ 已停止"
    lists = [e for e in seen if e["kind"] == "list"]
    assert len(lists) == 1 and lists[0]["count"] == 2 and seen[-1]["kind"] == "verdict"
    assert not any(e.get("stage") == "info" for e in seen)


def test_suggest_on_a_healthy_run_offers_nothing_unsafe(fixture_site_env: Any) -> None:
    run = _real_run(fixture_site_env.sources["good"], "我的")
    assert run.status == "pass"
    unsafe = [s for s in fs.suggest(fixture_site_env.sources["good"], run.session) if fs.is_safe(s)]
    assert all(s.code in ("F5", "F6", "F7") for s in unsafe)


def test_session_contract_for_the_suggester(fixture_site_env: Any) -> None:
    """The duck-typed contract WP-B reads from a real ``DebugSession``."""
    run = _real_run(fixture_site_env.sources["good"], "我的")
    session = run.session
    assert isinstance(session.events, list) and all(isinstance(e, dict) for e in session.events)
    assert session.raw is session.raw_store and session.raw.get(1) is not None and session.raw.latest("search")
    assert isinstance(session.raw.entries, list) and isinstance(session.variables, dict)
    fetch = next(e for e in session.events if e["kind"] == "fetch")
    for name in ("attempt", "history", "response_headers", "charset"):
        assert name in fetch["data"], name
    assert fetch["raw_id"] == 1 and fetch["data"]["attempt"] == 0 and fetch["data"]["charset"] == "utf-8"
    keyword = next(e for e in session.events if e["kind"] == "keyword")
    assert keyword["value"] == "我的"
