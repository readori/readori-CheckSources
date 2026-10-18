"""R146 WP-B: repair suggestions for a debugged book source.

``suggest(src, session)`` looks at what a debug run recorded (events + raw responses) and returns
a short list of ``Suggestion`` objects, each carrying a concrete ``patch`` that
``source_edit.SourceDocument.apply`` can apply.  ``preview`` proves a suggestion before it is
applied: offline (replay the saved response through the rule engine) or online (re-run the
debugger with the patched source).

Rules (see R146 spec section 4)::

    F1  domain migration / http->https     F5  lazy-loaded cover (data-src)
    F2  GBK / Big5 search charset          F6  missing nextTocUrl (paged TOC)
    F3  missing User-Agent / Referer       F7  ad-text clean-up (replaceRegex)
    F4  empty list -> candidate selector   F8  Cloudflare / human check (never auto-fixed)

Mutual exclusion: F8 stops F3/F4 (and F2) for the stage it hit; F1 stops F2/F3.

The session is duck-typed (``DebugSession`` of ``debug_engine`` is built by another work
package): ``session.events`` is a list of event dicts, ``session.raw`` a ``RawStore`` with
``get(raw_id)`` / ``latest(stage)`` / ``entries``.  Event fields may sit at the top level or in
``event["data"]``; raw entries may be objects or dicts.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal
from urllib.parse import quote, quote_plus, urljoin, urlsplit

try:  # package import (CLI / PyInstaller) or top-level import (scripts / tests)
    from . import app_profiles, source_edit  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised only outside the package
    import app_profiles  # type: ignore[no-redef]
    import source_edit  # type: ignore[no-redef]

log = logging.getLogger(__name__)

DEFAULT_APP_VERSION = app_profiles.DEFAULT_APP_VERSION
ONLINE_PREVIEW_TIMEOUT = 45
MAX_DELETE_RATIO = 0.30  # F7: refuse to apply when the clean-up removes more than 30% of the text
REDIRECT_STATUSES = (301, 302, 307, 308)
_ERROR_STATUSES = (401, 403, 503)
_CHARSET_FIXES = {"gbk": "gbk", "gb2312": "gbk", "gb18030": "gbk", "big5": "big5"}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Suggestion:
    id: str
    code: str
    title: str
    reason: str
    stage: str
    confidence: Literal["high", "medium", "low"]
    patch: list[dict]  # [{"op":"set","path":...,"old":...,"new":...}]; empty = hint only
    preview_kind: Literal["offline", "online", "none"]
    apply_allowed: bool
    meta: dict[str, Any] = field(default_factory=dict)  # evidence / alternatives for UIs (extra, optional)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "id": self.id, "code": self.code, "title": self.title, "reason": self.reason,
                "stage": self.stage, "confidence": self.confidence, "patch": self.patch,
                "preview_kind": self.preview_kind, "apply_allowed": self.apply_allowed, "meta": self.meta,
            }
        )


# ---------------------------------------------------------------------------
# Lazy module access
# ---------------------------------------------------------------------------

_VSP: Any = None


def _vsp() -> Any:
    global _VSP
    if _VSP is None:
        try:
            from . import validate_source_packages as module  # type: ignore[attr-defined]
        except ImportError:  # pragma: no cover
            import validate_source_packages as module  # type: ignore[no-redef]
        _VSP = module
    return _VSP


def _load_rule_tester() -> Any | None:
    """WP-A ``rule_tester`` when it exists and exposes ``test_rule``; ``None`` -> built-in fallback."""
    if not USE_RULE_TESTER:
        return None
    try:
        from . import rule_tester  # type: ignore[attr-defined]
    except ImportError:
        try:
            import rule_tester  # type: ignore[no-redef]
        except ImportError:
            return None
    return rule_tester if callable(getattr(rule_tester, "test_rule", None)) else None


def _load_debug_engine() -> Any | None:
    try:
        from . import debug_engine  # type: ignore[attr-defined]
    except ImportError:
        try:
            import debug_engine  # type: ignore[no-redef]
        except ImportError:
            return None
    return debug_engine if callable(getattr(debug_engine, "debug_source", None)) else None


USE_RULE_TESTER = True  # tests flip this to exercise the built-in fallback


# ---------------------------------------------------------------------------
# Session / event / raw adapters (duck typing)
# ---------------------------------------------------------------------------

def _g(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _ev(event: dict, name: str, default: Any = None) -> Any:
    value = event.get(name)
    if value is None and isinstance(event.get("data"), dict):
        value = event["data"].get(name)
    return default if value is None else value


def _session_events(session: Any) -> list[dict]:
    events = _g(session, "events")
    if callable(events):
        events = events()
    if events is None and isinstance(session, (list, tuple)):
        events = session
    return [e for e in (events or []) if isinstance(e, dict)]


def _raw_store(session: Any) -> Any:
    for name in ("raw", "raw_store", "rawStore"):
        store = _g(session, name)
        if store is not None:
            return store
    return None


def _raw_entries(session: Any) -> list[Any]:
    store = _raw_store(session)
    if store is None:
        return []
    entries = _g(store, "entries")
    if callable(entries):
        entries = entries()
    if isinstance(entries, dict):
        return list(entries.values())
    if entries is not None:
        return list(entries)
    if isinstance(store, dict):
        return list(store.values())
    try:
        return list(store)
    except TypeError:
        return []


def _raw_get(session: Any, raw_id: Any) -> Any:
    if raw_id is None:
        return None
    store = _raw_store(session)
    getter = getattr(store, "get", None) if store is not None and not isinstance(store, dict) else None
    if callable(getter):
        try:
            found = getter(raw_id)
        except Exception:  # noqa: BLE001 - unknown RawStore flavour, fall through to a scan
            found = None
        if found is not None:
            return found
    if isinstance(store, dict) and raw_id in store:
        return store[raw_id]
    for entry in _raw_entries(session):
        if _g(entry, "raw_id") == raw_id:
            return entry
    return None


def _raw_latest(session: Any, stage: str) -> Any:
    store = _raw_store(session)
    latest = getattr(store, "latest", None) if store is not None and not isinstance(store, dict) else None
    if callable(latest):
        try:
            found = latest(stage)
        except Exception:  # noqa: BLE001
            found = None
        if found is not None:
            return found
    best = None
    for entry in _raw_entries(session):
        if _g(entry, "stage") != stage:
            continue
        status = _g(entry, "status")
        body = _g(entry, "body")
        if isinstance(status, int) and 200 <= status < 400 and body:
            best = entry
    return best


def _parse_history(value: Any) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for item in value or []:
        try:
            if isinstance(item, dict):
                out.append((int(item.get("status") or item.get("status_code") or 0), str(item.get("url") or "")))
            else:
                out.append((int(item[0]), str(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _lower_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items()}


@dataclass
class _Fetch:
    seq: int
    stage: str
    url: str
    method: str
    attempt: Any
    status: int
    final_url: str
    history: list[tuple[int, str]]
    req_headers: dict[str, str]
    resp_headers: dict[str, str]
    charset: str
    req_body: str
    raw_id: Any
    raw: Any
    body: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _collect_fetches(session: Any, events: list[dict]) -> list[_Fetch]:
    out: list[_Fetch] = []
    for index, event in enumerate(events):
        if event.get("kind") != "fetch":
            continue
        raw_id = _ev(event, "raw_id")
        raw = _raw_get(session, raw_id)
        body = _first(_ev(event, "body"), _g(raw, "body")) or ""
        try:
            status = int(_first(_ev(event, "status"), _g(raw, "status")) or 0)
        except (TypeError, ValueError):
            status = 0
        url = str(_first(_ev(event, "url"), _g(raw, "url")) or "")
        out.append(
            _Fetch(
                seq=int(event.get("seq") if isinstance(event.get("seq"), int) else index),
                stage=str(event.get("stage") or _g(raw, "stage") or ""),
                url=url,
                method=str(_first(_ev(event, "method"), _g(raw, "method")) or "GET").upper(),
                attempt=_first(_ev(event, "attempt"), 0),
                status=status,
                final_url=str(_first(_ev(event, "final_url"), _g(raw, "final_url"), url) or ""),
                history=_parse_history(_first(_ev(event, "history"), _g(raw, "history"))),
                req_headers=_lower_headers(_first(_ev(event, "request_headers"), _g(raw, "request_headers"))),
                resp_headers=_lower_headers(_first(_ev(event, "response_headers"), _g(raw, "response_headers"))),
                charset=str(_first(_ev(event, "charset"), _g(raw, "charset")) or ""),
                req_body=str(_first(_ev(event, "request_body"), _g(raw, "request_body")) or ""),
                raw_id=raw_id,
                raw=raw,
                body=body if isinstance(body, str) else "",
            )
        )
    return out


class _Ctx:
    """Everything the rules look at, computed once per ``suggest`` call."""

    def __init__(self, src: dict[str, Any], session: Any, app_version: str | None, keep_source_url: bool) -> None:
        self.src = src
        self.session = session
        self.app_version = app_version or app_profiles.current_app_version()
        self.keep_source_url = keep_source_url
        self.events = _session_events(session)
        self.fetches = _collect_fetches(session, self.events)
        self._runtime: Any = None

    def rule(self, path: str) -> str:
        value = source_edit.get_path(self.src, path)
        return value if isinstance(value, str) else ""

    def raw_value(self, path: str) -> Any:
        return source_edit.get_path(self.src, path)

    def latest_body(self, stage: str) -> tuple[str, str, Any]:
        """``(body, final_url, entry)`` of the last successful response of ``stage``."""
        entry = _raw_latest(self.session, stage)
        if entry is None:
            return "", "", None
        body = _g(entry, "body")
        final_url = _g(entry, "final_url") or _g(entry, "url") or ""
        return (body if isinstance(body, str) else ""), str(final_url), entry

    def runtime(self) -> Any:
        if self._runtime is None:
            vsp = _vsp()
            view, _error = app_profiles.app_decode_view(self.src)
            self._runtime = vsp.make_runtime(view)
        return self._runtime

    def events_of(self, kind: str, stage: str | None = None, field_name: str | None = None) -> list[dict]:
        out = []
        for event in self.events:
            if event.get("kind") != kind:
                continue
            if stage is not None and event.get("stage") != stage:
                continue
            if field_name is not None and _ev(event, "field") != field_name:
                continue
            out.append(event)
        return out

    def variable(self, name: str) -> str:
        for holder in (_g(self.session, "variables"), _g(self.session, "runtime_variables")):
            if isinstance(holder, dict) and name in holder:
                return str(holder[name])
        return ""


# ---------------------------------------------------------------------------
# Offline rule evaluation (WP-A rule_tester when present, else a small built-in replay)
# ---------------------------------------------------------------------------

_LIST_PATHS = {"ruleSearch.bookList", "ruleExplore.bookList", "ruleToc.chapterList"}
_ITEM_FIELDS = {
    "ruleSearch.name": ("string", "ruleSearch.bookList"),
    "ruleSearch.author": ("string", "ruleSearch.bookList"),
    "ruleSearch.coverUrl": ("url", "ruleSearch.bookList"),
    "ruleSearch.bookUrl": ("url", "ruleSearch.bookList"),
    "ruleExplore.name": ("string", "ruleExplore.bookList"),
    "ruleExplore.author": ("string", "ruleExplore.bookList"),
    "ruleExplore.coverUrl": ("url", "ruleExplore.bookList"),
    "ruleExplore.bookUrl": ("url", "ruleExplore.bookList"),
    "ruleToc.chapterName": ("string", "ruleToc.chapterList"),
    "ruleToc.chapterUrl": ("url", "ruleToc.chapterList"),
}
_PLAIN_FIELDS = {
    "ruleBookInfo.coverUrl": "url",
    "ruleBookInfo.tocUrl": "url",
    "ruleBookInfo.name": "string",
    "ruleToc.nextTocUrl": "urlList",
    "ruleContent.content": "content",
    "ruleContent.title": "string",
    "ruleContent.nextContentUrl": "urlList",
    "ruleContent.replaceRegex": "replace",
}


def _result_dict(result: Any) -> dict[str, Any]:
    values = _g(result, "values") or []
    return {
        "ok": bool(_g(result, "ok", False)),
        "field": _g(result, "field", ""),
        "rule": _g(result, "rule", ""),
        "kind": _g(result, "kind", ""),
        "values": [str(v) for v in values][:50],
        "value": _g(result, "value", values[0] if values else ""),
        "count": _g(result, "count", len(values)),
        "items_count": _g(result, "items_count"),
        "elapsed_ms": _g(result, "elapsed_ms"),
        "error": _g(result, "error", "") or "",
        "warnings": list(_g(result, "warnings") or []),
        "data": _g(result, "data") or {},
    }


def _fallback_rule_test(src: dict[str, Any], field_path: str, *, body: str, base_url: str, rule: str | None,
                        item_index: int, variables: dict[str, str] | None) -> dict[str, Any]:
    vsp = _vsp()
    view, _error = app_profiles.app_decode_view(src)
    runtime = vsp.make_runtime(view)
    runtime = vsp.RuleRuntime(**{**runtime.__dict__, "source_url": base_url, "base_url": base_url,
                                 "variables": dict(variables or {})})
    ctype = "json" if vsp.is_json_content(body) else "html"
    chosen = rule if rule is not None else (source_edit.get_path(view, field_path) or "")
    out: dict[str, Any] = {"ok": False, "field": field_path, "rule": chosen, "kind": "", "values": [], "value": "",
                           "count": 0, "items_count": None, "elapsed_ms": None, "error": "", "warnings": [], "data": {}}
    if not str(chosen).strip():
        out["error"] = "规则为空"
        return out
    if field_path in _LIST_PATHS:
        out["kind"] = "list"
        items = vsp.evaluate_book_list(chosen, body, runtime)
        out.update(ok=bool(items), values=[str(i) for i in items[:50]], count=len(items), items_count=len(items),
                   value=str(items[0]) if items else "")
        return out
    if field_path in _ITEM_FIELDS:
        kind, list_field = _ITEM_FIELDS[field_path]
        out["kind"] = kind
        list_rule = source_edit.get_path(view, list_field) or ""
        if not list_rule and list_field.startswith("ruleExplore"):
            list_rule = source_edit.get_path(view, "ruleSearch.bookList") or ""
        items = vsp.evaluate_book_list(list_rule, body, runtime) if list_rule else []
        out["items_count"] = len(items)
        if item_index >= len(items):
            out["error"] = f"列表只有 {len(items)} 项"
            return out
        value = vsp.legado_get_string_value(vsp.evaluate_rule(chosen, items[item_index], runtime, ctype))
        if kind == "url" and value:
            value = vsp.resolve_url_preserving_options(value, base_url)
        out.update(ok=bool(value), values=[value] if value else [], value=value, count=1 if value else 0)
        return out
    kind = _PLAIN_FIELDS.get(field_path)
    if kind is None:
        out["error"] = f"不支持的字段: {field_path}"
        return out
    out["kind"] = kind
    if kind == "urlList":
        raw_values = vsp.legado_string_list_values(vsp.evaluate_rule(chosen, body, runtime, ctype))
        values = [vsp.resolve_url_preserving_options(v, base_url) for v in raw_values]
        out.update(ok=bool(values), values=values[:50], value=values[0] if values else "", count=len(values))
    elif kind == "content":
        text = vsp.evaluate_content_rule_with_inline_replace_guard(chosen, body, runtime, ctype)
        out.update(ok=bool(text.strip()), values=[text], value=text, count=len(text))
    elif kind == "replace":
        before, after = vsp.apply_legado_content_replace_regex(body, chosen, runtime)
        out.update(ok=bool(after.strip()), values=[after], value=after, count=len(after),
                   data={"before": len(before), "after": len(after)})
    else:
        value = vsp.legado_get_string_value(vsp.evaluate_rule(chosen, body, runtime, ctype))
        if kind == "url" and value:
            value = vsp.resolve_url_preserving_options(value, base_url)
        out.update(ok=bool(value), values=[value] if value else [], value=value, count=1 if value else 0)
    return out


def _run_rule(src: dict[str, Any], field_path: str, *, body: str, base_url: str, rule: str | None = None,
              item_index: int = 0, app_version: str | None = None,
              variables: dict[str, str] | None = None) -> dict[str, Any]:
    """Offline replay of one rule on a saved response; never raises, never touches the network."""
    version = app_version or app_profiles.current_app_version()
    try:
        with app_profiles.app_profile_scope(version):
            tester = _load_rule_tester()
            if tester is not None:
                result = tester.test_rule(src, field_path, body=body, base_url=base_url, rule=rule,
                                          item_index=item_index, app_version=version, variables=variables,
                                          offline=True)
                return _result_dict(result)
            return _fallback_rule_test(src, field_path, body=body, base_url=base_url, rule=rule,
                                       item_index=item_index, variables=variables)
    except Exception as exc:  # noqa: BLE001 - a broken rule must not break the suggester
        return {"ok": False, "field": field_path, "rule": rule or "", "kind": "", "values": [], "value": "",
                "count": 0, "items_count": None, "elapsed_ms": None, "error": f"{type(exc).__name__}: {exc}",
                "warnings": [], "data": {}}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_TWO_LABEL_SUFFIXES = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "com.hk", "com.tw", "org.tw", "net.tw", "idv.tw",
    "co.uk", "org.uk", "co.jp", "com.au", "com.sg", "co.kr", "com.my", "com.br", "co.in", "com.mo",
}


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _registered_domain(host: str) -> str:
    host = (host or "").lower().strip(".")
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    tail2 = ".".join(labels[-2:])
    if tail2 in _TWO_LABEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return tail2


def _bare_host(host: str) -> str:
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            return host[len(prefix):]
    return host


def _rule_syntax_is_js(rule: str) -> bool:
    low = (rule or "").lstrip().lower()
    return low.startswith("@js:") or "<js>" in low


def _visible_text_length(body: str) -> int:
    if not body:
        return 0
    stripped = body.lstrip()
    if stripped[:1] in "{[":
        return len(body)
    from bs4 import BeautifulSoup  # lazy: only F4 needs it

    try:
        soup = BeautifulSoup(body, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return len(re.sub(r"\s+", "", soup.get_text(" ", strip=True)))
    except Exception:  # noqa: BLE001
        return len(body)


def _soup(body: str) -> Any:
    from bs4 import BeautifulSoup

    return BeautifulSoup(body or "", "html.parser")


def _replay_runtime(ctx: _Ctx, base_url: str) -> Any:
    vsp = _vsp()
    runtime = ctx.runtime()
    return vsp.RuleRuntime(**{**runtime.__dict__, "source_url": base_url, "base_url": base_url,
                              "variables": dict(runtime.variables or {})})


def _eval_list(ctx: _Ctx, rule: str, body: str, base_url: str) -> list[str]:
    """Items of a list rule on a saved response (direct engine call, full item text, no network)."""
    vsp = _vsp()
    with app_profiles.app_profile_scope(ctx.app_version):
        return list(vsp.evaluate_book_list(rule, body, _replay_runtime(ctx, base_url)))


def _eval_on_item(ctx: _Ctx, rule: str, item: str, base_url: str) -> str:
    vsp = _vsp()
    ctype = "json" if vsp.is_json_content(item) else "html"
    with app_profiles.app_profile_scope(ctx.app_version):
        values = vsp.evaluate_rule(rule, item, _replay_runtime(ctx, base_url), ctype)
        return vsp.legado_get_string_value(values).strip()


def _source_root(src: dict[str, Any]) -> str:
    url = str(src.get("bookSourceUrl") or "")
    match = re.search(r"https?://[^/\s#,]+", url)
    return f"{match.group(0)}/" if match else ""


def _summarise_events(events: list[dict]) -> dict[str, Any]:
    book_count: int | None = None
    outcome: str | None = None
    errors: list[str] = []
    for event in events:
        kind = event.get("kind")
        if kind == "list" and _ev(event, "field") == "bookList":
            count = _ev(event, "count")
            if isinstance(count, (int, float)):
                book_count = max(book_count or 0, int(count))
        if event.get("state") == 1000:
            outcome = "pass"
        elif event.get("state") == -1 and outcome != "pass":
            outcome = "fail"
        if kind in ("error", "fetch_error"):
            errors.append(str(_ev(event, "value") or _ev(event, "error") or "")[:200])
    return {"bookListCount": book_count, "outcome": outcome, "errors": errors[:5]}


# ---------------------------------------------------------------------------
# F8 -- Cloudflare / human verification (computed first: it suppresses F3/F4)
# ---------------------------------------------------------------------------

_CF_SIGNS = ("just a moment", "cf-browser-verification", "cf_chl_", "challenge-platform", "cf-challenge-running",
             "attention required! | cloudflare", "_cf_chl_opt")


def _f8(ctx: _Ctx) -> tuple[list[Suggestion], set[str]]:
    stages: list[str] = []
    for f in ctx.fetches:
        if f.status not in (403, 429, 503):
            continue
        has_cf = "cloudflare" in f.resp_headers.get("server", "").lower() or "cf-ray" in f.resp_headers
        low = f.body.lower()
        if has_cf and any(sign in low for sign in _CF_SIGNS):
            if f.stage not in stages:
                stages.append(f.stage)
    by_variable = ctx.variable("__searchChallenge") == "1"
    challenge_events = [e for e in ctx.events if _ev(e, "code") == "challenge"]
    if not stages:
        for event in challenge_events:
            stage = str(event.get("stage") or "search")
            if stage not in stages:
                stages.append(stage)
        if by_variable and not stages:
            stages.append("search")
    if not stages:
        return [], set()
    strong = any(
        f.status in (403, 429, 503) and ("cloudflare" in f.resp_headers.get("server", "").lower()
                                         or "cf-ray" in f.resp_headers)
        for f in ctx.fetches
    )
    suggestion = Suggestion(
        id="F8", code="F8", title="站点受 Cloudflare / 人机验证保护",
        reason="响应为 Cloudflare 质询页（403/429/503 + cf-ray / 验证页特征），或书源脚本标记了 __searchChallenge。"
               "规则无法绕过，需在 App 内用网页过盾（需交互）；批量验证会把它判为「需要登录/验证」。",
        stage=stages[0], confidence="high" if strong else "medium", patch=[], preview_kind="none",
        apply_allowed=False, meta={"stages": list(stages)},
    )
    return [suggestion], set(stages)


# ---------------------------------------------------------------------------
# F1 -- domain migration / http -> https
# ---------------------------------------------------------------------------

_ROOT_INDEX_PATHS = {"", "index.html", "index.htm", "index.php", "default.html", "default.aspx", "wap", "m",
                     "mobile", "wap/index.php", "wap/index.html"}
_DEAD_HOST_SIGNS = ("nameresolutionerror", "getaddrinfo", "name or service not known", "failed to resolve",
                    "nodename nor servname", "connecttimeout", "connectionerror", "max retries exceeded",
                    "connection refused", "no route to host", "actively refused")


def _is_landing_redirect(f: _Fetch, ctx: _Ctx) -> bool:
    """The redirect target is just a home page (parked / dead domain), not the page that was asked for."""
    try:
        requested = urlsplit(f.url)
        final = urlsplit(f.final_url)
    except ValueError:
        return True
    final_path = final.path.strip("/").lower()
    requested_path = requested.path.strip("/").lower()
    if final_path in _ROOT_INDEX_PATHS and requested_path not in _ROOT_INDEX_PATHS and not final.query:
        return True
    try:
        return bool(_vsp().is_likely_search_or_explore_landing_url(f.final_url, ctx.runtime()))
    except Exception:  # noqa: BLE001
        return False


def _replace_origin_text(text: str, old_netloc: str, new_origin: str, *, scheme_only: tuple[str, str] | None) -> str:
    """Rewrite ``scheme://old`` (and protocol-relative ``//old``) to the new origin inside one string."""
    port_guard = "" if ":" in old_netloc else r"(?!:\d)"
    boundary = r"(?![A-Za-z0-9-])(?!\.[A-Za-z0-9])" + port_guard
    escaped = re.escape(old_netloc)
    if scheme_only is not None:  # F1b: same host, only the scheme changes
        old_scheme, new_scheme = scheme_only
        return re.sub(rf"{re.escape(old_scheme)}://{escaped}{boundary}", f"{new_scheme}://{old_netloc}", text,
                      flags=re.I)
    new_netloc = urlsplit(new_origin).netloc
    text = re.sub(rf"https?://{escaped}{boundary}", new_origin, text, flags=re.I)
    return re.sub(rf"(?<!:)//{escaped}{boundary}", f"//{new_netloc}", text, flags=re.I)


