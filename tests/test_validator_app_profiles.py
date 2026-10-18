"""R143 WP-B1: App version profiles wired into validate_source_packages (offline; no network)."""
from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import Any

import pytest
import requests

from validator import app_profiles
from validator import validate_source_packages as validator

ROOT = Path(__file__).resolve().parents[1]

needs_js = pytest.mark.skipif(
    not validator.node_binary_path() and validator.quickjs is None,
    reason="no JS engine (node / quickjs) available",
)


class _FakeResponse:
    status_code = 200
    text = ""
    content = b""
    url = "https://a.example"
    headers: dict[str, str] = {}


@pytest.fixture
def offline(monkeypatch):
    """No network: every fetch returns an empty body, every session.get returns an empty 200."""
    fetched: list[str] = []

    def fake_fetch_text(_session, url, *_args, **_kwargs):
        fetched.append(url)
        return "", url, 0

    monkeypatch.setattr(validator, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kwargs: _FakeResponse())
    return fetched


def _request_search(src: dict[str, Any], version: str, keyword: str = "abc"):
    with app_profiles.app_profile_scope(version):
        return validator.request_search_or_explore(requests.Session(), src, validator.make_runtime(src), keyword)


# ---------------------------------------------------------------- A1 search url


@needs_js
def test_search_request_111_literal_vs_112_script(offline) -> None:
    src = {
        "bookSourceUrl": "https://a.example",
        "searchUrl": "@js:\nvar host = 'https://a.example';\nhost + '/s?q=' + key + '&p=' + page",
        "ruleSearch": {"bookList": ".b"},
    }
    assert _request_search(src, "1.1.1")[0] == "https://a.example"
    assert _request_search(src, "1.1.2")[0] == "https://a.example/s?q=abc&p=1"
    assert validator.current_app_version() == "1.1.1"


@needs_js
def test_search_request_111_post_literal_drops_body(offline) -> None:
    src = {
        "bookSourceUrl": "https://a.example",
        "searchUrl": "@js:\nresult=\"http://api.example/H.ashx,\"+`{\"body\":\"key=${key}\",\"method\":\"POST\"}`",
        "ruleSearch": {"bookList": ".b"},
    }
    _url111, method111, options111 = _request_search(src, "1.1.1")
    assert method111 == "GET"
    assert options111["body"] is None
    _url112, method112, options112 = _request_search(src, "1.1.2")
    assert method112 == "POST"
    assert "key=abc" in (options112["body"] or "")


def test_search_template_follows_app_profile() -> None:
    src = {"searchUrl": "@js: 'https://a.example/s?q={{key}}'"}
    assert app_profiles.app_search_url_template(src, "1.1.1") == "https://a.example/s?q={{key}}"
    assert app_profiles.app_search_url_template(src, "1.1.2") == "@js: 'https://a.example/s?q={{key}}'"
    with app_profiles.app_profile_scope("1.1.2"):
        assert app_profiles.app_search_url_template(src) == "@js: 'https://a.example/s?q={{key}}'"


# ---------------------------------------------------------------- A2 decode view in the pipeline


def _explore_only_source() -> dict[str, Any]:
    return {
        "bookSourceName": "R143",
        "bookSourceUrl": "https://a.example",
        "exploreUrl": [{"title": "a", "url": "/c/1"}],
        "ruleExplore": {"bookList": ".b"},
    }


def test_quick_scan_111_drops_array_explore(offline) -> None:
    src = _explore_only_source()
    with app_profiles.app_profile_scope("1.1.1"):
        passed, seed, detail = validator.quick_scan_single_source(src)
    assert passed is False and seed is None
    assert detail["failureReason"] == "missing search/explore rule"
    assert not [url for url in offline if url.endswith("/c/1")]

    with app_profiles.app_profile_scope("1.1.2"):
        validator.quick_scan_single_source(src)
    assert "https://a.example/c/1" in offline
    assert isinstance(src["exploreUrl"], list)  # the caller's record is never rewritten


def test_quick_scan_rejects_object_book_source_url_on_111(offline) -> None:
    src = {"bookSourceUrl": {"x": 1}, "searchUrl": "/s?q={{key}}", "ruleSearch": {"bookList": ".b"}}
    with app_profiles.app_profile_scope("1.1.1"):
        passed, seed, detail = validator.quick_scan_single_source(src)
    assert (passed, seed) == (False, None)
    assert detail["failureReason"] == "app import rejects source"
    assert "bookSourceUrl" in detail["error"]
    with app_profiles.app_profile_scope("1.1.1"):
        outcome, record, full_detail = validator.validate_single_source(src)
    assert outcome.passed is False and record is None
    assert full_detail["failureReason"] == "app import rejects source"


def test_stringified_rule_objects_are_unwrapped(offline) -> None:
    src = {
        "bookSourceUrl": "https://a.example",
        "searchUrl": "/s?q={{key}}",
        "ruleSearch": json.dumps({"bookList": ".b", "name": "a@text"}),
    }
    for version in ("1.1.1", "1.1.2"):
        with app_profiles.app_profile_scope(version):
            passed, _seed, detail = validator.quick_scan_single_source(src)
        assert passed is False
        assert detail["failureReason"] != "missing search/explore rule", version
    # a plain string is the Legado shorthand for ruleSearch.bookList
    shorthand = dict(src, ruleSearch=".b li")
    with app_profiles.app_profile_scope("1.1.1"):
        _passed, _seed, detail = validator.quick_scan_single_source(shorthand)
    assert detail["failureReason"] != "missing search/explore rule"


def test_object_header_no_longer_crashes_make_runtime() -> None:
    runtime = validator.make_runtime({"bookSourceUrl": "https://a.example", "header": {"User-Agent": "UA-R143"}})
    assert runtime.headers["User-Agent"] == "UA-R143"
    assert json.loads(runtime.source_header) == {"User-Agent": "UA-R143"}


# ---------------------------------------------------------------- output record / pass tag / group token


def _stub_full_chain(monkeypatch, seen: dict[str, Any]) -> None:
    def fake_candidates(_session, src, _runtime):
        seen["explore"] = src.get("exploreUrl", "<missing>")
        return ["https://a.example/book/1"], "https://a.example"

    monkeypatch.setattr(validator, "fetch_book_candidates", fake_candidates)
    monkeypatch.setattr(validator, "recover_book_detail_url_from_reader_candidate", lambda *_a, **_k: "")
    monkeypatch.setattr(validator, "fetch_book_info", lambda *_a, **_k: (True, "Book", "https://a.example/toc", ""))
    monkeypatch.setattr(
        validator,
        "resolve_toc_source",
        lambda *_a, **_k: (True, "https://a.example/toc", "https://a.example/c/1", "Chapter 1", 1, 3, 3),
    )
    monkeypatch.setattr(validator, "resolve_chapter_content", lambda *_a, **_k: (True, "正文" * 40, 1))


