"""R146 WP-A: debug_engine (debug keys, RawStore, controller, debug_source, validate_one, compare_sources).

Everything runs against the local fixture site; no external network.
"""

from __future__ import annotations

import copy
import threading
import time
from types import SimpleNamespace

import pytest

import debug_fixture_site as fixture_site
from validator import debug_engine as de
from validator import debug_trace
from validator import validate_source_packages as vsp

APP = "1.1.1"


@pytest.fixture(scope="module")
def site():
    patch = pytest.MonkeyPatch()
    patch.setenv("READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK", "1")
    base, alt, stop = fixture_site.start()
    try:
        yield SimpleNamespace(base=base, alt=alt, sources=fixture_site.sources(base))
    finally:
        stop()
        patch.undo()


def _debug(site: SimpleNamespace, name: str, key: str, **kwargs):
    return de.debug_source(copy.deepcopy(site.sources[name]), key.replace("BASE", site.base), None,
                           app_version=kwargs.pop("app_version", APP), **kwargs)


# ---------------------------------------------------------------------------
# debug key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, kind, value, title", [
    ("", "auto", "", ""),
    ("   ", "auto", "", ""),
    ("我的", "search", "我的", ""),
    ("https://a.com/book/1", "info", "https://a.com/book/1", ""),
    ("HTTP://a.com/b", "info", "HTTP://a.com/b", ""),
    ("热门::/rank?p=1", "explore", "https://a.com/rank?p=1", "热门"),
    ("++/book/1/toc", "toc", "https://a.com/book/1/toc", ""),
    ("--/book/1/c1", "content", "https://a.com/book/1/c1", ""),
    ("++https://b.com/t", "toc", "https://b.com/t", ""),
])
def test_parse_debug_key(raw: str, kind: str, value: str, title: str) -> None:
    key = de.parse_debug_key(raw, "https://a.com")
    assert (key.kind, key.value, key.title) == (kind, value, title)


@pytest.mark.parametrize("raw", ["热门::", "++", "--", "  ++  "])
def test_parse_debug_key_rejects_missing_url(raw: str) -> None:
    with pytest.raises(ValueError):
        de.parse_debug_key(raw, "https://a.com")


# ---------------------------------------------------------------------------
# RawStore / DebugController
# ---------------------------------------------------------------------------

def test_raw_store_basics_latest_and_truncation() -> None:
    store = de.RawStore(per_entry_limit=10, total_limit=14)
    first = store.add(stage="search", url="u1", status=200, body="a" * 30)
    assert first.truncated and len(first.body) == 10 and first.size == 30 and first.raw_id == 1
    second = store.add(stage="search", url="u2", status=200, body="b" * 30)
    assert second.truncated and len(second.body) == 4  # total budget left
    third = store.add(stage="toc", url="u3", status=200, body="c")
    assert third.truncated and third.body == "" and third.raw_id == 3
    assert store.get(2) is second and store.get("2") is second and store.get(99) is None and store.get("x") is None
    assert store.latest("search") is second
    assert store.latest("toc") is None  # empty body is never "latest"
    assert store.latest("nope") is None
    assert [row["raw_id"] for row in store.summaries()] == [1, 2, 3]
    assert "body" not in store.summaries()[0]


def test_raw_store_latest_skips_failed_responses() -> None:
    store = de.RawStore()
    ok = store.add(stage="info", url="a", status=200, body="<p>ok</p>")
    store.add(stage="info", url="b", status=503, body="busy")
    store.add(stage="info", url="c", status=0, body="")
    assert store.latest("info") is ok


def test_raw_store_max_entries_keeps_the_listing() -> None:
    store = de.RawStore(max_entries=1)
    store.add(stage="s", url="a", status=200, body="x")
    over = store.add(stage="s", url="b", status=200, body="y")
    assert over.body == "" and over.truncated and len(store) == 2


def test_raw_entry_to_dict_truncates_to_max_bytes() -> None:
    entry = de.RawStore().add(stage="s", url="a", status=200, body="中" * 100)
    cut = entry.to_dict(max_bytes=10)
    assert cut["truncated"] is True and len(cut["body"].encode("utf-8")) <= 10
    assert entry.to_dict(include_body=False).get("body") is None
    assert entry.to_dict()["body"] == "中" * 100