def _replace_deep(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_replace_deep(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _replace_deep(v, fn) for k, v in value.items()}
    return value


def _string_patch(src: dict[str, Any], fn: Callable[[str], str], *, skip_paths: set[str]) -> list[dict]:
    """One ``set`` op per field whose strings change (dicts are walked, lists are replaced whole)."""
    ops: list[dict] = []

    def walk(obj: Any, prefix: str) -> None:
        for key, value in obj.items():
            if not isinstance(key, str) or "." in key or key == source_edit.INTERNAL_KEY:
                continue
            path = f"{prefix}.{key}" if prefix else key
            if path in skip_paths:
                continue
            if isinstance(value, dict):
                walk(value, path)
                continue
            new = _replace_deep(value, fn)
            if new != value:
                ops.append({"op": "set", "path": path, "old": copy.deepcopy(value), "new": new})

    walk(src, "")
    return ops


def _f1(ctx: _Ctx) -> list[Suggestion]:
    pairs: dict[tuple[str, str, str, str], list[_Fetch]] = {}
    for f in ctx.fetches:
        if not f.history or not any(status in REDIRECT_STATUSES for status, _url in f.history):
            continue
        if f.status != 200 or not f.url or not f.final_url:
            continue
        try:
            req, fin = urlsplit(f.url), urlsplit(f.final_url)
        except ValueError:
            continue
        old_host, new_host = (req.hostname or "").lower(), (fin.hostname or "").lower()
        if not old_host or not new_host:
            continue
        if _registered_domain(old_host) != _registered_domain(new_host):
            kind = "F1"
        elif old_host == new_host and req.scheme.lower() == "http" and fin.scheme.lower() == "https":
            kind = "F1b"
        else:
            continue  # www./m. prefix, other sub-domain, or same-scheme -- not a migration
        if _is_landing_redirect(f, ctx):
            continue
        pairs.setdefault((kind, req.netloc.lower(), fin.netloc.lower(), fin.scheme.lower()), []).append(f)

    suggestions: list[Suggestion] = []
    skip = {"bookSourceUrl"} if ctx.keep_source_url else set()
    ranked = sorted(pairs.items(), key=lambda item: (item[0][0] != "F1", -len(item[1])))
    seen_codes: dict[str, int] = {}
    for (kind, old_netloc, new_netloc, new_scheme), fetches in ranked:
        seen_codes[kind] = seen_codes.get(kind, 0) + 1
        suffix = "" if seen_codes[kind] == 1 else f"-{seen_codes[kind]}"
        confidence = "high" if len(fetches) >= 2 else "medium"
        new_origin = f"{new_scheme}://{new_netloc}"
        if kind == "F1":
            fn = lambda text, o=old_netloc, n=new_origin: _replace_origin_text(text, o, n, scheme_only=None)  # noqa: E731
            title = f"域名迁移：{old_netloc} → {new_netloc}"
            reason = (f"{len(fetches)} 次请求被 301/302/307/308 永久跳转到另一个站点 {new_origin}，"
                      "把书源里的旧域名统一换成新域名。注意：bookSourceUrl 是书源主键，改了之后导入 App 会成为新书源"
                      "（旧源仍保留）；不想改主键可选「保持书源地址不变」。")
        else:
            fn = lambda text, o=old_netloc: _replace_origin_text(text, o, "", scheme_only=("http", "https"))  # noqa: E731
            title = f"http 升级为 https：{old_netloc}"
            reason = f"{len(fetches)} 次 http 请求被跳转到 https，直接改用 https 可省一次跳转并避免被中途篡改。"
        patch = _string_patch(ctx.src, fn, skip_paths=skip)
        first = fetches[0]
        suggestions.append(
            Suggestion(
                id=f"{kind}{suffix}", code=kind, title=title, reason=reason, stage=first.stage or "search",
                confidence=confidence, patch=patch, preview_kind="online", apply_allowed=bool(patch),
                meta={"old_netloc": old_netloc, "new_origin": new_origin, "requests": len(fetches),
                      "keep_source_url": ctx.keep_source_url,
                      "fields": [op["path"] for op in patch],
                      "changes_primary_key": any(op["path"] == "bookSourceUrl" for op in patch)},
            )
        )
    if not any(s.code == "F1" for s in suggestions):
        hint = _f1_dead_host_hint(ctx)
        if hint is not None:
            suggestions.append(hint)
    return suggestions


def _f1_dead_host_hint(ctx: _Ctx) -> Suggestion | None:
    ok_hosts = {_host_of(f.url) for f in ctx.fetches if f.ok}
    for event in ctx.events:
        if event.get("kind") != "fetch_error":
            continue
        error = str(_ev(event, "error") or "").lower()
        host = _host_of(str(_ev(event, "url") or ""))
        if host and host not in ok_hosts and any(sign in error for sign in _DEAD_HOST_SIGNS):
            return Suggestion(
                id="F1-dead", code="F1", title=f"域名疑似失效：{host}",
                reason="域名无法解析或连接，无法推断新域名，请手动确认站点是否换了地址。",
                stage=str(event.get("stage") or "search"), confidence="low", patch=[], preview_kind="none",
                apply_allowed=False, meta={"host": host},
            )
    return None


# ---------------------------------------------------------------------------
# F2 -- GBK / Big5 charset for search
# ---------------------------------------------------------------------------

def _charset_of(f: _Fetch) -> str:
    """Normalised ``gbk`` / ``big5`` when any signal says so, else ``""``."""
    candidates = [f.charset]
    content_type = f.resp_headers.get("content-type", "")
    match = re.search(r"charset\s*=\s*['\"]?([a-zA-Z0-9_\-]+)", content_type, flags=re.I)
    if match:
        candidates.append(match.group(1))
    if f.body:
        try:
            meta = _vsp()._charset_from_html_meta(f.body[:4096])
        except Exception:  # noqa: BLE001
            meta = None
        if meta:
            candidates.append(meta)
    for name in candidates:
        key = (name or "").lower().replace("_", "-").replace("-", "")
        if key in _CHARSET_FIXES:
            return _CHARSET_FIXES[key]
    return ""


def _split_url_options(raw: str) -> tuple[str, str] | None:
    """Split ``url,{options}`` on the last ``,{`` the same way ``parse_url_options`` does (no normalising)."""
    matches = list(re.finditer(r",\s*(?=\{)", raw.strip()))
    text = raw.strip()
    if not matches or matches[-1].start() <= 0:
        return text, ""
    m = matches[-1]
    return text[: m.start()].strip(), text[m.end():].strip()


def _add_charset_option(search_url: str, charset: str) -> str | None:
    """``searchUrl`` with ``{"charset":...}`` merged in; ``None`` when it already has one or cannot be edited."""
    if _rule_syntax_is_js(search_url) or not search_url.strip():
        return None
    url_part, options = _split_url_options(search_url)
    if not options:
        return f"{url_part},{json.dumps({'charset': charset}, ensure_ascii=False, separators=(',', ':'))}"
    try:
        parsed = json.loads(options)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        if any(str(k).lower() == "charset" for k in parsed):
            return None
        merged = dict(parsed)
        merged["charset"] = charset
        return f"{url_part},{json.dumps(merged, ensure_ascii=False, separators=(',', ':'))}"
    # Lenient (single-quoted / JSON5-ish) options: keep the text, insert the key after the opening brace.
    if re.search(r"""["']?charset["']?\s*:""", options, flags=re.I):
        return None
    brace = options.find("{")
    if brace < 0:
        return None
    rest = options[brace + 1:].lstrip()
    separator = "" if rest.startswith("}") else ","
    return f'{url_part},{options[:brace + 1]}"charset":"{charset}"{separator}{options[brace + 1:]}'


def _f2(ctx: _Ctx, blocked_stages: set[str]) -> list[Suggestion]:
    if "search" in blocked_stages:
        return []
    search_url = ctx.raw_value("searchUrl")
    if not isinstance(search_url, str) or not search_url.strip():
        return []
    keywords = [str(_ev(e, "value") or "") for e in ctx.events_of("keyword", "search")]
    if not keywords:
        return []
    list_events = ctx.events_of("list", "search", "bookList")
    zero_list = bool(list_events) and all((_ev(e, "count") or 0) == 0 for e in list_events)
    for keyword in keywords:
        if not keyword or keyword.isascii():
            continue  # an ASCII keyword encodes the same in every charset
        encodings = {quote(keyword, encoding="utf-8").upper(), quote_plus(keyword, encoding="utf-8").upper()}
        for f in ctx.fetches:
            if f.stage != "search" or not f.ok:
                continue
            if not any(enc in f.url.upper() or enc in f.req_body.upper() for enc in encodings):
                continue
            charset = _charset_of(f)
            if not charset:
                continue
            if not (zero_list or keyword not in f.body):
                continue
            new_url = _add_charset_option(search_url, charset)
            if new_url is None:
                if _rule_syntax_is_js(search_url):
                    return [Suggestion(
                        id="F2", code="F2", title=f"搜索页是 {charset.upper()} 编码",
                        reason="搜索请求按 UTF-8 编码关键词，而站点是 GBK/Big5 页面，所以搜不到。searchUrl 是脚本，"
                               f'请手动在脚本返回的地址后加 ,{{"charset":"{charset}"}}。',
                        stage="search", confidence="medium", patch=[], preview_kind="none", apply_allowed=False,
                        meta={"charset": charset})]
                return []
            return [Suggestion(
                id="F2", code="F2", title=f"搜索请求补 charset={charset}",
                reason=f"站点页面是 {charset.upper()} 编码，但搜索请求把关键词按 UTF-8 编码，服务器收到乱码，所以搜不到。"
                       f'在 searchUrl 选项里加 "charset":"{charset}"。',
                stage="search", confidence="high" if zero_list else "medium",
                patch=[{"op": "set", "path": "searchUrl", "old": search_url, "new": new_url}],
                preview_kind="online", apply_allowed=True, meta={"charset": charset, "keyword": keyword})]
    return []


# ---------------------------------------------------------------------------
# F3 -- missing User-Agent / Referer
# ---------------------------------------------------------------------------

def _parse_header(raw: Any) -> tuple[dict[str, str] | None, str]:
    """``(header dict | None, mode)``; mode ``dict`` / ``text`` / ``empty`` / ``js`` / ``unknown``."""
    if raw is None or raw == "":
        return {}, "empty"
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v is not None}, "dict"
    if not isinstance(raw, str):
        return None, "unknown"
    text = raw.strip()
    if _rule_syntax_is_js(text):
        return None, "js"
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return {str(k): str(v) for k, v in parsed.items() if v is not None}, "text"
    lines: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            return None, "unknown"
        key, value = line.split(":", 1)
        lines[key.strip().strip("\"'")] = value.strip().strip("\"'")
    return (lines, "text") if lines else (None, "unknown")


