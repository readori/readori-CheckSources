#!/usr/bin/env python3
"""R146 book-source workbench: command line sub-commands and the JSON-lines ``rpc`` service for the desktop UI.

Sub-commands (``source_validator_cli.py <command> ...``; the batch validator keeps its flag-only interface):

* ``debug``        step-through debugging of one source (Legado / App style key; ``--step`` on a terminal)
* ``test-rule``    replay one rule on a saved response (offline unless ``--online``)
* ``fix``          debug -> suggestions -> offline / online preview -> apply -> (re-validate) -> export
* ``validate-one`` the batch pipeline for exactly one source (``--trace`` prints the events)
* ``export``       write sources (optionally with the changes of a ``fix-report.json`` applied)
* ``rpc``          JSON-lines service on stdin / stdout (used by ``workbench_gui.py``; see ``RpcServer``)

Exit codes: 0 pass / the rule produced a result, 1 source invalid or empty rule result, 2 usage / input error,
3 needs login / verification, 4 timeout, 130 Ctrl+C.

All times that are shown or written are America/Los_Angeles (``source_edit`` does the zone handling).
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import io
import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable

try:  # package import (CLI / PyInstaller) or top-level import (scripts / tests)
    from . import app_profiles, debug_engine, debug_trace, fix_suggester, net_guard, rule_tester, source_edit
    from . import validate_source_packages as vsp  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised only outside the package
    import app_profiles  # type: ignore[no-redef]
    import debug_engine  # type: ignore[no-redef]
    import debug_trace  # type: ignore[no-redef]
    import fix_suggester  # type: ignore[no-redef]
    import net_guard  # type: ignore[no-redef]
    import rule_tester  # type: ignore[no-redef]
    import source_edit  # type: ignore[no-redef]
    import validate_source_packages as vsp  # type: ignore[no-redef]

SUBCOMMANDS = ("debug", "test-rule", "fix", "validate-one", "export", "rpc")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INTERACTION = 3
EXIT_TIMEOUT = 4
EXIT_INTERRUPTED = 130

_STATUS_EXIT = {"pass": EXIT_OK, "fail": EXIT_FAIL, "error": EXIT_FAIL, "needsInteraction": EXIT_INTERACTION,
                "timeout": EXIT_TIMEOUT, "cancelled": EXIT_INTERRUPTED}
_VERDICT_EXIT = {"pass": EXIT_OK, "invalid": EXIT_FAIL, "needsInteraction": EXIT_INTERACTION, "timeout": EXIT_TIMEOUT}
_RULE_SECTIONS = ("ruleSearch", "ruleExplore", "ruleBookInfo", "ruleToc", "ruleContent", "ruleReview")
_SLIM_KEYS = ("verdict", "verdictZh", "passed", "failureReason", "failureGroupZh", "stages", "responseTimeMs",
              "stabilityPassCount", "appVersion")
_STAGE_LABEL = {"search": "搜索", "detail": "详情", "toc": "目录", "content": "正文", "explore": "发现"}


class CliError(Exception):
    """Usage / input problem: printed to stderr, exit code 2."""

    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def slim_validation(result: Any) -> dict[str, Any] | None:
    """``validate_one`` result without the bulky ``detail`` / ``record``."""
    if not isinstance(result, dict):
        return None
    return {key: copy.deepcopy(result.get(key)) for key in _SLIM_KEYS if key in result}


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (set, tuple, frozenset)):
        return list(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def dumps_line(value: Any) -> str:
    """One-line JSON (no raw newlines, so a JSON-lines reader can split on ``\\n``)."""
    return json.dumps(value, ensure_ascii=False, default=_json_default, separators=(",", ":"))


def _one_line(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _err(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _view_of(working: dict[str, Any]) -> dict[str, Any]:
    """``working`` with rule sections that are stored as JSON strings decoded to objects (display only)."""
    view = copy.deepcopy(working)
    for key in _RULE_SECTIONS:
        value = view.get(key)
        if isinstance(value, str):
            try:
                decoded = source_edit._decode_rule_object(key, value)  # same decoder the editor uses
            except Exception:  # noqa: BLE001
                decoded = None
            if decoded is None:
                try:
                    parsed = json.loads(value)
                    decoded = parsed if isinstance(parsed, dict) else None
                except (TypeError, ValueError):
                    decoded = None
            if decoded is not None:
                view[key] = decoded
    return view


def _load_sources(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path).expanduser()
    if not target.exists():
        raise CliError(f"找不到输入：{target}")
    with contextlib.redirect_stdout(sys.stderr):  # the loader may print; stdout belongs to the protocol / jsonl
        sources, _names = vsp.load_sources_from_paths([target])
    if not sources:
        raise CliError("没有读到书源（文件不是书源 JSON / JSON5，或是空的）")
    return sources


def _describe(sources: list[dict[str, Any]], indexes: list[int], limit: int = 20) -> str:
    lines = [f"  [{i}] {sources[i].get('bookSourceName') or '（无名）'}  {sources[i].get('bookSourceUrl') or ''}"
             for i in indexes[:limit]]
    if len(indexes) > limit:
        lines.append(f"  … 还有 {len(indexes) - limit} 条")
    return "\n".join(lines)


def select_source(sources: list[dict[str, Any]], selector: str | None = None,
                  index: int | None = None) -> tuple[int, dict[str, Any]]:
    """``--source`` lookup: exact bookSourceUrl -> normalized URL -> exact name -> unique name substring.

    ``--index`` (0-based) picks directly.  Ambiguity, no match, or several sources without a selector is a
    ``CliError`` (exit 2) that lists the candidates."""
    if index is not None:
        if selector:
            raise CliError("--source 与 --index 不能同时使用")
        if not 0 <= index < len(sources):
            raise CliError(f"--index 超出范围：文件里有 {len(sources)} 条书源（序号从 0 开始）")
        return index, sources[index]
    if not selector:
        if len(sources) == 1:
            return 0, sources[0]
        shown = _describe(sources, list(range(len(sources))))
        raise CliError(f"文件里有 {len(sources)} 条书源，请用 --source URL|名称 或 --index N 指定：\n{shown}")

    text = selector.strip()
    urls = [str(src.get("bookSourceUrl") or "") for src in sources]
    names = [str(src.get("bookSourceName") or "") for src in sources]

    def canonical(url: str) -> str:
        return vsp.normalize_book_source_url(url).rstrip("/").lower()

    wanted = canonical(text)
    tiers: list[list[int]] = [
        [i for i, url in enumerate(urls) if url == text],
        [i for i, url in enumerate(urls) if canonical(url) == wanted or canonical(vsp.effective_base_url(url)) == wanted],
        [i for i, name in enumerate(names) if name == text],
        [i for i, name in enumerate(names) if text.lower() in name.lower()],
    ]
    for hits in tiers:
        if len(hits) == 1:
            return hits[0], sources[hits[0]]
        if len(hits) > 1:
            raise CliError(f"「{text}」匹配到 {len(hits)} 条书源，请改用更精确的 URL，或用 --index N：\n{_describe(sources, hits)}")
    raise CliError(f"没有匹配「{text}」的书源（可用 --index N，序号从 0 开始）")


def _version_arg(args: argparse.Namespace) -> str:
    try:
        return app_profiles.normalize_app_version(args.app_version)
    except ValueError as exc:
        raise CliError(str(exc))


@contextlib.contextmanager
def _network_policy(args: argparse.Namespace):
    previous = net_guard.network_allows_private()
    if getattr(args, "block_private_network", False):
        net_guard.set_network_policy(False)
    try:
        yield
    finally:
        if getattr(args, "block_private_network", False):
            net_guard.set_network_policy(previous)


class _Printer:
    """Event sink of the console commands: text lines (default), JSON lines (``--jsonl``) or nothing."""

    def __init__(self, args: argparse.Namespace, *, enabled: bool = True) -> None:
        self.jsonl = bool(getattr(args, "jsonl", False))
        self.enabled = enabled
        # --json alone: stdout carries only the summary JSON, the log goes to stderr
        self.stream = sys.stderr if (getattr(args, "json", False) and not self.jsonl) else sys.stdout

    def event(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        if self.jsonl:
            print(dumps_line(event), flush=True)
            return
        text = event.get("text")
        if text:
            print(text, file=self.stream, flush=True)

    def line(self, text: str) -> None:
        print(text, file=sys.stderr if self.jsonl else self.stream, flush=True)

    def summary(self, args: argparse.Namespace, data: dict[str, Any]) -> None:
        if not getattr(args, "json", False):
            return
        print(dumps_line({"summary": data}) if self.jsonl else json.dumps(data, ensure_ascii=False, indent=2,
                                                                          default=_json_default), flush=True)


def _prompt_pick(event: dict[str, Any], controller: debug_engine.DebugController) -> None:
    data = event.get("data") or {}
    choices = data.get("choices") or []
    label = "书籍" if data.get("what") == "book" else "章节"
    last = max((int(c.get("index", 0)) for c in choices), default=0)
    while True:
        try:
            raw = input(f"选择{label}序号 [0-{last}]（回车=0，q=停止）：").strip()
        except (EOFError, KeyboardInterrupt):
            raw = "q"
        if raw.lower() == "q":
            controller.cancel()
            return
        if not raw:
            controller.resume(0)
            return
        if raw.isdigit() and int(raw) <= last:
            controller.resume(int(raw))
            return
        print(f"请输入 0 到 {last} 之间的整数，或 q。", flush=True)


# ---------------------------------------------------------------------------
# debug
# ---------------------------------------------------------------------------

def _save_raw(session: debug_engine.DebugSession, directory: str | Path) -> None:
    target = session.raw.save(directory)
    _write_text(target / "variables.json", json.dumps(session.variables, ensure_ascii=False, indent=2))


def cmd_debug(args: argparse.Namespace) -> int:
    version = _version_arg(args)
    sources = _load_sources(args.file)
    _idx, src = select_source(sources, args.source, args.index)
    base_url = vsp.effective_base_url(str(src.get("bookSourceUrl") or ""))
    try:
        debug_engine.parse_debug_key(args.key, base_url)
    except ValueError as exc:
        raise CliError(str(exc))
    interactive = bool(args.step and sys.stdin is not None and sys.stdin.isatty())
    if args.step and not interactive:
        _err("--step 需要交互终端；已改用 --pick-book / --pick-chapter 选择")
    controller = debug_engine.DebugController()
    printer = _Printer(args)

    def on_event(event: dict[str, Any]) -> None:
        printer.event(event)
        if interactive and event.get("kind") == "pause":
            _prompt_pick(event, controller)

    try:
        with _network_policy(args):
            result = debug_engine.debug_source(
                src, args.key, on_event, step=interactive, app_version=version, timeout=args.timeout,
                controller=controller, pick_book=args.pick_book, pick_chapter=args.pick_chapter,
            )
    except KeyboardInterrupt:
        printer.line("■ 已停止")
        return EXIT_INTERRUPTED
    if args.save_raw:
        _save_raw(result.session, args.save_raw)
    code = _STATUS_EXIT.get(result.status, EXIT_FAIL)
    summary = result.to_dict()
    summary["exitCode"] = code
    printer.summary(args, summary)
    return code


# ---------------------------------------------------------------------------
# test-rule
# ---------------------------------------------------------------------------

def _read_body(path: str) -> str:
    try:
        if path == "-":
            data = sys.stdin.buffer.read()
        else:
            data = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise CliError(f"读不到 --body-file：{exc}")
    return data.decode("utf-8", errors="replace")


def cmd_test_rule(args: argparse.Namespace) -> int:
    version = _version_arg(args)
    field = args.field
    if field not in rule_tester.FIELD_KIND:
        raise CliError(f"不支持的字段 {field}；可用字段：\n  " + "\n  ".join(sorted(rule_tester.FIELD_KIND)))
    if args.raw_id is not None and not args.raw:
        raise CliError("--raw-id 需要同时给 --raw DIR")
    if not args.body_file and not args.raw:
        raise CliError("需要响应内容：--body-file FILE（- 表示 stdin），或 --raw DIR [--raw-id N]")
    sources = _load_sources(args.file)
    _idx, src = select_source(sources, args.source, args.index)
    variables: dict[str, Any] = {}
    if args.body_file:
        body = _read_body(args.body_file)
        base_url = args.base_url or vsp.effective_base_url(str(src.get("bookSourceUrl") or ""))
    else:
        try:
            store = debug_engine.RawStore.load(args.raw)
        except (OSError, ValueError) as exc:
            raise CliError(f"读不到 --raw 目录：{exc}")
        if args.raw_id is not None:
            entry = store.get(args.raw_id)
        else:
            stage = rule_tester.stage_of_field(field)
            entry = store.latest(stage) or (store.latest("search") if stage == "explore" else None)
        if entry is None:
            raise CliError("--raw 目录里没有符合的响应（先用 debug --save-raw 保存一次）")
        body = entry.body or ""
        base_url = args.base_url or entry.final_url or entry.url
        variables_file = Path(args.raw) / "variables.json"
        if variables_file.is_file():
            try:
                loaded = json.loads(variables_file.read_text(encoding="utf-8"))
                variables = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                variables = {}
    with _network_policy(args):
        result = rule_tester.test_rule(
            src, field, body=body, base_url=base_url, rule=args.rule, item_index=args.item, app_version=version,
            variables=variables, offline=not args.online,
        )
    printer = _Printer(args)
    code = EXIT_OK if result.ok else EXIT_FAIL
    if printer.jsonl:
        print(dumps_line(result.to_dict()), flush=True)
    elif not args.json:
        _print_rule_result(result)
    if args.json:
        data = result.to_dict()
        data["exitCode"] = code
        printer.summary(args, data)
    return code


def _print_rule_result(result: rule_tester.RuleTestResult) -> None:
    print(f"规则 {result.field}  ‹{_one_line(result.rule, 200)}›  （{result.kind} · {result.elapsed_ms} ms）")
    if result.items_count is not None:
        print(f"列表大小：{result.items_count}")
    if result.kind == "content" or result.kind == "replace":
        print(f"字数：{result.count}")
    elif result.kind != "list":
        print(f"结果：{result.count} 项")
    for position, value in enumerate(result.values, 1):
        print(f"  [{position}] {_one_line(value) or '∅'}")
    if result.kind == "list" and result.count > len(result.values):
        print(f"  … 另有 {result.count - len(result.values)} 项")
    if result.data.get("before") is not None and result.data.get("after") is not None:
        print(f"净化前 {result.data['before']} 字 → 净化后 {result.data['after']} 字")
    for warning in result.warnings:
        print(f"⚠ {warning}")
    if not result.ok:
        print(f"✗ {result.error or '没有结果'}", file=sys.stderr)


# ---------------------------------------------------------------------------
# validate-one
# ---------------------------------------------------------------------------

def _stage_line(stages: dict[str, Any]) -> str:
    return " ".join(f"{_STAGE_LABEL.get(name, name)}{'✓' if ok else '✗'}" for name, ok in stages.items())


def _verdict_line(result: dict[str, Any]) -> str:
    version = result.get("appVersion") or ""
    if result.get("passed"):
        head = f"✓ 可用 · App {version}"
    else:
        reason = result.get("failureReason") or ""
        group = result.get("failureGroupZh") or ""
        head = f"✗ {result.get('verdictZh') or '失效'} · App {version}"
        if reason:
            head += f" · {reason}" + (f"（{group}）" if group else "")
    tail = f" · {result.get('responseTimeMs') or 0} ms"
    stages = result.get("stages") or {}
    if stages:
        tail += f" · {_stage_line(stages)}"
    return head + tail


def cmd_validate_one(args: argparse.Namespace) -> int:
    raw_version = args.app_version
    versions = ["1.1.1", "1.1.2"] if raw_version == "both" else [_version_arg(args)]
    sources = _load_sources(args.file)
    _idx, src = select_source(sources, args.source, args.index)
    printer = _Printer(args, enabled=bool(args.trace))
    results: dict[str, dict[str, Any]] = {}
    try:
        with _network_policy(args):
            for version in versions:
                trace = None
                if args.trace:
                    trace = debug_engine.DebugSession(on_event=printer.event, app_version=version)
                results[version] = debug_engine.validate_one(
                    copy.deepcopy(src), app_version=version, quick_timeout=args.quick_timeout,
                    source_timeout=args.source_timeout, rounds=args.rounds, trace=trace,
                )
    except KeyboardInterrupt:
        _err("■ 已停止")
        return EXIT_INTERRUPTED
    printer_line = _Printer(args)
    if not printer_line.jsonl:
        for version in versions:
            printer_line.line(_verdict_line(results[version]))
        if len(versions) == 2:
            printer_line.line("两版结论：" + app_profiles.compatibility_label(
                results["1.1.1"]["verdict"], results["1.1.2"]["verdict"]))
    code = max((_VERDICT_EXIT.get(r["verdict"], EXIT_FAIL) for r in results.values()),
               key=lambda c: {EXIT_OK: 0, EXIT_FAIL: 1, EXIT_INTERACTION: 2, EXIT_TIMEOUT: 3}.get(c, 1))
    data: dict[str, Any] = {"results": {v: slim_validation(r) for v, r in results.items()}, "exitCode": code}
    if len(versions) == 1:
        data.update(slim_validation(results[versions[0]]) or {})
    else:
        data["compatibility"] = app_profiles.compatibility_label(results["1.1.1"]["verdict"], results["1.1.2"]["verdict"])
    printer_line.summary(args, data)
    return code


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------

def _csv_codes(text: str | None) -> list[str]:
    return [item.strip().upper() for item in (text or "").replace(" ", ",").split(",") if item.strip()]


def _preview_state(suggestion: fix_suggester.Suggestion, outcome: dict[str, Any] | None, online: bool) -> str:
    if not suggestion.patch or suggestion.preview_kind == "none":
        return "仅提示"
    if suggestion.preview_kind == "offline":
        return "离线预览 ✓" if suggestion.apply_allowed else "离线预览 ✗"
    if online and outcome is not None and outcome.get("ok") is not None:
        return "在线预览 ✓" if outcome.get("ok") else "在线预览 ✗"
    return "需在线预览"


def _comment_block(source: dict[str, Any]) -> str | None:
    raw = source.get("bookSourceComment")
    if not isinstance(raw, str):
        return None
    for block in raw.split("\n\n"):
        if block.lstrip().startswith(source_edit.FIXED_PREFIX):
            return block
    return None


def cmd_fix(args: argparse.Namespace) -> int:
    version = _version_arg(args)
    sources = _load_sources(args.file)
    _idx, src = select_source(sources, args.source, args.index)
    base_url = vsp.effective_base_url(str(src.get("bookSourceUrl") or ""))
    try:
        debug_engine.parse_debug_key(args.key, base_url)
    except ValueError as exc:
        raise CliError(str(exc))
    want = _csv_codes(args.apply)
    printer = _Printer(args, enabled=bool(args.show_log) or bool(getattr(args, "jsonl", False)))
    controller = debug_engine.DebugController()
    out_path = Path(args.out).expanduser() if args.out else Path(args.file).expanduser().with_name(
        Path(args.file).stem + ".fixed.json")
    report_path = Path(args.report).expanduser() if args.report else out_path.with_name("fix-report.json")

    try:
        with _network_policy(args):
            result = debug_engine.debug_source(
                copy.deepcopy(src), args.key, printer.event, app_version=version, timeout=args.timeout,
                controller=controller,
            )
            session = result.session
            if args.save_raw:
                _save_raw(session, args.save_raw)
            suggestions = fix_suggester.suggest(src, session, keep_source_url=args.keep_source_url, app_version=version)
            previews: dict[str, dict[str, Any]] = {}
            for suggestion in suggestions:
                if not suggestion.patch or suggestion.preview_kind == "none":
                    continue
                online = bool(args.online_preview) and suggestion.preview_kind == "online"
                try:
                    outcome = fix_suggester.preview(src, suggestion, session, online=online, app_version=version)
                except Exception as exc:  # noqa: BLE001
                    outcome = {"ok": False, "summary": f"预览失败：{type(exc).__name__}: {exc}", "apply_allowed": False}
                previews[suggestion.id] = outcome
                if suggestion.preview_kind == "online" and online:
                    suggestion.apply_allowed = bool(outcome.get("apply_allowed"))
            doc = source_edit.SourceDocument(src)
            applied, refused = _apply_chosen(doc, suggestions, want, bool(args.apply_safe), _csv_codes_unknown(want, suggestions))
            doc.before_result = slim_validation(result.verdict)
            compare_data: dict[str, Any] | None = None
            if args.revalidate:
                if doc.changed:
                    compare = debug_engine.compare_sources(
                        doc.original, doc.working, app_version=version, quick_timeout=args.quick_timeout,
                        source_timeout=args.source_timeout)
                    doc.before_result, doc.after_result = slim_validation(compare["before"]), slim_validation(compare["after"])
                    compare_data = {"before": doc.before_result, "after": doc.after_result, "rows": compare["rows"],
                                    "changed": compare["changed"]}
                else:
                    before = debug_engine.validate_one(copy.deepcopy(src), app_version=version,
                                                       quick_timeout=args.quick_timeout, source_timeout=args.source_timeout)
                    doc.before_result = slim_validation(before)
    except KeyboardInterrupt:
        _err("■ 已停止")
        return EXIT_INTERRUPTED

    # ---- human summary -------------------------------------------------
    human = not getattr(args, "jsonl", False)
    if human:
        _print_fix_summary(printer, result, suggestions, previews, applied, refused, args)
    written: dict[str, str] = {}
    exported: list[dict[str, Any]] = []
    if doc.changed:
        exported = source_edit.export_sources([doc], keep_source_url=args.keep_source_url, stamp=True)
        entry = doc.to_entry()
        if exported:
            entry["comment"] = _comment_block(exported[0])
        report = source_edit.build_fix_report([entry], app_version=version, keep_source_url=args.keep_source_url)
        if not args.dry_run:
            _write_text(out_path, source_edit.dumps_sources(exported) + "\n")
            _write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            written = {"out": str(out_path), "report": str(report_path)}
    code = _fix_exit_code(result, doc, compare_data)
    if human:
        printer.line(_fix_verdict_line(result, doc, compare_data, written, args.dry_run))
    data = {
        "debug": result.to_dict(),
        "suggestions": [dict(s.to_dict(), preview=previews.get(s.id)) for s in suggestions],
        "applied": applied, "refused": refused, "diff": doc.diff(), "compare": compare_data,
        "dryRun": bool(args.dry_run), "written": written, "exitCode": code,
    }
    if printer.jsonl:
        for item in data["suggestions"]:
            print(dumps_line({"suggestion": item}), flush=True)
    printer.summary(args, data)
    return code


def _csv_codes_unknown(want: list[str], suggestions: list[fix_suggester.Suggestion]) -> list[str]:
    present = {s.code.upper() for s in suggestions}
    return [code for code in want if code not in present]


def _apply_chosen(doc: source_edit.SourceDocument, suggestions: list[fix_suggester.Suggestion], want: list[str],
                  apply_safe: bool, unknown: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    applied: list[str] = []
    refused: list[dict[str, str]] = [{"code": code, "reason": "本次调试没有产生这条建议"} for code in unknown]
    for suggestion in suggestions:
        chosen = suggestion.code.upper() in want
        if not chosen and not (apply_safe and fix_suggester.is_safe(suggestion)):
            continue
        if not suggestion.patch:
            refused.append({"code": suggestion.code, "reason": "仅提示，没有可应用的补丁"})
            continue
        if not suggestion.apply_allowed:
            refused.append({"code": suggestion.code, "reason": "预览未通过，不应用"})
            continue
        try:
            fix_suggester.apply_suggestion(doc, suggestion)
        except source_edit.PatchConflict as exc:
            refused.append({"code": suggestion.code, "reason": f"补丁已过期：{exc}"})
            continue
        except (ValueError, TypeError, KeyError) as exc:
            refused.append({"code": suggestion.code, "reason": f"应用失败：{exc}"})
            continue
        applied.append(suggestion.code)
    return applied, refused


def _print_fix_summary(printer: _Printer, result: debug_engine.DebugResult, suggestions: list[fix_suggester.Suggestion],
                       previews: dict[str, dict[str, Any]], applied: list[str], refused: list[dict[str, str]],
                       args: argparse.Namespace) -> None:
    if not suggestions:
        printer.line("没有可用的修复建议" + ("" if result.ok else f"（调试结论：{result.message}）"))
    else:
        printer.line(f"建议 {len(suggestions)} 条：")
        for suggestion in suggestions:
            outcome = previews.get(suggestion.id)
            state = _preview_state(suggestion, outcome, bool(args.online_preview))
            mark = "已应用" if suggestion.code in applied else "未应用"
            printer.line(f"  {suggestion.code} {suggestion.title}（{state}，{mark}）  [{suggestion.confidence}]")
            if suggestion.reason:
                printer.line(f"      原因：{_one_line(suggestion.reason, 200)}")
            if outcome and outcome.get("summary"):
                printer.line(f"      预览：{_one_line(outcome['summary'], 200)}")
    for item in refused:
        printer.line(f"  ✗ {item['code']} 未应用：{item['reason']}")


def _fix_exit_code(result: debug_engine.DebugResult, doc: source_edit.SourceDocument,
                   compare: dict[str, Any] | None) -> int:
    if compare is not None:
        after = compare.get("after") or {}
        return _VERDICT_EXIT.get(str(after.get("verdict")), EXIT_FAIL)
    if result.ok or doc.changed:
        return EXIT_OK
    return _STATUS_EXIT.get(result.status, EXIT_FAIL)


def _verdict_brief(result: dict[str, Any] | None, fallback: str = "未知") -> str:
    if not result:
        return fallback
    if result.get("passed"):
        return "可用"
    reason = result.get("failureReason") or ""
    return f"{result.get('verdictZh') or '失效'}" + (f"（{reason}）" if reason else "")


def _fix_verdict_line(result: debug_engine.DebugResult, doc: source_edit.SourceDocument, compare: dict[str, Any] | None,
                      written: dict[str, str], dry_run: bool) -> str:
    before = _verdict_brief(doc.before_result, "可用" if result.ok else f"失败（{result.message}）")
    parts = [f"修复前：{before}"]
    if compare is not None:
        parts.append(f"修复后：{_verdict_brief(compare.get('after'))}")
    elif doc.changed:
        parts.append("修复后：未重新校验（加 --revalidate）")
    else:
        parts.append("没有应用任何修改")
    line = " → ".join(parts)
    if written:
        line += f" · 已写 {written['out']}、{written['report']}"
    elif dry_run and doc.changed:
        line += " · --dry-run：未写文件"
    return line


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _apply_report_changes(doc: source_edit.SourceDocument, changes: list[dict[str, Any]]) -> list[str]:
    skipped: list[str] = []
    for change in changes:
        path = str(change.get("path") or "")
        if not path:
            continue
        op = "delete" if change.get("new") is None and change.get("old") is not None else "set"
        try:
            doc.apply([{"op": op, "path": path, "old": change.get("old"), "new": change.get("new")}],
                      reason=str(change.get("reason") or ""), origin=str(change.get("origin") or "manual"))
        except (ValueError, TypeError, KeyError) as exc:
            skipped.append(f"{path}: {exc}")
    return skipped


def cmd_export(args: argparse.Namespace) -> int:
    version = _version_arg(args)
    sources = _load_sources(args.file)
    if args.source or args.index is not None:
        _idx, picked = select_source(sources, args.source, args.index)
        subset = [picked]
    else:
        subset = sources
    report: dict[str, Any] | None = None
    if args.from_report:
        try:
            report = json.loads(Path(args.from_report).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CliError(f"读不到 --from-report：{exc}")
        if not isinstance(report, dict) or report.get("schema") != source_edit.REPORT_SCHEMA:
            raise CliError(f"--from-report 不是 {source_edit.REPORT_SCHEMA} 报告")
    # report item -> source: same original bookSourceUrl; several sources sharing it are told apart by name
    assigned: dict[int, dict[str, Any]] = {}
    for item in (report or {}).get("sources") or []:
        url = str(item.get("originalBookSourceUrl") or item.get("bookSourceUrl") or "")
        hits = [i for i, src in enumerate(sources) if str(src.get("bookSourceUrl") or "") == url]
        if len(hits) > 1:
            named = [i for i in hits if str(sources[i].get("bookSourceName") or "") == str(item.get("bookSourceName") or "")]
            hits = named or hits
        if len(hits) == 1:
            assigned.setdefault(hits[0], item)
        else:
            _err(f"⚠ 报告里的 {url or '（无地址）'} 在文件里{'找不到' if not hits else '对应多条书源'}，已跳过")
    docs: list[source_edit.SourceDocument] = []
    skipped: list[str] = []
    for position, src in enumerate(sources):
        if not any(src is picked for picked in subset):
            continue
        doc = source_edit.SourceDocument(src)
        item = assigned.get(position)
        if item:
            skipped += _apply_report_changes(doc, list(item.get("changes") or []))
            doc.before_result, doc.after_result = item.get("before"), item.get("after")
        docs.append(doc)
    exported = source_edit.export_sources(docs, only_changed=args.only_changed, only_passed=args.only_passed,
                                          keep_source_url=args.keep_source_url, stamp=True)
    for line in skipped:
        _err(f"⚠ 跳过无法应用的改动 {line}")
    out_path = Path(args.out).expanduser() if args.out else Path(args.file).expanduser().with_name(
        Path(args.file).stem + ".export.json")
    printer = _Printer(args)
    if not exported:
        printer.line("没有符合条件的书源可导出，未写文件")
        printer.summary(args, {"count": 0, "written": None, "exitCode": EXIT_FAIL, "appVersion": version})
        return EXIT_FAIL
    _write_text(out_path, source_edit.dumps_sources(exported) + "\n")
    printer.line(f"已导出 {len(exported)} 条书源 → {out_path}")
    printer.summary(args, {"count": len(exported), "written": str(out_path), "skipped": skipped, "exitCode": EXIT_OK,
                           "appVersion": version})
    return EXIT_OK


# ---------------------------------------------------------------------------
# rpc
# ---------------------------------------------------------------------------

class _Job:
    def __init__(self, request_id: Any, kind: str) -> None:
        self.id = request_id
        self.kind = kind
        self.controller = debug_engine.DebugController()


class RpcServer:
    """Request / response / stream protocol on JSON lines (UTF-8, one object per line).

    Request   ``{"id": n, "method": "...", "params": {...}}``
    Reply     ``{"id": n, "result": ...}`` or ``{"id": n, "error": "..."}``
    Stream    ``{"id": n, "event": {...}}`` (a debug event, see debug_engine) while job ``n`` runs
    Terminal  ``{"id": n, "done": {"status", "ok", "message", "result"?...}}`` once, after a job's last event

    Jobs (``debug``, ``validate``, ``compare`` and an online ``preview``) are acknowledged with ``result
    {"started": true}``; only one runs at a time (a second request gets ``error`` "正在运行").  Quick methods run in
    their own thread so ``cancel`` / ``resume`` stay responsive.  Documents live in the session under
    ``sourceKey`` = ``"s<index>"`` (index into the loaded list).

    Methods: load, edit (get / set / delete / undo / redo / reset / replace / restore), debug, resume, cancel, validate,
    compare, suggest, preview, apply_suggestion, test_rule, raw, raw_get, export, ping, shutdown.
    """

    JOB_METHODS = ("debug", "validate", "compare")
    INLINE_METHODS = ("load", "edit", "resume", "cancel", "apply_suggestion", "raw", "raw_get", "export", "ping",
                      "shutdown")
    THREAD_METHODS = ("test_rule", "suggest", "preview")

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self._emit = emit
        self.sources: list[dict[str, Any]] = []
        self.docs: dict[str, source_edit.SourceDocument] = {}
        self.sessions: dict[str, debug_engine.DebugSession] = {}
        self.suggestions: dict[str, dict[str, fix_suggester.Suggestion]] = {}
        self.job: _Job | None = None
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.threads: list[threading.Thread] = []

    # -- plumbing ----------------------------------------------------------
    def emit(self, message: dict[str, Any]) -> None:
        try:
            self._emit(message)
        except Exception:  # noqa: BLE001 - a closed pipe must not crash worker threads
            pass

    def reply(self, rid: Any, result: Any = None, error: str | None = None) -> None:
        if error is not None:
            self.emit({"id": rid, "error": error})
        else:
            self.emit({"id": rid, "result": result})

    def handle(self, message: Any) -> None:
        """Dispatch one decoded request.  Never raises."""
        if not isinstance(message, dict):
            self.reply(None, error="请求必须是 JSON 对象")
            return
        rid = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        handler = getattr(self, f"m_{method}", None) if method and method.replace("_", "").isalnum() else None
        if handler is None:
            self.reply(rid, error=f"未知方法：{method}")
            return
        if method in self.JOB_METHODS:
            self._start_job(rid, method, params, handler)
        elif method == "preview" and params.get("online"):
            self._start_job(rid, method, params, self._preview_job)
        elif method in self.THREAD_METHODS:
            self._spawn(rid, handler, params)
        else:
            self._run_inline(rid, handler, params)

    def _run_inline(self, rid: Any, handler: Callable[[dict[str, Any]], Any], params: dict[str, Any]) -> None:
        try:
            result = handler(params)
        except KeyError as exc:
            self.reply(rid, error=str(exc.args[0]) if exc.args else "KeyError")
        except (ValueError, source_edit.PatchConflict) as exc:
            self.reply(rid, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            self.reply(rid, error=f"{type(exc).__name__}: {exc}")
        else:
            self.reply(rid, result)

    def _spawn(self, rid: Any, handler: Callable[[dict[str, Any]], Any], params: dict[str, Any]) -> None:
        thread = threading.Thread(target=self._run_inline, args=(rid, handler, params), daemon=True)
        self.threads.append(thread)
        thread.start()

    def _start_job(self, rid: Any, kind: str, params: dict[str, Any], runner: Callable[..., Any]) -> None:
        with self.lock:
            if self.job is not None:
                self.reply(rid, error="正在运行")
                return
            try:
                if kind == "debug":
                    debug_engine.parse_debug_key(str(params.get("key") or ""), "")
                version = str(params.get("appVersion") or app_profiles.DEFAULT_APP_VERSION)
                if not (kind == "validate" and version == "both"):
                    app_profiles.normalize_app_version(version)
                if kind == "compare" and version == "both":
                    raise ValueError("对比请选择具体的 App 版本（1.1.1 或 1.1.2）")
                self._doc(params)  # fail early for an unknown source
            except (ValueError, KeyError) as exc:
                self.reply(rid, error=str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc))
                return
            job = _Job(rid, kind)
            self.job = job
        self.reply(rid, {"started": True, "kind": kind})
        thread = threading.Thread(target=self._job_main, args=(job, runner, params), daemon=True)
        self.threads.append(thread)
        thread.start()

    def _job_main(self, job: _Job, runner: Callable[..., Any], params: dict[str, Any]) -> None:
        payload: dict[str, Any] = {"status": "error", "ok": False, "message": ""}
        try:
            payload = runner(job, params)
        except debug_trace.DebugCancelled:
            payload = {"status": "cancelled", "ok": False, "message": "■ 已停止"}
        except Exception as exc:  # noqa: BLE001
            payload = {"status": "error", "ok": False, "message": f"{type(exc).__name__}: {exc}"}
        finally:
            with self.lock:
                self.job = None
            self.emit({"id": job.id, "done": payload})

    # -- documents -----------------------------------------------------------
    def _doc(self, params: dict[str, Any]) -> source_edit.SourceDocument:
        key = str(params.get("sourceKey") or "")
        doc = self.docs.get(key)
        if doc is None:
            raise KeyError(f"没有这条书源：{key or '（未指定 sourceKey）'}")
        return doc

    def _working(self, params: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self._doc(params).working)

    def _state(self, key: str) -> dict[str, Any]:
        doc = self.docs[key]
        return {"sourceKey": key, "working": copy.deepcopy(doc.working), "view": _view_of(doc.working),
                "changed": doc.changed, "canUndo": doc.can_undo, "canRedo": doc.can_redo, "diff": doc.diff(),
                "changes": doc.changes(), "unified": doc.unified_diff()}

    @staticmethod
    def _row(index: int, src: dict[str, Any]) -> dict[str, Any]:
        return {"sourceKey": f"s{index}", "index": index, "name": str(src.get("bookSourceName") or ""),
                "url": str(src.get("bookSourceUrl") or ""), "group": str(src.get("bookSourceGroup") or ""),
                "enabled": src.get("enabled", True) is not False}

    # -- methods ---------------------------------------------------------------
    def m_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "busy": self.job is not None}

    def m_shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        self.stopped.set()
        job = self.job
        if job is not None:
            job.controller.cancel()
        return {"bye": True}

    def m_load(self, params: dict[str, Any]) -> dict[str, Any]:
        path = params.get("path")
        if not path:
            raise ValueError("load 需要 path")
        loaded = _load_sources(path)
        with self.lock:
            if not params.get("append"):
                self.sources, self.docs, self.sessions, self.suggestions = [], {}, {}, {}
            for src in loaded:
                index = len(self.sources)
                self.sources.append(src)
                self.docs[f"s{index}"] = source_edit.SourceDocument(src)
            rows = [self._row(i, s) for i, s in enumerate(self.sources)]
        return {"sources": rows, "count": len(rows), "added": len(loaded)}

    def m_edit(self, params: dict[str, Any]) -> dict[str, Any]:
        key = str(params.get("sourceKey") or "")
        op = str(params.get("op") or "set")
        reason = str(params.get("reason") or "手动编辑")
        with self.lock:
            doc = self._doc(params)
            if op == "set":
                doc.set(str(params.get("path") or ""), params.get("value"), reason=reason)
            elif op == "delete":
                doc.delete(str(params.get("path") or ""), reason=reason)
            elif op == "get":
                pass  # just return the current state
            elif op == "undo":
                doc.undo()
            elif op == "redo":
                doc.redo()
            elif op == "reset":
                doc.reset()
            elif op in ("replace", "restore"):
                text = params.get("json")
                target = json.loads(text) if isinstance(text, str) else params.get("working")
                if not isinstance(target, dict):
                    raise ValueError("JSON 必须是一个书源对象（{…}）")
                if op == "restore":
                    doc.reset()
                patch = _patch_between(doc.working, target)
                if patch:
                    doc.apply(patch, reason="JSON 编辑" if op == "replace" else "恢复编辑", origin="manual")
            else:
                raise ValueError(f"未知编辑操作：{op}")
            return self._state(key)

    def m_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        job = self.job
        if job is None:
            return {"cancelled": False}
        job.controller.cancel()
        return {"cancelled": True, "job": job.kind}

    def m_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        job = self.job
        if job is None:
            return {"resumed": False}
        job.controller.resume(int(params.get("choice") or 0))
        return {"resumed": True}

    # jobs ---------------------------------------------------------------------
    def m_debug(self, job: _Job, params: dict[str, Any]) -> dict[str, Any]:
        working = self._working(params)
        version = app_profiles.normalize_app_version(params.get("appVersion") or app_profiles.DEFAULT_APP_VERSION)
        session = debug_engine.DebugSession(on_event=lambda event: self.emit({"id": job.id, "event": event}),
                                            controller=job.controller, app_version=version)
        key = str(params.get("sourceKey") or "")
        with self.lock:
            self.sessions[key] = session
            self.suggestions.pop(key, None)
        result = debug_engine.debug_source(
            working, str(params.get("key") or ""), None, step=bool(params.get("step")), app_version=version,
            timeout=int(params.get("timeout") or 60), controller=job.controller,
            pick_book=int(params.get("pickBook") or 0), pick_chapter=int(params.get("pickChapter") or 0),
            session=session,
        )
        payload = result.to_dict()
        payload["raw"] = session.raw.summaries()
        return payload

    def m_validate(self, job: _Job, params: dict[str, Any]) -> dict[str, Any]:
        working = self._working(params)
        raw_version = str(params.get("appVersion") or app_profiles.DEFAULT_APP_VERSION)
        versions = ["1.1.1", "1.1.2"] if raw_version == "both" else [raw_version]
        results: dict[str, dict[str, Any]] = {}
        for version in versions:
            trace = None
            if params.get("trace", True):
                trace = debug_engine.DebugSession(
                    on_event=lambda event: self.emit({"id": job.id, "event": event}),
                    controller=job.controller, app_version=version)
            results[version] = debug_engine.validate_one(
                copy.deepcopy(working), app_version=version, quick_timeout=int(params.get("quickTimeout") or 8),
                source_timeout=int(params.get("sourceTimeout") or 60), rounds=int(params.get("rounds") or 1), trace=trace,
            )
            if job.controller.cancelled:
                raise debug_trace.DebugCancelled()
        slim = {version: slim_validation(item) for version, item in results.items()}
        payload: dict[str, Any] = {"status": "pass" if all(r["passed"] for r in results.values()) else "fail",
                                   "ok": all(r["passed"] for r in results.values()), "message": "", "results": slim}
        if len(versions) == 1:
            payload["result"] = slim[versions[0]]
            payload["message"] = _verdict_line(results[versions[0]])
            with self.lock:
                doc = self._doc(params)
                if doc.changed:
                    doc.after_result = slim[versions[0]]
                else:
                    doc.before_result = slim[versions[0]]
        else:
            payload["compatibility"] = app_profiles.compatibility_label(results["1.1.1"]["verdict"],
                                                                         results["1.1.2"]["verdict"])
            payload["message"] = payload["compatibility"]
        return payload

    def m_compare(self, job: _Job, params: dict[str, Any]) -> dict[str, Any]:
        working = self._working(params)
        with self.lock:
            original = copy.deepcopy(self._doc(params).original)
        version = app_profiles.normalize_app_version(params.get("appVersion") or app_profiles.DEFAULT_APP_VERSION)
        compare = debug_engine.compare_sources(
            original, working, app_version=version, quick_timeout=int(params.get("quickTimeout") or 8),
            source_timeout=int(params.get("sourceTimeout") or 60), rounds=int(params.get("rounds") or 1),
        )
        before, after = slim_validation(compare["before"]), slim_validation(compare["after"])
        with self.lock:
            doc = self._doc(params)
            doc.before_result, doc.after_result = before, after
        return {"status": "pass" if compare["after"]["passed"] else "fail", "ok": bool(compare["after"]["passed"]),
                "message": f"修复前：{_verdict_brief(before)} → 修复后：{_verdict_brief(after)}",
                "before": before, "after": after, "rows": compare["rows"], "elapsedMs": compare["elapsedMs"],
                "changed": compare["changed"]}

    def _preview_job(self, job: _Job, params: dict[str, Any]) -> dict[str, Any]:
        outcome = self._preview(params, online=True)
        return {"status": "pass" if outcome.get("ok") else "fail", "ok": bool(outcome.get("ok")),
                "message": str(outcome.get("summary") or ""), "preview": outcome}

    # quick methods ----------------------------------------------------------
    def _session(self, params: dict[str, Any]) -> debug_engine.DebugSession:
        session = self.sessions.get(str(params.get("sourceKey") or ""))
        if session is None:
            raise ValueError("还没有调试记录，请先点「调试」")
        return session

    def m_test_rule(self, params: dict[str, Any]) -> dict[str, Any]:
        working = self._working(params)
        field = str(params.get("field") or "")
        if field not in rule_tester.FIELD_KIND:
            raise ValueError(f"不支持的字段：{field}")
        version = app_profiles.normalize_app_version(params.get("appVersion") or app_profiles.DEFAULT_APP_VERSION)
        rule = params.get("rule")
        rule = str(rule) if rule not in (None, "") else None
        item = int(params.get("itemIndex") or 0)
        online = bool(params.get("online"))
        session = self._session(params) if (params.get("rawId") is not None or params.get("body") is None) else None
        if params.get("body") is not None:
            result = rule_tester.test_rule(working, field, body=str(params["body"]), base_url=str(params.get("baseUrl") or ""),
                                           rule=rule, item_index=item, app_version=version, offline=not online)
        elif params.get("rawId") is not None:
            entry = session.raw.get(params["rawId"])  # type: ignore[union-attr]
            if entry is None:
                raise ValueError(f"没有这条原始响应：{params['rawId']}")
            result = rule_tester.test_rule(working, field, body=entry.body or "", base_url=entry.final_url or entry.url,
                                           rule=rule, item_index=item, app_version=version,
                                           variables=session.variables, offline=not online)  # type: ignore[union-attr]
        else:
            result = rule_tester.test_rule_on_session(working, session, field, rule=rule, item_index=item,
                                                      app_version=version, online=online)
        return result.to_dict()

    def m_suggest(self, params: dict[str, Any]) -> dict[str, Any]:
        working = self._working(params)
        session = self._session(params)
        version = app_profiles.normalize_app_version(params.get("appVersion") or app_profiles.DEFAULT_APP_VERSION)
        suggestions = fix_suggester.suggest(working, session, keep_source_url=bool(params.get("keepSourceUrl")),
                                            app_version=version)
        with self.lock:
            self.suggestions[str(params.get("sourceKey") or "")] = {s.id: s for s in suggestions}
        return {"suggestions": [s.to_dict() for s in suggestions]}

    def _suggestion(self, params: dict[str, Any]) -> fix_suggester.Suggestion:
        found = self.suggestions.get(str(params.get("sourceKey") or ""), {}).get(str(params.get("suggestionId") or ""))
        if found is None:
            raise ValueError("没有这条建议，请先重新生成建议")
        if params.get("candidate") is not None:
            found = fix_suggester.with_candidate(found, int(params["candidate"]))
        return found

    def _preview(self, params: dict[str, Any], *, online: bool) -> dict[str, Any]:
        working = self._working(params)
        session = self._session(params)
        suggestion = self._suggestion(params)
        version = app_profiles.normalize_app_version(params.get("appVersion") or app_profiles.DEFAULT_APP_VERSION)
        outcome = fix_suggester.preview(working, suggestion, session, online=online, app_version=version)
        return json.loads(json.dumps(outcome, default=_json_default))

    def m_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._preview(params, online=False)

    def m_apply_suggestion(self, params: dict[str, Any]) -> dict[str, Any]:
        suggestion = self._suggestion(params)
        key = str(params.get("sourceKey") or "")
        with self.lock:
            doc = self._doc(params)
            try:
                fix_suggester.apply_suggestion(doc, suggestion)
            except source_edit.PatchConflict as exc:
                raise ValueError(f"补丁已过期（书源在建议之后被改过）：{exc}")
            return self._state(key)

    def m_raw(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"entries": self._session(params).raw.summaries()}

    def m_raw_get(self, params: dict[str, Any]) -> dict[str, Any]:
        entry = self._session(params).raw.get(params.get("rawId"))
        if entry is None:
            raise ValueError(f"没有这条原始响应：{params.get('rawId')}")
        max_bytes = params.get("maxBytes")
        return entry.to_dict(include_body=True, max_bytes=int(max_bytes) if max_bytes else None)

    def m_export(self, params: dict[str, Any]) -> dict[str, Any]:
        keys = params.get("sourceKeys")
        with self.lock:
            docs = [self.docs[k] for k in keys] if keys else [self.docs[k] for k in sorted(self.docs, key=lambda x: int(x[1:]))]
            keep = bool(params.get("keepSourceUrl"))
            only_changed = bool(params.get("onlyChanged", True))
            only_passed = bool(params.get("onlyPassed"))
            exported: list[dict[str, Any]] = []
            entries: list[dict[str, Any]] = []
            for doc in docs:
                one = source_edit.export_sources([doc], only_changed=only_changed, only_passed=only_passed,
                                                 keep_source_url=keep, stamp=bool(params.get("stamp", True)))
                if not one:
                    continue
                exported.append(one[0])
                entry = doc.to_entry()
                entry["comment"] = _comment_block(one[0]) if doc.changed else ""
                entries.append(entry)
            version = str(params.get("appVersion") or app_profiles.DEFAULT_APP_VERSION)
            report = source_edit.build_fix_report(entries, app_version=version if version in app_profiles.APP_PROFILES else
                                                  app_profiles.DEFAULT_APP_VERSION, keep_source_url=keep)
        written: dict[str, str] = {}
        if exported and params.get("path"):
            _write_text(Path(str(params["path"])), source_edit.dumps_sources(exported) + "\n")
            written["path"] = str(params["path"])
            if params.get("reportPath"):
                _write_text(Path(str(params["reportPath"])), json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                written["reportPath"] = str(params["reportPath"])
        out: dict[str, Any] = {"count": len(exported), "written": written}
        if not params.get("path"):
            out["sources"] = exported
            out["report"] = report
        return out


def _patch_between(current: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
    """Patch (set / delete ops) that turns ``current`` into ``target``; ignores the 'string rule converted' notes."""
    missing = object()
    patch: list[dict[str, Any]] = []
    for entry in source_edit.diff_values(current, target):
        if "new" not in entry:
            continue
        path = entry["path"]
        if not path:
            continue
        deleted = source_edit.get_path(target, path, missing) is missing
        patch.append({"op": "delete" if deleted else "set", "path": path, "old": entry.get("old"),
                      "new": entry.get("new")})
    return patch


_ORIGINAL_STDOUT: Any = None
_PROTOCOL_STREAM: Any = None


def run_rpc(stdin: Any = None, stdout: Any = None) -> int:
    """Serve the protocol until EOF / ``shutdown``.  Stray ``print`` calls of the validator go to stderr."""
    global _ORIGINAL_STDOUT, _PROTOCOL_STREAM
    vsp.configure_stdio_encoding()
    if stdout is None:
        _ORIGINAL_STDOUT = sys.stdout
        _PROTOCOL_STREAM = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n", write_through=True)
        stdout = _PROTOCOL_STREAM
        sys.stdout = sys.stderr
    source = stdin if stdin is not None else sys.stdin.buffer
    write_lock = threading.Lock()

    def write(message: dict[str, Any]) -> None:
        line = dumps_line(message)
        with write_lock:
            stdout.write(line + "\n")
            stdout.flush()

    server = RpcServer(write)
    server.emit({"ready": True})
    for raw_line in iter(source.readline, b"" if stdin is None else ""):
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            server.reply(None, error="不是有效的 JSON")
            continue
        server.handle(message)
        if server.stopped.is_set():
            break
    job = server.job
    if job is not None:
        job.controller.cancel()
        for thread in list(server.threads):
            thread.join(timeout=3)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Argument parsing / entry point
# ---------------------------------------------------------------------------

def _common(parser: argparse.ArgumentParser, *, both: bool = False, with_file: bool = True) -> None:
    if with_file:
        parser.add_argument("file", help="书源 JSON / JSON5 文件")
    parser.add_argument("--source", default=None, help="书源 URL 或名称（先精确 URL → 规范化 URL → 精确名称 → 名称子串）")
    parser.add_argument("--index", type=int, default=None, help="书源在文件里的序号（从 0 开始）")
    parser.add_argument("--app-version", choices=("1.1.1", "1.1.2") + (("both",) if both else ()),
                        default=app_profiles.DEFAULT_APP_VERSION,
                        help="模拟的 App 版本（默认 1.1.1" + ("；both = 两版各跑一次" if both else "") + "）")
    parser.add_argument("--jsonl", action="store_true", help="stdout 每行输出一个 JSON 事件")
    parser.add_argument("--json", action="store_true", help="结束时输出一个摘要 JSON（单独使用时日志改走 stderr）")
    parser.add_argument("--block-private-network", action="store_true",
                        help="拒绝向私网 / 回环地址发请求（默认允许，方便调试本机站点）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="source_validator_cli.py",
        description="书源工作台：单步调试、规则测试、修复建议、单源校验与导出。批量验证请不带子命令直接使用 --input 等参数。",
        epilog="退出码：0 通过 / 有结果；1 失效或规则结果为空；2 用法或输入错误；3 需要登录/验证；4 超时；130 Ctrl+C。",
    )
    sub = parser.add_subparsers(dest="command", metavar="{" + ",".join(SUBCOMMANDS) + "}")

    debug = sub.add_parser("debug", help="调试一个书源（关键词 / 发现 / 详情 / ++目录 / --正文）")
    _common(debug)
    debug.add_argument("--key", default="", help="调试 key：关键词 | 标题::发现URL | 详情URL | ++目录URL | --正文URL；缺省 = 完整校验")
    debug.add_argument("--step", action="store_true", help="单步：在选书 / 选章处暂停（需要交互终端）")
    debug.add_argument("--pick-book", type=int, default=0, help="非单步时选第几本书（从 0 开始）")
    debug.add_argument("--pick-chapter", type=int, default=0, help="非单步时选第几章（从 0 开始）")
    debug.add_argument("--timeout", type=int, default=60, help="整个调试的硬截止秒数（默认 60）")
    debug.add_argument("--save-raw", metavar="DIR", default=None, help="把每个原始响应保存到目录（test-rule --raw 可重放）")

    test = sub.add_parser("test-rule", help="在保存的响应上重放一条规则")
    _common(test)
    test.add_argument("--field", required=True, help="字段路径，如 ruleSearch.name、ruleToc.chapterList")
    test.add_argument("--rule", default=None, help="要测试的规则；缺省用书源里现有的")
    test.add_argument("--body-file", default=None, help="响应内容文件（- = stdin）")
    test.add_argument("--raw", metavar="DIR", default=None, help="debug --save-raw 保存的目录")
    test.add_argument("--raw-id", type=int, default=None, help="--raw 目录里的响应序号；缺省取该字段阶段的最后一条成功响应")
    test.add_argument("--base-url", default=None, help="解析相对链接的基准 URL")
    test.add_argument("--item", type=int, default=0, help="列表字段取第几项（从 0 开始）")
    test.add_argument("--online", action="store_true", help="允许规则内 JS 联网（默认离线）")

    fix = sub.add_parser("fix", help="调试 → 建议 → 预览 → 应用 → 导出")
    _common(fix)
    fix.add_argument("--key", default="", help="调试 key（缺省 = 完整校验）")
    fix.add_argument("--timeout", type=int, default=60, help="调试硬截止秒数（默认 60）")
    fix.add_argument("--apply", default="", help="按代码应用建议，如 F5,F7（预览未通过的会被拒绝）")
    fix.add_argument("--apply-safe", action="store_true", help="应用全部离线且预览通过的建议")
    fix.add_argument("--online-preview", action="store_true", help="对需要联网的建议做在线预览（会重新请求站点）")
    fix.add_argument("--revalidate", action="store_true", help="应用后重新校验并对比修复前后")
    fix.add_argument("--keep-source-url", action="store_true", help="域名迁移时不改 bookSourceUrl（保持 App 里的书源主键）")
    fix.add_argument("--quick-timeout", type=int, default=8, help="重新校验的快速扫描秒数")
    fix.add_argument("--source-timeout", type=int, default=60, help="重新校验的完整验证秒数")
    fix.add_argument("--out", default=None, help="修复后书源输出文件；缺省 <输入>.fixed.json")
    fix.add_argument("--report", default=None, help="fix-report.json 路径；缺省与 --out 同目录")
    fix.add_argument("--dry-run", action="store_true", help="只显示将做的改动，不写任何文件")
    fix.add_argument("--show-log", action="store_true", help="同时打印调试日志")
    fix.add_argument("--save-raw", metavar="DIR", default=None, help="保存调试时的原始响应")

    one = sub.add_parser("validate-one", help="按批量流水线校验单个书源")
    _common(one, both=True)
    one.add_argument("--quick-timeout", type=int, default=8, help="快速扫描硬截止秒数（默认 8）")
    one.add_argument("--source-timeout", type=int, default=60, help="完整验证硬截止秒数（默认 60）")
    one.add_argument("--rounds", type=int, default=1, help="稳定复测总轮次（默认 1）")
    one.add_argument("--trace", action="store_true", help="打印调试事件")

    export = sub.add_parser("export", help="导出书源（可应用 fix-report.json 里的改动）")
    _common(export)
    export.add_argument("--from-report", default=None, help="fix-report.json；把里面的改动应用到对应书源再导出")
    export.add_argument("--only-changed", action="store_true", help="只导出有改动的书源")
    export.add_argument("--only-passed", action="store_true", help="只导出修复后通过的书源")
    export.add_argument("--keep-source-url", action="store_true", help="保持原 bookSourceUrl")
    export.add_argument("--out", default=None, help="输出文件；缺省 <输入>.export.json")

    sub.add_parser("rpc", help="JSON-lines 服务（供桌面工作台使用）")
    return parser


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "debug": cmd_debug,
    "test-rule": cmd_test_rule,
    "fix": cmd_fix,
    "validate-one": cmd_validate_one,
    "export": cmd_export,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else (EXIT_OK if exc.code is None else EXIT_USAGE)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if args.command == "rpc":
        return run_rpc()
    vsp.configure_stdio_encoding()
    try:
        return _COMMANDS[args.command](args)
    except CliError as exc:
        _err(str(exc))
        return exc.code
    except KeyboardInterrupt:
        _err("■ 已停止")
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