def test_raw_store_save_and_load_round_trip(tmp_path) -> None:
    store = de.RawStore()
    store.add(stage="search", url="http://x/s", final_url="http://y/s", status=200, body="<ul></ul>", charset="utf-8",
              request_headers={"User-Agent": "t"}, response_headers={"Content-Type": "text/html"},
              history=[(301, "http://x/s")], elapsed_ms=12, attempt=1)
    store.add(stage="info", url="http://x/i", status=200, body='{"a": 1}')
    store.add(stage="content", url="http://x/c", status=200, body="plain text")
    store.add(stage="content", url="http://x/img", status=200, body=None, size=4, binary=True)
    target = store.save(tmp_path / "raw")
    assert (target / "index.json").is_file()
    assert sorted(p.name for p in target.iterdir()) == ["001-search.html", "002-info.json", "003-content.txt", "index.json"]
    loaded = de.RawStore.load(target)
    assert len(loaded) == 4
    one = loaded.get(1)
    assert one.final_url == "http://y/s" and one.body == "<ul></ul>" and one.charset == "utf-8" and one.attempt == 1
    assert one.history == [(301, "http://x/s")] and one.request_headers == {"User-Agent": "t"} and one.elapsed_ms == 12
    assert loaded.get(2).body == '{"a": 1}' and loaded.get(4).binary and loaded.get(4).size == 4
    assert loaded.latest("search").raw_id == 1


def test_controller_choice_cancel_and_timeout() -> None:
    controller = de.DebugController()
    assert controller.wait_choice(0.05) is None and not controller.waiting
    box: dict[str, object] = {}

    def waiter() -> None:
        box["choice"] = controller.wait_choice(5)

    thread = threading.Thread(target=waiter)
    thread.start()
    deadline = time.time() + 3
    while not controller.waiting and time.time() < deadline:
        time.sleep(0.01)
    assert controller.waiting
    controller.resume(2)
    thread.join(3)
    assert box["choice"] == 2 and not controller.waiting
    controller.cancel()
    assert controller.cancelled
    with pytest.raises(debug_trace.DebugCancelled):
        controller.wait_choice(1)


# ---------------------------------------------------------------------------
# debug_source
# ---------------------------------------------------------------------------

def test_search_key_runs_the_whole_chain(site: SimpleNamespace) -> None:
    result = _debug(site, "good", "我的")
    assert (result.status, result.ok, result.state) == ("pass", True, 1000)
    assert result.message.startswith("正文调试完成") and result.key.kind == "search"
    assert result.book_url == site.base + "/book/1" and result.toc_url == site.base + "/book/1/toc"
    assert result.chapter_url == site.base + "/book/1/c1" and result.chapter_title == "第一章 重生"
    session = result.session
    assert [e["seq"] for e in session.events] == list(range(1, len(session.events) + 1))
    assert all(isinstance(e["text"], str) for e in session.events)
    stages = {e["stage"] for e in session.events if e["kind"] == "fetch"}
    assert stages == {"search", "info", "toc", "content"}
    assert len(session.raw) == 5  # search, detail, two toc pages, one chapter
    assert session.raw.latest("toc").stage == "toc" and "章节目录" not in session.raw.latest("search").body
    assert session.events[-1]["kind"] == "verdict" and session.events[-1]["state"] == 1000
    # state numbers: a saved response of search / info / toc / content is 10 / 20 / 30 / 40
    states = {e["stage"]: e["state"] for e in session.events if e["kind"] == "fetch"}
    assert states == {"search": 10, "info": 20, "toc": 30, "content": 40}
    assert session.key.kind == "search" and session.source_name == "夹具站"
    log = "\n".join(e["text"] for e in session.events if e["text"])
    assert "我的老婆是女帝" in log and "第一章 重生" in log and "夹具站" in log


def test_to_dict_summary(site: SimpleNamespace) -> None:
    result = _debug(site, "good", "我的")
    summary = result.to_dict()
    assert summary["status"] == "pass" and summary["state"] == 1000 and summary["rawCount"] == 5
    assert summary["key"] == {"kind": "search", "value": "我的", "title": ""}
    assert "events" not in summary and len(result.to_dict(include_events=True)["events"]) == summary["eventCount"]


@pytest.mark.parametrize("key, kind, raw_stages", [
    ("BASE/book/1", "info", {"info", "toc", "content"}),
    ("++BASE/book/1/toc", "toc", {"toc", "content"}),
    ("--BASE/book/1/c1", "content", {"content"}),
    ("夹具::BASE/search?kw=a", "explore", {"explore", "info", "toc", "content"}),
])
def test_other_keys_start_in_their_stage(site: SimpleNamespace, key: str, kind: str, raw_stages: set[str]) -> None:
    result = _debug(site, "good", key)
    assert result.status == "pass" and result.key.kind == kind, result.message
    assert {entry.stage for entry in result.session.raw.entries} == raw_stages