def _f3(ctx: _Ctx, blocked_stages: set[str]) -> list[Suggestion]:
    vsp = _vsp()
    strong: list[_Fetch] = []   # attempt 0 failed, attempt 1 (default browser headers) worked
    weak: list[_Fetch] = []     # 403 / empty body
    by_key: dict[tuple[str, str], list[_Fetch]] = {}
    for f in ctx.fetches:
        by_key.setdefault((f.method, f.url), []).append(f)
    for fetches in by_key.values():
        first = next((f for f in fetches if f.attempt in (0, "0")), None)
        retry = next((f for f in fetches if f.attempt in (1, "1")), None)
        if first is None or first.stage in blocked_stages:
            continue
        first_failed = first.status in _ERROR_STATUSES or vsp.contains_http_error_page_signals(first.body)
        if (first_failed and retry is not None and retry.ok and retry.body.strip()
                and not vsp.contains_http_error_page_signals(retry.body)):
            strong.append(first)
    strong_ids = {id(f) for f in strong}
    for f in ctx.fetches:
        if f.stage in blocked_stages or id(f) in strong_ids:
            continue
        if f.status in (401, 403) or (f.ok and not f.body.strip() and f.status != 204):
            weak.append(f)
    if not strong and not weak:
        return []

    raw_header = ctx.raw_value("header")
    header, mode = _parse_header(raw_header)
    stage = (strong or weak)[0].stage or "search"
    if header is None:
        title = "header 是脚本，请手动补 User-Agent"
        return [Suggestion(
            id="F3", code="F3", title=title,
            reason="请求被拒绝（401/403/空响应）且浏览器头可通过，但 header 是脚本或无法解析，无法自动修改，"
                   "请手动补 User-Agent（和详情/正文需要的 Referer）。",
            stage=stage, confidence="low", patch=[], preview_kind="none", apply_allowed=False)]

    new_header: dict[str, str] = {}
    renamed: list[str] = []
    canonical = {"user-agent": "User-Agent", "referer": "Referer"}
    for key, value in header.items():
        fixed = canonical.get(key.lower(), key)
        if fixed != key:
            renamed.append(f"{key}→{fixed}")
        new_header[fixed] = value
    default_ua = vsp.DEFAULT_BROWSER_HEADERS["User-Agent"]
    notes: list[str] = []
    ua = new_header.get("User-Agent")
    if ua is None or (strong and "mozilla" not in ua.lower()):
        new_header["User-Agent"] = default_ua
        notes.append("补浏览器 User-Agent")
    if renamed:
        notes.append("规范键大小写（" + "、".join(renamed) + "）")
    forbidden_detail = [f for f in (strong + weak) if f.stage in ("info", "toc", "content") and f.status in (401, 403)]
    root = _source_root(ctx.src)
    if forbidden_detail and root and "Referer" not in new_header:
        new_header["Referer"] = root
        notes.append("补 Referer")
    if new_header == header:
        return []
    if mode == "dict":
        new_value: Any = new_header
    else:
        new_value = json.dumps(new_header, ensure_ascii=False, separators=(",", ":"))
    old_value = copy.deepcopy(raw_header) if raw_header is not None else None
    return [Suggestion(
        id="F3", code="F3", title="请求头：" + "；".join(notes),
        reason=("同一地址第 1 次请求被拒绝（401/403/503 或错误页），换成浏览器默认请求头后第 2 次成功。"
                if strong else
                "请求被拒绝（403）或返回空内容，而书源 header 缺少 User-Agent / 键名大小写不对"
                "（Legado 的 header 键区分大小写）。") + "补全后请重新校验。",
        stage=stage, confidence="high" if strong else "medium",
        patch=[{"op": "set", "path": "header", "old": old_value, "new": new_value}],
        preview_kind="online", apply_allowed=True,
        meta={"evidence": "retry-with-browser-headers" if strong else "forbidden-or-empty", "notes": notes})]