def test_output_record_keeps_original_arrays_and_uses_s3_pass_tag(offline, monkeypatch) -> None:
    seen: dict[str, Any] = {}
    _stub_full_chain(monkeypatch, seen)
    src = {
        "bookSourceName": "R143",
        "bookSourceUrl": "https://a.example",
        "searchUrl": "/s?q={{key}}",
        "ruleSearch": {"bookList": ".b"},
        "exploreUrl": [{"title": "a", "url": "/c/1"}],
        "customTag": "old",
        "bookSourceGroup": "old-group",
    }
    mmdd = validator.current_validation_group_tag()
    with app_profiles.app_profile_scope("1.1.2"):
        outcome, record, detail = validator.validate_single_source(src)
    assert outcome.passed and record is not None
    assert seen["explore"] == '[{"title":"a","url":"/c/1"}]'  # rules ran on the App decode view
    assert isinstance(record["exploreUrl"], list)  # output keeps the original value
    assert record["customTag"] == app_profiles.VALIDATED_PASS_TAG == "✅ 完整验证通过（书籍+详情+目录+正文）"
    assert record["bookSourceGroup"] == f"{mmdd},需1.1.2"  # JSON-array exploreUrl is profile sensitive
    assert detail["appVersion"] == "1.1.2"

    with app_profiles.app_profile_scope("1.1.1"):
        outcome, record, detail = validator.validate_single_source(src)
    assert outcome.passed and record is not None
    assert seen["explore"] == "<missing>"  # 1.1.1 drops JSON containers in String fields
    assert isinstance(record["exploreUrl"], list)
    assert record["bookSourceGroup"] == mmdd
    assert detail["appVersion"] == "1.1.1"
    assert detail["appProfileFeatures"] == ["jsonStringField"]


def test_group_token_marks_112_only_sensitive_pass(offline, monkeypatch) -> None:
    seen: dict[str, Any] = {}
    _stub_full_chain(monkeypatch, seen)
    src = {
        "bookSourceName": "R143",
        "bookSourceUrl": "https://a.example",
        "searchUrl": "@js: 'https://a.example/s?q=' + key",
        "ruleSearch": {"bookList": ".b"},
    }
    mmdd = validator.current_validation_group_tag()
    with app_profiles.app_profile_scope("1.1.1"):
        _url, record, _detail = validator.validate_group("https://a.example", [src])
    assert record is not None and record["bookSourceGroup"] == mmdd
    with app_profiles.app_profile_scope("1.1.2"):
        _url, record, _detail = validator.validate_group("https://a.example", [src])
    assert record is not None
    assert record["bookSourceGroup"] == f"{mmdd},需1.1.2"
    assert record["customTag"] == app_profiles.VALIDATED_PASS_TAG


# ---------------------------------------------------------------- threads carry the profile


def test_parallel_stage_threads_inherit_profile() -> None:
    url = "https://thread.example"
    groups = {url: [{"bookSourceName": "T", "bookSourceUrl": url}]}
    seen: list[tuple[str, str]] = []

    def handler(key: str):
        seen.append((threading.current_thread().name, validator.current_app_version()))
        return key, None, {"stages": {}}

    for version in ("1.1.2", "1.1.1"):
        seen.clear()
        with app_profiles.app_profile_scope(version):
            validator.run_parallel_stage("quick-scan", [url], groups, 1, 30, 30, handler)
        assert [item[1] for item in seen] == [version]
    assert validator.current_app_version() == "1.1.1"


def test_round_workers_inherit_profile(monkeypatch) -> None:
    url = "https://round.example"
    groups = {url: [{"bookSourceName": "R", "bookSourceUrl": url}]}
    seen: list[str] = []

    def fake_validate(key, _candidates, quick_seed=None):
        seen.append(validator.current_app_version())
        return key, None, {"stages": {}}

    monkeypatch.setattr(validator, "validate_group", fake_validate)
    with app_profiles.app_profile_scope("1.1.2"):
        validator.run_round_with_idle_timeout([url], groups, 1, 1, 30, 30)
    assert seen == ["1.1.2"]


# ---------------------------------------------------------------- both mode


def _both_sources() -> tuple[dict[str, Any], dict[str, Any]]:
    plain = {
        "bookSourceName": "Plain",
        "bookSourceUrl": "https://plain.example",
        "searchUrl": "/s?q={{key}}",
        "ruleSearch": {"bookList": ".b", "name": "a@text"},
    }
    sensitive = {
        "bookSourceName": "Sensitive",
        "bookSourceUrl": "https://sensitive.example",
        "searchUrl": "@js: 'https://sensitive.example/s?q=' + key",
        "ruleSearch": {"bookList": ".b", "name": "a@text"},
    }
    return plain, sensitive


def _install_fake_stages(monkeypatch, calls: list[tuple[str, str, str]]) -> None:
    def fake_quick(url, candidates):
        calls.append(("quick", validator.current_app_version(), url))
        return url, {"source": dict(candidates[0]), "bookUrls": ["https://x.example/b/1"]}, {
            "bookSourceUrl": url,
            "pipelineStage": "quick-scan",
            "stages": {"search": True},
            "appVersion": validator.current_app_version(),
        }

    def fake_full(url, candidates, quick_seed=None):
        version = validator.current_app_version()
        calls.append(("full", version, url))
        detail = {"bookSourceUrl": url, "pipelineStage": "full-validation", "appVersion": version, "stages": {}}
        if "sensitive" in url and version == "1.1.1":
            detail["failureReason"] = "no search/explore result"
            return url, None, detail
        record = dict(candidates[0])
        record["customTag"] = app_profiles.VALIDATED_PASS_TAG
        record["bookSourceGroup"] = "stale"
        record["__readoriValidation"] = {"bookTitle": url, "bookAuthor": "A", "sampleBookUrl": url}
        return url, record, detail

    monkeypatch.setattr(validator, "quick_scan_group", fake_quick)
    monkeypatch.setattr(validator, "validate_group", fake_full)


