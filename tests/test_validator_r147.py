"""R147: remaining validator problems (offline; 127.0.0.1 fixture site and monkeypatched fetches only).

Task numbers follow the R147 brief: 1 keywords, 2 discovery, 3 aggregation (F13), 4 min-pass rounds (L1), 5 abandoned
workers (L2), 6 swallowed exceptions (L7), 7 validate_group fingerprints (L10), 8 java.log + cancel points.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
import requests

import debug_fixture_site as fixture_site
from validator import app_profiles
from validator import debug_engine as de
from validator import debug_trace
from validator import validate_source_packages as vsp

BASE = "https://example.test"
needs_node = pytest.mark.skipif(not vsp.node_binary_path(), reason="node is not available")
needs_quickjs = pytest.mark.skipif(vsp.quickjs is None, reason="quickjs is not available")


# ------------------------------------------------------------------------------------------------ helpers


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


def _src(**overrides: Any) -> dict[str, Any]:
    src: dict[str, Any] = {"bookSourceName": "R147 Source", "bookSourceUrl": BASE}
    src.update(overrides)
    return src


def _candidates(src: dict[str, Any]) -> tuple[list[str], str, Any]:
    runtime = vsp.make_runtime(src)
    urls, base = vsp.fetch_book_candidates(requests.Session(), src, runtime)
    return urls, base, runtime


LIST = "<html><body><div class='b'><a href='/book/{n}'>书{n}</a></div></body></html>"
EMPTY = "<html><body><p>nothing here</p></body></html>"
RULES = {"bookList": ".b", "name": "a@text", "bookUrl": "a@href"}


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


# ----------------------------------------------------------------------------------------------- 1  keywords


def test_task1_app_validation_keywords_are_exactly_the_apps_single_keyword() -> None:
    """App BookSourceValidator.validationSearchKeyword (BookSourceValidator.swift:2682-2691)."""
    assert vsp.app_validation_keywords(None) == ["我的"]
    assert vsp.app_validation_keywords("") == ["我的"]
    assert vsp.app_validation_keywords(" \n\t") == ["我的"]
    assert vsp.app_validation_keywords(123) == ["我的"]
    assert vsp.app_validation_keywords("书") == ["书"]
    exact = "  exact signed keyword\n"
    assert vsp.app_validation_keywords(exact) == [exact]  # verbatim: no trim, no shortening
    assert vsp.APP_DEFAULT_CHECK_KEYWORD == "我的"


def test_task1_search_requests_one_keyword_and_never_rotates(monkeypatch) -> None:
    fetch = install(monkeypatch, default=EMPTY)
    src = _src(searchUrl=f"{BASE}/search?q={{{{key}}}}", ruleSearch=dict(RULES))
    urls, _base, _runtime = _candidates(src)
    assert urls == []
    assert [u for u in fetch.urls if "/search" in u] == [f"{BASE}/search?q=%E6%88%91%E7%9A%84"]  # 我的, once
    fetch = install(monkeypatch, default=EMPTY)
    src = _src(searchUrl=f"{BASE}/search?q={{{{key}}}}", ruleSearch=dict(RULES, checkKeyWord="凡人"))
    _candidates(src)
    assert [u for u in fetch.urls if "/search" in u] == [f"{BASE}/search?q=%E5%87%A1%E4%BA%BA"]


def test_task1_a_fallback_keyword_no_longer_rescues_an_empty_search(monkeypatch) -> None:
    """The old rotation tried 夜无疆 / 凡人修仙传 / ... after an empty 我的 and could pass a source the App fails."""
    page = LIST.format(n=1)
    install(monkeypatch, {"q=%E5%A4%9C%E6%97%A0%E7%96%86": page, "q=%E5%87%A1%E4%BA%BA%E4%BF%AE%E4%BB%99%E4%BC%A0": page},
            default=EMPTY)
    src = _src(searchUrl=f"{BASE}/search?q={{{{key}}}}", ruleSearch=dict(RULES))
    urls, _base, _runtime = _candidates(src)
    assert urls == []


# ----------------------------------------------------------------------------------------------- 2  discovery


def _both(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "searchUrl": f"{BASE}/search?q={{{{key}}}}", "exploreUrl": f"分类::{BASE}/explore",
        "ruleSearch": dict(RULES), "ruleExplore": dict(RULES),
    }
    fields.update(overrides)
    return _src(**fields)


def test_task2_search_passes_and_discovery_passes(monkeypatch) -> None:
    fetch = install(monkeypatch, {f"{BASE}/explore": LIST.format(n=7)}, default=LIST.format(n=1))
    urls, _base, runtime = _candidates(_both())
    assert urls == [f"{BASE}/book/1"]  # the search candidate stays the validated one
    assert (runtime.variables or {}).get("__exploreFailed") is None
    assert fetch.urls.count(f"{BASE}/explore") == 1  # discovery ran (App doCheckSource 2342-2392)


def test_task2_search_passes_but_discovery_returns_no_book_fails_the_source(monkeypatch) -> None:
    """App BookSourceValidator.swift:2373: `发现失效` is a failure group even when search works."""
    install(monkeypatch, {f"{BASE}/explore": EMPTY}, default=LIST.format(n=1))
    urls, _base, runtime = _candidates(_both())
    assert urls == [f"{BASE}/book/1"]
    assert (runtime.variables or {}).get("__exploreFailed") == "1"
    install(monkeypatch, {f"{BASE}/explore": EMPTY}, default=LIST.format(n=1))
    passed, seed, detail = vsp.quick_scan_single_source(_both())
    assert not passed and seed is None
    assert detail["failureReason"] == "explore failed" and detail["failureGroup"] == "发现失效"
    install(monkeypatch, {f"{BASE}/explore": EMPTY}, default=LIST.format(n=1))
    outcome, record, detail = vsp.validate_single_source(_both())
    assert not outcome.passed and record is None
    assert detail["failureReason"] == "explore failed" and detail["failureGroup"] == "发现失效"
    assert de.failure_group_zh(detail["failureReason"]) == "发现失效"
    assert app_profiles.app_verdict(detail, False, "1.1.1") == "invalid"


def test_task2_discovery_http_error_also_fails(monkeypatch) -> None:
    install(monkeypatch, {f"{BASE}/explore": ("<html><body>oops</body></html>", f"{BASE}/explore", 500)}, default=LIST.format(n=1))
    _urls, _base, runtime = _candidates(_both())
    assert (runtime.variables or {}).get("__exploreFailed") == "1"


def test_task2_link_less_category_list_is_only_an_info_label(monkeypatch) -> None:
    """App 2391: a category list without any link is `发现规则为空` (not a failure group)."""
    install(monkeypatch, default=LIST.format(n=1))
    src = _both(exploreUrl="分类::")
    urls, _base, runtime = _candidates(src)
    assert urls == [f"{BASE}/book/1"]
    assert (runtime.variables or {}).get("__exploreFailed") is None


def test_task2_discovery_does_not_replace_the_search_candidates_rule_variables(monkeypatch) -> None:
    install(monkeypatch, {f"{BASE}/explore": LIST.format(n=7)}, default=LIST.format(n=1))
    src = _both()
    src["ruleSearch"]["author"] = "a@text"
    src["ruleExplore"]["author"] = "a@href"

    def run(**kwargs: Any) -> tuple[list[str], str, dict[str, str]]:
        runtime = vsp.make_runtime(src)
        runtime.variables = {"k": "v"}
        urls, _base = vsp.fetch_book_candidates(requests.Session(), src, runtime, **kwargs)
        return urls, runtime.validation_book_author, dict(runtime.variables or {})

    both = run()
    search_only = run(modes=("search",))
    assert both[0] == search_only[0] == [f"{BASE}/book/1"]
    assert both[1] == search_only[1] == "书1"  # the search candidate's author, not the discovery candidate's
    assert both[2] == search_only[2]


def test_task2_discovery_only_source_is_judged_by_its_discovery(monkeypatch) -> None:
    """No searchUrl: the App records `搜索链接规则为空` (info only) and validates the discovery candidate."""
    explore_only = _src(exploreUrl=f"分类::{BASE}/explore", ruleExplore=dict(RULES))
    install(monkeypatch, {f"{BASE}/explore": LIST.format(n=7)})
    urls, _base, runtime = _candidates(explore_only)
    assert urls == [f"{BASE}/book/7"] and (runtime.variables or {}).get("__exploreFailed") is None
    install(monkeypatch, {f"{BASE}/explore": EMPTY})
    urls, _base, runtime = _candidates(explore_only)
    assert urls == [] and (runtime.variables or {}).get("__exploreFailed") is None  # plain "no result", not the new flag


def test_task2_debug_stage_filters_still_run_a_single_entry(monkeypatch) -> None:
    fetch = install(monkeypatch, default=LIST.format(n=1))
    src = _both()
    runtime = vsp.make_runtime(src)
    urls, _base = vsp.fetch_book_candidates(requests.Session(), src, runtime, keywords=["x"], modes=("search",))
    assert urls and all("/explore" not in u for u in fetch.urls)
    assert (runtime.variables or {}).get("__exploreFailed") is None


# ----------------------------------------------------------------------------------------------- 3  aggregation


def _validated(src: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    outcome, record, detail = vsp.validate_single_source(copy.deepcopy(src))
    assert outcome.passed and record is not None, detail.get("failureReason")
    return record, detail


def test_task3_quality_key_orders_stages_then_content_then_chapters() -> None:
    def entry(stages: int, chars: int, chapters: int) -> dict[str, Any]:
        return {"__readoriValidation": {"stagesPassed": stages, "contentChars": chars, "tocChapters": chapters}}

    key = vsp.candidate_quality_key
    assert key(entry(4, 1200, 5), None) > key(entry(4, 1300, 3), None)  # same size class (1200 ~ 1300): chapters decide
    assert key(entry(4, 800, 3), None) > key(entry(4, 0, 900), None)  # a stub / empty text loses to real text
    assert key(entry(4, 0, 0), None) > key(entry(3, 5000, 900), None)  # every stage passing beats everything else
    assert key({}, None) == (0, 0, 0)
    detail_only = {"stages": {"search": True, "detail": True, "toc": True, "content": True}, "contentPreviewChars": 640,
                   "tocUniqueChapterCount": 12}
    assert key({}, detail_only) == (4, 640 .bit_length(), 12)


def test_task3_aggregation_keeps_the_variant_that_really_worked_better() -> None:
    """Both variants carry the same customTag, so score_candidate cannot tell them apart; measured quality can."""
    def record(name: str, respond: int, **quality: int) -> dict[str, Any]:
        return {"bookSourceName": name, "bookSourceUrl": BASE, "searchUrl": "/s?q={{key}}", "respondTime": respond,
                "customTag": app_profiles.VALIDATED_PASS_TAG,
                "__readoriValidation": {"bookTitle": "同一本书", "bookAuthor": "作者", "sampleBookUrl": f"{BASE}/b/1",
                                        "stagesPassed": 4, **quality}}

    thin = record("thin", 100, contentChars=40, tocChapters=3)
    full = record("full", 9000, contentChars=3200, tocChapters=480)
    kept, stats = vsp.aggregate_validated_sources([("a", thin, None), ("b", full, None)])
    assert [item["bookSourceName"] for item in kept] == ["full"]
    assert stats["removed"] == 1 and stats["duplicateGroups"] == 1
    kept, _stats = vsp.aggregate_validated_sources([("b", full, None), ("a", thin, None)])
    assert [item["bookSourceName"] for item in kept] == ["full"]
    # without measurements the old order (score, respondTime, name) still decides
    plain_fast = {k: v for k, v in thin.items() if k != "__readoriValidation"} | {
        "__readoriValidation": {"bookTitle": "同一本书", "bookAuthor": "作者"}}
    plain_slow = dict(plain_fast, bookSourceName="slow", respondTime=500)
    plain_fast = dict(plain_fast, bookSourceName="fast", respondTime=10)
    kept, _stats = vsp.aggregate_validated_sources([("a", plain_slow, None), ("b", plain_fast, None)])
    assert [item["bookSourceName"] for item in kept] == ["fast"]


def test_task3_r139_broken_variants_fail_and_the_working_one_is_kept(site: SimpleNamespace) -> None:
    """R139 measured: D (every rule wrong) and E (empty content) both 'passed' and E was kept. After R144a both fail."""
    d_all_wrong = copy.deepcopy(site.sources["good"])
    d_all_wrong["bookSourceName"] = "D 规则全错"
    d_all_wrong["ruleSearch"].update(bookList="class.no-such-list@tag.li")
    d_all_wrong["ruleToc"].update(chapterList="id.no-such-toc@tag.li")
    d_all_wrong["ruleContent"].update(content="id.no-such-content@html")
    e_empty_content = site.sources["content_broken"]
    for broken in (d_all_wrong, e_empty_content):
        outcome, record, detail = vsp.validate_single_source(copy.deepcopy(broken))
        assert not outcome.passed and record is None, broken["bookSourceName"]
    entries = []
    for name in ("good", "d", "e"):
        src = {"good": site.sources["good"], "d": d_all_wrong, "e": e_empty_content}[name]
        outcome, record, detail = vsp.validate_single_source(copy.deepcopy(src))
        if outcome.passed:
            entries.append((name, record, detail))
    kept, _stats = vsp.aggregate_validated_sources(entries)
    assert [item["bookSourceName"] for item in kept] == [site.sources["good"]["bookSourceName"]]


def test_task3_both_legitimate_but_unequal_variants_keep_the_fuller_one(site: SimpleNamespace) -> None:
    full, full_detail = _validated(site.sources["good"])
    no_next = copy.deepcopy(site.sources["no_next_toc"])  # walks only the first TOC page: 3 of 5 chapters
    stub = copy.deepcopy(site.sources["good"])
    stub["bookSourceName"] = "夹具站·stub"
    stub["ruleContent"] = {"content": "id.content@tag.p.0@text"}  # a legal rule, but only the first paragraph (no ads)
    thin_toc, thin_toc_detail = _validated(no_next)
    thin_text, thin_text_detail = _validated(stub)
    assert full_detail["tocUniqueChapterCount"] > thin_toc_detail["tocUniqueChapterCount"]
    assert full_detail["contentPreviewChars"] > thin_text_detail["contentPreviewChars"]
    for first, second in (
        [("a", thin_toc, thin_toc_detail), ("b", full, full_detail)],
        [("a", full, full_detail), ("b", thin_toc, thin_toc_detail)],
        [("a", thin_text, thin_text_detail), ("b", full, full_detail)],
    ):
        thin_record = first[1] if first[1] is not full else second[1]
        thin_record["respondTime"] = 1  # the thin variant is also the faster one: speed must not decide
        kept, stats = vsp.aggregate_validated_sources([first, second])
        assert [item["bookSourceName"] for item in kept] == [full["bookSourceName"]], stats


# ----------------------------------------------------------------------------------------------- 4  min pass rounds


def test_task4_min_pass_rounds_means_the_first_n_rounds_in_a_row(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    plan = {
        "quick-scan": {"A", "B", "C", "D"},
        "full-validation": {"A", "B", "C", "D"},
        "stability-2": {"A", "C"},  # B and D fail round 2
        "stability-3": {"A"},       # C fails round 3
    }

    def fake_stage(stage, url_list, groups, max_workers, idle_timeout, source_timeout, handler):
        calls.append((stage, list(url_list)))
        passed = {url for url in url_list if url in plan[stage]}
        return passed, {url: {"bookSourceUrl": url} for url in passed}, {url: {"failureReason": ""} for url in url_list}, 0

    monkeypatch.setattr(vsp, "run_parallel_stage", fake_stage)
    urls = ["A", "B", "C", "D"]
    groups = {url: [{"bookSourceUrl": url}] for url in urls}
    strict = vsp._run_staged_stages(urls, groups, 2, 5, 5, 3, 3, 5)
    assert strict.final_urls == {"A"}
    loose = vsp._run_staged_stages(urls, groups, 2, 5, 5, 3, 2, 5)
    # first two rounds in a row: A (3 passes) and C (2 passes); B failed round 2 and is not counted for a later pass
    assert loose.final_urls == {"A", "C"} and loose.pass_counts["B"] == 1
    third_round = [urls_ for stage, urls_ in calls if stage == "stability-3"]
    assert third_round and all("B" not in urls_ and "D" not in urls_ for urls_ in third_round)  # never retested


def test_task4_help_text_describes_the_real_behavior() -> None:
    text = open(vsp.__file__, encoding="utf-8").read()
    marker = text[text.index('"--min-pass-rounds"'):][:400]
    assert "连续通过前" in marker and "不会再复测" in marker


# ----------------------------------------------------------------------------------------------- 5  abandoned workers


@pytest.fixture()
def clean_abandoned():
    """Abandoned-worker registry isolation: release anything a test left blocked and start from zero."""
    released: list[threading.Event] = []
    vsp.reset_abandoned_worker_count()
    yield released
    for event in released:
        event.set()
    deadline = time.monotonic() + 5
    while vsp.abandoned_workers_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    vsp.reset_abandoned_worker_count()


def _blocked_thread(released: list[threading.Event]) -> threading.Thread:
    event = threading.Event()
    released.append(event)
    thread = threading.Thread(target=event.wait, daemon=True)
    thread.start()
    return thread


def test_task5_abandoned_workers_reduce_the_next_stage_budget(clean_abandoned) -> None:
    assert vsp.stage_worker_budget(4, 10, wait_seconds=0) == 4
    first = _blocked_thread(clean_abandoned)
    second = _blocked_thread(clean_abandoned)
    assert vsp.note_abandoned_workers([first, second]) == 2
    assert vsp.note_abandoned_workers([first]) == 1  # already known: counted once
    assert vsp.abandoned_workers_total() == 2 and vsp.abandoned_workers_alive() == 2
    assert vsp.stage_worker_budget(4, 10, wait_seconds=0) == 2
    assert vsp.stage_worker_budget(4, 1, wait_seconds=0) == 1
    assert vsp.stage_worker_budget(2, 10, wait_seconds=0) == 1  # whole budget abandoned: still one worker moves on
    started = time.monotonic()
    assert vsp.stage_worker_budget(2, 10, wait_seconds=0.4) == 1 and time.monotonic() - started >= 0.35  # bounded wait
    for event in clean_abandoned:
        event.set()
    first.join(2)
    second.join(2)
    assert vsp.abandoned_workers_alive() == 0
    assert vsp.stage_worker_budget(4, 10, wait_seconds=0) == 4
    assert vsp.abandoned_workers_total() == 2  # the run total is kept for the report
    vsp.reset_abandoned_worker_count()
    assert vsp.abandoned_workers_total() == 0


def test_task5_idle_timeout_records_the_stuck_worker_and_the_next_stage_runs_narrower(clean_abandoned) -> None:
    release = threading.Event()
    clean_abandoned.append(release)
    groups = {"stuck": [{"bookSourceName": "s", "bookSourceUrl": "https://stuck.test"}],
              "ok": [{"bookSourceName": "o", "bookSourceUrl": "https://ok.test"}]}

    def handler(url: str):
        if url == "stuck":
            release.wait(30)
        return url, ({"bookSourceUrl": url} if url == "ok" else None), {"failureReason": ""}

    passed, _records, details, timed_out = vsp.run_parallel_stage("full-validation", ["stuck", "ok"], groups, 2, 1, 0, handler)
    assert passed == {"ok"} and timed_out == 1 and "stuck" in details
    assert vsp.abandoned_workers_total() == 1 and vsp.abandoned_workers_alive() == 1

    gauge = {"now": 0, "max": 0}
    lock = threading.Lock()

    def counting_handler(url: str):
        with lock:
            gauge["now"] += 1
            gauge["max"] = max(gauge["max"], gauge["now"])
        time.sleep(0.15)
        with lock:
            gauge["now"] -= 1
        return url, {"bookSourceUrl": url}, {"failureReason": ""}

    urls = [f"u{i}" for i in range(6)]
    many = {url: [{"bookSourceName": url, "bookSourceUrl": f"https://{url}.test"}] for url in urls}
    original_wait = vsp.ABANDONED_WORKER_WAIT_SECONDS
    vsp.ABANDONED_WORKER_WAIT_SECONDS = 0
    try:
        passed, _records, _details, _timed = vsp.run_parallel_stage("stability-2", urls, many, 2, 30, 30, counting_handler)
    finally:
        vsp.ABANDONED_WORKER_WAIT_SECONDS = original_wait
    assert passed == set(urls)
    assert gauge["max"] == 1  # --workers 2, one slot is still held by the abandoned worker


def test_task5_report_summary_records_abandoned_workers(clean_abandoned) -> None:
    thread = _blocked_thread(clean_abandoned)
    vsp.note_abandoned_workers([thread])
    outcome = vsp._StagedOutcome(
        version="1.1.1", urls=[], groups={}, max_workers=4, quick_timeout=5, source_timeout=5, rounds=1, min_pass_rounds=1,
        quick_passed=set(), full_passed=set(), all_details={}, best_records={}, pass_counts={}, final_urls=set(),
    )
    report, _passed = vsp._finalize_staged(outcome, 0)
    assert report["summary"]["abandonedWorkers"] == 1
    assert report["summary"]["abandonedWorkersStillRunning"] == 1


# ----------------------------------------------------------------------------------------------- 6  debugErrors


def test_task6_note_debug_error_is_capped_truncated_and_never_raises() -> None:
    vsp.begin_debug_errors()
    for index in range(40):
        vsp.note_debug_error("where", ValueError(f"boom {index} " + "x" * 1000))
    detail: dict[str, Any] = {}
    vsp.attach_debug_errors(detail)
    errors = detail["debugErrors"]
    assert len(errors) == vsp.DEBUG_ERROR_LIMIT == 20
    assert all(len(item) <= vsp.DEBUG_ERROR_TEXT_LIMIT for item in errors)
    assert errors[0].startswith("where: ValueError: boom 0 ")
    vsp.begin_debug_errors()
    vsp.note_debug_error("same", "again")
    vsp.note_debug_error("same", "again")  # consecutive duplicates are dropped
    vsp.note_debug_error("x", vsp.SourceValidationDeadlineExceeded("deadline"))  # a deadline is not a swallowed error
    vsp.note_debug_error("bad", SimpleNamespace())  # anything else still never raises
    detail = {}
    vsp.attach_debug_errors(detail)
    assert detail["debugErrors"][0] == "same: again" and len(detail["debugErrors"]) == 2
    vsp.begin_debug_errors()
    clean: dict[str, Any] = {}
    vsp.attach_debug_errors(clean)
    assert "debugErrors" not in clean


def test_task6_swallowed_rule_errors_reach_the_detail_without_changing_the_verdict(site: SimpleNamespace) -> None:
    broken = copy.deepcopy(site.sources["good"])
    broken["ruleSearch"]["bookList"] = "@css:div[["  # an invalid CSS selector: evaluates to nothing, the App fails the search too
    outcome, record, detail = vsp.validate_single_source(copy.deepcopy(broken))
    assert not outcome.passed and record is None and detail["failureReason"] == "no search/explore result"
    css = [item for item in detail.get("debugErrors", []) if item.startswith("css selector: SelectorSyntaxError")]
    assert css and all("\n" not in item for item in detail["debugErrors"]), detail.get("debugErrors")
    passed, _seed, quick_detail = vsp.quick_scan_single_source(copy.deepcopy(broken))
    assert not passed and any(item.startswith("css selector") for item in quick_detail.get("debugErrors", []))
    # a later source on the same thread starts with a clean list
    _outcome, _record, detail2 = vsp.validate_single_source(copy.deepcopy(site.sources["good"]))
    assert not any(item.startswith("css selector") for item in detail2.get("debugErrors", []))


# ----------------------------------------------------------------------------------------------- 7  validate_group


def test_task7_validate_group_serializes_each_candidate_once_and_is_fast(monkeypatch) -> None:
    candidates = [{"bookSourceName": f"c{i}", "bookSourceUrl": f"https://c{i}.test", "ruleSearch": {"bookList": f".b{i}"},
                   "tags": list(range(20))} for i in range(1000)]
    preferred = candidates[700]
    seed = {"source": dict(preferred), "bookUrls": ["x"]}  # an equal copy, not the same object (like the quick seed)
    seen: list[tuple[str, bool]] = []

    def fake_validate(src, quick_seed=None):
        seen.append((src["bookSourceName"], quick_seed is not None))
        if src is candidates[999]:
            return SimpleNamespace(passed=True), dict(src), {"ok": True}
        return SimpleNamespace(passed=False), None, {"failureReason": "x"}

    monkeypatch.setattr(vsp, "validate_single_source", fake_validate)
    monkeypatch.setattr(vsp, "ensure_source_validation_time_remaining", lambda: None)
    real_dumps = json.dumps
    counter = {"calls": 0}

    def counting_dumps(*args, **kwargs):
        counter["calls"] += 1
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(vsp.json, "dumps", counting_dumps)
    started = time.perf_counter()
    url, record, _detail = vsp.validate_group("https://c0.test", candidates, quick_seed=seed)
    elapsed = time.perf_counter() - started
    assert record is not None and record["bookSourceName"] == "c999"
    assert seen[0] == ("c700", True)  # the preferred candidate goes first and is the only one that gets the seed
    assert sum(1 for _name, has_seed in seen if has_seed) == 1 and len(seen) == 1000
    assert counter["calls"] <= len(candidates) + 5  # was ~3 per candidate
    assert elapsed < 10  # generous: only guards against quadratic regressions


# ----------------------------------------------------------------------------------------------- 8  java.log


LOG_SCRIPT = "java.log('first'); java.log({a: 1}); java.log('x'.repeat(5000)); 'done'"


def _log_events(run) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with debug_trace.tracing(events.append):
        run()
    return [event for event in events if event.get("kind") == "log"]


def _runtime() -> Any:
    return vsp.make_runtime(_src())


@needs_node
def test_h9_java_log_reaches_the_debug_trace_on_the_node_path() -> None:
    logs = _log_events(lambda: vsp.evaluate_js_with_node(LOG_SCRIPT, "", _runtime()))
    assert len(logs) == 3 and logs[0]["value"] == "first" and len(logs[2]["value"]) == vsp.DEBUG_LOG_TEXT_LIMIT


@needs_quickjs
def test_h9_java_log_reaches_the_debug_trace_on_the_quickjs_path() -> None:
    logs = _log_events(lambda: vsp._evaluate_js_quickjs(LOG_SCRIPT, "", _runtime(), None, None))
    assert len(logs) == 3 and logs[0]["value"] == "first" and len(logs[2]["value"]) == vsp.DEBUG_LOG_TEXT_LIMIT


@needs_node
def test_h9_batch_validation_keeps_the_plain_log_stub() -> None:
    assert "__readoriDebugLog" not in vsp._build_java_api_js(_runtime())
    seen: list[dict[str, Any]] = []
    assert vsp.evaluate_js_with_node(LOG_SCRIPT, "", _runtime()) == "done"
    with debug_trace.tracing(seen.append):
        assert "__readoriDebugLog" in vsp._build_java_api_js(_runtime())
    assert not [event for event in seen if event.get("kind") == "log"]


@needs_node
def test_h9_log_message_count_is_capped() -> None:
    script = "for (var i = 0; i < 700; i++) java.log('m' + i); 'ok'"
    logs = _log_events(lambda: vsp.evaluate_js_with_node(script, "", _runtime()))
    assert len(logs) == vsp.DEBUG_LOG_LIMIT == 500 and logs[-1]["value"] == "m499"


def test_h9_render_event_prints_log_lines() -> None:
    assert de.render_event({"kind": "log", "stage": "search", "value": "hello"}).endswith("[log] hello")


# ----------------------------------------------------------------------------------------------- 8  cancel points


def test_cancel_before_validate_one_starts(site: SimpleNamespace) -> None:
    controller = de.DebugController()
    controller.cancel()
    fixture_site.REQUEST_LOG.clear()
    with pytest.raises(debug_trace.DebugCancelled):
        de.validate_one(copy.deepcopy(site.sources["good"]), app_version="1.1.1", controller=controller)
    assert fixture_site.REQUEST_LOG == []


def test_cancel_between_stages(monkeypatch, site: SimpleNamespace) -> None:
    controller = de.DebugController()
    real = vsp.quick_scan_group

    def cancel_after_quick_scan(url, candidates):
        out = real(url, candidates)
        controller.cancel()
        return out

    monkeypatch.setattr(vsp, "quick_scan_group", cancel_after_quick_scan)
    fixture_site.REQUEST_LOG.clear()
    with pytest.raises(debug_trace.DebugCancelled):
        de.validate_one(copy.deepcopy(site.sources["good"]), app_version="1.1.1", controller=controller)
    assert any(entry["path"].startswith("/search") for entry in fixture_site.REQUEST_LOG)
    assert not any(entry["path"].startswith("/book") for entry in fixture_site.REQUEST_LOG)  # full chain never started


def test_cancel_between_requests_without_a_trace_session(monkeypatch, site: SimpleNamespace) -> None:
    controller = de.DebugController()
    real = vsp.fetch_text
    state = {"calls": 0}

    def cancel_on_first_request(*args: Any, **kwargs: Any):
        state["calls"] += 1
        if state["calls"] == 1:
            controller.cancel()
        return real(*args, **kwargs)

    monkeypatch.setattr(vsp, "fetch_text", cancel_on_first_request)
    fixture_site.REQUEST_LOG.clear()
    with pytest.raises(debug_trace.DebugCancelled):
        de.validate_one(copy.deepcopy(site.sources["good"]), app_version="1.1.1", controller=controller)
    assert not any(entry["path"].startswith("/book") for entry in fixture_site.REQUEST_LOG)


def test_cancel_between_the_two_compare_runs(monkeypatch, site: SimpleNamespace) -> None:
    controller = de.DebugController()
    real = de.validate_one
    runs = {"count": 0}

    def validate_then_cancel(*args: Any, **kwargs: Any):
        runs["count"] += 1
        out = real(*args, **kwargs)
        controller.cancel()
        return out

    monkeypatch.setattr(de, "validate_one", validate_then_cancel)
    with pytest.raises(debug_trace.DebugCancelled):
        de.compare_sources(copy.deepcopy(site.sources["good"]), copy.deepcopy(site.sources["good"]), app_version="1.1.1",
                           controller=controller)
    assert runs["count"] == 1  # the edited source is never validated after the stop


def test_without_cancel_validate_one_and_compare_are_unchanged(site: SimpleNamespace) -> None:
    controller = de.DebugController()
    plain = de.validate_one(copy.deepcopy(site.sources["good"]), app_version="1.1.1")
    with_controller = de.validate_one(copy.deepcopy(site.sources["good"]), app_version="1.1.1", controller=controller)
    assert plain["passed"] and with_controller["passed"] and plain["stages"] == with_controller["stages"]
    session = de.DebugSession(app_version="1.1.1", controller=de.DebugController())
    traced = de.validate_one(copy.deepcopy(site.sources["good"]), app_version="1.1.1", trace=session)
    assert traced["passed"] and session.events[-1]["kind"] == "verdict"
    comparison = de.compare_sources(copy.deepcopy(site.sources["toc_broken"]), copy.deepcopy(site.sources["good"]),
                                    app_version="1.1.1", controller=controller)
    assert comparison["changed"] and comparison["after"]["passed"] and not comparison["before"]["passed"]