# ---------------------------------------------------------------------------
# F4 -- empty list -> candidate selectors
# ---------------------------------------------------------------------------

_TOC_TITLE_RE = re.compile(r"第\s*[0-9零一二三四五六七八九十百千两]+\s*[章节回话]")


def _list_rule_path(stage: str, ctx: _Ctx) -> str | None:
    if stage == "search":
        return "ruleSearch.bookList"
    if stage == "explore":
        return "ruleExplore.bookList" if ctx.rule("ruleExplore.bookList").strip() else "ruleSearch.bookList"
    if stage == "toc":
        return "ruleToc.chapterList"
    return None


def _css_ident(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", value or ""))


def _node_locator(node: Any) -> tuple[str, str] | None:
    """``(legado locator, css selector)`` for a node with an id or class; ``None`` otherwise."""
    node_id = node.get("id")
    if isinstance(node_id, str) and _css_ident(node_id):
        return f"id.{node_id}", f"#{node_id}"
    classes = [c for c in (node.get("class") or []) if _css_ident(c)]
    if classes:
        return f"class.{classes[0]}", f"{node.name}.{classes[0]}"
    return None


def _container_locators(container: Any) -> tuple[str, str]:
    """Locate ``container`` with up to two ancestors: ``(legado chain, css chain)``."""
    legado: list[str] = []
    css: list[str] = []
    node = container
    for _ in range(3):
        loc = _node_locator(node)
        if loc is not None:
            legado.insert(0, loc[0])
            css.insert(0, loc[1])
            return "@".join(legado), " > ".join(css)
        legado.insert(0, f"tag.{node.name}")
        css.insert(0, node.name)
        node = node.parent
        if node is None or node.name in (None, "[document]"):
            break
    return "@".join(legado), " > ".join(css)


def _has_link(node: Any) -> bool:
    if node.name == "a" and node.get("href"):
        return True
    return node.find("a", href=True) is not None


def _html_candidates(body: str, stage: str, keyword: str) -> list[dict[str, str]]:
    soup = _soup(body)
    groups: list[tuple[int, Any, str, tuple[str, ...]]] = []
    for container in soup.find_all(True):
        if container.name in ("select", "head", "script", "style"):
            continue
        children = [c for c in container.children if getattr(c, "name", None)]
        if len(children) < 3:
            continue
        buckets: dict[tuple[str, tuple[str, ...]], list[Any]] = {}
        for child in children:
            if child.name in ("option", "script", "style", "br", "hr"):
                continue
            buckets.setdefault((child.name, tuple(child.get("class") or [])), []).append(child)
        for (tag, classes), nodes in buckets.items():
            if len(nodes) >= 3 and all(_has_link(n) for n in nodes):
                groups.append((len(nodes), container, tag, classes))
    groups.sort(key=lambda g: -g[0])
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(rule: str, syntax: str) -> None:
        if rule not in seen:
            seen.add(rule)
            out.append({"rule": rule, "syntax": syntax})

    for _count, container, tag, classes in groups[:6]:
        legado, css = _container_locators(container)
        class_css = "".join(f".{c}" for c in classes if _css_ident(c))
        add(f"{legado}@tag.{tag}", "legado")
        if classes and _css_ident(classes[0]):
            add(f"class.{classes[0]}", "legado")
        add(f"@CSS:{css} > {tag}{class_css}", "css")
    return out


def _json_candidates(body: str) -> list[dict[str, str]]:
    try:
        data = json.loads(body)
    except ValueError:
        return []
    found: list[tuple[str, list[Any]]] = []

    def key_segment(key: str) -> str:
        return f".{key}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else f"['{key}']"

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > 6:
            return
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict)]
            if len(dicts) >= 2 and len(dicts) == len(node):
                found.append((path + "[*]", node))
            for child in node[:3]:
                walk(child, path + "[*]", depth + 1)
        elif isinstance(node, dict):
            for key, child in node.items():
                walk(child, path + key_segment(str(key)), depth + 1)

    walk(data, "$", 0)
    out: list[dict[str, str]] = []
    for path, items in found:
        first = items[0] if items else {}
        out.append({"rule": path, "syntax": "json", "keys": ",".join(list(first)[:12]) if isinstance(first, dict) else ""})
    return out