def test_both_mode_reuses_non_sensitive_and_labels(monkeypatch) -> None:
    plain, sensitive = _both_sources()
    groups = validator.group_sources([plain, sensitive])
    urls = list(groups)
    calls: list[tuple[str, str, str]] = []
    _install_fake_stages(monkeypatch, calls)

    report, passed_111, passed_112 = validator.run_staged_pipeline_both(urls, groups, 2, 2, 5, 5, 1, 1, 30)

    plain_calls = [(stage, version) for stage, version, url in calls if "plain" in url]
    assert plain_calls == [("quick", "1.1.1"), ("full", "1.1.1")]  # never re-run under 1.1.2
    sensitive_calls = sorted((version, stage) for stage, version, url in calls if "sensitive" in url)
    assert sensitive_calls == [("1.1.1", "full"), ("1.1.1", "quick"), ("1.1.2", "full"), ("1.1.2", "quick")]

    mmdd = validator.current_validation_group_tag()
    assert [item["bookSourceUrl"] for item in passed_111] == ["https://plain.example"]
    assert passed_111[0]["bookSourceGroup"] == "stale"  # fake stages wrote it; 1.1.1 file is not rewritten
    by_url_112 = {item["bookSourceUrl"]: item for item in passed_112}
    assert set(by_url_112) == {"https://plain.example", "https://sensitive.example"}
    assert by_url_112["https://plain.example"]["bookSourceGroup"] == mmdd
    assert by_url_112["https://sensitive.example"]["bookSourceGroup"] == f"{mmdd},需1.1.2"
    assert all("__readoriValidation" not in item for item in passed_111 + passed_112)

    summary = report["summary"]
    assert summary["appVersion"] == "both"
    assert summary["compatibility"] == {"两版均可用": 1, "仅 1.1.2 可用": 1}
    assert summary["profiles"]["1.1.1"]["sourceGroupsPassed"] == 1
    assert summary["profiles"]["1.1.2"]["sourceGroupsPassed"] == 2
    assert summary["profiles"]["1.1.2"]["rerunGroups"] == 1
    assert summary["profiles"]["1.1.2"]["reusedFrom111Groups"] == 1
    assert report["passedSources"] == passed_111
    assert report["passedSourcesByAppVersion"] == {"1.1.2": passed_112}

    results = {item["bookSourceUrl"]: item for item in report["results"]}
    assert results["https://plain.example"]["compatibility"]["label"] == "两版均可用"
    sens = results["https://sensitive.example"]["compatibility"]
    assert (sens["1.1.1"], sens["1.1.2"], sens["label"]) == ("invalid", "pass", "仅 1.1.2 可用")
    assert results["https://sensitive.example"]["appVerdict"] == "invalid"
    assert results["https://plain.example"]["appVerdict"] == "pass"
    assert validator.current_app_version() == "1.1.1"


def test_both_mode_without_sensitive_sources_skips_rerun(monkeypatch) -> None:
    plain, _sensitive = _both_sources()
    groups = validator.group_sources([plain])
    calls: list[tuple[str, str, str]] = []
    _install_fake_stages(monkeypatch, calls)
    report, passed_111, passed_112 = validator.run_staged_pipeline_both(list(groups), groups, 1, 1, 5, 5, 1, 1, 30)
    assert {version for _stage, version, _url in calls} == {"1.1.1"}
    assert len(passed_111) == len(passed_112) == 1
    assert report["summary"]["compatibility"] == {"两版均可用": 1}


# ---------------------------------------------------------------- CLI


def _write_input(tmp_path: Path) -> Path:
    fixture = tmp_path / "sources.json"
    plain, _sensitive = _both_sources()
    fixture.write_text(json.dumps([plain], ensure_ascii=False), encoding="utf-8")
    return fixture


def test_cli_app_version_flag(monkeypatch, tmp_path, capsys) -> None:
    fixture = _write_input(tmp_path)
    seen: dict[str, Any] = {}

    def fake_single(*args):
        seen["single"] = validator.current_app_version()
        return {"summary": {}, "results": [], "passedSources": []}, []

    def fake_both(*args):
        seen["both"] = True
        return {"summary": {}, "results": [], "passedSources": []}, [], []

    monkeypatch.setattr(validator, "run_staged_pipeline", fake_single)
    monkeypatch.setattr(validator, "run_staged_pipeline_both", fake_both)
    common = ["--input", str(fixture), "--report-only", "--no-mirror", "--report-path", str(tmp_path / "r.json")]

    assert validator.main(common) == 0
    assert seen == {"single": "1.1.1"}
    out = capsys.readouterr().out
    lines = out.splitlines()
    loaded = next(i for i, line in enumerate(lines) if line.startswith("Loaded "))
    assert lines[loaded + 1] == "App profile: 1.1.1"

    seen.clear()
    assert validator.main(common + ["--app-version", "1.1.2"]) == 0
    assert seen == {"single": "1.1.2"}
    assert "App profile: 1.1.2" in capsys.readouterr().out
    assert validator.current_app_version() == "1.1.1"

    seen.clear()
    assert validator.main(common + ["--app-version", "both"]) == 0
    assert seen == {"both": True}
    assert "App profile: both" in capsys.readouterr().out

    assert validator.main(common + ["--app-version", "both", "--pipeline", "legacy"]) == 2
    with pytest.raises(SystemExit):
        validator.main(common + ["--app-version", "1.0.0"])


def test_cli_both_writes_app_112_files(monkeypatch, tmp_path, capsys) -> None:
    fixture = _write_input(tmp_path)
    record_111 = {"bookSourceUrl": "https://plain.example", "bookSourceName": "Plain"}
    record_112 = [record_111, {"bookSourceUrl": "https://sensitive.example", "bookSourceName": "Sensitive"}]
    monkeypatch.setattr(
        validator,
        "run_staged_pipeline_both",
        lambda *args: ({"summary": {}, "results": [], "passedSources": [record_111]}, [record_111], record_112),
    )
    out111 = tmp_path / "out" / "validated.json"
    full111 = tmp_path / "out" / "validated-full.json"
    rc = validator.main([
        "--input", str(fixture), "--app-version", "both", "--no-mirror",
        "--report-path", str(tmp_path / "r.json"),
        "--validated-output", str(out111), "--validated-output-full", str(full111),
    ])
    assert rc == 0
    assert json.loads(out111.read_text(encoding="utf-8")) == [record_111]
    assert json.loads(full111.read_text(encoding="utf-8")) == [record_111]
    assert json.loads((tmp_path / "out" / "validated.app-1.1.2.json").read_text(encoding="utf-8")) == record_112
    assert json.loads((tmp_path / "out" / "validated-full.app-1.1.2.json").read_text(encoding="utf-8")) == record_112
    assert "validated sources (App 1.1.2) to" in capsys.readouterr().out


def test_cli_single_version_keeps_output_paths(monkeypatch, tmp_path) -> None:
    fixture = _write_input(tmp_path)
    monkeypatch.setattr(
        validator,
        "run_staged_pipeline",
        lambda *args: ({"summary": {}, "results": [], "passedSources": []}, []),
    )
    out = tmp_path / "validated.json"
    rc = validator.main([
        "--input", str(fixture), "--app-version", "1.1.2", "--no-mirror",
        "--report-path", str(tmp_path / "r.json"),
        "--validated-output", str(out), "--validated-output-full", str(tmp_path / "full.json"),
    ])
    assert rc == 0
    assert out.exists() and not (tmp_path / "validated.app-1.1.2.json").exists()


def test_staged_pipeline_summary_carries_profile(monkeypatch) -> None:
    plain, _sensitive = _both_sources()
    groups = validator.group_sources([plain])
    _install_fake_stages(monkeypatch, [])
    with app_profiles.app_profile_scope("1.1.2"):
        report, passed = validator.run_staged_pipeline(list(groups), groups, 1, 1, 5, 5, 1, 1, 30)
    summary = report["summary"]
    assert summary["appVersion"] == "1.1.2"
    assert summary["appProfile"]["version"] == "1.1.2"
    assert summary["appVerdicts"] == {"pass": 1, "invalid": 0, "needsInteraction": 0, "timeout": 0}
    assert report["results"][0]["appVerdict"] == "pass"
    assert len(passed) == 1


