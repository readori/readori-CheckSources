"""R146 offline rule replay: run one book-source rule (or one item field) against a saved response body.

The workbench saves every response of a debug run (``debug_engine.RawStore``).  ``test_rule`` re-evaluates a
single rule on such a body with the *same* helper functions the validation chain uses, so the answer matches what
a re-run would produce, without a network round trip and without disturbing the run.  By default the network is
blocked (``offline=True``): a rule whose JavaScript tries to fetch something gets an empty answer plus a warning.

Item fields (``ruleSearch.name``, ``ruleToc.chapterUrl`` ...) are tested on item ``item_index`` of the list the
source's *current* list rule produces, exactly like the chain does.
"""

from __future__ import annotations

import re
import time
import dataclasses
from dataclasses import asdict, dataclass
from typing import Any

try:  # package import (CLI / PyInstaller) or top-level import (scripts / tests)
    from . import app_profiles, debug_trace  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised only outside the package
    import app_profiles  # type: ignore[no-redef]
    import debug_trace  # type: ignore[no-redef]

MAX_VALUES = 50
MAX_VALUE_CHARS = 2000

# path -> (kind, list_field).  kind: list | string | url | urlList | content | replace | webjs
FIELD_KIND: dict[str, tuple[str, str | None]] = {}


def _register(prefix: str, list_field: str, strings: tuple[str, ...], urls: tuple[str, ...]) -> None:
    FIELD_KIND[f"{prefix}.bookList"] = ("list", None)
    for name in strings:
        FIELD_KIND[f"{prefix}.{name}"] = ("string", list_field)
    for name in urls:
        FIELD_KIND[f"{prefix}.{name}"] = ("url", list_field)


_register("ruleSearch", "ruleSearch.bookList", ("name", "author", "kind", "wordCount", "lastChapter", "intro"),
          ("coverUrl", "bookUrl"))
_register("ruleExplore", "ruleExplore.bookList", ("name", "author", "kind", "wordCount", "lastChapter", "intro"),
          ("coverUrl", "bookUrl"))
FIELD_KIND["ruleSearch.init"] = ("string", None)
for _name in ("init", "name", "author", "kind", "wordCount", "lastChapter", "intro"):
    FIELD_KIND[f"ruleBookInfo.{_name}"] = ("string", None)
for _name in ("tocUrl", "coverUrl"):
    FIELD_KIND[f"ruleBookInfo.{_name}"] = ("url", None)
FIELD_KIND["ruleToc.chapterList"] = ("list", None)
for _name in ("chapterName", "updateTime", "isVolume", "isVip", "isPay"):
    FIELD_KIND[f"ruleToc.{_name}"] = ("string", "ruleToc.chapterList")
FIELD_KIND["ruleToc.chapterUrl"] = ("url", "ruleToc.chapterList")
FIELD_KIND["ruleToc.nextTocUrl"] = ("urlList", None)
FIELD_KIND["ruleContent.content"] = ("content", None)
FIELD_KIND["ruleContent.title"] = ("string", None)
FIELD_KIND["ruleContent.nextContentUrl"] = ("urlList", None)
FIELD_KIND["ruleContent.replaceRegex"] = ("replace", None)
FIELD_KIND["ruleContent.webJs"] = ("webjs", None)

_STAGE_OF_RULE = {"ruleSearch": "search", "ruleExplore": "explore", "ruleBookInfo": "info", "ruleToc": "toc",
                  "ruleContent": "content"}
_PAGE_MARKUP_RE = re.compile(r"<\s*(?:html|body|head|div|p|span|ul|table|script)\b", re.I)


def stage_of_field(field_path: str) -> str:
    """Debug stage whose saved response a rule path is evaluated on (``ruleToc.x`` -> ``toc``)."""
    return _STAGE_OF_RULE.get(str(field_path).split(".", 1)[0], "")


@dataclass
class RuleTestResult:
    ok: bool = False
    field: str = ""
    rule: str = ""
    kind: str = ""
    values: list[str] = dataclasses.field(default_factory=list)
    value: str = ""
    count: int = 0
    items_count: int | None = None
    elapsed_ms: int = 0
    error: str = ""
    warnings: list[str] = dataclasses.field(default_factory=list)
    data: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _vsp() -> Any:
    try:
        from . import validate_source_packages as module  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover
        import validate_source_packages as module  # type: ignore[no-redef]
    return module