def _item_text(item: str) -> str:
    stripped = item.lstrip()
    if stripped[:1] in "{[":
        return item
    try:
        return _soup(item).get_text(" ", strip=True)
    except Exception:  # noqa: BLE001
        return item


def _f4(ctx: _Ctx, blocked_stages: set[str]) -> list[Suggestion]:
    vsp = _vsp()
    out: list[Suggestion] = []
    handled: set[str] = set()
    for event in ctx.events:
        if event.get("kind") != "list":
            continue
        list_field = _ev(event, "field")
        stage = str(event.get("stage") or "")
        if list_field not in ("bookList", "chapterList") or (_ev(event, "count") or 0) != 0:
            continue
        if stage in blocked_stages or stage in handled:
            continue
        if any((_ev(e, "count") or 0) > 0 for e in ctx.events_of("list", stage, list_field)):
            continue  # a later keyword / page did produce a list: the rule works
        path = _list_rule_path(stage, ctx)
        if path is None:
            continue
        body, final_url, _entry = ctx.latest_body(stage)
        if not body or _visible_text_length(body) <= 500:
            continue
        is_json = body.lstrip()[:1] in ("{", "[")
        if not is_json and vsp.contains_http_error_page_signals(body):
            continue
        keyword = ""
        if stage == "search":
            keywords = ctx.events_of("keyword", "search")
            keyword = str(_ev(keywords[-1], "value") or "") if keywords else ""
        handled.add(stage)
        raw_candidates = _json_candidates(body) if is_json else _html_candidates(body, stage, keyword)
        base_url = final_url or ctx.runtime().base_url
        scored: list[dict[str, Any]] = []
        for candidate in raw_candidates[:16]:
            try:
                items = _eval_list(ctx, candidate["rule"], body, base_url)
            except Exception:  # noqa: BLE001 - an unparsable candidate is simply not a candidate
                continue
            count = len(items)
            if count < (2 if candidate["syntax"] == "json" else 3):
                continue
            texts = [_item_text(v) for v in items[:30]]
            if stage == "toc":
                hit = sum(1 for t in texts if _TOC_TITLE_RE.search(t)) / max(len(texts), 1)
            elif keyword:
                hit = 1.0 if any(keyword in t for t in texts) else 0.0
            else:
                hit = 0.0
            scored.append({"rule": candidate["rule"], "syntax": candidate["syntax"], "count": count,
                           "hit": round(hit, 2), "sample": (texts[0][:80] if texts else ""),
                           "keys": candidate.get("keys", "")})
        if not scored:
            continue
        scored.sort(key=lambda c: (-(c["hit"] > 0), -c["count"]))
        top = scored[:5]
        heuristic = None
        if stage in ("search", "explore"):
            try:
                heuristic = len(vsp.heuristic_book_items_from_html(body, base_url, keyword))
            except Exception:  # noqa: BLE001
                heuristic = None
        current = ctx.raw_value(path)
        best = top[0]
        confidence = "medium" if best["hit"] >= 0.5 else "low"
        label = {"search": "搜索结果列表", "explore": "发现列表", "toc": "目录列表"}[stage]
        out.append(Suggestion(
            id=f"F4-{stage}", code="F4", title=f"{label}为 0：候选选择器 {best['rule']}（命中 {best['count']} 项）",
            reason=f"页面有 {_visible_text_length(body)} 字可见内容但列表规则命中 0 项。按重复结构找到 {len(top)} 个候选，"
                   "首选已用离线重放验证；应用前请确认首项名称/链接正确。",
            stage=stage, confidence=confidence,
            patch=[{"op": "set", "path": path, "old": current if current is not None else None, "new": best["rule"]}],
            preview_kind="offline", apply_allowed=False,
            meta={"candidates": top, "list_path": path, "heuristicItems": heuristic, "keyword": keyword}))
    return out