def test_empty_detail_carries_app_fields() -> None:
    detail = validator._empty_detail({"bookSourceName": "x", "bookSourceUrl": "https://a.example"})
    assert detail["appVersion"] == "1.1.1"
    assert detail["appProfileFeatures"] == []
    assert detail["appVerdict"] == ""
    assert detail["suppressedInteractions"] == []
    assert detail["failureBeforeInteraction"] is False


# ---------------------------------------------------------------- R139 carry-over: score_candidate


def test_score_candidate_tolerates_bad_types() -> None:
    bad = {"bookSourceUrl": "https://a.example", "customTag": ["x"], "weight": "abc", "searchUrl": "/s"}
    assert validator.score_candidate(bad) == 100  # searchUrl counts for completeness only
    assert validator.score_candidate({"customTag": 5, "weight": None}) == 0
    assert validator.score_candidate({"weight": "7"}) == 7
    assert validator.score_candidate({"weight": 3.9}) == 3
    assert validator.score_candidate({"weight": float("nan")}) == 0
    assert validator.score_candidate({"customTag": app_profiles.VALIDATED_PASS_TAG}) == 40_000
    groups = validator.group_sources([
        bad,
        dict(bad, customTag={"a": 1}, weight=[1]),
        dict(bad, bookSourceUrl="https://b.example", customTag=None, weight={"x": 1}),
    ])
    assert len(groups) == 2


def test_validator_does_not_write_the_legacy_pass_tag() -> None:
    text = (ROOT / "validator" / "validate_source_packages.py").read_text(encoding="utf-8")
    assert "✅ 书籍+详情+目录+正文通过" not in text
    assert 'template = str(src.get("searchUrl") or "").strip()' not in text
    assert "app_profiles.bind_current_profile(worker)" in text
    assert text.count("app_profiles.app_decode_view(original)") == 2


# ======================================================================================================
# WP-B2: JS runtime per profile (CryptoJS, App globals) and the S1 interaction model
# ======================================================================================================

needs_node = pytest.mark.skipif(not validator.node_binary_path(), reason="node is not available")
needs_quickjs = pytest.mark.skipif(validator.quickjs is None, reason="quickjs is not installed")

AES_MESSAGE_JS = (
    "CryptoJS.AES.encrypt('Message', CryptoJS.enc.Utf8.parse('0123456789abcdef'),"
    " {iv: CryptoJS.enc.Utf8.parse('abcdef0123456789')}).toString()"
)
AES_MESSAGE_B64 = "xjStDeDPpBHzGy+poB0qYA=="
ROLLUP_JSLIB = "var CryptoJS=CryptoJS||function(){return {mine:true}}();"


def _plain_runtime(js_lib: str | None = None) -> validator.RuleRuntime:
    """A directly constructed runtime: interaction is allowed (legacy behavior), like the existing unit tests."""
    return validator.RuleRuntime(
        source_url="https://a.example",
        base_url="https://a.example",
        book_source_url="https://a.example",
        headers={},
        js_lib=js_lib,
    )


def _run_js(
    code: str,
    version: str,
    runtime: validator.RuleRuntime | None = None,
    js_lib: str | None = None,
) -> tuple[str, str | None]:
    """evaluate_js under a profile, then surface the stage's script error the way the pipeline does."""
    rt = runtime or _plain_runtime(js_lib)
    with app_profiles.app_profile_scope(version):
        sink = validator.begin_script_error_stage(rt)
        out = validator.evaluate_js(code, "", rt)
        try:
            validator.finish_script_error_stage(rt, sink, {}, "search")
        except validator.RuleScriptError as exc:
            return out, str(exc)
    return out, None


# ---------------------------------------------------------------- A3 CryptoJS per profile


@needs_js
def test_cryptojs_aes_encrypt_per_profile(offline) -> None:
    out, error = _run_js(AES_MESSAGE_JS, "1.1.2")
    assert (out, error) == (AES_MESSAGE_B64, None)
    out, error = _run_js(AES_MESSAGE_JS, "1.1.1")
    assert out == ""
    assert error is not None and "is not a function" in error  # the 1.1.1 fake has no AES.encrypt


@needs_js
def test_cryptojs_jslib_rollup_shadowing(offline) -> None:
    # evaluate_js maps the string "undefined" to "", so prefix the value.
    code = "'mine=' + String(CryptoJS.mine)"
    assert _run_js(code, "1.1.1", js_lib=ROLLUP_JSLIB) == ("mine=undefined", None)  # the fake global short-circuits it
    assert _run_js(code, "1.1.2", js_lib=ROLLUP_JSLIB) == ("mine=true", None)  # self-defined: builtin is not installed


@needs_js
def test_cryptojs_legacy_shapes_111(offline) -> None:
    md5_type = "typeof CryptoJS.MD5('Message')"
    cbc = "CryptoJS.AES.CBC.encrypt('abc','k')"
    global_type = "typeof globalThis['Crypto'+'JS']"
    code = f"{md5_type} + '|' + {cbc} + '|' + {global_type}"
    assert _run_js(code, "1.1.1") == ("string|YWJj|object", None)
    assert _run_js(f"'t=' + {md5_type}", "1.1.2") == ("t=object", None)
    out, error = _run_js(cbc, "1.1.2")
    assert out == "" and error is not None and error.startswith("TypeError")
    # 1.1.2 installs only when the text mentions CryptoJS literally: an obfuscated global lookup stays undefined.
    assert _run_js(f"'g=' + {global_type}", "1.1.2") == ("g=undefined", None)
    assert _run_js("'g=' + typeof CryptoJS", "1.1.2") == ("g=object", None)


@needs_js
def test_cryptojs_112_passphrase_and_des(offline) -> None:
    decrypt = "CryptoJS.AES.decrypt('U2FsdGVkX18BAgMEBQYHCMemLnc0RQ221UfFXRPKuHg=','secret').toString(CryptoJS.enc.Utf8)"
    assert _run_js(decrypt, "1.1.2") == ("Message", None)
    des = (
        "CryptoJS.DES.encrypt('Message', CryptoJS.enc.Utf8.parse('12345678'),"
        " {iv: CryptoJS.enc.Utf8.parse('12345678')}).toString()"
    )
    assert _run_js(des, "1.1.2") == ("dg70b6zDmjU=", None)
    md5 = "CryptoJS.MD5('Message').toString()"
    assert _run_js(md5, "1.1.2") == ("4c2a8fe7eaf24721cc7a9f0175115bd4", None)


@needs_js
def test_app_globals_in_js_both_profiles(offline) -> None:
    code = "btoa('你') + '|' + atob('5L2g') + '|' + md5('Message') + '|' + atob('###')"
    expected = "5L2g|你|4c2a8fe7eaf24721cc7a9f0175115bd4|###"
    for version in ("1.1.1", "1.1.2"):
        assert _run_js(code, version) == (expected, None)