def test_chain_false_stops_after_the_keyed_stage(site: SimpleNamespace) -> None:
    result = _debug(site, "good", "我的", chain=False)
    assert result.status == "pass" and result.message == "搜索调试完成：2 本书"
    assert {entry.stage for entry in result.session.raw.entries} == {"search"}
    assert _debug(site, "good", "BASE/book/1", chain=False).message == "详情调试完成"
    assert _debug(site, "good", "++BASE/book/1/toc", chain=False).message == "目录调试完成"


def test_pick_book_and_chapter(site: SimpleNamespace) -> None:
    result = _debug(site, "good", "我的", pick_book=1, pick_chapter=2)
    assert result.status == "pass"
    assert result.book_url == site.base + "/book/2"
    assert result.chapter_url == site.base + "/book/1/c3" and result.chapter_title == "第三章 入局"
    out_of_range = _debug(site, "good", "我的", pick_book=9)
    assert out_of_range.status == "pass" and out_of_range.book_url == site.base + "/book/1"
    assert any((e.get("data") or {}).get("code") == "pick_range" for e in out_of_range.session.events)


def test_auto_key_runs_the_batch_pipeline(site: SimpleNamespace) -> None:
    result = _debug(site, "good", "")
    assert (result.status, result.ok, result.message) == ("pass", True, "校验通过") and result.verdict["verdict"] == "pass"
    assert result.verdict["stages"] == {"search": True, "detail": True, "toc": True, "content": True}
    assert result.session.events[-1]["kind"] == "verdict" and result.session.events[-1]["state"] == 1000
    failed = _debug(site, "toc_broken", "")
    assert failed.status == "fail" and not failed.ok and failed.state == -1
    assert failed.verdict["failureReason"] == "toc failed" and failed.verdict["failureGroupZh"]
    assert failed.session.events[-1]["state"] == -1


@pytest.mark.parametrize("name, key, message", [
    ("search_broken", "我的", "︽未获取到书籍"),
    ("toc_broken", "BASE/book/1", "︽目录解析失败：没有取到任何章节"),
    ("content_broken", "--BASE/book/1/c1", "︽正文解析失败：没有取到可读正文"),
])
def test_failures_are_reported_not_raised(site: SimpleNamespace, name: str, key: str, message: str) -> None:
    result = _debug(site, name, key)
    assert (result.status, result.ok, result.state) == ("fail", False, -1)
    assert result.message == message
    last = result.session.events[-1]
    assert last["kind"] == "verdict" and last["state"] == -1 and message in last["text"]


def test_bad_key_is_an_error_result(site: SimpleNamespace) -> None:
    result = _debug(site, "good", "++")
    assert result.status == "error" and "链接不能为空" in result.message and len(result.session.raw) == 0


def test_cloudflare_search_is_not_a_plain_failure(site: SimpleNamespace) -> None:
    result = _debug(site, "cloudflare", "我的")
    assert not result.ok and result.status in ("needsInteraction", "fail")
    # the fixture answers 503: the engine reports the HTTP error and keeps the saved response for inspection
    codes = {(e.get("data") or {}).get("code") for e in result.session.events}
    assert "challenge" in codes or "http_error" in codes or result.status == "needsInteraction"
    fetch = next(e for e in result.session.events if e["kind"] == "fetch")
    assert fetch["status"] == 503 and "Just a moment" in result.session.raw.get(fetch["raw_id"]).body


def test_disabled_source_warns_but_still_debugs(site: SimpleNamespace) -> None:
    result = _debug(site, "disabled", "我的")
    assert result.status == "pass"
    assert any((e.get("data") or {}).get("code") == "disabled" for e in result.session.events)


def test_hard_timeout_is_reported_as_timeout(site: SimpleNamespace) -> None:
    started = time.monotonic()
    result = _debug(site, "slow", "我的", timeout=1)
    assert result.status == "timeout" and not result.ok and "超时" in result.message
    assert time.monotonic() - started < 6
    assert result.session.events[-1]["kind"] == "verdict"


def test_on_event_gets_every_event_and_cancel_ends_the_run(site: SimpleNamespace) -> None:
    seen: list[dict] = []
    result = de.debug_source(copy.deepcopy(site.sources["good"]), "我的", seen.append, app_version=APP)
    assert result.status == "pass" and len(seen) == len(result.session.events) and seen == result.session.events

    def stop_at_list(event: dict) -> None:
        if event["kind"] == "list":
            raise debug_trace.DebugCancelled()

    result = de.debug_source(copy.deepcopy(site.sources["good"]), "我的", stop_at_list, app_version=APP)
    assert result.status == "cancelled" and not result.ok and result.message == "■ 已停止"
    assert result.session.events[-1]["kind"] == "verdict"
    assert not any(e["stage"] == "info" for e in result.session.events)