def with_candidate(suggestion: Suggestion, index: int) -> Suggestion:
    """Copy of an F4 suggestion that applies candidate ``index`` of ``meta["candidates"]`` instead of the first."""
    candidates = suggestion.meta.get("candidates") or []
    if suggestion.code != "F4" or not 0 <= index < len(candidates):
        raise IndexError(index)
    clone = copy.deepcopy(suggestion)
    clone.patch[0]["new"] = candidates[index]["rule"]
    clone.apply_allowed = False  # must be previewed again
    return clone


# ---------------------------------------------------------------------------
# F5 -- lazy-loaded cover (data-original / data-src)
# ---------------------------------------------------------------------------

_LAZY_ATTRS = ("data-original", "data-src", "data-lazy-src", "_src")
_PLACEHOLDER_RE = re.compile(r"data:image|loading|lazy|nocover|blank\.gif", re.I)
_COVER_SPECS = (
    ("ruleSearch.coverUrl", "ruleSearch.bookList", "search"),
    ("ruleExplore.coverUrl", "ruleExplore.bookList", "explore"),
    ("ruleBookInfo.coverUrl", None, "info"),
)


def _is_placeholder(value: str) -> bool:
    return (not value.strip()) or bool(_PLACEHOLDER_RE.search(value))


def _split_src_suffix(rule: str) -> tuple[str, str] | None:
    """``(prefix, separator)`` when a single-segment rule ends in ``@src`` (or XPath ``/@src``)."""
    text = rule.strip()
    if not text or _rule_syntax_is_js(text) or any(tok in text for tok in ("||", "&&", "%%", "##", "{{")):
        return None
    if text.endswith("/@src"):
        return text[: -len("/@src")], "/@"
    if text.endswith("@src"):
        return text[: -len("@src")], "@"
    return None


def _cover_items(ctx: _Ctx, path: str, list_path: str | None, body: str, base_url: str) -> list[str]:
    if list_path is None:
        return [body]
    list_rule = ctx.rule(list_path)
    if not list_rule.strip() and list_path.startswith("ruleExplore"):
        list_rule = ctx.rule("ruleSearch.bookList")
    if not list_rule.strip():
        return []
    return _eval_list(ctx, list_rule, body, base_url)


def _f5(ctx: _Ctx) -> list[Suggestion]:
    ops: list[dict] = []
    stages: list[str] = []
    found_attrs: list[str] = []
    for path, list_path, stage in _COVER_SPECS:
        rule = ctx.rule(path)
        split = _split_src_suffix(rule)
        if split is None:
            continue
        if stage == "info" and ctx.rule("ruleBookInfo.init").strip():
            continue  # the cover is evaluated on the init result; the replay cannot reproduce that safely
        body, final_url, _entry = ctx.latest_body(stage)
        if not body:
            continue
        base_url = final_url or ctx.runtime().base_url
        items = _cover_items(ctx, path, list_path, body, base_url)[:20]
        if not items:
            continue
        prefix, separator = split
        placeholders = [i for i in items if _is_placeholder(_eval_on_item(ctx, rule, i, base_url))]
        if not placeholders:
            continue
        for attr in _LAZY_ATTRS:
            lazy_rule = f"{prefix}{separator}{attr}"
            hits = [bool(_eval_on_item(ctx, lazy_rule, i, base_url)) and
                    not _is_placeholder(_eval_on_item(ctx, lazy_rule, i, base_url)) for i in items]
            if not any(hits):
                continue
            new = lazy_rule if all(hits) else f"{lazy_rule}||{rule.strip()}"
            ops.append({"op": "set", "path": path, "old": ctx.raw_value(path), "new": new})
            stages.append(stage)
            found_attrs.append(attr)
            break
    if not ops:
        return []
    return [Suggestion(
        id="F5", code="F5", title="封面懒加载：改取 " + "、".join(dict.fromkeys(found_attrs)),
        reason="封面规则取 @src，但页面里 src 是占位图（loading / data:image），真正的图片地址放在懒加载属性里。",
        stage=stages[0], confidence="high", patch=ops, preview_kind="offline", apply_allowed=False,
        meta={"attrs": found_attrs, "stages": stages})]


# ---------------------------------------------------------------------------
# F6 -- missing nextTocUrl
# ---------------------------------------------------------------------------

_NEXT_TEXT_RE = re.compile(r"下一页|下页|下一頁|next|»", re.I)


def _f6(ctx: _Ctx) -> list[Suggestion]:
    if ctx.rule("ruleToc.nextTocUrl").strip():
        return []
    body, final_url, _entry = ctx.latest_body("toc")
    if not body or body.lstrip()[:1] in "{[":
        return []
    lists = ctx.events_of("list", "toc", "chapterList")
    first_count = int(_ev(lists[0], "count") or 0) if lists else 0
    items = {str(_ev(e, "value")) for e in ctx.events_of("item", "toc") if _ev(e, "value") is not None}
    unique = len(items) if items else first_count
    if not (unique < 30 or (first_count and unique == first_count)):
        return []
    vsp = _vsp()
    page_host = _bare_host(_host_of(final_url))
    soup = _soup(body)
    links: list[tuple[str, str, str]] = []
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(strip=True)
        href = str(anchor.get("href") or "").strip()
        if not text or len(text) > 12 or not _NEXT_TEXT_RE.search(text):
            continue
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        resolved = urljoin(final_url, href)
        if _bare_host(_host_of(resolved)) != page_host or resolved.rstrip("/") == final_url.rstrip("/"):
            continue
        links.append((text, href, resolved))
    new_rule = ""
    kind = ""
    if links:
        token = next((t for t in ("下一页", "下页", "下一頁", "next", "»") if t in links[0][0].lower()), links[0][0])
        new_rule = f"text.{token}@href"
        kind = "link"
    else:
        try:
            options = vsp.guess_toc_range_option_urls_from_html(body, final_url)
        except Exception:  # noqa: BLE001
            options = []
        if options:
            selector = "select"
            for option in soup.find_all("option", value=True):
                if urljoin(final_url, str(option.get("value"))) in options and option.parent is not None:
                    loc = _node_locator(option.parent)
                    name = option.parent.get("name")
                    selector = (loc[1] if loc else f"select[name={name}]" if isinstance(name, str) and _css_ident(name)
                                else "select")
                    break
            new_rule = f"@CSS:{selector} option@value"
            kind = "select"
    if not new_rule:
        return []
    distinct = len({resolved for _t, _h, resolved in links})
    confidence = "medium" if kind == "select" or distinct <= 1 else "low"
    return [Suggestion(
        id="F6", code="F6", title=f"目录分页：补 nextTocUrl `{new_rule}`",
        reason="目录页有分页入口，但 nextTocUrl 为空，App 只读第一页目录。注意：校验器会自己猜分页链接，"
               "App 不会，所以校验通过的源在 App 里可能仍只有一页目录。",
        stage="toc", confidence=confidence,
        patch=[{"op": "set", "path": "ruleToc.nextTocUrl", "old": ctx.raw_value("ruleToc.nextTocUrl"), "new": new_rule}],
        preview_kind="offline", apply_allowed=False,
        meta={"kind": kind, "uniqueChapters": unique, "firstPageChapters": first_count,
              "links": [{"text": t, "href": h} for t, h, _r in links[:5]]})]


# ---------------------------------------------------------------------------
# F7 -- ad text clean-up
# ---------------------------------------------------------------------------