@needs_js
def test_java_base64_is_binary_safe_without_global_atob(offline) -> None:
    # latin1 round trip through the prelude's Base64 (no Buffer on the QuickJS path): bytes e4 ff -> "5P8="
    code = "Base64.getEncoder().encodeToString([-28, -1]).trim() + '|' + Base64.decode('5P8=', 0).join(',')"
    assert _run_js(code, "1.1.1") == ("5P8=|-28,-1", None)
    code = "__readoriLatin1Btoa('\\u00e4\\u00ff') + '|' + __readoriLatin1Atob('5P8=').length + '|' + __readoriLatin1Btoa('')"
    assert _run_js(code, "1.1.2") == ("5P8=|2|", None)


# ---------------------------------------------------------------- native bridge parity (node vs Python)


@needs_node
def test_node_native_bridges_match_python_natives(offline) -> None:
    key = "30313233343536373839616263646566"
    iv = "61626364656630313233343536373839"
    block = "4d657373616765" + "09" * 9
    cases = [
        ("__readoriCryptoJSCipher", ["e", "AES", "CBC", key, iv, block]),
        ("__readoriCryptoJSCipher", ["d", "AES", "CBC", key, iv, base64.b64decode(AES_MESSAGE_B64).hex()]),
        ("__readoriCryptoJSCipher", ["e", "AES", "ECB", key, "", block]),
        ("__readoriCryptoJSCipher", ["e", "aes", "CFB", key, iv, "4d657373616765"]),
        ("__readoriCryptoJSCipher", ["e", "AES", "OFB", key, iv, "4d657373616765"]),
        ("__readoriCryptoJSCipher", ["e", "AES", "CTR", key, iv, "4d657373616765"]),
        ("__readoriCryptoJSCipher", ["e", "AES", "CBC", key, "6162", block]),  # short iv is zero padded
        ("__readoriCryptoJSCipher", ["e", "DES", "CBC", "3132333435363738", "3132333435363738", "4d657373616765" + "09"]),
        ("__readoriCryptoJSCipher", ["e", "DES", "ECB", "3132333435363738", "", "4d657373616765" + "09"]),
        ("__readoriCryptoJSCipher", ["e", "3DES", "CBC", key + "3031323334353637", "3132333435363738", "4d657373616765" + "09"]),
        ("__readoriCryptoJSCipher", ["e", "DES", "CTR", "3132333435363738", "3132333435363738", "4d657373616765" + "09"]),
        ("__readoriCryptoJSCipher", ["e", "AES", "CBC", "3031323334", iv, block]),  # bad key length
        ("__readoriCryptoJSCipher", ["e", "AES", "CBC", key, iv, "4d65737361"]),  # not block aligned
        ("__readoriCryptoJSCipher", ["e", "AES", "GCM", key, iv, block]),
        ("__readoriCryptoJSCipher", ["e", "RC4", "CBC", key, iv, block]),
        ("__readoriCryptoJSCipher", ["e", "AES", "CBC", "zz", iv, block]),
        ("__readoriCryptoJSDigest", ["MD5", "4d657373616765"]),
        ("__readoriCryptoJSDigest", ["sha-256", "4d657373616765"]),
        ("__readoriCryptoJSDigest", ["SHA512", ""]),
        ("__readoriCryptoJSDigest", ["CRC32", "00"]),
        ("__readoriCryptoJSHmac", ["SHA256", "6b6579", "4d657373616765"]),
        ("__readoriCryptoJSHmac", ["MD5", "6b6579", "4d657373616765"]),
        ("__readoriCryptoJSPBKDF2", ["SHA256", "70617373", "73616c74", 3, 16]),
        ("__readoriCryptoJSPBKDF2", ["MD5", "70617373", "73616c74", 3, 16]),
        ("__readoriCryptoJSPBKDF2", ["SHA1", "70617373", "73616c74", 3, 0]),
    ]
    bridges = {
        "__readoriCryptoJSCipher": app_profiles.cryptojs_cipher_hex,
        "__readoriCryptoJSDigest": app_profiles.cryptojs_digest_hex,
        "__readoriCryptoJSHmac": app_profiles.cryptojs_hmac_hex,
        "__readoriCryptoJSPBKDF2": app_profiles.cryptojs_pbkdf2_hex,
    }
    script = "JSON.stringify(%s.map(function(c) { return this[c[0]].apply(null, c[1]); }, this))" % json.dumps(cases)
    rt = _plain_runtime()
    with app_profiles.app_profile_scope("1.1.2"):
        node_values = json.loads(validator.evaluate_js_with_node(script, "", rt))
    expected = [bridges[name](*args) for name, args in cases]
    assert node_values == expected
    assert expected[0] == base64.b64decode(AES_MESSAGE_B64).hex() and expected[1] == block
    assert expected[11] == "!Invalid key length" and expected[12] == "!data not block aligned"
    random_values = json.loads(
        validator.evaluate_js_with_node(
            "JSON.stringify([__readoriCryptoJSRandom(4).length, __readoriCryptoJSRandom(0), __readoriCryptoJSRandom(2000)])",
            "",
            rt,
        )
    )
    assert random_values == [8, "", "!invalid length"]


