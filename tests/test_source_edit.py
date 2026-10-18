"""R146 WP-B: SourceDocument / diff / // Fixed: comment / export / fix-report (offline)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from validator import source_edit as se


def _la_available() -> bool:
    try:
        ZoneInfo("America/Los_Angeles")
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


needs_tzdata = pytest.mark.skipif(not _la_available(), reason="no tz database (pip install tzdata)")


def _src() -> dict:
    return {
        "bookSourceUrl": "https://a.example.com",
        "bookSourceName": "示例",
        "bookSourceGroup": "grp",
        "searchUrl": "/s?q={{key}}",
        "ruleSearch": {"bookList": "class.list@tag.li", "name": "h3@text", "coverUrl": "img@src"},
        "ruleToc": {"chapterList": "id.list@tag.dd"},
        "customTag": "keep",
        "__readoriValidation": {"x": 1},
    }


def _la(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/Los_Angeles"))


# -- path get / set ----------------------------------------------------------

def test_get_and_set_nested_path() -> None:
    doc = se.SourceDocument(_src())
    assert doc.get("ruleSearch.coverUrl") == "img@src"
    assert doc.get("ruleSearch.author", "none") == "none"
    patch = doc.set("ruleSearch.coverUrl", "img@data-original", reason="封面懒加载", origin="suggestion:F5")
    assert patch == [{"op": "set", "path": "ruleSearch.coverUrl", "old": "img@src", "new": "img@data-original"}]
    assert doc.get("ruleSearch.coverUrl") == "img@data-original"
    assert doc.original["ruleSearch"]["coverUrl"] == "img@src"  # original untouched


def test_set_creates_missing_dicts_and_top_level_key() -> None:
    doc = se.SourceDocument({"bookSourceUrl": "https://x"})
    doc.set("ruleContent.replaceRegex", "##ad")
    doc.set("header", '{"User-Agent":"x"}')
    assert doc.working["ruleContent"] == {"replaceRegex": "##ad"}
    assert list(doc.working)[-1] == "header"
    assert doc.get("ruleContent.replaceRegex") == "##ad"


def test_set_same_value_is_noop() -> None:
    doc = se.SourceDocument(_src())
    assert doc.set("ruleSearch.name", "h3@text") == []
    assert doc.can_undo is False
    assert doc.changes() == []


def test_set_returns_deep_copies() -> None:
    doc = se.SourceDocument({"bookSourceUrl": "u", "x": {"a": [1, 2]}})
    got = doc.get("x")
    got["a"].append(3)
    assert doc.working["x"]["a"] == [1, 2]


def test_delete_op() -> None:
    doc = se.SourceDocument(_src())
    patch = doc.delete("ruleSearch.coverUrl", reason="无封面")
    assert patch[0]["op"] == "delete" and patch[0]["old"] == "img@src"
    assert "coverUrl" not in doc.working["ruleSearch"]
    assert doc.delete("ruleSearch.coverUrl") == []
    assert doc.undo() is True
    assert doc.working["ruleSearch"]["coverUrl"] == "img@src"


def test_invalid_path_rejected() -> None:
    doc = se.SourceDocument(_src())
    with pytest.raises(ValueError):
        doc.set("ruleSearch..name", "x")
    with pytest.raises(ValueError):
        doc.set("", "x")


def test_set_through_non_object_raises_and_leaves_document_unchanged() -> None:
    doc = se.SourceDocument(_src())
    before = copy.deepcopy(doc.working)
    with pytest.raises(TypeError):
        doc.set("searchUrl.sub", "x")
    assert doc.working == before
    assert doc.can_undo is False


# -- apply / undo / redo -----------------------------------------------------

def test_apply_is_one_undo_unit_and_undo_redo_roundtrip() -> None:
    doc = se.SourceDocument(_src())
    doc.apply(
        [
            {"op": "set", "path": "ruleSearch.coverUrl", "old": "img@src", "new": "img@data-original"},
            {"op": "set", "path": "ruleToc.nextTocUrl", "old": None, "new": "text.下一页@href"},
        ],
        reason="一次建议", origin="suggestion:F5",
    )
    assert len(doc.changes()) == 2
    assert doc.undo() is True
    assert doc.working == doc.original
    assert doc.changes() == []
    assert doc.redo() is True
    assert doc.working["ruleSearch"]["coverUrl"] == "img@data-original"
    assert doc.working["ruleToc"]["nextTocUrl"] == "text.下一页@href"
    assert len(doc.changes()) == 2
    assert doc.undo() and doc.undo() is False


def test_new_edit_clears_redo() -> None:
    doc = se.SourceDocument(_src())
    doc.set("ruleSearch.name", "h4@text")
    doc.undo()
    assert doc.can_redo is True
    doc.set("ruleSearch.name", "h5@text")
    assert doc.can_redo is False
    assert doc.redo() is False


def test_undo_stack_is_capped() -> None:
    doc = se.SourceDocument({"bookSourceUrl": "u", "n": 0})
    for i in range(1, se.UNDO_LIMIT + 30):
        doc.set("n", i)
    undone = 0
    while doc.undo():
        undone += 1
    assert undone == se.UNDO_LIMIT
    assert doc.working["n"] == 29  # 230 edits - 200 undone


def test_apply_is_atomic_on_failure() -> None:
    doc = se.SourceDocument(_src())
    before = copy.deepcopy(doc.working)
    with pytest.raises(TypeError):
        doc.apply([
            {"op": "set", "path": "ruleSearch.name", "new": "x"},
            {"op": "set", "path": "searchUrl.sub", "new": "y"},
        ])
    assert doc.working == before
    assert doc.changes() == []


def test_apply_unknown_op_rejected() -> None:
    doc = se.SourceDocument(_src())
    with pytest.raises(ValueError):
        doc.apply([{"op": "merge", "path": "a", "new": 1}])
    assert doc.working == doc.original


def test_check_old_detects_stale_patch() -> None:
    doc = se.SourceDocument(_src())
    stale = [{"op": "set", "path": "ruleSearch.name", "old": "something else", "new": "h4@text"}]
    with pytest.raises(se.PatchConflict):
        doc.apply(stale, check_old=True)
    assert doc.working == doc.original
    fresh = [{"op": "set", "path": "ruleSearch.name", "old": "h3@text", "new": "h4@text"}]
    doc.apply(fresh, check_old=True, reason="r", origin="manual")
    assert doc.get("ruleSearch.name") == "h4@text"


def test_changes_record_reason_origin_and_codes() -> None:
    doc = se.SourceDocument(_src())
    doc.set("ruleSearch.coverUrl", "img@data-original", reason="封面懒加载", origin="suggestion:F5")
    doc.set("ruleSearch.name", "h4@text")
    changes = doc.changes()
    assert changes[0] == {"path": "ruleSearch.coverUrl", "old": "img@src", "new": "img@data-original",
                          "reason": "封面懒加载", "origin": "suggestion:F5"}
    assert changes[1]["origin"] == "manual"
    assert doc.change_codes() == ["F5", "manual"]


def test_reset_restores_original() -> None:
    doc = se.SourceDocument(_src())
    doc.set("ruleSearch.name", "x")
    doc.reset()
    assert doc.working == doc.original and not doc.can_undo and not doc.can_redo


# -- diff ---------------------------------------------------------------------

def test_diff_is_structured_and_follows_original_key_order() -> None:
    doc = se.SourceDocument(_src())
    doc.set("ruleToc.nextTocUrl", "text.下一页@href")
    doc.set("ruleSearch.coverUrl", "img@data-original")
    doc.set("header", '{"User-Agent":"x"}')
    diff = doc.diff()
    assert diff == [
        {"path": "ruleSearch.coverUrl", "old": "img@src", "new": "img@data-original"},
        {"path": "ruleToc.nextTocUrl", "old": None, "new": "text.下一页@href"},
        {"path": "header", "old": None, "new": '{"User-Agent":"x"}'},
    ]


def test_diff_empty_when_unchanged_and_after_undo() -> None:
    doc = se.SourceDocument(_src())
    assert doc.diff() == []
    doc.set("ruleSearch.name", "x")
    doc.undo()
    assert doc.diff() == []


def test_diff_lists_compared_whole() -> None:
    doc = se.SourceDocument({"bookSourceUrl": "u", "tags": ["a", "b"]})
    doc.set("tags", ["a", "c"])
    assert doc.diff() == [{"path": "tags", "old": ["a", "b"], "new": ["a", "c"]}]


def test_diff_marks_removed_keys_with_none_new() -> None:
    doc = se.SourceDocument(_src())
    doc.delete("customTag")
    assert doc.diff() == [{"path": "customTag", "old": "keep", "new": None}]


def test_string_rule_is_converted_to_object_with_note() -> None:
    src = _src()
    src["ruleSearch"] = json.dumps({"bookList": "class.list@tag.li", "name": "h3@text"}, ensure_ascii=False)
    doc = se.SourceDocument(src)
    assert doc.get("ruleSearch.name") == "h3@text"  # readable without converting
    assert isinstance(doc.working["ruleSearch"], str)
    doc.set("ruleSearch.coverUrl", "img@data-original")
    assert isinstance(doc.working["ruleSearch"], dict)
    assert doc.working["ruleSearch"]["name"] == "h3@text"
    diff = doc.diff()
    assert diff[0] == {"path": "ruleSearch", "note": "字符串规则已转为对象"}
    assert {"path": "ruleSearch.coverUrl", "old": None, "new": "img@data-original"} in diff
    assert len(diff) == 2  # the unchanged decoded fields are not reported
    assert doc.undo() is True
    assert isinstance(doc.working["ruleSearch"], str) and doc.diff() == []


def test_untouched_string_rule_produces_no_diff() -> None:
    src = _src()
    src["ruleToc"] = json.dumps({"chapterList": "id.list@tag.dd"})
    doc = se.SourceDocument(src)
    doc.set("ruleSearch.name", "x")
    assert [d["path"] for d in doc.diff()] == ["ruleSearch.name"]


def test_unified_diff_text() -> None:
    doc = se.SourceDocument(_src())
    assert doc.unified_diff() == ""
    doc.set("ruleSearch.coverUrl", "img@data-original")
    text = doc.unified_diff()
    assert text.startswith("--- original")
    assert '-    "coverUrl": "img@src"' in text
    assert '+    "coverUrl": "img@data-original"' in text
    assert "示例" not in text or "\\u" not in text  # ensure_ascii=False


# -- // Fixed: comment --------------------------------------------------------

@needs_tzdata
def test_stamp_uses_la_time_and_blank_line_separator() -> None:
    src = {"bookSourceUrl": "u", "bookSourceComment": "原有说明"}
    se.stamp_fixed_comment(src, ["F5", "F7"], now=_la(2026, 10, 18, 10, 0), reasons=["封面懒加载", "广告净化"])
    blocks = src["bookSourceComment"].split("\n\n")
    assert blocks[0] == "// Fixed: 2026-10-18 10:00 America/Los_Angeles F5,F7: 封面懒加载; 广告净化"
    assert blocks[1] == "原有说明"
    assert "\n\n\n" not in src["bookSourceComment"]
    expected_ms = int(_la(2026, 10, 18, 10, 0).timestamp() * 1000)
    assert src["lastUpdateTime"] == expected_ms


@needs_tzdata
def test_stamp_converts_other_timezones_to_la_including_dst() -> None:
    summer_utc = datetime(2026, 7, 1, 17, 0, tzinfo=timezone.utc)   # PDT = UTC-7 -> 10:00
    winter_utc = datetime(2026, 12, 1, 18, 0, tzinfo=timezone.utc)  # PST = UTC-8 -> 10:00
    assert se.format_la_time(summer_utc) == "2026-07-01 10:00 America/Los_Angeles"
    assert se.format_la_time(winter_utc) == "2026-12-01 10:00 America/Los_Angeles"
    assert se.la_iso(summer_utc) == "2026-07-01T10:00:00-07:00"
    assert se.la_iso(winter_utc) == "2026-12-01T10:00:00-08:00"


@needs_tzdata
def test_stamp_naive_now_is_la_wall_time() -> None:
    assert se.format_la_time(datetime(2026, 3, 8, 12, 30)) == "2026-03-08 12:30 America/Los_Angeles"


@needs_tzdata
def test_stamp_replaces_old_fixed_block_not_stacking() -> None:
    now = _la(2026, 10, 18, 10, 0)
    src = {"bookSourceUrl": "u", "bookSourceComment": "说明A\n\n// Fixed: 2026-01-01 00:00 America/Los_Angeles F1: old\n\n说明B"}
    se.stamp_fixed_comment(src, ["F3"], now=now, reasons=["补 UA"])
    se.stamp_fixed_comment(src, ["F3"], now=now, reasons=["补 UA"])
    text = src["bookSourceComment"]
    assert text.count("// Fixed: ") == 1
    assert text.split("\n\n") == ["// Fixed: 2026-10-18 10:00 America/Los_Angeles F3: 补 UA", "说明A", "说明B"]


@needs_tzdata
def test_stamp_removes_error_block_only_when_passed() -> None:
    now = _la(2026, 10, 18, 10, 0)
    base = {"bookSourceUrl": "u", "bookSourceComment": "// Error: toc failed\n\n保留"}
    failing = copy.deepcopy(base)
    se.stamp_fixed_comment(failing, ["F6"], now=now)
    assert "// Error: toc failed" in failing["bookSourceComment"]
    passing = copy.deepcopy(base)
    se.stamp_fixed_comment(passing, ["F6"], now=now, passed=True)
    assert "// Error:" not in passing["bookSourceComment"]
    assert passing["bookSourceComment"].endswith("保留")


@needs_tzdata
def test_stamp_manual_when_no_codes_and_empty_comment() -> None:
    src = {"bookSourceUrl": "u"}
    se.stamp_fixed_comment(src, [], now=_la(2026, 10, 18, 10, 0))
    assert src["bookSourceComment"] == "// Fixed: 2026-10-18 10:00 America/Los_Angeles manual"


@needs_tzdata
def test_stamp_flattens_multiline_reasons_and_dedupes() -> None:
    block = se.fixed_comment_block(["F7"], ["去广告\n第二行", "去广告\n第二行", ""], _la(2026, 10, 18, 10, 0))
    assert block.endswith("F7: 去广告 第二行")


def test_stamp_without_tzdata_falls_back_to_local_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(se, "la_zone", lambda: None)
    now = datetime(2026, 10, 18, 17, 0, tzinfo=timezone(timedelta(hours=-7)))
    text = se.format_la_time(now)
    assert text.startswith("2026-") and "America/Los_Angeles" not in text and "(local)" in text
    src = {"bookSourceUrl": "u"}
    se.stamp_fixed_comment(src, ["F1"], now=now)
    assert src["bookSourceComment"].startswith("// Fixed: ")
    assert src["lastUpdateTime"] == int(now.timestamp() * 1000)


# -- export -------------------------------------------------------------------

def test_export_keeps_key_order_appends_new_and_drops_internal_key() -> None:
    doc = se.SourceDocument(_src())
    doc.set("header", '{"User-Agent":"x"}')
    doc.set("ruleSearch.coverUrl", "img@data-original")
    (out,) = se.export_sources([doc])
    assert "__readoriValidation" not in out
    keys = list(out)
    assert keys[:7] == ["bookSourceUrl", "bookSourceName", "bookSourceGroup", "searchUrl", "ruleSearch", "ruleToc", "customTag"]
    assert keys[-1] == "header"
    assert out["customTag"] == "keep" and out["bookSourceGroup"] == "grp"  # no validation tags written
    assert out["ruleSearch"]["coverUrl"] == "img@data-original"
    assert doc.working["__readoriValidation"] == {"x": 1}  # working untouched


def test_export_only_changed_and_only_passed() -> None:
    a = se.SourceDocument(_src())
    b = se.SourceDocument({**_src(), "bookSourceUrl": "https://b.example.com"})
    c = se.SourceDocument({**_src(), "bookSourceUrl": "https://c.example.com"})
    b.set("ruleSearch.name", "x")
    c.set("ruleSearch.name", "y")
    c.after_result = {"verdict": "pass", "passed": True}
    b.after_result = {"verdict": "invalid", "passed": False}
    assert [s["bookSourceUrl"] for s in se.export_sources([a, b, c])] == [
        "https://a.example.com", "https://b.example.com", "https://c.example.com"]
    assert [s["bookSourceUrl"] for s in se.export_sources([a, b, c], only_changed=True)] == [
        "https://b.example.com", "https://c.example.com"]
    assert [s["bookSourceUrl"] for s in se.export_sources([a, b, c], only_passed=True)] == ["https://c.example.com"]
    assert [s["bookSourceUrl"] for s in se.export_sources([a, b, c], only_changed=True, only_passed=True)] == [
        "https://c.example.com"]


def test_export_accepts_entry_dicts() -> None:
    doc = se.SourceDocument(_src())
    doc.set("ruleSearch.name", "x")
    entry = doc.to_entry()
    (out,) = se.export_sources([entry], only_changed=True)
    assert out["ruleSearch"]["name"] == "x"


def test_export_keep_source_url_restores_original() -> None:
    doc = se.SourceDocument(_src())
    doc.set("bookSourceUrl", "https://new.example.com", reason="域名迁移", origin="suggestion:F1")
    (migrated,) = se.export_sources([doc])
    assert migrated["bookSourceUrl"] == "https://new.example.com"
    (kept,) = se.export_sources([doc], keep_source_url=True)
    assert kept["bookSourceUrl"] == "https://a.example.com"


@needs_tzdata
def test_export_stamp_only_for_changed_sources() -> None:
    a = se.SourceDocument(_src())
    b = se.SourceDocument({**_src(), "bookSourceUrl": "https://b.example.com"})
    b.set("ruleSearch.name", "x", reason="改名", origin="suggestion:F4")
    now = _la(2026, 10, 18, 10, 0)
    out_a, out_b = se.export_sources([a, b], stamp=True, now=now)
    assert "bookSourceComment" not in out_a
    assert out_b["bookSourceComment"] == "// Fixed: 2026-10-18 10:00 America/Los_Angeles F4: 改名"
    assert "bookSourceComment" not in b.working  # the working copy is not stamped


def test_dumps_sources_is_utf8_readable_indent_2() -> None:
    text = se.dumps_sources([{"bookSourceName": "示例", "a": 1}])
    assert "示例" in text and '\n    "a": 1' in text


# -- fix-report ---------------------------------------------------------------

@needs_tzdata
def test_fix_report_schema_and_fields() -> None:
    doc = se.SourceDocument(_src())
    doc.set("bookSourceUrl", "https://new.example.com", reason="域名迁移", origin="suggestion:F1")
    doc.set("ruleSearch.coverUrl", "img@data-original", reason="封面懒加载", origin="suggestion:F5")
    doc.before_result = {"verdict": "invalid", "failureReason": "toc failed", "stages": {"search": True, "toc": False},
                         "responseTimeMs": 812, "detail": "big"}
    doc.after_result = {"verdict": "pass", "failureReason": "", "stages": {"search": True, "toc": True},
                        "responseTimeMs": 900}
    now = _la(2026, 10, 18, 10, 0)
    report = se.build_fix_report([doc], app_version="1.1.1", now=now)
    assert report["schema"] == "readori.fix-report/1"
    assert report["generatedAt"] == "2026-10-18T10:00:00-07:00"
    assert report["appVersion"] == "1.1.1"
    assert report["tool"] == "readori-source-validator R146"
    (entry,) = report["sources"]
    assert entry["bookSourceUrl"] == "https://new.example.com"
    assert entry["originalBookSourceUrl"] == "https://a.example.com"
    assert entry["bookSourceName"] == "示例"
    assert entry["changes"][1] == {"path": "ruleSearch.coverUrl", "old": "img@src", "new": "img@data-original",
                                    "reason": "封面懒加载", "origin": "suggestion:F5"}
    assert entry["before"] == {"verdict": "invalid", "failureReason": "toc failed",
                               "stages": {"search": True, "toc": False}, "responseTimeMs": 812}
    assert entry["after"]["verdict"] == "pass"
    assert entry["comment"] == "// Fixed: 2026-10-18 10:00 America/Los_Angeles F1,F5: 域名迁移; 封面懒加载"
    json.dumps(report, ensure_ascii=False)  # serialisable


def test_fix_report_without_changes_or_results() -> None:
    doc = se.SourceDocument(_src())
    report = se.build_fix_report([doc.to_entry()], now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    (entry,) = report["sources"]
    assert entry["changes"] == [] and entry["before"] is None and entry["after"] is None
    assert entry["comment"] == ""
    assert entry["bookSourceUrl"] == entry["originalBookSourceUrl"]


def test_fix_report_keep_source_url_uses_original_as_primary() -> None:
    doc = se.SourceDocument(_src())
    doc.set("bookSourceUrl", "https://new.example.com", origin="suggestion:F1")
    report = se.build_fix_report([doc], keep_source_url=True)
    assert report["sources"][0]["bookSourceUrl"] == "https://a.example.com"
    assert report["sources"][0]["originalBookSourceUrl"] == "https://a.example.com"


def test_source_has_guard_needles() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "validator" / "source_edit.py").read_text(encoding="utf-8")
    assert '"// Fixed: "' in text
    assert "America/Los_Angeles" in text
    assert "readori.fix-report/1" in text