_AD_PATTERNS: tuple[tuple[str, str], ...] = (
    # Tails stop at a newline or a tag so the same pattern is safe on text (replaceRegex) and on HTML (content ##).
    ("天才一秒记住", r"天才一秒记住[^\n<]{0,60}"),
    ("本章未完", r"本章未完[^\n<]{0,10}点击下一页[^\n<]{0,20}"),
    ("请记住本书首发域名", r"请记住本书首发域名[^\n<]{0,60}"),
    ("最新网址", r"最新网址[^\n<]{0,60}"),
    ("手机版阅读网址", r"手机版阅读网址[^\n<]{0,60}"),
    ("章节错误", r"章节错误[,，]点此举报[^\n<]{0,20}"),
    ("一秒记住", r"(?<!天才)一秒记住[^\n<]{0,60}"),
    ("域名", r"(?:www|m)\.[a-z0-9-]+\.(?:com|net|org|cc|la|info)"),
)


def _content_pages(ctx: _Ctx) -> list[str]:
    pages = []
    for event in ctx.events_of("field", "content", "content"):
        value = _ev(event, "value")
        if isinstance(value, str) and value.strip():
            pages.append(value)
    return pages


def _full_content(ctx: _Ctx, pages: list[str]) -> tuple[str, bool]:
    """Complete chapter text replayed from the saved response (``True``), else the event excerpt (``False``)."""
    body, final_url, _entry = ctx.latest_body("content")
    rule = ctx.rule("ruleContent.content")
    if body and rule.strip():
        result = _run_rule(ctx.src, "ruleContent.content", body=body, base_url=final_url or ctx.runtime().base_url,
                           rule=rule, app_version=ctx.app_version)
        text = result["values"][0] if result["values"] else ""
        if text.strip():
            return text, True
    return "\n".join(pages), False


def _repeated_lines(pages: list[str]) -> list[str]:
    if len(pages) < 2:
        return []
    sets = [{ln.strip() for ln in page.splitlines() if len(ln.strip()) >= 6} for page in pages]
    common = set.intersection(*sets) if sets else set()
    return sorted(common, key=lambda s: (-len(s), s))[:5]


def _append_to_content_rule(content_rule: str, regex: str) -> str | None:
    """``content`` rule with ``##regex`` appended; ``None`` when the rule shape makes that unsafe."""
    if _rule_syntax_is_js(content_rule):
        return None
    count = content_rule.count("##")
    if count == 0:
        return f"{content_rule}##{regex}"
    if count == 1 and content_rule.split("##", 1)[0].strip():
        return f"{content_rule}|{regex}"
    return None


def _f7(ctx: _Ctx) -> list[Suggestion]:
    pages = _content_pages(ctx)
    if not pages:
        return []
    text, _replayed = _full_content(ctx, pages)
    haystack = text + "\n" + "\n".join(pages)
    patterns: list[str] = []
    names: list[str] = []
    for name, regex in _AD_PATTERNS:
        if re.search(regex, haystack, flags=re.I):
            patterns.append(regex)
            names.append(name)
    for line in _repeated_lines(pages):
        patterns.append(re.escape(line))
        names.append("重复行")
    if not patterns:
        return []
    regex = "|".join(patterns)
    replace_rule = ctx.rule("ruleContent.replaceRegex")
    content_rule = ctx.rule("ruleContent.content")
    meta: dict[str, Any] = {"patterns": patterns, "names": names}
    if not replace_rule.strip():
        patch = [{"op": "set", "path": "ruleContent.replaceRegex", "old": ctx.raw_value("ruleContent.replaceRegex"),
                  "new": f"##{regex}"}]
        meta["target"] = "replaceRegex"
    else:
        appended = _append_to_content_rule(content_rule, regex)
        patch = ([{"op": "set", "path": "ruleContent.content", "old": ctx.raw_value("ruleContent.content"),
                   "new": appended}] if appended else [])
        meta["target"] = "content" if appended else "hint"
    reason = ("正文里出现站点广告 / 推广文字（" + "、".join(dict.fromkeys(names)) + "）。"
              + ("replaceRegex 为空，新建一条净化规则。" if meta["target"] == "replaceRegex" else
                 "replaceRegex 已有内容，建议把净化写进 content 规则尾部（##正则）。" if meta["target"] == "content" else
                 "content 规则含脚本或多段替换，无法自动追加，请手动加入净化正则：##" + regex))
    return [Suggestion(
        id="F7", code="F7", title="正文净化：去除广告文字", reason=reason, stage="content",
        confidence="medium", patch=patch, preview_kind="offline" if patch else "none", apply_allowed=False,
        meta=meta)]


# ---------------------------------------------------------------------------
# suggest / preview
# ---------------------------------------------------------------------------

_RULE_ORDER = ("F8", "F1", "F1b", "F2", "F3", "F4", "F5", "F6", "F7")


def suggest(src: dict[str, Any], session: Any, *, keep_source_url: bool = False,
            app_version: str | None = None) -> list[Suggestion]:
    """Repair suggestions for ``src`` (the current working source) given a finished debug ``session``.

    ``keep_source_url=True`` keeps ``bookSourceUrl`` out of the F1 patch (the primary key stays).
    Offline suggestions are replayed once here, so ``apply_allowed`` already reflects the preview.
    """
    ctx = _Ctx(src, session, app_version, keep_source_url)
    if not ctx.events:
        return []
    results: list[Suggestion] = []

    def run(fn: Callable[[], list[Suggestion]], name: str) -> list[Suggestion]:
        try:
            return fn()
        except Exception:  # noqa: BLE001 - one broken rule must not hide the others
            log.warning("fix suggestion rule %s failed", name, exc_info=True)
            return []

    f8, blocked = _f8(ctx)
    results += f8
    f1 = run(lambda: _f1(ctx), "F1")
    results += f1
    migration = any(s.code == "F1" and s.patch for s in f1)  # F1 outranks F2 / F3 (F1b and the dead-host hint do not)
    if not migration:
        results += run(lambda: _f2(ctx, blocked), "F2")
        results += run(lambda: _f3(ctx, blocked), "F3")
    results += run(lambda: _f4(ctx, blocked), "F4")
    results += run(lambda: _f5(ctx), "F5")
    results += run(lambda: _f6(ctx), "F6")
    results += run(lambda: _f7(ctx), "F7")
    results.sort(key=lambda s: _RULE_ORDER.index(s.code) if s.code in _RULE_ORDER else 99)

    for suggestion in results:
        if suggestion.preview_kind != "offline" or not suggestion.patch:
            continue
        try:
            outcome = preview(src, suggestion, session, online=False, app_version=ctx.app_version)
            suggestion.apply_allowed = bool(outcome.get("apply_allowed"))
        except Exception:  # noqa: BLE001
            log.warning("offline preview of %s failed", suggestion.id, exc_info=True)
            suggestion.apply_allowed = False
    return results


def apply_suggestion(doc: Any, suggestion: Suggestion, *, check_old: bool = True) -> None:
    """Apply a suggestion to a ``SourceDocument`` as one undo unit."""
    if not suggestion.patch:
        raise ValueError(f"{suggestion.id} is a hint without a patch")
    doc.apply(suggestion.patch, reason=suggestion.title, origin=f"suggestion:{suggestion.code}", check_old=check_old)


def is_safe(suggestion: Suggestion) -> bool:
    """CLI ``--apply-safe``: offline-verified and allowed."""
    return suggestion.preview_kind == "offline" and suggestion.apply_allowed and bool(suggestion.patch)


def _patched(src: dict[str, Any], suggestion: Suggestion) -> dict[str, Any]:
    doc = source_edit.SourceDocument(src)
    doc.apply(suggestion.patch, reason=suggestion.title, origin=f"suggestion:{suggestion.code}")
    return doc.working


def _plain(value: Any) -> str:
    return "" if value is None else str(value)


def preview(src: dict[str, Any], suggestion: Suggestion, session: Any, *, online: bool = False,
            app_version: str = DEFAULT_APP_VERSION) -> dict[str, Any]:
    """Prove a suggestion: ``{"ok", "before", "after", "summary", "apply_allowed", "kind"}``.

    Offline kinds replay the saved response (``online`` is ignored).  Online kinds (F1/F2/F3)
    re-run the debugger on the patched source when ``online=True``; otherwise ``ok`` is ``None``.
    """
    base = {"ok": None, "before": None, "after": None, "summary": "", "apply_allowed": False,
            "kind": suggestion.preview_kind, "id": suggestion.id}
    if not suggestion.patch or suggestion.preview_kind == "none":
        base.update(ok=False, summary="仅提示，无可应用的补丁")
        return base
    ctx = _Ctx(src, session, app_version, False)
    if suggestion.preview_kind == "online":
        return _preview_online(ctx, suggestion, base, online)
    handler = {"F4": _preview_f4, "F5": _preview_f5, "F6": _preview_f6, "F7": _preview_f7}.get(suggestion.code)
    if handler is None:
        base.update(ok=False, summary=f"{suggestion.code} 没有离线预览")
        return base
    result = handler(ctx, suggestion)
    base.update(result)
    return base


# -- offline previews ---------------------------------------------------------