@needs_quickjs
def test_quickjs_native_bridges_match_python_natives() -> None:
    """The QuickJS natives are pure JS (python-quickjs cannot call Python under a time limit): same bytes as Python."""
    ctx = validator.make_quickjs_context()
    ctx.eval(app_profiles.QUICKJS_APP_NATIVES_JS)
    key16, key24, key32 = "30313233343536373839616263646566", "31" * 24, "32" * 32
    iv = "61626364656630313233343536373839"
    msg = "4d657373616765" + "09" * 9
    long_msg = bytes(range(200)).hex()
    cases: list[tuple[str, Any, list[Any]]] = []
    for algorithm in ("MD5", "SHA1", "SHA224", "SHA-256", "SHA384", "sha512", "CRC32"):
        for data in ("", "4d657373616765", long_msg):
            cases.append(("__readoriCryptoJSDigest", app_profiles.cryptojs_digest_hex, [algorithm, data]))
            cases.append(("__readoriCryptoJSHmac", app_profiles.cryptojs_hmac_hex, [algorithm, "6b6579", data]))
    cases.append(("__readoriCryptoJSHmac", app_profiles.cryptojs_hmac_hex, ["SHA512", "ab" * 150, long_msg]))
    for hasher in ("SHA1", "SHA256", "SHA384", "SHA512", "MD5"):
        cases.append(("__readoriCryptoJSPBKDF2", app_profiles.cryptojs_pbkdf2_hex, [hasher, "70617373", "73616c74", 3, 70]))
    for mode in ("CBC", "ECB", "CFB", "OFB", "CTR", "GCM"):
        for key in (key16, key24, key32):
            for operation in ("e", "d"):
                cases.append(("__readoriCryptoJSCipher", app_profiles.cryptojs_cipher_hex, [operation, "AES", mode, key, iv, long_msg[:96]]))
        for algorithm, key in (("DES", "3132333435363738"), ("3DES", key24)):
            cases.append(("__readoriCryptoJSCipher", app_profiles.cryptojs_cipher_hex, ["e", algorithm, mode, key, iv[:16], msg + "09"]))
            cases.append(("__readoriCryptoJSCipher", app_profiles.cryptojs_cipher_hex, ["d", algorithm, mode, key, "", long_msg[:64]]))
    cases += [
        ("__readoriCryptoJSCipher", app_profiles.cryptojs_cipher_hex, ["e", "AES", "CTR", key16, "ff" * 16, long_msg[:100]]),
        ("__readoriCryptoJSCipher", app_profiles.cryptojs_cipher_hex, ["e", "AES", "CBC", key16, "6162", msg]),
        ("__readoriCryptoJSCipher", app_profiles.cryptojs_cipher_hex, ["e", "AES", "CBC", "3031323334", iv, msg]),
        ("__readoriCryptoJSCipher", app_profiles.cryptojs_cipher_hex, ["e", "AES", "CBC", key16, iv, "4d65737361"]),
        ("__readoriCryptoJSCipher", app_profiles.cryptojs_cipher_hex, ["e", "RC4", "CBC", key16, iv, msg]),
        ("__readoriCryptoJSCipher", app_profiles.cryptojs_cipher_hex, ["e", "AES", "CBC", "zz", iv, msg]),
        ("__readoriCryptoJSPBKDF2", app_profiles.cryptojs_pbkdf2_hex, ["SHA256", "zz", "00", 1, 4]),
        ("__readoriCryptoJSPBKDF2", app_profiles.cryptojs_pbkdf2_hex, ["SHA256", "00", "00", 1, 0]),
    ]
    mismatches = []
    for name, python_native, args in cases:
        got = ctx.eval("%s.apply(null, %s)" % (name, json.dumps(args)))
        expected = python_native(*args)
        if got != expected:
            mismatches.append((name, args, expected, got))
    assert mismatches == []
    assert ctx.eval("__readoriCryptoJSRandom(4).length") == 8
    assert ctx.eval("[__readoriCryptoJSRandom(0), __readoriCryptoJSRandom(2000)].join('|')") == "|!invalid length"
    for text in ("", "Message", "你好", "aäÿ", "😀x"):
        assert ctx.eval("btoa(%s)" % json.dumps(text)) == app_profiles.app_btoa(text)
        assert ctx.eval("md5(%s)" % json.dumps(text)) == app_profiles.app_md5(text)
        assert ctx.eval("sha256(%s)" % json.dumps(text)) == app_profiles.app_sha256(text)
    for text in ("", "5L2g", "###", "TWVzc2FnZQ==", "TWVzc2FnZQ", "/w==", "7aCA", "8J+YgA==", "TW Vz", "w4w=", "9J+Ygg=="):
        assert ctx.eval("atob(%s)" % json.dumps(text)) == app_profiles.app_atob(text)


@needs_quickjs
def test_quickjs_python_natives_run_through_replay(monkeypatch) -> None:
    """A time-limited QuickJS cannot call Python: natives are answered from a cache after a replay, each executed once."""
    fetched: list[str] = []

    def fake_fetch(_session, url, *_args, **_kwargs):
        fetched.append(url)
        return f"<div class='x'>page-{url[-1]}</div>", url, 200

    monkeypatch.setattr(validator, "fetch_text", fake_fetch)
    monkeypatch.setattr(validator, "node_binary_path", lambda: "")
    rt = _plain_runtime()
    code = (
        "java.put('k', 'v'); var a = java.ajax('/p1'); var b = java.ajax('/p2');"
        " java.getString('class.x@text', a) + '|' + java.getString('class.x@text', b) + '|' + java.ajax('/p1').length"
    )
    assert validator.evaluate_js(code, "", rt) == "page-1|page-2|" + str(len("<div class='x'>page-1</div>"))
    assert fetched == ["https://a.example/p1", "https://a.example/p2", "https://a.example/p1"]
    assert (rt.variables or {}).get("k") == "v"
    assert validator.evaluate_js("java.getString('class.x@text', '<b class=x>t</b>')", "", rt) == "t"


@needs_quickjs
def test_quickjs_replay_defers_interactions_and_script_errors_to_the_final_attempt(monkeypatch) -> None:
    monkeypatch.setattr(validator, "node_binary_path", lambda: "")
    src = {"bookSourceName": "R143", "bookSourceUrl": "https://a.example", "searchUrl": "/s?q={{key}}", "ruleSearch": {"bookList": ".b"}}
    rt = validator.make_runtime(src)
    code = "var t = java.getString('class.x@text', '<b class=x>t</b>'); java.startBrowserAwait('https://a.example/v', 'v'); t"
    out, error = _run_js(code, "1.1.2", runtime=rt)
    assert out == ""
    assert error is not None and "验证结果为空" in error and "__readoriNativeMiss" not in error
    assert rt.interaction_state["kinds"] == ["startBrowserAwait"]


def test_quickjs_value_text_matches_the_node_runner() -> None:
    class FakeObject:
        def json(self) -> str:
            return '{"n":""}'

    class FakeFunction:
        def json(self) -> None:
            return None

    assert validator.quickjs_value_text(None) == ""
    assert validator.quickjs_value_text("s") == "s"
    assert validator.quickjs_value_text(True) == "true"
    assert validator.quickjs_value_text(False) == "false"
    assert validator.quickjs_value_text(5) == "5"
    assert validator.quickjs_value_text(1.0) == "1"
    assert validator.quickjs_value_text(1.5) == "1.5"
    assert validator.quickjs_value_text(float("nan")) == "NaN"
    assert validator.quickjs_value_text(float("-inf")) == "-Infinity"
    assert validator.quickjs_value_text(FakeObject()) == '{"n":""}'
    assert validator.quickjs_value_text(FakeFunction()) == ""


@needs_quickjs
def test_quickjs_object_results_are_json_like_node() -> None:
    assert validator.evaluate_js('({n:""});', "", _plain_runtime()) == '{"n":""}'
    assert validator.evaluate_js("[1, 'a', {x: 1}]", "", _plain_runtime()) == '[1,"a",{"x":1}]'
    assert validator.evaluate_js("1 < 2", "", _plain_runtime()) == "true"


def test_jsonpath_accepts_a_bare_property_right_after_a_bracket_like_jayway() -> None:
    data = {"data": {"page": [{"image": "a"}, {"image": "b"}]}}
    expr = validator.parse_jsonpath_expression("$.data.page[*]image")
    assert [m.value for m in expr.find(data)] == ["a", "b"]
    quoted = validator.parse_jsonpath_expression("$.data['pa]ge'][0]image")
    assert [m.value for m in quoted.find({"data": {"pa]ge": [{"image": "z"}]}})] == ["z"]