def get_path(obj: Any, path: str) -> Any:
    current = obj
    for part in str(path).split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _clip(value: str, limit: int = MAX_VALUE_CHARS) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def test_rule(
    src: dict[str, Any],
    field_path: str,
    *,
    body: str,
    base_url: str,
    rule: str | None = None,
    item_index: int = 0,
    app_version: str | None = None,
    variables: dict[str, Any] | None = None,
    offline: bool = True,
) -> RuleTestResult:
    """Replay ``rule`` (default: the source's rule at ``field_path``) on ``body``.  Never raises."""
    started = time.perf_counter()
    result = RuleTestResult(field=field_path)
    blocked: list[str] = []

    def collector(event: dict) -> None:
        if event.get("kind") == "blocked":
            blocked.append(str(event.get("url") or ""))

    try:
        version = app_version or app_profiles.current_app_version()
        with app_profiles.app_profile_scope(version):
            with debug_trace.tracing(collector, offline=offline):
                _run(src, field_path, body or "", base_url or "", rule, int(item_index or 0), variables, result)
    except Exception as exc:  # noqa: BLE001 - a broken rule must never break the caller
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
    if blocked:
        result.warnings.append("规则内 JS 发起了网络请求，离线重放已拦截")
        result.data["blockedRequests"] = blocked[:10]
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


def evaluate_field(
    src: dict[str, Any],
    field_path: str,
    *,
    body: str,
    base_url: str,
    rule: str | None = None,
    item_index: int = 0,
    variables: dict[str, Any] | None = None,
    app_version: str | None = None,
) -> RuleTestResult:
    """Offline evaluation of a field the validation chain itself does not read (debug engine 'extra' fields)."""
    return test_rule(src, field_path, body=body, base_url=base_url, rule=rule, item_index=item_index,
                     app_version=app_version, variables=variables, offline=True)


def test_rule_on_session(
    src: dict[str, Any],
    session: Any,
    field_path: str,
    *,
    rule: str | None = None,
    item_index: int = 0,
    app_version: str | None = None,
    online: bool = False,
) -> RuleTestResult:
    """Single-field test on the last saved response of the field's stage (what the workbench's '▶' button does)."""
    stage = stage_of_field(field_path)
    store = getattr(session, "raw", None) or getattr(session, "raw_store", None)
    entry = store.latest(stage) if store is not None else None
    if entry is None and stage == "explore" and store is not None:
        entry = store.latest("search")
    if entry is None:
        result = RuleTestResult(field=field_path, rule=_text(rule), error=f"没有保存 {stage or '该'} 阶段的响应，请先调试一次")
        return result
    variables = getattr(session, "variables", None)
    return test_rule(src, field_path, body=entry.body or "", base_url=entry.final_url or entry.url, rule=rule,
                     item_index=item_index, app_version=app_version, variables=variables, offline=not online)


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

