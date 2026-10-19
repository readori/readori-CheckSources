#!/usr/bin/env python3
"""Compare the live Legado reference report with Readori's validator report.

Inputs
  --legado  report written by ``LegadoLiveSourceCheckTest`` (``engine: legado-live``, ``results[]``)
  --readori report written by ``validator/validate_source_packages.py`` (``results[]`` with ``ok`` /
            ``appVerdict`` / ``failureReason``)

Both engines must have been given the same source package. Sources are matched by the normalized
``bookSourceUrl`` (``normalize_book_source_url`` from the validator when it can be imported).

Headline number (target >= 95 %)::

    relativeRate = |Legado pass AND Readori pass| / |Legado pass|

The report also lists the *work list*: sources Legado passes and Readori does not (``legadoOnly``) and the
reverse (``readoriOnly``). Readori timeouts / network errors / missing entries are classified separately,
because they are usually site flakiness rather than an engine gap.

Output: ``parity.json`` and ``parity.md`` (the Markdown is safe to append to ``$GITHUB_STEP_SUMMARY``;
every untrusted string is escaped and truncated).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_TARGET = 0.95
DEFAULT_MAX_ROWS = 300

# Readori failure classes that are not rule/engine gaps.
FLAKY_READORI_CLASSES = ("timeout", "network", "missing")

_TIMEOUT_PATTERN = re.compile(r"time(?:d)?\s*-?out|超时", re.IGNORECASE)


def _fallback_normalize(url: str) -> str:
    return (url or "").strip()


def load_normalizer():
    """Return the validator's ``normalize_book_source_url`` or a ``strip()`` fallback.

    The validator is a large module; importing it is optional and any failure (missing optional
    dependencies, different working directory) silently falls back to the identical ``strip()`` rule.
    """
    root = Path(__file__).resolve().parents[1]
    added = False
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
        added = True
    try:
        from validator.validate_source_packages import normalize_book_source_url  # type: ignore

        return normalize_book_source_url
    except Exception:  # pragma: no cover - depends on the environment
        return _fallback_normalize
    finally:
        if added:
            try:
                sys.path.remove(str(root))
            except ValueError:
                pass


def relaxed_key(normalized_url: str) -> str:
    return normalized_url.strip().lower().rstrip("/")


def load_report(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        return {"results": data}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: report must be a JSON object")
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError(f"{path}: report has no results[] list")
    return data


def _index_results(results: list[Any], normalize) -> dict[str, dict[str, Any]]:
    """Index by normalized URL. A duplicate URL keeps a passing entry in preference to a failing one."""
    index: dict[str, dict[str, Any]] = {}
    for entry in results:
        if not isinstance(entry, dict):
            continue
        url = normalize(str(entry.get("bookSourceUrl") or ""))
        if not url:
            continue
        previous = index.get(url)
        if previous is None or (not previous.get("ok") and entry.get("ok")):
            index[url] = entry
    return index


# ------------------------------------------------------------ classification


def classify_readori_failure(entry: dict[str, Any] | None) -> str:
    """``pass`` | ``timeout`` | ``network`` | ``needsInteraction`` | ``rule`` | ``missing``."""
    if entry is None:
        return "missing"
    if entry.get("ok"):
        return "pass"
    verdict = str(entry.get("appVerdict") or "").lower()
    reason = str(entry.get("failureReason") or "")
    error = str(entry.get("error") or "")
    if verdict == "timeout" or _TIMEOUT_PATTERN.search(reason) or _TIMEOUT_PATTERN.search(error):
        return "timeout"
    if verdict in ("needsinteraction", "needs_interaction"):
        return "needsInteraction"
    network = entry.get("networkError")
    if reason.lower().startswith("network error") or (isinstance(network, dict) and network):
        return "network"
    return "rule"


def classify_legado_failure(entry: dict[str, Any] | None) -> str:
    """``pass`` | ``timeout`` | ``harness`` | ``js`` | ``site`` | ``rule`` | ``import`` | ``missing``."""
    if entry is None:
        return "missing"
    if entry.get("ok"):
        return "pass"
    if entry.get("failureStage") == "import":
        return "import"
    group = entry.get("failureGroup")
    if entry.get("hardTimeout") or group == "校验超时":
        return "timeout"
    if entry.get("harnessSuspect"):
        return "harness"
    if group == "js失效":
        return "js"
    if group == "网站失效":
        return "site"
    return "rule"


def _stage_text(value: Any) -> str:
    if value is True:
        return "ok"
    if value is False:
        return "x"
    return "-"


def legado_stage_summary(entry: dict[str, Any]) -> str:
    stages = entry.get("stages") if isinstance(entry.get("stages"), dict) else {}
    return " ".join(
        f"{label}:{_stage_text(stages.get(key))}"
        for label, key in (("S", "search"), ("E", "explore"), ("I", "info"), ("T", "toc"), ("C", "content"))
    )


def legado_counts(entry: dict[str, Any]) -> dict[str, int]:
    def number(key: str) -> int:
        value = entry.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    return {
        "search": number("searchCount"),
        "explore": number("exploreCount"),
        "toc": number("tocCount"),
        "contentChars": number("contentChars"),
    }


def _truncate(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


# --------------------------------------------------------------- comparison


def compare(
    legado_doc: dict[str, Any],
    readori_doc: dict[str, Any],
    *,
    normalize=None,
    target: float = DEFAULT_TARGET,
) -> dict[str, Any]:
    normalize = normalize or load_normalizer()
    legado = _index_results(legado_doc["results"], normalize)
    readori = _index_results(readori_doc["results"], normalize)
    readori_by_relaxed: dict[str, list[str]] = {}
    for key in readori:
        readori_by_relaxed.setdefault(relaxed_key(key), []).append(key)

    matched_readori: set[str] = set()
    relaxed_matches = 0
    legado_only: list[dict[str, Any]] = []
    readori_only: list[dict[str, Any]] = []
    both = 0
    neither = 0
    both_non_vacuous = 0
    legado_pass_non_vacuous = 0
    legado_vacuous_pass = 0
    legado_failure_classes: dict[str, int] = {}
    readori_failure_classes_of_legado_pass: dict[str, int] = {}
    readori_missing = 0

    for key, legado_entry in legado.items():
        readori_key = key if key in readori else None
        if readori_key is None:
            candidates = [k for k in readori_by_relaxed.get(relaxed_key(key), []) if k not in matched_readori]
            if len(candidates) == 1:
                readori_key = candidates[0]
                relaxed_matches += 1
        readori_entry = readori.get(readori_key) if readori_key is not None else None
        if readori_key is not None:
            matched_readori.add(readori_key)
        else:
            readori_missing += 1

        legado_pass = bool(legado_entry.get("ok"))
        readori_pass = bool(readori_entry and readori_entry.get("ok"))
        vacuous = bool(legado_entry.get("vacuous")) and legado_pass
        if legado_pass:
            legado_pass_non_vacuous += 0 if vacuous else 1
            legado_vacuous_pass += 1 if vacuous else 0
        else:
            failure_class = classify_legado_failure(legado_entry)
            legado_failure_classes[failure_class] = legado_failure_classes.get(failure_class, 0) + 1

        if legado_pass and readori_pass:
            both += 1
            if not vacuous:
                both_non_vacuous += 1
        elif legado_pass and not readori_pass:
            readori_class = classify_readori_failure(readori_entry)
            readori_failure_classes_of_legado_pass[readori_class] = (
                readori_failure_classes_of_legado_pass.get(readori_class, 0) + 1
            )
            legado_only.append(
                {
                    "bookSourceUrl": key,
                    "bookSourceName": str(legado_entry.get("bookSourceName") or (readori_entry or {}).get("bookSourceName") or ""),
                    "readoriFailureClass": readori_class,
                    "readoriFailureReason": str((readori_entry or {}).get("failureReason") or ""),
                    "readoriError": _truncate((readori_entry or {}).get("error"), 300),
                    "readoriAppVerdict": str((readori_entry or {}).get("appVerdict") or ""),
                    "legadoStages": legado_stage_summary(legado_entry),
                    "legadoCounts": legado_counts(legado_entry),
                    "legadoElapsedMs": legado_entry.get("elapsedMs"),
                    "vacuous": vacuous,
                    "relaxedMatch": readori_key is not None and readori_key != key,
                }
            )
        elif readori_pass and not legado_pass:
            readori_only.append(
                {
                    "bookSourceUrl": key,
                    "bookSourceName": str(legado_entry.get("bookSourceName") or (readori_entry or {}).get("bookSourceName") or ""),
                    "legadoFailureClass": classify_legado_failure(legado_entry),
                    "legadoFailureStage": legado_entry.get("failureStage"),
                    "legadoFailureGroup": legado_entry.get("failureGroup"),
                    "legadoError": _truncate(legado_entry.get("error"), 300),
                    "legadoHarnessSuspect": legado_entry.get("harnessSuspect"),
                    "legadoStages": legado_stage_summary(legado_entry),
                }
            )
        else:
            neither += 1

    total = len(legado)
    legado_pass_count = both + len(legado_only)
    readori_pass_count = both + len(readori_only)
    flaky_legado_only = sum(1 for item in legado_only if item["readoriFailureClass"] in FLAKY_READORI_CLASSES)

    def ratio(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 6) if denominator else None

    relative = ratio(both, legado_pass_count)
    work_order = {"rule": 0, "needsInteraction": 1, "network": 2, "timeout": 3, "missing": 4}
    legado_only.sort(key=lambda item: (work_order.get(item["readoriFailureClass"], 9), item["bookSourceUrl"]))
    readori_only.sort(key=lambda item: (item["legadoFailureClass"], item["bookSourceUrl"]))

    rates = {
        "relativeRate": relative,
        "relativeRateExcludingFlaky": ratio(both, legado_pass_count - flaky_legado_only),
        "relativeRateNonVacuous": ratio(both_non_vacuous, legado_pass_non_vacuous),
        "legadoPassRate": ratio(legado_pass_count, total),
        "readoriPassRate": ratio(readori_pass_count, total),
        "target": target,
        "meetsTarget": (relative is not None and relative >= target),
    }
    counts = {
        "total": total,
        "legadoPass": legado_pass_count,
        "readoriPass": readori_pass_count,
        "both": both,
        "legadoOnly": len(legado_only),
        "readoriOnly": len(readori_only),
        "neither": neither,
        "legadoOnlyFlaky": flaky_legado_only,
        "legadoVacuousPass": legado_vacuous_pass,
        "legadoPassNonVacuous": legado_pass_non_vacuous,
        "readoriMissing": readori_missing,
        "readoriReportOnly": len(set(readori) - matched_readori),
        "relaxedUrlMatches": relaxed_matches,
        "legadoFailureClasses": dict(sorted(legado_failure_classes.items())),
        "readoriFailureClassesOfLegadoPass": dict(sorted(readori_failure_classes_of_legado_pass.items())),
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "rates": rates,
        "legadoOnly": legado_only,
        "readoriOnly": readori_only,
        "inputs": {
            "legado": _legado_meta(legado_doc),
            "readori": _readori_meta(readori_doc),
        },
    }


def _legado_meta(doc: dict[str, Any]) -> dict[str, Any]:
    summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else {}
    return {
        "engine": doc.get("engine"),
        "legadoRevision": doc.get("legadoRevision"),
        "partial": bool(doc.get("partial")),
        "generatedAt": doc.get("generatedAt"),
        "elapsedMs": doc.get("elapsedMs"),
        "config": doc.get("config") if isinstance(doc.get("config"), dict) else {},
        "summary": summary,
    }


def _readori_meta(doc: dict[str, Any]) -> dict[str, Any]:
    summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else {}
    return {
        "appVersion": summary.get("appVersion"),
        "generatedAt": summary.get("generatedAt"),
        "uniqueSourceUrls": summary.get("uniqueSourceUrls"),
        "passed": summary.get("passed"),
        "failed": summary.get("failed"),
        "deduplicatedAfterValidation": summary.get("deduplicatedAfterValidation"),
    }


# ------------------------------------------------------------------ markdown


def md_cell(text: Any, limit: int = 120) -> str:
    """Escape untrusted text for a Markdown table cell (no HTML, no pipes, no backticks, one line)."""
    value = _truncate(text, limit)
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace("|", "\\|").replace("`", "'")
    return value


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def render_markdown(parity: dict[str, Any], max_rows: int = DEFAULT_MAX_ROWS) -> str:
    counts = parity["counts"]
    rates = parity["rates"]
    inputs = parity["inputs"]
    legado_meta = inputs["legado"]
    verdict = "meets" if rates["meetsTarget"] else "BELOW"
    lines: list[str] = []
    lines.append("## Readori vs Legado live reference")
    lines.append("")
    if legado_meta.get("partial"):
        lines.append("> WARNING: the Legado report is PARTIAL (the run did not finish); numbers cover finished sources only.")
        lines.append("")
    lines.append(
        f"**Relative rate: {_percent(rates['relativeRate'])}** "
        f"(target {_percent(rates['target'])}, {verdict} target) = both pass / Legado pass = "
        f"{counts['both']} / {counts['legadoPass']}"
    )
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | ---: |")
    rows = [
        ("Sources compared (Legado report)", counts["total"]),
        ("Legado pass", counts["legadoPass"]),
        ("Readori pass", counts["readoriPass"]),
        ("Both pass", counts["both"]),
        ("Legado only (work list)", counts["legadoOnly"]),
        ("Readori only", counts["readoriOnly"]),
        ("Neither", counts["neither"]),
        ("Legado pass rate (absolute)", _percent(rates["legadoPassRate"])),
        ("Readori pass rate (absolute)", _percent(rates["readoriPassRate"])),
        ("Relative rate", _percent(rates["relativeRate"])),
        ("Relative rate excluding Readori timeout / network / missing", _percent(rates["relativeRateExcludingFlaky"])),
        ("Relative rate excluding vacuous Legado passes", _percent(rates["relativeRateNonVacuous"])),
        ("Legado vacuous passes (no search / explore ran)", counts["legadoVacuousPass"]),
        ("Missing from Readori report", counts["readoriMissing"]),
    ]
    for label, value in rows:
        lines.append(f"| {md_cell(label, 80)} | {value} |")
    lines.append("")
    revision = md_cell(legado_meta.get("legadoRevision") or "unknown", 40)
    app_version = md_cell(inputs["readori"].get("appVersion") or "unknown", 20)
    lines.append(f"Legado revision `{revision}`, Readori App profile `{app_version}`.")
    lines.append("")

    classes = counts.get("readoriFailureClassesOfLegadoPass") or {}
    if classes:
        lines.append("### Why Readori failed where Legado passed")
        lines.append("")
        lines.append("| Readori class | Sources |")
        lines.append("| --- | ---: |")
        for name, number in classes.items():
            lines.append(f"| {md_cell(name, 40)} | {number} |")
        lines.append("")
        lines.append(
            "`rule` and `needsInteraction` are engine gaps; `timeout`, `network` and `missing` are usually site flakiness."
        )
        lines.append("")

    lines.append(f"### Legado pass, Readori fail ({counts['legadoOnly']})")
    lines.append("")
    legado_only = parity["legadoOnly"]
    if legado_only:
        lines.append(
            "| Source | URL | Readori class | Readori reason | Readori error | Legado stages | Legado counts | Vacuous |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for item in legado_only[:max_rows]:
            counts_text = (
                f"search={item['legadoCounts']['search']} explore={item['legadoCounts']['explore']} "
                f"toc={item['legadoCounts']['toc']} chars={item['legadoCounts']['contentChars']}"
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        md_cell(item["bookSourceName"], 40),
                        md_cell(item["bookSourceUrl"], 70),
                        md_cell(item["readoriFailureClass"], 20),
                        md_cell(item["readoriFailureReason"], 80),
                        md_cell(item["readoriError"], 100),
                        md_cell(item["legadoStages"], 40),
                        md_cell(counts_text, 60),
                        "yes" if item["vacuous"] else "",
                    ]
                )
                + " |"
            )
        if len(legado_only) > max_rows:
            lines.append("")
            lines.append(f"... {len(legado_only) - max_rows} more rows in parity.json")
    else:
        lines.append("None.")
    lines.append("")

    lines.append(f"### Readori pass, Legado fail ({counts['readoriOnly']})")
    lines.append("")
    readori_only = parity["readoriOnly"]
    if readori_only:
        lines.append("| Source | URL | Legado class | Legado stage | Legado group | Legado error | Harness suspect |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for item in readori_only[:max_rows]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        md_cell(item["bookSourceName"], 40),
                        md_cell(item["bookSourceUrl"], 70),
                        md_cell(item["legadoFailureClass"], 20),
                        md_cell(item["legadoFailureStage"], 30),
                        md_cell(item["legadoFailureGroup"], 20),
                        md_cell(item["legadoError"], 100),
                        md_cell(item["legadoHarnessSuspect"], 30),
                    ]
                )
                + " |"
            )
        if len(readori_only) > max_rows:
            lines.append("")
            lines.append(f"... {len(readori_only) - max_rows} more rows in parity.json")
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------- CLI


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--legado", type=Path, required=True, help="Legado live report JSON")
    parser.add_argument("--readori", type=Path, required=True, help="Readori validator report JSON")
    parser.add_argument("--output-dir", type=Path, default=Path("."), help="directory for parity.json / parity.md")
    parser.add_argument("--json-out", type=Path, default=None, help="override the parity.json path")
    parser.add_argument("--md-out", type=Path, default=None, help="override the parity.md path")
    parser.add_argument("--target", type=float, default=DEFAULT_TARGET, help="relative-rate target (default 0.95)")
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS, help="max table rows per list in parity.md")
    parser.add_argument(
        "--fail-under",
        action="store_true",
        help="exit 1 when the relative rate is below --target (default: always exit 0 when the inputs are valid)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        legado_doc = load_report(args.legado)
        readori_doc = load_report(args.readori)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[PARITY] ERROR: {error}", file=sys.stderr)
        return 2
    parity = compare(legado_doc, readori_doc, target=args.target)
    json_path = args.json_out or (args.output_dir / "parity.json")
    md_path = args.md_out or (args.output_dir / "parity.md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(parity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(parity, max_rows=max(1, args.max_rows)), encoding="utf-8", newline="\n")
    counts = parity["counts"]
    rates = parity["rates"]
    print(
        "[PARITY] total={total} legadoPass={legadoPass} readoriPass={readoriPass} both={both} "
        "legadoOnly={legadoOnly} readoriOnly={readoriOnly} relativeRate={rate}".format(
            rate=_percent(rates["relativeRate"]), **{k: counts[k] for k in ("total", "legadoPass", "readoriPass", "both", "legadoOnly", "readoriOnly")}
        )
    )
    print(f"[PARITY] wrote {json_path} and {md_path}")
    if args.fail_under and not rates["meetsTarget"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
