"""R146 WP-A: the debug trace hooks in validate_source_packages are free when off and exact when on.

Offline: every request goes to the local fixture site (tests/debug_fixture_site.py).
"""

from __future__ import annotations

import copy
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

import debug_fixture_site as fixture_site
from validator import debug_trace
from validator import validate_source_packages as vsp


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


def _runtime_and_view(src: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    from validator import app_profiles

    view, error = app_profiles.app_decode_view(src)
    assert not error
    return view, vsp.make_runtime(view)


def _detail_without_timing(detail: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(detail)
    out.pop("responseTimeMs", None)
    if isinstance(out.get("quickScan"), dict):
        out["quickScan"].pop("responseTimeMs", None)
    return out


def test_hooks_never_call_emit_when_tracing_is_off(site: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("debug_trace.emit must not be called without an active sink")

    monkeypatch.setattr(debug_trace, "emit", boom)
    monkeypatch.setattr(debug_trace, "emit_fetch", boom)
    monkeypatch.setattr(debug_trace, "emit_fetch_error", boom)
    outcome, _record, detail = vsp.validate_single_source(copy.deepcopy(site.sources["good"]))
    assert outcome.passed and detail["stages"] == {"search": True, "detail": True, "toc": True, "content": True}
    # failing paths (blocked / error hooks) are silent too
    outcome, _record, detail = vsp.validate_single_source(copy.deepcopy(site.sources["toc_broken"]))
    assert not outcome.passed and detail["failureReason"] == "toc failed"


def test_tracing_does_not_change_the_validation_result(site: SimpleNamespace) -> None:
    for name in ("good", "toc_broken", "search_broken", "gbk_missing_charset"):
        plain_outcome, _r1, plain = vsp.validate_single_source(copy.deepcopy(site.sources[name]))
        events: list[dict] = []
        with debug_trace.tracing(events.append):
            traced_outcome, _r2, traced = vsp.validate_single_source(copy.deepcopy(site.sources[name]))
        assert traced_outcome.passed == plain_outcome.passed, name
        assert _detail_without_timing(traced) == _detail_without_timing(plain), name
        assert events, name


def test_events_cover_every_stage(site: SimpleNamespace) -> None:
    events: list[dict] = []
    with debug_trace.tracing(events.append):
        outcome, _record, _detail = vsp.validate_single_source(copy.deepcopy(site.sources["good"]))
    assert outcome.passed
    kinds = [event["kind"] for event in events]
    for kind in ("begin", "end", "keyword", "fetch", "list", "field", "item", "page"):
        assert kind in kinds, kind
    fetches = [e for e in events if e["kind"] == "fetch"]
    assert {e["stage"] for e in fetches} == {"search", "info", "toc", "content"}
    first = fetches[0]
    assert first["status"] == 200 and first["method"] == "GET" and first["attempt"] == 0
    assert "<h3>" in first["body"] and first["charset"] == "utf-8" and first["elapsed_ms"] >= 0
    assert first["response_headers"].get("Content-Type", "").startswith("text/html")
    stages_with_field = {(e["stage"], e["field"]) for e in events if e["kind"] == "field"}
    assert ("search", "name") in stages_with_field and ("info", "tocUrl") in stages_with_field
    assert ("toc", "chapterUrl") in stages_with_field and ("content", "content") in stages_with_field
    list_events = [e for e in events if e["kind"] == "list"]
    assert [(e["stage"], e["field"], e["count"]) for e in list_events[:2]] == [("search", "bookList", 2), ("toc", "chapterList", 3)]
    content_field = next(e for e in events if e["kind"] == "field" and e["field"] == "content")
    assert content_field["count"] == len(content_field["value"]) or content_field["count"] >= len(content_field["value"])
    ends = [e for e in events if e["kind"] == "end"]
    assert ends[-1]["stage"] == "full-validation" and ends[-1]["ok"] is True


def test_redirect_history_is_reported(site: SimpleNamespace) -> None:
    events: list[dict] = []
    with debug_trace.tracing(events.append):
        vsp.validate_single_source(copy.deepcopy(site.sources["moved"]))
    redirected = next(e for e in events if e["kind"] == "fetch" and e["history"])
    assert redirected["history"][0][0] == 301
    assert redirected["history"][0][1].startswith(site.base + "/old/search")
    assert redirected["final_url"].startswith(site.alt + "/site2/search")


def test_a_failing_sink_is_ignored_but_cancel_passes_through(site: SimpleNamespace) -> None:
    def broken(event: dict) -> None:
        raise RuntimeError("listener bug")

    with debug_trace.tracing(broken):
        outcome, _record, _detail = vsp.validate_single_source(copy.deepcopy(site.sources["good"]))
    assert outcome.passed

    def stopper(event: dict) -> None:
        if event["kind"] == "list":
            raise debug_trace.DebugCancelled()

    with pytest.raises(debug_trace.DebugCancelled):
        with debug_trace.tracing(stopper):
            vsp.validate_single_source(copy.deepcopy(site.sources["good"]))


def test_fetch_book_candidates_defaults_are_unchanged(site: SimpleNamespace) -> None:
    signature = inspect.signature(vsp.fetch_book_candidates)
    assert {"keywords", "modes", "explore_url"} <= set(signature.parameters)
    assert signature.parameters["modes"].default == ("search", "explore")
    view, runtime = _runtime_and_view(site.sources["good"])
    plain = vsp.fetch_book_candidates(vsp.make_source_session(view), view, runtime)
    view2, runtime2 = _runtime_and_view(site.sources["good"])
    explicit = vsp.fetch_book_candidates(vsp.make_source_session(view2), view2, runtime2, keywords=None,
                                         modes=("search", "explore"), explore_url=None)
    assert plain == explicit
    assert plain[0] == [site.base + "/book/1", site.base + "/book/2"]


def test_fetch_book_candidates_keywords_modes_and_explore_url(site: SimpleNamespace) -> None:
    view, runtime = _runtime_and_view(site.sources["good"])
    fixture_site.REQUEST_LOG.clear()
    urls, _base = vsp.fetch_book_candidates(vsp.make_source_session(view), view, runtime, keywords=["夜无疆"],
                                            modes=("search",))
    assert urls
    assert fixture_site.REQUEST_LOG[0]["path"] == "/search?kw=%E5%A4%9C%E6%97%A0%E7%96%86"
    # explore only, with an explicit URL (the source has no exploreUrl at all)
    fixture_site.REQUEST_LOG.clear()
    view, runtime = _runtime_and_view(site.sources["good"])
    urls, base = vsp.fetch_book_candidates(vsp.make_source_session(view), view, runtime, modes=("explore",),
                                           explore_url=site.base + "/search?kw=rank")
    assert urls == [site.base + "/book/1", site.base + "/book/2"] and base.startswith(site.base + "/search")
    assert [entry["path"] for entry in fixture_site.REQUEST_LOG] == ["/search?kw=rank"]
    # explore mode without a usable explore entry never searches
    view, runtime = _runtime_and_view(site.sources["good"])
    fixture_site.REQUEST_LOG.clear()
    assert vsp.fetch_book_candidates(vsp.make_source_session(view), view, runtime, modes=("explore",)) == ([], "")
    assert fixture_site.REQUEST_LOG == []


def test_search_only_mode_does_not_record_a_failure_or_fall_back(site: SimpleNamespace) -> None:
    view, runtime = _runtime_and_view(site.sources["search_broken"])
    urls, _base = vsp.fetch_book_candidates(vsp.make_source_session(view), view, runtime, modes=("search",))
    assert urls == []
    assert (runtime.variables or {}).get("__exploreOnlyRescue") != "1"


def test_offline_mode_blocks_fetch_text_and_fetch_bytes(site: SimpleNamespace) -> None:
    session = vsp.make_source_session()
    events: list[dict] = []
    fixture_site.REQUEST_LOG.clear()
    with debug_trace.tracing(events.append, offline=True):
        assert vsp.fetch_text(session, site.base + "/search?kw=x") == ("", site.base + "/search?kw=x", 0)
        assert vsp.fetch_bytes(session, site.base + "/static/c.png") == (b"", site.base + "/static/c.png", 0)
    assert fixture_site.REQUEST_LOG == []
    assert [e["kind"] for e in events] == ["blocked", "blocked"]
    assert vsp.fetch_text(session, site.base + "/search?kw=x")[2] == 200  # off again outside the context


def test_fetch_error_and_binary_fetch_events(site: SimpleNamespace) -> None:
    session = vsp.make_source_session()
    events: list[dict] = []
    with debug_trace.tracing(events.append):
        vsp.fetch_text(session, "http://127.0.0.1:9/never", timeout=2)
        content, _final, status = vsp.fetch_bytes(session, site.base + "/static/cover1.jpg")
    error = next(e for e in events if e["kind"] == "fetch_error")
    assert error["url"].endswith("/never") and error["error"]
    binary = next(e for e in events if e["kind"] == "fetch" and e.get("binary"))
    assert binary["body"] is None and binary["size"] == len(content) and status == 200


def test_reset_thread_session_gives_a_clean_session() -> None:
    first = vsp.reset_thread_session()
    first.cookies.set("sid", "1")
    second = vsp.reset_thread_session()
    assert second is not first and not second.cookies.get_dict()
    assert vsp._thread_local.session is second


def test_vsp_hooks_are_guarded_and_the_file_stays_crlf() -> None:
    from pathlib import Path

    path = Path(vsp.__file__)
    raw = path.read_bytes()
    # Checkouts differ (CRLF in the main repo on Windows, LF in the standalone mirror); never mixed.
    crlf = raw.count(b"\r\n")
    assert crlf in (0, raw.count(b"\n")), "validate_source_packages.py must not mix CRLF and LF line endings"
    text = raw.decode("utf-8")
    assert "if _dbg.active():" in text and "def reset_thread_session(" in text
    # every emit goes through the guard: no bare `_dbg.emit(` at the start of a statement outside an `if _dbg...:` block
    lines = text.splitlines()
    for number, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("_dbg.emit") or stripped.startswith("_dbg.set_stage"):
            guard = next(prev for prev in reversed(lines[:number]) if prev.strip() and not prev.strip().startswith("_dbg.")
                         and not prev.strip().startswith(("request_headers", "started=", "value=", '"')))
            assert "_dbg." in guard or "if " in guard, f"unguarded hook near line {number + 1}: {line.strip()}"