def _preview_f4(ctx: _Ctx, suggestion: Suggestion) -> dict[str, Any]:
    path = suggestion.meta.get("list_path") or suggestion.patch[0]["path"]
    stage = suggestion.stage
    body, final_url, _entry = ctx.latest_body(stage)
    if not body:
        return {"ok": False, "summary": f"没有保存 {stage} 阶段的响应，无法离线重放"}
    base_url = final_url or ctx.runtime().base_url
    new_rule = suggestion.patch[0]["new"]
    before = _run_rule(ctx.src, path, body=body, base_url=base_url, app_version=ctx.app_version)
    after = _run_rule(ctx.src, path, body=body, base_url=base_url, rule=new_rule, app_version=ctx.app_version)
    count = int(after.get("items_count") or after.get("count") or 0)
    item_field = {"search": "ruleSearch.name", "explore": "ruleExplore.name", "toc": "ruleToc.chapterName"}.get(stage)
    first_item = ""
    warnings: list[str] = list(after.get("warnings") or [])
    if item_field and count:
        patched = _patched(ctx.src, suggestion)
        if source_edit.get_path(patched, item_field):
            item = _run_rule(patched, item_field, body=body, base_url=base_url, app_version=ctx.app_version)
            first_item = _plain(item.get("value"))
            if not first_item:
                warnings.append("候选列表可用，但现有名称规则在首项上取不到值，应用后还需检查名称/链接规则")
    ok = count > 0
    summary = (f"离线重放：列表 {before.get('count', 0)} → {count} 项" + (f"，首项：{first_item[:40]}" if first_item else "")
               if ok else "离线重放：候选规则命中 0 项")
    return {"ok": ok, "before": before, "after": after, "summary": summary, "apply_allowed": ok,
            "warnings": warnings}


def _preview_f5(ctx: _Ctx, suggestion: Suggestion) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    all_ok = True
    for op in suggestion.patch:
        path = op["path"]
        stage = {"ruleSearch.coverUrl": "search", "ruleExplore.coverUrl": "explore",
                 "ruleBookInfo.coverUrl": "info"}.get(path, "search")
        body, final_url, _entry = ctx.latest_body(stage)
        base_url = final_url or ctx.runtime().base_url
        if not body:
            rows.append({"path": path, "ok": False, "before": "", "after": "", "error": "没有保存响应"})
            all_ok = False
            continue
        before = _run_rule(ctx.src, path, body=body, base_url=base_url, app_version=ctx.app_version)
        patched = _patched(ctx.src, suggestion)
        after = _run_rule(patched, path, body=body, base_url=base_url, rule=op["new"], app_version=ctx.app_version)
        value = _plain(after.get("value"))
        row_ok = bool(re.match(r"https?://", value, flags=re.I)) and not _is_placeholder(value)
        all_ok = all_ok and row_ok
        rows.append({"path": path, "ok": row_ok, "before": _plain(before.get("value")), "after": value})
    summary = "；".join(f"{r['path']}：{r['before'][:30] or '∅'} → {r['after'][:50] or '∅'}" for r in rows)
    return {"ok": all_ok, "before": [r["before"] for r in rows], "after": [r["after"] for r in rows],
            "rows": rows, "summary": "离线重放：" + summary, "apply_allowed": all_ok}


def _preview_f6(ctx: _Ctx, suggestion: Suggestion) -> dict[str, Any]:
    body, final_url, _entry = ctx.latest_body("toc")
    if not body:
        return {"ok": False, "summary": "没有保存目录页响应，无法离线重放"}
    new_rule = suggestion.patch[0]["new"]
    result = _run_rule(ctx.src, "ruleToc.nextTocUrl", body=body, base_url=final_url or ctx.runtime().base_url,
                       rule=new_rule, app_version=ctx.app_version)
    values = [v for v in result.get("values", []) if re.match(r"https?://", v, flags=re.I)]
    distinct = list(dict.fromkeys(values))
    ok = bool(distinct)
    warnings = []
    if suggestion.meta.get("kind") == "link" and len(distinct) > 2:
        warnings.append("命中多个不同链接，可能是页码列表而非「下一页」，请确认")
    summary = (f"离线重放：nextTocUrl 返回 {len(distinct)} 个地址，首个 {distinct[0]}" if ok
               else "离线重放：规则没有返回有效的下一页地址")
    return {"ok": ok, "before": None, "after": result, "summary": summary, "apply_allowed": ok,
            "urls": distinct[:20], "warnings": warnings}


def _preview_f7(ctx: _Ctx, suggestion: Suggestion) -> dict[str, Any]:
    vsp = _vsp()
    pages = _content_pages(ctx)
    body, final_url, _entry = ctx.latest_body("content")
    op = suggestion.patch[0]
    patterns = suggestion.meta.get("patterns") or []
    base_url = final_url or ctx.runtime().base_url
    with app_profiles.app_profile_scope(ctx.app_version):
        view, _err = app_profiles.app_decode_view(ctx.src)
        runtime = vsp.make_runtime(view)
        if suggestion.meta.get("target") == "replaceRegex":
            text, _full = _full_content(ctx, pages)
            if not text.strip():
                return {"ok": False, "summary": "没有可用的正文，无法离线重放"}
            before_text, after_text = vsp.apply_legado_content_replace_regex(text, op["new"], runtime)
            hits = sum(len(re.findall(p, before_text, flags=re.I)) for p in patterns)
        else:  # appended to the content rule: compare the full content evaluation
            if not body:
                return {"ok": False, "summary": "没有保存正文页响应，无法离线重放"}
            runtime = vsp.RuleRuntime(**{**runtime.__dict__, "source_url": base_url, "base_url": base_url})
            ctype = "json" if vsp.is_json_content(body) else "html"
            old_rule = _plain(op.get("old"))
            before_text = vsp.evaluate_content_rule_with_inline_replace_guard(old_rule, body, runtime, ctype)
            after_text = vsp.evaluate_content_rule_with_inline_replace_guard(op["new"], body, runtime, ctype)
            hits = sum(len(re.findall(p, before_text, flags=re.I)) for p in patterns)
    before_len, after_len = len(before_text), len(after_text)
    deleted = max(before_len - after_len, 0)
    ratio = deleted / before_len if before_len else 0.0
    ok = deleted > 0 and hits > 0 and after_len > 0
    allowed = ok and ratio <= MAX_DELETE_RATIO
    if ratio > MAX_DELETE_RATIO:
        summary = f"离线重放：会删除 {deleted} 字（{ratio:.0%}），超过 {MAX_DELETE_RATIO:.0%} 上限，疑似误伤正文，不允许应用"
    elif ok:
        summary = f"离线重放：正文 {before_len} → {after_len} 字，删 {deleted} 字，命中 {hits} 处"
    else:
        summary = "离线重放：净化规则没有命中正文"
    return {"ok": ok, "before": {"chars": before_len}, "after": {"chars": after_len, "hits": hits,
                                                                "deletedRatio": round(ratio, 3)},
            "summary": summary, "apply_allowed": allowed}


# -- online preview -------------------------------------------------------------

def _online_key(ctx: _Ctx, suggestion: Suggestion) -> str:
    if suggestion.stage == "search":
        keywords = ctx.events_of("keyword", "search")
        if keywords:
            return str(_ev(keywords[0], "value") or "")
    return ""  # "auto": the same full pipeline as batch validation


def _preview_online(ctx: _Ctx, suggestion: Suggestion, base: dict[str, Any], online: bool) -> dict[str, Any]:
    before = _summarise_events(ctx.events)
    base["before"] = before
    if not online:
        base["summary"] = "需要联网预览（会按补丁后的书源重新请求站点）"
        return base
    engine = _load_debug_engine()
    if engine is None:
        base["summary"] = "调试引擎不可用，无法在线预览"
        return base
    patched = _patched(ctx.src, suggestion)
    key = _online_key(ctx, suggestion)
    events: list[dict] = []
    stop_after_search = suggestion.stage in ("search", "explore") and bool(key)

    def on_event(event: dict) -> None:
        events.append(event)
        if stop_after_search and event.get("kind") == "list" and _ev(event, "field") == "bookList":
            try:  # best effort: end the run once the search list is known
                from . import debug_trace  # type: ignore[attr-defined]
            except ImportError:
                try:
                    import debug_trace  # type: ignore[no-redef]
                except ImportError:
                    return
            raise debug_trace.DebugCancelled()

    try:
        engine.debug_source(patched, key, on_event, app_version=ctx.app_version, timeout=ONLINE_PREVIEW_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        base.update(ok=False, summary=f"在线预览失败：{type(exc).__name__}: {exc}", after={"errors": [str(exc)]})
        return base
    after = _summarise_events(events)
    if suggestion.stage in ("search", "explore") and key:
        ok = (after["bookListCount"] or 0) > 0
        summary = f"在线重跑：搜索结果 {before['bookListCount'] or 0} → {after['bookListCount'] or 0} 本"
    else:
        ok = after["outcome"] == "pass"
        summary = f"在线重跑：校验结论 {before['outcome'] or '?'} → {after['outcome'] or '?'}"
    base.update(ok=ok, after=after, summary=summary, apply_allowed=ok)
    return base