# ---------------------------------------------------------------- QuickJS install order (recorded, no quickjs needed)


class _RecordingContext:
    """Stands in for quickjs.Context: records add_callable / set / eval so the install order can be asserted."""

    def __init__(self) -> None:
        self.ops: list[tuple[str, str]] = []

    def set_time_limit(self, *_args) -> None:
        pass

    def set_memory_limit(self, *_args) -> None:
        pass

    def set(self, name, _value) -> None:
        self.ops.append(("set", name))

    def add_callable(self, name, _function) -> None:
        self.ops.append(("callable", name))

    def eval(self, source):
        self.ops.append(("eval", source))
        return None


def _quickjs_op_names(ops: list[tuple[str, str]]) -> list[str]:
    names: list[str] = []
    prelude = validator._build_java_api_js(_plain_runtime())
    for kind, value in ops:
        if kind != "eval":
            names.append(f"{kind}:{value}")
        elif value == prelude:
            names.append("eval:prelude")
        elif value == app_profiles.QUICKJS_APP_NATIVES_JS:
            names.append("eval:app-natives")
        elif value == app_profiles.APP_111_CRYPTOJS_POLYFILL:
            names.append("eval:legacy-cryptojs")
        elif value == app_profiles.APP_112_CRYPTOJS_BUILTIN:
            names.append("eval:builtin-cryptojs")
        elif value == ROLLUP_JSLIB:
            names.append("eval:jslib")
        elif "CryptoJS.mine" in value:
            names.append("eval:code")
    return names


def _is_app_native(op_name: str) -> bool:
    name = op_name.split(":", 1)[1]
    return name in ("btoa", "atob", "md5", "sha256") or name.startswith(("__readoriCryptoJS", "__readoriRecordSuppressed"))


def _recorded_quickjs_run(monkeypatch, version: str, code: str, js_lib: str | None) -> list[str]:
    ctx = _RecordingContext()
    monkeypatch.setattr(validator, "make_quickjs_context", lambda: ctx)
    monkeypatch.setattr(validator, "node_binary_path", lambda: "")
    rt = _plain_runtime(js_lib)
    with app_profiles.app_profile_scope(version):
        validator.evaluate_js(code, "", rt)
    return _quickjs_op_names(ctx.ops)


def test_quickjs_path_installs_app_globals_and_profile_cryptojs_in_order(monkeypatch) -> None:
    code = "String(CryptoJS.mine)"
    ops = _recorded_quickjs_run(monkeypatch, "1.1.1", code, ROLLUP_JSLIB)
    # python-quickjs cannot call back into Python while a time limit is set: the App globals / CryptoJS bridges /
    # interaction log are one pure-JS install (no add_callable) that comes before the java prelude.
    assert not [name for name in ops if name.startswith("callable:") and _is_app_native(name)]
    assert "set:__readoriAllowsUserInteraction" in ops
    order = [name for name in ops if name.startswith("eval:")]
    assert order[:5] == ["eval:app-natives", "eval:prelude", "eval:legacy-cryptojs", "eval:jslib", "eval:code"]
    assert "eval:builtin-cryptojs" not in ops

    ops = _recorded_quickjs_run(monkeypatch, "1.1.2", code, ROLLUP_JSLIB)
    order = [name for name in ops if name.startswith("eval:")]
    # jsLib defines CryptoJS itself: no builtin before it; the code references CryptoJS without defining it, so the
    # builtin is installed (and is a no-op at runtime because the global already exists).
    assert order[:5] == ["eval:app-natives", "eval:prelude", "eval:jslib", "eval:builtin-cryptojs", "eval:code"]
    assert "eval:legacy-cryptojs" not in ops
    assert not [name for name in ops if name.startswith("callable:") and _is_app_native(name)]

    ops = _recorded_quickjs_run(monkeypatch, "1.1.2", code, "var helper = 1; CryptoJS.AES;")
    order = [name for name in ops if name.startswith("eval:")]
    assert order.index("eval:builtin-cryptojs") < order.index("eval:code")

    ops = _recorded_quickjs_run(monkeypatch, "1.1.2", "1 + 1", None)
    assert "eval:builtin-cryptojs" not in ops and "eval:legacy-cryptojs" not in ops  # not referenced: not installed


# ---------------------------------------------------------------- S1: interaction is blocked in the pipeline runtime


@needs_js
def test_suppressed_start_browser_await_is_script_error(offline) -> None:
    src = {"bookSourceName": "R143", "bookSourceUrl": "https://a.example", "searchUrl": "/s?q={{key}}", "ruleSearch": {"bookList": ".b"}}
    rt = validator.make_runtime(src)
    assert rt.allows_user_interaction is False
    out, error = _run_js("java.startBrowserAwait('https://a.example/v', 'v').body()", "1.1.1", runtime=rt)
    assert out == ""
    assert error is not None and "验证结果为空" in error
    assert rt.interaction_state["kinds"] == ["startBrowserAwait"]
    assert rt.interaction_state["failuresAtFirst"] == 0
    assert offline == []  # nothing was prefetched or requested for the blocked browser URL


@needs_js
def test_suppressed_interaction_state_is_shared_with_child_runtimes(offline) -> None:
    src = {"bookSourceUrl": "https://a.example"}
    rt = validator.make_runtime(src)
    child = validator.RuleRuntime(**{**rt.__dict__, "base_url": "https://a.example/x"})
    _run_js("java.getVerificationCode('https://a.example/c.png')", "1.1.2", runtime=child)
    assert rt.interaction_state["kinds"] == ["getVerificationCode"]


@needs_js
def test_other_interaction_apis_are_recorded_like_the_app(offline) -> None:
    rt = validator.make_runtime({"bookSourceUrl": "https://a.example"})
    code = (
        "var a = java.showReadingBrowser('https://a.example/r', 't').body();"
        "var b = java.showBrowser('https://a.example/u', '<p>x</p>', '').body();"
        "var c = java.showBrowser('https://a.example/u', '', '').body();"
        "var d = java.startBrowserDp('weixin://dl', 't');"
        "var e = java.startBrowserDp('https://a.example/d', 't');"
        "var f = java.getVerificationCode('https://a.example/c.png');"
        "var g = java.startBrowser('https://a.example/l', 't');"
        "java.openUrl('https://a.example/o');"
        "'done:' + a + b + c + d + e + f + g"
    )
    out, error = _run_js(code, "1.1.2", runtime=rt)
    assert (out, error) == ("done:", None)  # showReadingBrowser / showBrowser answer with empty bodies, no throw
    # openUrl and non-http deep links are not recorded (BookSourceBrowserBridge only records the browser calls)
    assert rt.interaction_state["kinds"] == ["startBrowserAwait", "showBrowser", "startBrowser", "getVerificationCode"]
    only_dp = validator.make_runtime({"bookSourceUrl": "https://a.example"})
    _run_js("java.startBrowserDp('weixin://dl', 't'); java.openUrl('https://a.example/o'); 'x'", "1.1.2", runtime=only_dp)
    assert only_dp.interaction_state["kinds"] == []
    assert only_dp.interaction_state["failuresAtFirst"] is None


