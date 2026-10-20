"""R153: Legado live reference tooling (offline): parity comparison, source preparation, wrapper, static layout.

Nothing here touches the network, Gradle or Android. The Kotlin test and the workflow are checked statically
(needles for the CheckSourceService port, wrapper/class-name agreement, workflow injection hygiene).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from legado_reference import compare_parity as cp  # noqa: E402
from legado_reference import prepare_sources as ps  # noqa: E402
from legado_reference import run_legado_live_reference as rr  # noqa: E402

REFERENCE_DIR = ROOT / "legado_reference"
KOTLIN_TEST = REFERENCE_DIR / "kotlin" / "io" / "legado" / "app" / "readori" / "LegadoLiveSourceCheckTest.kt"
INIT_SCRIPT = REFERENCE_DIR / "legado-live.init.gradle"
WORKFLOW = next(
    (
        candidate
        for candidate in (parent / "docs" / "公开仓专用ci" / "readori-legado-reference.yml" for parent in ROOT.parents)
        if candidate.is_file()
    ),
    ROOT / "docs-not-in-this-checkout" / "readori-legado-reference.yml",
)


# ------------------------------------------------------------------ helpers


def legado_entry(url: str, ok: bool, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "bookSourceUrl": url,
        "bookSourceName": f"name {url}",
        "ok": ok,
        "stages": {"search": True, "explore": None, "info": True, "toc": True, "content": ok},
        "searchCount": 5,
        "exploreCount": 0,
        "tocCount": 120,
        "contentChars": 1800 if ok else 0,
        "failureStage": None if ok else "search.content",
        "failureGroup": None,
        "error": None if ok else "boom",
        "elapsedMs": 1000,
        "vacuous": False,
    }
    entry.update(extra)
    return entry


def readori_entry(url: str, ok: bool, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "bookSourceUrl": url,
        "bookSourceName": f"name {url}",
        "ok": ok,
        "appVerdict": "pass" if ok else "invalid",
        "failureReason": "" if ok else "toc empty",
        "error": "" if ok else "no chapters",
    }
    entry.update(extra)
    return entry


def synthetic_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    legado = {
        "engine": "legado-live",
        "legadoRevision": "da17bb2",
        "partial": False,
        "results": [
            legado_entry("http://a.test", True),  # both
            legado_entry("http://b.test", True),  # legado only: rule gap
            legado_entry("http://c.test", True),  # legado only: readori timeout
            legado_entry("http://d.test", True),  # legado only: readori network
            legado_entry("http://e.test", False, failureGroup="js失效", failureStage="search"),  # readori only
            legado_entry("http://f.test", False),  # neither
            legado_entry("http://g.test", True),  # legado only: missing in readori report
            legado_entry("http://h.test", True, vacuous=True),  # both, vacuous
            legado_entry("HTTP://Rel.test", True),  # relaxed match with http://rel.test/
        ],
    }
    readori = {
        "summary": {"appVersion": "1.1.1", "passed": 4},
        "results": [
            readori_entry("http://a.test ", True),  # whitespace is stripped
            readori_entry("http://b.test", False),
            readori_entry("http://c.test", False, appVerdict="timeout", failureReason="timeout", error="timed out"),
            readori_entry(
                "http://d.test",
                False,
                failureReason="network error: http 403",
                error="http 403: HTTP 403",
                networkError={"category": "http 403", "status": 403},
            ),
            readori_entry("http://e.test", True),
            readori_entry("http://f.test", False),
            readori_entry("http://h.test", True),
            readori_entry("http://rel.test/", True),
            readori_entry("http://only-readori.test", True),
        ],
    }
    return legado, readori


# ------------------------------------------------------------ compare_parity


def test_r156_tls_and_disabled_are_own_classes_excluded_from_the_headline() -> None:
    """R156: a Readori TLS-certificate failure and a disabled source (Legado ignores `enabled`) are deliberate
    differences: they keep their own rows but leave the headline numerator and denominator."""
    legado = {
        "engine": "legado-live",
        "results": [
            legado_entry("http://ok.test", True),
            legado_entry("http://gap.test", True),
            legado_entry("http://tls.test", True),
            legado_entry("http://tls-reason.test", True),
            legado_entry("http://off.test", True),
            legado_entry("http://off-vacuous.test", True, vacuous=True),
        ],
    }
    readori = {
        "summary": {"appVersion": "1.1.2"},
        "results": [
            readori_entry("http://ok.test", True),
            readori_entry("http://gap.test", False),
            readori_entry(
                "http://tls.test",
                False,
                failureReason="network error: tls",
                error="tls: certificate verify failed",
                networkError={"category": "tls", "message": "certificate verify failed", "stage": "search"},
            ),
            readori_entry("http://tls-reason.test", False, failureReason="network error: tls"),
            readori_entry("http://off.test", False, failureReason="disabled", enabled=False, appVerdict="invalid"),
            readori_entry("http://off-vacuous.test", False, failureReason="disabled", enabled=False),
        ],
    }
    parity = cp.compare(legado, readori, normalize=lambda url: (url or "").strip())
    classes = {item["bookSourceUrl"]: item["readoriFailureClass"] for item in parity["legadoOnly"]}
    assert classes == {
        "http://gap.test": "rule",
        "http://tls.test": "legadoInsecureTls",
        "http://tls-reason.test": "legadoInsecureTls",
        "http://off.test": "disabled",
        "http://off-vacuous.test": "disabled",
    }
    counts, rates = parity["counts"], parity["rates"]
    assert counts["legadoOnly"] == 5
    assert counts["legadoOnlyExcluded"] == 4
    assert counts["legadoOnlyExcludedClasses"] == {"legadoInsecureTls": 2, "disabled": 2}
    # 1 of the 4 non-vacuous Legado passes outside the excluded classes (ok, gap) -> 1/2.
    assert counts["legadoPassNonVacuous"] == 5
    assert counts["legadoPassHeadlineDenominator"] == 2
    assert rates["relativeRate"] == 0.5
    assert rates["relativeRateInclExcludedClasses"] == round(1 / 5, 6)
    # The work list orders engine gaps first, deliberate differences last.
    assert [item["readoriFailureClass"] for item in parity["legadoOnly"]][0] == "rule"
    assert {item["readoriFailureClass"] for item in parity["legadoOnly"][-4:]} == {"legadoInsecureTls", "disabled"}
    markdown = cp.render_markdown(parity)
    assert "| legadoInsecureTls | 2 |" in markdown
    assert "| disabled | 2 |" in markdown
    assert "Excluded: Readori TLS certificate failure" in markdown
    assert "Excluded: source disabled" in markdown


def test_r156_other_network_errors_stay_network_class() -> None:
    assert cp.classify_readori_failure(
        readori_entry("http://x.test", False, failureReason="network error: dns", networkError={"category": "dns"})
    ) == "network"
    assert cp.classify_readori_failure(readori_entry("http://x.test", False)) == "rule"
    assert cp.classify_readori_failure(readori_entry("http://x.test", False, failureReason="disabled")) == "disabled"




def test_compare_counts_and_rates() -> None:
    legado, readori = synthetic_documents()
    parity = cp.compare(legado, readori, normalize=lambda url: (url or "").strip())
    counts, rates = parity["counts"], parity["rates"]
    assert counts["total"] == 9
    assert counts["legadoPass"] == 7  # a b c d g h Rel
    assert counts["readoriPass"] == 4  # a e h rel (matched); the readori-only report entry is NOT counted
    assert counts["both"] == 3  # a, h, Rel (relaxed)
    assert counts["legadoOnly"] == 4  # b c d g
    assert counts["readoriOnly"] == 1  # e
    assert counts["neither"] == 1  # f
    assert counts["readoriMissing"] == 1  # g
    assert counts["readoriReportOnly"] == 1  # only-readori.test
    assert counts["relaxedUrlMatches"] == 1
    assert counts["legadoVacuousPass"] == 1
    assert counts["legadoOnlyFlaky"] == 3  # timeout, network, missing
    assert counts["readoriFailureClassesOfLegadoPass"] == {"missing": 1, "network": 1, "rule": 1, "timeout": 1}
    assert counts["legadoFailureClasses"] == {"js": 1, "rule": 1}
    assert rates["relativeRate"] == pytest.approx(2 / 6, abs=1e-6)  # headline: vacuous Legado passes excluded
    assert rates["relativeRateInclVacuous"] == pytest.approx(3 / 7, abs=1e-6)
    assert rates["relativeRateExcludingFlaky"] == pytest.approx(2 / 3, abs=1e-6)
    assert rates["relativeRateExcludingFlakyInclVacuous"] == pytest.approx(3 / 4, abs=1e-6)
    assert rates["relativeRateNonVacuous"] == rates["relativeRate"]
    assert counts["bothNonVacuous"] == 2 and counts["legadoPassNonVacuous"] == 6
    assert rates["legadoPassRate"] == pytest.approx(7 / 9, abs=1e-6)
    assert rates["readoriPassRate"] == pytest.approx(4 / 9, abs=1e-6)
    assert rates["meetsTarget"] is False


def test_compare_work_list_order_and_columns() -> None:
    legado, readori = synthetic_documents()
    parity = cp.compare(legado, readori, normalize=lambda url: (url or "").strip())
    order = [item["bookSourceUrl"] for item in parity["legadoOnly"]]
    assert order == ["http://b.test", "http://d.test", "http://c.test", "http://g.test"]  # rule, network, timeout, missing
    first = parity["legadoOnly"][0]
    assert first["readoriFailureClass"] == "rule"
    assert first["readoriFailureReason"] == "toc empty"
    assert first["legadoStages"] == "S:ok E:- I:ok T:ok C:ok"
    assert first["legadoCounts"] == {"search": 5, "explore": 0, "toc": 120, "contentChars": 1800}
    classes = {item["bookSourceUrl"]: item["readoriFailureClass"] for item in parity["legadoOnly"]}
    assert classes["http://c.test"] == "timeout"
    assert classes["http://d.test"] == "network"
    assert classes["http://g.test"] == "missing"
    only = parity["readoriOnly"][0]
    assert only["bookSourceUrl"] == "http://e.test"
    assert only["legadoFailureClass"] == "js"
    assert only["legadoFailureGroup"] == "js失效"


def test_url_normalization_uses_validator_rule_and_strips_whitespace() -> None:
    normalize = cp.load_normalizer()
    assert normalize("  http://a.test/x  ") == "http://a.test/x"
    legado = {"results": [legado_entry("  http://a.test/x\n", True)]}
    readori = {"results": [readori_entry("http://a.test/x", True)]}
    parity = cp.compare(legado, readori, normalize=normalize)
    assert parity["counts"]["both"] == 1
    assert parity["counts"]["relaxedUrlMatches"] == 0
    assert parity["rates"]["relativeRate"] == 1.0
    assert parity["rates"]["meetsTarget"] is True


def test_duplicate_urls_keep_a_passing_entry() -> None:
    legado = {"results": [legado_entry("http://dup.test", False), legado_entry("http://dup.test", True)]}
    readori = {"results": [readori_entry("http://dup.test", True)]}
    parity = cp.compare(legado, readori, normalize=lambda url: url.strip())
    assert parity["counts"]["total"] == 1
    assert parity["counts"]["both"] == 1


def test_classify_readori_failure() -> None:
    assert cp.classify_readori_failure(None) == "missing"
    assert cp.classify_readori_failure({"ok": True}) == "pass"
    assert cp.classify_readori_failure({"ok": False, "appVerdict": "timeout"}) == "timeout"
    assert cp.classify_readori_failure({"ok": False, "error": "Read timed out"}) == "timeout"
    assert cp.classify_readori_failure({"ok": False, "failureReason": "network error: http 403"}) == "network"
    assert cp.classify_readori_failure({"ok": False, "networkError": {"status": 500}}) == "network"
    assert cp.classify_readori_failure({"ok": False, "appVerdict": "needsInteraction"}) == "needsInteraction"
    assert cp.classify_readori_failure({"ok": False, "failureReason": "toc empty"}) == "rule"


def test_classify_legado_failure() -> None:
    assert cp.classify_legado_failure({"ok": True}) == "pass"
    assert cp.classify_legado_failure({"ok": False, "failureGroup": "校验超时"}) == "timeout"
    assert cp.classify_legado_failure({"ok": False, "hardTimeout": True}) == "timeout"
    assert cp.classify_legado_failure({"ok": False, "harnessSuspect": "webview", "failureGroup": "网站失效"}) == "harness"
    assert cp.classify_legado_failure({"ok": False, "failureGroup": "js失效"}) == "js"
    assert cp.classify_legado_failure({"ok": False, "failureGroup": "网站失效"}) == "site"
    assert cp.classify_legado_failure({"ok": False, "failureGroup": None}) == "rule"
    assert cp.classify_legado_failure({"ok": False, "failureStage": "import"}) == "import"


def test_markdown_is_safe_and_complete() -> None:
    legado, readori = synthetic_documents()
    legado["results"].append(
        legado_entry("http://x.test|evil", True, bookSourceName="Evil | <b>name</b>\n`code`")
    )
    readori["results"].append(
        readori_entry("http://x.test|evil", False, failureReason="a|b<script>alert(1)</script>", error="line1\nline2")
    )
    parity = cp.compare(legado, readori, normalize=lambda url: url.strip())
    markdown = cp.render_markdown(parity)
    assert "## Readori vs Legado live reference" in markdown
    assert "Relative rate" in markdown
    assert "### Legado pass, Readori fail (5)" in markdown
    assert "### Readori pass, Legado fail (1)" in markdown
    assert "<script>" not in markdown and "<b>" not in markdown
    assert "&lt;script&gt;" in markdown
    assert "\\|" in markdown
    for line in markdown.splitlines():
        assert "\r" not in line
    # every table row keeps its column count (a raw pipe would add cells)
    rows = [line for line in markdown.splitlines() if line.startswith("| ") and "Evil" in line]
    assert rows and all(row.replace("\\|", "").count("|") == 9 for row in rows)


def test_markdown_row_cap_and_partial_warning() -> None:
    legado = {
        "partial": True,
        "results": [legado_entry(f"http://s{i}.test", True) for i in range(6)],
    }
    readori = {"results": [readori_entry(f"http://s{i}.test", False) for i in range(6)]}
    parity = cp.compare(legado, readori, normalize=lambda url: url.strip())
    markdown = cp.render_markdown(parity, max_rows=2)
    assert "PARTIAL" in markdown
    assert "4 more rows in parity.json" in markdown
    assert len(parity["legadoOnly"]) == 6


def test_empty_legado_pass_set_has_no_rate() -> None:
    legado = {"results": [legado_entry("http://a.test", False)]}
    readori = {"results": [readori_entry("http://a.test", True)]}
    parity = cp.compare(legado, readori, normalize=lambda url: url.strip())
    assert parity["rates"]["relativeRate"] is None
    assert parity["rates"]["meetsTarget"] is False
    assert "n/a" in cp.render_markdown(parity)


# ----------------------------------------------- R155b: groups and vacuous passes


def prepared_source(url: str, **extra: Any) -> dict[str, Any]:
    source: dict[str, Any] = {
        "bookSourceName": f"src {url}",
        "bookSourceUrl": url,
        "searchUrl": "/s?q={{key}}",
        "ruleSearch": {"bookList": "$.list", "name": "$.n"},
    }
    source.update(extra)
    return source


def fake_index(groups: dict[str, list[str]]) -> Any:
    return cp.GroupIndex(
        {member: key for key, members in groups.items() for member in members},
        {key: list(members) for key, members in groups.items()},
    )


def test_validator_grouping_is_imported_not_reimplemented() -> None:
    grouper, reason = cp.load_grouper()
    assert grouper is not None, reason
    from validator import validate_source_packages as validator

    assert grouper is validator.group_sources
    source = (Path(cp.__file__)).read_text(encoding="utf-8")
    assert "source_rule_fingerprint" not in source and "canonical_source_site_key" not in source


def test_group_members_inherit_the_representative_verdict_with_real_validator_grouping() -> None:
    grouper, reason = cp.load_grouper()
    assert grouper is not None, reason
    sources = [
        prepared_source("http://same.test"),
        prepared_source("http://same.test#🎃"),  # export cosmetic: same group as the line above
        prepared_source("http://same.test/"),
        prepared_source("http://other.test"),
    ]
    normalize = cp.load_normalizer()
    index = cp.build_group_index(sources, normalize, grouper)
    assert len(index) == 2
    legado = {"results": [legado_entry(source["bookSourceUrl"], True) for source in sources]}
    # the validator reports ONE representative per group (a URL spelling Legado may not even have used)
    representative = next(key for key, members in index.members.items() if "http://same.test" in members)
    readori = {
        "summary": {"uniqueSourceUrls": 2},
        "results": [readori_entry(representative, True), readori_entry("http://other.test", False)],
    }
    parity = cp.compare(legado, readori, normalize=normalize, group_index=index)
    counts = parity["counts"]
    assert counts["both"] == 3 and counts["legadoOnly"] == 1
    assert counts["readoriMissing"] == 0 and counts["readoriSkipped"] == 0
    assert counts["readoriMapping"]["exact"] + counts["readoriMapping"]["group"] == 4
    assert counts["readoriReportOnly"] == 0
    assert parity["grouping"] == {"status": "grouped", "groups": 2}


def test_member_maps_to_report_entry_of_another_member_of_its_group() -> None:
    # the report names the group by a member URL that is not the group key (best candidate changed)
    index = fake_index({"http://g.test": ["http://g.test", "http://g.test#a", "http://g.test#b"]})
    legado = {"results": [legado_entry("http://g.test#a", True), legado_entry("http://g.test", True)]}
    readori = {"results": [readori_entry("http://g.test#b", False, failureReason="toc empty")]}
    parity = cp.compare(legado, readori, normalize=lambda url: url.strip(), group_index=index)
    assert parity["counts"]["readoriMissing"] == 0
    assert parity["counts"]["readoriMapping"] == {"exact": 0, "group": 2, "relaxed": 0, "skipped": 0, "missing": 0}
    assert {item["readoriFailureClass"] for item in parity["legadoOnly"]} == {"rule"}
    assert {item["readoriRepresentative"] for item in parity["legadoOnly"]} == {"http://g.test#b"}
    assert parity["counts"]["readoriReportOnly"] == 0


def test_group_prefers_a_passing_entry_when_several_members_were_reported() -> None:
    index = fake_index({"http://g.test": ["http://g.test", "http://g.test#a", "http://g.test#c"]})
    legado = {"results": [legado_entry("http://g.test#c", True)]}  # no entry of its own: inherits the group's
    readori = {"results": [readori_entry("http://g.test", False), readori_entry("http://g.test#a", True)]}
    parity = cp.compare(legado, readori, normalize=lambda url: url.strip(), group_index=index)
    assert parity["counts"]["both"] == 1


def test_group_without_report_entry_is_skipped_not_missing() -> None:
    index = fake_index({"http://a.test": ["http://a.test"], "http://skipped.test": ["http://skipped.test", "http://skipped.test#x"]})
    legado = {"results": [legado_entry(u, True) for u in ("http://a.test", "http://skipped.test", "http://skipped.test#x")]}
    readori = {"summary": {"uniqueSourceUrls": 2}, "results": [readori_entry("http://a.test", True)]}
    parity = cp.compare(legado, readori, normalize=lambda url: url.strip(), group_index=index)
    counts = parity["counts"]
    assert counts["readoriMissing"] == 0
    assert counts["readoriSkipped"] == 2
    assert counts["readoriSkippedReasons"] == {"not-validated": 2}
    assert counts["readoriFailureClassesOfLegadoPass"] == {"skipped:not-validated": 2}
    assert counts["legadoOnlyFlaky"] == 0  # a skipped group is not site flakiness
    assert {item["readoriFailureClass"] for item in parity["legadoOnly"]} == {"skipped:not-validated"}
    assert all(item["readoriMatch"] == "skipped" for item in parity["legadoOnly"])
    # the validator ran with --limit: fewer groups reported than the package has
    limited = cp.compare(
        legado,
        {"summary": {"uniqueSourceUrls": 1}, "results": [readori_entry("http://a.test", True)]},
        normalize=lambda url: url.strip(),
        group_index=index,
    )
    assert limited["counts"]["readoriSkippedReasons"] == {"validator-limit": 2}
    assert "skipped:validator-limit" in cp.render_markdown(limited)


def test_without_groups_the_old_matching_is_used_and_the_report_says_so() -> None:
    legado, readori = synthetic_documents()
    parity = cp.compare(legado, readori, normalize=lambda url: (url or "").strip())
    assert parity["grouping"]["status"].startswith("unavailable")
    assert parity["counts"]["readoriMapping"]["missing"] == 1
    assert "WARNING: validator groups were not reconstructed" in cp.render_markdown(parity)


def test_vacuous_legado_passes_are_not_in_the_headline_rate() -> None:
    legado = {
        "results": [
            legado_entry("http://real-both.test", True),
            legado_entry("http://real-gap.test", True),
            legado_entry("http://vac-both.test", True, vacuous=True),
            legado_entry("http://vac-gap-1.test", True, vacuous=True),
            legado_entry("http://vac-gap-2.test", True, vacuous=True),
            legado_entry("http://vac-fail.test", False, vacuous=True),  # a failing source is never vacuous-counted
        ]
    }
    readori = {
        "results": [
            readori_entry("http://real-both.test", True),
            readori_entry("http://real-gap.test", False),
            readori_entry("http://vac-both.test", True),
            readori_entry("http://vac-gap-1.test", False),
            readori_entry("http://vac-gap-2.test", False),
            readori_entry("http://vac-fail.test", False),
        ]
    }
    parity = cp.compare(legado, readori, normalize=lambda url: url.strip())
    counts, rates = parity["counts"], parity["rates"]
    assert counts["legadoPass"] == 5 and counts["both"] == 2
    assert counts["legadoPassNonVacuous"] == 2 and counts["bothNonVacuous"] == 1
    assert counts["legadoVacuousPass"] == 3
    assert rates["relativeRate"] == pytest.approx(1 / 2, abs=1e-6)  # headline = non-vacuous
    assert rates["relativeRateInclVacuous"] == pytest.approx(2 / 5, abs=1e-6)
    assert rates["relativeRateNonVacuous"] == rates["relativeRate"]
    markdown = cp.render_markdown(parity)
    header = next(line for line in markdown.splitlines() if line.startswith("**Relative rate"))
    assert "non-vacuous" in header and "50.00%" in header and "1 / 2" in header
    assert "| Relative rate including vacuous Legado passes | 40.00% |" in markdown
    vacuous_rows = [item for item in parity["legadoOnly"] if item["vacuous"]]
    assert len(vacuous_rows) == 2  # still listed in the work list, flagged
    only_vacuous = {"results": [legado_entry("http://v.test", True, vacuous=True)]}
    none_real = cp.compare(only_vacuous, {"results": [readori_entry("http://v.test", True)]}, normalize=lambda url: url.strip())
    assert none_real["rates"]["relativeRate"] is None and none_real["rates"]["meetsTarget"] is False
    assert none_real["rates"]["relativeRateInclVacuous"] == 1.0


def test_cli_uses_sources_next_to_the_legado_report_and_the_explicit_flag(tmp_path: Path) -> None:
    sources = [prepared_source("http://same.test"), prepared_source("http://same.test#🎃"), prepared_source("http://other.test")]
    legado = {"results": [legado_entry(s["bookSourceUrl"], True) for s in sources]}
    normalize = cp.load_normalizer()
    grouper, reason = cp.load_grouper()
    assert grouper is not None, reason
    index = cp.build_group_index(sources, normalize, grouper)
    representative = next(key for key, members in index.members.items() if "http://same.test" in members)
    readori = {
        "summary": {"uniqueSourceUrls": 2},
        "results": [readori_entry(representative, True), readori_entry("http://other.test", True)],
    }
    work = tmp_path / "work"
    work.mkdir()
    (work / "legado_report.json").write_text(json.dumps(legado, ensure_ascii=False), encoding="utf-8")
    (work / "report.json").write_text(json.dumps(readori, ensure_ascii=False), encoding="utf-8")
    (work / "sources.json").write_text(json.dumps(sources, ensure_ascii=False), encoding="utf-8")
    auto = tmp_path / "auto"
    assert cp.main(["--legado", str(work / "legado_report.json"), "--readori", str(work / "report.json"), "--output-dir", str(auto)]) == 0
    parity = json.loads((auto / "parity.json").read_text(encoding="utf-8"))
    assert parity["grouping"]["status"] == "grouped"
    assert parity["counts"]["both"] == 3 and parity["counts"]["readoriMissing"] == 0
    # explicit --sources wins; a package without a sources.json next to it falls back with a warning
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for name in ("legado_report.json", "report.json"):
        (elsewhere / name).write_text((work / name).read_text(encoding="utf-8"), encoding="utf-8")
    plain = tmp_path / "plain"
    assert cp.main(["--legado", str(elsewhere / "legado_report.json"), "--readori", str(elsewhere / "report.json"), "--output-dir", str(plain)]) == 0
    assert json.loads((plain / "parity.json").read_text(encoding="utf-8"))["grouping"]["status"].startswith("unavailable")
    explicit = tmp_path / "explicit"
    code = cp.main(
        [
            "--legado", str(elsewhere / "legado_report.json"),
            "--readori", str(elsewhere / "report.json"),
            "--sources", str(work / "sources.json"),
            "--output-dir", str(explicit),
        ]
    )
    assert code == 0
    assert json.loads((explicit / "parity.json").read_text(encoding="utf-8"))["counts"]["both"] == 3


def test_cli_writes_json_and_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    legado, readori = synthetic_documents()
    legado_path = tmp_path / "legado_report.json"
    readori_path = tmp_path / "report.json"
    legado_path.write_text(json.dumps(legado, ensure_ascii=False), encoding="utf-8")
    readori_path.write_text(json.dumps(readori, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    code = cp.main(["--legado", str(legado_path), "--readori", str(readori_path), "--output-dir", str(out)])
    assert code == 0
    parity = json.loads((out / "parity.json").read_text(encoding="utf-8"))
    assert parity["schemaVersion"] == 1
    assert parity["counts"]["both"] == 3
    assert (out / "parity.md").read_text(encoding="utf-8").startswith("## Readori vs Legado live reference")
    assert "relativeRate=33.33%" in capsys.readouterr().out
    assert cp.main(["--legado", str(legado_path), "--readori", str(readori_path), "--output-dir", str(out), "--fail-under"]) == 1


def test_cli_rejects_bad_input(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"results": []}), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cp.main(["--legado", str(bad), "--readori", str(good), "--output-dir", str(tmp_path)]) == 2
    assert cp.main(["--legado", str(tmp_path / "missing.json"), "--readori", str(good), "--output-dir", str(tmp_path)]) == 2
    no_results = tmp_path / "noresults.json"
    no_results.write_text(json.dumps({"summary": {}}), encoding="utf-8")
    assert cp.main(["--legado", str(no_results), "--readori", str(good), "--output-dir", str(tmp_path)]) == 2


# ----------------------------------------------------------- prepare_sources


def test_prepare_sources_shapes_dedupe_offset_limit(tmp_path: Path) -> None:
    sources = [{"bookSourceUrl": f"http://s{i}.test", "bookSourceName": str(i)} for i in range(5)]
    sources.insert(2, {"bookSourceUrl": " http://s0.test ", "bookSourceName": "dup"})
    sources.append({"bookSourceName": "no url"})
    sources.append("not an object")  # type: ignore[arg-type]
    wrapper = tmp_path / "wrapper.json"
    wrapper.write_text("﻿" + json.dumps({"bookSources": sources}), encoding="utf-8")
    loaded = ps.load_sources([wrapper])
    assert len(loaded) == 7  # the non-object entry is dropped
    prepared = ps.prepare(loaded)
    assert [item["bookSourceName"] for item in prepared] == ["0", "1", "2", "3", "4"]
    assert [item["bookSourceName"] for item in ps.prepare(loaded, offset=1, limit=2)] == ["1", "2"]
    assert len(ps.prepare(loaded, dedupe=False)) == 6
    single = tmp_path / "single.json"
    single.write_text(json.dumps(sources[0]), encoding="utf-8")
    assert ps.extract_sources(json.loads(single.read_text(encoding="utf-8"))) == [sources[0]]


def test_prepare_sources_cli(tmp_path: Path) -> None:
    source = tmp_path / "in.json"
    source.write_text(
        json.dumps([{"bookSourceUrl": "http://a.test"}, {"bookSourceUrl": "http://b.test"}]), encoding="utf-8"
    )
    output = tmp_path / "nested" / "out.json"
    assert ps.main(["--input", str(source), "--output", str(output), "--limit", "1"]) == 0
    written = output.read_bytes()
    assert b"\r\n" not in written and not written.startswith(b"\xef\xbb\xbf")
    assert json.loads(written) == [{"bookSourceUrl": "http://a.test"}]
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    assert ps.main(["--input", str(empty), "--output", str(tmp_path / "x.json")]) == 3
    assert ps.main(["--input", str(tmp_path / "missing.json"), "--output", str(tmp_path / "x.json")]) == 2


def test_smoke_sample_is_a_small_real_package() -> None:
    smoke = json.loads((REFERENCE_DIR / "samples" / "smoke.json").read_text(encoding="utf-8"))
    assert 5 <= len(smoke) <= 10
    urls = [item["bookSourceUrl"] for item in smoke]
    assert len(set(urls)) == len(urls)
    assert all(url.startswith(("http://", "https://")) for url in urls)
    assert all(item.get("searchUrl") for item in smoke)
    assert len(ps.prepare(ps.extract_sources(smoke))) == len(smoke)
    raw = (REFERENCE_DIR / "samples" / "smoke.json").read_bytes()
    assert b"\r\n" not in raw and not raw.startswith(b"\xef\xbb\xbf")


# ------------------------------------------------------------------- wrapper


def make_legado_root(tmp_path: Path) -> Path:
    root = tmp_path / "legado"
    root.mkdir()
    (root / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "gradlew.bat").write_text("@echo off\n", encoding="utf-8")
    return root


def command_kwargs(tmp_path: Path, **override: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "legado_root": tmp_path / "legado",
        "sources": tmp_path / "sources.json",
        "output": tmp_path / "legado_report.json",
        "concurrency": 8,
        "timeout": 180,
        "limit": 0,
        "offset": 0,
        "build_root": None,
    }
    kwargs.update(override)
    return kwargs


def test_gradle_task_names() -> None:
    assert rr.gradle_task("AppRelease") == ":app:testAppReleaseUnitTest"
    assert rr.gradle_task("AppDebug") == ":app:testAppDebugUnitTest"
    for bad in ("appRelease", "App Release", "App;rm", ""):
        with pytest.raises(rr.WrapperError):
            rr.gradle_task(bad)


def test_build_command_linux_and_windows(tmp_path: Path) -> None:
    linux = rr.build_command(**command_kwargs(tmp_path, platform="linux", legado_revision="da17bb2", flush_every=10))
    assert linux[:2] == ["sh", str(tmp_path / "legado" / "gradlew")]
    assert "--no-daemon" in linux and "--console=plain" in linux
    assert linux[linux.index("-I") + 1] == str(rr.INIT_SCRIPT)
    assert ":app:testAppReleaseUnitTest" in linux
    assert linux[linux.index("--tests") + 1] == "io.legado.app.readori.LegadoLiveSourceCheckTest"
    assert f"-PreadoriLiveAdapter={rr.ADAPTER_SOURCE}" in linux
    assert f"-PreadoriLiveSources={tmp_path / 'sources.json'}" in linux
    assert f"-PreadoriLiveOutput={tmp_path / 'legado_report.json'}" in linux
    assert "-PreadoriLiveConcurrency=8" in linux
    assert "-PreadoriLiveTimeoutSeconds=180" in linux
    assert "-PreadoriLiveLimit=0" in linux and "-PreadoriLiveOffset=0" in linux
    assert "-PreadoriLiveFlushEvery=10" in linux
    assert "-PreadoriLiveLegadoRevision=da17bb2" in linux
    assert f"-Dorg.gradle.jvmargs={rr.DEFAULT_GRADLE_JVMARGS}" in linux
    assert not any(arg.startswith("-PreadoriLiveBuildRoot") for arg in linux)
    assert "-PreadoriLiveIncludeResources=false" not in linux

    windows = rr.build_command(
        **command_kwargs(
            tmp_path,
            platform="win32",
            timeout=None,
            build_root=tmp_path / "build",
            include_resources=False,
            variant="AppDebug",
        )
    )
    assert windows[0] == str(tmp_path / "legado" / "gradlew.bat")
    assert ":app:testAppDebugUnitTest" in windows
    assert not any(arg.startswith("-PreadoriLiveTimeoutSeconds") for arg in windows)
    assert f"-PreadoriLiveBuildRoot={tmp_path / 'build'}" in windows
    assert "-PreadoriLiveIncludeResources=false" in windows


def test_local_properties_plan(tmp_path: Path) -> None:
    root = make_legado_root(tmp_path)
    assert rr.plan_local_properties(root, {}) == ("missing-sdk", None)
    assert rr.plan_local_properties(root, {"ANDROID_SDK_ROOT": "/sdk-root"}) == ("write", "/sdk-root")
    assert rr.plan_local_properties(root, {"ANDROID_HOME": "/sdk-home", "ANDROID_SDK_ROOT": "/sdk-root"}) == (
        "write",
        "/sdk-home",
    )
    assert rr.plan_local_properties(root, {"ANDROID_HOME": "  "}) == ("missing-sdk", None)
    secret = "RELEASE_STORE_PASSWORD=do-not-touch\n"
    (root / "local.properties").write_text(secret, encoding="utf-8")
    assert rr.plan_local_properties(root, {"ANDROID_HOME": "/sdk-home"}) == ("keep", None)
    assert (root / "local.properties").read_text(encoding="utf-8") == secret


def test_write_local_properties_uses_forward_slashes(tmp_path: Path) -> None:
    root = make_legado_root(tmp_path)
    sdk = tmp_path / "android sdk"
    target = rr.write_local_properties(root, str(sdk))
    assert target.read_bytes() == f"sdk.dir={sdk.as_posix()}\n".encode()


def test_dry_run_prints_command_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_legado_root(tmp_path)
    output = tmp_path / "out" / "legado_report.json"
    code = rr.main(
        [
            "--legado-root",
            str(root),
            "--sources",
            str(tmp_path / "sources.json"),
            "--output",
            str(output),
            "--concurrency",
            "4",
            "--dry-run",
        ],
        env={"ANDROID_HOME": str(tmp_path / "sdk")},
    )
    assert code == 0
    text = capsys.readouterr().out
    assert "local.properties: write" in text
    assert ":app:testAppReleaseUnitTest" in text
    assert "-PreadoriLiveConcurrency=4" in text
    assert "--tests io.legado.app.readori.LegadoLiveSourceCheckTest" in text
    assert not (root / "local.properties").exists()
    assert not output.parent.exists()


def test_main_reports_environment_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_legado_root(tmp_path)
    sources = tmp_path / "sources.json"
    sources.write_text("[]", encoding="utf-8")
    base = ["--legado-root", str(root), "--sources", str(sources), "--output", str(tmp_path / "r.json")]
    assert rr.main(base, env={}) == 2  # no SDK and no local.properties
    assert "no Android SDK" in capsys.readouterr().err
    assert rr.main(base + ["--variant", "bad variant", "--dry-run"], env={"ANDROID_HOME": "x"}) == 2
    assert rr.main(["--legado-root", str(root), "--sources", str(tmp_path / "nope.json"), "--output", "r.json"], env={"ANDROID_HOME": "x"}) == 2
    assert rr.main(base + ["--concurrency", "0"], env={"ANDROID_HOME": "x"}) == 2


def fake_report(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "engine": "legado-live",
                "partial": False,
                "summary": {"total": 1, "completed": 1, "passed": 1, "failed": 0},
                "results": [legado_entry("http://a.test", True)],
            }
        ),
        encoding="utf-8",
    )


def run_main_with_fake_gradle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, returncode: int, write_report: bool, env: dict[str, str]
) -> tuple[int, list[list[str]], Path]:
    root = make_legado_root(tmp_path)
    sources = tmp_path / "sources.json"
    sources.write_text("[]", encoding="utf-8")
    output = tmp_path / "reports" / "legado_report.json"
    output.parent.mkdir()
    output.write_text("stale", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path | None = None, **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        assert cwd == root.resolve()
        assert not output.exists()  # a stale report must be removed before the run
        if write_report:
            fake_report(output)
        return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(rr.subprocess, "run", fake_run)
    monkeypatch.setattr(rr, "git_revision", lambda _root: "abc123")
    code = rr.main(
        ["--legado-root", str(root), "--sources", str(sources), "--output", str(output)],
        env=env,
    )
    return code, calls, root


def test_main_runs_gradle_and_validates_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = tmp_path / "sdk"
    code, calls, root = run_main_with_fake_gradle(
        tmp_path, monkeypatch, returncode=0, write_report=True, env={"ANDROID_HOME": str(sdk)}
    )
    assert code == 0
    assert len(calls) == 1 and "-PreadoriLiveLegadoRevision=abc123" in calls[0]
    assert (root / "local.properties").read_text(encoding="utf-8") == f"sdk.dir={sdk.as_posix()}\n"


def test_main_exit_codes_for_gradle_and_report_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"ANDROID_HOME": str(tmp_path / "sdk")}
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    code, _calls, _root = run_main_with_fake_gradle(tmp_path / "a", monkeypatch, returncode=0, write_report=False, env=env)
    assert code == 3  # Gradle "succeeded" but produced no report
    code, _calls, _root = run_main_with_fake_gradle(tmp_path / "b", monkeypatch, returncode=1, write_report=True, env=env)
    assert code == 1  # Gradle failed; the partial report is kept for the comparison
    code, _calls, _root = run_main_with_fake_gradle(tmp_path / "c", monkeypatch, returncode=1, write_report=False, env=env)
    assert code == 1


def test_main_keeps_existing_local_properties(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_legado_root(tmp_path)
    secret = "sdk.dir=/somewhere\nRELEASE_STORE_PASSWORD=do-not-touch\n"
    (root / "local.properties").write_text(secret, encoding="utf-8")
    sources = tmp_path / "sources.json"
    sources.write_text("[]", encoding="utf-8")
    output = tmp_path / "legado_report.json"

    def fake_run(command: list[str], cwd: Path | None = None, **_: Any) -> subprocess.CompletedProcess[str]:
        fake_report(output)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(rr.subprocess, "run", fake_run)
    monkeypatch.setattr(rr, "git_revision", lambda _root: "")
    code = rr.main(["--legado-root", str(root), "--sources", str(sources), "--output", str(output)], env={})
    assert code == 0  # no ANDROID_HOME needed when local.properties already exists
    assert (root / "local.properties").read_text(encoding="utf-8") == secret


def test_validate_output_rejects_foreign_reports(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    with pytest.raises(ValueError):
        rr.validate_output(path)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        rr.validate_output(path)
    path.write_text(json.dumps({"engine": "other", "results": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        rr.validate_output(path)
    fake_report(path)
    assert rr.validate_output(path)["engine"] == "legado-live"


# ----------------------------------------------------- Kotlin / Gradle layout


def test_kotlin_test_mirrors_check_source_service_flow() -> None:
    text = KOTLIN_TEST.read_text(encoding="utf-8")
    assert text.startswith("package io.legado.app.readori\n")
    assert "class LegadoLiveSourceCheckTest" in text
    assert rr.TEST_CLASS == "io.legado.app.readori.LegadoLiveSourceCheckTest"
    assert "@RunWith(RobolectricTestRunner::class)" in text
    assert "@Config(sdk = [34]" in text
    for needle in (
        "source.getCheckKeyword(CheckSource.keyword)",
        "WebBook.searchBookAwait(source, searchWord)",
        "source.exploreKinds().firstOrNull",
        "WebBook.exploreBookAwait(source, url)",
        "WebBook.getBookInfoAwait(source, book)",
        "WebBook.getChapterListAwait(source, book).getOrThrow()",
        ".filter { !(it.isVolume && it.url.startsWith(it.title)) }",
        ".take(2)",
        "toc.getOrNull(1)?.url ?: toc.first().url",
        "WebBook.getContentAwait(",
        "needSave = false",
        "source.getInvalidGroupNames()",
        "throw NoStackTraceException(finalCheckMessage)",
        "withTimeout(timeoutMs)",
        "is TimeoutCancellationException",
        "is ScriptException, is WrappedException",
        "!is NoStackTraceException",
        "is ContentEmptyException",
        "is TocEmptyException",
        "CheckSourceService.kt:134",
        "CheckSourceService.kt:154",
        "CheckSourceService.kt:198",
        "GSON.fromJsonObject<BookSource>",
        "Semaphore(concurrency)",
        "withTimeoutOrNull(timeoutMs + hardGraceMs)",
        "StandardCopyOption.ATOMIC_MOVE",
    ):
        assert needle in text, needle
    for prop in (
        "readori.live.sources",
        "readori.live.output",
        "readori.live.concurrency",
        "readori.live.timeoutSeconds",
        "readori.live.limit",
        "readori.live.offset",
    ):
        assert prop in text and prop in text.split("class LegadoLiveSourceCheckTest")[0], prop
    for field in (
        "bookSourceUrl", "bookSourceName", "ok", "stages", "searchCount", "exploreCount", "tocCount",
        "contentChars", "failureStage", "error", "elapsedMs", "keyword",
    ):
        assert f'"{field}"' in text, field
    # path matches the package so Gradle finds it under the adapter source root
    assert KOTLIN_TEST.relative_to(REFERENCE_DIR / "kotlin").as_posix() == "io/legado/app/readori/LegadoLiveSourceCheckTest.kt"
    assert b"\r\n" not in KOTLIN_TEST.read_bytes()


def test_init_script_attaches_adapter_without_touching_the_checkout() -> None:
    text = INIT_SCRIPT.read_text(encoding="utf-8")
    for needle in (
        "org.robolectric:robolectric:4.14.1",
        "project.android.sourceSets.test.java.srcDir(new File(adapterSource))",
        "maxHeapSize",
        "upToDateWhen { false }",
        "showStandardStreams = true",
        "includeAndroidResources",
        "readori.live.sources",
        "readori.live.output",
        "readori.live.concurrency",
        "readori.live.timeoutSeconds",
        "readori.live.limit",
        "readori.live.offset",
        "readoriLiveBuildRoot",
    ):
        assert needle in text, needle
    # the init script only configures the :app unit-test tasks; it never writes files
    assert "writeText" not in text and "Files.write" not in text and "delete" not in text.lower()
    assert b"\r\n" not in INIT_SCRIPT.read_bytes()
    assert rr.INIT_SCRIPT == INIT_SCRIPT and rr.ADAPTER_SOURCE == REFERENCE_DIR / "kotlin"


# ------------------------------------------------------------------ workflow


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="workflow lives in the main repository docs, not in the mirror")
def test_workflow_static_contract_and_injection_hygiene() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.startswith("name: Readori Legado Reference\n")
    assert "\r" not in text
    for needle in (
        "workflow_dispatch:",
        "sources_url:",
        "sources_path:",
        "legado_ref:",
        "concurrency:",
        "timeout_seconds:",
        "limit:",
        "app_version:",
        'default: "legado_reference/samples/smoke.json"',
        'default: "master"',
        'default: "1.1.1"',
        "runs-on: ubuntu-latest",
        "timeout-minutes: 300",
        "contents: read",
        "repository: readori/legado",
        "repository: readori/readori-CheckSources",
        "actions/setup-java@v4",
        "distribution: temurin",
        'java-version: "17"',
        "actions/setup-python@v5",
        'python-version: "3.12"',
        "actions/cache@v4",
        "actions/upload-artifact@v4",
        "checksources/requirements-validate-sources.txt",
        "checksources/legado_reference/run_legado_live_reference.py",
        "checksources/legado_reference/compare_parity.py",
        "checksources/legado_reference/prepare_sources.py",
        "checksources/validator/validate_source_packages.py",
        "--pipeline staged",
        "--no-mirror",
        "--app-version",
        "$GITHUB_STEP_SUMMARY",
        "--max-filesize",
        "--proto '=https'",
    ):
        assert needle in text, needle
    assert "secrets." not in text
    # every ${{ inputs.* }} reaches the shell only through an env: entry (or a with: ref)
    for number, line in enumerate(text.splitlines(), 1):
        if "${{ inputs." in line:
            stripped = line.strip()
            assert stripped.startswith(("IN_", "ref: ")), (number, line)
    assert "run: ${{" not in text
    assert text.count("${{ github.run_id }}") >= 1


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="workflow lives in the main repository docs, not in the mirror")
def test_workflow_parses_as_yaml_without_inline_expressions_in_run_blocks() -> None:
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert document["name"] == "Readori Legado Reference"
    trigger = document.get("on", document.get(True))
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"sources_url", "sources_path", "legado_ref", "concurrency", "timeout_seconds", "limit", "app_version"}
    assert inputs["app_version"]["default"] == "1.1.1"
    job = document["jobs"]["reference"]
    assert job["runs-on"] == "ubuntu-latest" and job["timeout-minutes"] == 300
    assert document["permissions"] == {"contents": "read"}
    for step in job["steps"]:
        if "run" in step:
            assert "${{" not in step["run"], step.get("name")


def test_no_real_secret_material_in_reference_dir() -> None:
    forbidden = ("RELEASE_STORE_PASSWORD=", "RELEASE_KEY_PASSWORD=", "AuthKey_", "BEGIN PRIVATE KEY")
    for path in REFERENCE_DIR.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".kt", ".gradle", ".json"} and "__pycache__" not in path.parts:
            content = path.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in content, (path, token)