def _run(src: dict[str, Any], field_path: str, body: str, base_url: str, rule: str | None, item_index: int,
         variables: dict[str, Any] | None, result: RuleTestResult) -> None:
    vsp = _vsp()
    spec = FIELD_KIND.get(field_path)
    if spec is None:
        result.error = f"不支持的字段: {field_path}"
        return
    kind, list_field = spec
    result.kind = kind
    view, decode_error = app_profiles.app_decode_view(src)
    if decode_error:
        result.error = f"App 导入会拒绝这条书源：{decode_error}"
        return
    rule_text = _text(rule) if rule is not None else _text(get_path(view, field_path))
    if field_path.startswith("ruleExplore.") and rule is None and not _text(get_path(view, "ruleExplore.bookList")).strip():
        # fetch_book_candidates: an explore section without bookList uses the whole ruleSearch section.
        rule_text = _text(get_path(view, field_path.replace("ruleExplore.", "ruleSearch.", 1)))
    result.rule = rule_text
    if not rule_text.strip():
        result.error = "规则为空"
        return

    base_runtime = vsp.make_runtime(view)
    stage_name = field_path.split(".", 1)[0]
    overrides: dict[str, Any] = {
        "source_url": base_url,
        "base_url": base_url,
        "variables": dict(variables or {}),
        "script_error_sink": [],
    }
    if stage_name in ("ruleBookInfo", "ruleToc", "ruleContent"):
        overrides["book_binding"] = "bound"
    if stage_name == "ruleBookInfo":
        overrides["book_url"] = base_url
    runtime = vsp.RuleRuntime(**{**base_runtime.__dict__, **overrides})
    ctype = "json" if vsp.is_json_content(body) else "html"

    if kind == "list":
        items = vsp.evaluate_book_list(rule_text, body, runtime)
        _finish_script_errors(runtime, result, bool(items))
        result.items_count = len(items)
        result.count = len(items)
        result.values = [_clip(_text(item)) for item in items[:MAX_VALUES]]
        result.value = result.values[0] if result.values else ""
        result.ok = bool(items)
        if not items:
            result.error = result.error or "列表规则没有匹配到任何项"
        return

    if list_field is not None:
        _run_item_field(vsp, view, runtime, field_path, kind, list_field, rule_text, body, base_url, item_index, result)
        return

    # Single-document fields.
    if stage_name == "ruleBookInfo" and field_path != "ruleBookInfo.init":
        body, ctype = _apply_info_init(vsp, view, runtime, body, ctype)
    if kind == "string":
        values = vsp.evaluate_rule(rule_text, body, runtime, ctype)
        _set_single(result, vsp.legado_get_string_value(values))
    elif kind == "url":
        values = vsp.evaluate_rule(rule_text, body, runtime, ctype)
        value = vsp.legado_get_string_value(values).strip()
        _set_single(result, vsp.resolve_url_preserving_options(value, base_url) if value else "")
    elif kind == "urlList":
        raw_values = vsp.legado_string_list_values(vsp.evaluate_rule(rule_text, body, runtime, ctype))
        urls = [vsp.resolve_url_preserving_options(value, base_url) for value in raw_values]
        result.values = [_clip(value) for value in urls[:MAX_VALUES]]
        result.value = result.values[0] if result.values else ""
        result.count = len(urls)
        result.ok = bool(urls)
    elif kind == "content":
        text = vsp.evaluate_content_rule_with_inline_replace_guard(rule_text, body, runtime, ctype)
        result.values = [_clip(text)] if text else []
        result.value = result.values[0] if result.values else ""
        result.count = len(text)
        result.ok = bool(text.strip())
        result.data["chars"] = len(text)
    elif kind == "replace":
        text = _text_for_replace(vsp, view, runtime, body, ctype, result)
        before, after = vsp.apply_legado_content_replace_regex(text, rule_text, runtime)
        after = after.strip()
        result.values = [_clip(after)] if after else []
        result.value = result.values[0] if result.values else ""
        result.count = len(after)
        result.ok = bool(after)
        result.data.update(before=len(before), after=len(after))
        if after == before.strip():
            result.warnings.append("净化规则没有改变正文")
    elif kind == "webjs":
        values = vsp.evaluate_rule(rule_text, body, runtime, ctype)
        _set_single(result, values[0] if values else "")
    _finish_script_errors(runtime, result, result.ok)
    if not result.ok and not result.error:
        result.error = "规则没有返回结果"


def _set_single(result: RuleTestResult, value: str) -> None:
    value = _text(value)
    result.values = [_clip(value)] if value.strip() else []
    result.value = result.values[0] if result.values else ""
    result.count = 1 if result.values else 0
    result.ok = bool(result.values)


def _finish_script_errors(runtime: Any, result: RuleTestResult, produced: bool) -> None:
    sink = getattr(runtime, "script_error_sink", None) or []
    errors = [str(entry.get("message") or "") for entry in sink if entry.get("kind") == "error"]
    compat = [str(entry.get("message") or "") for entry in sink if entry.get("kind") == "compat"]
    if errors:
        if produced:
            result.warnings.append(f"规则脚本报错：{errors[0]}")
        else:
            result.error = f"js失效：{errors[0]}"
    for message in dict.fromkeys(compat):
        result.warnings.append(f"工具兼容层缺口（不是书源的错）：{message}")