@needs_js
def test_directly_constructed_runtime_keeps_browser_await_get_behavior(offline, monkeypatch) -> None:
    monkeypatch.setattr(
        validator,
        "fetch_text",
        lambda _session, url, *_args, **_kwargs: (("<p>verified</p>" if url.endswith("/v") else ""), url, 200),
    )
    rt = _plain_runtime()
    assert rt.allows_user_interaction is True and rt.interaction_state is None
    out, error = _run_js("java.startBrowserAwait('https://a.example/v', 'v').body()", "1.1.1", runtime=rt)
    assert (out, error) == ("<p>verified</p>", None)
    assert rt.interaction_state is None


def test_note_failure_and_first_record_semantics() -> None:
    rt = validator.make_runtime({"bookSourceUrl": "https://a.example"})
    validator.note_app_validation_failure(rt)
    validator.record_suppressed_interaction(rt, "startBrowserAwait")
    validator.record_suppressed_interaction(rt, "startBrowserAwait")
    validator.record_suppressed_interaction(rt, "showBrowser")
    validator.note_app_validation_failure(rt)
    assert rt.interaction_state == {"kinds": ["startBrowserAwait", "showBrowser"], "failuresAtFirst": 1, "failures": 2}
    detail: dict[str, Any] = {}
    validator.apply_interaction_state_to_detail(detail, rt.interaction_state)
    assert detail == {"suppressedInteractions": ["startBrowserAwait", "showBrowser"], "failureBeforeInteraction": True}
    # a plain runtime has no state: recording is a no-op
    plain = _plain_runtime()
    validator.record_suppressed_interaction(plain, "startBrowser")
    validator.note_app_validation_failure(plain)
    assert plain.interaction_state is None


# ---------------------------------------------------------------- S1: pipeline verdicts per profile

BOOK_PAGE = "<html><body><div class='b'><a href='/b/1'>Book One</a></div></body></html>"


def _run_pipeline(src: dict[str, Any], version: str) -> dict[str, Any]:
    groups = validator.group_sources([src])
    with app_profiles.app_profile_scope(version):
        report, _passed = validator.run_staged_pipeline(list(groups), groups, 1, 1, 30, 30, 1, 1, 60)
    results = report["results"]
    assert len(results) == 1
    return results[0]


@pytest.fixture
def book_pages(monkeypatch):
    """/s?... returns nothing (status 0); every other page is a one-book list. Records the fetched URLs."""
    fetched: list[str] = []

    def fake_fetch_text(_session, url, *_args, **_kwargs):
        fetched.append(url)
        if "/s?" in url:
            return "", url, 0
        return BOOK_PAGE, url, 200

    monkeypatch.setattr(validator, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kwargs: _FakeResponse())
    return fetched


@needs_js
def test_needs_interaction_verdict_112_only(book_pages) -> None:
    src = {
        "bookSourceName": "Interactive",
        "bookSourceUrl": "https://a.example",
        "searchUrl": "/p?q={{key}}",
        "ruleSearch": {
            "bookList": "<js>java.startBrowserAwait(baseUrl,'v');result</js>",
            "name": "a@text",
            "bookUrl": "a@href",
        },
    }
    detail111 = _run_pipeline(src, "1.1.1")
    assert detail111["appVerdict"] == "invalid"
    assert detail111["suppressedInteractions"] == ["startBrowserAwait"]
    assert detail111["failureBeforeInteraction"] is False
    detail112 = _run_pipeline(src, "1.1.2")
    assert detail112["appVerdict"] == "needsInteraction"
    assert detail112["suppressedInteractions"] == ["startBrowserAwait"]
    assert detail112["failureBeforeInteraction"] is False


@needs_js
def test_search_failure_before_interaction_stays_invalid(book_pages) -> None:
    src = {
        "bookSourceName": "SearchFirst",
        "bookSourceUrl": "https://a.example",
        "searchUrl": "/s?q={{key}}",
        "ruleSearch": {"bookList": ".b", "name": "a@text", "bookUrl": "a@href"},
        "exploreUrl": "/c/1",
        "ruleExplore": {
            "bookList": "<js>java.startBrowserAwait(baseUrl,'v');result</js>",
            "name": "a@text",
            "bookUrl": "a@href",
        },
    }
    detail112 = _run_pipeline(src, "1.1.2")
    assert detail112["suppressedInteractions"] == ["startBrowserAwait"]
    assert detail112["failureBeforeInteraction"] is True  # search already failed for real
    assert detail112["appVerdict"] == "invalid"
    assert _run_pipeline(src, "1.1.1")["appVerdict"] == "invalid"


@needs_js
def test_interaction_state_travels_from_quick_scan_to_full_validation(book_pages) -> None:
    # Search works and the book page is fine; the detail rule asks for a browser: the quick-scan seed carries the
    # (empty) state, full validation records the interaction and the verdict is per profile.
    src = {
        "bookSourceName": "DetailBrowser",
        "bookSourceUrl": "https://a.example",
        "searchUrl": "/p?q={{key}}",
        "ruleSearch": {"bookList": ".b", "name": "a@text", "bookUrl": "a@href"},
        "ruleBookInfo": {"name": "<js>java.startBrowserAwait(baseUrl,'v');result</js>"},
    }
    passed, seed, detail = validator.quick_scan_single_source(src)
    assert passed and seed is not None
    assert seed["interactionState"] == {"kinds": [], "failuresAtFirst": None, "failures": 0}
    assert detail["suppressedInteractions"] == []
    outcome, merged, full_detail = validator.validate_single_source(src, seed)
    assert merged is None and outcome.passed is False
    assert full_detail["suppressedInteractions"] == ["startBrowserAwait"]
    assert full_detail["failureBeforeInteraction"] is False
    with app_profiles.app_profile_scope("1.1.2"):
        assert app_profiles.app_verdict(full_detail, False) == "needsInteraction"
    assert app_profiles.app_verdict(full_detail, False) == "invalid"  # default profile 1.1.1


def test_static_interaction_kinds_are_recorded_on_the_original_record(offline) -> None:
    src = {
        "bookSourceName": "Static",
        "bookSourceUrl": "https://a.example",
        "searchUrl": "/s?q={{key}}",
        "ruleSearch": {"bookList": ".b", "name": "<js>java.getVerificationCode('u')</js>"},
        "exploreUrl": [{"title": "t", "url": "/c"}],
    }
    passed, seed, detail = validator.quick_scan_single_source(src)
    assert (passed, seed) == (False, None)
    assert detail["failureReason"] == "interactive verification required"
    assert detail["staticInteractionKinds"] == ["getVerificationCode"]
    with app_profiles.app_profile_scope("1.1.2"):
        assert app_profiles.app_verdict(detail, False) == "needsInteraction"
    assert app_profiles.app_verdict(detail, False) == "invalid"