def test_listener_exceptions_do_not_stop_the_run(site: SimpleNamespace) -> None:
    def broken(event: dict) -> None:
        raise RuntimeError("ui bug")

    result = de.debug_source(copy.deepcopy(site.sources["good"]), "我的", broken, app_version=APP)
    assert result.status == "pass"


def test_controller_cancel_before_start_ends_cancelled(site: SimpleNamespace) -> None:
    controller = de.DebugController()
    controller.cancel()
    result = _debug(site, "good", "我的", controller=controller)
    assert result.status == "cancelled"


def test_step_mode_pauses_for_book_and_chapter_choice(site: SimpleNamespace) -> None:
    controller = de.DebugController()
    holder: dict[str, de.DebugResult] = {}
    session = de.DebugSession(controller=controller, app_version=APP)

    def run() -> None:
        holder["result"] = de.debug_source(copy.deepcopy(site.sources["good"]), "我的", None, step=True,
                                           controller=controller, session=session, app_version=APP)

    thread = threading.Thread(target=run)
    thread.start()

    def wait_for_pause(count: int) -> dict:
        deadline = time.time() + 15
        while time.time() < deadline:
            pauses = [e for e in session.events if e["kind"] == "pause"]
            if len(pauses) >= count and controller.waiting:
                return pauses[count - 1]
            time.sleep(0.02)
        raise AssertionError("no pause event")

    book_pause = wait_for_pause(1)
    assert book_pause["data"]["what"] == "book" and book_pause["data"]["total"] == 2
    assert [c["name"] for c in book_pause["data"]["choices"]] == ["我的老婆是女帝", "夜无疆"]
    controller.resume(1)
    chapter_pause = wait_for_pause(2)
    assert chapter_pause["data"]["what"] == "chapter" and chapter_pause["data"]["total"] == 5
    controller.resume(4)
    thread.join(20)
    result = holder["result"]
    assert result.status == "pass"
    assert result.book_url == site.base + "/book/2"
    assert result.chapter_url == site.base + "/book/1/c5" and result.chapter_title == "第五章 余波"


def test_cancel_while_paused_stops_the_run(site: SimpleNamespace) -> None:
    controller = de.DebugController()
    session = de.DebugSession(controller=controller, app_version=APP)
    holder: dict[str, de.DebugResult] = {}
    thread = threading.Thread(target=lambda: holder.update(result=de.debug_source(
        copy.deepcopy(site.sources["good"]), "我的", None, step=True, controller=controller, session=session, app_version=APP)))
    thread.start()
    deadline = time.time() + 15
    while not controller.waiting and time.time() < deadline:
        time.sleep(0.02)
    controller.cancel()
    thread.join(10)
    assert holder["result"].status == "cancelled"


def test_events_are_capped_but_requests_and_verdict_survive(site: SimpleNamespace) -> None:
    session = de.DebugSession(max_events=12, app_version=APP)
    result = de.debug_source(copy.deepcopy(site.sources["good"]), "我的", None, session=session, app_version=APP)
    assert result.status == "pass" and session.dropped > 0
    assert any((e.get("data") or {}).get("code") == "events_truncated" for e in session.events)
    assert len([e for e in session.events if e["kind"] == "fetch"]) == 5
    assert session.events[-1]["kind"] == "verdict"


def test_item_log_lists_only_the_first_ten_items_per_stage(site: SimpleNamespace) -> None:
    src = copy.deepcopy(site.sources["good"])
    result = de.debug_source(src, "++BASE/book/1/toc".replace("BASE", site.base), None, app_version=APP, chain=False)
    items = [e for e in result.session.events if e["kind"] == "item" and e["stage"] == "toc"]
    assert len(items) == 5 and all(e["text"] for e in items)


def test_debug_does_not_touch_the_callers_source(site: SimpleNamespace) -> None:
    src = copy.deepcopy(site.sources["good"])
    snapshot = copy.deepcopy(src)
    de.debug_source(src, "我的", None, app_version=APP)
    de.validate_one(src, app_version=APP)
    assert src == snapshot


def test_two_app_versions_both_work(site: SimpleNamespace) -> None:
    for version in ("1.1.1", "1.1.2"):
        result = _debug(site, "good", "我的", app_version=version)
        assert result.status == "pass", version
        assert result.session.app_version == version
        assert version in result.session.events[0]["text"]