def _apply_info_init(vsp: Any, view: dict[str, Any], runtime: Any, body: str, ctype: str) -> tuple[str, str]:
    """ruleBookInfo.init replaces the body the other detail fields see (fetch_book_info)."""
    info = view.get("ruleBookInfo") if isinstance(view.get("ruleBookInfo"), dict) else {}
    init_rule = _text(info.get("init") or info.get("initJs")).strip()
    if not init_rule:
        return body, ctype
    transformed = vsp.evaluate_rule(init_rule, body, runtime, ctype)
    if transformed:
        body = transformed[0]
        ctype = "json" if vsp.is_json_content(body) else "html"
    return body, ctype


def _text_for_replace(vsp: Any, view: dict[str, Any], runtime: Any, body: str, ctype: str, result: RuleTestResult) -> str:
    """replaceRegex works on the *merged content text*.  A saved page (HTML / JSON) is turned into that text first."""
    content_rule = _text(get_path(view, "ruleContent.content")).strip()
    looks_like_page = ctype == "json" or bool(_PAGE_MARKUP_RE.search(body[:5000]))
    if content_rule and looks_like_page:
        text = vsp.evaluate_content_rule_with_inline_replace_guard(content_rule, body, runtime, ctype)
        result.data["preparedWithContentRule"] = True
        return text
    return body


def _item_runtime(vsp: Any, runtime: Any, stage_name: str, item_index: int, count: int) -> Any:
    overrides: dict[str, Any] = {"variables": dict(runtime.variables or {})}
    if stage_name == "ruleToc":
        overrides.update(chapter_url="", chapter_title="", chapter_active=True, chapter_index=item_index,
                         chapter_count=count)
    return vsp.RuleRuntime(**{**runtime.__dict__, **overrides})


def _run_item_field(vsp: Any, view: dict[str, Any], runtime: Any, field_path: str, kind: str, list_field: str,
                    rule_text: str, body: str, base_url: str, item_index: int, result: RuleTestResult) -> None:
    list_rule = _text(get_path(view, list_field)).strip()
    if not list_rule and list_field.startswith("ruleExplore"):
        list_rule = _text(get_path(view, "ruleSearch.bookList")).strip()
    if not list_rule:
        result.error = f"{list_field} 为空，无法先取出列表项"
        return
    items = vsp.evaluate_book_list(list_rule, body, runtime)
    result.items_count = len(items)
    if not items:
        result.error = "列表规则没有匹配到任何项"
        return
    if item_index < 0 or item_index >= len(items):
        result.error = f"列表只有 {len(items)} 项"
        return
    item = items[item_index]
    stage_name = field_path.split(".", 1)[0]
    item_runtime = _item_runtime(vsp, runtime, stage_name, item_index, len(items))
    item_type = "json" if vsp.is_json_content(item) else ("json" if vsp.is_json_content(body) else "html")
    if stage_name in ("ruleSearch", "ruleExplore"):
        if field_path.endswith(".bookUrl"):
            kind_rule = _text(get_path(view, field_path.rsplit(".", 1)[0] + ".kind"))
            value = vsp.first_result_to_book_url(view, item, rule_text, item_runtime, kind_rule)
        else:
            value = vsp.first_result_to_text(item, rule_text, item_runtime)
            if kind == "url" and value.strip():
                value = vsp.resolve_url_preserving_options(value.strip(), base_url)
    else:  # ruleToc
        values = vsp.evaluate_rule(rule_text, item, item_runtime, item_type)
        if kind == "url":
            raw = values[0] if values and str(values[0]).strip() else ""
            if raw and vsp.contains_json_placeholder(raw):
                raw = vsp.substitute_json_placeholders(raw, item)
            value = vsp.resolve_url_preserving_options(raw, base_url) if raw.strip() else ""
        else:
            value = vsp.legado_get_string_value(values).strip()
    _set_single(result, value)
    result.data["itemIndex"] = item_index
    _finish_script_errors(runtime, result, result.ok)  # the sink list is shared with every child runtime
    if not result.ok and not result.error:
        result.error = "规则在这一项上没有返回结果"