# ---------------------------------------------------------------------------
# validate_one / compare_sources
# ---------------------------------------------------------------------------

def test_validate_one_matches_the_batch_pipeline(site: SimpleNamespace) -> None:
    for name in ("good", "toc_broken", "search_broken", "content_broken"):
        src = copy.deepcopy(site.sources[name])
        one = de.validate_one(src, app_version=APP)
        from validator import app_profiles

        with app_profiles.app_profile_scope(APP):
            outcome, _record, detail = vsp.validate_single_source(copy.deepcopy(site.sources[name]))
        assert one["passed"] == outcome.passed, name
        assert one["failureReason"] == ("" if outcome.passed else detail["failureReason"]), name
        assert one["stages"] == detail["stages"], name


def test_validate_one_result_shape_and_trace(site: SimpleNamespace) -> None:
    result = de.validate_one(copy.deepcopy(site.sources["good"]), app_version=APP)
    assert result["verdict"] == "pass" and result["passed"] and result["failureReason"] == ""
    assert result["stabilityPassCount"] == 1 and result["appVersion"] == APP and result["record"]
    assert result["verdictZh"] and "__readoriValidation" not in result["record"]
    session = de.DebugSession(app_version=APP)
    traced = de.validate_one(copy.deepcopy(site.sources["toc_broken"]), app_version=APP, trace=session)
    assert not traced["passed"] and traced["verdict"] != "pass" and traced["failureReason"] == "toc failed"
    assert session.events[0]["kind"] == "info" and session.events[-1]["kind"] == "verdict"
    assert session.events[-1]["state"] == -1 and session.events[-1]["data"]["failureReason"] == "toc failed"


def test_validate_one_stability_rounds_run_fresh_searches(site: SimpleNamespace) -> None:
    fixture_site.REQUEST_LOG.clear()
    result = de.validate_one(copy.deepcopy(site.sources["good"]), app_version=APP, rounds=2)
    assert result["passed"] and result["stabilityPassCount"] == 2
    searches = [entry for entry in fixture_site.REQUEST_LOG if entry["path"].startswith("/search")]
    # quick scan searches once, the seeded full validation reuses its seed, the stability round searches afresh
    assert len(searches) == 2
    fixture_site.REQUEST_LOG.clear()
    assert de.validate_one(copy.deepcopy(site.sources["good"]), app_version=APP, rounds=1)["stabilityPassCount"] == 1
    assert len([e for e in fixture_site.REQUEST_LOG if e["path"].startswith("/search")]) == 1


def test_validate_one_quick_scan_deadline(site: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fixture_site, "SLOW_SECONDS", 2.0)
    started = time.monotonic()
    result = de.validate_one(copy.deepcopy(site.sources["slow"]), app_version=APP, quick_timeout=1)
    assert not result["passed"] and result["failureReason"] and not result["stages"].get("search")
    assert time.monotonic() - started < 8


def test_compare_sources_reports_improvements_and_regressions(site: SimpleNamespace) -> None:
    broken, good = site.sources["toc_broken"], site.sources["good"]
    fixed = de.compare_sources(broken, good, app_version=APP)
    assert fixed["changed"] and fixed["before"]["passed"] is False and fixed["after"]["passed"] is True
    changes = {row["stage"]: row["change"] for row in fixed["rows"]}
    assert changes["toc"] == "improved" and changes["verdict"] == "improved" and changes["search"] == "same"
    assert [row["label"] for row in fixed["rows"]] == ["快速扫描", "搜索", "详情", "目录", "正文", "结论"]
    worse = de.compare_sources(good, broken, app_version=APP)
    assert {row["stage"]: row["change"] for row in worse["rows"]}["toc"] == "regressed"
    same = de.compare_sources(good, good, app_version=APP)
    assert not same["changed"] and all(row["change"] == "same" for row in same["rows"])


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------

def test_failure_group_zh_has_text_for_known_and_unknown_reasons() -> None:
    assert de.failure_group_zh("toc failed")
    assert isinstance(de.failure_group_zh("something unforeseen"), str)
    assert de.VERDICT_ZH["pass"]


def test_render_header_names_source_version_and_key() -> None:
    key = de.parse_debug_key("我的", "https://a.com")
    text = de.render_header({"bookSourceName": "测试源", "bookSourceUrl": "https://a.com"}, "1.1.2", key)
    assert "测试源" in text and "1.1.2" in text and "我的" in text
