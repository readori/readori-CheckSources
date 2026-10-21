#!/usr/bin/env python3
# 书源分阶段验证（与 iOS BookSourceValidator / BookSourceEngine 行为对齐）：
#   去重 → 快速扫描（连通性、搜索/发现入口）
#   → 完整链路（详情、目录、正文）→ 稳定性复测
# 完整链路：详情页 → 书名（或 tocUrl）→ 首章目录 → 非空正文；
# 目录/正文支持 nextTocUrl/nextContentUrl，最多 6 页。
# 输出：默认写入 docs/Json，可通过 CLI 指定任意输入/输出路径，便于独立整理书源
# 依赖：pip install -r tools/source-validator/requirements-validate-sources.txt
from __future__ import annotations

import argparse
import ast
import atexit
import base64
import contextlib
import contextvars
import copy
import functools
import hashlib
import http.cookiejar
import io
import json
import math
import os
import queue
import re
import shutil
import sys
import subprocess
import tempfile
import time
import threading
import unicodedata
import warnings
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import html.entities as _html_entities
import html.parser as _html_parser_module
from html import escape as html_escape, unescape as html_unescape
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote, quote_plus, unquote, unquote_to_bytes, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

import requests as _requests_lib
from bs4 import BeautifulSoup
from bs4 import MarkupResemblesLocatorWarning
from bs4 import XMLParsedAsHTMLWarning
from bs4.formatter import HTMLFormatter
from jsonpath_ng import parse as jsonpath_parse
try:
    from jsonpath_ng.ext import parse as jsonpath_ext_parse
except Exception:  # pragma: no cover - optional parser variant
    jsonpath_ext_parse = None
try:
    import quickjs
except (ImportError, OSError):
    quickjs = None
try:
    import py7zr
    from py7zr.io import BytesIOFactory
except (ImportError, OSError):
    py7zr = None
    BytesIOFactory = None
try:
    import rarfile
except (ImportError, OSError):
    rarfile = None
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except (ImportError, OSError):
    Cipher = algorithms = modes = None

# R143: App version profiles. Loaded as a package module (CLI / PyInstaller) or, from scripts/tests, as a
# top-level module with the validator directory on sys.path; both must resolve the same app_profiles module.
try:
    from . import app_profiles  # type: ignore[attr-defined]
except ImportError:
    import app_profiles  # type: ignore[no-redef]

# R146: opt-in debug trace channel (validator/debug_trace.py). Every hook is `if _dbg.active(): ...`, so a normal
# validation run only pays one ContextVar.get() per hook and behaves exactly as before.
try:
    from . import debug_trace as _dbg  # type: ignore[attr-defined]
except ImportError:
    import debug_trace as _dbg  # type: ignore[no-redef]

# R145b: every outbound request of the engine goes through net_guard's GuardedSession (per-hop SSRF check, connection
# pinned to a validated address, policy from net_guard.network_policy_scope / set_network_policy).
try:
    from . import net_guard  # type: ignore[attr-defined]
except ImportError:
    import net_guard  # type: ignore[no-redef]

set_network_policy = net_guard.set_network_policy
network_policy_scope = net_guard.network_policy_scope
network_allows_private = net_guard.network_allows_private


class _GuardedRequests:
    """Drop-in for the `requests` module inside this file: `requests.Session()` and the module-level helpers build a
    policy-enforcing session, everything else (exceptions, utils, Response) is the real requests module. A new
    `requests.Session()` added anywhere in the engine is therefore guarded without further edits."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_requests_lib, name)

    @property
    def Session(self) -> Any:  # noqa: N802 - mirrors requests.Session
        return net_guard.guarded_session_class()

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        with net_guard.guarded_session_class()() as session:
            return session.request(method=method, url=url, **kwargs)

    def get(self, url: str, params: Any = None, **kwargs: Any) -> Any:
        return self.request("get", url, params=params, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("allow_redirects", False)
        return self.request("head", url, **kwargs)

    def post(self, url: str, data: Any = None, json: Any = None, **kwargs: Any) -> Any:
        return self.request("post", url, data=data, json=json, **kwargs)


requests = _GuardedRequests()

DEFAULT_APP_VERSION = app_profiles.DEFAULT_APP_VERSION
app_profile_scope = app_profiles.app_profile_scope
current_app_version = app_profiles.current_app_version


def configure_rar_extractor() -> None:
    if rarfile is None:
        return
    candidates = [
        ("UNRAR_TOOL", ["unrar"]),
        ("UNAR_TOOL", ["unar"]),
        ("SEVENZIP_TOOL", [
            "7z",
            "7zz",
            str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip" / "7z.exe"),
        ]),
        ("BSDTAR_TOOL", ["bsdtar"]),
    ]
    for attribute, tools in candidates:
        for tool in tools:
            resolved = shutil.which(tool)
            if resolved is None and Path(tool).is_file():
                resolved = str(Path(tool))
            if resolved:
                setattr(rarfile, attribute, resolved)
                break


configure_rar_extractor()


ROOT = Path(__file__).resolve().parents[1]
JSON_DIR = ROOT / "docs" / "Json"
DEFAULT_VALIDATED_FILE_NAME = "bookinfo_validated_sources.json"
DEFAULT_VALIDATED_FULL_FILE_NAME = "bookinfo_validated_sources_full.json"
VALIDATION_TIMEZONE_KEY = "America/Los_Angeles"
URL_ATTRS = {"href", "src", "data-src", "data-href", "data-original", "data-url", "data-link"}

# 兼容旧版：额外合并这些文件（若存在且不在 discover 结果中）
# 注意：不含 bookinfo_validated_*，避免与验证输出互相污染
LEGACY_INPUT_FILES = [
    JSON_DIR / "book_sources_remaining_for_repair.json",
]

FALLBACK_KEYWORDS = [
    "我的",
    "夜无疆",
    "凡人修仙传",
    "我真不想修行啊",
]

REQUEST_TIMEOUT = 20
REQUEST_CONNECT_TIMEOUT = 6
DEFAULT_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
QUICKJS_TIME_LIMIT_SECONDS = 2
QUICKJS_MEMORY_LIMIT_BYTES = 32 * 1024 * 1024
NODE_DYNAMIC_REQUEST_REPLAY_LIMIT = 4
def _response_needs_raw_bytes(content_type: str, raw: bytes) -> bool:
    """Only a non-text Content-Type or a payload that is not valid UTF-8 needs its raw bytes next to the decoded text (an
    AES-encrypted body read back through `bodyAsBytes`); ordinary UTF-8 text responses never carry `bodyBase64`."""
    kind = content_type.split(";", 1)[0].strip().lower()
    textual = kind.startswith("text/") or kind in {
        "", "application/json", "application/xml", "application/xhtml+xml", "application/javascript", "application/x-javascript",
    } or kind.endswith(("+json", "+xml"))
    if not textual:
        return True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


RESPONSE_RAW_BYTES_LIMIT = 512 * 1024  # R157: raw response bytes kept per Node replay fixture (`bodyBase64`)
NODE_DYNAMIC_REQUESTS_PER_REPLAY = 8

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


# R149 (B10): attribute values of `BeautifulSoup(..., "html.parser")` come out of CPython's `html.parser`, which runs
# `html.unescape` on them: `href="/c?id=1&section_slot=0"` turns `&sect` into `§` (`§ion_slot=0`, a 404 for the
# chapter). Jsoup / SwiftSoup (Tokeniser.consumeCharacterReference with inAttribute) leave a semicolon-less named
# reference alone when a letter, digit, `=`, `-` or `_` follows it, and text nodes are untouched (bs4 sets
# convert_charrefs=False, so html.parser's `unescape` is only reached for attribute values).
_ATTRIBUTE_CHARREF_RE = re.compile(r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)")


def jsoup_unescape_attribute(value: str) -> str:
    if "&" not in value:
        return value

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token[0] == "#":
            return html_unescape(match.group(0))
        if token.endswith(";"):
            return html_unescape(match.group(0))
        if token in _html_entities.html5:
            return html_unescape(match.group(0))
        for end in range(len(token) - 1, 1, -1):
            if token[:end] in _html_entities.html5:
                following = token[end]
                if following.isalnum() or following in "=-_":
                    return match.group(0)
                return html_unescape(match.group(0))
        return match.group(0)

    return _ATTRIBUTE_CHARREF_RE.sub(replace, value)


def install_jsoup_attribute_unescape() -> None:
    if getattr(_html_parser_module, "unescape", None) is not None:
        _html_parser_module.unescape = jsoup_unescape_attribute


install_jsoup_attribute_unescape()

_thread_local = threading.local()


class SourceValidationDeadlineExceeded(TimeoutError):
    """Raised when one source exhausts its complete-chain wall-clock budget."""


def begin_source_validation_deadline(seconds: float) -> None:
    _thread_local.source_validation_deadline = (
        time.monotonic() + seconds if seconds > 0 else None
    )


def clear_source_validation_deadline() -> None:
    if hasattr(_thread_local, "source_validation_deadline"):
        delattr(_thread_local, "source_validation_deadline")


def remaining_source_validation_seconds(default: float) -> float:
    """Clamp a blocking operation to the current source's remaining budget."""
    deadline = getattr(_thread_local, "source_validation_deadline", None)
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SourceValidationDeadlineExceeded("source validation hard timeout")
    return max(0.05, min(default, remaining))


def ensure_source_validation_time_remaining() -> None:
    remaining_source_validation_seconds(float("inf"))


def configure_stdio_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
                write_through=True,
            )
        except TypeError:
            # Python implementations that do not expose write_through still
            # need line buffering when stdout is connected to the GUI pipe.
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass
        except Exception:
            pass


configure_stdio_encoding()


def make_quickjs_context():
    if quickjs is None:
        return None
    ctx = quickjs.Context()
    try:
        ctx.set_time_limit(QUICKJS_TIME_LIMIT_SECONDS)
    except Exception:
        pass
    try:
        ctx.set_memory_limit(QUICKJS_MEMORY_LIMIT_BYTES)
    except Exception:
        pass
    return ctx


_NODE_BINARY_CACHE: dict[str, str] = {}


def node_binary_path() -> str:
    # R156 (network T): the lookup (Path.is_file x3 + shutil.which) ran once per JS evaluation; the answer only
    # depends on the READORI_NODE_BIN override, so it is cached per override value.
    override = os.environ.get("READORI_NODE_BIN", "")
    cached = _NODE_BINARY_CACHE.get(override)
    if cached is not None:
        return cached
    candidates = [
        override,
        str(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"),
        shutil.which("node") or "",
    ]
    found = ""
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            found = candidate
            break
    _NODE_BINARY_CACHE[override] = found
    return found


@dataclass
class RuleRuntime:
    source_url: str
    base_url: str
    book_source_url: str
    headers: dict[str, str]
    rule_url: str = ""
    rule_request_method: str = "GET"
    rule_request_body: str = ""
    js_lib: str | None = None
    variables: dict[str, str] | None = None
    book_url: str = ""
    book_toc_url: str = ""
    book_name: str = ""
    book_author: str = ""
    book_intro: str = ""
    book_kind: str = ""
    book_cover_url: str = ""
    book_last_chapter: str = ""
    book_origin: str = ""
    book_origin_name: str = ""
    book_type: int = 0
    book_dur_chapter_index: int = 0
    book_dur_chapter_title: str = ""
    book_total_chapter_num: int = 0
    book_can_update: bool = True
    book_custom_intro: str = ""
    book_variable: str = ""
    book_use_replace_rule: bool | None = None
    book_reverse_toc: bool = False
    book_image_style: str = ""
    book_order: int = 0
    chapter_url: str = ""
    chapter_title: str = ""
    chapter_tag: str = ""
    chapter_is_volume: bool = False
    chapter_is_vip: bool = False
    chapter_is_pay: bool = False
    chapter_index: int = 0
    chapter_count: int = 0
    chapter_variable: str = ""
    chapter_active: bool = False
    cookie: str = ""
    previous_result: str = ""
    source_book_source_name: str = ""
    source_book_source_comment: str = ""
    source_variable_comment: str = ""
    source_book_source_type: int = 0
    source_last_update_time: int | float = 0
    source_header: str = ""
    source_login_url: str = ""
    source_login_ui: str = ""
    source_enabled_cookie_jar: bool = True
    source_book_source_group: str = ""
    source_explore_url: str = ""
    source_search_url: str = ""
    source_login_header: str = ""
    source_login_info: str = ""
    source_concurrent_rate: str = ""
    # First uncaught exception thrown by a source script since the last reset
    # (Legado ScriptException). Evaluations keep their legacy result; callers
    # that need Legado's failure semantics use raise_if_script_error().
    script_error: str = ""
    # Legado AnalyzeRule.book = ruleData as? BaseBook: "bound" (item fields,
    # detail, TOC, content), "unbound" (searchUrl/exploreUrl/RSS RuleData) or
    # "automatic" (bound except while a list rule is evaluated).
    book_binding: str = "automatic"
    book_list_depth: int = 0
    # Shared (by reference) across RuleRuntime(**{**runtime.__dict__, ...})
    # child copies, so the validation stage that owns a runtime sees every
    # script error/compat warning recorded by its child runtimes.
    script_error_sink: list | None = None
    # Validation report metadata only (never read by rule evaluation): the
    # author string that the selected search/explore item's `author` rule
    # already produced. Same-site book/author aggregation uses it, so
    # capturing it cannot alter Legado book binding or rule results.
    validation_book_author: str = ""
    # R143 (S1 = A): App batch validation runs with allowsUserInteraction=false, so the browser / verification-code
    # APIs are blocked there. make_runtime() (the pipeline) sets False; directly constructed runtimes keep the
    # legacy "assume the user finished the verification" behavior. interaction_state is shared by reference across
    # RuleRuntime(**{**runtime.__dict__, ...}) child copies (like script_error_sink):
    # {"kinds": [...], "failuresAtFirst": int | None, "failures": int}.
    allows_user_interaction: bool = True
    interaction_state: dict | None = None
    # R151: the discovery candidate fetch_book_candidates() selected after a passing search (App doCheckSource's
    # `.discovery` candidate): {"bookUrl", "base", "variables", "author", "reused"}. Report/validation metadata only;
    # it is never read by rule evaluation. Serialisable, so the quick-scan seed can carry it.
    explore_candidate: dict | None = None


class RuleScriptError(RuntimeError):
    """An uncaught source-script exception (Legado lets ScriptException escape)."""

    def __init__(self, raw_message: str) -> None:
        text = (raw_message or "").strip()
        if not re.match(r"^[A-Za-z]*(?:Error|Exception)\b", text):
            text = "JavaScriptException: " + (text or "unknown")
        super().__init__(text)


# Legado binding/global names a source script may use without defining them
# (AnalyzeRule.evalJS / AnalyzeUrl.evalJS bindings, Rhino JavaImporter and
# Java package roots). A ReferenceError/TypeError that points at one of these
# comes from the Python JS compatibility layer not implementing that Legado
# API, not from the source script itself.
_LEGADO_JS_BINDING_ROOTS = frozenset({
    "java", "source", "book", "chapter", "cookie", "cache", "rssArticle",
    "Packages", "JavaImporter", "importPackage", "importClass", "JavaAdapter",
    "org", "android", "androidx", "javax", "com", "cn", "okhttp3", "kotlin",
})


def is_js_compat_gap_error(message: str, script: str = "") -> bool:
    """Whether a JS error is attributable to a gap in the Python polyfill.

    Only ReferenceError/TypeError qualify, and only when the message names the
    Legado binding API surface:

    * ``X is not defined`` where X is a Legado binding/Java package root;
    * ``A.B... is not a function`` / ``is not a constructor`` where A is one;
    * ``Cannot read properties of undefined (reading 'p')`` where the script
      reads ``p`` from a member chain rooted at a binding (``java.foo.p``),
      i.e. the undefined value is a shim member the polyfill lacks.

    Everything else (syntax errors, ``throw``, errors on the script's own
    names, reading from a ``null`` book/chapter) is a genuine Legado
    ScriptException.
    """
    text = (message or "").strip()
    match = re.match(r"^(ReferenceError|TypeError)\b:?\s*(.*)$", text, flags=re.S)
    if not match:
        return False
    detail = match.group(2)
    undefined_name = re.match(r"^'?([A-Za-z_$][\w$]*)'? is not defined", detail)
    if undefined_name:
        return undefined_name.group(1) in _LEGADO_JS_BINDING_ROOTS
    member_call = re.match(r"^([A-Za-z_$][\w$]*)(?:\.[\w$]+)+ is not a (?:function|constructor)", detail)
    if member_call:
        return member_call.group(1) in _LEGADO_JS_BINDING_ROOTS
    undefined_read = re.match(r"^Cannot read propert(?:y|ies) of undefined \(reading '([^']+)'\)", detail)
    if undefined_read and script:
        prop = re.escape(undefined_read.group(1))
        roots = "|".join(sorted(_LEGADO_JS_BINDING_ROOTS))
        return re.search(rf"(?<![\w$.])(?:{roots})(?:\.[A-Za-z_$][\w$]*)+\s*(?:\.\s*{prop}\b|\[\s*['\"]{prop}['\"]\s*\])", script) is not None
    return False


def record_script_error(runtime: RuleRuntime, message: str, script: str = "") -> None:
    """Record an uncaught source-script exception on ``runtime``.

    Compat-layer gaps (is_js_compat_gap_error) are only reported to the stage
    sink as warnings; genuine errors also set ``runtime.script_error``.
    """
    if runtime is None or not message:
        return
    compat = is_js_compat_gap_error(message, script)
    sink = getattr(runtime, "script_error_sink", None)
    if sink is not None:
        sink.append({"kind": "compat" if compat else "error", "message": str(message)})
    if not compat and not getattr(runtime, "script_error", ""):
        runtime.script_error = str(message)


def begin_script_error_stage(runtime: RuleRuntime) -> list:
    """Start a validation stage: a fresh sink shared with every child runtime."""
    sink: list = []
    runtime.script_error_sink = sink
    runtime.script_error = ""
    return sink


def finish_script_error_stage(runtime: RuleRuntime, sink: list, detail: dict[str, Any], stage: str) -> None:
    """Record compat warnings in ``detail`` and fail the stage on a genuine error."""
    if runtime.script_error_sink is sink:
        runtime.script_error_sink = None
    compat = [entry["message"] for entry in sink if entry.get("kind") == "compat"]
    if compat:
        warnings = detail.setdefault("jsCompatWarnings", [])
        for message in dict.fromkeys(compat):
            if len(warnings) >= 10:
                break
            warnings.append(f"{stage}: {message}")
    errors = [entry["message"] for entry in sink if entry.get("kind") == "error"]
    runtime.script_error = errors[0] if errors else ""
    if errors:
        detail["failedStage"] = stage
    raise_if_script_error(runtime)


def raise_if_script_error(runtime: RuleRuntime) -> None:
    """Throw (and clear) the source-script exception recorded on ``runtime``."""
    message = getattr(runtime, "script_error", "")
    if message:
        runtime.script_error = ""
        raise RuleScriptError(message)


# ---- R143 App interaction model (S1 = A) ----------------------------------------------------------------------
# BookSourceBrowserBridge records every blocked interaction (startBrowser / startBrowserAwait / showBrowser /
# getVerificationCode) and the number of failure groups already recorded when the first one happened. The validator
# mirrors that with a per-runtime state; app_profiles.app_verdict() turns it into invalid / needsInteraction.


# Message of the NoStackTraceException SourceVerificationHelp throws when the (blocked) browser returns nothing.
BLOCKED_BROWSER_AWAIT_MESSAGE = "验证结果为空"


def new_interaction_state() -> dict[str, Any]:
    return {"kinds": [], "failuresAtFirst": None, "failures": 0}


def note_app_validation_failure(runtime: RuleRuntime) -> None:
    """A real (non-interaction) failure was recorded, e.g. search failed and validation fell back to explore."""
    state = getattr(runtime, "interaction_state", None)
    if isinstance(state, dict):
        state["failures"] = int(state.get("failures") or 0) + 1


def record_suppressed_interaction(runtime: RuleRuntime, kind: str) -> None:
    state = getattr(runtime, "interaction_state", None)
    if not isinstance(state, dict) or not kind:
        return
    kinds = state.setdefault("kinds", [])
    if state.get("failuresAtFirst") is None:
        state["failuresAtFirst"] = int(state.get("failures") or 0)
    if kind not in kinds:
        kinds.append(kind)


def apply_interaction_state_to_detail(detail: dict[str, Any], state: dict[str, Any] | None) -> None:
    """Fill detail["suppressedInteractions"] / ["failureBeforeInteraction"] from a runtime's interaction state."""
    if not isinstance(state, dict):
        return
    kinds = [str(kind) for kind in (state.get("kinds") or [])]
    detail["suppressedInteractions"] = kinds
    detail["failureBeforeInteraction"] = bool(kinds) and int(state.get("failuresAtFirst") or 0) > 0


def runtime_binds_book_as_null(runtime: RuleRuntime) -> bool:
    binding = getattr(runtime, "book_binding", "automatic")
    if binding == "bound":
        return False
    if binding == "unbound":
        return True
    return getattr(runtime, "book_list_depth", 0) > 0


def legado_is_true(raw: Any, null_is_true: bool = False) -> bool:
    """Mirror Legado String?.isTrue(nullIsTrue) token semantics."""
    if raw is None:
        return null_is_true
    text = str(raw)
    if not text.strip() or text == "null":
        return null_is_true
    return text.strip().lower() not in {"false", "no", "not", "0"}


def _insert_dot_after_bracket_before_name(path: str) -> str:
    """Jayway reads a bare property token right after "]" ($.a[*]b); jsonpath_ng needs the dot ($.a[*].b)."""
    out: list[str] = []
    quote = ""
    escaped = False
    for index, char in enumerate(path):
        out.append(char)
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
        elif char == "]" and index + 1 < len(path) and (path[index + 1].isalpha() or path[index + 1] == "_"):
            out.append(".")
    return "".join(out)


def parse_jsonpath_expression(path: str):
    # Legado's pinned Jayway version accepts but ignores the third slice part.
    path = re.sub(r"\[([^:\]]*):([^:\]]*):[^\]]*\]", r"[\1:\2]", path)
    path = _insert_dot_after_bracket_before_name(path)
    try:
        return jsonpath_parse(path)
    except Exception:
        if jsonpath_ext_parse is None:
            raise
        return jsonpath_ext_parse(path)


def seed_runtime_variables_from_url(runtime: RuleRuntime, url: str) -> None:
    if not url:
        return
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query, keep_blank_values=False)
    except Exception:
        return
    runtime.variables = dict(runtime.variables or {})
    for key, values in query.items():
        if not key or key in runtime.variables:
            continue
        value = next((str(v) for v in values if str(v).strip()), "")
        if value:
            runtime.variables[key] = value
            runtime.variables.setdefault(key.lower(), value)
    path_segments = [unquote(part) for part in (parsed.path or "").split("/") if part]
    book_id = ""
    for idx, segment in enumerate(path_segments[:-1]):
        marker = segment.lower()
        if marker in {"book", "books", "novel", "novels"}:
            candidate = path_segments[idx + 1].strip()
            if re.fullmatch(r"\d{2,}", candidate):
                book_id = candidate
                break
    if book_id:
        for key in ("book", "bookId", "bookid", "bid", "id", "novelId", "novelid"):
            runtime.variables.setdefault(key, book_id)


def substitute_variable_references(text: str, runtime: RuleRuntime) -> str:
    if "@get:{" not in (text or ""):
        return text
    variables = runtime.variables or {}

    def repl(match: re.Match[str]) -> str:
        return variables.get(match.group(1), match.group(0))

    return re.sub(r"@get:\{([^}]+)\}", repl, text)


def seed_runtime_book_kind_from_variables(runtime: RuleRuntime) -> None:
    if (runtime.book_kind or "").strip():
        return
    variables = runtime.variables or {}
    for key in ("book.kind", "bookKind", "resourceID", "resourceId", "bookId", "bookid", "bid", "id", "novelId", "novelid"):
        value = str(variables.get(key) or "").strip()
        if value:
            runtime.book_kind = value
            variables.setdefault("book.kind", value)
            variables.setdefault("bookKind", value)
            variables.setdefault("kind", value)
            runtime.variables = variables
            return


def sync_runtime_book_state_from_variables(runtime: RuleRuntime) -> None:
    variables = runtime.variables or {}

    def text(name: str, fallback: str) -> str:
        value = variables.get(f"__book.{name}")
        return fallback if value is None else str(value)

    def number(name: str, fallback: int) -> int:
        value = variables.get(f"__book.{name}")
        if value is None:
            return fallback
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return 0

    def boolean(name: str, fallback: bool) -> bool:
        value = variables.get(f"__book.{name}")
        if value is None:
            return fallback
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"", "0", "false", "null", "none", "undefined"}

    runtime.book_url = text("bookUrl", runtime.book_url)
    runtime.book_toc_url = text("tocUrl", runtime.book_toc_url)
    runtime.book_name = text("name", runtime.book_name)
    runtime.book_author = text("author", runtime.book_author)
    runtime.book_intro = text("intro", runtime.book_intro)
    runtime.book_kind = text("kind", runtime.book_kind)
    runtime.book_cover_url = text("coverUrl", runtime.book_cover_url)
    runtime.book_last_chapter = text("lastChapter", runtime.book_last_chapter)
    runtime.book_origin = text("origin", runtime.book_origin)
    runtime.book_origin_name = text("originName", runtime.book_origin_name)
    runtime.book_type = number("type", runtime.book_type)
    runtime.book_dur_chapter_index = number("durChapterIndex", runtime.book_dur_chapter_index)
    runtime.book_dur_chapter_title = text("durChapterTitle", runtime.book_dur_chapter_title)
    runtime.book_total_chapter_num = number("totalChapterNum", runtime.book_total_chapter_num)
    runtime.book_can_update = boolean("canUpdate", runtime.book_can_update)
    runtime.book_custom_intro = text("customIntro", runtime.book_custom_intro)
    runtime.book_variable = text("variable", runtime.book_variable)
    runtime.book_reverse_toc = boolean("reverseToc", runtime.book_reverse_toc)
    runtime.book_image_style = text("imageStyle", runtime.book_image_style)
    runtime.book_order = number("order", runtime.book_order)
    use_replace = variables.get("__book.useReplaceRule")
    if use_replace is not None:
        if isinstance(use_replace, bool):
            runtime.book_use_replace_rule = use_replace
        elif str(use_replace).strip().lower() in {"", "null", "none", "undefined"}:
            runtime.book_use_replace_rule = None
        else:
            runtime.book_use_replace_rule = str(use_replace).strip().lower() not in {"0", "false"}


def merge_runtime_book_state(target: RuleRuntime, source: RuleRuntime) -> None:
    """Carry the live Legado Book object across rule stages/pages."""
    for field_name in (
        "book_url",
        "book_toc_url",
        "book_name",
        "book_author",
        "book_intro",
        "book_kind",
        "book_cover_url",
        "book_last_chapter",
        "book_origin",
        "book_origin_name",
        "book_type",
        "book_dur_chapter_index",
        "book_dur_chapter_title",
        "book_total_chapter_num",
        "book_can_update",
        "book_custom_intro",
        "book_variable",
        "book_use_replace_rule",
        "book_reverse_toc",
        "book_image_style",
        "book_order",
    ):
        setattr(target, field_name, getattr(source, field_name))
    target.variables = dict(source.variables or {})


def sync_runtime_chapter_state_from_variables(runtime: RuleRuntime) -> None:
    variables = runtime.variables or {}
    raw = variables.get("__chapter.variable")
    if raw is not None:
        runtime.chapter_variable = str(raw)
    title = variables.get("__chapter.title")
    if title is not None:
        runtime.chapter_title = str(title)
    url = variables.get("__chapter.url")
    if url is not None:
        runtime.chapter_url = str(url)
    tag = variables.get("__chapter.tag")
    if tag is not None:
        runtime.chapter_tag = str(tag)
    for name, attr in (
        ("isVolume", "chapter_is_volume"),
        ("isVip", "chapter_is_vip"),
        ("isPay", "chapter_is_pay"),
    ):
        value = variables.get(f"__chapter.{name}")
        if value is not None:
            setattr(runtime, attr, legado_is_true(value))
    for name, attr in (("index", "chapter_index"), ("chapterCount", "chapter_count")):
        value = variables.get(f"__chapter.{name}")
        if value is None:
            continue
        try:
            setattr(runtime, attr, int(float(str(value))))
        except (TypeError, ValueError):
            setattr(runtime, attr, 0)


@dataclass
class ValidationOutcome:
    passed: bool
    search_mode: str = ""
    source_name: str = ""
    book_url: str = ""
    detail_name: str = ""
    detail_toc_url: str = ""
    reason: str = ""
    response_time_ms: int = 0


def load_json_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        # Legado exports and files forwarded through some Android/Telegram
        # clients commonly retain a UTF-8 BOM. ``json.loads`` rejects that
        # marker when the text was decoded as plain UTF-8, even though the iOS
        # importer accepts the same file.
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("sources", "bookSources", "list", "data"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        # A single exported BookSource object is also a valid Legado import.
        if data.get("bookSourceUrl") or data.get("bookSourceName"):
            return [data]
    return []


def discover_json_files(json_dir: Path) -> list[Path]:
    jd = json_dir.resolve()
    if not jd.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(jd.glob("*.json")):
        name = path.name.lower()
        if name.startswith("validation_report_") or name.startswith("bookinfo_validated"):
            continue
        out.append(path)
    return out


def display_path(path: Path, root: Path = ROOT) -> str:
    """Return a stable report path for bundled and external inputs."""
    resolved = path.expanduser().resolve()
    try:
        return str(resolved.relative_to(root.expanduser().resolve()))
    except ValueError:
        # PyInstaller runs from a temporary _MEI directory while users select
        # JSON files/directories anywhere on disk.
        return str(resolved)


def load_sources_from_dir(json_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """加载目录下全部书源 JSON（排除验证报告输出），并合并不在目录中的 LEGACY 路径。"""
    root = ROOT.resolve()
    out: list[dict[str, Any]] = []
    loaded_names: list[str] = []
    discovered = discover_json_files(json_dir)
    seen_paths: set[Path] = {p.resolve() for p in discovered}
    for path in discovered:
        batch = load_json_file(path)
        if batch:
            out.extend(batch)
            loaded_names.append(display_path(path, root))
    for path in LEGACY_INPUT_FILES:
        rp = path.resolve()
        if rp in seen_paths:
            continue
        batch = load_json_file(path)
        if batch:
            out.extend(batch)
            loaded_names.append(display_path(rp, root))
            seen_paths.add(rp)
    return out, loaded_names


def load_sources_from_paths(input_paths: list[Path]) -> tuple[list[dict[str, Any]], list[str]]:
    out: list[dict[str, Any]] = []
    loaded_names: list[str] = []
    seen_files: set[Path] = set()
    root_resolved = ROOT.resolve()

    for raw_path in input_paths:
        path = raw_path.expanduser().resolve()
        if path.is_dir():
            batch, names = load_sources_from_dir(path)
            out.extend(batch)
            loaded_names.extend(names)
            continue
        if not path.is_file() or path in seen_files:
            continue
        batch = load_json_file(path)
        if not batch:
            continue
        out.extend(batch)
        try:
            loaded_names.append(str(path.relative_to(root_resolved)))
        except ValueError:
            loaded_names.append(str(path))
        seen_files.add(path)

    return out, loaded_names


def load_sources() -> list[dict[str, Any]]:
    sources, _ = load_sources_from_dir(JSON_DIR)
    return sources


def default_report_path(json_dir: Path) -> Path:
    date_slug = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return json_dir / f"validation_report_{date_slug}.json"


def default_validated_output_paths(json_dir: Path) -> tuple[Path, Path]:
    return json_dir / DEFAULT_VALIDATED_FILE_NAME, json_dir / DEFAULT_VALIDATED_FULL_FILE_NAME


def current_validation_group_tag() -> str:
    try:
        return datetime.now(ZoneInfo(VALIDATION_TIMEZONE_KEY)).strftime("%m%d")
    except ZoneInfoNotFoundError:
        return datetime.now().strftime("%m%d")


def normalize_book_source_url(url: str) -> str:
    """Return the serialized Legado source identity without request rewriting."""
    return (url or "").strip()


def effective_base_url(url: str) -> str:
    """Return the network request base while retaining identity elsewhere."""
    text = normalize_book_source_url(url)
    if "##" in text:
        text = text.split("##", 1)[0]
    if "#" in text:
        text = text.split("#", 1)[0]
    return text.strip()


def _source_url_part(url: str) -> tuple[str, dict[str, Any]]:
    """Extract the network URL from a Legado serialized source identity."""

    raw = normalize_book_source_url(url)
    match = re.search(r"https?://", raw, flags=re.I)
    if match and match.start() > 0:
        raw = raw[match.start():]
    try:
        url_part, options = parse_url_options(raw)
    except Exception:
        url_part, options = raw, {}
    return effective_base_url(url_part), options


def _source_identity_tag(source_or_url: dict[str, Any] | str) -> str:
    """Return the Legado ``##tag`` suffix that makes a serialized identity distinct.

    ``https://site##route-a`` and ``https://site##route-b`` are separate
    BookSource identities in Legado and the iOS app, so dedupe keeps them
    apart. A plain ``#fragment`` is treated as an export cosmetic.
    """

    raw = source_or_url if isinstance(source_or_url, str) else str(source_or_url.get("bookSourceUrl") or "")
    text = normalize_book_source_url(raw)
    if "##" not in text:
        return ""
    return text.split("##", 1)[1].strip()


def canonical_source_site_key(source_or_url: dict[str, Any] | str) -> str:
    """Return a cosmetic-insensitive site key without changing source URLs."""

    raw = source_or_url if isinstance(source_or_url, str) else str(source_or_url.get("bookSourceUrl") or "")
    url_part, _ = _source_url_part(raw)
    if not url_part:
        return ""
    if url_part.startswith(("{", "[")):
        return url_part.casefold().rstrip("/")
    try:
        parsed = urlparse(url_part)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        if hostname:
            if hostname.startswith("www."):
                hostname = hostname[4:]
            host = hostname
            try:
                port = parsed.port
            except ValueError:
                port = None
            if port and port not in {80, 443}:
                host = f"{host}:{port}"
            path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
            pairs = sorted(parse_qs(parsed.query, keep_blank_values=True).items())
            query = "&".join(
                f"{quote(str(key), safe='')}={quote(str(value), safe='')}"
                for key, values in pairs
                for value in sorted(values)
            )
            return f"{host}{path}" + (f"?{query}" if query else "")
    except Exception:
        pass
    return url_part.casefold().rstrip("/")


def _source_identity_scope(source_or_url: dict[str, Any] | str) -> str:
    """Canonical site plus any Legado ``##tag`` identity suffix."""

    site = canonical_source_site_key(source_or_url)
    tag = _source_identity_tag(source_or_url)
    return f"{site}##{tag}" if tag else site


_RULE_FINGERPRINT_FIELDS = (
    "bookSourceType", "searchUrl", "exploreUrl", "bookUrlPattern", "ruleSearch", "ruleExplore",
    "ruleBookInfo", "ruleToc", "ruleContent", "header", "loginUrl", "loginUi",
    "loginCheckJs", "jsLib", "enabledCookieJar", "enabledExplore", "concurrentRate",
)


def _stable_rule_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable_rule_value(value[key]) for key in sorted(value, key=lambda item: str(item))}
    if isinstance(value, list):
        return [_stable_rule_value(item) for item in value]
    if isinstance(value, str):
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        # URL cosmetics inside search/explore/login rules should not produce
        # a different fingerprint.  Keep templates and all non-URL rule text
        # untouched while canonicalizing embedded HTTP(S) endpoints.
        def normalize_embedded_url(match: re.Match[str]) -> str:
            site = canonical_source_site_key(match.group(0))
            return f"https://{site}" if site else match.group(0)

        return re.sub(r"https?://[^\s'\"<>`]+", normalize_embedded_url, text, flags=re.I)
    return value


def source_rule_fingerprint(source: dict[str, Any]) -> str:
    """Fingerprint behavior-affecting rules, excluding names and URL cosmetics."""

    raw_url = str(source.get("bookSourceUrl") or "")
    _, url_options = _source_url_part(raw_url)
    payload = {
        field: _stable_rule_value(source.get(field))
        for field in _RULE_FINGERPRINT_FIELDS
        if field in source
    }
    if url_options:
        payload["bookSourceUrlOptions"] = _stable_rule_value(url_options)
    if not source.get("enabled", True):
        # R156 (dedupe): a disabled source is not checked (`failureReason: disabled`), so it must never become the group
        # representative of enabled copies with the same rules (or the other way round). Enabled sources keep their
        # historical fingerprint (the key is only added for a disabled source; same predicate as the full validation).
        payload["enabled"] = False
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def source_dedupe_key(source: dict[str, Any]) -> str:
    """Layers 1+2: canonical site (keeping any ``##tag``) + rule fingerprint."""

    return f"site:{_source_identity_scope(source)}|rule:{source_rule_fingerprint(source)}"


def normalize_book_identity(value: Any) -> str:
    text = html_unescape(unicodedata.normalize("NFKC", str(value or ""))).strip().casefold()
    text = re.sub(r"^(?:作者|author)\s*[:：]\s*", "", text)
    return "".join(char for char in text if char.isalnum())


def _detail_book_identity(record: dict[str, Any], detail: dict[str, Any] | None) -> tuple[str, str, str] | None:
    metadata = record.get("__readoriValidation") if isinstance(record.get("__readoriValidation"), dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    title = normalize_book_identity(metadata.get("bookTitle") or detail.get("detailBookName"))
    if not title:
        return None
    author = normalize_book_identity(metadata.get("bookAuthor") or detail.get("detailBookAuthor"))
    sample = canonical_source_site_key(str(metadata.get("sampleBookUrl") or detail.get("sampleBookUrl") or ""))
    return title, author, sample


_QUALITY_STAGES = ("search", "detail", "toc", "content")


def candidate_quality_key(record: dict[str, Any], detail: dict[str, Any] | None) -> tuple[int, int, int]:
    """R147 (F13): how completely a validated variant really worked, higher is better.

    ``(stages passed, content size class, unique chapters)`` from what the validation measured (the
    ``__readoriValidation`` metadata the passing record carries, falling back to the ``detail`` of the pipeline). The
    content size class is ``bit_length`` of the first-chapter text length (a 1200 vs 1300 character difference is
    noise, 0 vs 800 is not), so a variant whose rules return the whole chapter beats one that returns a stub, and a
    variant that walks every TOC page beats one that stops at the first. Records without measurements score ``(0,0,0)``.
    """
    metadata = record.get("__readoriValidation") if isinstance(record.get("__readoriValidation"), dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    # The metadata travels with the passing record; ``detail`` may belong to a later (failed) stability round.
    stages = detail.get("stages") if isinstance(detail.get("stages"), dict) else None
    if "stagesPassed" in metadata:
        passed = _int_or_zero(metadata.get("stagesPassed"))
    elif stages is not None:
        passed = sum(1 for name in _QUALITY_STAGES if stages.get(name))
    else:
        passed = 0

    def measured(metadata_key: str, detail_key: str) -> int:
        value = _int_or_zero(metadata.get(metadata_key))
        return value if value > 0 else _int_or_zero(detail.get(detail_key))

    content_chars = measured("contentChars", "contentPreviewChars")
    chapters = measured("tocChapters", "tocUniqueChapterCount")
    return passed, max(0, content_chars).bit_length(), max(0, chapters)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def aggregate_validated_sources(
    entries: Iterable[tuple[str, dict[str, Any], dict[str, Any] | None]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Layer 3: collapse same-site validated variants by normalized title and author.

    Aggregation never crosses sites or Legado ``##tag`` identities, and records
    without a usable book identity pass through untouched.
    """

    groups: dict[tuple[str, str, str, str], list[tuple[str, dict[str, Any], dict[str, Any] | None]]] = {}
    passthrough: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
    input_count = 0
    for group_key, record, detail in entries:
        input_count += 1
        identity = _detail_book_identity(record, detail)
        if identity is None:
            passthrough.append((group_key, record, detail))
            continue
        title, author, sample = identity
        if not author and not sample:
            passthrough.append((group_key, record, detail))
            continue
        groups.setdefault((_source_identity_scope(record), title, author, "" if author else sample), []).append((group_key, record, detail))
    selected: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = list(passthrough)
    duplicate_groups = 0
    for candidates in groups.values():
        if len(candidates) > 1:
            duplicate_groups += 1
        # R147 (F13): measured quality first (all stages really passed, fuller chapter text, more chapters); the
        # customTag based score_candidate cannot tell rule quality apart (every passing record carries the same tag).
        candidates.sort(key=lambda item: (tuple(-part for part in candidate_quality_key(item[1], item[2])), -score_candidate(item[1]), int(item[1].get("respondTime") or 0), str(item[1].get("bookSourceName") or "").casefold(), str(item[1].get("bookSourceUrl") or "")))
        selected.append(candidates[0])
    selected.sort(key=lambda item: (canonical_source_site_key(item[1]), str(item[1].get("bookSourceName") or "").casefold()))
    return [record for _, record, _ in selected], {"input": input_count, "output": len(selected), "removed": max(0, input_count - len(selected)), "duplicateGroups": duplicate_groups}


def normalized_book_candidate_url_for_comparison(url: str) -> str:
    text = effective_base_url(url)
    if not text or text.startswith(("{", "[")):
        return ""
    text, _ = parse_url_options(text)
    try:
        parsed = urlparse(text)
        scheme = (parsed.scheme or "").lower()
        netloc = (parsed.netloc or "").lower()
        path = (parsed.path or "").rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        if scheme and netloc:
            return f"{scheme}://{netloc}{path}{query}".rstrip("/")
    except Exception:
        pass
    return text.rstrip("/").lower()


def response_url_differs_from_request(request_url: str, final_url: str) -> bool:
    """True when the response URL is a real redirect target (Swift `finalUrl != searchUrl`).

    `requests` re-quotes the request URL (`resp.url`), so a plain string compare would call every URL with raw
    non-ASCII characters a redirect; the request URL is re-quoted the same way before comparing. `url,{options}`
    carriers compare by their URL part."""
    request_part = parse_url_options(request_url or "")[0].strip()
    final_part = str(final_url or "").strip()
    if app_profiles.current_app_profile().redirect_guard_ignores_url_fragment:
        # R158 (G2, 1.1.2 `redirect_guard_ignores_url_fragment`; wenxue88 `https://host#{{key}}`): a fragment is never sent, so a response whose URL
        # differs from the request only by it (`https://host#我的` vs `https://host/#%E6%88%91...`, `requests` re-quotes and adds the path slash)
        # is not a redirect to a landing page. Both sides lose their fragment, then the usual compare runs.
        request_part = request_part.split("#", 1)[0]
        final_part = final_part.split("#", 1)[0]
    # R155b (P3-5): `http://host` and `http://host/` are the same resource, not a redirect.
    request_part = request_part.rstrip("/")
    final_part = final_part.rstrip("/")
    if final_part == request_part:
        return False
    if app_profiles.current_app_profile().redirect_guard_ignores_url_fragment and unquote(final_part) == unquote(request_part):
        return False  # R158: URLs that differ only by percent-encoding are the same URL (App 1.1.2 guard)
    try:
        from requests.utils import requote_uri

        if final_part == requote_uri(request_part).rstrip("/"):
            return False
    except Exception:
        pass
    return True


def is_likely_search_or_explore_landing_url(url: str, runtime: RuleRuntime, *, reject_search_endpoint: bool = False) -> bool:
    candidate = normalized_book_candidate_url_for_comparison(url)
    if not candidate:
        return False
    landing_urls = {
        normalized_book_candidate_url_for_comparison(runtime.source_url),
        normalized_book_candidate_url_for_comparison(runtime.base_url),
        normalized_book_candidate_url_for_comparison(runtime.book_source_url),
    }
    if candidate in {x for x in landing_urls if x}:
        return True
    if reject_search_endpoint and is_search_endpoint_url(url) and any(is_same_site_url(url, landing) for landing in (runtime.source_url, runtime.base_url, runtime.book_source_url)):
        return True
    return any(is_same_source_root_landing_url(url, landing) for landing in (runtime.source_url, runtime.base_url, runtime.book_source_url))


def is_same_site_url(candidate_url: str, landing_url: str) -> bool:
    try:
        candidate = urlparse(parse_url_options(candidate_url or "")[0])
        landing = urlparse(parse_url_options(landing_url or "")[0])
    except Exception:
        return False
    candidate_host = (candidate.hostname or candidate.netloc or "").lower()
    landing_host = (landing.hostname or landing.netloc or "").lower()
    if not candidate_host or not landing_host:
        return False
    return candidate_host == landing_host or ".".join(candidate_host.split(".")[-2:]) == ".".join(landing_host.split(".")[-2:])


def is_search_endpoint_url(url: str) -> bool:
    url_part, _ = parse_url_options(url or "")
    parsed = urlparse(url_part)
    path = (parsed.path or "").lower()
    if re.match(r"^/tags(?:[-_]\d+)?/[^/]+/\d+\.(?:html?|shtml)$", path):
        return True
    last = path.rstrip("/").rsplit("/", 1)[-1] if path.strip("/") else ""
    common_search_paths = {"ar.php", "search.php", "search.html", "search.htm", "s.php", "s.html", "s.htm", "so.php"}
    if last in common_search_paths or re.search(r"(?:^|/)search(?:/|$|\.)", path):
        return True
    query = parse_qs(parsed.query or "", keep_blank_values=True)
    if not query:
        return False
    query_keys = {key.lower() for key in query}
    if not (query_keys & {"key", "keyword", "searchkey", "searchword", "q", "wd", "word"}):
        return False
    return True


def is_same_source_root_landing_url(candidate_url: str, landing_url: str) -> bool:
    try:
        candidate_part, _ = parse_url_options(candidate_url or "")
        landing_part, _ = parse_url_options(landing_url or "")
        candidate = urlparse(candidate_part)
        landing = urlparse(landing_part)
    except Exception:
        return False
    candidate_host = (candidate.hostname or candidate.netloc or "").lower()
    landing_host = (landing.hostname or landing.netloc or "").lower()
    if not candidate_host or not landing_host:
        return False
    same_host = candidate_host == landing_host
    same_site = same_host or ".".join(candidate_host.split(".")[-2:]) == ".".join(landing_host.split(".")[-2:])
    candidate_path = (candidate.path or "").strip("/").lower()
    landing_path = (landing.path or "").strip("/").lower()
    root_index_paths = {
        "", "index.html", "index.htm", "index.php", "default.html", "default.aspx",
        "wap", "wap/", "wap/index.php", "wap/index.html", "m", "mobile", "mobile/index.php",
        "mobile/index.html", "mobile/mobile.php",
    }
    if not same_host and (not same_site or candidate_path not in root_index_paths):
        return False
    if candidate_path not in root_index_paths or landing_path not in root_index_paths:
        return False
    if candidate_path not in {"", "index.html", "index.htm", "index.php", "default.html", "default.aspx"}:
        return True
    if not candidate.query:
        return True
    tracking_keys = {"from", "fromid", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "spm"}
    query = parse_qs(candidate.query, keep_blank_values=True)
    return bool(query) and set(query).issubset(tracking_keys)


def normalize_book_candidate_url_to_source_mirror(candidate_url: str, source_url: str) -> str:
    text = (candidate_url or "").strip()
    if not text or text.startswith(("{", "[")):
        return text
    url_part, opts = parse_url_options(text)
    try:
        candidate = urlparse(url_part)
        source = urlparse(effective_base_url(source_url or ""))
    except Exception:
        return text
    if candidate.scheme not in {"http", "https"} or source.scheme not in {"http", "https"}:
        return text
    if not candidate.netloc or not source.netloc or candidate.netloc.lower() == source.netloc.lower():
        return text
    source_host = (source.hostname or source.netloc or "").lower()
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", source_host):
        return text
    source_segments = [seg for seg in (source.path or "").split("/") if seg]
    if not source_segments or "." not in source_segments[0]:
        return text
    mirror_prefix = "/" + "/".join(source_segments)
    candidate_path = candidate.path or "/"
    if candidate_path == "/" or candidate_path.startswith(mirror_prefix + "/"):
        return text
    merged_path = mirror_prefix.rstrip("/") + "/" + candidate_path.lstrip("/")
    merged = urlunparse((source.scheme, source.netloc, merged_path, "", candidate.query, candidate.fragment))
    return compose_url_with_options(merged, opts)


def compose_url_with_options(url: str, opts: dict[str, Any]) -> str:
    if not opts:
        return url
    return f"{url},{json.dumps(opts, ensure_ascii=False, separators=(',', ':'))}"


def has_valid_base_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def source_has_absolute_entry_point(src: dict[str, Any]) -> bool:
    for value in (src.get("searchUrl"), build_first_explore_url(str(src.get("exploreUrl") or ""), RuleRuntime(
        source_url="",
        base_url="",
        book_source_url="",
        headers={},
        js_lib=src.get("jsLib"),
    ))):
        text = str(value or "").strip().lower()
        if text.startswith(("http://", "https://", "@js:", "<js>")):
            return True
    return False


def fallback_base_url_from_entry_point(src: dict[str, Any], base_url: str) -> str:
    if has_valid_base_url(base_url):
        return base_url
    search = str(src.get("searchUrl") or "").strip()
    candidates = [search]
    explore = str(src.get("exploreUrl") or "").strip()
    if explore:
        candidates.extend(url for _, url in parse_explore_urls(explore, str(src.get("bookSourceName") or "")))
    for candidate in candidates:
        match = re.search(r"https?://[^\s'\"<>`{},]+", candidate, flags=re.I)
        if not match:
            continue
        parsed = urlparse(match.group(0))
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/"
    return base_url


def score_candidate(src: dict[str, Any]) -> int:
    raw_tag = src.get("customTag")
    tag = raw_tag.strip() if isinstance(raw_tag, str) else ""
    if "完整验证通过" in tag:
        validation = 4
    elif "模板修复" in tag:
        validation = 3
    elif "验证通过" in tag:
        validation = 2
    elif "连通性测试" in tag:
        validation = 1
    else:
        validation = 0

    completeness = 0
    if str(src.get("searchUrl") or "").strip():
        completeness += 1
    if str(src.get("exploreUrl") or "").strip():
        completeness += 1
    if isinstance(src.get("ruleSearch"), dict) and str(src["ruleSearch"].get("bookList") or "").strip():
        completeness += 2
    if isinstance(src.get("ruleExplore"), dict) and str(src["ruleExplore"].get("bookList") or "").strip():
        completeness += 2
    if isinstance(src.get("ruleBookInfo"), dict):
        completeness += 2

    raw_weight = src.get("weight")
    try:
        weight = int(float(str(raw_weight))) if raw_weight not in (None, "") else 0
    except (TypeError, ValueError, OverflowError):
        weight = 0
    return validation * 10_000 + completeness * 100 + weight


def group_sources(sources: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group import records for validation.

    Records sharing the exact serialized ``bookSourceUrl`` (the Legado/iOS
    primary key) always share a group. Different URLs collapse only when the
    canonical site (case, default port, ``www.``, trailing slash, query order
    or a plain ``#fragment``) and the behaviour rule fingerprint both match;
    Legado ``##tag`` identities stay distinct. Groups are keyed by the best
    candidate's serialized URL.
    """
    by_dedupe_key: dict[str, list[dict[str, Any]]] = {}
    dedupe_key_for_url: dict[str, str] = {}
    for src in sources:
        url = normalize_book_source_url(str(src.get("bookSourceUrl") or ""))
        if not url:
            continue
        key = dedupe_key_for_url.get(url)
        if key is None:
            key = source_dedupe_key(src)
            dedupe_key_for_url[url] = key
        by_dedupe_key.setdefault(key, []).append(src)
    groups: dict[str, list[dict[str, Any]]] = {}
    for items in by_dedupe_key.values():
        items.sort(key=score_candidate, reverse=True)
        groups[normalize_book_source_url(str(items[0].get("bookSourceUrl") or ""))] = items
    return groups


def parse_headers(header_text: str | None, runtime: RuleRuntime) -> dict[str, str]:
    raw = (header_text or "").strip()
    if not raw:
        return {}
    evaluated = resolve_template(raw, runtime)
    evaluated = execute_js_block_if_needed(evaluated, runtime)
    dict_headers = parse_json_dict(evaluated)
    if dict_headers:
        flattened = flatten_headers(dict_headers)
        if flattened:
            return flattened
    lines = {}
    for line in evaluated.splitlines():
        parsed = parse_header_line(line)
        if parsed:
            key, value = parsed
            lines[key] = value
    if lines:
        return lines
    if looks_like_user_agent_header(evaluated):
        return {"User-Agent": evaluated.strip()}
    return {}


def parse_header_line(line: str) -> tuple[str, str] | None:
    text = line.strip()
    if not text or ":" not in text:
        return None
    key, value = text.split(":", 1)
    key = key.strip().strip("\"'")
    value = value.strip().strip("\"'")
    if not key or not value:
        return None
    return key, value


def parse_json_dict(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    try:
        normalized = re.sub(r"\btrue\b", "True", text, flags=re.I)
        normalized = re.sub(r"\bfalse\b", "False", normalized, flags=re.I)
        normalized = re.sub(r"\bnull\b", "None", normalized, flags=re.I)
        obj = ast.literal_eval(normalized)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    try:
        obj = json.loads(normalize_legado_lenient_json(text))
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return None


def normalize_legado_lenient_json(raw: str) -> str:
    """Normalize the data-only JSON-like syntax used by Legado source configs."""

    quoted: list[str] = []
    index = 0
    while index < len(raw):
        ch = raw[index]
        if ch not in ("'", '"'):
            quoted.append(ch)
            index += 1
            continue
        delimiter = ch
        quoted.append('"')
        index += 1
        while index < len(raw):
            inner = raw[index]
            index += 1
            if inner == "\\" and index < len(raw):
                escaped = raw[index]
                index += 1
                if delimiter == "'" and escaped == "'":
                    quoted.append("'")
                elif escaped == '"':
                    quoted.append('\\"')
                elif escaped in "\\/bfnrtu":
                    quoted.extend(("\\", escaped))
                else:
                    quoted.extend(("\\\\", escaped))
                continue
            if inner == delimiter:
                quoted.append('"')
                break
            if inner == '"':
                quoted.append('\\"')
            elif inner == "\n":
                quoted.append("\\n")
            elif inner == "\r":
                quoted.append("\\r")
            elif inner == "\t":
                quoted.append("\\t")
            elif ord(inner) < 0x20:
                quoted.append(f"\\u{ord(inner):04X}")
            else:
                quoted.append(inner)

    source = "".join(quoted)
    output: list[str] = []
    index = 0
    in_string = False
    while index < len(source):
        ch = source[index]
        if in_string:
            output.append(ch)
            if ch == "\\" and index + 1 < len(source):
                index += 1
                output.append(source[index])
            elif ch == '"':
                in_string = False
            index += 1
            continue
        if ch == '"':
            in_string = True
            output.append(ch)
            index += 1
            continue
        if source.startswith("//", index):
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            index = len(source) if end < 0 else end + 2
            continue
        if ch == ",":
            lookahead = index + 1
            while lookahead < len(source) and source[lookahead].isspace():
                lookahead += 1
            if lookahead < len(source) and source[lookahead] in "}]":
                index += 1
                continue
        if ch in "{,":
            output.append(ch)
            index += 1
            while index < len(source) and source[index].isspace():
                output.append(source[index])
                index += 1
            match = re.match(r"[A-Za-z_$][\w$]*", source[index:])
            if match:
                key = match.group(0)
                key_end = index + len(key)
                colon = key_end
                while colon < len(source) and source[colon].isspace():
                    colon += 1
                if colon < len(source) and source[colon] == ":":
                    output.extend(('"', key, '"'))
                    output.append(source[key_end:colon])
                    index = colon
                    continue
            continue
        output.append(ch)
        index += 1

    keyed = "".join(output)
    output = []
    index = 0
    in_string = False
    while index < len(keyed):
        ch = keyed[index]
        if in_string:
            output.append(ch)
            if ch == "\\" and index + 1 < len(keyed):
                index += 1
                output.append(keyed[index])
            elif ch == '"':
                in_string = False
            index += 1
            continue
        if ch == '"':
            in_string = True
            output.append(ch)
            index += 1
            continue
        if ch != ":":
            output.append(ch)
            index += 1
            continue
        output.append(ch)
        index += 1
        while index < len(keyed) and keyed[index].isspace():
            output.append(keyed[index])
            index += 1
        if index >= len(keyed) or keyed[index] in '"{[':
            continue
        value_start = index
        while index < len(keyed) and keyed[index] not in ",}]":
            index += 1
        raw_value = keyed[value_start:index]
        trimmed = raw_value.strip()
        trailing = raw_value[len(raw_value.rstrip()):]
        lowered = trimmed.lower()
        if lowered in ("true", "false", "null"):
            output.append(lowered)
        elif re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", trimmed):
            output.append(trimmed)
        elif trimmed:
            output.append(json.dumps(trimmed, ensure_ascii=False))
        else:
            output.append(raw_value)
        output.append(trailing)
    return "".join(output)


def leading_json_object_prefix(raw: str) -> str:
    text = (raw or "").lstrip()
    if not text.startswith("{"):
        return text.strip()
    depth = 0
    in_single = False
    in_double = False
    escaped = False
    for index, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and (in_single or in_double):
            escaped = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if in_single or in_double:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[: index + 1].strip()
    return text.strip()


def flatten_headers(obj: dict[str, Any]) -> dict[str, str]:
    if isinstance(obj.get("headers"), dict):
        return flatten_headers(obj["headers"])  # type: ignore[arg-type]
    out: dict[str, str] = {}
    for key, value in obj.items():
        if isinstance(value, str):
            out[key] = value
        elif isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = str(value)
    return out


def looks_like_user_agent_header(header: str) -> bool:
    lower = header.lower()
    if "mozilla/" in lower or "okhttp/" in lower or "applewebkit/" in lower:
        return True
    return ":" not in header and "/" in header and len(header) >= 12


def _resolve_js_template_expressions(
    text: str,
    runtime: RuleRuntime,
    keyword: str | None = None,
    page: int = 1,
    input_text: str = "",
) -> str:
    """Resolve {{expr}} JS expression templates that aren't simple variable lookups."""
    if "{{" not in text:
        return text

    def resolve_java_put_match(inner: str) -> str | None:
        match = re.fullmatch(
            r"""java\.put\(\s*['"]([^'"]+)['"]\s*,\s*(baseUrl|sourceUrl|bookSourceUrl)\.match\(/((?:\\/|[^/])*)/[a-z]*\)\[(\d+)\]\s*\)\s*;?""",
            inner,
        )
        if not match:
            return None
        key, source_name, pattern, index_text = match.groups()
        source_value = {
            "baseUrl": runtime.base_url,
            "sourceUrl": runtime.source_url,
            "bookSourceUrl": runtime.book_source_url,
        }.get(source_name, "")
        regex = pattern.replace(r"\/", "/").replace(r"\\/", "/")
        try:
            found = re.search(regex, source_value)
            if not found:
                value = ""
            else:
                index = int(index_text)
                value = found.group(index) if index <= len(found.groups()) else ""
        except Exception:
            value = ""
        runtime.variables = runtime.variables or {}
        runtime.variables[key] = value
        return value

    def resolve_source_match(inner: str) -> str | None:
        match = re.fullmatch(
            r"""(baseUrl|sourceUrl|bookSourceUrl)\.match\(/((?:\\/|[^/])*)/[a-z]*\)\[(\d+)\]\s*;?""",
            inner,
        )
        if not match:
            return None
        source_name, pattern, index_text = match.groups()
        source_value = {
            "baseUrl": runtime.base_url,
            "sourceUrl": runtime.source_url,
            "bookSourceUrl": runtime.book_source_url,
        }.get(source_name, "")
        regex = pattern.replace(r"\/", "/").replace(r"\\/", "/")
        try:
            found = re.search(regex, source_value)
            if not found:
                return query_param_match_fallback(regex, source_value, int(index_text))
            index = int(index_text)
            return found.group(index) if index <= len(found.groups()) else ""
        except Exception:
            return ""

    def query_param_match_fallback(regex: str, source_value: str, index: int) -> str:
        if index != 1:
            return ""
        match = re.fullmatch(r"([A-Za-z_][\w-]*)=\(\\d\+\)", regex.strip())
        if not match:
            return ""
        key = match.group(1).lower()
        try:
            parsed = urlparse(source_value)
            query = parse_qs(parsed.query or "", keep_blank_values=False)
        except Exception:
            query = {}
        for query_key, values in query.items():
            if query_key.lower() != key:
                continue
            value = next((str(v) for v in values if str(v).strip()), "")
            digits = re.sub(r"\D+", "", value)
            if digits:
                return digits
        value = str((runtime.variables or {}).get(key) or "")
        return re.sub(r"\D+", "", value)

    def resolve_source_replace(inner: str) -> str | None:
        match = re.fullmatch(
            r"""(baseUrl|sourceUrl|bookSourceUrl)\.replace\(\s*(['"])(.*?)\2\s*,\s*(['"])(.*?)\4\s*\)\s*;?""",
            inner,
        )
        if not match:
            return None
        source_name, _old_quote, old, _new_quote, new = match.groups()
        source_value = {
            "baseUrl": runtime.base_url,
            "sourceUrl": runtime.source_url,
            "bookSourceUrl": runtime.book_source_url,
        }.get(source_name, "")
        old = old.replace(r"\/", "/").replace(r"\\", "\\")
        new = new.replace(r"\/", "/").replace(r"\\", "\\")
        if old == "":
            return source_value
        return source_value.replace(old, new, 1)

    def split_js_args(arg_text: str) -> list[str]:
        args: list[str] = []
        current: list[str] = []
        depth = 0
        quote_char = ""
        escape = False
        for ch in arg_text:
            if quote_char:
                current.append(ch)
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote_char:
                    quote_char = ""
                continue
            if ch in {"'", '"'}:
                quote_char = ch
                current.append(ch)
                continue
            if ch == "(":
                depth += 1
            elif ch == ")" and depth > 0:
                depth -= 1
            if ch == "," and depth == 0:
                args.append("".join(current).strip())
                current = []
                continue
            current.append(ch)
        if current or arg_text.strip():
            args.append("".join(current).strip())
        return args

    def decode_js_string_literal(value: str) -> str | None:
        text = value.strip()
        if len(text) < 2 or text[0] not in {"'", '"'} or text[-1] != text[0]:
            return None
        try:
            return bytes(text[1:-1], "utf-8").decode("unicode_escape")
        except Exception:
            return text[1:-1]

    def resolve_java_encode_uri_value(expr: str) -> str | None:
        text = expr.strip().rstrip(";").strip()
        nested = resolve_java_encode_uri(text)
        if nested is not None:
            return nested
        literal = decode_js_string_literal(text)
        if literal is not None:
            return literal
        low = text.lower()
        if low in {"key", "keyword", "rawkeyword"}:
            return keyword if keyword is not None else None
        if low == "searchkey":
            return encode_keyword(keyword) if keyword is not None else None
        if text in (runtime.variables or {}):
            return str((runtime.variables or {}).get(text) or "")
        return None

    def resolve_java_encode_uri(inner: str) -> str | None:
        match = re.fullmatch(r"""java\.encodeURI(?:Component)?\(([\s\S]*)\)\s*;?""", inner.strip())
        if not match:
            return None
        args = split_js_args(match.group(1))
        if not args:
            return ""
        value = resolve_java_encode_uri_value(args[0])
        if value is None:
            return None
        charset = None
        if len(args) > 1:
            charset = decode_js_string_literal(args[1]) or args[1].strip()
        return encode_form_keyword(value, charset)

    def replace_expr(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if inner.lower().startswith("@css:"):
            if not input_text:
                return match.group(0)
            values = evaluate_rule(inner, input_text, runtime, "json" if is_json_content(input_text) else "html")
            if values and values[0].strip():
                return values[0].strip()
            attr_match = re.fullmatch(r"@css:([A-Za-z_][\w:-]*)@text", inner, flags=re.I)
            if attr_match:
                try:
                    soup = BeautifulSoup(input_text or "", "html.parser")
                    first = soup.find()
                    if first is not None:
                        value = first.get(attr_match.group(1))
                        if value is not None:
                            return str(value)
                except Exception:
                    pass
            return ""
        # Skip simple variable names (handled by resolve_template)
        if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", inner):
            return match.group(0)
        # Skip JSONPath patterns
        if inner.startswith("$") or inner.startswith("."):
            return match.group(0)
        # Skip embedded Legado makeUpRule snippets; evaluate_rule resolves them
        # against the current document after fixed template replacement.
        if inner.startswith("@@"):
            return match.group(0)
        java_put = resolve_java_put_match(inner)
        if java_put is not None:
            return java_put
        source_match = resolve_source_match(inner)
        if source_match is not None:
            return source_match
        source_replace = resolve_source_replace(inner)
        if source_replace is not None:
            return source_replace
        java_encode_uri = resolve_java_encode_uri(inner)
        if java_encode_uri is not None:
            return java_encode_uri
        # This is a JS expression — evaluate it
        fast_ctx = None
        try:
            ctx = make_quickjs_context()
            if ctx is None:
                raise RuntimeError("quickjs unavailable")
            fast_ctx = ctx
            ctx.set("page", page)
            ctx.set("pageIndex", page - 1)
            ctx.set("pagePlus1", page + 1)
            ctx.set("pageSize", 20)
            ctx.set("baseUrl", runtime.base_url)
            ctx.set("sourceUrl", runtime.source_url)
            # R156 (searchjs R): `_build_java_api_js` references `bookSourceUrl`; unbound it raised a ReferenceError that the
            # blanket `except` below swallowed, so every expression fell through to the slow path.
            ctx.set("bookSourceUrl", runtime.book_source_url)
            ctx.set("result", input_text)
            ctx.set("src", input_text)
            ctx.set("input", input_text)
            if keyword is not None:
                ctx.set("key", keyword)
                ctx.set("keyword", keyword)
                ctx.set("searchKey", encode_keyword(keyword))
            # Inject java stub
            install_quickjs_app_globals(ctx, runtime)
            java_api = _build_java_api_js(runtime)
            ctx.eval(java_api)
            # No jsLib on this path: the plan only decides the profile's CryptoJS for the expression itself.
            fast_plan = app_profiles.app_js_setup_plan("", inner)
            install_quickjs_app_cryptojs(ctx, fast_plan, "jslib")
            install_quickjs_app_cryptojs(ctx, fast_plan, "code")
            result = ctx.eval(f"String({inner})")
            if result is not None and str(result) != "undefined":
                text_result = str(result)
                if text_result or not js_requires_node_crypto(inner):
                    return text_result
        except Exception:
            pass
        finally:
            if fast_ctx is not None:
                drain_quickjs_interactions(fast_ctx, runtime)
        # R156 (searchjs R): Legado AnalyzeUrl.kt:185-193 replaces `{{js}}` with the script result even when it is the empty string
        # (`index{{page - 1 == 0 ? '' : '_' + page}}.html` on page 1). An empty result counts only when the script raised no
        # error; the stage sink is swapped for a local one to tell the two apart and its entries are forwarded afterwards.
        outer_sink = getattr(runtime, "script_error_sink", None)
        local_sink: list = []
        runtime.script_error_sink = local_sink
        try:
            result = evaluate_js(
                inner,
                input_text,
                runtime,
                extra_vars={
                    "key": keyword or "",
                    "keyword": keyword or "",
                    "rawKeyword": keyword or "",
                    "searchKey": encode_keyword(keyword or ""),
                    "page": page,
                    "pageIndex": page - 1,
                    "pagePlus1": page + 1,
                },
            )
            if outer_sink is not None:
                outer_sink.extend(local_sink)
            if result != inner and (result or (result == "" and not local_sink)):
                return result
        except Exception:
            if outer_sink is not None:
                outer_sink.extend(local_sink)
        finally:
            runtime.script_error_sink = outer_sink
        return match.group(0)

    # R150 (B11): RuleType.evalPattern `\{\{[\w\W]*?\}\}` (RuleType.swift:413-416, RuleParser.templateParts): the inner text
    # runs to the FIRST `}}` and may itself contain single `}` / newlines (`{"headers": {...} }` inside a template
    # literal, `if (x) { ... }`). The old `[^}]+` stopped at the first `}` and left such templates unevaluated.
    return _LEGADO_RULE_TEMPLATE_PATTERN.sub(replace_expr, text)


def resolve_template(
    template: str,
    runtime: RuleRuntime,
    keyword: str | None = None,
    page: int = 1,
    raw_keyword: str | None = None,
    rule_template: bool = False,
    input_text: str = "",
) -> str:
    """Substitute URL/rule placeholders.

    ``rule_template`` is Legado makeUpRule for rules: only double-brace
    ``{{name}}`` placeholders expand; a single-brace ``{name}`` is literal rule
    text (``{$.…}`` inner JSONPath rules are handled by the rule engine).
    ``input_text`` is what ``result`` is inside a ``{{js}}`` expression (RuleEvaluator.swift:716-720 evaluates the
    expression with the rule's input as ``result``); rule evaluation passes the content being analysed.
    """
    out = template
    replacements = {
        "{{baseUrl}}": runtime.base_url,
        "{{sourceUrl}}": runtime.source_url,
        "{{bookSourceUrl}}": runtime.book_source_url,
        "{{page}}": str(page),
        "{{pageNum}}": str(page),
        "{{pageIndex}}": str(page - 1),
        "{{pagePlus1}}": str(page + 1),
        "{page}": str(page),
        "{pageNum}": str(page),
        "{pageIndex}": str(page - 1),
        "{pagePlus1}": str(page + 1),
        "{{pageSize}}": "20",
        "{pageSize}": "20",
    }
    if runtime.book_kind:
        replacements.update({
            "{{book.kind}}": runtime.book_kind,
            "{book.kind}": runtime.book_kind,
            "{{bookKind}}": runtime.book_kind,
            "{bookKind}": runtime.book_kind,
        })
    if runtime.book_name:
        replacements.update({"{{book.name}}": runtime.book_name, "{book.name}": runtime.book_name})
    if runtime.book_author:
        replacements.update({"{{book.author}}": runtime.book_author, "{book.author}": runtime.book_author})
    if keyword is not None:
        encoded = encode_keyword(keyword)
        raw = raw_keyword if raw_keyword is not None else keyword
        out = apply_keyword_index_templates(out, keyword=keyword, encoded_charset=None, raw_keyword=raw)
        replacements.update({
            "{{searchKey}}": encoded,
            "{{key}}": encoded,
            "{{keyword}}": encoded,
            "{searchKey}": encoded,
            "{key}": encoded,
            "{keyword}": encoded,
            "{{rawKeyword}}": raw,
            "{rawKeyword}": raw,
        })
    for key, value in replacements.items():
        if rule_template and not key.startswith("{{"):
            continue
        if "${" in out and not key.startswith("{{"):
            # R156 (searchjs Q): `${page}` / `${key}` is a JS template literal placeholder, not a legacy `{page}` token (the App
            # has no single-brace replacement; `$1` left the POST body of "笔趣小说" with `page=$1`).
            out = re.sub(r"(?<!\$)" + re.escape(key), lambda _match, _value=value: _value, out)
        else:
            out = out.replace(key, value)
    for key, value in (runtime.variables or {}).items():
        replacement = str(value).lower() if isinstance(value, bool) else str(value)
        out = out.replace(f"{{{{{key}}}}}", replacement)
    # Resolve remaining JS expression templates like {{Math.round(...)}}
    if "{{" in out:
        out = _resolve_js_template_expressions(out, runtime, keyword=keyword, page=page, input_text=input_text)
    return out


def apply_keyword_index_templates(template: str, keyword: str, encoded_charset: str | None = None, raw_keyword: str | None = None) -> str:
    text = template or ""
    if "[" not in text or "key" not in text.lower() and "keyword" not in text.lower():
        return text
    raw = raw_keyword if raw_keyword is not None else keyword

    def char_at(value: str, raw_index: str) -> str:
        try:
            index = int(raw_index)
        except Exception:
            return ""
        chars = list(value or "")
        if index < 0:
            index = len(chars) + index
        if index < 0 or index >= len(chars):
            return ""
        return chars[index]

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = char_at(raw, match.group(2))
        if not value:
            return ""
        if name.lower() == "rawkeyword":
            return value
        return encode_keyword(value, encoded_charset)

    return re.sub(r"\{\{(key|keyword|searchKey|rawKeyword)\[(-?\d+)\]\}\}", replace, text, flags=re.I)


def substitute_json_placeholders(rule: str, content: str) -> str:
    text = rule
    if not contains_json_placeholder(text):
        return text
    json_text = extract_json(content)
    if not json_text:
        return text
    try:
        obj = json.loads(json_text)
    except Exception:
        return text

    def resolve_jsonpath_value(path_expr: str) -> str:
        path = path_expr.strip()
        if "||" in path:
            for part in _split_rule_operator(path, "||"):
                value = resolve_jsonpath_value(part)
                if value:
                    return value
            return ""
        replace_chain = ""
        if "##" in path:
            path, replace_chain = path.split("##", 1)
            path = path.strip()
        if path.startswith("."):
            path = "$" + path
        elif not path.startswith(("$", "[")):
            path = "$." + path
        try:
            expr = parse_jsonpath_expression(path)
            matches = [m.value for m in expr.find(obj)]
        except Exception:
            matches = []
        id_like_paths = {
            "$.id", "$._id", "$.book", "$.bookId", "$.bookid", "$.book_id",
            "$.novelId", "$.novelid", "$.novel_id", "$.bid", "$.resourceID",
            "$.resourceId", "$.resourceid", "$.ios_id", "$.IOS_id",
            "$.book.id", "$.book.bookId", "$.book.book_id",
            "$.data.id", "$.data.bookId", "$.data.book_id",
        }
        if not matches and path in id_like_paths:
            for alias in [
                "$.bookId", "$.bookid", "$.book_id", "$.book", "$.id", "$._id",
                "$.novelId", "$.novelid", "$.novel_id", "$.bid",
                "$.resourceID", "$.resourceId", "$.resourceid", "$.ios_id", "$.IOS_id",
                "$.book.bookId", "$.book.book_id", "$.book.id",
                "$.data.bookId", "$.data.book_id", "$.data.id",
            ]:
                try:
                    expr = parse_jsonpath_expression(alias)
                    matches = [m.value for m in expr.find(obj)]
                except Exception:
                    matches = []
                if matches:
                    break
        if not matches:
            return ""
        value = matches[0]
        if isinstance(value, (dict, list)):
            try:
                text_value = json.dumps(value, ensure_ascii=False)
            except Exception:
                text_value = str(value)
        else:
            text_value = "" if value is None else str(value)
        if replace_chain:
            text_value = legado_replace_regex(text_value, replace_chain)
        return text_value

    # First handle double-brace {{$.xxx}} pattern (legado makeUpRule / jsRuleType behavior):
    # {{$.novelId}} → evaluate $.novelId as JSONPath, substitute entire {{$.novelId}}
    def replace_double_brace(match: re.Match[str]) -> str:
        # group(1) is the part after {$ e.g. ".novelId"
        inner = match.group(1).strip()
        if not is_jsonpath_placeholder_expr(inner):
            return match.group(0)
        return resolve_jsonpath_value("$" + inner if inner.startswith(".") or not inner.startswith(("$", "[")) else inner)

    if contains_json_placeholder(text):
        text = re.sub(r"\{\{\s*(\$[^}]*)\s*\}\}", replace_double_brace, text)

    # Then handle single-brace {$.xxx} (legado innerRule behavior)
    if contains_json_placeholder(text):
        def replace_single_brace(match: re.Match[str]) -> str:
            expr_text = match.group(1).strip()
            if not expr_text:
                return ""
            if not is_jsonpath_placeholder_expr(expr_text):
                return match.group(0)
            return resolve_jsonpath_value(expr_text)

        text = re.sub(r"\{\s*(\$[^}]+)\s*\}", replace_single_brace, text)

    return text


def root_attr_from_item(item: str, attr: str) -> str:
    try:
        soup = BeautifulSoup(item or "", "html.parser")
        first = soup.find()
        if first is None:
            return ""
        value = first.get(attr)
        return str(value) if value is not None else ""
    except Exception:
        return ""


def child_tag_text_from_item(item: str, tag: str) -> str:
    try:
        soup = BeautifulSoup(item or "", "html.parser")
        found = soup.find(tag)
        return found.get_text(strip=True) if found is not None else ""
    except Exception:
        return ""


def substitute_css_placeholders(rule: str, content: str, runtime: RuleRuntime, content_type: str) -> str:
    if "{{" not in rule or "@css:" not in rule:
        return rule

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner.lower().startswith("@css:"):
            return match.group(0)
        tag_match = re.fullmatch(r"@css:([A-Za-z_][\w:-]*)@text", inner, flags=re.I)
        if tag_match:
            value = child_tag_text_from_item(content, tag_match.group(1))
            if value:
                return value
            value = root_attr_from_item(content, tag_match.group(1))
            if value:
                return value
        values = evaluate_rule(inner, content, runtime, content_type)
        if values and values[0].strip():
            return values[0].strip()
        return ""

    return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", replace, rule)


def _has_double_brace_json_placeholder(text: str) -> bool:
    return re.search(r"\{\{\s*\$(?:\.|\[)[^}]*\}\}", text or "") is not None


def contains_json_placeholder(text: str) -> bool:
    return re.search(r"\{\{?\s*\$(?:\.|\[)", text or "") is not None


def is_literal_json_template_rule(text: str) -> bool:
    """Whether a JSONPath placeholder rule is a composed literal result.

    A rule such as ``/detail?id={{$.id}}`` or
    ``{{$.source}} {{$.last_chapter_title}}`` is a text template in Legado,
    not a CSS selector after substitution. Explicit rule operators outside the
    placeholders still opt into the ordinary rule pipeline.
    """
    raw = text or ""
    if not contains_json_placeholder(raw):
        return False
    skeleton = re.sub(r"\{\{?\s*\$[^}]*\}\}?", "", raw)
    lowered = skeleton.lower()
    return not any(
        marker in lowered
        for marker in ("@js:", "<js>", "@css:", "@xpath:", "@json:", "||", "&&", "##", "@put:")
    )


def is_jsonpath_placeholder_expr(text: str) -> bool:
    value = (text or "").strip()
    return value.startswith("$.") or value.startswith("$[")


def substitute_makeup_rule_templates(rule: str, content: str, runtime: RuleRuntime, content_type: str = "html") -> str:
    """Resolve embedded Legado makeUpRule templates like {{@@#rating@data-novel-id}}."""
    text = rule or ""
    if "{{@@" not in text:
        return text

    def replace_makeup_rule(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner.startswith("@@"):
            return match.group(0)
        values = evaluate_rule(inner[2:], content, runtime, content_type)
        return values[0].strip() if values else ""

    return re.sub(r"\{\{\s*(@@[\s\S]*?)\s*\}\}", replace_makeup_rule, text)


def encode_keyword(keyword: str, charset: str | None = None) -> str:
    if charset:
        try:
            return quote(keyword.encode(charset, errors="ignore"))
        except Exception:
            pass
    return quote_plus(keyword)


def encode_form_keyword(keyword: str, charset: str | None = None) -> str:
    """Match java.net.URLEncoder for application/x-www-form-urlencoded values."""
    if not keyword:
        return ""
    try:
        raw = keyword.encode(charset or "utf-8", errors="ignore")
    except LookupError:
        raw = keyword.encode("utf-8")
    return quote_plus(raw, safe=b".-*_")


def _has_explicit_content_type(headers: dict[str, Any] | None) -> bool:
    return any(
        str(key).lower() == "content-type" and bool(str(value).strip())
        for key, value in (headers or {}).items()
    )


def _is_json_or_xml_request_body(body: str) -> bool:
    text = (body or "").strip()
    if not text:
        return False
    try:
        json.loads(text)
        return True
    except Exception:
        pass
    return text.startswith("<") and text.endswith(">")


def _request_body_looks_structured(body: Any) -> bool:
    """App NetworkManager.swift `bodyLooksStructured`: trimmed body wrapped in `{}` / `[]` / `<>`."""
    text = str(body or "").strip()
    return (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")) or (
        text.startswith("<") and text.endswith(">")
    )


def _is_already_legado_form_encoded(value: str) -> bool:
    safe = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789*-._")
    index = 0
    while index < len(value):
        char = value[index]
        if char in safe:
            index += 1
            continue
        if char == "%" and index + 2 < len(value):
            pair = value[index + 1 : index + 3]
            if all(item in "0123456789abcdefABCDEF" for item in pair):
                index += 3
                continue
        return False
    return True


def _legado_escape(value: str) -> str:
    safe = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@*_+-./")
    chunks: list[str] = []
    for char in value:
        code = ord(char)
        if char in safe:
            chunks.append(char)
        elif code <= 0xFF:
            chunks.append(f"%{code:02X}")
        elif code <= 0xFFFF:
            chunks.append(f"%u{code:04X}")
        else:
            adjusted = code - 0x10000
            chunks.append(f"%u{0xD800 + (adjusted >> 10):04X}%u{0xDC00 + (adjusted & 0x3FF):04X}")
    return "".join(chunks)


def _encode_legado_form_component(value: str, charset: str | None) -> str:
    normalized = (charset or "").strip()
    if not normalized and _is_already_legado_form_encoded(value):
        return value
    if normalized.lower() == "escape":
        return _legado_escape(value)
    try:
        raw = value.encode(normalized or "utf-8", errors="strict")
    except (LookupError, UnicodeEncodeError):
        raw = value.encode("utf-8")
    return quote_plus(raw, safe=b"*-._")


def encode_legado_form_body(body: str, charset: str | None = None) -> str:
    """Encode each form key/value like AnalyzeUrl.encodeParams, preserving separators."""
    fields: list[str] = []
    for field in (body or "").split("&"):
        key, separator, value = field.partition("=")
        encoded_key = _encode_legado_form_component(key, charset)
        if not separator:
            fields.append(encoded_key)
        else:
            fields.append(f"{encoded_key}={_encode_legado_form_component(value, charset)}")
    return "&".join(fields)


def encode_percent_body(template: str, runtime: RuleRuntime, keyword: str, page: int) -> str:
    return resolve_template(template, runtime, keyword=keyword, page=page, raw_keyword=keyword)


def normalize_legacy_template(raw: str) -> str:
    replacements = [
        ("{{page - 1}}", "{{pageIndex}}"),
        ("{{page-1}}", "{{pageIndex}}"),
        ("{page-1}", "{{pageIndex}}"),
        ("{{page + 1}}", "{{pagePlus1}}"),
        ("{{page+1}}", "{{pagePlus1}}"),
        ("{page+1}", "{{pagePlus1}}"),
        ("<,PageIndex=", "&PageIndex="),
    ]
    out = raw
    for old, new in replacements:
        out = out.replace(old, new)
    return out


def strip_leading_cookie_side_effect_template(raw: str) -> str:
    text = (raw or "").strip()
    if not text.startswith("{{") or "cookie" not in text.lower():
        return raw
    match = re.match(r"^\s*\{\{([\s\S]*?)\}\}\s*", raw)
    if not match:
        return raw
    body = match.group(1).strip()
    lower = body.lower()
    # A value template such as `{{cookie.getCookie(url)}}/path` contributes to
    # the URL and must stay. A leading statement block (`url=...;` followed by
    # remove/put cookie) only mutates source state and Legado continues with the
    # request text after `}}`; exporting that block across lines must not leak
    # JavaScript into the URL.
    is_direct_cookie_statement = lower.startswith("cookie.")
    has_cookie_mutation = re.search(
        r"\bcookie\.(?:removeCookie|put|setCookie|remove)\s*\(",
        body,
        flags=re.IGNORECASE,
    ) is not None
    if is_direct_cookie_statement or has_cookie_mutation:
        return raw[match.end() :].strip()
    return raw


def extract_likely_book_id(book_url: str) -> str:
    raw = (book_url or "").strip()
    if not raw:
        return ""
    m = re.search(r"\{([^{}]+)\}", raw)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"/(?:novel|book)/([^/?#{}]+)", raw, flags=re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()

    try:
        parsed = urlparse(raw)
        q = {k: (v[-1] if isinstance(v, list) and v else "") for k, v in parse_qs(parsed.query).items()}
        for key in ["bookid", "bookId", "book_id", "id", "resourceid", "resourceId", "novelid", "novelId", "bid"]:
            val = (q.get(key) or "").strip()
            if val:
                return val
        for seg in reversed([s for s in (parsed.path or "").split("/") if s]):
            if re.fullmatch(r"\d+", seg):
                return seg
            m2 = re.search(r"\d+", seg)
            if m2:
                return m2.group(0)
    except Exception:
        pass
    return ""


def normalize_book_linked_template_url(raw_url: str, book_url: str, toc_url: str = "") -> str:
    out = (raw_url or "").strip()
    if not out:
        return out
    book = (book_url or "").strip()
    toc = (toc_url or "").strip()
    if book:
        out = out.replace("{{bookUrl}}", book).replace("{bookUrl}", book)
        out = out.replace("{{book.url}}", book).replace("{book.url}", book)
    if toc:
        out = out.replace("{{tocUrl}}", toc).replace("{tocUrl}", toc)
        out = out.replace("{{bookTocUrl}}", toc).replace("{bookTocUrl}", toc)
    book_id = extract_likely_book_id(book)
    if book_id:
        out = out.replace("{}", book_id)
        out = out.replace("{{$.novelId}}", book_id)
        out = out.replace("{{$.bookId}}", book_id)
        out = out.replace("{{bookId}}", book_id).replace("{bookId}", book_id)
    # Only reject if there are clearly unresolved template placeholders
    # Don't reject URLs that contain < or > from legitimate HTML entity encoding
    if re.search(r"\{\{[^}]*\}\}", out):
        return ""
    out = collapse_concatenated_absolute_url(out)
    return out


def collapse_concatenated_absolute_url(url: str) -> str:
    text = (url or "").strip()
    match = re.match(r"^(https?://[^/\s?#]+)(https?://.+)$", text, flags=re.I | re.S)
    if match:
        return match.group(2).strip()
    return text


def apply_page_expression_templates(raw: str, page: int) -> str:
    pattern = re.compile(r"\{\{\s*\(?\s*page\s*([+\-])?\s*(\d+)?\s*\)?\s*(?:\*\s*(\d+))?\s*\}\}", re.I)

    def repl(match: re.Match[str]) -> str:
        op = match.group(1) or ""
        delta_raw = match.group(2) or ""
        mul_raw = match.group(3) or ""
        value = page
        if delta_raw and op:
            delta = int(delta_raw)
            value = page - delta if op == "-" else page + delta
        if mul_raw:
            value *= int(mul_raw)
        return str(value)

    return pattern.sub(repl, raw)


def apply_legado_page_segments(raw: str, page: int, requires_comma: bool = False) -> str:
    if not raw or "<" not in raw or ">" not in raw:
        return raw

    def repl(match: re.Match[str]) -> str:
        if requires_comma:
            # POST body (App applyLegadoPageSegments, requiresComma): only obvious page segments; `</x>`, `<?xml ...?>`, `<!-- -->`
            # and start tags with attributes (`<root a="x,y">`) are XML.
            body = match.group(1)
            if "," not in body or body[:1] in ("/", "?", "!") or re.match(r"[A-Za-z][\w:.-]*\s", body):
                return match.group(0)
        segments = match.group(1).split(",")
        if not segments:
            return match.group(0)
        idx = max(0, min(max(1, page) - 1, len(segments) - 1))
        return segments[idx].strip()

    return re.sub(r"<([^<>]*)>", repl, raw)


def extract_first_http_url(text: str) -> str | None:
    m = re.search(r"https?://[^\s'\"<>`]+", text, flags=re.I)
    return m.group(0) if m else None


def _build_java_api_js(runtime: RuleRuntime) -> str:
    """构建 java.* 对象的 JS 代码，对齐 Legado AnalyzeRule/java 桥接 API"""
    import base64
    import hashlib
    java_api_js = r"""
function __readoriBufferFromUtf8(value) {
    if (typeof Buffer !== 'undefined') return Buffer.from(String(value || ''), 'utf8');
    return String(value || '');
}
function __readoriNormalizeAesKey(value) {
    if (typeof Buffer === 'undefined') return String(value || '');
    var buf = Buffer.isBuffer(value) ? value : __readoriBufferFromUtf8(value);
    var lengths = [32, 24, 16];
    for (var i = 0; i < lengths.length; i++) {
        if (buf.length >= lengths[i]) return buf.subarray(0, lengths[i]);
    }
    var out = Buffer.alloc(16);
    buf.copy(out, 0, 0, Math.min(buf.length, 16));
    return out;
}
function __readoriNormalizeAesIv(value) {
    if (typeof Buffer === 'undefined') return String(value || '');
    var buf = Buffer.isBuffer(value) ? value : __readoriBufferFromUtf8(value);
    if (buf.length >= 16) return buf.subarray(0, 16);
    var out = Buffer.alloc(16);
    buf.copy(out, 0, 0, Math.min(buf.length, 16));
    return out;
}
function __readoriHmacAlgorithm(algorithm) {
    var text = String(algorithm || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (text.indexOf('MD5') >= 0) return 'md5';
    if (text.indexOf('SHA512') >= 0) return 'sha512';
    if (text.indexOf('SHA384') >= 0) return 'sha384';
    if (text.indexOf('SHA224') >= 0) return 'sha224';
    if (text.indexOf('SHA1') >= 0) return 'sha1';
    return 'sha256';
}
function __readoriPercentEncode(value, charset) {
    var text = String(value || '');
    var normalizedCharset = String(charset || '').toLowerCase();
    if (/gbk|gb2312|gb18030/.test(normalizedCharset)) {
        try {
            if (typeof __readoriPercentEncodeNative === 'function') {
                return __readoriPercentEncodeNative(text, normalizedCharset);
            }
        } catch (error) {}
        try {
            if (typeof __readoriPercentEncodeMap !== 'undefined' && __readoriPercentEncodeMap) {
                var charsetMap = __readoriPercentEncodeMap[normalizedCharset] || __readoriPercentEncodeMap.gbk || {};
                if (Object.prototype.hasOwnProperty.call(charsetMap, text)) return charsetMap[text];
            }
        } catch (error) {}
    }
    if (typeof encodeURIComponent === 'function') {
        return encodeURIComponent(text)
            .replace(/%20/g, '+')
            .replace(/[!'()~]/g, function(character) {
                return '%' + character.charCodeAt(0).toString(16).toUpperCase();
            });
    }
    return text;
}
function __readoriBase64ToBuffer(value) {
    if (typeof Buffer === 'undefined') return String(value || '');
    var text = String(value || '').replace(/-/g, '+').replace(/_/g, '/');
    var pad = text.length % 4;
    if (pad) text += '='.repeat(4 - pad);
    return Buffer.from(text, 'base64');
}
function __readoriBufferFromBytes(value) {
    if (typeof Buffer === 'undefined') return value;
    if (!value) return Buffer.alloc(0);
    if (Buffer.isBuffer(value)) return value;
    if (value.value !== undefined) return __readoriBufferFromBytes(value.value);
    if (typeof value === 'string' || Object.prototype.toString.call(value) === '[object String]') {
        return Buffer.from(String(value || ''), 'utf8');
    }
    if (typeof value.toArray === 'function') value = value.toArray();
    if (Array.isArray(value) || typeof value.length === 'number') {
        var out = [];
        for (var i = 0; i < value.length; i++) out.push(Number(value[i]) & 255);
        return Buffer.from(out);
    }
    return Buffer.from(String(value || ''), 'utf8');
}
if (typeof Array.prototype.toArray !== 'function') {
    // R158 (E, BOTH profiles): Rhino's NativeJavaList enumerates only the indices (`for (i in list)`), and the App installs this helper with
    // _readoriDefineHidden (JSEngine.swift `Array.prototype.toArray`, already in the 1.1.1 build: `git show HEAD:Readori/Core/Engine/JSEngine.swift`
    // ~8686); an enumerable one made `for (i in $)` visit "toArray" as the last key (czbooks: `$[i].select` is not a function). The other
    // list helpers keep their `js_list_helpers_non_enumerable` gating.
    Object.defineProperty(Array.prototype, 'toArray', { value: function(){ return this; }, writable: true, configurable: true, enumerable: false });
}
if (typeof String.prototype.getBytes !== 'function') {
    String.prototype.getBytes = function(charset) {
        var value = String(this);
        if (typeof Buffer !== 'undefined') {
            return __readoriSignedBytes(Array.from(Buffer.from(value, /gbk|gb2312|gb18030/i.test(String(charset || '')) ? 'binary' : 'utf8')));
        }
        var out = [];
        for (var i = 0; i < value.length; i++) out.push(value.charCodeAt(i) & 255);
        return __readoriSignedBytes(out);
    };
}
// R143 (S1 = A): App batch validation blocks interaction (allowsUserInteraction=false). The runtime sets
// __readoriAllowsUserInteraction; directly constructed runtimes leave it true/undefined and keep the legacy behavior.
// AnalyzeRule.content: the input until java.setContent replaces it (the `src` binding itself never changes).
var __readoriContent;
function __readoriCurrentContent() {
    if (typeof __readoriContent !== 'undefined') return __readoriContent;
    return typeof src !== 'undefined' ? src : result;
}
function __readoriInteractionBlocked(kind) {
    if (typeof __readoriAllowsUserInteraction !== 'undefined' && !__readoriAllowsUserInteraction) {
        try {
            if (kind && typeof __readoriRecordSuppressedInteraction === 'function') {
                __readoriRecordSuppressedInteraction(kind);
            }
        } catch (error) {}
        return true;
    }
    return false;
}
// Binary-safe (latin1) base64 for the prelude itself: the App-semantics global atob/btoa are UTF-8 and would
// corrupt binary data.
var __readoriB64Alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
function __readoriLatin1Btoa(input) {
    var s = String(input === undefined || input === null ? '' : input);
    var out = '';
    for (var i = 0; i < s.length; i += 3) {
        var hasB = i + 1 < s.length;
        var hasC = i + 2 < s.length;
        var n = ((s.charCodeAt(i) & 255) << 16)
            | ((hasB ? s.charCodeAt(i + 1) & 255 : 0) << 8)
            | (hasC ? s.charCodeAt(i + 2) & 255 : 0);
        out += __readoriB64Alphabet.charAt((n >> 18) & 63)
            + __readoriB64Alphabet.charAt((n >> 12) & 63)
            + (hasB ? __readoriB64Alphabet.charAt((n >> 6) & 63) : '=')
            + (hasC ? __readoriB64Alphabet.charAt(n & 63) : '=');
    }
    return out;
}
function __readoriLatin1Atob(input) {
    var s = String(input === undefined || input === null ? '' : input).replace(/[^A-Za-z0-9+\/]/g, '');
    var out = '';
    var buffer = 0;
    var bits = 0;
    for (var i = 0; i < s.length; i++) {
        buffer = (buffer << 6) | __readoriB64Alphabet.indexOf(s.charAt(i));
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out += String.fromCharCode((buffer >> bits) & 255);
            buffer &= (1 << bits) - 1;
        }
    }
    return out;
}
function __readoriToByteArray(value) {
    if (!value) return [];
    // (a string has `toArray` too - the App's String.prototype shim - but it is text to encode, not a Java list)
    if (typeof value !== 'string' && typeof value.toArray === 'function') value = value.toArray();
    if (typeof value === 'string') {
        if (typeof Buffer !== 'undefined') return Array.from(Buffer.from(value, 'utf8'));
        var stringOut = [];
        for (var s = 0; s < value.length; s++) stringOut.push(value.charCodeAt(s) & 255);
        return stringOut;
    }
    if (typeof value.length === 'number') {
        var out = [];
        for (var i = 0; i < value.length; i++) out.push(Number(value[i]) & 255);
        out.toArray = function(){ return this; };
        return out;
    }
    return __readoriToByteArray(String(value));
}
function __readoriBytesToUtf8(value) {
    var bytes = __readoriToByteArray(value);
    if (typeof Buffer !== 'undefined') return Buffer.from(bytes).toString('utf8');
    var s = '';
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i] & 255);
    try { return decodeURIComponent(escape(s)); } catch(e) { return s; }
}
function __readoriCharsetName(charset) {
    var value = String(charset || 'utf-8').trim().toLowerCase().replace(/_/g, '-');
    if (!value || value === 'utf8') return 'utf-8';
    if (value === 'gb2312' || value === 'gb18030') return 'gbk';
    if (value === 'latin1' || value === 'iso8859-1') return 'iso-8859-1';
    if (value === 'sjis' || value === 'shift-jis') return 'shift_jis';
    return value;
}
function __readoriStringToBytes(value, charset) {
    var text = String(value === undefined || value === null ? '' : value);
    var normalized = __readoriCharsetName(charset);
    if (typeof __readoriEncodeBytesNative === 'function') {
        try {
            var nativeBytes = JSON.parse(__readoriEncodeBytesNative(text, normalized) || '[]');
            return __readoriByteArrayWithUtf8String(nativeBytes);
        } catch (error) {}
    }
    if (typeof Buffer !== 'undefined') {
        if (normalized === 'utf-8') return __readoriByteArrayWithUtf8String(Array.from(Buffer.from(text, 'utf8')));
        if (normalized === 'utf-16le') return __readoriByteArrayWithUtf8String(Array.from(Buffer.from(text, 'utf16le')));
        if (normalized === 'ascii') return __readoriByteArrayWithUtf8String(Array.from(Buffer.from(text, 'ascii')));
        if (normalized === 'iso-8859-1') return __readoriByteArrayWithUtf8String(Array.from(Buffer.from(text, 'latin1')));
        try {
            var encodedMap = (typeof __readoriPercentEncodeMap !== 'undefined' && __readoriPercentEncodeMap)
                ? (__readoriPercentEncodeMap[normalized] || {}) : {};
            var encoded = encodedMap[text];
            if (encoded !== undefined) {
                var mapped = [];
                for (var p = 0; p < encoded.length; p++) {
                    if (encoded[p] === '%' && p + 2 < encoded.length) {
                        mapped.push(parseInt(encoded.slice(p + 1, p + 3), 16) & 255);
                        p += 2;
                    } else if (encoded[p] === '+') {
                        mapped.push(32);
                    } else {
                        mapped.push(encoded.charCodeAt(p) & 255);
                    }
                }
                return __readoriByteArrayWithUtf8String(mapped);
            }
        } catch (error) {}
        return __readoriByteArrayWithUtf8String(Array.from(Buffer.from(text, 'utf8')));
    }
    var escaped = encodeURIComponent(text);
    var out = [];
    for (var i = 0; i < escaped.length; i++) {
        if (escaped[i] === '%' && i + 2 < escaped.length) {
            out.push(parseInt(escaped.slice(i + 1, i + 3), 16) & 255);
            i += 2;
        } else {
            out.push(escaped.charCodeAt(i) & 255);
        }
    }
    return __readoriByteArrayWithUtf8String(out);
}
function __readoriBytesToString(value, charset) {
    var bytes = __readoriToByteArray(value);
    var normalized = __readoriCharsetName(charset);
    if (typeof __readoriDecodeBytesNative === 'function') {
        try { return String(__readoriDecodeBytesNative(JSON.stringify(bytes), normalized)); } catch (error) {}
    }
    if (typeof Buffer !== 'undefined') {
        if (normalized === 'utf-8') return Buffer.from(bytes).toString('utf8');
        if (normalized === 'utf-16le') return Buffer.from(bytes).toString('utf16le');
        if (normalized === 'ascii') return Buffer.from(bytes).toString('ascii');
        if (normalized === 'iso-8859-1') return Buffer.from(bytes).toString('latin1');
        try {
            if (typeof TextDecoder !== 'undefined') {
                return new TextDecoder(normalized).decode(Uint8Array.from(bytes));
            }
        } catch (error) {}
    }
    return __readoriBytesToUtf8(bytes);
}
function __readoriHexToByteArray(value) {
    var clean = String(value || '').replace(/0x/ig, '').replace(/[^0-9a-f]/ig, '');
    var out = [];
    for (var i = 0; i < clean.length; i += 2) {
        var pair = clean.slice(i, i + 2);
        if (!pair) continue;
        var byte = parseInt(pair, 16);
        if (!isNaN(byte)) out.push(byte > 127 ? byte - 256 : byte);
    }
    out.toArray = function(){ return this; };
    return out;
}
// Java byte[] result: signed elements (-128..127) whose toString() decodes UTF-8.
// Inputs everywhere are normalised with `Number(v) & 255`.
function __readoriByteArrayWithUtf8String(value) {
    var out = __readoriToByteArray(value).map(function(v){ return v > 127 ? v - 256 : v; });
    out.toArray = function(){ return this; };
    out.toString = function(){ return __readoriBytesToUtf8(this); };
    return out;
}
function __readoriSignedBytes(bytes) {
    var out = __readoriToByteArray(bytes).map(function(v){
        v = Number(v) || 0;
        return v > 127 ? v - 256 : v;
    });
    out.toArray = function(){ return this; };
    return out;
}
function __readoriHexToSignedBytes(hex) {
    var out = [];
    hex = String(hex || '').replace(/[^0-9a-f]/gi, '');
    for (var i = 0; i + 1 < hex.length; i += 2) {
        var v = parseInt(hex.substring(i, i + 2), 16) || 0;
        out.push(v > 127 ? v - 256 : v);
    }
    out.toArray = function(){ return this; };
    return out;
}
function StringBuilder(initial) {
    this.parts = [];
    if (initial !== undefined && initial !== null) this.parts.push(String(initial));
    this.append = function(value){ this.parts.push(String(value)); return this; };
    this.toString = function(){ return this.parts.join(''); };
}
var Integer = {
    toHexString: function(value) {
        var n = Number(value) || 0;
        if (n < 0) n = 0xffffffff + n + 1;
        return Math.floor(n).toString(16);
    },
    parseInt: function(value, radix) { return parseInt(String(value), radix || 10); }
};
function JavaString(value, charset) {
    var text = value && typeof value.length === 'number' && typeof value !== 'string'
        ? __readoriBytesToUtf8(value)
        : String(value === undefined || value === null ? '' : value);
    if (!(this instanceof JavaString)) return text;
    return new String(text);
}
JavaString.fromCharCode = String.fromCharCode;
JavaString.prototype = String.prototype;
var Thread = {
    sleep: function(ms) { return ''; }
};
var URLEncoder = {
    encode: function(value, charset) {
        try { return __readoriPercentEncode(value, charset).replace(/%20/g, '+'); } catch (error) {}
        return encodeURIComponent(String(value === undefined || value === null ? '' : value)).replace(/%20/g, '+');
    }
};
var UUID = {
    randomUUID: function() {
        var value = (typeof __readoriCrypto !== 'undefined' && __readoriCrypto.randomUUID)
            ? __readoriCrypto.randomUUID()
            : '00000000-0000-4000-8000-000000000000';
        return { toString: function(){ return value; }, valueOf: function(){ return value; } };
    }
};
var AndroidBuild = {
    MANUFACTURER: 'Apple',
    MODEL: 'iPhone'
};
var MessageDigest = {
    getInstance: function(algorithm) {
        return {
            algorithm: String(algorithm || 'MD5'),
            digest: function(bytes) {
                if (typeof __readoriCrypto !== 'undefined' && __readoriCrypto.createHash) {
                    var alg = /^sha-?256$/i.test(this.algorithm) ? 'sha256' : String(this.algorithm || 'md5').toLowerCase();
                    return __readoriHexToSignedBytes(
                        __readoriCrypto.createHash(alg).update(Buffer.from(__readoriToByteArray(bytes))).digest('hex')
                    );
                }
                return __readoriSignedBytes(bytes);
            }
        };
    }
};
var Base64 = {
    decode: function(value, flags) {
        var raw = (typeof value === 'string') ? value : __readoriBytesToUtf8(value);
        raw = String(raw || '').replace(/-/g, '+').replace(/_/g, '/').replace(/\s+/g, '');
        while (raw.length % 4) raw += '=';
        var bytes = typeof Buffer !== 'undefined' ? Array.from(Buffer.from(raw, 'base64')) : __readoriToByteArray(__readoriLatin1Atob(raw));
        return __readoriSignedBytes(bytes);
    },
    getDecoder: function() {
        return { decode: function(value) { return Base64.decode(value, 0); } };
    },
    getEncoder: function() {
        return {
            encodeToString: function(value) {
                var bytes = __readoriToByteArray(value);
                if (typeof Buffer !== 'undefined') return Buffer.from(bytes).toString('base64');
                var s = '';
                for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i] & 255);
                return __readoriLatin1Btoa(s);
            }
        };
    },
    encodeToString: function(value, flags) {
        var encoded = Base64.getEncoder().encodeToString(value);
        flags = Number(flags || 0);
        if ((flags & 8) !== 0) encoded = encoded.replace(/\+/g, '-').replace(/\//g, '_');
        if ((flags & 1) !== 0) encoded = encoded.replace(/=+$/g, '');
        if ((flags & 2) === 0 && encoded) {
            var separator = (flags & 4) !== 0 ? '\r\n' : '\n';
            encoded = encoded.match(/.{1,76}/g).join(separator) + separator;
        }
        return encoded;
    },
    NO_PADDING: 1,
    NO_WRAP: 2,
    CRLF: 4,
    URL_SAFE: 8,
    DEFAULT: 0
};
var Arrays = {
    copyOfRange: function(arr, start, end) {
        return Array.prototype.slice.call(arr || [], start || 0, end == null ? (arr || []).length : end);
    }
};
function SecretKeySpec(value, alg) { return { value: value, algorithm: alg }; }
function IvParameterSpec(value) { return { value: value }; }
function DESKeySpec(value) { return { value: value, algorithm: 'DES' }; }
var SecretKeyFactory = {
    getInstance: function(alg) {
        return {
            algorithm: String(alg || 'DES'),
            generateSecret: function(spec) {
                return SecretKeySpec(spec && spec.value !== undefined ? spec.value : spec, this.algorithm);
            }
        };
    }
};
var Cipher = {
    ENCRYPT_MODE: 1,
    DECRYPT_MODE: 2,
    getInstance: function(alg) {
        return {
            algorithm: String(alg || 'AES/CBC/PKCS5Padding'),
            init: function(mode, key, iv) { this.mode = Number(mode) || 2; this.key = key; this.iv = iv; },
            doFinal: function(bytes) {
                if (typeof Buffer === 'undefined' || typeof __readoriCrypto === 'undefined' || !__readoriCrypto.createDecipheriv) {
                    return __readoriSignedBytes(bytes);
                }
                try {
                    var keyBuf = __readoriNormalizeCipherKey(this.algorithm, this.key && this.key.value !== undefined ? this.key.value : this.key);
                    var ivBuf = __readoriNormalizeCipherIv(this.algorithm, this.iv && this.iv.value !== undefined ? this.iv.value : this.iv);
                    var fn = (this.mode === 1 && __readoriCrypto.createCipheriv) ? __readoriCrypto.createCipheriv : __readoriCrypto.createDecipheriv;
                    var cipher = fn(__readoriCipherAlgorithm(this.algorithm, keyBuf), keyBuf, ivBuf);
                    cipher.setAutoPadding(String(this.algorithm || '').toUpperCase().indexOf('NOPADDING') < 0);
                    var out = Buffer.concat([cipher.update(__readoriBufferFromBytes(bytes)), cipher.final()]);
                    return __readoriByteArrayWithUtf8String(Array.from(out));
                } catch (error) {
                    return __readoriByteArrayWithUtf8String(bytes);
                }
            }
        };
    }
};
var Mac = {
    getInstance: function(alg) {
        return {
            algorithm: String(alg || ''),
            init: function(key) { this.key = key; },
            doFinal: function(bytes) {
                if (/HmacSHA256/i.test(this.algorithm) && typeof __readoriCrypto !== 'undefined' && __readoriCrypto.createHmac) {
                    var keyBytes = this.key && this.key.value !== undefined ? this.key.value : this.key;
                    var out = __readoriCrypto.createHmac('sha256', __readoriBufferFromBytes(keyBytes))
                        .update(__readoriBufferFromBytes(bytes))
                        .digest();
                    return __readoriSignedBytes(Array.from(out));
                }
                return __readoriSignedBytes(bytes);
            }
        };
    }
};
var Signature = {
    getInstance: function(alg) {
        return {
            algorithm: String(alg || ''),
            initSign: function(key) { this.key = key; },
            initVerify: function(key) { this.key = key; },
            update: function(bytes) {
                this.bytes = (this.bytes || []).concat(__readoriToByteArray(bytes));
            },
            sign: function() {
                if (/SHA256withRSA/i.test(this.algorithm) && typeof __readoriCrypto !== 'undefined' && __readoriCrypto.sign) {
                    try {
                        var keyBytes = this.key && this.key.value !== undefined ? this.key.value : this.key;
                        var keyObject = __readoriCrypto.createPrivateKey({
                            key: __readoriBufferFromBytes(keyBytes),
                            format: 'der',
                            type: 'pkcs8'
                        });
                        var out = __readoriCrypto.sign('RSA-SHA256', Buffer.from(__readoriToByteArray(this.bytes || [])), keyObject);
                        return __readoriSignedBytes(Array.from(out));
                    } catch (error) {
                        return [];
                    }
                }
                return [];
            },
            verify: function() { return false; }
        };
    }
};
var KeyFactory = {
    getInstance: function(alg) {
        return {
            algorithm: String(alg || ''),
            generatePrivate: function(spec) { return { value: spec && spec.value !== undefined ? spec.value : spec, algorithm: this.algorithm }; },
            generatePublic: function(spec) { return { value: spec && spec.value !== undefined ? spec.value : spec, algorithm: this.algorithm }; }
        };
    }
};
function PKCS8EncodedKeySpec(value) { return { value: value }; }
function X509EncodedKeySpec(value) { return { value: value }; }
function ByteArrayInputStream(bytes) {
    this.bytes = __readoriToByteArray(bytes);
    this.offset = 0;
    this.read = function(buffer) {
        if (arguments.length === 0 || buffer == null) {
            if (this.offset >= this.bytes.length) return -1;
            return this.bytes[this.offset++] & 255;
        }
        var target = __readoriToByteArray(buffer);
        var count = Math.min(target.length, this.bytes.length - this.offset);
        if (count <= 0) return -1;
        for (var i = 0; i < count; i++) {
            target[i] = this.bytes[this.offset++] & 255;
            buffer[i] = target[i] > 127 ? target[i] - 256 : target[i];
        }
        return count;
    };
    this.close = function(){};
}
function __readoriBytesFromInputStream(stream) {
    if (!stream) return [];
    if (stream.bytes) return __readoriToByteArray(stream.bytes).slice(stream.offset || 0);
    var out = [], b;
    while ((b = stream.read()) !== -1) out.push(b & 255);
    out.toArray = function(){ return this; };
    return out;
}
function __readoriInflateBytes(value) {
    var bytes = __readoriToByteArray(value);
    if (typeof __readoriZlib !== 'undefined') {
        try {
            var inflated = __readoriZlib.inflateSync(Buffer.from(bytes));
            return __readoriSignedBytes(inflated);
        } catch (error) {
            try {
                var raw = __readoriZlib.inflateRawSync(Buffer.from(bytes));
                return __readoriSignedBytes(raw);
            } catch (inner) {}
        }
    }
    if (typeof __readoriInflateBytesBase64 === 'function') {
        try {
            var encoded = Base64.getEncoder().encodeToString(bytes);
            var decoded = Base64.decode(__readoriInflateBytesBase64(encoded), 0);
            if (decoded && decoded.length) return decoded;
        } catch (error) {}
    }
    return __readoriSignedBytes(bytes);
}
function __readoriGunzipBytes(value) {
    var bytes = __readoriToByteArray(value);
    if (typeof __readoriZlib !== 'undefined') {
        try {
            var gunzipped = __readoriZlib.gunzipSync(Buffer.from(bytes));
            return __readoriSignedBytes(gunzipped);
        } catch (error) {}
    }
    if (typeof __readoriGunzipBytesBase64 === 'function') {
        try {
            var encoded = Base64.getEncoder().encodeToString(bytes);
            var decoded = Base64.decode(__readoriGunzipBytesBase64(encoded), 0);
            if (decoded && decoded.length) return decoded;
        } catch (error) {}
    }
    return __readoriSignedBytes(bytes);
}
function InflaterInputStream(stream) {
    var input = __readoriBytesFromInputStream(stream);
    var decoded = [];
    try {
        if (typeof java !== 'undefined' && typeof java.inflateBytes === 'function') decoded = java.inflateBytes(input);
    } catch (error) {}
    ByteArrayInputStream.call(this, decoded && decoded.length ? decoded : input);
}
function GZIPInputStream(stream) {
    var input = __readoriBytesFromInputStream(stream);
    var decoded = [];
    try {
        if (typeof java !== 'undefined' && typeof java.gzipDecodeBytes === 'function') decoded = java.gzipDecodeBytes(input);
    } catch (error) {}
    ByteArrayInputStream.call(this, decoded && decoded.length ? decoded : input);
}
function ByteArrayOutputStream(size) {
    this.bytes = [];
    this.write = function(value, offset, length) {
        if (arguments.length >= 3) {
            var arr = __readoriToByteArray(value);
            var start = Number(offset) || 0;
            var end = Math.min(arr.length, start + (Number(length) || 0));
            for (var i = start; i < end; i++) this.bytes.push(arr[i] & 255);
        } else {
            this.bytes.push(Number(value) & 255);
        }
    };
    this.toByteArray = function(){ return __readoriSignedBytes(this.bytes); };
    this.toString = function(){ return __readoriBytesToUtf8(this.bytes); };
    this.close = function(){};
}
var ZipUtil = {
    unGzip: function(value, charset) {
        var bytes = __readoriGunzipBytes(value);
        if (arguments.length > 1 && charset) return __readoriBytesToUtf8(bytes);
        return bytes;
    },
    unGzipBytes: function(value) {
        return __readoriGunzipBytes(value);
    }
};
function LinkedHashMap(value) {
    return value || {};
}
function JavaImporter() {
    return {
        importPackage: function(){},
        MessageDigest: MessageDigest,
        String: JavaString,
        StringBuilder: StringBuilder,
        Integer: Integer,
        Thread: Thread,
        Base64: Base64,
        Arrays: Arrays,
        UUID: UUID,
        LinkedHashMap: LinkedHashMap,
        URLEncoder: URLEncoder,
        ByteArrayInputStream: ByteArrayInputStream,
        ByteArrayOutputStream: ByteArrayOutputStream,
        InflaterInputStream: InflaterInputStream,
        GZIPInputStream: GZIPInputStream,
        SecretKeySpec: SecretKeySpec,
        IvParameterSpec: IvParameterSpec,
        DESKeySpec: DESKeySpec,
        SecretKeyFactory: SecretKeyFactory,
        Cipher: Cipher,
        Mac: Mac,
        Signature: Signature,
        KeyFactory: KeyFactory,
        PKCS8EncodedKeySpec: PKCS8EncodedKeySpec,
        X509EncodedKeySpec: X509EncodedKeySpec,
        ZipUtil: ZipUtil
    };
}
var Packages = {
    java: {
        lang: { String: JavaString, StringBuilder: StringBuilder, Integer: Integer, Thread: Thread },
        security: { MessageDigest: MessageDigest, Signature: Signature, KeyFactory: KeyFactory, spec: { PKCS8EncodedKeySpec: PKCS8EncodedKeySpec, X509EncodedKeySpec: X509EncodedKeySpec }, interfaces: {} },
        io: { ByteArrayInputStream: ByteArrayInputStream, ByteArrayOutputStream: ByteArrayOutputStream },
        util: { Arrays: Arrays, Base64: Base64, UUID: UUID, LinkedHashMap: LinkedHashMap, zip: { InflaterInputStream: InflaterInputStream, GZIPInputStream: GZIPInputStream } },
        net: { URLEncoder: URLEncoder }
    },
    javax: { crypto: { Cipher: Cipher, Mac: Mac, SecretKeyFactory: SecretKeyFactory, spec: { SecretKeySpec: SecretKeySpec, IvParameterSpec: IvParameterSpec, DESKeySpec: DESKeySpec } } },
    android: { util: { Base64: Base64 }, os: { Build: AndroidBuild } },
    cn: { hutool: { core: { util: { ZipUtil: ZipUtil } } } }
};
// R158 D1 (1.1.2 `js_hutool_base64_shim`; gongzicp jsLib `var base64 = Packages.cn.hutool.core.codec.Base64`): hutool is on Legado's classpath.
// A String argument is taken as UTF-8 bytes; encode / encodeStr / encodeUrlSafe (no padding) / encodeWithoutPadding answer a String (the
// 3-argument encode answers byte[]), decode answers byte[], decodeStr UTF-8 text. Mirrors App `JSEngine` HutoolBase64.
if (typeof __readoriHutoolBase64 !== 'undefined' && __readoriHutoolBase64) {
    Packages.cn.hutool.core.codec = { Base64: (function() {
        function utf8Bytes(value) {
            if (value === null || value === undefined) return [];
            if (typeof value === 'string' || Object.prototype.toString.call(value) === '[object String]') {
                var raw = unescape(encodeURIComponent(String(value)));
                var out = [];
                for (var i = 0; i < raw.length; i++) out.push(raw.charCodeAt(i) & 255);
                return out;
            }
            return __readoriToByteArray(value);
        }
        function encodeText(value, urlSafe, padded) {
            var text = Base64.getEncoder().encodeToString(utf8Bytes(value));
            if (urlSafe) text = text.replace(/\+/g, '-').replace(/\//g, '_');
            if (urlSafe || !padded) text = text.replace(/=+$/, '');
            return text;
        }
        function decodeBytes(value) {
            var text = (typeof value === 'string') ? value : __readoriBytesToUtf8(value);
            text = String(text || '').replace(/\s+/g, '').replace(/-/g, '+').replace(/_/g, '/').replace(/=+$/, '');
            while (text.length % 4 !== 0) text += '=';
            return Base64.decode(text, 0);
        }
        return {
            encode: function(value, isMultiLine, isUrlSafe) {
                var text = encodeText(value, arguments.length > 2 && !!isUrlSafe, true);
                if (arguments.length > 2) return __readoriSignedBytes(utf8Bytes(text));
                return text;
            },
            encodeStr: function(value, isMultiLine, isUrlSafe) { return encodeText(value, arguments.length > 2 && !!isUrlSafe, true); },
            encodeUrlSafe: function(value) { return encodeText(value, true, false); },
            encodeWithoutPadding: function(value) { return encodeText(value, false, false); },
            decode: function(value) { return decodeBytes(value); },
            decodeStr: function(value) { return __readoriBytesToUtf8(decodeBytes(value)); },
            isBase64: function(value) {
                var text = String(value === undefined || value === null ? '' : value);
                return text.length > 0 && /^[A-Za-z0-9+\/_\-\s]*={0,2}$/.test(text);
            }
        };
    })() };
}
// R158 (1.1.2 `js_str_response_shim`; asgportal loginCheckJs `Packages.io.legado.app.help.http.StrResponse(result.url(), body)`): Legado's
// StrResponse(url, body) constructor (StrResponse.kt: status 200, no headers) as a response object like `java.getStrResponse`.
if (typeof __readoriStrResponseShim !== 'undefined' && __readoriStrResponseShim) {
    // The first argument may also be an existing response object (its url(), like the App's shim); the namespace is merged, not replaced.
    Packages.io = Packages.io || {};
    Packages.io.legado = Packages.io.legado || {};
    Packages.io.legado.app = Packages.io.legado.app || {};
    Packages.io.legado.app.help = Packages.io.legado.app.help || {};
    Packages.io.legado.app.help.http = Packages.io.legado.app.help.http || {};
    Packages.io.legado.app.help.http.StrResponse = function(urlOrResponse, body) {
        var responseUrl = (urlOrResponse && typeof urlOrResponse.url === 'function')
            ? String(urlOrResponse.url() || '')
            : String(urlOrResponse === undefined || urlOrResponse === null ? '' : urlOrResponse);
        return __readoriResponseObject((body === undefined || body === null) ? '' : String(body), responseUrl, 200, {}, {});
    };
}
// R158 (1.1.2 `js_json_object_result_get`; asgportal `result.get("statusTips")`): a JSON-object `result` is a Java Map in Legado (AnalyzeRule
// binds the parsed JSON), so `get(key)` answers the value or null. Non-enumerable like the other helpers.
if (typeof __readoriJsonResultGet !== 'undefined' && __readoriJsonResultGet
    && typeof result === 'object' && result !== null && !Array.isArray(result) && typeof result.get === 'undefined') {
    try {
        Object.defineProperty(result, 'get', { value: function(key) { var v = this[key]; return v === undefined ? null : v; }, configurable: true, writable: true, enumerable: false });
    } catch (error) {}
}
function __readoriAesAlgorithm(mode, key) {
    var modeText = String(mode || 'AES/CBC/PKCS5Padding').toUpperCase();
    var blockMode = modeText.indexOf('/') < 0 || modeText.indexOf('/ECB/') >= 0 ? 'ecb' : 'cbc';
    var keyBits = (key && key.length ? key.length : 16) * 8;
    if (keyBits !== 128 && keyBits !== 192 && keyBits !== 256) keyBits = 128;
    return 'aes-' + keyBits + '-' + blockMode;
}
function __readoriSymmetricFamily(mode) {
    var algo = String(mode || 'AES/CBC/PKCS5Padding').toUpperCase().split('/')[0];
    if (algo.indexOf('RC4') === 0 || algo.indexOf('ARCFOUR') === 0) return 'RC4';
    if (algo.indexOf('BLOWFISH') === 0) return 'BLOWFISH';
    if (algo.indexOf('SM4') === 0) return 'SM4';
    return '';
}
function __readoriCipherAlgorithm(mode, key) {
    var modeText = String(mode || 'AES/CBC/PKCS5Padding').toUpperCase();
    var useEcb = modeText.indexOf('/') < 0 || modeText.indexOf('/ECB/') >= 0;
    var family = __readoriSymmetricFamily(modeText);
    if (family === 'RC4') {
        // Hutool/JCE RC4/ARCFOUR: stream cipher, no IV, no padding.
        return 'rc4';
    }
    if (family === 'BLOWFISH' || family === 'SM4') {
        var block = useEcb ? 'ecb' : String(modeText.split('/')[1] || 'ecb').toLowerCase().replace(/\d+$/, '');
        var allowed = family === 'SM4' ? ['ecb', 'cbc', 'cfb', 'ofb', 'ctr'] : ['ecb', 'cbc', 'cfb', 'ofb'];
        if (allowed.indexOf(block) < 0) throw new Error('Unsupported transformation: ' + modeText);
        return (family === 'SM4' ? 'sm4-' : 'bf-') + block;
    }
    if (modeText.indexOf('DESEDE') >= 0 || modeText.indexOf('3DES') >= 0) {
        return useEcb ? 'des-ede3' : 'des-ede3-cbc';
    }
    if (modeText.indexOf('DES') === 0) {
        return useEcb ? 'des-ecb' : 'des-cbc';
    }
    return __readoriAesAlgorithm(modeText, key);
}
function __readoriNormalizeCipherKey(mode, value) {
    if (typeof Buffer === 'undefined') return value;
    var modeText = String(mode || 'AES/CBC/PKCS5Padding').toUpperCase();
    var buf = __readoriBufferFromBytes(value);
    var family = __readoriSymmetricFamily(modeText);
    if (family === 'RC4') {
        if (buf.length < 1 || buf.length > 256) throw new Error('Invalid RC4 key length');
        return buf;
    }
    if (family === 'BLOWFISH') {
        // JCE Blowfish accepts 32..448-bit keys.
        if (buf.length < 4 || buf.length > 56) throw new Error('Invalid Blowfish key length');
        return buf;
    }
    if (family === 'SM4') {
        if (buf.length !== 16) throw new Error('Invalid SM4 key length');
        return buf;
    }
    var wanted = 16;
    if (modeText.indexOf('DESEDE') >= 0 || modeText.indexOf('3DES') >= 0) wanted = 24;
    else if (modeText.indexOf('DES') === 0) wanted = 8;
    else return __readoriNormalizeAesKey(buf);
    if (buf.length >= wanted) return buf.subarray(0, wanted);
    var out = Buffer.alloc(wanted);
    buf.copy(out, 0, 0, Math.min(buf.length, wanted));
    return out;
}
function __readoriNormalizeCipherIv(mode, value) {
    if (typeof Buffer === 'undefined') return value;
    var modeText = String(mode || 'AES/CBC/PKCS5Padding').toUpperCase();
    var family = __readoriSymmetricFamily(modeText);
    if (family === 'RC4') return null;
    if (modeText.indexOf('/') < 0 || modeText.indexOf('/ECB/') >= 0) return null;
    var wanted = (modeText.indexOf('DES') === 0 || family === 'BLOWFISH') ? 8 : 16;
    var buf = __readoriBufferFromBytes(value);
    if (buf.length >= wanted) return buf.subarray(0, wanted);
    var out = Buffer.alloc(wanted);
    buf.copy(out, 0, 0, Math.min(buf.length, wanted));
    return out;
}
function __readoriMapObject(values) {
    var out = {};
    values = values || {};
    for (var key in values) {
        if (Object.prototype.hasOwnProperty.call(values, key) && key !== 'get') {
            out[key] = values[key] === undefined || values[key] === null ? '' : String(values[key]);
        }
    }
    Object.defineProperty(out, 'get', {
        value: function(key) {
            key = String(key || '');
            return Object.prototype.hasOwnProperty.call(out, key) ? out[key] : '';
        },
        enumerable: false
    });
    Object.defineProperty(out, 'put', {
        value: function(key, value) {
            out[String(key || '')] = value === undefined || value === null ? '' : String(value);
            return value;
        },
        enumerable: false
    });
    Object.defineProperty(out, 'containsKey', {
        value: function(key) { return Object.prototype.hasOwnProperty.call(out, String(key || '')); },
        enumerable: false
    });
    Object.defineProperty(out, 'isEmpty', {
        value: function() { return Object.keys(out).length === 0; },
        enumerable: false
    });
    Object.defineProperty(out, 'toString', {
        value: function() { return JSON.stringify(out); },
        enumerable: false
    });
    return out;
}
function __readoriParseMapText(raw, nestedKey) {
    if (raw && typeof raw === 'object') return __readoriMapObject(raw);
    var text = String(raw || '').trim();
    if (!text) return __readoriMapObject({});
    try {
        var parsed = JSON.parse(text);
        if (parsed && typeof parsed === 'object') {
            if (nestedKey && parsed[nestedKey] && typeof parsed[nestedKey] === 'object') {
                return __readoriMapObject(parsed[nestedKey]);
            }
            return __readoriMapObject(parsed);
        }
    } catch (error) {}
    var values = {};
    String(text).split(/\r?\n/).forEach(function(line) {
        var idx = String(line || '').indexOf(':');
        if (idx <= 0) return;
        var key = line.slice(0, idx).trim();
        if (!key) return;
        values[key] = line.slice(idx + 1).trim();
    });
    return __readoriMapObject(values);
}
function __readoriMergeMapObjects() {
    var merged = {};
    for (var i = 0; i < arguments.length; i++) {
        var item = arguments[i] || {};
        for (var key in item) {
            if (Object.prototype.hasOwnProperty.call(item, key) && typeof item[key] !== 'function') {
                merged[key] = item[key];
            }
        }
    }
    return __readoriMapObject(merged);
}
function __readoriCacheKey(kind, key) {
    return '__cache.' + String(kind || 'value') + '.' + String(key || '');
}
function __readoriCacheDeadlineKey(kind, key) {
    return '__cache.deadline.' + String(kind || 'value') + '.' + String(key || '');
}
function __readoriCacheMemorySerialize(value) {
    try {
        var json = JSON.stringify(value);
        if (json !== undefined) return 'j:' + json;
    } catch (error) {}
    return 's:' + String(value);
}
function __readoriCacheMemoryDeserialize(value) {
    if (String(value).indexOf('j:') === 0) {
        try { return JSON.parse(String(value).slice(2)); } catch (error) { return null; }
    }
    return String(value).indexOf('s:') === 0 ? String(value).slice(2) : String(value);
}
function __readoriCacheGet(kind, key) {
    if (typeof variables === 'undefined') return null;
    var storageKey = __readoriCacheKey(kind, key);
    if (!Object.prototype.hasOwnProperty.call(variables, storageKey)) return null;
    var deadlineKey = __readoriCacheDeadlineKey(kind, key);
    var deadline = Number(variables[deadlineKey] || 0);
    if (deadline !== 0 && deadline <= Date.now()) {
        delete variables[storageKey];
        delete variables[deadlineKey];
        return null;
    }
    var value = variables[storageKey];
    return kind === 'memory' ? __readoriCacheMemoryDeserialize(value) : String(value);
}
function __readoriCachePut(kind, key, value, seconds) {
    if (typeof variables !== 'undefined') {
        var storageKey = __readoriCacheKey(kind, key);
        variables[storageKey] = kind === 'memory'
            ? __readoriCacheMemorySerialize(value)
            : String(value);
        var ttl = Number(seconds || 0);
        variables[__readoriCacheDeadlineKey(kind, key)] =
            kind === 'memory' || ttl === 0 ? '0' : String(Date.now() + ttl * 1000);
    }
}
function __readoriCacheDelete(kind, key) {
    if (typeof variables !== 'undefined') {
        delete variables[__readoriCacheKey(kind, key)];
        delete variables[__readoriCacheDeadlineKey(kind, key)];
    }
}
function __readoriCacheDeleteAll(key) {
    __readoriCacheDelete('value', key);
    __readoriCacheDelete('file', key);
    __readoriCacheDelete('memory', key);
    __readoriCacheDelete('bytes', key);
}
// Kotlin String.toIntOrNull/toLongOrNull: optional sign + digits, no trimming.
function __readoriCacheInteger(key, min, max) {
    var value = __readoriCacheGet('value', key);
    if (value === null || value === undefined) return null;
    var text = String(value);
    if (!/^[+-]?\d+$/.test(text)) return null;
    var number = Number(text);
    return (number >= min && number <= max) ? number : null;
}
// Kotlin String.toDoubleOrNull (Java Double.valueOf grammar, decimal forms).
function __readoriCacheDouble(key) {
    var value = __readoriCacheGet('value', key);
    if (value === null || value === undefined) return null;
    var text = String(value).trim();
    if (!/^[+-]?(NaN|Infinity|((\d+\.?\d*|\.\d+)([eE][+-]?\d+)?[fFdD]?))$/.test(text)) return null;
    return parseFloat(text.replace(/[fFdD]$/, ''));
}
function __readoriCacheIsByteArray(value) {
    if (!value || typeof value === 'string' || typeof value.length !== 'number' || value.length === 0) return false;
    for (var i = 0; i < value.length; i++) {
        var item = value[i];
        if (typeof item !== 'number' || item % 1 !== 0 || item < -128 || item > 255) return false;
    }
    return true;
}
function __readoriCacheClear() {
    if (typeof variables === 'undefined') return;
    for (var key in variables) {
        if (Object.prototype.hasOwnProperty.call(variables, key) && String(key).indexOf('__cache.') === 0) {
            delete variables[key];
        }
    }
}
function __readoriHide(obj, name, value) {
    // R157 (js baidu, 1.1.2): Rhino's NativeJavaList enumerates only the indices (`for (i in list)`), so the helpers are not
    // enumerable; 1.1.1 keeps them as plain (enumerable) properties.
    if (typeof __readoriListNonEnumerable !== 'undefined' && __readoriListNonEnumerable) {
        try { Object.defineProperty(obj, name, { value: value, writable: true, configurable: true, enumerable: false }); return; } catch (error) {}
    }
    obj[name] = value;
}
function __readoriListArray(values) {
    var arr = Array.isArray(values) ? values : [];
    if (typeof arr.toArray !== 'function') __readoriHide(arr, 'toArray', function() { return this; });
    if (typeof arr.size !== 'function') __readoriHide(arr, 'size', function() { return this.length; });
    if (typeof arr.get !== 'function') __readoriHide(arr, 'get', function(index) { return this[Number(index || 0)]; });
    if (typeof arr.isEmpty !== 'function') __readoriHide(arr, 'isEmpty', function() { return this.length === 0; });
    return arr;
}
var __readoriT2SMap = {
    '劍':'剑','來':'来','與':'与','龍':'龙','門':'门','書':'书','閱':'阅','雲':'云','類':'类','靈':'灵','輕':'轻','職':'职','場':'场',
    '歷':'历','俠':'侠','戲':'戏','戰':'战','醫':'医','術':'术','脈':'脉','懸':'悬','虛':'虚','擬':'拟','發':'发','財':'财',
    '間':'间','總':'总','裝':'装','備':'备','輪':'轮','種':'种','癒':'愈','療':'疗','爭':'争','鬥':'斗','開':'开','關':'关',
    '後':'后','復':'复','億':'亿','萬':'万','頁':'页','讀':'读','載':'载','圖':'图','畫':'画','個':'个','單':'单','過':'过',
    '這':'这','說':'说','語':'语','義':'义','題':'题','標':'标','籤':'签','簡':'简','節':'节','狀':'状','時':'时','數':'数',
    '內':'内','搜尋':'搜索','現':'现','園':'园','國':'国','貝':'贝','寶':'宝','產':'产','變':'变','異':'异','體':'体','電':'电',
    '腦':'脑','驚':'惊','殭':'僵','鑑':'鉴','盜':'盗','賊':'贼','貴':'贵','醫':'医','師':'师','強':'强','農':'农','婦':'妇',
    '棄':'弃','歡':'欢','權':'权','專':'专','寵':'宠','傷':'伤','閃':'闪','親':'亲','愛':'爱','錯':'错','齣':'出','斷':'断',
    '風':'风','歡':'欢','夢':'梦','險':'险','軍':'军','競':'竞','輩':'辈','選':'选','擇':'择','錄':'录','錄':'录'
};
var __readoriS2TMap = {
    '剑':'劍','来':'來','与':'與','龙':'龍','门':'門','书':'書','阅':'閱','云':'雲','类':'類','灵':'靈','轻':'輕','职':'職','场':'場',
    '历':'歷','侠':'俠','戏':'戲','战':'戰','医':'醫','术':'術','脉':'脈','悬':'懸','虚':'虛','拟':'擬','发':'發','财':'財',
    '间':'間','总':'總','装':'裝','备':'備','轮':'輪','种':'種','愈':'癒','疗':'療','争':'爭','斗':'鬥','开':'開','关':'關',
    '后':'後','复':'復','亿':'億','万':'萬','页':'頁','读':'讀','载':'載','图':'圖','画':'畫','个':'個','单':'單','过':'過',
    '这':'這','说':'說','语':'語','义':'義','题':'題','标':'標','签':'籤','简':'簡','节':'節','状':'狀','时':'時','数':'數',
    '内':'內','现':'現','园':'園','国':'國','贝':'貝','宝':'寶','产':'產','变':'變','异':'異','体':'體','电':'電',
    '脑':'腦','惊':'驚','僵':'殭','鉴':'鑑','盗':'盜','贼':'賊','贵':'貴','师':'師','强':'強','农':'農','妇':'婦',
    '弃':'棄','欢':'歡','权':'權','专':'專','宠':'寵','伤':'傷','闪':'閃','亲':'親','爱':'愛','错':'錯','断':'斷',
    '风':'風','梦':'夢','险':'險','军':'軍','竞':'競','辈':'輩','选':'選','择':'擇','录':'錄'
};
function __readoriConvertChinese(value, map) {
    var text = String(value === undefined || value === null ? '' : value);
    var out = '';
    for (var i = 0; i < text.length; i++) {
        var ch = text.charAt(i);
        out += Object.prototype.hasOwnProperty.call(map, ch) ? map[ch] : ch;
    }
    return out;
}
function __readoriChineseNumberValue(value) {
    var digits = {
        '\u96f6': 0, '\u3007': 0, '\u4e00': 1, '\u4e8c': 2, '\u4e24': 2,
        '\u4e09': 3, '\u56db': 4, '\u4e94': 5, '\u516d': 6, '\u4e03': 7,
        '\u516b': 8, '\u4e5d': 9
    };
    var units = {'\u5341': 10, '\u767e': 100, '\u5343': 1000, '\u4e07': 10000};
    var total = 0, section = 0, number = 0, sawToken = false;
    var text = String(value || '');
    for (var i = 0; i < text.length; i++) {
        var ch = text.charAt(i);
        if (Object.prototype.hasOwnProperty.call(digits, ch)) {
            number = digits[ch];
            sawToken = true;
        } else if (Object.prototype.hasOwnProperty.call(units, ch)) {
            var unit = units[ch];
            sawToken = true;
            if (unit === 10000) {
                section = (section + Math.max(number, 1)) * unit;
                total += section;
                section = 0;
            } else {
                section += Math.max(number, 1) * unit;
            }
            number = 0;
        }
    }
    return sawToken ? total + section + number : null;
}
// Legado JsExtensions.toNumChapter: titleNumPattern (第)(.+?)(章), first match only,
// group 2 through StringUtils.stringToInt (full->half width, Integer.parseInt, then
// chineseNumToInt, -1 on failure); returns only group1 + number + group3.
function __readoriStringToInt(raw) {
    var text = String(raw === undefined || raw === null ? '' : raw).replace(/[\uff01-\uff5e]/g, function(ch) {
        return String.fromCharCode(ch.charCodeAt(0) - 0xfee0);
    }).replace(/\u3000/g, ' ').replace(/\s+/g, '');
    if (/^[+-]?\d+$/.test(text)) {
        var parsed = Number(text);
        if (parsed >= -2147483648 && parsed <= 2147483647) return parsed;
    }
    var number = __readoriChineseNumberValue(text);
    return number === null ? -1 : number;
}
function __readoriToNumChapter(value) {
    if (value === undefined || value === null) return null;
    var text = String(value);
    var match = /(\u7b2c)(.+?)(\u7ae0)/.exec(text);
    if (!match) return text;
    return match[1] + __readoriStringToInt(match[2]) + match[3];
}
function __readoriFormatTimestamp(timestamp, fmt, useUTC) {
    var value = Number(timestamp || 0);
    var rawOffsetMilliseconds = arguments.length > 3 ? Number(arguments[3] || 0) : 0;
    var date = new Date(value + (useUTC ? rawOffsetMilliseconds : 0));
    var format = String(fmt || 'yyyy-MM-dd HH:mm:ss');
    function part(name) {
        if (useUTC) {
            if (name === 'year') return date.getUTCFullYear();
            if (name === 'month') return date.getUTCMonth() + 1;
            if (name === 'day') return date.getUTCDate();
            if (name === 'hour') return date.getUTCHours();
            if (name === 'minute') return date.getUTCMinutes();
            if (name === 'second') return date.getUTCSeconds();
        }
        if (name === 'year') return date.getFullYear();
        if (name === 'month') return date.getMonth() + 1;
        if (name === 'day') return date.getDate();
        if (name === 'hour') return date.getHours();
        if (name === 'minute') return date.getMinutes();
        if (name === 'second') return date.getSeconds();
        return 0;
    }
    function pad(value, size) { return String(value).padStart(size, '0'); }
    return format
        .replace(/yyyy/g, pad(part('year'), 4))
        .replace(/yy/g, pad(part('year') % 100, 2))
        .replace(/MM/g, pad(part('month'), 2))
        .replace(/M/g, String(part('month')))
        .replace(/dd/g, pad(part('day'), 2))
        .replace(/d/g, String(part('day')))
        .replace(/HH/g, pad(part('hour'), 2))
        .replace(/H/g, String(part('hour')))
        .replace(/mm/g, pad(part('minute'), 2))
        .replace(/m/g, String(part('minute')))
        .replace(/ss/g, pad(part('second'), 2))
        .replace(/s/g, String(part('second')));
}
function __readoriHtmlFormatKeepImg(value) {
    var text = String(value === undefined || value === null ? '' : value);
    text = text
        .replace(/(?:&nbsp;)+/gi, ' ')
        .replace(/&(?:ensp|emsp);/gi, ' ')
        .replace(/&(?:thinsp|zwnj|zwj);|\u2009|\u200c|\u200d/gi, '')
        .replace(/<\/?(?:div|p|br|hr|h\d|article|dd|dl)[^>]*>/gi, '\n')
        .replace(/<!--[^>]*-->/gs, '')
        .replace(/<\/?(?!img\b)[a-z]+(?=[ >])[^<>]*>/gi, '')
        .replace(/\s*\n+\s*/g, '\n　　')
        .replace(/^[\n\s]+/g, '　　')
        .replace(/[\n\s]+$/g, '');
    return text.replace(/<img\b[^>]*>/gi, function(tag) {
        var match = /\bsrc\s*=\s*["']([^"']+)["']/i.exec(tag)
            || /\bdata-src\s*=\s*["']([^"']+)["']/i.exec(tag)
            || /\bdata-[^=\s>]+\s*=\s*["']([^"']*)["']/i.exec(tag);
        return match ? '<img src="' + match[1] + '">' : tag;
    });
}
function __readoriResolveUrl(url) {
    var target = String(url || '');
    try {
        return new URL(target, baseUrl || bookSourceUrl || sourceUrl || 'https://example.test/').toString();
    } catch (error) {
        return target;
    }
}
var __readoriTextFileCache = {};
var __readoriSourceFiles = {};
var __readoriSourceDirectories = { '': true };
var __readoriSourceFileModified = {};
function __readoriCanonicalSourcePath(path) {
    var raw = String(path === null || path === undefined ? '' : path)
        .replace(/\\/g, '/');
    var parts = [];
    raw.split('/').forEach(function(part) {
        if (!part || part === '.') return;
        if (part === '..') {
            if (!parts.length) throw new Error('SecurityException: illegal file path');
            parts.pop();
            return;
        }
        parts.push(part);
    });
    return parts.join('/');
}
function __readoriStoredSourceFileKey(path) {
    var wanted = __readoriCanonicalSourcePath(path);
    var keys = Object.keys(__readoriSourceFiles);
    for (var i = 0; i < keys.length; i++) {
        if (__readoriCanonicalSourcePath(keys[i]) === wanted) return keys[i];
    }
    return null;
}
function __readoriSourceFileProxy(path) {
    var key = __readoriCanonicalSourcePath(path);
    function prefix() { return key ? key + '/' : ''; }
    function storedKey() { return __readoriStoredSourceFileKey(key); }
    function childKeys() {
        var p = prefix();
        var out = {};
        Object.keys(__readoriSourceFiles).concat(Object.keys(__readoriSourceDirectories)).forEach(function(existing) {
            var canonical = __readoriCanonicalSourcePath(existing);
            if (canonical === key || canonical.indexOf(p) !== 0) return;
            var child = canonical.substring(p.length).split('/')[0];
            if (child) out[child] = true;
        });
        return Object.keys(out).sort();
    }
    function parentKey() {
        if (!key) return null;
        var slash = key.lastIndexOf('/');
        return slash < 0 ? '' : key.substring(0, slash);
    }
    function exists() {
        return key === '' || storedKey() !== null
            || !!__readoriSourceDirectories[key] || childKeys().length > 0;
    }
    function isDirectory() {
        return exists() && storedKey() === null;
    }
    var absolute = '/cache' + (key ? '/' + key : '');
    var file = {
        path: absolute,
        absolutePath: absolute,
        canonicalPath: absolute,
        name: key ? key.substring(key.lastIndexOf('/') + 1) : 'cache',
        parent: parentKey() === null ? null : '/cache' + (parentKey() ? '/' + parentKey() : ''),
        getPath: function() { return this.path; },
        getAbsolutePath: function() { return this.absolutePath; },
        getCanonicalPath: function() { return this.canonicalPath; },
        getName: function() { return this.name; },
        getParent: function() { return this.parent; },
        getParentFile: function() {
            var parent = parentKey();
            return parent === null ? null : __readoriSourceFileProxy(parent);
        },
        exists: exists,
        isFile: function() { return storedKey() !== null; },
        isDirectory: isDirectory,
        isHidden: function() { return this.name.charAt(0) === '.'; },
        canRead: exists,
        canWrite: exists,
        length: function() {
            var stored = storedKey();
            return stored === null ? 0 : __readoriToByteArray(__readoriSourceFiles[stored]).length;
        },
        lastModified: function() {
            return Number(__readoriSourceFileModified[key] || 0);
        },
        setLastModified: function(milliseconds) {
            if (!exists() || !Number.isFinite(Number(milliseconds)) || Number(milliseconds) < 0) return false;
            __readoriSourceFileModified[key] = Number(milliseconds);
            return true;
        },
        mkdir: function() {
            if (exists()) return false;
            var parent = parentKey();
            if (parent !== null && !__readoriSourceFileProxy(parent).isDirectory()) return false;
            __readoriSourceDirectories[key] = true;
            return true;
        },
        mkdirs: function() {
            if (exists()) return false;
            var current = '';
            key.split('/').forEach(function(part) {
                current = current ? current + '/' + part : part;
                __readoriSourceDirectories[current] = true;
            });
            return true;
        },
        createNewFile: function() {
            if (exists()) return false;
            var parent = parentKey();
            if (parent !== null && !__readoriSourceFileProxy(parent).isDirectory()) return false;
            __readoriSourceFiles[key] = [];
            __readoriSourceFileModified[key] = Date.now();
            return true;
        },
        delete: function() {
            var stored = storedKey();
            if (stored !== null) {
                delete __readoriSourceFiles[stored];
                delete __readoriSourceFileModified[key];
                return true;
            }
            if (!isDirectory() || childKeys().length) return false;
            if (key === '') return false;
            delete __readoriSourceDirectories[key];
            delete __readoriSourceFileModified[key];
            return true;
        },
        list: function() {
            return isDirectory() ? childKeys() : null;
        },
        listFiles: function() {
            var names = this.list();
            return names === null ? null : names.map(function(name) {
                return __readoriSourceFileProxy(key ? key + '/' + name : name);
            });
        },
        renameTo: function(destination) {
            if (!destination || typeof destination.getCanonicalPath !== 'function'
                || !exists() || destination.exists()) return false;
            var destinationKey = __readoriCanonicalSourcePath(destination.getCanonicalPath().replace(/^\/cache\/?/, ''));
            var stored = storedKey();
            if (stored !== null) {
                __readoriSourceFiles[destinationKey] = __readoriSourceFiles[stored];
                delete __readoriSourceFiles[stored];
                return true;
            }
            return false;
        },
        compareTo: function(other) {
            var otherPath = other && typeof other.getPath === 'function' ? other.getPath() : '';
            return this.path < otherPath ? -1 : (this.path > otherPath ? 1 : 0);
        },
        toString: function() { return this.path; }
    };
    return file;
}
function __readoriSourceFilePath(url, requireExplicitType) {
    var raw = String(url || '');
    var parsed = __readoriParseUrlOptions(raw);
    var explicitType = String((parsed.options || {}).type || '').replace(/^\.+|\.+$/g, '').trim();
    if (requireExplicitType && !explicitType) return '';
    var cleanUrl = String(parsed.url || '').split(/[?#]/)[0];
    var slash = cleanUrl.lastIndexOf('/');
    var name = slash >= 0 ? cleanUrl.substring(slash + 1) : cleanUrl;
    var dot = name.lastIndexOf('.');
    var pathSuffix = dot >= 0 ? name.substring(dot + 1) : '';
    if (!pathSuffix || pathSuffix.length > 5 || !/^[a-z\d]+$/i.test(pathSuffix)) pathSuffix = 'ext';
    var suffix = explicitType || pathSuffix;
    suffix = String(suffix).replace(/\\/g, '/').split('/').pop().trim() || 'ext';
    return '/' + java.md5Encode16(raw) + '.' + suffix;
}
function __readoriWriteSourceFile(path, bytes) {
    var key = String(path || '');
    if (!key) return '';
    __readoriSourceFiles[key] = __readoriSignedBytes(__readoriToByteArray(bytes));
    __readoriSourceFileModified[__readoriCanonicalSourcePath(key)] = Date.now();
    return key;
}
function __readoriUnarchiveSourceFile(path) {
    var key = String(path || '');
    var sourceBytes = __readoriSourceFiles[key];
    if (!key || sourceBytes === undefined) return '';
    var fileName = key.replace(/\\/g, '/').split('/').pop();
    var destination = 'ArchiveTemp/' + java.md5Encode16(fileName);
    Object.keys(__readoriSourceFiles).forEach(function(existing) {
        if (existing === destination || existing.indexOf(destination + '/') === 0) {
            delete __readoriSourceFiles[existing];
        }
    });
    try {
        var archiveResult = null;
        if (typeof __readoriRarArchiveFixtures !== 'undefined'
            && __readoriRarArchiveFixtures) {
            archiveResult = __readoriRarArchiveFixtures[key] || null;
        }
        if (!archiveResult && typeof __readoriRarEntriesNative === 'function') {
            var nativeRarResult = __readoriRarEntriesNative(
                JSON.stringify(__readoriToByteArray(sourceBytes))
            );
            archiveResult = nativeRarResult ? JSON.parse(nativeRarResult) : null;
        }
        if (!archiveResult && typeof __readoriSevenZipArchiveFixtures !== 'undefined'
            && __readoriSevenZipArchiveFixtures) {
            archiveResult = __readoriSevenZipArchiveFixtures[key] || null;
        }
        if (!archiveResult && typeof __readoriSevenZipEntriesNative === 'function') {
            var nativeSevenZipResult = __readoriSevenZipEntriesNative(
                JSON.stringify(__readoriToByteArray(sourceBytes))
            );
            archiveResult = nativeSevenZipResult ? JSON.parse(nativeSevenZipResult) : null;
        }
        if (archiveResult && (archiveResult.format === 'rar' || archiveResult.format === '7z')) {
            (archiveResult.directories || []).forEach(function(name) {
                var clean = __readoriCanonicalSourcePath(name);
                if (clean) __readoriSourceDirectories[destination + '/' + clean] = true;
            });
            var files = archiveResult.files || {};
            Object.keys(files).forEach(function(name) {
                var clean = __readoriCanonicalSourcePath(name);
                if (!clean) return;
                var stored = destination + '/' + clean;
                __readoriSourceFiles[stored] =
                    __readoriSignedBytes(__readoriToByteArray(files[name]));
                __readoriSourceFileModified[__readoriCanonicalSourcePath(stored)] = Date.now();
            });
            return destination;
        }
    } catch (error) {}
    if (typeof Buffer === 'undefined' || typeof __readoriZlib === 'undefined') return '';
    try {
        var bytes = Buffer.from(__readoriToByteArray(sourceBytes));
        var offset = 0;
        while (offset + 30 <= bytes.length) {
            var signature = bytes.readUInt32LE(offset);
            if (signature === 0x02014b50 || signature === 0x06054b50) break;
            if (signature !== 0x04034b50) return '';
            var flags = bytes.readUInt16LE(offset + 6);
            var method = bytes.readUInt16LE(offset + 8);
            var compressedSize = bytes.readUInt32LE(offset + 18);
            var nameLength = bytes.readUInt16LE(offset + 26);
            var extraLength = bytes.readUInt16LE(offset + 28);
            var nameStart = offset + 30;
            var dataStart = nameStart + nameLength + extraLength;
            if (dataStart > bytes.length || ((flags & 0x08) !== 0 && compressedSize === 0)) return '';
            var dataEnd = dataStart + compressedSize;
            if (dataEnd > bytes.length) return '';
            var name = bytes.slice(nameStart, nameStart + nameLength).toString('utf8')
                .replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
            if (name && name.indexOf('../') < 0 && name !== '..' && name.charAt(name.length - 1) !== '/') {
                var compressed = bytes.slice(dataStart, dataEnd);
                var output = method === 0
                    ? compressed
                    : (method === 8 ? __readoriZlib.inflateRawSync(compressed) : null);
                if (output) {
                    __readoriSourceFiles[destination + '/' + name] =
                        __readoriSignedBytes(Array.from(output));
                }
            }
            if (dataEnd <= offset) return '';
            offset = dataEnd;
        }
        return destination;
    } catch (error) {
        return '';
    }
}
function __readoriTextInSourceFolder(path) {
    var folder = String(path || '').replace(/[\\/]+$/g, '');
    if (!folder) return '';
    var prefix = folder + '/';
    var keys = Object.keys(__readoriSourceFiles).filter(function(key) {
        if (key.indexOf(prefix) !== 0) return false;
        return key.substring(prefix.length).indexOf('/') < 0;
    });
    var text = keys.map(function(key) {
        return __readoriBytesToString(__readoriSourceFiles[key], 'UTF-8');
    }).join('\n');
    Object.keys(__readoriSourceFiles).forEach(function(key) {
        if (key === folder || key.indexOf(prefix) === 0) delete __readoriSourceFiles[key];
    });
    return text;
}
function __readoriFullRegexMatch(value, pattern) {
    var text = String(value || '');
    try {
        var match = new RegExp(String(pattern || '')).exec(text);
        return !!match && match.index === 0 && String(match[0] || '').length === text.length;
    } catch (error) {
        throw new Error('invalid WebView regex: ' + pattern);
    }
}
function __readoriWebViewFixture(url) {
    var target = String(url || '');
    var resolved = __readoriResolveUrl(target);
    if (typeof __readoriWebViewFixtures === 'undefined' || !__readoriWebViewFixtures) return {};
    return __readoriWebViewFixtures[target]
        || __readoriWebViewFixtures[resolved]
        || {};
}
function __readoriHTMLResourceURLs(html, base) {
    var text = String(html || '');
    var out = [];
    var seen = {};
    var pattern = /(?:src|href|data-src|data-href|data-original|data-url|data-link|poster)\s*=\s*["']([^"']+)["']/gi;
    var match;
    while ((match = pattern.exec(text)) !== null) {
        var value = String(match[1] || '');
        try {
            if (typeof URL !== 'undefined') value = String(new URL(value, String(base || __readoriResolveUrl(''))));
        } catch (error) {}
        if (value && !seen[value]) {
            seen[value] = true;
            out.push(value);
        }
    }
    return out;
}
function __readoriToURLObject(url, explicitBase) {
    var target = String(url || '');
    var base = String(explicitBase || '');
    try {
        if (typeof __readoriToURLJson === 'function') {
            var nativeValue = JSON.parse(__readoriToURLJson(target, base) || '{}');
            if (nativeValue && nativeValue.searchParams !== null) {
                nativeValue.searchParams = __readoriMapObject(nativeValue.searchParams || {});
            }
            return nativeValue;
        }
    } catch (error) {}
    var parsed = base ? new URL(target, base) : new URL(target);
    var values = {};
    var query = String(parsed.search || '');
    if (query.charAt(0) === '?') query = query.substring(1);
    if (query) {
        query.split('&').forEach(function(part) {
            var equal = part.indexOf('=');
            if (equal < 0) return;
            var key = part.substring(0, equal);
            var encoded = part.substring(equal + 1).replace(/\+/g, ' ');
            try { values[key] = decodeURIComponent(encoded); }
            catch (error) { values[key] = encoded; }
        });
    }
    var origin = String(parsed.protocol || '') + '//' + String(parsed.hostname || '');
    if (parsed.port) origin += ':' + parsed.port;
    return {
        host: String(parsed.hostname || ''),
        origin: origin,
        pathname: String(parsed.pathname || ''),
        searchParams: parsed.search ? __readoriMapObject(values) : null
    };
}
function __readoriZipEntryBytes(url, entryName) {
    var target = String(url || '');
    var resolved = __readoriResolveUrl(target);
    var encoded = '';
    var rawHex = !/^https?:/i.test(target) && /^[0-9a-f]+$/i.test(target) && target.length % 2 === 0
        ? target : '';
    try {
        if (!rawHex && typeof __readoriBinaryFixtures !== 'undefined' && __readoriBinaryFixtures) {
            encoded = __readoriBinaryFixtures[target] || __readoriBinaryFixtures[resolved] || __readoriBinaryFixtures[String(url || '')] || '';
        }
        if (!rawHex && !encoded && typeof __readoriAjaxBytesBase64 === 'function') {
            encoded = __readoriAjaxBytesBase64(target) || '';
        }
    } catch (error) {
        encoded = '';
    }
    if ((!encoded && !rawHex) || typeof Buffer === 'undefined' || typeof __readoriZlib === 'undefined') return null;
    var wanted = String(entryName || '');
    if (!wanted) return null;
    try {
        var bytes = rawHex
            ? Buffer.from(rawHex, 'hex')
            : Buffer.from(String(encoded || ''), 'base64');
        var offset = 0;
        while (offset + 30 <= bytes.length) {
            var signature = bytes.readUInt32LE(offset);
            if (signature === 0x02014b50 || signature === 0x06054b50) break;
            if (signature !== 0x04034b50) break;
            var flags = bytes.readUInt16LE(offset + 6);
            var method = bytes.readUInt16LE(offset + 8);
            var compressedSize = bytes.readUInt32LE(offset + 18);
            var nameLength = bytes.readUInt16LE(offset + 26);
            var extraLength = bytes.readUInt16LE(offset + 28);
            var nameStart = offset + 30;
            var dataStart = nameStart + nameLength + extraLength;
            if (dataStart > bytes.length) break;
            var name = bytes.slice(nameStart, nameStart + nameLength).toString('utf8');
            if ((flags & 0x08) !== 0 && compressedSize === 0) break;
            var dataEnd = dataStart + compressedSize;
            if (dataEnd > bytes.length) break;
            if (name === wanted) {
                var compressed = bytes.slice(dataStart, dataEnd);
                var output = method === 0 ? compressed : (method === 8 ? __readoriZlib.inflateRawSync(compressed) : Buffer.alloc(0));
                return __readoriSignedBytes(Array.from(output));
            }
            if (dataEnd <= offset) break;
            offset = dataEnd;
        }
    } catch (error) {}
    return null;
}
function __readoriZipEntryString(url, entryName, charset) {
    var bytes = __readoriZipEntryBytes(url, entryName);
    if (bytes === null || bytes === undefined) return '';
    return __readoriBytesToString(bytes, charset || 'UTF-8');
}
function __readoriRarEntryBytes(url, entryName) {
    var target = String(url || '');
    var resolved = __readoriResolveUrl(target);
    var wanted = String(entryName || '');
    if (!wanted) return null;
    if (typeof __readoriRarFixtures !== 'undefined' && __readoriRarFixtures) {
        var fixture = __readoriRarFixtures[target]
            || __readoriRarFixtures[resolved] || null;
        var value = fixture && fixture[wanted];
        if (Array.isArray(value)) return __readoriSignedBytes(value);
        if (typeof value === 'string' && typeof Buffer !== 'undefined') {
            return __readoriSignedBytes(Array.from(Buffer.from(value, 'base64')));
        }
    }
    if (typeof __readoriRarEntryNative !== 'function') return null;
    var rawHex = !/^https?:/i.test(target) && /^[0-9a-f]+$/i.test(target)
        && target.length % 2 === 0 ? target : '';
    var payload = rawHex;
    var encoding = rawHex ? 'hex' : 'base64';
    if (!payload) {
        try {
            if (typeof __readoriBinaryFixtures !== 'undefined' && __readoriBinaryFixtures) {
                payload = __readoriBinaryFixtures[target]
                    || __readoriBinaryFixtures[resolved] || '';
            }
            if (!payload && typeof __readoriAjaxBytesBase64 === 'function') {
                payload = __readoriAjaxBytesBase64(target) || '';
            }
        } catch (error) {
            payload = '';
        }
    }
    if (!payload) return null;
    try {
        var result = __readoriRarEntryNative(payload, encoding, wanted);
        return result ? __readoriSignedBytes(JSON.parse(result)) : null;
    } catch (error) {
        return null;
    }
}
function __readoriRarEntryString(url, entryName, charset) {
    var bytes = __readoriRarEntryBytes(url, entryName);
    if (bytes === null || bytes === undefined) return '';
    return __readoriBytesToString(bytes, charset || 'UTF-8');
}
function __readoriSevenZipEntryBytes(url, entryName) {
    var target = String(url || '');
    var resolved = __readoriResolveUrl(target);
    var wanted = String(entryName || '');
    if (!wanted) return null;
    if (typeof __readoriSevenZipFixtures !== 'undefined' && __readoriSevenZipFixtures) {
        var fixture = __readoriSevenZipFixtures[target]
            || __readoriSevenZipFixtures[resolved] || null;
        var value = fixture && (fixture[wanted] || fixture[entryName]);
        if (Array.isArray(value)) return __readoriSignedBytes(value);
        if (typeof value === 'string' && typeof Buffer !== 'undefined') {
            return __readoriSignedBytes(Array.from(Buffer.from(value, 'base64')));
        }
    }
    if (typeof __readoriSevenZipEntryNative !== 'function') return null;
    var rawHex = !/^https?:/i.test(target) && /^[0-9a-f]+$/i.test(target)
        && target.length % 2 === 0 ? target : '';
    var payload = rawHex;
    var encoding = rawHex ? 'hex' : 'base64';
    if (!payload) {
        try {
            if (typeof __readoriBinaryFixtures !== 'undefined' && __readoriBinaryFixtures) {
                payload = __readoriBinaryFixtures[target]
                    || __readoriBinaryFixtures[resolved] || '';
            }
            if (!payload && typeof __readoriAjaxBytesBase64 === 'function') {
                payload = __readoriAjaxBytesBase64(target) || '';
            }
        } catch (error) {
            payload = '';
        }
    }
    if (!payload) return null;
    try {
        var result = __readoriSevenZipEntryNative(payload, encoding, wanted);
        return result ? __readoriSignedBytes(JSON.parse(result)) : null;
    } catch (error) {
        return null;
    }
}
function __readoriSevenZipEntryString(url, entryName, charset) {
    var bytes = __readoriSevenZipEntryBytes(url, entryName);
    if (bytes === null || bytes === undefined) return '';
    return __readoriBytesToString(bytes, charset || 'UTF-8');
}
// R158 (asgportal `java.ajax(url, {"method": "post", body: "..."})`): the options are Legado's lenient JSON (UrlOption is read with Gson
// lenient: bare keys, single quotes, trailing commas); the App parses them with UrlWithOptions.parse + LegadoLenientJSON. Turn them into
// strict JSON text (data only: no expressions are evaluated).
function __readoriLenientJsonText(text) {
    var out = '', i = 0, n = text.length;
    while (i < n) {
        var ch = text.charAt(i);
        if (ch === '"' || ch === "'") {
            var quote = ch, body = '';
            i++;
            while (i < n && text.charAt(i) !== quote) {
                var c = text.charAt(i);
                if (c === '\\' && i + 1 < n) {
                    var e = text.charAt(i + 1);
                    if (quote === "'" && e === "'") body += "'";
                    else if (e === '"') body += '\\"';
                    else body += c + e;
                    i += 2;
                    continue;
                }
                if (c === '"') body += '\\"';
                else if (c === '\n') body += '\\n';
                else if (c === '\r') body += '\\r';
                else if (c === '\t') body += '\\t';
                else body += c;
                i++;
            }
            i++;
            out += '"' + body + '"';
            continue;
        }
        if (/[A-Za-z_$]/.test(ch)) {
            var j = i;
            while (j < n && /[\w$-]/.test(text.charAt(j))) j++;
            var word = text.slice(i, j), k = j;
            while (k < n && /\s/.test(text.charAt(k))) k++;
            out += text.charAt(k) === ':' ? '"' + word + '"' : word;
            i = j;
            continue;
        }
        if (ch === '}' || ch === ']') out = out.replace(/,\s*$/, '');
        out += ch;
        i++;
    }
    return out;
}
function __readoriParseUrlOptions(raw) {
    var text = String(raw || '').trim();
    var idx = -1;
    for (var i = text.length - 1; i >= 0; i--) {
        if (text.charAt(i) !== ',') continue;
        var tail = text.slice(i + 1).trim();
        if (tail.charAt(0) !== '{') continue;
        var candidates = [tail];
        try { candidates.push(__readoriLenientJsonText(tail)); } catch (error) {}
        for (var c = 0; c < candidates.length; c++) {
            try {
                var opts = JSON.parse(candidates[c]);
                if (opts && typeof opts === 'object') {
                    return { url: text.slice(0, i).trim(), options: opts };
                }
            } catch (error) {}
        }
        if (idx < 0) idx = i;
    }
    return { url: text, options: {} };
}
function __readoriMergeHeaders(base, extra) {
    var out = {};
    base = base || {};
    extra = extra || {};
    for (var key in base) {
        if (Object.prototype.hasOwnProperty.call(base, key)) out[key] = base[key];
    }
    for (var name in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, name)) out[name] = extra[name];
    }
    return out;
}
function __readoriNormalizeHeaderMap(values) {
    var out = {};
    values = values || {};
    for (var key in values) {
        if (Object.prototype.hasOwnProperty.call(values, key)) {
            out[String(key || '')] = values[key] === undefined || values[key] === null ? '' : String(values[key]);
        }
    }
    return out;
}
function __readoriHeaderLookup(values, key) {
    var wanted = String(key || '').toLowerCase();
    values = values || {};
    for (var name in values) {
        if (Object.prototype.hasOwnProperty.call(values, name) && String(name || '').toLowerCase() === wanted) {
            return values[name] === undefined || values[name] === null ? '' : String(values[name]);
        }
    }
    return '';
}
function __readoriResponseObject(body, finalUrl, status, responseHeaders, responseCookies, bodyBase64) {
    var text = body === undefined || body === null ? '' : String(body);
    // R157 (js baidu): `bodyBase64` carries the raw payload of a binary response (an AES-encrypted body decoded as text is
    // corrupted); Jsoup Response.bodyAsBytes() / getByteArrayString are the bytes, not the text re-encoded.
    var rawBase64 = bodyBase64 === undefined || bodyBase64 === null ? '' : String(bodyBase64);
    var url = finalUrl === undefined || finalUrl === null ? '' : String(finalUrl);
    var codeValue = Number(status || (text ? 200 : 0));
    var headerMap = __readoriNormalizeHeaderMap(responseHeaders || {});
    var cookieMap = __readoriNormalizeHeaderMap(responseCookies || {});
    var out = {
        body: function() { return text; },
        getStrResponse: function() { return text; },
        bodyAsBytes: function() {
            if (rawBase64) { try { return Base64.decode(rawBase64, 0); } catch (error) {} }
            return __readoriSignedBytes(__readoriToByteArray(text));
        },
        getByteArrayString: function() {
            if (rawBase64) return rawBase64;
            return Base64.getEncoder().encodeToString(__readoriToByteArray(text));
        },
        code: function() { return codeValue; },
        statusCode: function() { return codeValue; },
        statusMessage: function() {
            if (codeValue >= 200 && codeValue < 300) return 'OK';
            if (codeValue >= 300 && codeValue < 400) return 'Redirection';
            if (codeValue >= 400) return 'Error';
            return '';
        },
        isSuccessful: function() { return codeValue >= 200 && codeValue < 300; },
        header: function(key) {
            return __readoriHeaderLookup(headerMap, key);
        },
        headers: function(key) {
            if (arguments.length > 0 && key) {
                var value = this.header(key);
                return value ? [value] : [];
            }
            return __readoriMapObject(headerMap);
        },
        cookie: function(key) { return __readoriHeaderLookup(cookieMap, key); },
        cookies: function() { return __readoriMapObject(cookieMap); },
        url: function() { return url; },
        raw: function() { return { request: function() { return { url: function() { return url; } }; } }; },
        toString: function() { return text; },
        valueOf: function() { return text; }
    };
    [
        'match', 'replace', 'search', 'indexOf', 'lastIndexOf', 'includes',
        'split', 'trim', 'substring', 'substr', 'slice', 'startsWith',
        'endsWith', 'toLowerCase', 'toUpperCase'
    ].forEach(function(name) {
        if (typeof String.prototype[name] === 'function') {
            out[name] = function() { return String.prototype[name].apply(text, arguments); };
        }
    });
    try { Object.defineProperty(out, 'length', { get: function() { return text.length; } }); } catch (error) {}
    return out;
}
function __readoriLookupResponseFixture(url) {
    if (typeof __readoriResponseFixtures === 'undefined' || !__readoriResponseFixtures) return null;
    var parsed = __readoriParseUrlOptions(url);
    var target = String(parsed.url || url || '');
    var resolved = __readoriResolveUrl(target);
    var fixture = __readoriResponseFixtures[String(url || '')] || __readoriResponseFixtures[target] || __readoriResponseFixtures[resolved];
    if (!fixture) return null;
    if (typeof fixture !== 'object') fixture = { body: String(fixture || '') };
    return {
        body: fixture.body === undefined || fixture.body === null ? '' : String(fixture.body),
        url: fixture.url || resolved || target,
        status: Number(fixture.status || 200),
        headers: fixture.headers || {},
        cookies: fixture.cookies || {},
        bodyBase64: fixture.bodyBase64 === undefined || fixture.bodyBase64 === null ? '' : String(fixture.bodyBase64)
    };
}
function __readoriFetchResponse(url, method, body, requestHeaders, followRedirects) {
    var parsed = __readoriParseUrlOptions(url);
    var options = parsed.options || {};
    var target = __readoriResolveUrl(parsed.url);
    var effectiveMethod = String(options.method || method || 'GET').toUpperCase();
    var effectiveBody = options.body === undefined || options.body === null ? body : options.body;
    if (effectiveBody && typeof effectiveBody === 'object') {
        try { effectiveBody = JSON.stringify(effectiveBody); } catch (error) { effectiveBody = String(effectiveBody); }
    }
    var effectiveHeaders = __readoriMergeHeaders(requestHeaders || {}, options.headers || {});
    if (typeof __readoriRequestLog !== 'undefined'
        && Array.isArray(__readoriRequestLog)) {
        __readoriRequestLog.push({
            url: target,
            method: effectiveMethod,
            body: String(effectiveBody || ''),
            headers: effectiveHeaders,
            followRedirects: followRedirects !== false
        });
    }
    var fixture = __readoriLookupResponseFixture(url) || __readoriLookupResponseFixture(target);
    if (fixture) return __readoriResponseObject(fixture.body, fixture.url, fixture.status, fixture.headers, fixture.cookies, fixture.bodyBase64);
    try {
        if (typeof __readoriHttpResponse === 'function') {
            var document = JSON.parse(__readoriHttpResponse(
                target,
                effectiveMethod,
                String(effectiveBody || ''),
                JSON.stringify(effectiveHeaders || {}),
                followRedirects !== false
            ) || '{}');
            return __readoriResponseObject(
                document.body,
                document.url || target,
                document.status || 0,
                document.headers || {},
                document.cookies || {}
            );
        }
        if (effectiveMethod === 'POST' && typeof __readoriAjaxPost === 'function') {
            var postText = __readoriAjaxPost(target, String(effectiveBody || ''), JSON.stringify(effectiveHeaders || {})) || '';
            return __readoriResponseObject(postText, target, postText ? 200 : 0, {}, {});
        }
        if (typeof __readoriAjax === 'function') {
            var text = __readoriAjax(target) || '';
            return __readoriResponseObject(text, target, text ? 200 : 0, {}, {});
        }
    } catch (error) {}
    return __readoriResponseObject('', target, 0, {}, {});
}
var java = {
    getSource: function() { return typeof source === 'undefined' ? null : source; },
    ruleUrl: (typeof ruleUrl !== 'undefined' && String(ruleUrl || '').trim())
        ? String(ruleUrl)
        : String(baseUrl || ''),
    ajax: function(url) {
        return __readoriFetchResponse(url, 'GET', '', {}, true).body();
    },
    ajaxAll: function(urls) {
        var list = Array.isArray(urls) ? urls : [];
        return __readoriListArray(list.map(function(url) {
            return __readoriFetchResponse(url, 'GET', '', {}, true);
        }));
    },
    connect: function(url) {
        return __readoriFetchResponse(url, 'GET', '', {}, true);
    },
    head: function(url, requestHeaders) {
        return __readoriFetchResponse(url, 'HEAD', '', requestHeaders || {}, false);
    },
    get: function(key, requestHeaders) {
        var target = String(key || '');
        var hasBinaryFixture = typeof __readoriBinaryFixtures !== 'undefined' && __readoriBinaryFixtures && __readoriBinaryFixtures[target];
        if (arguments.length > 1 || target.indexOf('://') >= 0) {
            var response = __readoriFetchResponse(target, 'GET', '', requestHeaders || {}, false);
            if (hasBinaryFixture) {
                response.bodyAsBytes = function() { return Base64.decode(__readoriBinaryFixtures[target], 0); };
                response.getByteArrayString = function() { return String(__readoriBinaryFixtures[target] || ''); };
            } else if (typeof __readoriAjaxBytesBase64 === 'function') {
                response.bodyAsBytes = function() {
                    try { return Base64.decode(__readoriAjaxBytesBase64(target), 0); } catch (error) {}
                    return __readoriSignedBytes(__readoriToByteArray(response.body()));
                };
                response.getByteArrayString = function() {
                    try { return __readoriAjaxBytesBase64(target); } catch (error) {}
                    return Base64.getEncoder().encodeToString(__readoriToByteArray(response.body()));
                };
            }
            return response;
        }
        if (key === 'url') return baseUrl;
        if (key === 'cookie') return cookie || '';
        if (key === 'result') return result || '';
        if (key === 'baseUrl') return baseUrl || '';
        if (key === 'bookName' && typeof book !== 'undefined' && book) return String(book.name || '');
        if (key === 'title' && typeof chapter !== 'undefined' && chapter) return String(chapter.title || '');
        if (typeof chapter !== 'undefined' && chapter && typeof chapter.getVariable === 'function') {
            var chapterValue = chapter.getVariable(target);
            if (chapterValue !== '') return chapterValue;
        }
        if (typeof book !== 'undefined' && book && typeof book.getVariable === 'function') {
            var bookValue = book.getVariable(target);
            if (bookValue !== '') return bookValue;
        }
        return (variables && variables[key]) || '';
    },
    put: function(key, value) {
        // R157 (searchExplore G4): AnalyzeRule.put(key: String, value: String) - Rhino converts the argument with ToString, so a
        // list / array value is stored as the text `a,b` (an array object was kept and later turned into a Python list repr).
        if (value !== null && value !== undefined && typeof value !== 'string') value = String(value);
        if (typeof chapter !== 'undefined' && chapter && chapter.__active
            && typeof chapter.putVariable === 'function') {
            return chapter.putVariable(key, value);
        }
        if (typeof book !== 'undefined' && book && (book.bookUrl || book.name)
            && typeof book.putVariable === 'function') {
            return book.putVariable(key, value);
        }
        if (typeof variables !== 'undefined') variables[key] = value;
        return value;
    },
    log: function(msg) { return msg; },
    logType: function(msg) {},
    toast: function(msg) {},
    longToast: function(msg) {},
    sleep: function(ms) {},
    openUrl: function(url, mimeType) {
        var target = String(url === null || url === undefined ? '' : url);
        if (target.length >= 64 * 1024) {
            throw new Error('openUrl parameter url too long');
        }
    },
    startBrowser: function(url, title) {
        __readoriInteractionBlocked('startBrowser');
        return '';
    },
    startBrowserAwait: function(url, title, refetchAfterSuccess) {
        // App batch validation: no window can open, the browser answers with an empty response and
        // SourceVerificationHelp.getVerificationResult throws "验证结果为空" (JSEngine startBrowserAwait requireResult).
        if (__readoriInteractionBlocked('startBrowserAwait')) {
            throw new Error('验证结果为空');
        }
        var target = String(url || baseUrl || '');
        var shouldRefetch = arguments.length < 3 ? true : !!refetchAfterSuccess;
        if (!shouldRefetch) {
            var fixture = __readoriWebViewFixture(target);
            if (fixture && fixture.browserBody !== undefined) {
                return __readoriResponseObject(
                    String(fixture.browserBody || ''),
                    target,
                    200,
                    {},
                    {}
                );
            }
        }
        return __readoriFetchResponse(target, 'GET', '', {}, true);
    },
    startBrowserAwaitAwait: function(url, title, refetchAfterSuccess) {
        return arguments.length < 3
            ? java.startBrowserAwait(url, title)
            : java.startBrowserAwait(url, title, refetchAfterSuccess);
    },
    // App: openBlocking() records every blocked blocking-browser call as startBrowserAwait and answers with an empty
    // proxy; showReadingBrowser has no requireResult, so it does not throw.
    showReadingBrowser: function(url, title) {
        if (__readoriInteractionBlocked('startBrowserAwait')) return __readoriResponseObject('', '', 0, {}, {});
        return java.startBrowserAwait(url, title, false);
    },
    showBrowser: function(url, html, js) {
        var content = String(html === undefined || html === null ? '' : html);
        var hasContent = content.replace(/\s+/g, '') !== '';
        if (__readoriInteractionBlocked(hasContent ? 'showBrowser' : 'startBrowserAwait')) {
            return __readoriResponseObject('', '', 0, {}, {});
        }
        if (hasContent) return __readoriResponseObject(content, String(url || ''), 200, {}, {});
        return java.startBrowserAwait(url, '', false);
    },
    startBrowserDp: function(url, title) {
        // Deep links with a non-http(s)/data scheme leave the app (not recorded); everything else is startBrowser.
        var scheme = /^([A-Za-z][A-Za-z0-9+.\-]*):/.exec(String(url === undefined || url === null ? '' : url));
        if (scheme && !/^(?:https?|data)$/i.test(scheme[1])) return '';
        __readoriInteractionBlocked('startBrowser');
        return '';
    },
    webView: function(first, second, script) {
        var target = arguments.length > 1 && second ? second : first;
        return __readoriFetchResponse(target, 'GET', '', {}, true).body();
    },
    webview: function(first, second, script) { return java.webView(first, second, script); },
    webViewGetSource: function(html, url, script, sourceRegex) {
        var target = String(url || baseUrl || '');
        var fixture = __readoriWebViewFixture(target);
        var page = String(html || fixture.body || '');
        if (!page && target) page = __readoriFetchResponse(target, 'GET', '', {}, true).body();
        var resources = Array.isArray(fixture.resources)
            ? fixture.resources.slice()
            : __readoriHTMLResourceURLs(page, target);
        for (var i = 0; i < resources.length; i++) {
            if (__readoriFullRegexMatch(resources[i], sourceRegex)) return String(resources[i]);
        }
        return '';
    },
    webViewGetOverrideUrl: function(html, url, script, overrideUrlRegex) {
        var fixture = __readoriWebViewFixture(String(url || baseUrl || ''));
        var urls = Array.isArray(fixture.overrideUrls) ? fixture.overrideUrls : [];
        for (var i = 0; i < urls.length; i++) {
            if (__readoriFullRegexMatch(urls[i], overrideUrlRegex)) return String(urls[i]);
        }
        return '';
    },
    getZipStringContent: function(url, entryName, charset) {
        return __readoriZipEntryString(
            url,
            entryName,
            arguments.length > 2 ? String(charset || 'UTF-8') : 'UTF-8'
        );
    },
    getZipByteArrayContent: function(url, entryName) {
        return __readoriZipEntryBytes(url, entryName);
    },
    getRarStringContent: function(url, entryName, charset) {
        return __readoriRarEntryString(
            url,
            entryName,
            arguments.length > 2 ? String(charset || 'UTF-8') : 'UTF-8'
        );
    },
    getRarByteArrayContent: function(url, entryName) {
        return __readoriRarEntryBytes(url, entryName);
    },
    get7zStringContent: function(url, entryName, charset) {
        return __readoriSevenZipEntryString(
            url,
            entryName,
            arguments.length > 2 ? String(charset || 'UTF-8') : 'UTF-8'
        );
    },
    get7zByteArrayContent: function(url, entryName) {
        return __readoriSevenZipEntryBytes(url, entryName);
    },
    downloadFile: function(first, second) {
        var rawUrl = arguments.length > 1 ? String(second || '') : String(first || '');
        var path = __readoriSourceFilePath(rawUrl, arguments.length > 1);
        if (!path) return '';
        var bytes;
        if (arguments.length > 1) {
            bytes = __readoriHexToSignedBytes(String(first || ''));
        } else {
            bytes = __readoriFetchResponse(rawUrl, 'GET', '', {}, true).bodyAsBytes();
        }
        return __readoriWriteSourceFile(path, bytes);
    },
    getFile: function(path) {
        return __readoriSourceFileProxy(path);
    },
    readFile: function(path) {
        var key = String(path || '');
        if (!Object.prototype.hasOwnProperty.call(__readoriSourceFiles, key)) return null;
        return __readoriSignedBytes(__readoriToByteArray(__readoriSourceFiles[key]));
    },
    readTxtFile: function(path, charset) {
        var bytes = java.readFile(path);
        if (bytes === null || bytes === undefined) return '';
        return __readoriBytesToString(bytes, arguments.length > 1 ? String(charset || 'UTF-8') : 'UTF-8');
    },
    deleteFile: function(path) {
        var key = String(path || '');
        var prefix = key.replace(/[\\/]+$/g, '') + '/';
        var removed = false;
        Object.keys(__readoriSourceFiles).forEach(function(existing) {
            if (existing === key || existing.indexOf(prefix) === 0) {
                delete __readoriSourceFiles[existing];
                removed = true;
            }
        });
        return removed;
    },
    unArchiveFile: function(path) { return __readoriUnarchiveSourceFile(path); },
    unzipFile: function(path) { return __readoriUnarchiveSourceFile(path); },
    un7zFile: function(path) { return __readoriUnarchiveSourceFile(path); },
    unrarFile: function(path) { return __readoriUnarchiveSourceFile(path); },
    getTxtInFolder: function(path) { return __readoriTextInSourceFolder(path); },
    cacheFile: function(url, saveTime) {
        var key = String(url || '');
        if (Object.prototype.hasOwnProperty.call(__readoriTextFileCache, key)) {
            return __readoriTextFileCache[key];
        }
        var text = __readoriFetchResponse(key, 'GET', '', {}, true).body();
        if (text) __readoriTextFileCache[key] = text;
        return text;
    },
    importScript: function(path) {
        var target = String(path || '');
        var text = /^http/.test(target)
            ? java.cacheFile(target)
            : ((typeof __readoriScriptFixtures !== 'undefined' && __readoriScriptFixtures)
                ? String(__readoriScriptFixtures[target] || '') : '');
        if (!String(text || '').trim()) throw new Error(target + ' 内容获取失败或者为空');
        return text;
    },
    toURL: function(url, explicitBase) {
        return __readoriToURLObject(
            String(url || ''),
            arguments.length > 1 ? String(explicitBase || '') : ''
        );
    },
    // R158 (1.1.2 `set_content_keeps_result_binding`): Legado AnalyzeRule.setContent (:84-96) replaces only `content`; the `result` / `src` Rhino
    // bindings are built per evalJS (:774-788) and stay as they were. 1.1.1 App (JSEngine setContent_raw) overwrote `result` and `src`.
    setContent: function(content) {
        if (!(typeof __readoriSetContentKeepsResult !== 'undefined' && __readoriSetContentKeepsResult)) result = content;
        __readoriContent = content;
    },
    getElement: function(selector, html) {
        var sourceHtml = arguments.length > 1 ? String(html || '') : String(__readoriCurrentContent() || '');
        // R158 (G6, 1.1.2 `js_get_element_json_whole_value`): Legado AnalyzeRule.getElement (:331-358) in JSON mode answers `getObject(rule)` - the matched VALUE (a string
        // or the object), not a Jsoup element row (the App evaluates the rule with resolveElementsRule(...).first, JSEngine getElement).
        // The WHOLE value (`$.data.list` is the whole array, an indefinite path the array of all hits - JSONPathEngine.selectWholeValueJSON).
        if (typeof __readoriGetElementJsonWhole !== 'undefined' && __readoriGetElementJsonWhole
            && /^\s*(\$[.\[]|@json:)/i.test(String(selector || '')) && /^\s*[\[{]/.test(sourceHtml) && typeof __readoriEvaluateWholeJson === 'function') {
            var wholeText = '';
            try { wholeText = __readoriEvaluateWholeJson(String(selector || ''), sourceHtml) || ''; } catch (error) { wholeText = ''; }
            if (!wholeText) return '';
            try { return JSON.parse(wholeText); } catch (error) { return ''; }
        }
        var rows = _readoriJsoupRowsByLegadoRule(sourceHtml, String(selector || ''));
        if (rows === null) rows = _readoriJsoupRows(sourceHtml, String(selector || ''));
        // R156 (tocContent G, App profile 1.1.2): Legado AnalyzeRule.getElement (:332-358) returns the whole Elements list in
        // Default / XPath mode; only Json mode answers a single object.
        if (typeof __readoriGetElementReturnsList !== 'undefined' && __readoriGetElementReturnsList
            && !/^\s*(\$[.\[]|@json:)/i.test(String(selector || '')) && !/^\s*[\[{]/.test(sourceHtml)) {
            return _readoriJsoupElements(rows, null);
        }
        return rows.length ? _readoriJsoupElement(rows[0], null) : _readoriJsoupElement({}, null);
    },
    getElements: function(selector, html) {
        var sourceHtml = arguments.length > 1 ? String(html || '') : String(__readoriCurrentContent() || '');
        // R157 (js baidu, 1.1.2): in JSON mode Legado AnalyzeRule.getElements (AnalyzeByJSonPath.getList) returns the matched
        // objects, not their JSON text, so `list[i].title` works.
        // Mode as in SourceRule.init: `$.` / `$[` / `@json:` is JSON; so is any bare rule on JSON content; an explicit `@CSS:`, `@@`,
        // `@XPath:` or `/`-prefixed rule never is.
        var __ruleText = String(selector || '');
        var __explicitNonJson = /^\s*(@css:|@@|@xpath:|\/)/i.test(__ruleText);
        var __jsonMode = /^\s*(\$[.\[]|@json:)/i.test(__ruleText) || (!__explicitNonJson && /^\s*[\[{]/.test(sourceHtml));
        if (typeof __readoriGetElementsJsonObjects !== 'undefined' && __readoriGetElementsJsonObjects
            && __jsonMode && typeof __readoriEvaluateList === 'function') {
            var jsonRows = [];
            try { jsonRows = JSON.parse(__readoriEvaluateList(String(selector || ''), sourceHtml) || '[]') || []; } catch (error) { jsonRows = []; }
            return __readoriListArray(jsonRows.map(function(value) {
                if (typeof value === 'string' && /^\s*[\[{]/.test(value)) { try { return JSON.parse(value); } catch (error) {} }
                return value;
            }));
        }
        var rows = _readoriJsoupRowsByLegadoRule(sourceHtml, String(selector || ''));
        if (rows === null) rows = _readoriJsoupRowsForRule(sourceHtml, String(selector || ''));
        return _readoriJsoupElements(rows, null);
    },
    selectElementsJSON: function(selector, html) {
        try {
            if (typeof __readoriSelectElementsJSON === 'function') {
                return __readoriSelectElementsJSON(String(selector || ''), String(html || ''));
            }
        } catch (error) {}
        return '[]';
    },
    getString: function(ruleStr, html) {
        try {
            if (typeof __readoriEvaluateString === 'function') {
                // R157 (searchExplore G2): `java.getString(rule, true|false)` is AnalyzeRule.getString(ruleStr, unescape: Boolean)
                // (AnalyzeRule.kt:256); the boolean is NOT the content (it used to be stringified into "true" and evaluated as the page).
                // 1.1.1 App (`get_string_unescape_flag` off): the boolean is not the content but is ignored, it always unescapes.
                var unescapeFlag = true, hasContent = arguments.length > 1;
                if (typeof html === 'boolean') {
                    if (typeof __readoriGetStringUnescapeFlag !== 'undefined' && __readoriGetStringUnescapeFlag) unescapeFlag = html;
                    hasContent = false;
                }
                var sourceHtml = hasContent ? String(html || '') : String(__readoriCurrentContent() || '');
                return __readoriEvaluateString(String(ruleStr || ''), sourceHtml, unescapeFlag) || '';
            }
        } catch (error) {}
        return '';
    },
    getStringList: function(ruleStr, html, raw) {
        var sourceHtml = arguments.length > 1 ? String(html || '') : String(__readoriCurrentContent() || '');
        var values = [];
        try {
            if (typeof __readoriEvaluateList === 'function') {
                values = JSON.parse(__readoriEvaluateList(String(ruleStr || ''), sourceHtml) || '[]') || [];
            }
        } catch (error) { values = []; }
        return __readoriListArray(values);
    },
    base64Encode: function(str, flags) {
        return Base64.encodeToString(__readoriStringToBytes(str, 'utf-8'), arguments.length > 1 ? Number(flags || 0) : 2);
    },
    base64Encoder: function(str) {
        return Base64.encodeToString(__readoriStringToBytes(str, 'utf-8'), 2);
    },
    base64Decode: function(str, charsetOrFlags) {
        var charset = arguments.length > 1 && typeof charsetOrFlags !== 'number'
            ? String(charsetOrFlags || 'utf-8') : 'utf-8';
        var flags = arguments.length > 1 && typeof charsetOrFlags === 'number'
            ? Number(charsetOrFlags || 0) : 0;
        return __readoriBytesToString(Base64.decode(str, flags), charset);
    },
    base64Decoder: function(str) {
        return __readoriBytesToString(Base64.decode(str, 0), 'utf-8');
    },
    base64DecodeToByteArray: function(str, flags) { return Base64.decode(str, arguments.length > 1 ? Number(flags || 0) : 0); },
    hexDecodeToByteArray: function(str) { return __readoriHexToByteArray(str); },
    hexDecodeToString: function(str) { return __readoriBytesToUtf8(__readoriHexToByteArray(str)); },
    hexEncodeToString: function(str) {
        return __readoriStringToBytes(str, 'utf-8').map(function(value) {
            return ('0' + (Number(value) & 255).toString(16)).slice(-2);
        }).join('');
    },
    md5Encode: function(str) {
        if (typeof __readoriCrypto !== 'undefined' && __readoriCrypto.createHash) {
            return __readoriCrypto.createHash('md5').update(String(str || ''), 'utf8').digest('hex');
        }
        return str;
    },
    digestHex: function(str, algorithm) {
        if (typeof __readoriCrypto !== 'undefined' && __readoriCrypto.createHash) {
            var alg = String(algorithm || 'MD5').toLowerCase().replace(/[^a-z0-9]/g, '');
            if (alg === 'sha1') alg = 'sha1';
            else if (alg === 'sha224') alg = 'sha224';
            else if (alg === 'sha256') alg = 'sha256';
            else if (alg === 'sha384') alg = 'sha384';
            else if (alg === 'sha512') alg = 'sha512';
            else alg = 'md5';
            return __readoriCrypto.createHash(alg).update(String(str || ''), 'utf8').digest('hex');
        }
        return String(str || '');
    },
    digestBase64Str: function(str, algorithm) {
        if (typeof __readoriCrypto !== 'undefined' && __readoriCrypto.createHash) {
            var alg = String(algorithm || 'MD5').toLowerCase().replace(/[^a-z0-9]/g, '');
            if (alg !== 'sha1' && alg !== 'sha224' && alg !== 'sha256'
                && alg !== 'sha384' && alg !== 'sha512') {
                alg = 'md5';
            }
            return __readoriCrypto.createHash(alg).update(String(str || ''), 'utf8').digest('base64');
        }
        return java.base64Encode(String(str || ''), 2);
    },
    md5Encode16: function(str) {
        var hash = java.md5Encode(str);
        return String(hash).length >= 24 ? String(hash).substring(8, 24) : String(hash);
    },
    inflateBytes: function(value) { return __readoriInflateBytes(value); },
    gzipDecodeBytes: function(value) { return __readoriGunzipBytes(value); },
    timeFormat: function(ts) { return __readoriFormatTimestamp(ts, 'yyyy/MM/dd HH:mm', false); },
    timeFormatUTC: function(ts, fmt, offset) { return __readoriFormatTimestamp(ts, fmt, true, offset); },
    t2s: function(str) { return __readoriConvertChinese(str, __readoriT2SMap); },
    s2t: function(str) { return __readoriConvertChinese(str, __readoriS2TMap); },
    toNumChapter: function(str) { return __readoriToNumChapter(str); },
    utf8ToGbk: function(str) { return str; },
    strToBytes: function(str, charset) { return __readoriStringToBytes(str, arguments.length > 1 ? charset : 'utf-8'); },
    bytesToStr: function(bytes, charset) { return __readoriBytesToString(bytes, arguments.length > 1 ? charset : 'utf-8'); },
    encodeURI: function(value, charset) { return __readoriPercentEncode(value, charset); },
    HMacHex: function(text, algorithm, key) {
        if (typeof __readoriCrypto !== 'undefined' && __readoriCrypto.createHmac) {
            return __readoriCrypto.createHmac(__readoriHmacAlgorithm(algorithm), __readoriBufferFromBytes(key))
                .update(String(text || ''), 'utf8')
                .digest('hex');
        }
        return String(text || '');
    },
    HMacBase64: function(text, algorithm, key) {
        if (typeof __readoriCrypto !== 'undefined' && __readoriCrypto.createHmac) {
            return __readoriCrypto.createHmac(__readoriHmacAlgorithm(algorithm), __readoriBufferFromBytes(key))
                .update(String(text || ''), 'utf8')
                .digest('base64');
        }
        return java.base64Encode(String(text || ''));
    },
    randomUUID: function() {
        var value = (typeof __readoriCrypto !== 'undefined' && __readoriCrypto.randomUUID)
            ? __readoriCrypto.randomUUID()
            : '00000000-0000-4000-8000-000000000000';
        // R157 (searchExplore G4): JsExtensions.randomUUID() returns a Java String, so `.replace(...)` / `.length` work on it.
        return value;
    },
    androidId: function() { return '0000000000000000'; },
    deviceID: function() { return '0000000000000000'; },
    aesEncrypt: function(data, key, mode, iv) { return data; },
    aesDecrypt: function(data, key, mode, iv) { return data; },
    aesBase64DecodeToString: function(data, key, mode, iv) {
        return java.createSymmetricCrypto(
            mode || 'AES/CBC/PKCS5Padding',
            key,
            iv
        ).decryptStr(data);
    },
    aesBase64DecodeToByteArray: function(data, key, mode, iv) {
        return java.createSymmetricCrypto(mode || 'AES/CBC/PKCS5Padding', key, iv).decrypt(data);
    },
    aesEncodeToBase64String: function(data, key, mode, iv) {
        return java.createSymmetricCrypto(mode || 'AES/CBC/PKCS5Padding', key, iv).encryptBase64(data);
    },
    createSymmetricCrypto: function(mode, key, iv) {
        function cryptBytes(input, operation) {
            try {
                if (typeof Buffer === 'undefined' || typeof __readoriCrypto === 'undefined') {
                    return __readoriToByteArray(input);
                }
                var keyBuf = __readoriNormalizeCipherKey(mode, key);
                var ivBuf = String(mode || '').toUpperCase().indexOf('/ECB/') >= 0 ? null : __readoriNormalizeCipherIv(mode, iv);
                var fn = operation === 1 ? __readoriCrypto.createCipheriv : __readoriCrypto.createDecipheriv;
                if (!fn) return [];
                var cipher = fn(__readoriCipherAlgorithm(mode, keyBuf), keyBuf, ivBuf);
                cipher.setAutoPadding(String(mode || '').toUpperCase().indexOf('NOPADDING') < 0);
                var source;
                if (operation === 1) {
                    source = __readoriBufferFromBytes(input);
                } else if (typeof input === 'string' || Object.prototype.toString.call(input) === '[object String]') {
                    var encoded = String(input || '').trim();
                    source = encoded && encoded.length % 2 === 0 && /^[0-9a-f]+$/i.test(encoded)
                        ? Buffer.from(encoded, 'hex')
                        : __readoriBase64ToBuffer(encoded);
                } else {
                    source = __readoriBufferFromBytes(input);
                }
                var out = Buffer.concat([cipher.update(source), cipher.final()]);
                return __readoriByteArrayWithUtf8String(Array.from(out));
            } catch (error) {
                return [];
            }
        }
        return {
            encrypt: function(value) { return cryptBytes(value, 1); },
            encryptBase64: function(value) {
                return Base64.getEncoder().encodeToString(cryptBytes(value, 1));
            },
            encryptHex: function(value) {
                return cryptBytes(value, 1).map(function(byte) {
                    return ('0' + (Number(byte) & 255).toString(16)).slice(-2);
                }).join('');
            },
            encryptStr: function(value) {
                return Base64.getEncoder().encodeToString(cryptBytes(value, 1));
            },
            decrypt: function(value) { return cryptBytes(value, 2); },
            decryptBase64: function(value) { return cryptBytes(value, 2); },
            decryptStr: function(value) { return __readoriBytesToUtf8(cryptBytes(value, 2)); }
        };
    },
    createAsymmetricCrypto: function(transformation) {
        var algorithm = String(transformation || 'RSA');
        var privateKey = null;
        var publicKey = null;

        function ensureKeys() {
            if (privateKey || publicKey || typeof __readoriCrypto === 'undefined'
                || !__readoriCrypto.generateKeyPairSync) {
                return;
            }
            var pair = __readoriCrypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
            privateKey = pair.privateKey;
            publicKey = pair.publicKey;
        }

        function importPrivateKey(value) {
            if (typeof __readoriCrypto === 'undefined' || !__readoriCrypto.createPrivateKey) return null;
            var source = typeof value === 'string' ? String(value) : __readoriBufferFromBytes(value);
            var attempts = [
                function() { return __readoriCrypto.createPrivateKey(source); },
                function() {
                    return __readoriCrypto.createPrivateKey({
                        key: __readoriBufferFromBytes(value), format: 'der', type: 'pkcs8'
                    });
                },
                function() {
                    return __readoriCrypto.createPrivateKey({
                        key: __readoriBufferFromBytes(value), format: 'der', type: 'pkcs1'
                    });
                }
            ];
            for (var i = 0; i < attempts.length; i++) {
                try { return attempts[i](); } catch (error) {}
            }
            return null;
        }

        function importPublicKey(value) {
            if (typeof __readoriCrypto === 'undefined' || !__readoriCrypto.createPublicKey) return null;
            var source = typeof value === 'string' ? String(value) : __readoriBufferFromBytes(value);
            var attempts = [
                function() { return __readoriCrypto.createPublicKey(source); },
                function() {
                    return __readoriCrypto.createPublicKey({
                        key: __readoriBufferFromBytes(value), format: 'der', type: 'spki'
                    });
                },
                function() {
                    return __readoriCrypto.createPublicKey({
                        key: __readoriBufferFromBytes(value), format: 'der', type: 'pkcs1'
                    });
                }
            ];
            for (var i = 0; i < attempts.length; i++) {
                try { return attempts[i](); } catch (error) {}
            }
            return null;
        }

        function cryptoOptions(key) {
            var upper = algorithm.toUpperCase();
            var constants = __readoriCrypto.constants || {};
            var options = { key: key };
            if (upper.indexOf('NOPADDING') >= 0) {
                options.padding = constants.RSA_NO_PADDING;
            } else if (upper.indexOf('OAEP') >= 0) {
                options.padding = constants.RSA_PKCS1_OAEP_PADDING;
                if (upper.indexOf('SHA-512') >= 0 || upper.indexOf('SHA512') >= 0) options.oaepHash = 'sha512';
                else if (upper.indexOf('SHA-384') >= 0 || upper.indexOf('SHA384') >= 0) options.oaepHash = 'sha384';
                else if (upper.indexOf('SHA-256') >= 0 || upper.indexOf('SHA256') >= 0) options.oaepHash = 'sha256';
                else options.oaepHash = 'sha1';
            } else {
                options.padding = constants.RSA_PKCS1_PADDING;
            }
            return options;
        }

        function keyBlockSize(key) {
            try {
                var bits = key.asymmetricKeyDetails && key.asymmetricKeyDetails.modulusLength;
                return bits ? Math.ceil(bits / 8) : 256;
            } catch (error) {
                return 256;
            }
        }

        function transform(value, encrypting, usePublicKey) {
            try {
                ensureKeys();
                var usePublic = usePublicKey === undefined || usePublicKey === null
                    ? true : Boolean(usePublicKey);
                var key = usePublic ? publicKey : privateKey;
                if (!key || typeof __readoriCrypto === 'undefined') return [];
                var source;
                if (encrypting) {
                    source = __readoriBufferFromBytes(value);
                } else if (typeof value === 'string' || Object.prototype.toString.call(value) === '[object String]') {
                    var encoded = String(value || '').trim();
                    source = encoded && encoded.length % 2 === 0 && /^[0-9a-f]+$/i.test(encoded)
                        ? Buffer.from(encoded, 'hex')
                        : __readoriBase64ToBuffer(encoded);
                } else {
                    source = __readoriBufferFromBytes(value);
                }
                var fn;
                if (encrypting) {
                    fn = usePublic ? __readoriCrypto.publicEncrypt : __readoriCrypto.privateEncrypt;
                } else {
                    fn = usePublic ? __readoriCrypto.publicDecrypt : __readoriCrypto.privateDecrypt;
                }
                if (!fn) return [];
                var blockSize = keyBlockSize(key);
                var upper = algorithm.toUpperCase();
                var constants = __readoriCrypto.constants || {};
                var sourceBlockSize = blockSize;
                if (encrypting) {
                    if (upper.indexOf('NOPADDING') >= 0) {
                        sourceBlockSize = blockSize;
                    } else if (upper.indexOf('OAEP') >= 0) {
                        var hashLength = upper.indexOf('512') >= 0 ? 64
                            : (upper.indexOf('384') >= 0 ? 48 : (upper.indexOf('256') >= 0 ? 32 : 20));
                        sourceBlockSize = blockSize - 2 * hashLength - 2;
                    } else {
                        sourceBlockSize = blockSize - 11;
                    }
                }
                var chunks = [];
                var pkcs1Failed = false;
                try {
                    for (var offset = 0; offset < source.length; offset += sourceBlockSize) {
                        chunks.push(fn(cryptoOptions(key), source.subarray(offset, offset + sourceBlockSize)));
                    }
                } catch (padError) {
                    // R158 A (1.1.2 `rsa_bare_public_decrypt_no_padding`; bookld): Legado JsEncodeUtils.kt:77 hands the bare transformation "RSA" to hutool
                    // AsymmetricCrypto -> Cipher.getInstance("RSA") (no explicit padding: raw RSA on Android, the leading zero bytes of every block are
                    // dropped). Data encrypted by the server WITHOUT padding fails the PKCS1 type-1 check. Only a public-key decrypt with no `/` in the
                    // transformation whose PKCS1 pass FAILED is redone as NoPadding over the whole input (App rawRSAPublicDecryptDroppingLeadingZeros).
                    if (typeof __readoriRsaBareNoPadding === 'undefined' || !__readoriRsaBareNoPadding
                        || encrypting || !usePublic || algorithm.indexOf('/') >= 0) throw padError;
                    pkcs1Failed = true;
                }
                if (pkcs1Failed) {
                    chunks = [];
                    if (source.length === 0 || source.length % blockSize !== 0) return [];
                    for (var rawOffset = 0; rawOffset < source.length; rawOffset += blockSize) {
                        var rawBlock = fn({ key: key, padding: constants.RSA_NO_PADDING }, source.subarray(rawOffset, rawOffset + blockSize));
                        var skip = 0;
                        while (skip < rawBlock.length && rawBlock[skip] === 0) skip++;
                        chunks.push(rawBlock.subarray(skip));
                    }
                }
                var output = chunks.length ? Buffer.concat(chunks) : Buffer.alloc(0);
                return __readoriByteArrayWithUtf8String(Array.from(output));
            } catch (error) {
                return [];
            }
        }

        function exportKey(key, type) {
            if (!key) return '';
            try {
                return key.export({ format: 'der', type: type }).toString('base64');
            } catch (error) {
                return '';
            }
        }

        var api = {
            setPrivateKey: function(value) {
                privateKey = importPrivateKey(value);
                if (privateKey && !publicKey) {
                    try { publicKey = __readoriCrypto.createPublicKey(privateKey); } catch (error) {}
                }
                return api;
            },
            setPublicKey: function(value) {
                publicKey = importPublicKey(value);
                return api;
            },
            setKey: function(value) {
                var importedPrivate = importPrivateKey(value);
                if (importedPrivate) {
                    privateKey = importedPrivate;
                    try { publicKey = __readoriCrypto.createPublicKey(privateKey); } catch (error) {}
                } else {
                    publicKey = importPublicKey(value);
                }
                return api;
            },
            getPrivateKeyBase64: function() {
                ensureKeys();
                return exportKey(privateKey, 'pkcs8');
            },
            getPublicKeyBase64: function() {
                ensureKeys();
                return exportKey(publicKey, 'spki');
            },
            encrypt: function(value, usePublicKey) {
                return transform(value, true, usePublicKey);
            },
            encryptHex: function(value, usePublicKey) {
                return transform(value, true, usePublicKey).map(function(byte) {
                    return ('0' + (Number(byte) & 255).toString(16)).slice(-2);
                }).join('');
            },
            encryptBase64: function(value, usePublicKey) {
                return Base64.getEncoder().encodeToString(transform(value, true, usePublicKey));
            },
            decrypt: function(value, usePublicKey) {
                return transform(value, false, usePublicKey);
            },
            decryptStr: function(value, usePublicKey) {
                return __readoriBytesToUtf8(transform(value, false, usePublicKey));
            }
        };
        ensureKeys();
        return api;
    },
    createSign: function(algorithm) {
        var signAlgorithm = String(algorithm || 'SHA256withRSA');
        var privateKey = null;
        var publicKey = null;

        function keyKind() {
            return /ECDSA/i.test(signAlgorithm) ? 'ec' : (/DSA/i.test(signAlgorithm) ? 'dsa' : 'rsa');
        }

        function ensureKeys() {
            if (privateKey || publicKey || typeof __readoriCrypto === 'undefined'
                || !__readoriCrypto.generateKeyPairSync) {
                return;
            }
            var kind = keyKind();
            var pair;
            if (kind === 'ec') {
                pair = __readoriCrypto.generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
            } else if (kind === 'dsa') {
                pair = __readoriCrypto.generateKeyPairSync('dsa', {
                    modulusLength: 2048, divisorLength: 256
                });
            } else {
                pair = __readoriCrypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
            }
            privateKey = pair.privateKey;
            publicKey = pair.publicKey;
        }

        function importPrivateKey(value) {
            if (typeof __readoriCrypto === 'undefined' || !__readoriCrypto.createPrivateKey) return null;
            var source = typeof value === 'string' ? String(value) : __readoriBufferFromBytes(value);
            var attempts = [
                function() { return __readoriCrypto.createPrivateKey(source); },
                function() {
                    return __readoriCrypto.createPrivateKey({
                        key: __readoriBufferFromBytes(value), format: 'der', type: 'pkcs8'
                    });
                },
                function() {
                    return __readoriCrypto.createPrivateKey({
                        key: __readoriBufferFromBytes(value), format: 'der',
                        type: keyKind() === 'ec' ? 'sec1' : 'pkcs1'
                    });
                }
            ];
            for (var i = 0; i < attempts.length; i++) {
                try { return attempts[i](); } catch (error) {}
            }
            return null;
        }

        function importPublicKey(value) {
            if (typeof __readoriCrypto === 'undefined' || !__readoriCrypto.createPublicKey) return null;
            var source = typeof value === 'string' ? String(value) : __readoriBufferFromBytes(value);
            var attempts = [
                function() { return __readoriCrypto.createPublicKey(source); },
                function() {
                    return __readoriCrypto.createPublicKey({
                        key: __readoriBufferFromBytes(value), format: 'der', type: 'spki'
                    });
                },
                function() {
                    return __readoriCrypto.createPublicKey({
                        key: __readoriBufferFromBytes(value), format: 'der', type: 'pkcs1'
                    });
                }
            ];
            for (var i = 0; i < attempts.length; i++) {
                try { return attempts[i](); } catch (error) {}
            }
            return null;
        }

        function hashName() {
            var upper = signAlgorithm.toUpperCase().replace(/[^A-Z0-9]/g, '');
            if (upper.indexOf('SHA512') >= 0) return 'sha512';
            if (upper.indexOf('SHA384') >= 0) return 'sha384';
            if (upper.indexOf('SHA256') >= 0) return 'sha256';
            if (upper.indexOf('SHA224') >= 0) return 'sha224';
            if (upper.indexOf('SHA1') >= 0) return 'sha1';
            if (upper.indexOf('MD5') >= 0) return 'md5';
            return null;
        }

        function signatureOptions(key) {
            var options = { key: key };
            if (/PSS/i.test(signAlgorithm) && __readoriCrypto.constants) {
                options.padding = __readoriCrypto.constants.RSA_PKCS1_PSS_PADDING;
                options.saltLength = __readoriCrypto.constants.RSA_PSS_SALTLEN_DIGEST;
            }
            return options;
        }

        function sourceBytes(value) {
            return __readoriBufferFromBytes(value);
        }

        function exportKey(key, type) {
            if (!key) return '';
            try { return key.export({ format: 'der', type: type }).toString('base64'); }
            catch (error) { return ''; }
        }

        var api = {
            setPrivateKey: function(value) {
                privateKey = importPrivateKey(value);
                if (privateKey && !publicKey) {
                    try { publicKey = __readoriCrypto.createPublicKey(privateKey); } catch (error) {}
                }
                return api;
            },
            setPublicKey: function(value) {
                publicKey = importPublicKey(value);
                return api;
            },
            setKey: function(value) {
                var importedPrivate = importPrivateKey(value);
                if (importedPrivate) {
                    privateKey = importedPrivate;
                    try { publicKey = __readoriCrypto.createPublicKey(privateKey); } catch (error) {}
                } else {
                    publicKey = importPublicKey(value);
                }
                return api;
            },
            getPrivateKeyBase64: function() {
                ensureKeys();
                return exportKey(privateKey, 'pkcs8');
            },
            getPublicKeyBase64: function() {
                ensureKeys();
                return exportKey(publicKey, 'spki');
            },
            sign: function(value) {
                try {
                    ensureKeys();
                    if (!privateKey) return [];
                    var result = __readoriCrypto.sign(
                        hashName(), sourceBytes(value), signatureOptions(privateKey)
                    );
                    return __readoriByteArrayWithUtf8String(Array.from(result));
                } catch (error) {
                    return [];
                }
            },
            signHex: function(value) {
                return api.sign(value).map(function(byte) {
                    return ('0' + (Number(byte) & 255).toString(16)).slice(-2);
                }).join('');
            },
            verify: function(value, signature) {
                try {
                    ensureKeys();
                    return Boolean(publicKey && __readoriCrypto.verify(
                        hashName(),
                        sourceBytes(value),
                        signatureOptions(publicKey),
                        sourceBytes(signature)
                    ));
                } catch (error) {
                    return false;
                }
            }
        };
        ensureKeys();
        return api;
    },
    aesDecodeToByteArray: function(data, key, transformation, iv) {
        return java.createSymmetricCrypto(transformation, key, iv).decrypt(data);
    },
    aesDecodeToString: function(data, key, transformation, iv) {
        return java.createSymmetricCrypto(transformation, key, iv).decryptStr(data);
    },
    aesDecodeArgsBase64Str: function(data, key, mode, padding, iv) {
        return java.createSymmetricCrypto(
            'AES/' + mode + '/' + padding,
            Base64.decode(key, 2),
            Base64.decode(iv, 2)
        ).decryptStr(data);
    },
    aesEncodeToByteArray: function(data, key, transformation, iv) {
        return java.createSymmetricCrypto(transformation, key, iv).encrypt(data);
    },
    aesEncodeToString: function(data, key, transformation, iv) {
        return java.createSymmetricCrypto(transformation, key, iv).decryptStr(data);
    },
    aesEncodeToBase64ByteArray: function(data, key, transformation, iv) {
        return __readoriStringToBytes(
            java.createSymmetricCrypto(transformation, key, iv).encryptBase64(data),
            'utf-8'
        );
    },
    aesEncodeArgsBase64Str: function(data, key, mode, padding, iv) {
        return java.createSymmetricCrypto(
            'AES/' + mode + '/' + padding,
            key,
            iv
        ).encryptBase64(data);
    },
    desEncrypt: function(data, key, mode, iv) { return data; },
    desDecrypt: function(data, key, mode, iv) { return data; },
    desDecodeToString: function(data, key, transformation, iv) {
        return java.createSymmetricCrypto(transformation, key, iv).decryptStr(data);
    },
    desBase64DecodeToString: function(data, key, transformation, iv) {
        return java.createSymmetricCrypto(transformation, key, iv).decryptStr(data);
    },
    desEncodeToString: function(data, key, transformation, iv) {
        return __readoriBytesToUtf8(
            java.createSymmetricCrypto(transformation, key, iv).encrypt(data)
        );
    },
    desEncodeToBase64String: function(data, key, mode, iv) {
        return java.createSymmetricCrypto(mode || 'DES/ECB/PKCS5Padding', key, iv || '').encryptBase64(data);
    },
    tripleDESDecodeStr: function(data, key, mode, padding, iv) {
        return java.createSymmetricCrypto(
            'DESede/' + mode + '/' + padding,
            key,
            iv
        ).decryptStr(data);
    },
    tripleDESDecodeArgsBase64Str: function(data, key, mode, padding, iv) {
        return java.createSymmetricCrypto(
            'DESede/' + mode + '/' + padding,
            Base64.decode(key, 2),
            __readoriStringToBytes(iv, 'utf-8')
        ).decryptStr(data);
    },
    tripleDESEncodeBase64Str: function(data, key, mode, padding, iv) {
        return java.createSymmetricCrypto(
            'DESede/' + mode + '/' + padding,
            key,
            iv
        ).encryptBase64(data);
    },
    tripleDESEncodeArgsBase64Str: function(data, key, mode, padding, iv) {
        return java.createSymmetricCrypto(
            'DESede/' + mode + '/' + padding,
            Base64.decode(key, 2),
            __readoriStringToBytes(iv, 'utf-8')
        ).encryptBase64(data);
    },
    getResponse: function(url, requestHeaders) { return __readoriFetchResponse(url, 'GET', '', requestHeaders || {}, false); },
    getStrResponse: function(jsStr, sourceRegex, useWebView) {
        return __readoriFetchResponse(
            (typeof ruleUrl !== 'undefined' && String(ruleUrl || '').trim())
                ? String(ruleUrl)
                : String(baseUrl || ''),
            (typeof ruleRequestMethod !== 'undefined' && String(ruleRequestMethod || '').toUpperCase() === 'POST')
                ? 'POST'
                : 'GET',
            (typeof ruleRequestBody !== 'undefined') ? String(ruleRequestBody || '') : '',
            headers || {},
            true
        );
    },
    initUrl: function() { return ''; },
    getByteResponse: function(url) { return ''; },
    post: function(url, body, requestHeaders) { return __readoriFetchResponse(url, 'POST', body || '', requestHeaders || {}, false); },
    postBody: function(url, body, requestHeaders) { return __readoriFetchResponse(url, 'POST', body || '', requestHeaders || {}, false).body(); },
    getCookie: function(url, key) {
        if (arguments.length > 1 && key) return __readoriCookieValue(url, key);
        return __readoriCookieHeader(url);
    },
    getWebViewUA: function() { return 'Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 Chrome/102.0.0.0 Mobile Safari/537.36'; },
    getUserAgent: function() { return 'Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 Chrome/102.0.0.0 Mobile Safari/537.36'; },
    refreshExplore: function() { return ''; },
    refreshJSLib: function() { return ''; },
    refreshTocUrl: function() { return ''; },
    reGetBook: function() { return ''; },
    refreshContent: function() { return ''; },
    refreshBookUrl: function() { return ''; },
    openVideoPlayer: function(url, title, float) { return ''; },
    getVerificationCode: function(url) {
        __readoriInteractionBlocked('getVerificationCode');
        return '';
    },
    queryTTF: function(urlOrBase64) { return { replaceText: function(text) { return text; } }; },
    queryBase64TTF: function(base64) { return java.queryTTF(base64); },
    replaceFont: function(text, font) {
        try {
            if (font && typeof font.replaceText === 'function') return font.replaceText(text);
        } catch (error) {}
        return text;
    },
    htmlFormat: function(text) { return __readoriHtmlFormatKeepImg(text); },
};
function __readoriRequestBodyText(value) {
    if (value === null || value === undefined) return '';
    if (value._content !== undefined) return String(value._content || '');
    if (typeof value === 'string' || value instanceof String) return String(value);
    if (typeof value.length === 'number') return java.bytesToStr(value);
    return String(value);
}
function __readoriOkHttpResponse(response, request) {
    return {
        body: function() {
            return {
                string: function(){ return response.body(); },
                bytes: function(){ return __readoriListArray(response.bodyAsBytes()); },
                byteStream: function(){ return new Packages.java.io.ByteArrayInputStream(response.bodyAsBytes()); },
                contentLength: function(){ return response.bodyAsBytes().length; },
                close: function(){}
            };
        },
        code: function(){ return response.code(); },
        isSuccessful: function(){ return response.isSuccessful(); },
        header: function(name, fallback) {
            var value = response.header(String(name || ''));
            return value || (arguments.length > 1 ? fallback : null);
        },
        headers: function(){ return response.headers(); },
        request: function(){ return request || null; },
        close: function(){},
        toString: function(){ return response.body(); }
    };
}
function __readoriOkHttpRequestBuilder(existing) {
    var seed = existing || {};
    this.urlValue = String(seed.urlValue || '');
    this.methodValue = String(seed.methodValue || 'GET').toUpperCase();
    this.bodyValue = seed.bodyValue === undefined ? '' : seed.bodyValue;
    this.headerValues = Object.assign({}, seed.headerValues || {});
}
__readoriOkHttpRequestBuilder.prototype.url = function(value) {
    this.urlValue = String(value === null || value === undefined ? '' : value);
    return this;
};
__readoriOkHttpRequestBuilder.prototype.method = function(method, body) {
    this.methodValue = String(method || 'GET').toUpperCase();
    this.bodyValue = body === null || body === undefined ? '' : body;
    return this;
};
__readoriOkHttpRequestBuilder.prototype.get = function(){ return this.method('GET', ''); };
__readoriOkHttpRequestBuilder.prototype.post = function(body){ return this.method('POST', body); };
__readoriOkHttpRequestBuilder.prototype.put = function(body){ return this.method('PUT', body); };
__readoriOkHttpRequestBuilder.prototype.patch = function(body){ return this.method('PATCH', body); };
__readoriOkHttpRequestBuilder.prototype.delete = function(body) {
    return this.method('DELETE', arguments.length ? body : '');
};
__readoriOkHttpRequestBuilder.prototype.head = function(){ return this.method('HEAD', ''); };
__readoriOkHttpRequestBuilder.prototype.header = function(name, value) {
    this.headerValues[String(name || '')] = String(value || '');
    return this;
};
__readoriOkHttpRequestBuilder.prototype.addHeader = __readoriOkHttpRequestBuilder.prototype.header;
__readoriOkHttpRequestBuilder.prototype.removeHeader = function(name) {
    var wanted = String(name || '').toLowerCase();
    Object.keys(this.headerValues).forEach(function(key) {
        if (key.toLowerCase() === wanted) delete this.headerValues[key];
    }, this);
    return this;
};
__readoriOkHttpRequestBuilder.prototype.build = function() {
    return {
        urlValue: this.urlValue,
        methodValue: this.methodValue,
        bodyValue: this.bodyValue,
        headerValues: this.headerValues,
        url: function(){ return this.urlValue; },
        method: function(){ return this.methodValue; },
        body: function(){ return this.bodyValue; },
        header: function(name) {
            var wanted = String(name || '').toLowerCase();
            var names = Object.keys(this.headerValues);
            for (var i = 0; i < names.length; i++) {
                if (names[i].toLowerCase() === wanted) return this.headerValues[names[i]];
            }
            return null;
        },
        headers: function(){ return __readoriMapObject(this.headerValues); },
        newBuilder: function(){ return new __readoriOkHttpRequestBuilder(this); },
        toString: function(){ return this.methodValue + ' ' + this.urlValue; }
    };
};
function __readoriOkHttpRequest() {}
__readoriOkHttpRequest.Builder = __readoriOkHttpRequestBuilder;
function __readoriOkHttpClientBuilder(existing) {
    this.followRedirectsValue = !existing || existing.followRedirectsValue !== false;
}
__readoriOkHttpClientBuilder.prototype.followRedirects = function(value) {
    this.followRedirectsValue = !!value;
    return this;
};
__readoriOkHttpClientBuilder.prototype.followSslRedirects =
    __readoriOkHttpClientBuilder.prototype.followRedirects;
[
    'connectTimeout', 'readTimeout', 'writeTimeout', 'callTimeout',
    'retryOnConnectionFailure', 'addInterceptor', 'addNetworkInterceptor'
].forEach(function(name) {
    __readoriOkHttpClientBuilder.prototype[name] = function(){ return this; };
});
__readoriOkHttpClientBuilder.prototype.build = function() {
    return new __readoriOkHttpClient(this);
};
function __readoriOkHttpClient(options) {
    this.followRedirectsValue = !options || options.followRedirectsValue !== false;
}
__readoriOkHttpClient.Builder = __readoriOkHttpClientBuilder;
__readoriOkHttpClient.prototype.newBuilder = function() {
    return new __readoriOkHttpClientBuilder(this);
};
__readoriOkHttpClient.prototype.newCall = function(request) {
    var client = this;
    return {
        execute: function() {
            return __readoriOkHttpResponse(
                __readoriFetchResponse(
                    String(request && request.urlValue || ''),
                    String(request && request.methodValue || 'GET'),
                    __readoriRequestBodyText(request && request.bodyValue),
                    request && request.headerValues || {},
                    client.followRedirectsValue
                ),
                request
            );
        },
        cancel: function(){},
        isCanceled: function(){ return false; }
    };
};
var __readoriOkHttpMediaType = {
    parse: function(value){ return String(value || ''); },
    get: function(value){ return String(value || ''); }
};
var __readoriOkHttpRequestBody = {
    create: function(mediaType, content) {
        var firstLooksLikeMediaType = typeof mediaType === 'string' &&
            /^[\w.+-]+\/[\w.+-]+(?:\s*;.*)?$/i.test(mediaType);
        var value = arguments.length > 1
            ? (firstLooksLikeMediaType ? content : mediaType)
            : mediaType;
        var type = arguments.length > 1
            ? (firstLooksLikeMediaType ? mediaType : content)
            : '';
        return { _content: __readoriRequestBodyText(value), _mediaType: String(type || '') };
    }
};
function __readoriOkHttpFormBodyBuilder() {
    this.values = [];
}
__readoriOkHttpFormBodyBuilder.prototype.add = function(name, value) {
    this.values.push(
        encodeURIComponent(String(name || '')) + '=' +
        encodeURIComponent(String(value || '')).replace(/%20/g, '+')
    );
    return this;
};
__readoriOkHttpFormBodyBuilder.prototype.addEncoded = function(name, value) {
    this.values.push(String(name || '') + '=' + String(value || ''));
    return this;
};
__readoriOkHttpFormBodyBuilder.prototype.build = function() {
    return { _content: this.values.join('&') };
};
function __readoriOkHttpFormBody() {}
__readoriOkHttpFormBody.Builder = __readoriOkHttpFormBodyBuilder;
Packages.okhttp3 = {
    Request: __readoriOkHttpRequest,
    OkHttpClient: __readoriOkHttpClient,
    MediaType: __readoriOkHttpMediaType,
    RequestBody: __readoriOkHttpRequestBody,
    FormBody: __readoriOkHttpFormBody
};
function __readoriJsoupConnection(url) {
    var state = {
        url: String(url || ''),
        method: 'GET',
        body: '',
        headers: {},
        followRedirects: true
    };
    var connection = {
        method: function(value){ state.method = String(value || 'GET').toUpperCase(); return this; },
        header: function(name, value){ state.headers[String(name || '')] = String(value || ''); return this; },
        headers: function(values) {
            values = values || {};
            Object.keys(values).forEach(function(key){ state.headers[String(key)] = String(values[key]); });
            return this;
        },
        requestBody: function(value){ state.body = String(value === null || value === undefined ? '' : value); return this; },
        data: function(name, value) {
            var pair = encodeURIComponent(String(name || '')) + '=' +
                encodeURIComponent(String(value || '')).replace(/%20/g, '+');
            state.body += (state.body ? '&' : '') + pair;
            if (state.method === 'GET') state.method = 'POST';
            return this;
        },
        cookie: function(name, value) {
            var pair = String(name || '') + '=' + String(value || '');
            state.headers.Cookie = state.headers.Cookie ? state.headers.Cookie + '; ' + pair : pair;
            return this;
        },
        cookies: function(values) {
            values = values || {};
            Object.keys(values).forEach(function(key){ connection.cookie(key, values[key]); });
            return this;
        },
        userAgent: function(value){ return this.header('User-Agent', value); },
        referrer: function(value){ return this.header('Referer', value); },
        followRedirects: function(value){ state.followRedirects = !!value; return this; },
        timeout: function(){ return this; },
        maxBodySize: function(){ return this; },
        ignoreContentType: function(){ return this; },
        ignoreHttpErrors: function(){ return this; },
        request: function() {
            return {
                url: function(){ return state.url; },
                method: function(){ return state.method; },
                requestBody: function(){ return state.body; },
                header: function(name) {
                    var wanted = String(name || '').toLowerCase();
                    var names = Object.keys(state.headers);
                    for (var i = 0; i < names.length; i++) {
                        if (names[i].toLowerCase() === wanted) return state.headers[names[i]];
                    }
                    return '';
                },
                headers: function(){ return __readoriMapObject(state.headers); }
            };
        },
        execute: function() {
            var response = __readoriFetchResponse(
                state.url,
                state.method,
                state.body,
                state.headers,
                state.followRedirects
            );
            // R157 (js baidu): Jsoup Response.bodyAsBytes() is the raw payload (binary / encrypted bodies), not the decoded text
            // re-encoded. A recorded fixture already carries its bytes (bodyBase64); otherwise a plain GET is re-read as bytes.
            if (state.method === 'GET' && !state.body && typeof __readoriAjaxBytesBase64 === 'function'
                && !(typeof __readoriLookupResponseFixture === 'function' && __readoriLookupResponseFixture(state.url))) {
                var rawTarget = state.url;
                response.bodyAsBytes = function() {
                    try { return Base64.decode(__readoriAjaxBytesBase64(rawTarget), 0); } catch (error) {}
                    return __readoriSignedBytes(__readoriToByteArray(response.body()));
                };
            }
            return response;
        },
        get: function(){ state.method = 'GET'; return this.execute().body(); },
        post: function(){ state.method = 'POST'; return this.execute().body(); }
    };
    return connection;
}
Packages.org = {
    jsoup: {
        Connection: {
            Method: {
                GET: 'GET', POST: 'POST', PUT: 'PUT', DELETE: 'DELETE',
                PATCH: 'PATCH', HEAD: 'HEAD', OPTIONS: 'OPTIONS', TRACE: 'TRACE'
            }
        },
        Jsoup: { connect: __readoriJsoupConnection }
    }
};
var source = {
    getKey: function() { return bookSourceUrl || ''; },
    getTag: function() { return sourceBookSourceName || bookSourceUrl || ''; },
    getSource: function() { return source; },
    key: bookSourceUrl || '',
    url: bookSourceUrl || '',
    bookSourceUrl: bookSourceUrl || '',
    name: sourceBookSourceName || bookSourceUrl || '',
    bookSourceName: sourceBookSourceName || '',
    bookSourceType: Number(sourceBookSourceType || 0),
    loginUrl: sourceLoginUrl || '',
    loginUi: typeof sourceLoginUi !== 'undefined' ? String(sourceLoginUi || '') : '',
    header: typeof sourceHeader !== 'undefined' ? String(sourceHeader || '') : '',
    jsLib: typeof sourceJSLib !== 'undefined' ? String(sourceJSLib || '') : '',
    enabledCookieJar: typeof sourceEnabledCookieJar === 'undefined' ? true : !!sourceEnabledCookieJar,
    bookSourceGroup: typeof sourceBookSourceGroup !== 'undefined' ? String(sourceBookSourceGroup || '') : '',
    exploreUrl: typeof sourceExploreUrl !== 'undefined' ? String(sourceExploreUrl || '') : '',
    searchUrl: typeof sourceSearchUrl !== 'undefined' ? String(sourceSearchUrl || '') : '',
    bookSourceComment: sourceBookSourceComment || '',
    variableComment: sourceVariableComment || '',
    lastUpdateTime: Number(sourceLastUpdateTime || 0),
    concurrentRate: typeof sourceConcurrentRate !== 'undefined' ? String(sourceConcurrentRate || '') : '',
    getVariable: function(key) {
        if (typeof variables === 'undefined') return '';
        if (arguments.length === 0 || key === undefined || key === null || String(key) === '') {
            return variables.source || variables.custom || '';
        }
        return variables[String(key)] || '';
    },
    putVariable: function(key, value) {
        if (typeof variables !== 'undefined') variables[String(key || '')] = value;
        return value;
    },
    setVariable: function(value) {
        if (arguments.length > 1) return source.putVariable(value, arguments[1]);
        if (typeof variables !== 'undefined') {
            if (value === null || value === undefined) delete variables.source;
            else variables.source = value;
        }
        return value;
    },
    removeVariable: function(key) {
        if (typeof variables === 'undefined') return;
        if (arguments.length === 0 || key === undefined || key === null || String(key) === '') {
            delete variables.source;
            return;
        }
        delete variables[String(key || '')];
    },
    // Legado BaseSource.getLoginInfo/getLoginHeader return null when nothing is stored.
    getLoginInfo: function() { return typeof sourceLoginInfo !== 'undefined' && sourceLoginInfo ? String(sourceLoginInfo) : null; },
    getLoginHeader: function() { return typeof sourceLoginHeader !== 'undefined' && sourceLoginHeader ? String(sourceLoginHeader) : null; },
    getLoginInfoMap: function() {
        var raw = typeof sourceLoginInfo !== 'undefined' ? String(sourceLoginInfo || '').trim() : '';
        return raw ? __readoriParseMapText(raw) : null;
    },
    getLoginHeaderMap: function() {
        var raw = typeof sourceLoginHeader !== 'undefined' ? String(sourceLoginHeader || '').trim() : '';
        return raw ? __readoriParseMapText(raw, 'headers') : null;
    },
    putLoginInfo: function(value) {
        sourceLoginInfo = String(value || '');
        if (typeof variables !== 'undefined') variables.__sourceLoginInfo = sourceLoginInfo;
        return true;
    },
    putLoginHeader: function(value) {
        sourceLoginHeader = String(value || '');
        if (typeof variables !== 'undefined') variables.__sourceLoginHeader = sourceLoginHeader;
        return sourceLoginHeader;
    },
    removeLoginInfo: function() {
        sourceLoginInfo = '';
        if (typeof variables !== 'undefined') variables.__sourceLoginInfo = '';
    },
    removeLoginHeader: function() {
        sourceLoginHeader = '';
        if (typeof variables !== 'undefined') variables.__sourceLoginHeader = '';
    },
    getHeaderMap: function(includeLogin) {
        var base = __readoriMapObject(headers || {});
        var hasUA = Object.keys(headers || {}).some(function(key) {
            return String(key).toLowerCase() === 'user-agent';
        });
        if (!hasUA) base['User-Agent'] = java.getUserAgent();
        if (includeLogin) return __readoriMergeMapObjects(base, source.getLoginHeaderMap());
        return base;
    },
    getLoginJs: function() {
        var raw = String(source.loginUrl || '');
        if (raw.indexOf('@js:') === 0) return raw.slice(4);
        if (raw.indexOf('<js>') === 0 && raw.lastIndexOf('</js>') >= 4) {
            return raw.slice(4, raw.lastIndexOf('</js>'));
        }
        return raw || null;
    },
    evalJS: function(script) {
        return (0, eval)(String(script == null ? '' : script));
    },
    login: function() {
        var script = source.getLoginJs();
        if (!script) return;
        source.evalJS(script);
        if (typeof globalThis.login === 'function') return globalThis.login.apply(this, arguments);
        throw new Error('Function login not implements!!!');
    },
    refreshExplore: function() { return ''; },
    refreshJSLib: function() { return ''; },
};
function __readoriCookieStoreKey(url) {
    return '__cookie.' + String(url || bookSourceUrl || '');
}
function __readoriCookieHeader(url) {
    if (typeof variables === 'undefined') return '';
    return String(variables[__readoriCookieStoreKey(url)] || '');
}
function __readoriSetCookieHeader(url, value) {
    if (typeof variables !== 'undefined') variables[__readoriCookieStoreKey(url)] = String(value || '');
    return '';
}
function __readoriCookieValue(url, key) {
    var target = String(key || '');
    if (!target) return '';
    var parts = __readoriCookieHeader(url).split(';');
    for (var i = 0; i < parts.length; i++) {
        var part = parts[i].trim();
        if (!part) continue;
        var idx = part.indexOf('=');
        var name = idx >= 0 ? part.slice(0, idx).trim() : part;
        if (name === target) return idx >= 0 ? part.slice(idx + 1).trim() : '';
    }
    return '';
}
var cookie = {
    getCookie: function(url) { return __readoriCookieHeader(url); },
    removeCookie: function(url) {
        if (typeof variables !== 'undefined') delete variables[__readoriCookieStoreKey(url)];
        return '';
    },
    setCookie: function(url, value) { return __readoriSetCookieHeader(url, value); },
    setWebCookie: function(url, value) { return __readoriSetCookieHeader(url, value); },
    replaceCookie: function(url, value) { return __readoriSetCookieHeader(url, value); },
    getKey: function(url, key) { return __readoriCookieValue(url, key); },
};
var cache = {
    get: function(key) { return __readoriCacheGet('value', key); },
    getInt: function(key) { return __readoriCacheInteger(key, -2147483648, 2147483647); },
    getLong: function(key) { return __readoriCacheInteger(key, -9223372036854775808, 9223372036854775807); },
    getDouble: function(key) { return __readoriCacheDouble(key); },
    getFloat: function(key) {
        var value = __readoriCacheDouble(key);
        return value === null ? null : Math.fround(value);
    },
    // Legado CacheManager.getByteArray -> ACache binary tier (signed Java bytes).
    getByteArray: function(key) {
        var hex = __readoriCacheGet('bytes', key);
        return hex === null || hex === undefined ? null : __readoriHexToByteArray(hex);
    },
    put: function(key, value, seconds) {
        if (__readoriCacheIsByteArray(value)) {
            var bytes = __readoriToByteArray(value);
            __readoriCachePut('bytes', key, bytes.map(function(b){ return ('0' + b.toString(16)).slice(-2); }).join(''), seconds);
        }
        __readoriCachePut('value', key, value, seconds);
    },
    getFile: function(key) { return __readoriCacheGet('file', key); },
    putFile: function(key, value, seconds) { __readoriCachePut('file', key, value, seconds); },
    getFromMemory: function(key) { return __readoriCacheGet('memory', key); },
    putMemory: function(key, value) { __readoriCachePut('memory', key, value, 0); },
    remove: function(key) { __readoriCacheDeleteAll(key); },
    delete: function(key) { __readoriCacheDeleteAll(key); },
    deleteMemory: function(key) { __readoriCacheDelete('memory', key); },
    clear: function() { __readoriCacheClear(); },
};
var book = {
    name: bookName || '',
    author: bookAuthor || '',
    bookUrl: bookUrl || '',
    tocUrl: bookTocUrl || '',
    kind: bookKind || '',
    intro: bookIntro || '',
    coverUrl: bookCoverUrl || '',
    lastChapter: bookLastChapter || '',
    lastChapterName: bookLastChapter || '',
    origin: bookOrigin || bookSourceUrl || '',
    originName: bookOriginName || sourceBookSourceName || '',
    type: Number(bookType || 0),
    durChapterIndex: Number(bookDurChapterIndex || 0),
    durChapterTitle: bookDurChapterTitle || '',
    totalChapterNum: Number(bookTotalChapterNum || 0),
    canUpdate: bookCanUpdate !== false,
    customIntro: bookCustomIntro || '',
    variable: bookVariable || '',
    imageStyle: bookImageStyle || '',
    order: Number(bookOrder || 0),
    getVariable: function(key) {
        if (typeof variables === 'undefined') return '';
        var name = String(key || '');
        return variables['book.' + name] || variables[name] || '';
    },
    putVariable: function(key, value) {
        if (typeof variables !== 'undefined') {
            var name = String(key || '');
            variables['book.' + name] = value;
            variables[name] = value;
        }
        return value;
    },
    putCustomVariable: function(value) {
        return this.putVariable('custom', value);
    },
    upCustomIntro: function() {
        this.customIntro = this.intro;
    },
    setReverseToc: function(value) {
        this.__setState('reverseToc', !!value);
    },
    getReverseToc: function() {
        var value = this.__getState('reverseToc', !!bookReverseToc);
        if (String(value).toLowerCase() === 'false' || String(value) === '0') return false;
        return !!value;
    },
    setUseReplaceRule: function(value) {
        this.__setState('useReplaceRule', !!value);
        this.__setState('readConfigInitialized', true);
    },
    getUseReplaceRule: function() {
        var value = this.__getState('useReplaceRule', bookUseReplaceRule);
        if (value === null || value === undefined || value === '') return true;
        if (String(value).toLowerCase() === 'false' || String(value) === '0') return false;
        return !!value;
    },
    setImageStyle: function(value) {
        this.imageStyle = String(value || '');
    },
    getImageStyle: function() {
        return String(this.imageStyle || '');
    },
    __getState: function(name, fallback) {
        if (typeof variables === 'undefined') return fallback;
        var key = '__book.' + String(name || '');
        return Object.prototype.hasOwnProperty.call(variables, key) ? variables[key] : fallback;
    },
    __setState: function(name, value) {
        if (typeof variables !== 'undefined') variables['__book.' + String(name || '')] = value;
        return value;
    },
};
var __readoriChapterVariableMap = {};
try {
    var __readoriChapterVariableRaw = String(
        (typeof variables !== 'undefined' && variables['__chapter.variable'] !== undefined)
            ? variables['__chapter.variable']
            : (chapterVariable || '')
    );
    if (__readoriChapterVariableRaw) {
        var __readoriParsedChapterVariable = JSON.parse(__readoriChapterVariableRaw);
        if (__readoriParsedChapterVariable && typeof __readoriParsedChapterVariable === 'object') {
            Object.keys(__readoriParsedChapterVariable).forEach(function(key) {
                var value = __readoriParsedChapterVariable[key];
                __readoriChapterVariableMap[String(key)] =
                    value === null || value === undefined ? '' : String(value);
            });
        }
    }
} catch (error) {}
if (typeof variables !== 'undefined') {
    Object.keys(variables).forEach(function(key) {
        if (String(key).indexOf('__chapter.var.') !== 0) return;
        __readoriChapterVariableMap[String(key).slice('__chapter.var.'.length)] = String(variables[key] || '');
    });
}
function __readoriSyncChapterVariable() {
    var raw = JSON.stringify(__readoriChapterVariableMap);
    if (typeof variables !== 'undefined') variables['__chapter.variable'] = raw;
    return raw;
}
function __readoriBookStringProperty(name, initialValue, stateName) {
    Object.defineProperty(book, name, {
        configurable: true,
        enumerable: true,
        get: function() {
            var value = book.__getState(stateName || name, initialValue);
            return value === null || value === undefined ? '' : String(value);
        },
        set: function(value) {
            book.__setState(stateName || name, value === null || value === undefined ? '' : String(value));
        },
    });
}
function __readoriBookNumberProperty(name, initialValue) {
    Object.defineProperty(book, name, {
        configurable: true,
        enumerable: true,
        get: function() {
            var value = Number(book.__getState(name, initialValue));
            return isFinite(value) ? value : 0;
        },
        set: function(value) {
            var number = Number(value);
            book.__setState(name, isFinite(number) ? number : 0);
        },
    });
}
function __readoriBookBoolProperty(name, initialValue) {
    Object.defineProperty(book, name, {
        configurable: true,
        enumerable: true,
        get: function() {
            var value = book.__getState(name, initialValue);
            if (String(value).toLowerCase() === 'false' || String(value) === '0') return false;
            return !!value;
        },
        set: function(value) {
            book.__setState(name, !!value);
        },
    });
}
[
    ['bookUrl', bookUrl || ''],
    ['tocUrl', bookTocUrl || ''],
    ['name', bookName || ''],
    ['author', bookAuthor || ''],
    ['intro', bookIntro || ''],
    ['kind', bookKind || ''],
    ['coverUrl', bookCoverUrl || ''],
    ['lastChapter', bookLastChapter || ''],
    ['lastChapterName', bookLastChapter || '', 'lastChapter'],
    ['origin', bookOrigin || bookSourceUrl || ''],
    ['originName', bookOriginName || sourceBookSourceName || ''],
    ['durChapterTitle', bookDurChapterTitle || ''],
    ['customIntro', bookCustomIntro || ''],
    ['variable', bookVariable || ''],
    ['imageStyle', bookImageStyle || ''],
].forEach(function(item) {
    __readoriBookStringProperty(item[0], item[1], item[2]);
});
[
    ['type', Number(bookType || 0)],
    ['durChapterIndex', Number(bookDurChapterIndex || 0)],
    ['totalChapterNum', Number(bookTotalChapterNum || 0)],
    ['order', Number(bookOrder || 0)],
].forEach(function(item) {
    __readoriBookNumberProperty(item[0], item[1]);
});
__readoriBookBoolProperty('canUpdate', bookCanUpdate !== false);
var __readoriBookReadConfig = {};
Object.defineProperty(__readoriBookReadConfig, 'useReplaceRule', {
    configurable: true,
    enumerable: true,
    get: function() {
        var value = book.__getState('useReplaceRule', bookUseReplaceRule);
        if (value === null || value === undefined || value === '') return null;
        if (String(value).toLowerCase() === 'false' || String(value) === '0') return false;
        return !!value;
    },
    set: function(value) {
        book.__setState('useReplaceRule', value === null || value === undefined ? null : !!value);
        book.__setState('readConfigInitialized', true);
    },
});
Object.defineProperty(book, 'readConfig', {
    configurable: true,
    enumerable: true,
    get: function() {
        var initialized = book.__getState(
            'readConfigInitialized',
            bookUseReplaceRule !== null && bookUseReplaceRule !== undefined
        );
        return initialized ? __readoriBookReadConfig : null;
    },
});
var chapter = {
    __active: !!chapterActive,
    title: chapterTitle || '',
    url: chapterUrl || '',
    tag: chapterTag || '',
    isVolume: !!chapterIsVolume,
    isVip: !!chapterIsVip,
    isPay: !!chapterIsPay,
    index: Number(chapterIndex || 0),
    chapterCount: Number(chapterCount || 0),
    variable: __readoriSyncChapterVariable(),
    getVariable: function(key) {
        var name = String(key || '');
        return Object.prototype.hasOwnProperty.call(__readoriChapterVariableMap, name)
            ? String(__readoriChapterVariableMap[name] || '')
            : '';
    },
    putVariable: function(key, value) {
        var name = String(key || '');
        var text = value === null || value === undefined ? '' : String(value);
        __readoriChapterVariableMap[name] = text;
        if (typeof variables !== 'undefined') variables['__chapter.var.' + name] = text;
        this.variable = __readoriSyncChapterVariable();
        return value;
    },
};
['title', 'url', 'tag'].forEach(function(name) {
    var initialValue = String(chapter[name] || '');
    Object.defineProperty(chapter, name, {
        configurable: true,
        enumerable: true,
        get: function() {
            var value = (typeof variables !== 'undefined') ? variables['__chapter.' + name] : undefined;
            return value === undefined ? initialValue : String(value || '');
        },
        set: function(value) {
            initialValue = value === null || value === undefined ? '' : String(value);
            if (typeof variables !== 'undefined') variables['__chapter.' + name] = initialValue;
        },
    });
});
['isVolume', 'isVip', 'isPay'].forEach(function(name) {
    var initialValue = !!chapter[name];
    Object.defineProperty(chapter, name, {
        configurable: true,
        enumerable: true,
        get: function() {
            var value = (typeof variables !== 'undefined') ? variables['__chapter.' + name] : undefined;
            if (value === undefined) return initialValue;
            if (value === null || String(value).trim() === '' || value === 'null') return false;
            return !/^(false|no|not|0)$/i.test(String(value).trim());
        },
        set: function(value) {
            initialValue = !!value;
            if (typeof variables !== 'undefined') variables['__chapter.' + name] = initialValue;
        },
    });
});
if (typeof org === 'undefined') {
    var org = { jsoup: { Jsoup: {} } };
} else {
    org.jsoup = org.jsoup || {};
    org.jsoup.Jsoup = org.jsoup.Jsoup || {};
}
function _readoriJsoupRows(html, selector) {
    var rows = [];
    try {
        rows = JSON.parse(java.selectElementsJSON(String(selector || ''), String(html || '')) || '[]') || [];
    } catch (error) {
        rows = [];
    }
    return rows;
}
function _readoriJsoupRowsByLegadoRule(html, selector) {
    // null = not a Legado element rule (plain CSS: the caller keeps its CSS selection)
    try {
        if (typeof __readoriSelectElementsByRuleJSON === 'function') {
            var rows = JSON.parse(__readoriSelectElementsByRuleJSON(String(selector || ''), String(html || '')) || 'null');
            if (Array.isArray(rows)) return rows;
        }
    } catch (error) {}
    return null;
}
function _readoriSelectorToken(token) {
    var text = String(token || '').trim();
    if (!text) return '';
    if (text.indexOf('class.') === 0) return '.' + text.slice(6).replace(/\s+/g, '.');
    return text;
}
function _readoriJsoupRowsForRule(html, selector) {
    var branches = String(selector || '').split('||').map(function(s){ return s.trim(); }).filter(function(s){ return !!s; });
    for (var b = 0; b < branches.length; b++) {
        var parts = branches[b].split('@').map(function(s){ return _readoriSelectorToken(s); }).filter(function(s){ return !!s; });
        if (!parts.length) continue;
        var fragments = [String(html || '')];
        var rows = [];
        for (var i = 0; i < parts.length; i++) {
            rows = [];
            for (var f = 0; f < fragments.length; f++) {
                rows = rows.concat(_readoriJsoupRows(fragments[f], parts[i]));
            }
            fragments = rows.map(function(row){ return row.outerHtml || row.html || row.text || ''; }).filter(function(s){ return !!s; });
            if (!rows.length) break;
        }
        if (rows.length) return rows;
    }
    return _readoriJsoupRows(String(html || ''), String(selector || ''));
}
function _readoriRemoveHtmlFromParent(parent, htmls) {
    if (!parent || !htmls || !htmls.length) return;
    for (var i = 0; i < htmls.length; i++) {
        var html = htmls[i];
        if (!html) continue;
        if (typeof parent._sourceHtml === 'string') {
            parent._sourceHtml = parent._sourceHtml.split(html).join('');
        }
        if (typeof parent._html === 'string') {
            parent._html = parent._html.split(html).join('');
        }
    }
}
function _readoriJsoupRootHtml(node) {
    var root = node;
    while (root && root._parent) root = root._parent;
    if (!root) return '';
    return String(root._sourceHtml || (typeof root.toString === 'function' ? root.toString() : '') || '');
}
function _readoriJsoupParents(element) {
    // Jsoup Element.parents(): the ancestors, nearest first. The shim has no DOM tree, so the ancestors are the elements of the
    // root markup whose outer HTML strictly contains this element's outer HTML (smallest = nearest).
    var own = String(element.toString() || '');
    var rootHtml = _readoriJsoupRootHtml(element);
    var rows = own && rootHtml ? _readoriJsoupRows(rootHtml, '*') : [];
    rows = rows.filter(function(row) {
        var outer = String(row.outerHtml || '');
        return outer.length > own.length && outer.indexOf(own) >= 0;
    });
    rows.sort(function(a, b) { return String(a.outerHtml || '').length - String(b.outerHtml || '').length; });
    return _readoriJsoupElements(rows, null);
}
function _readoriJsoupHasClass(row, name) {
    var wanted = String(name || '').trim().toLowerCase();
    if (!wanted) return false;
    return String(row['class'] || '').toLowerCase().split(/\s+/).indexOf(wanted) >= 0;
}
function _readoriJsoupExtrasEnabled() {
    return typeof __readoriJsoupExtras !== 'undefined' && !!__readoriJsoupExtras;
}
function _readoriJsoupElement(row, parent) {
    row = row || {};
    var elementHtml = row.outerHtml || row.html || row.text || '';
    var element = {
        _html: elementHtml,
        _innerHtml: row.html || '',
        _parent: parent || null,
        text: function(){ return row.text || ''; },
        ownText: function(){ return row.ownText || row.text || ''; },
        attr: function(name){
            name = String(name || '').toLowerCase();
            if (name === 'html' || name === 'innerhtml') return this.html();
            if (name === 'outerhtml' || name === 'all') return this.toString();
            return row[name] || '';
        },
        html: function(){ return this._innerHtml || row.html || ''; },
        outerHtml: function(){ return this.toString(); },
        select: function(selector) {
            return _readoriJsoupElements(_readoriJsoupRows(this.toString(), selector), this);
        },
        remove: function(){
            _readoriRemoveHtmlFromParent(this._parent, [this.toString()]);
            this._html = '';
            this._innerHtml = '';
            row.html = '';
            row.outerHtml = '';
            row.text = '';
            return this;
        },
        toString: function(){ return this._html || row.outerHtml || row.html || row.text || ''; }
    };
    if (_readoriJsoupExtrasEnabled()) {
        // R157 (js G1, 1.1.2): Rhino exposes the full Java Element API.
        element.hasClass = function(name) { return _readoriJsoupHasClass(row, name); };
        element.parents = function() { return _readoriJsoupParents(this); };
    }
    return element;
}
function _readoriJsoupElements(rows, parent) {
    rows = rows || [];
    var out = rows.map(function(row){ return _readoriJsoupElement(row, parent); });
    var rawRows = rows;
    __readoriHide(out, '_sourceHtml', out.map(function(e){ return e.toString(); }).join(''));
    __readoriHide(out, '_parent', parent || null);
    __readoriHide(out, 'toArray', function(){ return this; });
    __readoriHide(out, 'isEmpty', function(){ return this.length === 0; });
    __readoriHide(out, 'size', function(){ return this.length; });
    __readoriHide(out, 'get', function(index){ return this[Number(index) || 0]; });
    __readoriHide(out, 'eq', function(index){
        var item = this.get(index);
        return item ? _readoriJsoupElements([item], this) : _readoriJsoupElements([], this);
    });
    __readoriHide(out, 'text', function(){ return this.map(function(e){ return e.text(); }).join(' '); });
    __readoriHide(out, 'eachText', function(){
        var arr = this.map(function(e){ return e.text(); });
        __readoriHide(arr, 'toArray', function(){ return this; });
        return arr;
    });
    __readoriHide(out, 'attr', function(name){ return this.length ? this[0].attr(name) : ''; });
    __readoriHide(out, 'html', function(){ return this.map(function(e){ return e.html(); }).join(''); });
    __readoriHide(out, 'outerHtml', function(){ return this.toString(); });
    __readoriHide(out, 'select', function(selector) {
        return _readoriJsoupElements(_readoriJsoupRows(this.toString(), selector), this);
    });
    __readoriHide(out, 'remove', function(){
        var htmls = this.map(function(e){ return e.toString(); }).filter(function(s){ return !!s; });
        _readoriRemoveHtmlFromParent(this._parent, htmls);
        this._sourceHtml = '';
        this.length = 0;
        return this;
    });
    __readoriHide(out, 'toString', function(){
        if (typeof this._sourceHtml === 'string' && this._sourceHtml.length > 0) return this._sourceHtml;
        return Array.prototype.map.call(this, function(e){ return e.toString(); }).join('');
    });
    if (_readoriJsoupExtrasEnabled()) {
        // R157 (js G1, 1.1.2): Elements.first() / last() (null when empty), hasClass (any element), parents() (union of the
        // ancestors, nearest first) and java.util.List.contains (an element equals another with the same outer HTML).
        __readoriHide(out, 'first', function(){ return this.length ? this[0] : null; });
        __readoriHide(out, 'last', function(){ return this.length ? this[this.length - 1] : null; });
        __readoriHide(out, 'hasClass', function(name){
            for (var i = 0; i < rawRows.length; i++) { if (_readoriJsoupHasClass(rawRows[i], name)) return true; }
            return false;
        });
        __readoriHide(out, 'parents', function(){
            var seen = {}, rows = [];
            for (var i = 0; i < this.length; i++) {
                var ancestors = this[i].parents ? this[i].parents() : [];
                for (var j = 0; j < ancestors.length; j++) {
                    var key = String(ancestors[j].toString());
                    if (!seen[key]) { seen[key] = true; rows.push({ outerHtml: key }); }
                }
            }
            return _readoriJsoupElements(rows, null);
        });
        __readoriHide(out, 'contains', function(target){
            var wanted = target === null || target === undefined ? '' : String(target.toString());
            for (var i = 0; i < this.length; i++) { if (String(this[i].toString()) === wanted) return true; }
            return false;
        });
    }
    return out;
}
function _readoriJsoupDocument(html) {
    return {
        _sourceHtml: String(html || ''),
        html: function(){ return this._sourceHtml; },
        text: function(){
            var rows = _readoriJsoupRows(this._sourceHtml, 'body');
            if (!rows.length) rows = _readoriJsoupRows(this._sourceHtml, '*');
            return _readoriJsoupElements(rows, this).text();
        },
        select: function(selector) {
            return _readoriJsoupElements(_readoriJsoupRows(this._sourceHtml, selector), this);
        },
        remove: function(){
            this._sourceHtml = '';
            return this;
        },
        toString: function(){ return this._sourceHtml; }
    };
}
org.jsoup.Jsoup.parse = function(html) {
    return _readoriJsoupDocument(html);
};
// R157 REV: `org.jsoup.Jsoup.connect` exists in the App since before 1.1.1, so it is on both profiles.
org.jsoup.Jsoup.connect = __readoriJsoupConnection;
if (_readoriJsoupExtrasEnabled()) {
    // R157 (js G1, 1.1.2): Rhino resolves `org.jsoup.*` and `Packages.org.jsoup.*` to the same Java package tree.
    org.jsoup.Connection = Packages.org.jsoup.Connection;
    org.jsoup.nodes = org.jsoup.nodes || { Document: function Document() {}, Element: function Element() {}, Elements: function Elements() {} };
    Packages.org.jsoup.nodes = org.jsoup.nodes;
    Packages.org.jsoup.Jsoup.parse = org.jsoup.Jsoup.parse;
}
// R157 REV (1.1.2 list rules): the `result` a JS segment after the first segment of a list rule receives - an array of the current
// items with the helpers the App gives it (toString / first / last / text / attr / select; text() joins the items' text).
function __readoriListResult(items, raws, jsonMode) {
    // JSON mode (E1 final; the page content is JSON, so an empty list prints `[]`): the list's toString is `[` + the items' JSON joined by `,` + `]` and every parsed object's toString is its
    // raw JSON; HTML mode: the outerHTML items joined by a newline, plus the Elements methods below.
    var arr = __readoriListArray(Array.isArray(items) ? items.slice() : []);
    function asText(item) { return typeof item === 'string' ? item : (item !== null && item !== undefined && typeof item.toString === 'function' && item.toString !== Object.prototype.toString ? String(item) : JSON.stringify(item)); }
    {
        for (var i = 0; i < arr.length; i++) {
            if (arr[i] !== null && typeof arr[i] === 'object' && Array.isArray(raws) && typeof raws[i] === 'string') {
                (function(raw) { try { Object.defineProperty(arr[i], 'toString', { value: function() { return raw; }, writable: true, configurable: true, enumerable: false }); } catch (error) {} })(raws[i]);
            }
        }
    }
    function elementTexts(list) {
        return list.map(function(item) { return typeof item === 'string' ? org.jsoup.Jsoup.parse(item).text() : JSON.stringify(item); });
    }
    __readoriHide(arr, 'first', function() { return this.length ? this[0] : null; });
    __readoriHide(arr, 'last', function() { return this.length ? this[this.length - 1] : null; });
    __readoriHide(arr, 'toString', function() {
        if (jsonMode) return '[' + this.map(asText).join(',') + ']';
        return this.map(asText).join('\n');
    });
    __readoriHide(arr, 'text', function() { return elementTexts(this).join(' '); });
    __readoriHide(arr, 'eachText', function() { return __readoriListArray(elementTexts(this)); });
    __readoriHide(arr, 'html', function() {
        return this.map(function(item) {
            var rows = typeof item === 'string' ? _readoriJsoupRows(item, '*') : [];
            return rows.length ? String(rows[0].html || '') : asText(item);
        }).join('\n');
    });
    __readoriHide(arr, 'outerHtml', function() { return this.map(asText).join('\n'); });
    __readoriHide(arr, 'eq', function(index) {
        var n = Number(index);
        return __readoriListResult(n >= 0 && n < this.length ? [this[n]] : [], null, false);
    });
    __readoriHide(arr, 'attr', function(name) {
        for (var i = 0; i < this.length; i++) {
            var value = typeof this[i] === 'string' ? String(this[i]).attr(name) : '';
            if (value) return value;
        }
        return '';
    });
    __readoriHide(arr, 'hasClass', function(name) {
        var wanted = String(name || '').trim().toLowerCase();
        for (var i = 0; i < this.length; i++) {
            var rows = typeof this[i] === 'string' ? _readoriJsoupRows(this[i], '*') : [];
            if (rows.length && _readoriJsoupHasClass(rows[0], wanted)) return true;
        }
        return false;
    });
    // Elements.remove() detaches the nodes from their parent; the list itself is unchanged.
    __readoriHide(arr, 'remove', function() { return this; });
    __readoriHide(arr, 'select', function(selector) { return org.jsoup.Jsoup.parse(this.map(asText).join('')).select(selector); });
    return arr;
}
// What a list-rule JS tail returned (E1 final): undefined / null / "" is an empty list, undefined / null array elements are skipped,
// objects keep their JSON text; anything else is one item. The sentinel tells the validator "empty list"; the prefix tells it
// whether the JS returned an array (a list for the next segment) or a string (raw content for the next segment).
function __readoriListTailFinish(out) {
    var EMPTY = '__readoriEmptyList__';
    if (out === undefined || out === null) return EMPTY;
    if (Array.isArray(out)) {
        var kept = [];
        for (var i = 0; i < out.length; i++) {
            var value = out[i];
            if (value === undefined || value === null) continue;
            if (typeof value === 'string') kept.push(value);
            else if (typeof value === 'object' && !Array.isArray(value) && typeof value.toString === 'function' && value.toString !== Object.prototype.toString) kept.push(String(value));
            else if (typeof value === 'object') kept.push(JSON.stringify(value));
            else kept.push(String(value));
        }
        return kept.length ? '__readoriTail:arr:' + JSON.stringify(kept) : EMPTY;
    }
    var text = String(out);
    return text === '' ? EMPTY : '__readoriTail:str:' + text;
}
function __readoriListTailRun(code) {
    try { return (0, eval)(code); }
    catch (error) {
        if (error instanceof SyntaxError) return (new Function(code))();
        throw error;
    }
}
// R157 (js tadu isVip `result.select('i').hasClass('vip')`): in Legado `result` of a list-item rule is a Jsoup Element; the App
// (JSEngine.swift `String.prototype.select` / `text` / `attr` / `html` / `outerHtml` / `ownText`, 1.1.1 too) gives the HTML string
// the same methods. Non-enumerable so `for..in` over a string is unchanged.
(function() {
    function defineStringMethod(name, fn) {
        if (typeof String.prototype[name] === 'function') return;
        try { Object.defineProperty(String.prototype, name, { value: fn, writable: true, configurable: true, enumerable: false }); }
        catch (error) {}
    }
    defineStringMethod('select', function(selector) { return org.jsoup.Jsoup.parse(String(this)).select(selector); });
    defineStringMethod('text', function() { return org.jsoup.Jsoup.parse(String(this)).text(); });
    defineStringMethod('ownText', function() { return org.jsoup.Jsoup.parse(String(this)).text(); });
    defineStringMethod('toArray', function() { return [String(this)]; });
    defineStringMethod('isEmpty', function() { return String(this).length === 0; });
    defineStringMethod('html', function() { return String(this); });
    defineStringMethod('outerHtml', function() { return String(this); });
    defineStringMethod('attr', function(name) {
        var wanted = String(name || '').toLowerCase();
        var tag = String(this).match(/^\s*<[^>]*>/);
        if (!tag) return '';
        var found = tag[0].match(new RegExp('\\s' + wanted.replace(/[^a-z0-9_-]/g, '') + '\\s*=\\s*(?:"([^"]*)"|\'([^\']*)\'|([^\\s>]+))', 'i'));
        return found ? (found[1] || found[2] || found[3] || '') : '';
    });
})();
// Legado binds `book = ruleData as? BaseBook` and `chapter` only when set:
// searchUrl/exploreUrl/RSS and list rules see `book == null`; `chapter` is
// non-null only in TOC item fields and content.
if (typeof __readoriBookIsNull !== 'undefined' && __readoriBookIsNull) { book = null; }
if (typeof chapterActive !== 'undefined' && !chapterActive) { chapter = null; }
// R157 (js mingzw): Legado AnalyzeRule.evalJS binds `title = chapter?.title` (AnalyzeRule.kt:784); the App binds it in chapter
// contexts (JSEngine.swift:2395-2405). A configurable global property, so a script's own `let title` / `var title` still works.
try {
    if (typeof chapterActive !== 'undefined' && chapterActive && typeof globalThis.title === 'undefined') {
        globalThis.title = String(chapterTitle || '');
    }
} catch (error) {}
"""
    if _dbg.active():
        # R147 (H9): java.log(msg) also lands in the debug trace (kind="log"); batch validation keeps the plain stub.
        java_api_js = java_api_js.replace(
            "log: function(msg) { return msg; },",
            "log: function(msg) { __readoriDebugLogPush(msg); return msg; },",
            1,
        ) + _DEBUG_LOG_JS
    return java_api_js


DEBUG_LOG_LIMIT = 500
DEBUG_LOG_TEXT_LIMIT = 2000
# Appended to the java prelude while a debug trace is open (Node vm and QuickJS both run the prelude as plain JS).
_DEBUG_LOG_JS = r"""
var __readoriDebugLog = [];
function __readoriDebugLogPush(msg) {
    if (__readoriDebugLog.length >= %d) return;
    var text;
    try { text = String(msg); } catch (error) { text = '[unprintable]'; }
    __readoriDebugLog.push(text.length > %d ? text.slice(0, %d) : text);
}
""" % (DEBUG_LOG_LIMIT, DEBUG_LOG_TEXT_LIMIT, DEBUG_LOG_TEXT_LIMIT)


def emit_debug_log_messages(messages: Any) -> None:
    """Forward the messages a script passed to java.log to the active debug trace (no-op without one)."""
    if not _dbg.active() or not isinstance(messages, list):
        return
    for message in messages[:DEBUG_LOG_LIMIT]:
        if _dbg.active():
            _dbg.emit("log", value=str(message)[:DEBUG_LOG_TEXT_LIMIT])


NODE_JS_RUNNER = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const { URL, URLSearchParams } = require('node:url');

// R145b: source scripts run in a vm context whose objects/functions come from this (host) realm, so
// `constructor.constructor('return process')()` used to reach the host. Capture what the runner itself needs, then strip
// the host realm before any untrusted code runs. Main defences are outside: the Node permission model
// (no fs / child_process / worker / addons) and a minimal environment, both set by the Python launcher.
const __hostProcess = process;
const __hostConsole = console;
const __hostStdout = process.stdout;
const __debugNode = !!(process.env && process.env.READORI_DEBUG_NODE_JS);
function __readoriHardenHostRealm() {
  const blocked = function constructorBlocked() { throw new TypeError('blocked: host constructors are not available to source scripts'); };
  const prototypes = [Function.prototype];
  for (const make of [() => async function () {}, () => function* () {}, () => async function* () {}]) {
    try { prototypes.push(Object.getPrototypeOf(make())); } catch (error) {}
  }
  for (const proto of prototypes) {
    try { Object.defineProperty(proto, 'constructor', { value: blocked, writable: false, configurable: false }); } catch (error) {}
  }
  try {
    Object.defineProperty(Error, 'prepareStackTrace', { get() { return undefined; }, set() {}, configurable: false });
  } catch (error) {}
  // The permission model does not restrict the network, so remove every host-realm door to it (and to process).
  for (const name of [
    'process', 'require', 'module', 'exports', '__filename', '__dirname', 'console', 'fetch', 'Request', 'Response',
    'Headers', 'FormData', 'WebSocket', 'EventSource', 'XMLHttpRequest', 'Worker', 'MessageChannel', 'MessagePort',
    'BroadcastChannel', 'navigator', 'Navigator',
  ]) {
    try { delete globalThis[name]; } catch (error) {}
    try {
      if (Object.prototype.hasOwnProperty.call(globalThis, name)) {
        Object.defineProperty(globalThis, name, { value: undefined, writable: false, configurable: false });
      }
    } catch (error) {}
  }
}
__readoriHardenHostRealm();

// R143: App globals (btoa/atob/md5/sha256, UTF-8 semantics) and the native bridges behind the App's CryptoJS
// (hex in, hex out, failures come back as "!" + reason). Single DES runs as 3DES with K||K||K because OpenSSL 3's
// default provider has no single DES.
function __readoriAppNatives() {
  const HASHES = { MD5: 'md5', SHA1: 'sha1', SHA224: 'sha224', SHA256: 'sha256', SHA384: 'sha384', SHA512: 'sha512' };
  const hashKey = (name) => String(name).toUpperCase().replace(/-/g, '');
  const hexBytes = (value) => (typeof value === 'string' && /^(?:[0-9a-fA-F]{2})*$/.test(value)) ? Buffer.from(value, 'hex') : null;
  const textOf = (value) => (value === undefined || value === null) ? '' : String(value);
  return {
    btoa(value) { return Buffer.from(textOf(value), 'utf8').toString('base64'); },
    atob(value) {
      const raw = textOf(value);
      if (!/^(?:[A-Za-z0-9+\/]{4})*(?:[A-Za-z0-9+\/]{2}==|[A-Za-z0-9+\/]{3}=)?$/.test(raw)) return raw;
      try {
        return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(Buffer.from(raw, 'base64'));
      } catch (error) {
        return raw;
      }
    },
    md5(value) { return crypto.createHash('md5').update(Buffer.from(textOf(value), 'utf8')).digest('hex'); },
    sha256(value) { return crypto.createHash('sha256').update(Buffer.from(textOf(value), 'utf8')).digest('hex'); },
    __readoriCryptoJSCipher(operation, algorithm, mode, keyHex, ivHex, dataHex) {
      const key = hexBytes(keyHex);
      const ivRaw = hexBytes(ivHex);
      const data = hexBytes(dataHex);
      if (!key || !ivRaw || !data) return '!invalid hex';
      const alg = String(algorithm).toUpperCase();
      let blockSize;
      let keyBytes = key;
      let prefix;
      if (alg === 'AES') {
        if (![16, 24, 32].includes(key.length)) return '!Invalid key length';
        blockSize = 16;
        prefix = 'aes-' + (key.length * 8);
      } else if (alg === 'DES') {
        if (key.length !== 8) return '!Invalid key length';
        blockSize = 8;
        keyBytes = Buffer.concat([key, key, key]);
        prefix = 'des-ede3';
      } else if (alg === '3DES') {
        if (key.length !== 24) return '!Invalid key length';
        blockSize = 8;
        prefix = 'des-ede3';
      } else {
        return '!unsupported algorithm';
      }
      const modeName = String(mode).toUpperCase();
      let needsAligned = true;
      if (modeName === 'CBC' || modeName === 'ECB') {
        needsAligned = true;
      } else if (modeName === 'CFB' || modeName === 'OFB' || modeName === 'CTR') {
        needsAligned = false;
      } else {
        return '!unsupported mode';
      }
      if (alg !== 'AES' && modeName === 'CTR') return '!unsupported mode';
      if (needsAligned && data.length % blockSize !== 0) return '!data not block aligned';
      if (data.length === 0) return '';
      let iv = ivRaw.subarray(0, blockSize);
      if (iv.length < blockSize) iv = Buffer.concat([iv, Buffer.alloc(blockSize - iv.length)]);
      const cipherName = prefix + '-' + modeName.toLowerCase();
      try {
        const worker = operation === 'e'
          ? crypto.createCipheriv(cipherName, keyBytes, modeName === 'ECB' ? null : iv)
          : crypto.createDecipheriv(cipherName, keyBytes, modeName === 'ECB' ? null : iv);
        worker.setAutoPadding(false);
        return Buffer.concat([worker.update(data), worker.final()]).toString('hex');
      } catch (error) {
        return '!cipher init failed';
      }
    },
    __readoriCryptoJSDigest(algorithm, dataHex) {
      const data = hexBytes(dataHex);
      if (!data) return '!invalid hex';
      const name = HASHES[hashKey(algorithm)];
      if (!name) return '!unsupported algorithm';
      return crypto.createHash(name).update(data).digest('hex');
    },
    __readoriCryptoJSHmac(algorithm, keyHex, dataHex) {
      const key = hexBytes(keyHex);
      const data = hexBytes(dataHex);
      if (!key || !data) return '!invalid hex';
      const name = HASHES[hashKey(algorithm)];
      if (!name) return '!unsupported algorithm';
      return crypto.createHmac(name, key).update(data).digest('hex');
    },
    __readoriCryptoJSRandom(byteCount) {
      const count = Math.trunc(Number(byteCount));
      if (!Number.isFinite(count) || count < 0 || count > 1024) return '!invalid length';
      return crypto.randomBytes(count).toString('hex');
    },
    __readoriCryptoJSPBKDF2(hasher, passwordHex, saltHex, iterations, keyBytes) {
      const password = hexBytes(passwordHex);
      const salt = hexBytes(saltHex);
      if (!password || !salt) return '!invalid hex';
      const key = hashKey(hasher);
      if (!HASHES[key] || key === 'MD5') return '!unsupported hasher';
      const size = Math.trunc(Number(keyBytes));
      const rounds = Math.max(1, Math.min(Math.trunc(Number(iterations)), 2147483647));
      if (!Number.isFinite(size) || !Number.isFinite(rounds)) return '!invalid key size';
      if (size <= 0 || size > 1024) return '!invalid key size';
      try {
        return crypto.pbkdf2Sync(password, salt, rounds, size, HASHES[key]).toString('hex');
      } catch (error) {
        return '!pbkdf2 failed';
      }
    }
  };
}

{
  const payload = fs.readFileSync(0, 'utf8');
  let data;
  try {
    data = JSON.parse(payload || '{}');
  } catch (error) {
    if (__debugNode) {
      __hostConsole.error('JSON_PARSE_FAIL', error && error.message, payload.slice(0, 120));
    }
    __hostConsole.log('');
    __hostProcess.exit(0);
  }
  if (__debugNode) {
    __hostConsole.error('NODE_RUN_CODE', (data.code || '').slice(0, 120));
  }
  const inputText = data.inputText || '';
  let parsedInputResult;
  try {
    const parsed = JSON.parse(inputText);
    if (parsed && typeof parsed === 'object') parsedInputResult = parsed;
  } catch (error) {}
  const context = {
    result: parsedInputResult === undefined ? inputText : parsedInputResult,
    src: inputText,
    input: inputText,
    baseUrl: data.baseUrl || '',
    sourceUrl: data.sourceUrl || '',
    bookSourceUrl: data.bookSourceUrl || '',
    ruleUrl: data.ruleUrl || data.baseUrl || '',
    ruleRequestMethod: data.ruleRequestMethod || 'GET',
    ruleRequestBody: data.ruleRequestBody || '',
    cookie: data.cookie || '',
    previousResult: data.previousResult || '',
    bookName: data.bookName || '',
    bookAuthor: data.bookAuthor || '',
    bookIntro: data.bookIntro || '',
    bookKind: data.bookKind || '',
    bookCoverUrl: data.bookCoverUrl || '',
    bookLastChapter: data.bookLastChapter || '',
    bookUrl: data.bookUrl || '',
    bookTocUrl: data.bookTocUrl || '',
    bookOrigin: data.bookOrigin || '',
    bookOriginName: data.bookOriginName || '',
    bookType: Number(data.bookType || 0),
    bookDurChapterIndex: Number(data.bookDurChapterIndex || 0),
    bookDurChapterTitle: data.bookDurChapterTitle || '',
    bookTotalChapterNum: Number(data.bookTotalChapterNum || 0),
    bookCanUpdate: data.bookCanUpdate !== false,
    bookCustomIntro: data.bookCustomIntro || '',
    bookVariable: data.bookVariable || '',
    bookUseReplaceRule: data.bookUseReplaceRule === null || data.bookUseReplaceRule === undefined
      ? null
      : !!data.bookUseReplaceRule,
    bookReverseToc: !!data.bookReverseToc,
    bookImageStyle: data.bookImageStyle || '',
    bookOrder: Number(data.bookOrder || 0),
    chapterUrl: data.chapterUrl || '',
    chapterTitle: data.chapterTitle || '',
    chapterTag: data.chapterTag || '',
    chapterIsVolume: !!data.chapterIsVolume,
    chapterIsVip: !!data.chapterIsVip,
    chapterIsPay: !!data.chapterIsPay,
    chapterIndex: Number(data.chapterIndex || 0),
    chapterCount: Number(data.chapterCount || 0),
    chapterVariable: data.chapterVariable || '',
    chapterActive: !!data.chapterActive,
    __readoriBookIsNull: !!data.bookIsNull,
    sourceBookSourceName: data.sourceBookSourceName || '',
    sourceBookSourceComment: data.sourceBookSourceComment || '',
    sourceVariableComment: data.sourceVariableComment || '',
    sourceBookSourceType: data.sourceBookSourceType || 0,
    sourceLastUpdateTime: data.sourceLastUpdateTime || 0,
    sourceHeader: data.sourceHeader || '',
    sourceLoginUrl: data.sourceLoginUrl || '',
    sourceLoginUi: data.sourceLoginUi || '',
    sourceEnabledCookieJar: data.sourceEnabledCookieJar !== false,
    sourceBookSourceGroup: data.sourceBookSourceGroup || '',
    sourceExploreUrl: data.sourceExploreUrl || '',
    sourceSearchUrl: data.sourceSearchUrl || '',
    sourceJSLib: data.jsLib || '',
    sourceLoginHeader: data.sourceLoginHeader || '',
    sourceLoginInfo: data.sourceLoginInfo || '',
    sourceConcurrentRate: data.sourceConcurrentRate || '',
    headers: data.headers || {},
    variables: data.variables || {},
    Buffer,
    TextDecoder,
    URL,
    URLSearchParams,
    __readoriCrypto: crypto,
    __readoriZlib: zlib,
    __readoriPercentEncodeMap: data.percentEncodeMap || {},
    __readoriGunzipBytesBase64(value) {
      try {
        return zlib.gunzipSync(Buffer.from(String(value || ''), 'base64')).toString('base64');
      } catch (error) {
        return String(value || '');
      }
    },
    __readoriInflateBytesBase64(value) {
      try {
        return zlib.inflateSync(Buffer.from(String(value || ''), 'base64')).toString('base64');
      } catch (error) {
        try {
          return zlib.inflateRawSync(Buffer.from(String(value || ''), 'base64')).toString('base64');
        } catch (inner) {
          return String(value || '');
        }
      }
    },
    console: { log() {}, error() {}, warn() {} },
    setTimeout() {},
    clearTimeout() {},
    __readoriSelectElementsJSON(selector, html) {
      selector = String(selector || '').trim();
      html = String(html || '');
      if (!selector) selector = '*';
      function decodeEntities(text) {
        return String(text || '')
          .replace(/&nbsp;/g, ' ')
          .replace(/&lt;/g, '<')
          .replace(/&gt;/g, '>')
          .replace(/&amp;/g, '&')
          .replace(/&quot;/g, '"')
          .replace(/&#39;/g, "'");
      }
      function stripTags(text) {
        return decodeEntities(String(text || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim());
      }
      function attrsOf(rawAttrs) {
        const attrs = {};
        String(rawAttrs || '').replace(/([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g, (_, key, a, b, c) => {
          attrs[String(key || '').toLowerCase()] = decodeEntities(a || b || c || '');
          return '';
        });
        return attrs;
      }
      function matches(row, simpleSelector) {
        const sel = String(simpleSelector || '').trim();
        if (!sel || sel === '*') return true;
        if (sel[0] === '#') return row.id === sel.slice(1);
        if (sel[0] === '.') {
          const needle = sel.slice(1);
          return String(row.class || '').split(/\s+/).includes(needle);
        }
        const classMatch = sel.match(/^([A-Za-z][\w:-]*)\.([\w-]+)$/);
        if (classMatch) {
          return row.tag === classMatch[1].toLowerCase() && String(row.class || '').split(/\s+/).includes(classMatch[2]);
        }
        const attrMatch = sel.match(/^([A-Za-z][\w:-]*)?\[([A-Za-z_:][-A-Za-z0-9_:.]*)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]+)))?\]$/);
        if (attrMatch) {
          const tag = attrMatch[1] ? attrMatch[1].toLowerCase() : '';
          const attr = String(attrMatch[2] || '').toLowerCase();
          const expected = attrMatch[3] || attrMatch[4] || attrMatch[5];
          if (tag && row.tag !== tag) return false;
          if (!(attr in row)) return false;
          if (expected === undefined) return true;
          return String(row[attr] || '') === String(expected || '').replace(/^['"]|['"]$/g, '');
        }
        return row.tag === sel.toLowerCase();
      }
      const all = [];
      function collect(fragment) {
        const paired = /<([A-Za-z][\w:-]*)([^>]*)>([\s\S]*?)<\/\1>/g;
        let match;
        while ((match = paired.exec(fragment))) {
          const tag = String(match[1] || '').toLowerCase();
          const attrs = attrsOf(match[2] || '');
          const inner = match[3] || '';
          const outer = match[0] || '';
          all.push(Object.assign({
            tag,
            outerHtml: outer,
            html: inner,
            text: stripTags(inner),
            ownText: stripTags(inner.replace(/<([A-Za-z][\w:-]*)([^>]*)>[\s\S]*?<\/\1>/g, '')),
          }, attrs));
          if (/<[A-Za-z][\w:-]*[\s>]/.test(inner)) collect(inner);
        }
        const voidTags = new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
        const voidRe = /<([A-Za-z][\w:-]*)([^>]*)\/?>/g;
        while ((match = voidRe.exec(fragment))) {
          const tag = String(match[1] || '').toLowerCase();
          if (!voidTags.has(tag)) continue;
          const attrs = attrsOf(match[2] || '');
          const outer = match[0] || '';
          all.push(Object.assign({
            tag,
            outerHtml: outer,
            html: '',
            text: attrs.alt || attrs.value || '',
            ownText: attrs.alt || attrs.value || '',
          }, attrs));
        }
      }
      collect(html);
      if (selector.toLowerCase() === 'body' && !all.some(row => row.tag === 'body')) {
        return JSON.stringify([{ tag: 'body', outerHtml: html, html, text: stripTags(html), ownText: stripTags(html) }]);
      }
      const selectors = selector.split(',').map(s => s.trim()).filter(Boolean);
      const out = [];
      for (const sel of selectors) {
        for (const row of all) {
          if (matches(row, sel) && !out.includes(row)) out.push(row);
        }
      }
      return JSON.stringify(out);
    },
    __readoriUnescapeHtml4(text) {
      // commons-text unescapeHtml4 (Legado AnalyzeRule.getString): only entities that end in `;`.
      const named = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0', copy: '\u00a9', reg: '\u00ae', hellip: '\u2026', mdash: '\u2014', ndash: '\u2013', ldquo: '\u201c', rdquo: '\u201d', lsquo: '\u2018', rsquo: '\u2019' };
      return String(text || '').replace(/&(?:#(\d+)|#[xX]([0-9a-fA-F]+)|([A-Za-z][A-Za-z0-9]*));/g, (m, dec, hex, name) => {
        if (dec !== undefined) return String.fromCodePoint(parseInt(dec, 10));
        if (hex !== undefined) return String.fromCodePoint(parseInt(hex, 16));
        return Object.prototype.hasOwnProperty.call(named, name) ? named[name] : m;
      });
    },
    __readoriEvaluateString(rule, html, unescape) {
      // R157 REV: AnalyzeRule.getString(ruleStr, unescape) - the QuickJS native defaults to unescape = true as well.
      const value = context.__readoriEvaluateStringRaw(rule, html);
      return unescape === false ? value : context.__readoriUnescapeHtml4(value);
    },
    __readoriEvaluateStringRaw(rule, html) {
      rule = String(rule || '').trim();
      html = String(html || '');
      if (!rule) return '';
      function stringify(value) {
        if (value === undefined || value === null) return '';
        if (Array.isArray(value) || (value && typeof value === 'object')) return JSON.stringify(value);
        return String(value);
      }
      function readPath(root, path) {
        let text = String(path || '').trim();
        if (!text) return '';
        if (text.toLowerCase().startsWith('@json:')) text = text.slice(6).trim();
        if (text.startsWith('$')) text = text.slice(1);
        if (text.startsWith('.')) text = text.slice(1);
        if (!text) return stringify(root);
        const parts = text.split('.').filter(Boolean);
        let cur = root;
        for (const rawPart of parts) {
          const match = rawPart.match(/^([^\[]+)((?:\[-?\d+\])*)$/);
          if (!match) return '';
          const key = match[1];
          if (cur === undefined || cur === null) return '';
          cur = cur[key];
          const indexes = match[2] || '';
          const re = /\[(-?\d+)\]/g;
          let idxMatch;
          while ((idxMatch = re.exec(indexes))) {
            if (!Array.isArray(cur)) return '';
            let idx = parseInt(idxMatch[1], 10);
            if (idx < 0) idx = cur.length + idx;
            cur = cur[idx];
          }
        }
        return stringify(cur);
      }
      function readHtml(ruleText) {
        const branches = String(ruleText || '').split('||').map(s => s.trim()).filter(Boolean);
        for (const branch of branches) {
          const parts = branch.split('@').map(s => s.trim());
          let selector = parts[0] || '';
          let attr = (parts[1] || 'text').toLowerCase();
          let index = 0;
          const indexMatch = selector.match(/^(.*)\.(-?\d+)$/);
          if (indexMatch) {
            selector = indexMatch[1] || selector;
            index = parseInt(indexMatch[2], 10) || 0;
          }
          if (selector.toLowerCase().startsWith('class.')) selector = '.' + selector.slice(6);
          if (/\s+/.test(selector)) selector = selector.split(/\s+/).filter(Boolean).pop() || selector;
          if (!selector) selector = 'body';
          let rows = [];
          try { rows = JSON.parse(context.__readoriSelectElementsJSON(selector, html) || '[]') || []; } catch (error) { rows = []; }
          if (index < 0) index = rows.length + index;
          const row = rows[index];
          if (!row) continue;
          let value = '';
          if (attr === 'html' || attr === 'innerhtml') value = row.html || row.outerHtml || '';
          else if (attr === 'outerhtml' || attr === 'all') value = row.outerHtml || '';
          else if (attr === 'text' || attr === 'textnodes') value = row.text || '';
          else value = row[attr] || '';
          if (value) return String(value);
        }
        return '';
      }
      if (/<[A-Za-z][\w:-]*(?:\s|>)/.test(html) && !rule.startsWith('$') && !rule.toLowerCase().startsWith('@json:')) {
        const htmlValue = readHtml(rule);
        if (htmlValue) return htmlValue;
      }
      try {
        const obj = JSON.parse(html);
        if (rule.startsWith('$') || rule.toLowerCase().startsWith('@json:') || /^[A-Za-z_][\w]*(?:\.|\[|$)/.test(rule)) {
          return readPath(obj, rule);
        }
      } catch (error) {}
      return '';
    },
    __readoriEvaluateWholeJson(rule, html) {
      return context.__readoriEvaluateList(rule, html, true);
    },
    __readoriEvaluateList(rule, html, whole) {
      rule = String(rule || '').trim();
      html = String(html || '');
      if (!rule) return whole ? '' : '[]';
      function stringify(value) {
        if (value === undefined || value === null) return '';
        if (Array.isArray(value) || (value && typeof value === 'object')) return JSON.stringify(value);
        return String(value);
      }
      function collectRecursive(root, key, out) {
        if (root === undefined || root === null) return;
        if (Array.isArray(root)) {
          for (const item of root) collectRecursive(item, key, out);
          return;
        }
        if (typeof root !== 'object') return;
        for (const [name, value] of Object.entries(root)) {
          if (name === key) out.push(value);
          collectRecursive(value, key, out);
        }
      }
      function readPathAll(root, path) {
        let text = String(path || '').trim();
        if (text.toLowerCase().startsWith('@json:')) text = text.slice(6).trim();
        const recursive = text.match(/^\$\.\.([A-Za-z_$][\w$-]*)$/);
        if (recursive) {
          const out = [];
          collectRecursive(root, recursive[1], out);
          return out;
        }
        if (text.startsWith('$')) text = text.slice(1);
        if (text.startsWith('.')) text = text.slice(1);
        text = text.replace(/\[\*\]([A-Za-z_$][\w$-]*)/g, '[*].$1');
        if (!text) return [root];
        const tokens = [];
        for (const part of text.split('.').filter(Boolean)) {
          const key = part.replace(/\[.*$/, '');
          if (key) tokens.push(key);
          const re = /\[(-?\d+|\*)\]/g;
          let match;
          while ((match = re.exec(part))) tokens.push(match[1]);
        }
        let current = [root];
        for (const token of tokens) {
          const next = [];
          for (const item of current) {
            if (item === undefined || item === null) continue;
            if (token === '*') {
              if (Array.isArray(item)) next.push(...item);
              continue;
            }
            if (/^-?\d+$/.test(token)) {
              if (!Array.isArray(item)) continue;
              let idx = parseInt(token, 10);
              if (idx < 0) idx = item.length + idx;
              if (idx >= 0 && idx < item.length) next.push(item[idx]);
              continue;
            }
            if (Array.isArray(item)) {
              for (const child of item) {
                if (child && typeof child === 'object' && Object.prototype.hasOwnProperty.call(child, token)) next.push(child[token]);
              }
            } else if (typeof item === 'object' && Object.prototype.hasOwnProperty.call(item, token)) {
              next.push(item[token]);
            }
          }
          current = next;
        }
        return current;
      }
      function readHtmlList(ruleText) {
        const branches = String(ruleText || '').split('||').map(s => s.trim()).filter(Boolean);
        for (const branch of branches) {
          const parts = branch.split('@').map(s => s.trim());
          let selector = parts[0] || '';
          let attr = (parts[1] || 'text').toLowerCase();
          if (selector.toLowerCase().startsWith('class.')) selector = '.' + selector.slice(6);
          if (/\s+/.test(selector)) selector = selector.split(/\s+/).filter(Boolean).pop() || selector;
          if (!selector) selector = 'body';
          let rows = [];
          try { rows = JSON.parse(context.__readoriSelectElementsJSON(selector, html) || '[]') || []; } catch (error) { rows = []; }
          const values = [];
          for (const row of rows) {
            let value = '';
            if (attr === 'html' || attr === 'innerhtml') value = row.html || '';
            else if (attr === 'outerhtml' || attr === 'all') value = row.outerHtml || '';
            else if (attr === 'text' || attr === 'textnodes') value = row.text || '';
            else value = row[attr] || '';
            if (/^(href|src|data-src|data-href|action)$/.test(attr) && value) {
              try { value = new URL(value, context.baseUrl || context.bookSourceUrl || 'https://example.test/').toString(); } catch (error) {}
            }
            if (value) values.push(String(value));
          }
          if (values.length) return values;
        }
        return [];
      }
      if (whole) {
        // R158 (G6): the whole JSONPath value as JSON text ('' when nothing matched); an indefinite path answers the array of all hits.
        try {
          const wholeObj = JSON.parse(html);
          const hits = readPathAll(wholeObj, rule);
          if (!hits.length) return '';
          const indefinite = /\*|\.\.|\[\s*\?|\[[^\]]*,|\[\s*-?\d*\s*:/.test(rule);
          return JSON.stringify(indefinite ? hits : hits[0]) || '';
        } catch (error) { return ''; }
      }
      let values = [];
      try {
        const obj = JSON.parse(html);
        if (rule.startsWith('$') || rule.toLowerCase().startsWith('@json:') || /^[A-Za-z_][\w]*(?:\.|\[|$)/.test(rule)) {
          values = readPathAll(obj, rule).map(stringify).filter(Boolean);
        }
      } catch (error) {}
      if (!values.length && /<[A-Za-z][\w:-]*(?:\s|>)/.test(html) && !rule.startsWith('$') && !rule.toLowerCase().startsWith('@json:')) {
        values = readHtmlList(rule);
      }
      return JSON.stringify(values);
    },
  };
  for (const [key, value] of Object.entries(data.extraVars || {})) {
    context[key] = value;
  }
  Object.assign(context, __readoriAppNatives());
  // R143 (S1 = A): blocked interactions are reported back to Python in the result JSON.
  const suppressedInteractions = [];
  context.__readoriAllowsUserInteraction = data.allowsUserInteraction !== false;
  context.__readoriGetElementReturnsList = data.getElementReturnsList === true;
  context.__readoriJsoupExtras = data.jsoupExtras === true;
  context.__readoriListNonEnumerable = data.listNonEnumerable === true;
  context.__readoriGetStringUnescapeFlag = data.getStringUnescapeFlag === true;
  context.__readoriGetElementsJsonObjects = data.getElementsJsonObjects === true;
  Object.assign(context, data.r158Flags || {});
  context.__readoriRecordSuppressedInteraction = function(kind) {
    const name = String(kind);
    if (!suppressedInteractions.includes(name)) suppressedInteractions.push(name);
  };
  const initialResult = context.result;
  vm.createContext(context, {
    codeGeneration: { strings: true, wasm: false },
  });
  try {
    vm.runInContext(`
      (function(){
        var __readoriJSONParse = JSON.parse;
        JSON.parse = function(value, reviver) {
          if (value && typeof value === 'object') return value;
          return __readoriJSONParse.call(JSON, value, reviver);
        };
      })();
    `, context, { timeout: 700 });
  } catch (error) {}
  // R157 (js G3 goway): the replay loop re-runs the script after each materialized request; a moving clock changes every
  // time-stamped / HMAC-signed request URL, so the fixtures of the previous pass are never hit again. Freeze Date for the
  // whole replay (Legado runs the script once, live, so there is a single clock reading there).
  if (data.frozenNowMs) {
    try {
      vm.runInContext(`(function(t){var R=Date;function D(){var a=[].slice.call(arguments);if(!(this instanceof D))return new R(t).toString();if(a.length===0)return new R(t);return new (Function.prototype.bind.apply(R,[null].concat(a)))();}D.prototype=R.prototype;D.now=function(){return t;};D.UTC=R.UTC;D.parse=R.parse;Date=D;})(${Number(data.frozenNowMs)});`, context, { timeout: 700 });
    } catch (error) {}
  }
  try {
    if (context.result && typeof context.result === 'object' && inputText) {
      Object.defineProperty(context.result, 'toString', {
        value: function() { return inputText; },
        configurable: true
      });
      const stringMethods = [
        'search', 'match', 'replace', 'indexOf', 'lastIndexOf',
        'includes', 'startsWith', 'endsWith', 'substring', 'substr',
        'slice', 'split', 'charAt', 'charCodeAt', 'trim',
        'toLowerCase', 'toUpperCase'
      ];
      for (const name of stringMethods) {
        if (typeof context.result[name] !== 'undefined' || typeof String.prototype[name] !== 'function') continue;
        Object.defineProperty(context.result, name, {
          value: function(...args) { return String.prototype[name].apply(inputText, args); },
          configurable: true
        });
      }
    }
  } catch (error) {}
  try {
    vm.runInContext(data.javaApi || '', context, { timeout: 700, filename: 'readori-api.js' });
  } catch (error) {}
  // R143: the App installs CryptoJS right after the java prelude and before jsLib (1.1.1: the fake global;
  // 1.1.2: the crypto-js 4.1 builtin only when the text references it and does not define it itself).
  const appJs = data.appJs || {};
  const appJsSources = data.appJsSources || {};
  function __readoriInstallAppJs(source, filename) {
    if (!source) return;
    try {
      vm.runInContext(source, context, { timeout: 700, filename });
    } catch (error) {}
  }
  if (appJs.legacyCryptoJS) __readoriInstallAppJs(appJsSources.legacy, 'readori-app-cryptojs-111.js');
  if (appJs.cryptoJSBeforeJsLib) __readoriInstallAppJs(appJsSources.builtin, 'readori-app-cryptojs-112.js');
  // Source scripts see Readori's Buffer shim semantics: Buffer.from/concat called
  // directly from source (or jsLib) code return a signed Java byte array whose
  // toString() decodes UTF-8. The polyfill itself keeps the native Buffer.
  try {
    const NativeBuffer = Buffer;
    const calledFromSource = function() {
      const frames = String(new Error().stack || '').split('\n');
      // [0] "Error", [1] calledFromSource, [2] SourceBuffer.from/concat, [3] caller
      return /readori-(?:source|jslib)\.js/.test(frames[3] || '');
    };
    const toSigned = function(buffer) {
      return typeof context.__readoriByteArrayWithUtf8String === 'function'
        ? context.__readoriByteArrayWithUtf8String(Array.from(buffer))
        : Array.from(buffer, function(b) { return b > 127 ? b - 256 : b; });
    };
    const SourceBuffer = function(a, b, c) { return NativeBuffer(a, b, c); };
    Object.setPrototypeOf(SourceBuffer, NativeBuffer);
    SourceBuffer.prototype = NativeBuffer.prototype;
    SourceBuffer.from = function(value, encodingOrOffset, length) {
      const source = calledFromSource();
      let input = value;
      if (source && input && typeof input !== 'string' && !NativeBuffer.isBuffer(input) && typeof input.length === 'number') {
        input = Array.from(input, function(v) { return Number(v) & 255; });
      }
      const out = NativeBuffer.from(input, encodingOrOffset, length);
      return source ? toSigned(out) : out;
    };
    SourceBuffer.concat = function(list, totalLength) {
      const source = calledFromSource();
      const parts = Array.from(list || [], function(item) {
        return NativeBuffer.isBuffer(item) ? item : NativeBuffer.from(Array.from(item || [], function(v) { return Number(v) & 255; }));
      });
      const out = NativeBuffer.concat(parts, totalLength);
      return source ? toSigned(out) : out;
    };
    context.Buffer = SourceBuffer;
  } catch (error) {}
  function __readoriWrapJsoupResultArray(value) {
    if (!Array.isArray(value) || typeof context._readoriJsoupElements !== 'function' || typeof context._readoriJsoupRows !== 'function') {
      return value;
    }
    const rows = [];
    for (const item of value) {
      if (item && typeof item.attr === 'function') {
        rows.push({
          tag: '',
          outerHtml: String(item),
          html: typeof item.html === 'function' ? String(item.html()) : String(item),
          text: typeof item.text === 'function' ? String(item.text()) : String(item).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim(),
          ownText: ''
        });
        continue;
      }
      const html = String(item === undefined || item === null ? '' : item);
      let parsed = [];
      try { parsed = context._readoriJsoupRows(html, '*') || []; } catch (error) { parsed = []; }
      if (parsed.length) rows.push(parsed[0]);
      else rows.push({ tag: '', outerHtml: html, html, text: html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim(), ownText: '' });
    }
    return context._readoriJsoupElements(rows, null);
  }
  try {
    if (
      Object.prototype.hasOwnProperty.call(data.extraVars || {}, 'result') &&
      /result\s*\.\s*toArray\s*\(|\.attr\s*\(|\.text\s*\(/.test(String(data.code || ''))
    ) {
      context.result = __readoriWrapJsoupResultArray(context.result);
    }
  } catch (error) {}
  try {
    if (data.jsLib) {
      vm.runInContext(data.jsLib, context, { timeout: 700, filename: 'readori-jslib.js' });
    }
  } catch (error) {}
  if (appJs.cryptoJSBeforeCode) __readoriInstallAppJs(appJsSources.builtin, 'readori-app-cryptojs-112.js');
  let value;
  let scriptError = '';
  let scriptThrew = false;
  function __readoriErrorText(error) {
    let text = 'unknown';
    try { text = String(error); } catch (ignored) {}
    // vm timeouts are engine limits, not Legado ScriptExceptions.
    return /Script execution timed out/i.test(text) ? '' : text;
  }
  function __readoriIsTopLevelReturn(text) {
    return /illegal return statement|return statements are only valid inside functions|return not in a function/i.test(text || '');
  }
  try {
    value = vm.runInContext(data.code || '', context, { timeout: 1200, filename: 'readori-source.js' });
  } catch (error) {
    if (__debugNode) {
      __hostConsole.error('NODE_DIRECT_FAIL', error && error.message);
    }
    const directText = __readoriErrorText(error);
    if (directText && !__readoriIsTopLevelReturn(directText)) scriptError = directText;
    // R158 (B): the App keeps the `fallback != input` guard only for a script that THREW (JSEngine.swift `hadException` branches).
    scriptThrew = !__readoriIsTopLevelReturn(directText);
    try {
      if (/has already been declared/i.test(directText || '')) {
        // R158 (D1, gongzicp): jsLib `let d;` + rule script `var d` - Rhino and the App (function-scoped direct eval) accept it.
        scriptError = '';
        scriptThrew = false;
        value = vm.runInContext('(function(){ return eval(' + JSON.stringify(data.code || '') + '); })()', context, { timeout: 1200, filename: 'readori-source.js' });
      } else {
        value = vm.runInContext(`(function(){ ${data.code || ''} })()`, context, { timeout: 1200, filename: 'readori-source.js' });
      }
    } catch (inner) {
      if (__debugNode) {
        __hostConsole.error('NODE_WRAP_FAIL', inner && inner.message);
      }
      scriptThrew = true;
      if (!scriptError && directText) scriptError = __readoriErrorText(inner);
      value = context.result !== initialResult ? context.result : '';
    }
  }
  if (value === '' && !scriptThrew && data.tailControlFlow === true && context.__readoriEmptyCompletionKeepsEmpty !== true) {
    // R158d: 1.1.1 App - an empty completion value falls back to the global `result` like an undefined one (JSEngine.evaluate `result.isEmpty`).
    value = context.result;
  }
  if (value === undefined || value === null || String(value) === 'undefined') {
    // R158 (B): an unchanged `result` is the script value only where the ES5 completion value explains it (a control-flow tail,
    // `data.tailControlFlow`, computed by `_js_tail_is_control_flow`); the guarded fallback of a script that threw stays.
    value = (context.result !== initialResult || (!scriptThrew && data.tailControlFlow === true)) ? context.result : '';
  }
  let text = '';
  try {
    if (Object.prototype.toString.call(value) === '[object String]') {
      text = String(value);
    } else if (Array.isArray(value) || (value && typeof value === 'object')) {
      text = JSON.stringify(value);
    } else {
      text = value === undefined || value === null ? '' : String(value);
    }
  } catch (error) {
    text = '';
  }
  try {
    __hostStdout.write(JSON.stringify({
      value: text,
      scriptError: scriptError,
      variables: context.variables || {},
      requests: Array.isArray(context.__readoriRequestLog) ? context.__readoriRequestLog : [],
      interactions: suppressedInteractions,
      logs: Array.isArray(context.__readoriDebugLog) ? context.__readoriDebugLog : []
    }));
  } catch (error) {
    __hostStdout.write(JSON.stringify({ value: '', variables: {} }));
  }
}
"""


def app_cryptojs_sources(plan: dict[str, bool]) -> dict[str, str]:
    """The App JS texts a setup plan needs (kept out of the Node payload when unused: 22 KB)."""
    sources: dict[str, str] = {}
    if plan.get("legacyCryptoJS"):
        sources["legacy"] = app_profiles.APP_111_CRYPTOJS_POLYFILL
    if plan.get("cryptoJSBeforeJsLib") or plan.get("cryptoJSBeforeCode"):
        sources["builtin"] = app_profiles.APP_112_CRYPTOJS_BUILTIN
    return sources


def _js_text(value: Any) -> str:
    return "" if value is None else str(value)


def _r158_js_flags() -> dict[str, bool]:
    """R158 profile flags the JS prelude reads (QuickJS: ctx.set; node: payload `r158Flags` copied onto the context)."""
    profile = app_profiles.current_app_profile()
    return {
        "__readoriHutoolBase64": bool(profile.js_hutool_base64_shim),
        "__readoriStrResponseShim": bool(profile.js_str_response_shim),
        "__readoriJsonResultGet": bool(profile.js_json_object_result_get),
        "__readoriSetContentKeepsResult": bool(profile.set_content_keeps_result_binding),
        "__readoriRsaBareNoPadding": bool(profile.rsa_bare_public_decrypt_no_padding),
        "__readoriGetElementJsonWhole": bool(profile.js_get_element_json_whole_value),
        "__readoriEmptyCompletionKeepsEmpty": bool(profile.js_empty_completion_keeps_empty),
    }


def install_quickjs_app_globals(ctx: Any, runtime: RuleRuntime) -> None:
    """App globals (btoa/atob/md5/sha256, UTF-8 semantics), the CryptoJS native bridges and the interaction gate.

    python-quickjs refuses callbacks into Python while a time limit is set, and make_quickjs_context always sets one,
    so everything here is plain JS (QUICKJS_APP_NATIVES_JS). Blocked interactions are collected on the JS side and
    moved into the runtime's interaction state by drain_quickjs_interactions. Runs before the java prelude.
    """
    try:
        ctx.eval(app_profiles.QUICKJS_APP_NATIVES_JS)
    except Exception:
        pass
    try:
        ctx.set("__readoriAllowsUserInteraction", bool(getattr(runtime, "allows_user_interaction", True)))
        ctx.set("__readoriGetElementReturnsList", bool(app_profiles.current_app_profile().get_element_returns_list))
        # R157 (1.1.2): Jsoup shim extras, non-enumerable list helpers, `java.getElements` JSON objects.
        ctx.set("__readoriJsoupExtras", bool(app_profiles.current_app_profile().jsoup_shim_extras))
        ctx.set("__readoriListNonEnumerable", bool(app_profiles.current_app_profile().js_list_helpers_non_enumerable))
        ctx.set("__readoriGetStringUnescapeFlag", bool(app_profiles.current_app_profile().get_string_unescape_flag))
        ctx.set("__readoriGetElementsJsonObjects", bool(app_profiles.current_app_profile().js_get_elements_json_objects))
        # R158 (1.1.2): hutool Base64, StrResponse constructor, Map.get on a JSON-object `result`, java.setContent keeps `result`.
        for _flag_name, _flag_value in _r158_js_flags().items():
            ctx.set(_flag_name, _flag_value)
    except Exception:
        pass


def read_quickjs_debug_log(ctx: Any) -> list[str]:
    """The java.log messages collected on the JS side (empty unless the prelude was built under a debug trace)."""
    try:
        raw = ctx.eval("JSON.stringify(typeof __readoriDebugLog === 'undefined' ? [] : __readoriDebugLog)")
        messages = json.loads(str(raw or "[]"))
    except Exception as _swallowed:
        note_debug_error("quickjs debug log", _swallowed)
        return []
    return [str(message) for message in messages] if isinstance(messages, list) else []


def drain_quickjs_interactions(ctx: Any, runtime: RuleRuntime) -> None:
    """Move the interaction kinds the JS side logged (java.startBrowser* etc.) into runtime.interaction_state."""
    if _dbg.active():
        emit_debug_log_messages(read_quickjs_debug_log(ctx))
        try:
            ctx.eval("if (typeof __readoriDebugLog !== 'undefined') __readoriDebugLog.length = 0;")
        except Exception:
            pass
    try:
        raw = ctx.eval(
            "JSON.stringify(typeof __readoriSuppressedInteractionLog === 'undefined' ? [] : __readoriSuppressedInteractionLog)"
        )
        kinds = json.loads(str(raw or "[]"))
    except Exception:
        return
    if not isinstance(kinds, list):
        return
    for kind in kinds:
        record_suppressed_interaction(runtime, _js_text(kind))
    try:
        ctx.eval("if (typeof __readoriSuppressedInteractionLog !== 'undefined') __readoriSuppressedInteractionLog.length = 0;")
    except Exception:
        pass


def install_quickjs_app_cryptojs(ctx: Any, plan: dict[str, bool], phase: str) -> None:
    """phase "jslib": 1.1.1 fake global / 1.1.2 builtin before jsLib. phase "code": 1.1.2 builtin before the rule script.

    Every install is best-effort (the App clears a failed install's exception too).
    """
    if phase == "jslib":
        if plan.get("legacyCryptoJS"):
            source = app_profiles.APP_111_CRYPTOJS_POLYFILL
        elif plan.get("cryptoJSBeforeJsLib"):
            source = app_profiles.APP_112_CRYPTOJS_BUILTIN
        else:
            return
    else:
        if not plan.get("cryptoJSBeforeCode"):
            return
        source = app_profiles.APP_112_CRYPTOJS_BUILTIN
    try:
        # The native bridges (__readoriCryptoJS*) are already JS globals from install_quickjs_app_globals.
        ctx.eval(source)
    except Exception:
        pass


# R145b: Node sandbox for source JS. Source scripts are untrusted; the validator may run on a server whose environment
# holds the API key. Defences, outermost first:
#   1. minimal environment (only what node needs to start; nothing from the parent's secrets),
#   2. the Node permission model (`--permission`, Node >= 23.5): no fs read/write (the runner is passed with -e and
#      reads only stdin), no child_process, no worker, no addons, no wasi. It does NOT restrict the network,
#   3. inside the runner (NODE_JS_RUNNER): before untrusted code runs the host realm is stripped - host
#      `Function.prototype.constructor` (the `constructor.constructor('return process')()` escape), `process`,
#      `require`, `console`, `fetch`, `WebSocket`, ... are removed - so the escape cannot reach a socket either.
NODE_ENV_KEEP = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "TZ",
    "READORI_DEBUG_NODE_JS",
)
NODE_OPENSSL_LEGACY_OPTION = "--openssl-legacy-provider"
_node_permission_support: dict[str, bool] = {}
_node_permission_lock = threading.Lock()


def node_sandbox_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The only environment variables a node child gets (case-insensitive match, so Windows `SystemRoot` is kept)."""
    source = os.environ if environ is None else environ
    keep = {name.upper() for name in NODE_ENV_KEEP}
    env = {str(key): str(value) for key, value in source.items() if str(key).upper() in keep}
    env["NODE_OPTIONS"] = NODE_OPENSSL_LEGACY_OPTION
    return env


def node_supports_permission_model(node: str) -> bool:
    """Whether `node --permission` is accepted (Node >= 23.5; v24 verified). Probed once per binary."""
    with _node_permission_lock:
        cached = _node_permission_support.get(node)
    if cached is not None:
        return cached
    try:
        probe = subprocess.run(
            [node, "--permission", "-e", "process.exit(process.permission && !process.permission.has('child') ? 0 : 3)"],
            capture_output=True,
            timeout=15,
            env=node_sandbox_env(),
        )
        supported = probe.returncode == 0
    except Exception:
        supported = False
    with _node_permission_lock:
        _node_permission_support[node] = supported
    if not supported:
        print(
            f"[source-validator] WARNING: {node} does not support --permission (needs Node >= 23.5); source JS runs "
            "without the Node permission model (environment minimisation + in-runner host-realm hardening only). "
            "Install Node 24 or newer.",
            file=sys.stderr,
            flush=True,
        )
    return supported


_node_runner_lock = threading.Lock()
_node_runner_dir: str | None = None


def _cleanup_node_runner_dir() -> None:
    directory = _node_runner_dir
    if directory:
        shutil.rmtree(directory, ignore_errors=True)


def node_runner_script() -> str | None:
    """Path of the runner script. It is ~33 KB, more than the Windows 32767-char command line allows for `-e`, so it
    lives in a private per-process temp directory (mkdtemp: owner-only) that is the only path node may read."""
    global _node_runner_dir
    digest = hashlib.sha1(NODE_JS_RUNNER.encode("utf-8")).hexdigest()[:12]
    with _node_runner_lock:
        try:
            if not _node_runner_dir or not Path(_node_runner_dir).is_dir():
                _node_runner_dir = tempfile.mkdtemp(prefix="readori-node-")
                atexit.register(_cleanup_node_runner_dir)
            script = Path(_node_runner_dir) / f"readori-runner-{digest}.cjs"
            if not script.is_file():
                script.write_bytes(NODE_JS_RUNNER.encode("utf-8"))
            return str(script)
        except OSError:
            return None


def node_launch_command(node: str) -> list[str]:
    """argv for one sandboxed node run. With the permission model: `--permission` and no `--allow-*` grant except
    read access to the runner directory (deny fs write, child_process, worker, addons, wasi, inspector)."""
    script = node_runner_script()
    flags: list[str] = []
    if node_supports_permission_model(node):
        flags.append("--permission")
        if script:
            flags.append("--allow-fs-read=" + str(Path(script).parent))
    return [node, *flags, script] if script else [node, *flags, "-e", NODE_JS_RUNNER]


def evaluate_js_with_node(script: str, input_text: str, runtime: RuleRuntime, extra_vars: dict[str, Any] | None = None) -> str:
    node = node_binary_path()
    if not node:
        return ""
    app_js_plan = app_profiles.app_js_setup_plan(runtime.js_lib or "", script)
    extra_vars = dict(extra_vars or {})
    inject_rar_script_fixtures(script, runtime, extra_vars)
    should_materialize_dynamic_requests = "__readoriRequestLog" not in extra_vars
    extra_vars.setdefault("__readoriRequestLog", [])
    payload = {
        "code": script,
        "inputText": input_text,
        "baseUrl": runtime.base_url,
        "sourceUrl": runtime.source_url,
        "bookSourceUrl": runtime.book_source_url,
        "ruleUrl": runtime.rule_url or runtime.base_url,
        "ruleRequestMethod": runtime.rule_request_method,
        "ruleRequestBody": runtime.rule_request_body,
        "cookie": runtime.cookie,
        "previousResult": runtime.previous_result,
        "bookName": runtime.book_name,
        "bookAuthor": runtime.book_author,
        "bookIntro": runtime.book_intro,
        "bookKind": runtime.book_kind,
        "bookCoverUrl": runtime.book_cover_url,
        "bookLastChapter": runtime.book_last_chapter,
        "bookUrl": runtime.book_url,
        "bookTocUrl": runtime.book_toc_url,
        "bookOrigin": runtime.book_origin,
        "bookOriginName": runtime.book_origin_name,
        "bookType": runtime.book_type,
        "bookDurChapterIndex": runtime.book_dur_chapter_index,
        "bookDurChapterTitle": runtime.book_dur_chapter_title,
        "bookTotalChapterNum": runtime.book_total_chapter_num,
        "bookCanUpdate": runtime.book_can_update,
        "bookCustomIntro": runtime.book_custom_intro,
        "bookVariable": runtime.book_variable,
        "bookUseReplaceRule": runtime.book_use_replace_rule,
        "bookReverseToc": runtime.book_reverse_toc,
        "bookImageStyle": runtime.book_image_style,
        "bookOrder": runtime.book_order,
        "chapterUrl": runtime.chapter_url,
        "chapterTitle": runtime.chapter_title,
        "chapterTag": runtime.chapter_tag,
        "chapterIsVolume": runtime.chapter_is_volume,
        "chapterIsVip": runtime.chapter_is_vip,
        "chapterIsPay": runtime.chapter_is_pay,
        "chapterIndex": runtime.chapter_index,
        "chapterCount": runtime.chapter_count,
        "chapterVariable": runtime.chapter_variable,
        "chapterActive": runtime.chapter_active,
        "bookIsNull": runtime_binds_book_as_null(runtime),
        "sourceBookSourceName": runtime.source_book_source_name,
        "sourceBookSourceComment": runtime.source_book_source_comment,
        "sourceVariableComment": runtime.source_variable_comment,
        "sourceBookSourceType": runtime.source_book_source_type,
        "sourceLastUpdateTime": runtime.source_last_update_time,
        "sourceHeader": runtime.source_header,
        "sourceLoginUrl": runtime.source_login_url,
        "sourceLoginUi": runtime.source_login_ui,
        "sourceEnabledCookieJar": runtime.source_enabled_cookie_jar,
        "sourceBookSourceGroup": runtime.source_book_source_group,
        "sourceExploreUrl": runtime.source_explore_url,
        "sourceSearchUrl": runtime.source_search_url,
        "sourceLoginHeader": runtime.source_login_header,
        "sourceLoginInfo": runtime.source_login_info,
        "sourceConcurrentRate": runtime.source_concurrent_rate,
        "headers": runtime.headers,
        "variables": runtime.variables or {},
        "extraVars": extra_vars,
        "jsLib": _js_lib_for_engine(runtime),
        "javaApi": _build_java_api_js(runtime),
        "allowsUserInteraction": bool(getattr(runtime, "allows_user_interaction", True)),
        "getElementReturnsList": bool(app_profiles.current_app_profile().get_element_returns_list),
        "jsoupExtras": bool(app_profiles.current_app_profile().jsoup_shim_extras),
        "listNonEnumerable": bool(app_profiles.current_app_profile().js_list_helpers_non_enumerable),
        "getStringUnescapeFlag": bool(app_profiles.current_app_profile().get_string_unescape_flag),
        "getElementsJsonObjects": bool(app_profiles.current_app_profile().js_get_elements_json_objects),
        "r158Flags": _r158_js_flags(),
        "tailControlFlow": _js_tail_is_control_flow(script),
        "appJs": app_js_plan,
        "appJsSources": app_cryptojs_sources(app_js_plan),
        "percentEncodeMap": build_percent_encode_map(
            input_text,
            extra_vars or {},
            runtime.variables or {},
            runtime.book_name,
            runtime.book_author,
            runtime.chapter_title,
        ),
    }
    def run_node(current_payload: dict[str, Any]) -> dict[str, Any] | None:
        # R158: the App has no JS CPU limit of its own; a source is bounded by the per-source bulk timeout (BookSourceValidator.swift
        # bulkTimeoutCeiling 15 s for >= 100 sources, 20 s otherwise), and `remaining_source_validation_seconds` clamps to what is left of that
        # budget. A fixed 4 s cut was the CI-only `TimeoutExpired` on shubl / shuqi when many workers started node processes at once.
        node_timeout = remaining_source_validation_seconds(NODE_JS_SUBPROCESS_SECONDS)
        # R145b: minimal environment (no server token / API key reaches source scripts) + Node permission model.
        env = node_sandbox_env()
        try:
            proc = subprocess.run(
                node_launch_command(node),
                input=json.dumps(current_payload, ensure_ascii=True),
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=node_timeout,
                env=env,
            )
        except Exception as _swallowed:
            note_debug_error("node js subprocess", _swallowed)
            return None
        if proc.returncode != 0:
            if os.environ.get("READORI_DEBUG_NODE_JS"):
                print(proc.stderr, file=sys.stderr, flush=True)
            return None
        if os.environ.get("READORI_DEBUG_NODE_JS") and proc.stderr:
            print(proc.stderr, file=sys.stderr, flush=True)
        stdout = (proc.stdout or "").strip()
        try:
            result_payload = json.loads(stdout)
        except Exception:
            return {"value": stdout, "variables": {}, "requests": []}
        return result_payload if isinstance(result_payload, dict) else None

    node_result: dict[str, Any] | None = None
    fixtures = dict(extra_vars.get("__readoriResponseFixtures") or {})
    dynamic_session = requests.Session()
    if should_materialize_dynamic_requests:
        # Only evaluations that can be replayed (the request log decides whether another pass is needed) freeze the clock, so a
        # time-derived URL is identical in every pass; a single-pass evaluation (replay disabled) sees the real clock.
        payload["frozenNowMs"] = int(time.time() * 1000)
    for _ in range(NODE_DYNAMIC_REQUEST_REPLAY_LIMIT):
        payload["extraVars"]["__readoriRequestLog"] = []
        if fixtures:
            payload["extraVars"]["__readoriResponseFixtures"] = fixtures
        node_result = run_node(payload)
        if not node_result:
            return ""
        for interaction_kind in node_result.get("interactions") or []:
            record_suppressed_interaction(runtime, str(interaction_kind))
        # If the rule already produced a value, any logged browser/navigation
        # requests are side effects rather than missing data dependencies. Do
        # not turn guarded CAPTCHA/settings fallbacks into extra validation I/O.
        # R157 (js G2 jjwxc): a pass that THREW (a dynamic request still answered "" before its fixture existed) has not produced
        # a value even if the error handler returned text; only an error-free non-empty result ends the replay.
        if str(node_result.get("value") or "") and not node_result.get("scriptError"):
            break
        discovered = (
            materialize_js_request_log_fixtures(
                node_result.get("requests"),
                runtime,
                existing_fixtures=fixtures,
                session=dynamic_session,
            )
            if should_materialize_dynamic_requests
            else {}
        )
        if not discovered:
            break
        fixtures.update(discovered)

    if not isinstance(node_result, dict) or "value" not in node_result:
        return ""
    # R147 (H9): the replay loop above re-runs the script; only the final run's java.log output is reported.
    emit_debug_log_messages(node_result.get("logs"))
    if node_result.get("scriptError"):
        record_script_error(runtime, str(node_result.get("scriptError")), script)
    variables = node_result.get("variables")
    if isinstance(variables, dict):
        runtime.variables = dict(runtime.variables or {})
        for key, value in variables.items():
            if key:
                runtime.variables[str(key)] = "" if value is None else str(value)
        sync_runtime_book_state_from_variables(runtime)
        sync_runtime_chapter_state_from_variables(runtime)
    return "" if node_result.get("value") is None else str(node_result.get("value"))


def _js_lib_crypto_needed(code: str, runtime: RuleRuntime) -> bool:
    """R157 (tocContent K): the node-crypto routing looked at the rule script only. A script that merely calls a jsLib helper
    (`encode(...)` -> `java.createSymmetricCrypto(...).encryptBase64`) ran on QuickJS where the crypto shims degrade to
    identity, so the request carried plaintext. Route to node when the jsLib uses a crypto marker and the script calls
    something the jsLib declares. Legado has no such split (Rhino runs everything), this only picks the emulation engine."""
    lib = str(getattr(runtime, "js_lib", "") or "")
    if not lib or not js_requires_node_crypto(lib):
        return False
    names = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", lib))
    names.update(re.findall(r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:function|\()", lib))
    return any(re.search(r"(?<![\w$.])" + re.escape(n) + r"\s*\(", code) for n in names)


def _source_comment_crypto_needed(code: str, runtime: RuleRuntime) -> bool:
    """R157 (searchExplore G4): a script that `eval(String(source.bookSourceComment))` runs the comment as code (bookld: the
    `java.md5Encode` signature lives in the comment), so the comment decides the engine like the script text does."""
    if "bookSourceComment" not in code:
        return False
    comment = str(getattr(runtime, "source_book_source_comment", "") or "")
    return bool(comment) and js_requires_node_crypto(comment)


def _js_obfuscated_needs_node(code: str) -> bool:
    """R158 (F, lcread): a jsfuck-style script with renamed non-ASCII identifiers builds its `java.*` calls (the anti-tamper
    `java.HMacBase64(source.bookSourceComment, 'HmacSHA256', key)` + DES decrypt) from characters at run time, so the text scan of
    `js_requires_node_crypto` cannot see them and QuickJS degrades HMacBase64 to a plain base64 (the App runs the native HMAC in
    JavaScriptCore, JSEngine.swift `HMacBase64`). Such a script (long, mostly characters above U+2000, no literal `java.`) goes to node."""
    if len(code) < 1500 or "java." in code.lower():
        return False
    non_ascii = sum(1 for ch in code if ord(ch) > 0x2000)
    return non_ascii * 3 > len(code)


def js_needs_node_crypto_engine(code: str, runtime: RuleRuntime) -> bool:
    return (
        js_requires_node_crypto(code)
        or _js_obfuscated_needs_node(code)
        or _js_lib_crypto_needed(code, runtime)
        or _source_comment_crypto_needed(code, runtime)
    )


def js_requires_node_crypto(script: str) -> bool:
    low = script.lower()
    return any(
        marker in low
        for marker in (
            "java.md5encode",
            "java.md5encode16",
            "java.digesthex",
            "java.digestbase64str",
            "java.hmachex",
            "java.hmacbase64",
            "java.aesbase64decodetostring",
            "java.aesbase64decodetobytearray",
            "java.createsymmetriccrypto",
            "java.createasymmetriccrypto",
            "java.createsign",
            "java.aesdecodeto",
            "java.aesdecodeargsbase64str",
            "java.aesencodeto",
            "java.aesencodeargsbase64str",
            "java.desdecodetostring",
            "java.desbase64decodetostring",
            "java.desencodeto",
            "java.tripledes",
            "java.getzipstringcontent",
            "java.getzipbytearraycontent",
            "java.unzipfile",
            "java.unarchivefile",
            "inflaterinputstream",
            "gzipinputstream",
            "bytearrayinputstream",
            "bytearrayoutputstream",
            "mac.getinstance",
            "messagedigest.getinstance",
            "cipher.getinstance",
            "secretkeyfactory.getinstance",
            "signature.getinstance",
            "hmacsha256",
            "sha256withrsa",
        )
    )


def build_percent_encode_map(*value_sources: Any) -> dict[str, dict[str, str]]:
    values: set[str] = set()

    def collect(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)
            return
        text = str(value)
        if text:
            values.add(text)

    for source in value_sources:
        collect(source)
    for keyword in FALLBACK_KEYWORDS:
        collect(keyword)

    out: dict[str, dict[str, str]] = {}
    for charset in ("gbk", "gb2312", "gb18030"):
        encoded: dict[str, str] = {}
        for value in values:
            encoded[value] = encode_form_keyword(value, charset)
        out[charset] = encoded
    return out


def collect_static_js_request_urls(script: str, include_browser_await: bool = True) -> list[tuple[str, str, str, bool]]:
    """Collect literal java network calls so the Node fallback can mirror QuickJS fetch callbacks."""
    code = script or ""
    requests_out: list[tuple[str, str, str, bool]] = []

    def decode_literal(value: str) -> str:
        if "\\" not in value:
            return value
        try:
            return bytes(value, "utf-8").decode("unicode_escape")
        except Exception:
            return value

    def add(method: str, url: str, body: str = "", follow_redirects: bool = True) -> None:
        value = (url or "").strip()
        if not value:
            return
        requests_out.append((method.upper(), value, body or "", follow_redirects))

    string_re = r"""(['"])((?:\\.|(?!\1).)*?)\1"""
    for name in (
        "ajax", "connect", "startBrowserAwait", "startBrowserAwaitAwait",
        "cacheFile", "importScript",
    ):
        if not include_browser_await and name.startswith("startBrowserAwait"):
            continue
        pattern = rf"""java\s*\.\s*{name}\s*\(\s*{string_re}"""
        for match in re.finditer(pattern, code):
            add("GET", decode_literal(match.group(2)))
    for name, method in (("getResponse", "GET"), ("head", "HEAD")):
        pattern = rf"""java\s*\.\s*{name}\s*\(\s*{string_re}"""
        for match in re.finditer(pattern, code):
            add(method, decode_literal(match.group(2)), follow_redirects=False)
    # AnalyzeRule.get(key) is the one-argument variable lookup. The network
    # overload is JsExtensions.get(url, headers) and therefore requires a
    # second argument even when the key happens to look like an absolute URL.
    get_pattern = rf"""java\s*\.\s*get\s*\(\s*{string_re}\s*,"""
    for match in re.finditer(get_pattern, code):
        add("GET", decode_literal(match.group(2)), follow_redirects=False)
    get_absolute_pattern = rf"""java\s*\.\s*get\s*\(\s*{string_re}"""
    for match in re.finditer(get_absolute_pattern, code):
        literal = decode_literal(match.group(2))
        if "://" in literal:
            add("GET", literal, follow_redirects=False)
    webview_pattern = rf"""java\s*\.\s*web[Vv]iew(?:GetSource|GetOverrideUrl)?\s*\(\s*(?:null|undefined|['"][^'"]*['"])?\s*,\s*{string_re}"""
    for match in re.finditer(webview_pattern, code):
        add("GET", decode_literal(match.group(2)))
    post_pattern = rf"""java\s*\.\s*(?:post|postBody)\s*\(\s*{string_re}\s*(?:,\s*{string_re})?"""
    for match in re.finditer(post_pattern, code):
        body = decode_literal(match.group(4)) if match.lastindex and match.lastindex >= 4 and match.group(4) is not None else ""
        add("POST", decode_literal(match.group(2)), body, follow_redirects=False)
    ajax_all_pattern = rf"""java\s*\.\s*ajaxAll\s*\(\s*\[([^\]]*)\]"""
    for list_match in re.finditer(ajax_all_pattern, code, flags=re.S):
        for string_match in re.finditer(string_re, list_match.group(1)):
            add("GET", decode_literal(string_match.group(2)))

    deduped: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in requests_out:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def materialize_js_request_log_fixtures(
    request_log: Any,
    runtime: RuleRuntime,
    existing_fixtures: dict[str, dict[str, Any]] | None = None,
    session: requests.Session | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch bounded URLs that a Node rule computed only while executing.

    The Node fallback cannot call Python synchronously like QuickJS can. A first
    pass therefore records dynamic requests, Python materializes their responses,
    and the caller replays the rule. Multiple bounded passes support a request
    whose URL depends on the previous response without allowing unbounded crawl.
    """
    if not isinstance(request_log, list):
        return {}
    known = existing_fixtures or {}
    generated: dict[str, dict[str, Any]] = {}
    network_session = session or requests.Session()
    for entry in request_log[:NODE_DYNAMIC_REQUESTS_PER_REPLAY]:
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("url") or "").strip()
        if not target or urlparse(target).scheme.lower() not in {"http", "https"}:
            continue
        if target in known or target in generated:
            continue
        method = str(entry.get("method") or "GET").upper()
        if method not in {"GET", "POST", "HEAD"}:
            continue
        body = str(entry.get("body") or "")
        headers = dict(runtime.headers or {})
        raw_headers = entry.get("headers")
        if isinstance(raw_headers, dict):
            headers.update({
                str(key): str(value)
                for key, value in raw_headers.items()
                if key and value is not None
            })
        response_meta: dict[str, Any] = {}
        text, final_url, status = fetch_text(
            network_session,
            target,
            method=method,
            headers=headers,
            body=body if method == "POST" else None,
            timeout=min(REQUEST_TIMEOUT, 12),
            allow_redirects=entry.get("followRedirects") is not False,
            response_meta=response_meta,
            retry_with_default_headers=False,
        )
        payload = {
            "body": text,
            "url": final_url or target,
            "status": int(status or (200 if text else 0)),
            "headers": response_meta.get("headers", {}),
            "cookies": response_meta.get("cookies", {}),
        }
        if response_meta.get("bodyBase64"):
            payload["bodyBase64"] = response_meta["bodyBase64"]
        generated[target] = payload
        if final_url:
            generated[final_url] = payload
    return generated


def build_js_response_fixtures(script: str, runtime: RuleRuntime) -> dict[str, dict[str, Any]]:
    fixtures: dict[str, dict[str, Any]] = {}
    # App batch validation blocks startBrowserAwait (the script throws before any request), so nothing is prefetched.
    browser_await_allowed = bool(getattr(runtime, "allows_user_interaction", True))
    static_requests = collect_static_js_request_urls(script, include_browser_await=browser_await_allowed)
    variable_urls = {
        "baseUrl": runtime.base_url,
        "sourceUrl": runtime.source_url,
        "bookSourceUrl": runtime.book_source_url,
        "ruleUrl": runtime.rule_url or runtime.base_url,
    }
    for variable_name, variable_url in variable_urls.items():
        if not variable_url:
            continue
        if browser_await_allowed and re.search(rf"java\s*\.\s*startBrowserAwait(?:Await)?\s*\(\s*{variable_name}\b", script or ""):
            static_requests.append(("GET", variable_url, "", True))
        if re.search(rf"java\s*\.\s*web[Vv]iew(?:GetSource|GetOverrideUrl)?\s*\(\s*(?:null|undefined|['\"][^'\"]*['\"])?\s*,\s*{variable_name}\b", script or ""):
            static_requests.append(("GET", variable_url, "", True))
    for alias in re.findall(
        r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*java\s*\.\s*ruleUrl\b",
        script or "",
    ):
        if browser_await_allowed and runtime.rule_url and re.search(
            rf"java\s*\.\s*startBrowserAwait(?:Await)?\s*\(\s*{re.escape(alias)}\b",
            script or "",
        ):
            static_requests.append(("GET", runtime.rule_url, "", True))
    if runtime.rule_url and re.search(
        r"java\s*\.\s*getStrResponse\s*\(",
        script or "",
    ):
        static_requests.append((
            runtime.rule_request_method.upper() or "GET",
            runtime.rule_url,
            runtime.rule_request_body,
            True,
        ))
    if not static_requests:
        return fixtures
    session = requests.Session()
    seen_requests: set[tuple[str, str, str, bool]] = set()
    for method, raw_url, body, follow_redirects in static_requests:
        request_key = (method, raw_url, body, follow_redirects)
        if request_key in seen_requests:
            continue
        seen_requests.add(request_key)
        url_part, opts = parse_url_options(raw_url)
        effective_method = str(opts.get("method") or method or "GET").upper()
        effective_body = str(opts.get("body") if opts.get("body") is not None else body or "")
        target = urljoin(runtime.base_url or runtime.book_source_url or runtime.source_url, url_part)
        headers = dict(runtime.headers or {})
        if isinstance(opts.get("headers"), dict):
            headers.update({str(k): str(v) for k, v in opts["headers"].items() if v is not None})
        response_meta: dict[str, Any] = {}
        text, final_url, status = fetch_text(
            session,
            target,
            method=effective_method,
            headers=headers,
            body=effective_body if effective_method == "POST" else None,
            allow_redirects=follow_redirects,
            response_meta=response_meta,
        )
        payload = {
            "body": text,
            "url": final_url or target,
            "status": int(status or (200 if text else 0)),
            "headers": response_meta.get("headers", {}),
            "cookies": response_meta.get("cookies", {}),
        }
        fixtures[raw_url] = payload
        fixtures[url_part] = payload
        fixtures[target] = payload
    return fixtures


def rar_entry_json(payload: str, encoding: str, entry_name: str) -> str:
    if rarfile is None:
        return ""
    try:
        data = (
            bytes.fromhex(str(payload or ""))
            if str(encoding or "").lower() == "hex"
            else base64.b64decode(str(payload or ""), validate=False)
        )
        wanted = str(entry_name or "")
        if not data.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")) or not wanted:
            return ""
        with rarfile.RarFile(io.BytesIO(data), mode="r") as archive:
            matched = next(
                (
                    info
                    for info in archive.infolist()
                    if info.filename == wanted and not info.isdir()
                ),
                None,
            )
            if matched is None:
                return ""
            return json.dumps(list(archive.read(matched)), separators=(",", ":"))
    except Exception:
        return ""


def rar_entries_json(bytes_json: str) -> str:
    if rarfile is None:
        return ""
    try:
        values = json.loads(str(bytes_json or "[]"))
        data = bytes((int(value) & 255 for value in values))
        if not data.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
            return ""
        with rarfile.RarFile(io.BytesIO(data), mode="r") as archive:
            files: dict[str, list[int]] = {}
            directories: list[str] = []
            for info in archive.infolist():
                name = str(info.filename or "")
                if not name:
                    continue
                if info.isdir():
                    directories.append(name)
                else:
                    files[name] = list(archive.read(info))
            return json.dumps(
                {"format": "rar", "files": files, "directories": directories},
                ensure_ascii=False,
                separators=(",", ":"),
            )
    except Exception:
        return ""


def build_rar_script_fixtures(
    script: str,
    runtime: RuleRuntime,
) -> tuple[dict[str, dict[str, list[int]]], dict[str, dict[str, Any]]]:
    if rarfile is None or not re.search(
        r"java\s*\.\s*(?:getRar(?:String|ByteArray)Content|unrarFile|unArchiveFile)",
        script or "",
    ):
        return {}, {}

    entry_fixtures: dict[str, dict[str, list[int]]] = {}
    archive_fixtures: dict[str, dict[str, Any]] = {}
    documents: dict[str, dict[str, Any]] = {}

    def add_document(keys: list[str], data: bytes) -> dict[str, Any] | None:
        if not data.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
            return None
        try:
            document = json.loads(rar_entries_json(json.dumps(list(data))))
        except Exception:
            return None
        if not isinstance(document, dict) or document.get("format") != "rar":
            return None
        files = document.get("files")
        if not isinstance(files, dict):
            return None
        normalized_files = {
            str(name): [int(value) & 255 for value in values]
            for name, values in files.items()
            if isinstance(values, list)
        }
        for key in keys:
            if key:
                entry_fixtures[key] = normalized_files
                documents[key] = document
        return document

    for value in re.findall(
        r"""(['"])([0-9a-fA-F]{14,})\1""",
        script or "",
    ):
        raw_hex = value[1]
        try:
            data = bytes.fromhex(raw_hex)
        except ValueError:
            continue
        add_document([raw_hex], data)

    session = requests.Session()
    for match in re.finditer(
        r"""java\s*\.\s*getRar(?:String|ByteArray)Content\s*\(\s*(['"])((?:\\.|(?!\1).)*)\1""",
        script or "",
    ):
        raw_url = match.group(2)
        if not re.match(r"^https?://", raw_url, flags=re.I):
            continue
        url_part, opts = parse_url_options(raw_url)
        target = urljoin(
            runtime.base_url or runtime.book_source_url or runtime.source_url,
            url_part,
        )
        headers = dict(runtime.headers or {})
        if isinstance(opts.get("headers"), dict):
            headers.update({
                str(key): str(value)
                for key, value in opts["headers"].items()
                if value is not None
            })
        try:
            data, final_url, _ = fetch_bytes(session, target, headers=headers)
        except Exception:
            continue
        add_document([raw_url, url_part, target, final_url], data)

    for match in re.finditer(
        r"""java\s*\.\s*downloadFile\s*\(\s*(['"])([0-9a-fA-F]+)\1\s*,\s*(['"])((?:\\.|(?!\3).)*)\3""",
        script or "",
    ):
        raw_hex = match.group(2)
        raw_url = match.group(4)
        document = documents.get(raw_hex)
        if document is None:
            try:
                document = add_document([raw_hex], bytes.fromhex(raw_hex))
            except ValueError:
                document = None
        if document is None:
            continue
        _url_part, opts = parse_url_options(raw_url)
        explicit_type = str(opts.get("type") or "").strip(" .")
        if not explicit_type:
            continue
        safe_type = explicit_type.replace("\\", "/").split("/")[-1].strip() or "ext"
        digest_path = "/" + hashlib.md5(raw_url.encode("utf-8")).hexdigest()[8:24]
        archive_fixtures[f"{digest_path}.{safe_type}"] = document
        # QuickJS deliberately has no native crypto object under its execution
        # time limit, so the existing md5Encode shim falls back to the input
        # string before md5Encode16 applies substring(8, 24).
        fallback_hash = raw_url[8:24] if len(raw_url) >= 24 else raw_url
        archive_fixtures[f"/{fallback_hash}.{safe_type}"] = document

    return entry_fixtures, archive_fixtures


def inject_rar_script_fixtures(
    script: str,
    runtime: RuleRuntime,
    extra_vars: dict[str, Any],
) -> None:
    entry_fixtures, archive_fixtures = build_rar_script_fixtures(script, runtime)
    for key, generated in (
        ("__readoriRarFixtures", entry_fixtures),
        ("__readoriRarArchiveFixtures", archive_fixtures),
    ):
        if not generated:
            continue
        existing = extra_vars.get(key)
        if isinstance(existing, dict):
            merged = dict(generated)
            merged.update(existing)
            extra_vars[key] = merged
        else:
            extra_vars[key] = generated


def seven_zip_entry_json(payload: str, encoding: str, entry_name: str) -> str:
    if py7zr is None or BytesIOFactory is None:
        return ""
    try:
        data = (
            bytes.fromhex(str(payload or ""))
            if str(encoding or "").lower() == "hex"
            else base64.b64decode(str(payload or ""), validate=False)
        )
        wanted = str(entry_name or "")
        if not data or not wanted:
            return ""
        with py7zr.SevenZipFile(io.BytesIO(data), mode="r") as archive:
            matched = next(
                (
                    name
                    for name in archive.getnames()
                    if name == wanted
                ),
                "",
            )
            if not matched:
                return ""
            factory = BytesIOFactory(limit=64 * 1024 * 1024)
            archive.extract(targets=[matched], factory=factory)
            return json.dumps(
                list(factory.get(matched).read()),
                separators=(",", ":"),
            )
    except Exception:
        return ""


def seven_zip_entries_json(bytes_json: str) -> str:
    if py7zr is None or BytesIOFactory is None:
        return ""
    try:
        values = json.loads(str(bytes_json or "[]"))
        data = bytes((int(value) & 255 for value in values))
        if not data.startswith(b"7z\xbc\xaf'\x1c"):
            return ""
        with py7zr.SevenZipFile(io.BytesIO(data), mode="r") as archive:
            infos = archive.list()
            factory = BytesIOFactory(limit=64 * 1024 * 1024)
            archive.extractall(factory=factory)
            files: dict[str, list[int]] = {}
            directories: list[str] = []
            for info in infos:
                name = str(info.filename or "")
                if info.is_directory:
                    directories.append(name)
                elif info.is_file and not info.is_symlink:
                    files[name] = list(factory.get(name).read())
            return json.dumps(
                {
                    "format": "7z",
                    "files": files,
                    "directories": directories,
                },
                separators=(",", ":"),
            )
    except Exception:
        return ""


def repair_glued_var_declarations(script: str) -> str:
    """Repair only unambiguous `varname=` export damage.

    A declaration is changed only when its suffix also appears as a standalone
    identifier elsewhere in the same script. This keeps legitimate variables
    such as `varid` unchanged while recovering public rules exported as
    `varid=...; ... id ...` / `variid=...; ... iid ...`.
    """
    candidates: list[tuple[str, str]] = []
    for match in re.finditer(r"(?<![A-Za-z0-9_$])var([A-Za-z_$][A-Za-z0-9_$]*)\s*=", script):
        identifier = match.group(1)
        standalone = re.compile(
            rf"(?<![A-Za-z0-9_$]){re.escape(identifier)}(?![A-Za-z0-9_$])"
        )
        declaration_removed = script[: match.start()] + script[match.end() :]
        if standalone.search(declaration_removed):
            candidates.append((match.group(0), f"var {identifier}="))
    repaired = script
    for original, replacement in candidates:
        repaired = repaired.replace(original, replacement, 1)
    return repaired


def _quickjs_error_message(error: BaseException) -> str:
    text = str(error or "").strip()
    # quickjs.JSException text is "ReferenceError: x is not defined\n    at …".
    first = text.splitlines()[0].strip() if text else ""
    if re.search(r"interrupted|timed out|out of memory", first, flags=re.I):
        return ""  # engine limits are not source-script exceptions
    if first == "TypeError: not a function":
        # QuickJS drops the callee text; V8 / JavaScriptCore both say "<callee> is not a function".
        first = "TypeError: value is not a function"
    return first or type(error).__name__


def quickjs_value_text(value: Any) -> str:
    """Script completion value as text, the way the Node runner reports it.

    python-quickjs hands back Python objects: str() of a quickjs.Object is a Python repr, and True / 1.0 would print as
    "True" / "1.0". The Node path (and the App) give strings as-is, objects / arrays as JSON, everything else as
    String(value).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return "NaN"
        if value in (float("inf"), float("-inf")):
            return "Infinity" if value > 0 else "-Infinity"
        if value == int(value) and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    json_method = getattr(value, "json", None)
    if callable(json_method):
        try:
            return str(json_method() or "")
        except Exception:
            return ""
    return str(value)


def _is_top_level_return_error(message: str) -> bool:
    lower = (message or "").lower()
    return (
        "return not in a function" in lower
        or "illegal return statement" in lower
        or "return statements are only valid inside functions" in lower
    )


_JS_LIST_URL_ATTR_RE = re.compile(r"@\s*(?:href|src|data-src|data-href|action)\s*$", re.IGNORECASE)


def evaluate_js_string_list(rule: str, html: str, runtime: RuleRuntime) -> list[str]:
    """java.getStringList(rule): the rule's values; href / src style attributes come back resolved like the Node native."""
    mode = "json" if is_json_content(html) else "html"
    values = evaluate_rule(rule, html, runtime, mode)
    if mode != "html" or not values or "<js>" in rule or "@js:" in rule or "{{" in rule:
        return values
    base = runtime.base_url or runtime.book_source_url or "https://example.test/"
    for branch in rule.split("||"):
        branch = branch.strip()
        if not branch:
            continue
        branch_values = evaluate_rule(branch, html, runtime, mode)
        if not branch_values:
            continue
        if _JS_LIST_URL_ATTR_RE.search(branch):
            return [urljoin(base, str(value)) if value else value for value in branch_values]
        return values
    return values


_JS_REGEX_LITERAL_PREFIXES = frozenset("(,=:[!&|?{};+-*%<>~^")


def _is_js_format_character(char: str) -> bool:
    return ord(char) > 127 and char != "\ufeff" and unicodedata.category(char) == "Cf"


def _js_lib_for_engine(runtime: "RuleRuntime") -> str:
    """The shared jsLib as the engine evaluates it: App 1.1.2 strips Unicode format characters from it like from every script
    (`JSEngine` bootstrap `strippingJSFormatCharactersOutsideStrings`) and relaxes parameter redeclarations (R158 D1)."""
    lib = runtime.js_lib or ""
    if lib and app_profiles.current_app_profile().js_strip_format_chars:
        lib = strip_js_format_characters_outside_strings(lib)
    if lib and app_profiles.current_app_profile().js_param_redeclare_relaxed:
        lib = relax_parameter_redeclarations(lib)
    return lib


_JS_REGEX_KEYWORDS = frozenset((
    "return", "typeof", "case", "in", "of", "new", "delete", "void", "throw", "instanceof", "do", "else", "yield", "await", "default",
))
_JS_REGEX_PREFIX_CHARS = frozenset("(,=:[!&|?{};+-*%<>~^")


def _js_identifier_start(ch: str) -> bool:
    return ch == "_" or ch == "$" or ch.isalpha()


def _js_identifier_part(ch: str) -> bool:
    return _js_identifier_start(ch) or ch.isdigit()


def _js_code_mask(source: str) -> list[bool]:
    """True for every character that is JavaScript code (False inside strings, templates, comments and regex literals)."""
    count = len(source)
    mask = [True] * count

    def skip_string(start: int) -> int:
        quote = source[start]
        i = start + 1
        while i < count:
            current = source[i]
            if current == "\\":
                i += 2
                continue
            if current == quote:
                return i + 1
            if current == "\n":
                return i
            i += 1
        return count

    def skip_interpolation(start: int) -> int:
        depth = 1
        i = start
        while i < count:
            current = source[i]
            if current in "'\"":
                i = skip_string(i)
                continue
            if current == "`":
                i = skip_template(i)
                continue
            if source.startswith("//", i):
                while i < count and source[i] != "\n":
                    i += 1
                continue
            if source.startswith("/*", i):
                end = source.find("*/", i + 2)
                i = count if end < 0 else end + 2
                continue
            if current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return count

    def skip_template(start: int) -> int:
        i = start + 1
        while i < count:
            current = source[i]
            if current == "\\":
                i += 2
                continue
            if current == "`":
                return i + 1
            if current == "$" and i + 1 < count and source[i + 1] == "{":
                i = skip_interpolation(i + 2)
                continue
            i += 1
        return count

    previous_significant = ""
    previous_word_is_keyword = False
    index = 0
    while index < count:
        char = source[index]
        following = source[index + 1] if index + 1 < count else ""
        if char == "/" and following == "/":
            while index < count and source[index] != "\n":
                mask[index] = False
                index += 1
            continue
        if char == "/" and following == "*":
            end = source.find("*/", index + 2)
            stop = count if end < 0 else end + 2
            for position in range(index, stop):
                mask[position] = False
            index = stop
            continue
        if char in "'\"`":
            end = skip_template(index) if char == "`" else skip_string(index)
            for position in range(index, min(end, count)):
                mask[position] = False
            index = end
            previous_significant = char
            previous_word_is_keyword = False
            continue
        if char == "/":
            regex_allowed = previous_word_is_keyword or not previous_significant or previous_significant in _JS_REGEX_PREFIX_CHARS
            if regex_allowed:
                position = index + 1
                in_class = False
                while position < count:
                    current = source[position]
                    if current == "\\":
                        position += 2
                        continue
                    if current == "\n":
                        break
                    if current == "[":
                        in_class = True
                    elif current == "]":
                        in_class = False
                    if current == "/" and not in_class:
                        position += 1
                        break
                    position += 1
                end = min(position, count)
                for mark in range(index, end):
                    mask[mark] = False
                index = end
                previous_significant = "/"
                previous_word_is_keyword = False
                continue
        if _js_identifier_start(char):
            end = index + 1
            while end < count and _js_identifier_part(source[end]):
                end += 1
            previous_word_is_keyword = source[index:end] in _JS_REGEX_KEYWORDS
            previous_significant = source[end - 1]
            index = end
            continue
        if not char.isspace():
            # `i++ / 2`, `i-- / 2`: a `/` after an increment / decrement is a division, not a regex start (App JSEngine.swift ~1612).
            if char in "+-" and index > 0 and source[index - 1] == char:
                previous_significant = ")"
            else:
                previous_significant = char
            previous_word_is_keyword = False
        index += 1
    return mask


@functools.lru_cache(maxsize=64)
def relax_parameter_redeclarations(code: str) -> str:
    """R158 D1 (1.1.2 `js_param_redeclare_relaxed`; gongzicp): `function f(text, key, iv) { let key = "..."; ... }`. Rhino (Legado) treats a
    function-body `let` that repeats a parameter name as an assignment to the parameter; JavaScriptCore (App) and QuickJS reject it as an
    early SyntaxError, so the whole jsLib fails to load. Mirrors App `JSEngine.relaxingParameterRedeclarations`: the `let` / `const` keyword
    of a declaration at the outermost brace depth of a `function` body whose first declarator is one of the function's own parameters is
    replaced by `var` (`var key = 1` re-declares the parameter legally, so it is an assignment; the other declarators of the same statement
    stay function-local instead of leaking into the global scope). Block-scoped shadowing (inner braces) is legal and untouched; strings, templates, comments and
    regex literals are never scanned; code without `function` and `let` / `const` is returned as it is."""
    if "function" not in code or ("let " not in code and "const " not in code):
        return code
    count = len(code)
    mask = _js_code_mask(code)

    def matches(keyword: str, position: int) -> bool:
        if not code.startswith(keyword, position):
            return False
        end = position + len(keyword)
        before_ok = position == 0 or not (_js_identifier_part(code[position - 1]) or code[position - 1] == ".")
        after_ok = end >= count or not _js_identifier_part(code[end])
        return before_ok and after_ok

    def skip_whitespace(position: int) -> int:
        while position < count and code[position].isspace():
            position += 1
        return position

    def closing_index(open_at: int, open_char: str, close_char: str) -> int | None:
        depth = 0
        cursor = open_at
        while cursor < count:
            if mask[cursor]:
                if code[cursor] == open_char:
                    depth += 1
                elif code[cursor] == close_char:
                    depth -= 1
                    if depth == 0:
                        return cursor
            cursor += 1
        return None

    keyword_spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor < count:
        if not (mask[cursor] and code[cursor] == "f" and matches("function", cursor)):
            cursor += 1
            continue
        after_keyword = cursor + len("function")
        position = skip_whitespace(after_keyword)
        cursor = after_keyword
        if position < count and code[position] == "*":
            position = skip_whitespace(position + 1)
        if position < count and _js_identifier_start(code[position]):
            while position < count and _js_identifier_part(code[position]):
                position += 1
            position = skip_whitespace(position)
        if position >= count or code[position] != "(":
            continue
        parameter_close = closing_index(position, "(", ")")
        if parameter_close is None:
            continue
        body_open = skip_whitespace(parameter_close + 1)
        if body_open >= count or code[body_open] != "{":
            continue
        body_close = closing_index(body_open, "{", "}")
        if body_close is None:
            continue
        body_open += 1

        parameters: set[str] = set()
        part_start = position + 1
        nesting = 0
        scan = position + 1
        while scan <= parameter_close:
            if scan == parameter_close or (nesting == 0 and mask[scan] and code[scan] == ","):
                start = skip_whitespace(part_start)
                if start < scan and _js_identifier_start(code[start]):
                    end = start
                    while end < scan and _js_identifier_part(code[end]):
                        end += 1
                    parameters.add(code[start:end])
                part_start = scan + 1
            elif mask[scan]:
                if code[scan] in "([{":
                    nesting += 1
                elif code[scan] in ")]}":
                    nesting -= 1
            scan += 1
        if not parameters:
            continue

        depth = 0
        body_index = body_open
        while body_index < body_close:
            if not mask[body_index]:
                body_index += 1
                continue
            char = code[body_index]
            if char in "{([":
                depth += 1
            elif char in "})]":
                depth -= 1
            elif depth == 0 and char in "lc":
                keyword_length = 3 if matches("let", body_index) else (5 if matches("const", body_index) else 0)
                if keyword_length and body_index + keyword_length < count and code[body_index + keyword_length].isspace():
                    name_start = skip_whitespace(body_index + keyword_length)
                    declared_start = name_start
                    while name_start < count and _js_identifier_part(code[name_start]):
                        name_start += 1
                    if name_start > declared_start and code[declared_start:name_start] in parameters:
                        keyword_spans.append((body_index, body_index + keyword_length))
                    body_index = name_start
                    continue
            body_index += 1
    if not keyword_spans:
        return code
    out: list[str] = []
    last = 0
    for start, stop in sorted(keyword_spans):
        out.append(code[last:start])
        out.append("var")
        last = stop
    out.append(code[last:])
    return "".join(out)


def strip_js_format_characters_outside_strings(code: str) -> str:
    """R156 (U+200B, App profile 1.1.2): Rhino (Legado, TokenStream.getChar(skipFormattingChars = true)) skips Unicode format
    characters (Cf: U+200B, U+200C, U+200D, U+2060, ...) everywhere outside string / template text, so a script such as
    `source.getSource()` copied with a zero-width space (U+200B) in the name runs in Legado, while QuickJS / JavaScriptCore reject it.
    Mirrors `JSEngine.strippingJSFormatCharactersOutsideStrings`: strings, templates, comments and regex literals are copied
    untouched; U+FEFF stays (it is whitespace to the lexer)."""
    if not any(_is_js_format_character(char) for char in code):
        return code
    out: list[str] = []
    index = 0
    length = len(code)
    previous_significant = ""

    def copy_quoted(quote: str) -> None:
        nonlocal index
        out.append(code[index])
        index += 1
        while index < length:
            current = code[index]
            out.append(current)
            index += 1
            if current == "\\" and index < length:
                out.append(code[index])
                index += 1
                continue
            if current == quote:
                break
            if current == "\n" and quote != "`":
                break

    def copy_regex_literal() -> None:
        nonlocal index
        out.append(code[index])
        index += 1
        in_class = False
        while index < length:
            current = code[index]
            out.append(current)
            index += 1
            if current == "\\" and index < length:
                out.append(code[index])
                index += 1
                continue
            if current == "\n":
                break
            if current == "[":
                in_class = True
            elif current == "]":
                in_class = False
            if current == "/" and not in_class:
                break

    while index < length:
        char = code[index]
        if _is_js_format_character(char):
            index += 1
            continue
        following = code[index + 1] if index + 1 < length else ""
        if char == "/" and following == "/":
            while index < length:
                current = code[index]
                out.append(current)
                index += 1
                if current == "\n":
                    break
        elif char == "/" and following == "*":
            out.append("/*")
            index += 2
            while index < length:
                current = code[index]
                out.append(current)
                index += 1
                if current == "*" and index < length and code[index] == "/":
                    out.append("/")
                    index += 1
                    break
        elif char in "\"'`":
            copy_quoted(char)
            previous_significant = char
        elif char == "/" and (not previous_significant or previous_significant in _JS_REGEX_LITERAL_PREFIXES):
            copy_regex_literal()
            previous_significant = "/"
        else:
            out.append(char)
            index += 1
            if not char.isspace():
                previous_significant = char
    return "".join(out)


def evaluate_js(script: str, input_text: str, runtime: RuleRuntime, extra_vars: dict[str, Any] | None = None) -> str:
    code = script.strip()
    if not code:
        return ""
    if app_profiles.current_app_profile().js_strip_format_chars:
        code = strip_js_format_characters_outside_strings(code)
    if app_profiles.current_app_profile().js_param_redeclare_relaxed:
        # R158 D1: App evaluateRuleScript = hoistTopLevelLetConstToVar(relaxingParameterRedeclarations(strip(script)))
        code = relax_parameter_redeclarations(code)
    code = repair_glued_var_declarations(code)
    extra_vars = dict(extra_vars or {})
    inject_rar_script_fixtures(code, runtime, extra_vars)
    ajax_result_match = re.fullmatch(r"(?:return\s+)?java\.ajax\(\s*result\s*\)\s*;?", code)
    if ajax_result_match:
        target = urljoin(runtime.base_url, str(input_text or ""))
        text, _, _ = fetch_text(requests.Session(), target, headers=runtime.headers)
        return text
    json_text = extract_json(input_text)
    json_obj: Any | None = None
    if json_text:
        try:
            json_obj = json.loads(json_text)
        except Exception:
            json_obj = None

    def replace_json_placeholder(match: re.Match[str]) -> str:
        expr_text = match.group(1).strip()
        if not expr_text or json_obj is None:
            return match.group(0)
        path = expr_text
        if path.startswith("."):
            path = "$" + path
        elif not path.startswith(("$", "[")):
            path = "$." + path
        path = re.sub(r"\.\*", "[*]", path)
        try:
            expr = parse_jsonpath_expression(path)
            matches = [m.value for m in expr.find(json_obj)]
        except Exception:
            matches = []
        if not matches:
            return "null"
        value = matches[0]
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return json.dumps(str(value), ensure_ascii=False)

    code = re.sub(r"\{\{\s*(\$\.[^}]+)\s*\}\}", replace_json_placeholder, code)
    if node_binary_path() and "__readoriResponseFixtures" not in extra_vars:
        fixtures = build_js_response_fixtures(code, runtime)
        if fixtures:
            extra_vars["__readoriResponseFixtures"] = fixtures
    if js_needs_node_crypto_engine(code, runtime) and node_binary_path():
        return evaluate_js_with_node(code, input_text, runtime, extra_vars=extra_vars)
    # R156 (searchjs J): `result` is an object only when the WHOLE input is JSON (Legado String.isJson()). A JSONP body
    # (`search({...})`) stays the string it is, so `eval(result)` evaluates the callback (the node path already does this).
    result_object = json_obj if is_legado_json_text(input_text or "") else None
    return _evaluate_js_quickjs(code, input_text, runtime, extra_vars, result_object)


# python-quickjs refuses callbacks into Python while a time limit is set ("Can not call into Python with a time limit
# set."), and the time limit is what stops runaway scripts. So Python natives are never called from inside QuickJS:
# each one is a JS stub that answers from a result cache, or records the call and throws. The driver runs the
# script, executes the first recorded call in Python, caches its result and replays the script (the Node path does
# the same with its request log). Results are cached per call position, so repeated identical calls still execute.
QUICKJS_NATIVE_REPLAY_LIMIT = 150
# R158 (G7): JS time summed over the replays (native calls themselves not counted). The App runs the script once and is bounded only by the
# per-source bulk timeout (15 s, BookSourceValidator.swift bulkTimeoutCeiling); a 6 s cap cut `m.jjwxc.net` (20 sequential java.ajax plus
# Jsoup.parse of a 795 KB page per hit, every replay re-runs the prefix). The source deadline still clamps every native call.
QUICKJS_NATIVE_REPLAY_SCRIPT_SECONDS = 15.0
# R158: per-call node subprocess ceiling (the App's bulk per-source timeout, see run_node).
NODE_JS_SUBPROCESS_SECONDS = 15.0
_QUICKJS_NATIVE_STUB = r"""
(function (name) {
  var stringify = JSON.stringify, slice = Array.prototype.slice;
  globalThis[name] = function () {
    var args = slice.call(arguments), size = 0;
    try { size = stringify(args).length; } catch (error) {}
    var key = (__readoriNativeSeq++) + ':' + name + ':' + size;
    var hit = __readoriNativeCache[key];
    if (hit !== undefined) {
      if (hit.e !== undefined) throw new Error(hit.e);
      return hit.v;
    }
    if (__readoriNativeMisses.length === 0) __readoriNativeMisses.push({ key: key, name: name, args: args });
    throw new Error('__readoriNativeMiss');
  };
})(%s);
"""


class _QuickJSNativeBridge:
    """Wraps a quickjs.Context so add_callable installs replay stubs instead of real (blocked) Python callbacks."""

    def __init__(self, ctx: Any, results: dict[str, Any]) -> None:
        self._ctx = ctx
        self.natives: dict[str, Callable[..., Any]] = {}
        try:
            ctx.eval(
                "var __readoriNativeCache = JSON.parse(%s); var __readoriNativeSeq = 0; var __readoriNativeMisses = [];"
                % json.dumps(json.dumps(results, ensure_ascii=False))
            )
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)

    def add_callable(self, name: str, function: Callable[..., Any]) -> None:
        self.natives[name] = function
        self._ctx.eval(_QUICKJS_NATIVE_STUB % json.dumps(name))

    def first_native_miss(self) -> dict[str, Any] | None:
        try:
            raw = self._ctx.eval("JSON.stringify(__readoriNativeMisses.length ? __readoriNativeMisses[0] : null)")
            miss = json.loads(str(raw)) if raw else None
        except Exception:
            return None
        return miss if isinstance(miss, dict) else None

    def has_native_miss(self) -> bool:
        try:
            return bool(self._ctx.eval("__readoriNativeMisses.length"))
        except Exception:
            return False

    def interaction_effects(self, runtime: RuleRuntime) -> list[Callable[[], None]]:
        """Blocked-interaction kinds the JS side logged, as deferred runtime updates."""
        try:
            raw = self._ctx.eval(
                "JSON.stringify(typeof __readoriSuppressedInteractionLog === 'undefined' ? [] : __readoriSuppressedInteractionLog)"
            )
            kinds = json.loads(str(raw or "[]"))
        except Exception:
            return []
        if not isinstance(kinds, list):
            return []
        return [lambda kind=kind: record_suppressed_interaction(runtime, _js_text(kind)) for kind in kinds]

    def debug_log_effects(self) -> list[Callable[[], None]]:
        """R147 (H9): java.log messages the script wrote (only collected while a debug trace was open at build time)."""
        if not _dbg.active():
            return []
        messages = read_quickjs_debug_log(self._ctx)
        return [lambda: emit_debug_log_messages(messages)] if messages else []


def _evaluate_js_quickjs(
    code: str,
    input_text: str,
    runtime: RuleRuntime,
    extra_vars: dict[str, Any] | None,
    json_obj: Any,
) -> str:
    native_results: dict[str, Any] = {}
    outcome = ""
    state: dict[str, Any] = {"pending": [], "miss": None, "bridge": None}
    script_seconds = 0.0
    for _attempt in range(QUICKJS_NATIVE_REPLAY_LIMIT):
        state = {"pending": [], "miss": None, "bridge": None}
        started = time.monotonic()
        outcome = _evaluate_js_quickjs_attempt(code, input_text, runtime, extra_vars, json_obj, native_results, state)
        script_seconds += time.monotonic() - started
        miss = state["miss"]
        bridge = state["bridge"]
        if not miss or bridge is None or script_seconds > QUICKJS_NATIVE_REPLAY_SCRIPT_SECONDS:
            break
        function = bridge.natives.get(str(miss.get("name") or ""))
        if function is None:
            break
        try:
            args = miss.get("args")
            value = function(*(args if isinstance(args, list) else []))
            native_results[str(miss["key"])] = {"v": json.loads(json.dumps(value, ensure_ascii=False, default=str))}
        except Exception as error:
            native_results[str(miss["key"])] = {"e": str(error) or type(error).__name__}
    for effect in state["pending"]:
        effect()
    return outcome


def _js_tail_is_control_flow(code: str) -> bool:
    """R158 (B): True when `code` ends in a block (`}`) and has a top-level `if` / `for` / `while` / `switch` / `try` statement - the only
    statements whose ES5 completion value (Rhino keeps the last executed expression statement of a branch that ran) differs from ES2015+
    (`undefined`). Strings, templates, comments and regex literals are not scanned."""
    text = code.rstrip()
    if not text.endswith("}"):
        return False
    mask = _js_code_mask(text)
    count = len(text)
    depth = 0
    found = False
    index = 0
    while index < count:
        if not mask[index]:
            index += 1
            continue
        char = text[index]
        if char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
        elif depth == 0 and (char.isalpha() or char in "_$") and (index == 0 or not (_js_identifier_part(text[index - 1]) or text[index - 1] == ".")):
            end = index
            while end < count and _js_identifier_part(text[end]):
                end += 1
            if text[index:end] in ("if", "for", "while", "switch", "try"):
                found = True
            index = end
            continue
        index += 1
    return found and depth == 0


def _is_global_redeclaration_error(message: str) -> bool:
    """R158: V8 (`Identifier 'd' has already been declared`) and QuickJS (`redeclaration of 'd'`) reject a global `var` next to a global `let`."""
    return bool(re.search(r"has already been declared|redeclaration of", message or ""))


def _evaluate_js_quickjs_attempt(
    code: str,
    input_text: str,
    runtime: RuleRuntime,
    extra_vars: dict[str, Any] | None,
    json_obj: Any,
    native_results: dict[str, Any],
    state: dict[str, Any],
) -> str:
    pending: list[Callable[[], None]] = state["pending"]
    js_ctx = None

    def defer_script_error(message: str) -> None:
        pending.append(lambda: record_script_error(runtime, message, code))

    try:
        raw_ctx = make_quickjs_context()
        if raw_ctx is None:
            return evaluate_js_with_node(code, input_text, runtime, extra_vars=extra_vars)
        ctx = _QuickJSNativeBridge(raw_ctx, native_results)
        js_ctx = ctx
        state["bridge"] = ctx
        ctx.set("result", input_text)
        ctx.set("src", input_text)
        ctx.set("input", input_text)
        if isinstance(json_obj, (dict, list)):
            try:
                ctx.eval("result = JSON.parse(%s)" % json.dumps(json.dumps(json_obj, ensure_ascii=False)))
                ctx.eval(
                    "Object.defineProperty(result, 'toString', { value: function() { return %s; }, configurable: true });"
                    % json.dumps(input_text, ensure_ascii=False)
                )
                ctx.eval(
                    """
                    (function(value, raw) {
                      var methods = [
                        'search', 'match', 'replace', 'indexOf', 'lastIndexOf',
                        'includes', 'startsWith', 'endsWith', 'substring', 'substr',
                        'slice', 'split', 'charAt', 'charCodeAt', 'trim',
                        'toLowerCase', 'toUpperCase'
                      ];
                      methods.forEach(function(name) {
                        if (typeof value[name] !== 'undefined' || typeof String.prototype[name] !== 'function') return;
                        Object.defineProperty(value, name, {
                          value: function() { return String.prototype[name].apply(raw, arguments); },
                          configurable: true
                        });
                      });
                    })(result, %s);
                    """
                    % json.dumps(input_text, ensure_ascii=False)
                )
            except Exception:
                pass
        ctx.set("baseUrl", runtime.base_url)
        ctx.set("sourceUrl", runtime.source_url)
        ctx.set("bookSourceUrl", runtime.book_source_url)
        ctx.set("ruleUrl", runtime.rule_url or runtime.base_url)
        ctx.set("ruleRequestMethod", runtime.rule_request_method)
        ctx.set("ruleRequestBody", runtime.rule_request_body)
        ctx.set("cookie", runtime.cookie)
        ctx.set("previousResult", runtime.previous_result)
        ctx.set("bookName", runtime.book_name)
        ctx.set("bookAuthor", runtime.book_author)
        ctx.set("bookIntro", runtime.book_intro)
        ctx.set("bookKind", runtime.book_kind)
        ctx.set("bookCoverUrl", runtime.book_cover_url)
        ctx.set("bookLastChapter", runtime.book_last_chapter)
        ctx.set("bookUrl", runtime.book_url)
        ctx.set("bookTocUrl", runtime.book_toc_url)
        ctx.set("bookOrigin", runtime.book_origin)
        ctx.set("bookOriginName", runtime.book_origin_name)
        ctx.set("bookType", runtime.book_type)
        ctx.set("bookDurChapterIndex", runtime.book_dur_chapter_index)
        ctx.set("bookDurChapterTitle", runtime.book_dur_chapter_title)
        ctx.set("bookTotalChapterNum", runtime.book_total_chapter_num)
        ctx.set("bookCanUpdate", runtime.book_can_update)
        ctx.set("bookCustomIntro", runtime.book_custom_intro)
        ctx.set("bookVariable", runtime.book_variable)
        ctx.set("bookUseReplaceRule", runtime.book_use_replace_rule)
        ctx.set("bookReverseToc", runtime.book_reverse_toc)
        ctx.set("bookImageStyle", runtime.book_image_style)
        ctx.set("bookOrder", runtime.book_order)
        ctx.set("chapterUrl", runtime.chapter_url)
        ctx.set("chapterTitle", runtime.chapter_title)
        ctx.set("chapterTag", runtime.chapter_tag)
        ctx.set("chapterIsVolume", runtime.chapter_is_volume)
        ctx.set("chapterIsVip", runtime.chapter_is_vip)
        ctx.set("chapterIsPay", runtime.chapter_is_pay)
        ctx.set("chapterIndex", runtime.chapter_index)
        ctx.set("chapterCount", runtime.chapter_count)
        ctx.set("chapterVariable", runtime.chapter_variable)
        ctx.set("chapterActive", runtime.chapter_active)
        ctx.set("sourceBookSourceName", runtime.source_book_source_name)
        ctx.set("sourceBookSourceComment", runtime.source_book_source_comment)
        ctx.set("sourceVariableComment", runtime.source_variable_comment)
        ctx.set("sourceBookSourceType", runtime.source_book_source_type)
        ctx.set("sourceLastUpdateTime", runtime.source_last_update_time)
        ctx.set("sourceHeader", runtime.source_header)
        ctx.set("sourceLoginUrl", runtime.source_login_url)
        ctx.set("sourceLoginUi", runtime.source_login_ui)
        ctx.set("sourceEnabledCookieJar", runtime.source_enabled_cookie_jar)
        ctx.set("sourceBookSourceGroup", runtime.source_book_source_group)
        ctx.set("sourceExploreUrl", runtime.source_explore_url)
        ctx.set("sourceSearchUrl", runtime.source_search_url)
        ctx.set("sourceJSLib", runtime.js_lib or "")
        ctx.set("sourceLoginHeader", runtime.source_login_header)
        ctx.set("sourceLoginInfo", runtime.source_login_info)
        ctx.set("sourceConcurrentRate", runtime.source_concurrent_rate)
        for key, value in (extra_vars or {}).items():
            try:
                if isinstance(value, (dict, list)):
                    ctx.eval(
                        "globalThis[%s] = JSON.parse(%s);"
                        % (
                            json.dumps(str(key), ensure_ascii=False),
                            json.dumps(json.dumps(value, ensure_ascii=False), ensure_ascii=False),
                        )
                    )
                else:
                    ctx.set(key, value)
            except Exception:
                pass
        # R155 (G): configurable globalThis properties instead of `var` (non-configurable): Rhino lets a source script
        # declare its own `let headers = {...}`; a QuickJS top-level `var headers` made that a redeclaration SyntaxError
        # (JSEngine.swift:2348 installs them with setObject, also configurable).
        ctx.eval(f"globalThis.headers = {json.dumps(runtime.headers, ensure_ascii=False)};")
        ctx.eval(f"globalThis.variables = {json.dumps(runtime.variables or {}, ensure_ascii=False)};")
        try:
            ctx.eval(
                """
                (function(){
                    var __readoriJSONParse = JSON.parse;
                    JSON.parse = function(value, reviver) {
                        if (value && typeof value === 'object') return value;
                        return __readoriJSONParse.call(JSON, value, reviver);
                    };
                })();
                """
            )
        except Exception:
            pass
        try:
            ctx.add_callable(
                "__readoriSelectElementsJSON",
                lambda selector, html: select_elements_json(str(selector or ""), str(html or ""), runtime.base_url),
            )
        except Exception:
            pass
        try:
            # R155 (L): java.getElements / java.getElement take Legado rules (AnalyzeRule.getElements), not only CSS.
            ctx.add_callable(
                "__readoriSelectElementsByRuleJSON",
                lambda rule, html: select_elements_by_rule_json(str(rule or ""), str(html or ""), runtime),
            )
        except Exception:
            pass

        # 注入 java/source/cookie/cache/book/chapter 对象 API
        try:
            ctx.add_callable(
                "__readoriEvaluateString",
                lambda rule, html, unescape=True: legado_get_string(
                    str(rule or ""), str(html or ""), runtime, "json" if is_json_content(str(html or "")) else "html",
                    unescape=bool(unescape),
                ),
            )
        except Exception:
            pass
        try:
            ctx.add_callable(
                "__readoriEvaluateList",
                lambda rule, html: json.dumps(
                    evaluate_js_string_list(str(rule or ""), str(html or ""), runtime),
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
        if app_profiles.current_app_profile().js_get_element_json_whole_value:
            try:
                ctx.add_callable(
                    "__readoriEvaluateWholeJson",
                    lambda rule, html: evaluate_json_whole_value(str(rule or ""), str(html or "")),
                )
            except Exception:
                pass
        try:
            ctx.add_callable(
                "__readoriPercentEncodeNative",
                lambda value, charset: encode_form_keyword(str(value or ""), str(charset or "")),
            )
        except Exception:
            pass
        try:
            def _encode_bytes(value: str, charset: str) -> str:
                normalized = _encoding_from_charset(str(charset or "")) or "utf-8"
                return json.dumps(list(str(value or "").encode(normalized)), separators=(",", ":"))

            def _decode_bytes(value: str, charset: str) -> str:
                normalized = _encoding_from_charset(str(charset or "")) or "utf-8"
                try:
                    raw = json.loads(str(value or "[]"))
                except Exception:
                    raw = []
                return bytes((int(item) & 255 for item in raw)).decode(normalized, errors="replace")

            ctx.add_callable("__readoriEncodeBytesNative", _encode_bytes)
            ctx.add_callable("__readoriDecodeBytesNative", _decode_bytes)
        except Exception:
            pass
        try:
            def _to_url_json(url: str, explicit_base: str) -> str:
                raw_url = str(url or "")
                raw_base = str(explicit_base or "")
                target = urljoin(raw_base, raw_url) if raw_base else raw_url
                parsed = urlparse(target)
                if not parsed.scheme:
                    return "{}"
                host = parsed.hostname or ""
                origin = f"{parsed.scheme}://{host}"
                try:
                    if parsed.port is not None:
                        origin += f":{parsed.port}"
                except ValueError:
                    return "{}"
                params: dict[str, str] = {}
                if parsed.query:
                    for part in parsed.query.split("&"):
                        if "=" not in part:
                            continue
                        key, value = part.split("=", 1)
                        params[key] = unquote(value.replace("+", " "))
                document: dict[str, Any] = {
                    "host": host,
                    "origin": origin,
                    "pathname": parsed.path,
                    "searchParams": params if "?" in target.split("#", 1)[0] else None,
                }
                return json.dumps(document, ensure_ascii=False)

            ctx.add_callable("__readoriToURLJson", _to_url_json)
        except Exception:
            pass
        try:
            def _ajax(url: str) -> str:
                target = urljoin(runtime.base_url, str(url or ""))
                text, _, _ = fetch_text(requests.Session(), target, headers=runtime.headers)
                return text
            ctx.add_callable("__readoriAjax", _ajax)
        except Exception:
            pass
        try:
            def _ajax_post(url: str, body: str, request_headers: str) -> str:
                target = urljoin(runtime.base_url, str(url or ""))
                headers = dict(runtime.headers or {})
                try:
                    parsed_headers = json.loads(str(request_headers or "{}"))
                    if isinstance(parsed_headers, dict):
                        headers.update({str(k): str(v) for k, v in parsed_headers.items() if v is not None})
                except Exception:
                    pass
                text, _, _ = fetch_text(requests.Session(), target, method="POST", headers=headers, body=str(body or ""))
                return text
            ctx.add_callable("__readoriAjaxPost", _ajax_post)
        except Exception:
            pass
        try:
            def _http_response(
                url: str,
                method: str,
                body: str,
                request_headers: str,
                follow_redirects: bool,
            ) -> str:
                target = urljoin(runtime.base_url, str(url or ""))
                try:
                    parsed_headers = json.loads(str(request_headers or "{}"))
                except Exception:
                    parsed_headers = {}
                headers = dict(runtime.headers)
                if isinstance(parsed_headers, dict):
                    headers.update({
                        str(key): str(value)
                        for key, value in parsed_headers.items()
                    })
                response_meta: dict[str, Any] = {}
                text, final_url, status = fetch_text(
                    requests.Session(),
                    target,
                    method=str(method or "GET"),
                    headers=headers,
                    body=str(body or ""),
                    allow_redirects=bool(follow_redirects),
                    response_meta=response_meta,
                )
                return json.dumps(
                    {
                        "body": text,
                        "url": final_url,
                        "status": status,
                        "headers": response_meta.get("headers", {}),
                        "cookies": response_meta.get("cookies", {}),
                    },
                    ensure_ascii=False,
                )

            ctx.add_callable("__readoriHttpResponse", _http_response)
        except Exception:
            pass
        try:
            def _ajax_bytes_base64(url: str) -> str:
                target = urljoin(runtime.base_url, str(url or ""))
                data, _, _ = fetch_bytes(requests.Session(), target, headers=runtime.headers)
                return base64.b64encode(data).decode("ascii")
            ctx.add_callable("__readoriAjaxBytesBase64", _ajax_bytes_base64)
        except Exception:
            pass
        try:
            def _gunzip_bytes_base64(encoded: str) -> str:
                raw = base64.b64decode(str(encoded or ""), validate=False)
                try:
                    decoded = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
                except Exception:
                    decoded = raw
                return base64.b64encode(decoded).decode("ascii")
            ctx.add_callable("__readoriGunzipBytesBase64", _gunzip_bytes_base64)
        except Exception:
            pass
        try:
            ctx.add_callable("__readoriRarEntryNative", rar_entry_json)
            ctx.add_callable("__readoriRarEntriesNative", rar_entries_json)
        except Exception:
            pass
        try:
            ctx.add_callable("__readoriSevenZipEntryNative", seven_zip_entry_json)
            ctx.add_callable("__readoriSevenZipEntriesNative", seven_zip_entries_json)
        except Exception:
            pass
        try:
            def _head_header(url: str, key: str) -> str:
                target = urljoin(runtime.base_url, str(url or ""))
                try:
                    request_timeout = remaining_source_validation_seconds(REQUEST_TIMEOUT)
                    response = requests.head(
                        target,
                        headers=runtime.headers,
                        timeout=(min(REQUEST_CONNECT_TIMEOUT, request_timeout), request_timeout),
                        allow_redirects=False,
                    )
                    value = response.headers.get(str(key or ""), "")
                    if str(key or "").lower() == "location" and value:
                        return urljoin(target, value)
                    return value
                except Exception:
                    return ""
            ctx.add_callable("__readoriHeadHeader", _head_header)
        except Exception:
            pass

        ctx.set("__readoriBookIsNull", runtime_binds_book_as_null(runtime))
        install_quickjs_app_globals(ctx, runtime)
        java_api = _build_java_api_js(runtime)
        ctx.eval(java_api)
        app_js_plan = app_profiles.app_js_setup_plan(runtime.js_lib or "", code)
        install_quickjs_app_cryptojs(ctx, app_js_plan, "jslib")

        def sync_quickjs_variables() -> None:
            # Read the script's variables now, apply them to the runtime only if this attempt is the final one.
            try:
                serialized = ctx.eval("JSON.stringify(variables || {})")
                values = json.loads(str(serialized or "{}"))
            except Exception:
                return
            if not isinstance(values, dict):
                return

            def apply_variables() -> None:
                runtime.variables = dict(runtime.variables or {})
                for key, value in values.items():
                    if key:
                        runtime.variables[str(key)] = "" if value is None else str(value)
                sync_runtime_book_state_from_variables(runtime)
                sync_runtime_chapter_state_from_variables(runtime)

            pending.append(apply_variables)

        # 注入 jsLib（书源共享 JS 库）
        if runtime.js_lib:
            try:
                ctx.eval(_js_lib_for_engine(runtime))
            except Exception:
                pass
        install_quickjs_app_cryptojs(ctx, app_js_plan, "code")

        direct_error = ""
        direct_empty_completion = False  # R158d: the script's completion value is a DEFINED "" (not undefined / null)
        try:
            ret = ctx.eval(code)
            text = quickjs_value_text(ret)
            if text and text != "undefined":
                sync_quickjs_variables()
                return text
            direct_empty_completion = isinstance(ret, str) and ret == ""
        except Exception as error:
            direct_error = _quickjs_error_message(error)
            if _is_global_redeclaration_error(direct_error):
                # R158 (D1, gongzicp): the jsLib declares `let d;` at top level and the rule script `var d = ...`. Rhino and the App accept it
                # (JSEngine.hoistTopLevelLetConstToVar rewrites the top-level let / const to var and each script runs in its own function
                # scope, `scopedEvaluationProgram`); a QuickJS global script cannot. Re-run as a direct eval inside a function: var /
                # function declarations become locals, the completion value is kept.
                try:
                    ret = ctx.eval("(function(){ return eval(" + json.dumps(code) + "); })()")
                    direct_error = ""
                    text = quickjs_value_text(ret)
                    if text and text != "undefined":
                        sync_quickjs_variables()
                        return text
                    direct_empty_completion = isinstance(ret, str) and ret == ""
                except Exception as error2:
                    direct_error = _quickjs_error_message(error2)

        script_threw = bool(direct_error) and not _is_top_level_return_error(direct_error)
        try:
            wrapped = f"(function(){{ {code} }})()"
            ret = ctx.eval(wrapped)
            text = quickjs_value_text(ret)
            if direct_error and not _is_top_level_return_error(direct_error):
                defer_script_error(direct_error)
            if text and text != "undefined":
                sync_quickjs_variables()
                return text
        except Exception as error:
            script_threw = True
            if direct_error and not _is_top_level_return_error(direct_error):
                defer_script_error(direct_error)
            elif direct_error:
                defer_script_error(_quickjs_error_message(error))

        try:
            fallback = ctx.eval("String(result)")
            fallback_text = "" if fallback is None else str(fallback)
            sync_quickjs_variables()
            # R158 (B, both profiles): Rhino keeps the value of the last EXECUTED expression statement when a script ends in an `if` / `for` /
            # `switch` / `try` whose branch is not taken (ES5 completion value, ndlib `if(...){result=result}else{...}` + a false `if`), and
            # ES2015+ engines give `undefined`. The App falls back to the global `result` binding for an empty script value (JSEngine.swift
            # evaluate() `result.isEmpty, fallback = result`; evaluateToArray `s.isEmpty` fallbacks) and keeps the `fallback != input` guard only for
            # a script that THREW (the `hadException` branches). The validator mirrors the unguarded branch ONLY where the ES5 completion value
            # explains an unchanged `result` (a control-flow tail) - a script that merely returned "" (`{{java.get("k")}}`) still yields "".
            # R158d (jjwxc explore junk row, 1.1.2 `js_empty_completion_keeps_empty`): a DEFINED "" completion (`if(c){java.getString(..)}` that found
            # nothing) is "" - Legado / Rhino - and must not turn the whole list item into the field value. 1.1.1 keeps the App's `result.isEmpty` fallback.
            empty_completion_is_final = direct_empty_completion and app_profiles.current_app_profile().js_empty_completion_keeps_empty
            if not script_threw and fallback_text and fallback_text == input_text and _js_tail_is_control_flow(code) and not empty_completion_is_final:
                return fallback_text
            return fallback_text if fallback_text and fallback_text != input_text else ""
        except Exception:
            if js_ctx.has_native_miss():
                return ""
            return evaluate_js_with_node(code, input_text, runtime, extra_vars=extra_vars)
    except Exception:
        if js_ctx is not None and js_ctx.has_native_miss():
            return ""
        return evaluate_js_with_node(code, input_text, runtime, extra_vars=extra_vars)
    finally:
        if js_ctx is not None:
            state["miss"] = js_ctx.first_native_miss()
            pending.extend(js_ctx.interaction_effects(runtime))
            pending.extend(js_ctx.debug_log_effects())


def apply_login_check_js(
    src: dict[str, Any],
    runtime: RuleRuntime,
    body: str,
    final_url: str,
    rule_url: str,
    status: int = 200,
    request_method: str = "GET",
    request_body: str = "",
    request_headers: dict[str, str] | None = None,
    response_headers: dict[str, str] | None = None,
    response_cookies: dict[str, str] | None = None,
) -> tuple[bool, str, str]:
    """Apply BaseSource.loginCheckJs with Legado StrResponse semantics."""
    script = str(src.get("loginCheckJs") or "").strip()
    if not script:
        return True, body, final_url

    check_runtime = RuleRuntime(**{
        **runtime.__dict__,
        "rule_url": rule_url,
        "rule_request_method": request_method,
        "rule_request_body": request_body,
        "headers": dict(runtime.headers if request_headers is None else request_headers),
        "source_url": final_url,
        "base_url": final_url,
    })
    wrapper = """
result = __readoriResponseObject(
    input,
    %s,
    %d,
    %s,
    %s
);
var __readoriCheckedResponse = (function() {
    var evaluated = eval(%s);
    return (evaluated === undefined || evaluated === null) ? result : evaluated;
})();
if (__readoriCheckedResponse !== undefined && __readoriCheckedResponse !== null) {
    result = __readoriCheckedResponse;
}
(function() {
    var valid = result && typeof result.body === 'function'
        && typeof result.url === 'function';
    return JSON.stringify({
        valid: !!valid,
        body: valid ? String(result.body() || '') : '',
        url: valid ? String(result.url() || '') : ''
    });
})()
""" % (
        json.dumps(final_url, ensure_ascii=False),
        int(status or (200 if body else 0)),
        json.dumps(response_headers or {}, ensure_ascii=False),
        json.dumps(response_cookies or {}, ensure_ascii=False),
        json.dumps(script, ensure_ascii=False),
    )
    encoded = evaluate_js(wrapper, body, check_runtime)
    try:
        document = json.loads(encoded)
    except Exception as _swallowed:
        note_debug_error("loginCheckJs result", _swallowed)
        return False, body, final_url
    if not isinstance(document, dict) or document.get("valid") is not True:
        return False, body, final_url
    checked_body = str(document.get("body") or "")
    checked_url = str(document.get("url") or final_url)
    return True, checked_body, checked_url


def execute_js_block_if_needed(text: str, runtime: RuleRuntime, extra_vars: dict[str, Any] | None = None, session: requests.Session | None = None) -> str:
    stripped = text.strip()
    if not stripped:
        return text
    if stripped.lower().startswith("@js:"):
        fast = try_evaluate_ajax_token_search_js(stripped[4:], runtime, extra_vars, session=session)
        if fast is not None:
            return fast
        return evaluate_js(stripped[4:], "", runtime, extra_vars=extra_vars).strip()
    if stripped.startswith("<js>") and stripped.endswith("</js>"):
        fast = try_evaluate_ajax_token_search_js(stripped[4:-5], runtime, extra_vars, session=session)
        if fast is not None:
            return fast
        return evaluate_js(stripped[4:-5], "", runtime, extra_vars=extra_vars).strip()
    return text


def try_evaluate_ajax_token_search_js(script: str, runtime: RuleRuntime, extra_vars: dict[str, Any] | None = None, session: requests.Session | None = None) -> str | None:
    compact = re.sub(r"\s+", "", script or "")
    if "java.ajax(source.getKey())" not in compact:
        return None
    if "input[name=_token]" not in compact and 'input[name="_token"]' not in compact and "input[name='_token']" not in compact:
        return None
    if "_token=${token}" not in compact or "kw=${key}" not in compact:
        return None
    source_url = runtime.book_source_url or runtime.base_url or runtime.source_url
    if not source_url:
        return None
    active_session = session or requests.Session()
    html, final_url, status = fetch_text(active_session, source_url, headers=runtime.headers)
    if status == 0 or not html:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
        token = str((soup.select_one('input[name="_token"]') or {}).get("value") or "").strip()
    except Exception:
        token = ""
    if not token:
        return None
    key = str((extra_vars or {}).get("key") or (extra_vars or {}).get("keyword") or "")
    url_match = re.search(r"""['"]([^'"]*,)\s*['"]\s*\+\s*JSON\.stringify""", script)
    url_part = url_match.group(1) if url_match else "/search,"
    if final_url and url_part.endswith(","):
        url_part = urljoin(final_url, url_part[:-1]) + ","
    body = f"_token={token}&kw={key}"
    return url_part + json.dumps({"body": body, "method": "POST"}, ensure_ascii=False)


def execute_embedded_js_blocks_if_needed(text: str, runtime: RuleRuntime) -> str:
    if "<js>" in text and "</js>" in text:
        pattern = re.compile(r"<js>\s*([\s\S]*?)\s*</js>", re.I)

        def repl(match: re.Match[str]) -> str:
            return evaluate_js(match.group(1), "", runtime).strip()

        return pattern.sub(repl, text)
    if "%3cjs%3e" in text.lower() and "%3c%2fjs%3e" in text.lower():
        pattern = re.compile(r"%3[cC]js%3[eE]([\s\S]*?)%3[cC]%2[fF]js%3[eE]", re.I)

        def repl(match: re.Match[str]) -> str:
            code = requests.utils.unquote(match.group(1))
            return evaluate_js(code, "", runtime).strip()

        return pattern.sub(repl, text)
    return text


def parse_url_options(raw: str) -> tuple[str, dict[str, Any]]:
    text = raw.strip()
    matches = list(re.finditer(r",\s*(?=\{)", text))
    if not matches:
        return text, {}
    match = matches[-1]
    idx = match.start()
    if idx <= 0:
        return text, {}
    url_part = text[:idx].strip()
    options_part = text[match.end() :].strip()
    opts = parse_json_dict(leading_json_object_prefix(options_part))
    if opts is None:
        return url_part, {}
    return url_part, normalize_url_options(opts)


def _lenient_header_pairs(text: str) -> dict[str, Any]:
    """R156 (searchjs T): a `headers` option given as a loose string (`{Content-Type: application/x-www-form-urlencoded}`,
    unquoted keys and values) read as `Key: value` pairs; anything unreadable is an empty header set - never an exception."""
    inner = (text or "").strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    pairs: dict[str, Any] = {}
    for chunk in re.split(r",\s*(?=[\"']?[\w-]+[\"']?\s*:)|\n", inner):
        parsed = parse_header_line(chunk)
        if parsed:
            pairs[parsed[0]] = parsed[1]
    return pairs


def normalize_url_options(opts: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(opts)
    headers = normalized.get("headers")
    if isinstance(headers, str):
        headers = parse_json_dict(headers)
        if headers is None:
            headers = _lenient_header_pairs(str(normalized.get("headers") or ""))
    if isinstance(headers, dict):
        normalized_headers: dict[str, str] = {}
        for key, value in headers.items():
            if value is None:
                continue
            if isinstance(value, bool):
                normalized_headers[str(key)] = "true" if value else "false"
            elif isinstance(value, (str, int, float)):
                normalized_headers[str(key)] = str(value)
        normalized["headers"] = normalized_headers
    elif headers is not None:
        normalized["headers"] = {}

    body = normalized.get("body")
    if isinstance(body, (dict, list)):
        normalized["body"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        headers = normalized.get("headers")
        headers_dict = dict(headers) if isinstance(headers, dict) else {}
        if not any(str(k).lower() == "content-type" for k in headers_dict):
            headers_dict["Content-Type"] = "application/json"
        normalized["headers"] = headers_dict
    elif body is not None and not isinstance(body, str):
        normalized["body"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))

    if "webView" in normalized:
        web_view = normalized.get("webView")
        normalized["webView"] = not (
            web_view is None
            or web_view == ""
            or web_view is False
            or web_view == "false"
        )
    if isinstance(normalized.get("method"), str):
        normalized["method"] = normalized["method"].upper()
    return normalized


def decode_data_url_as_hex_response(raw_url: str) -> tuple[str, str, str] | None:
    """Execute Legado's typed `data:` response path without network I/O.

    Stateful aggregation sources encode search/detail/catalog/content state as
    `data:;base64,<payload>,{"type":"..."}`. `AnalyzeUrl.getStrResponseAwait`
    routes every non-null `type` through `getByteArrayAwait()` and returns the
    bytes as lowercase hexadecimal. OkHttp cannot represent the `data:` URI as
    a request URL, so Legado's synthetic `StrResponse` reports localhost.
    """
    url_part, options = parse_url_options(raw_url or "")
    if not url_part.lower().startswith("data:"):
        return None
    response_type = str(options.get("type") or "").strip()
    if not response_type:
        return None

    data_body = url_part[len("data:") :]
    if "," not in data_body:
        candidate = data_body.strip()
        if candidate and len(candidate) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", candidate):
            return candidate.lower(), "http://localhost/", response_type
        return "", "http://localhost/", response_type

    metadata, payload = data_body.split(",", 1)
    try:
        if ";base64" in metadata.lower():
            padded = payload + ("=" * (-len(payload) % 4))
            decoded = base64.b64decode(padded, validate=False)
        else:
            decoded = unquote_to_bytes(payload)
    except Exception:
        decoded = b""
    return decoded.hex(), "http://localhost/", response_type


def resolve_url_with_options(raw_url: str, base_url: str, headers: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    url_part, opts = parse_url_options((raw_url or "").strip())
    # R157 (tocContent K): detail / toc / content requests share Legado AnalyzeUrl.analyzeUrl (:265-268): a POST form body
    # without an explicit Content-Type (source header included) that is not JSON / XML is encoded field by field
    # (encodeParams :287-343; App BookSourceEngine shouldEncodeLegadoFormBody). Search / explore did this in
    # build_url_with_options; these paths sent the body raw, so a base64 `secret_content=` with `+ / =` arrived mangled.
    # `headers` is the runtime header set; callers that omit it keep the raw body.
    if headers is not None and str(opts.get("method") or "").upper() == "POST" and isinstance(opts.get("body"), str):
        merged_headers = dict(headers)
        if isinstance(opts.get("headers"), dict):
            merged_headers.update(opts["headers"])
        if not _has_explicit_content_type(merged_headers) and not _is_json_or_xml_request_body(opts["body"]):
            opts = dict(opts)
            opts["body"] = encode_legado_form_body(
                opts["body"], opts.get("charset") if isinstance(opts.get("charset"), str) else None
            )
    url_part = collapse_concatenated_absolute_url(url_part)
    if not url_part:
        return "", opts
    if url_part.lower().startswith(("http://", "https://", "@js:", "<js>")):
        return url_part, opts
    return urljoin(base_url, url_part), opts


def request_url_preserving_typed_data_options(url: str, opts: dict[str, Any]) -> str:
    """Keep AnalyzeUrl's synthetic response type attached to data carriers."""

    if (url or "").strip().lower().startswith("data:"):
        return compose_url_with_options(url, opts)
    return url


def sniff_source_regex_response_body(html: str, final_url: str, source_regex: str) -> str:
    pattern = (source_regex or "").strip()
    if not pattern:
        return ""
    try:
        compiled = re.compile(pattern)
    except re.error:
        return ""
    for resource_url in source_regex_resource_urls(html, final_url):
        if compiled.fullmatch(resource_url):
            return resource_url
    return ""


def source_regex_resource_urls(html: str, base_url: str) -> list[str]:
    if not html:
        return []
    patterns = [
        r"""(?:src|href|data-src|data-href|data-original|data-url|data-link|poster)\s*=\s*["']?([^"'\s>]+)""",
        r"""url\(\s*["']?([^"')\s]+)""",
        r"""(?:https?:)?//[^\s"'<>)]+"?""",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.I):
            raw = match.group(1) if match.lastindex else match.group(0)
            resolved = normalize_source_regex_resource_url(raw, base_url)
            if resolved and resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
    return out


def normalize_source_regex_resource_url(raw: str, base_url: str) -> str:
    text = html_unescape(str(raw or "")).replace("\\/", "/").strip().strip('"\'')
    text = re.sub(r"[,;.]+$", "", text)
    if not text:
        return ""
    low = text.lower()
    if low.startswith(("data:", "javascript:", "mailto:", "#")):
        return ""
    if text.startswith("//"):
        scheme = urlparse(base_url).scheme or "https"
        text = f"{scheme}:{text}"
    return urljoin(base_url, text)


def is_plausible_source_url(url: str) -> bool:
    text = (url or "").strip()
    if not text or len(text) > 4096:
        return False
    url_part, _ = parse_url_options(text)
    decoded = unquote(url_part)
    low = decoded.lower()
    if "option@" in low or "@put" in low or "@get" in low:
        return False
    if "jieqi_pc_location" in low:
        return False
    if "{{" in low and "}}" in low:
        return False
    if "{}" in low:
        return False
    if re.search(r"%0a|%0d|[\r\n\t]", low):
        return False
    if any(marker in low for marker in ["<!doctype", "<html", "<head", "<body", "<script", "</", "window.__cf", "cloudflare"]):
        return False
    if low == "#" or low.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "blob:", "about:")):
        return False
    if low.startswith(("http://", "https://")):
        parsed = urlparse(url_part)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return False
        path = (parsed.path or "").lower()
        if any(marker in path for marker in ["/book//", "/books//", "/novel//", "/novels//", "/chapter//", "/chapters//", "/info//", "/detail//", "/undefined/"]):
            return False
        last = path.rstrip("/").rsplit("/", 1)[-1] if path.strip("/") else ""
        utility_exact = {"help", "help.html", "login", "login.php", "register", "register.php", "passport", "passport.php", "bookcase", "bookcase.php", "mybook", "mybook.php", "top", "top.html", "rank", "rank.html"}
        utility_fragments = ["/help/", "/login", "/register", "/passport", "/bookcase", "/mybook", "/bookshelf", "/rank/", "/top/", "/list/top", "/download/"]
        if last in utility_exact or any(fragment in path for fragment in utility_fragments):
            return False
        return True
    return True


def is_plausible_request_url(url: str) -> bool:
    text = (url or "").strip()
    if not text or len(text) > 4096:
        return False
    url_part, _ = parse_url_options(text)
    decoded = unquote(url_part)
    low = decoded.lower()
    if "option@" in low or "@put" in low or "@get" in low:
        return False
    if "jieqi_pc_location" in low:
        return False
    if "{{" in low and "}}" in low:
        return False
    if "{}" in low:
        return False
    if re.search(r"%0a|%0d|[\r\n\t]", low):
        return False
    if any(marker in low for marker in ["<!doctype", "<html", "<head", "<body", "<script", "</", "window.__cf", "cloudflare"]):
        return False
    if low == "#" or low.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "blob:", "about:")):
        return False
    if low.startswith(("http://", "https://")):
        parsed = urlparse(url_part)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return False
        path = (parsed.path or "").lower()
        if any(marker in path for marker in ["/book//", "/books//", "/novel//", "/novels//", "/chapter//", "/chapters//", "/info//", "/detail//", "/undefined/"]):
            return False
    return True


def is_static_asset_url(url: str) -> bool:
    text = (url or "").strip()
    if not text:
        return False
    url_part, _ = parse_url_options(text)
    path = urlparse(url_part).path.lower()
    return bool(re.search(r"\.(?:css|js|mjs|map|png|jpe?g|gif|webp|svg|ico|woff2?|ttf|otf|eot|apk|ipa)(?:$|[?#])", path))


def is_likely_toc_data_asset_url(url: str) -> bool:
    url_part, _ = parse_url_options(url or "")
    path = urlparse(url_part).path.lower()
    if not re.search(r"\.(?:js|json)(?:$|[?#])", path):
        return False
    return any(marker in path for marker in ["chapter_list", "chapterlist", "catalog", "toc", "directory"])


def is_unusable_toc_url_candidate(url: str, book_url: str = "") -> bool:
    text = (url or "").strip()
    if not text:
        return False
    url_part, _ = parse_url_options(text)
    path = urlparse(url_part).path.lower()
    if is_static_asset_url(text) and not is_likely_toc_data_asset_url(text):
        return True
    if "/mstackslistright/" in path:
        return True
    if "/user/" in path:
        return True
    if path.startswith("/kol-list"):
        return True
    last = path.rstrip("/").rsplit("/", 1)[-1] if path.strip("/") else ""
    if last in {"toplist", "toplist.html", "toplist.php", "sns_list", "app_list"}:
        return True
    if re.match(r"^/(?:quanben|complete|completed|full)(?:/list)?/?$", path):
        return True
    if re.match(r"^/(?:quanben|complete|completed|full)/\d+\.(?:html?|shtml)$", path):
        return True
    if re.match(r"^/list-\d+/?$", path):
        return True
    if re.match(r"^/lists?/\d+\.(?:html?|shtml)$", path):
        return True
    if path.endswith("/index/book_list") or path.endswith("/book_list"):
        return True
    if path.endswith("/scorelist.php") or path.endswith("/signauthorlist.php"):
        return True
    if book_url:
        book_path = (urlparse(parse_url_options(book_url)[0]).path or "").lower()
        if re.match(r"^/(?:list\d*|lists|booklist|alllist|sort|class|category|cat|top|rank|full|quanben|shuku|new|zuojia|writer|author|authorlist)(?:/|$|\.)", path):
            book_ids = set(re.findall(r"\d{2,}", book_path))
            candidate_ids = set(re.findall(r"\d{2,}", path))
            if not book_ids or not (book_ids & candidate_ids):
                return True
    return False


def is_usable_search_book_url(url: str) -> bool:
    text = (url or "").strip()
    if not is_plausible_source_url(text):
        return False
    if is_static_asset_url(text):
        return False
    if text.startswith(("{", "[")):
        return True
    url_part, _ = parse_url_options(text)
    parsed = urlparse(url_part)
    path = (parsed.path or "").lower()
    host = (parsed.netloc or "").lower()
    query = (parsed.query or "").lower()
    decoded_url = unquote(url_part).lower()
    scheme_prefix = f"{parsed.scheme.lower()}://" if parsed.scheme else ""
    if scheme_prefix and decoded_url.startswith(scheme_prefix):
        remainder = decoded_url[len(scheme_prefix):]
        if "http://" in remainder or "https://" in remainder:
            return False
    last = path.rstrip("/").rsplit("/", 1)[-1] if path.strip("/") else ""
    segments = [seg for seg in path.strip("/").split("/") if seg]
    if re.match(r"^/(?:book|books|novel|novels)/\d{2,}/\d{2,}\.(?:html?|shtml)$", path):
        return False
    if "@js:" in url_part.lower() or "%40js" in url_part.lower():
        return False
    if is_search_endpoint_url(url_part):
        return False
    social_hosts = {
        "twitter.com", "x.com", "facebook.com", "instagram.com", "t.me", "telegram.me", "discord.gg", "patreon.com", "microsoft.com", "go.microsoft.com", "theporndude.com", "cyberpolice.cn",
    }
    if host in social_hosts or any(host.endswith("." + social) for social in social_hosts):
        return False
    if host == "gov.cn" or host.endswith(".gov.cn") or host == "12377.cn" or host.endswith(".12377.cn"):
        return False
    if host == "baidu.com" or host.endswith(".baidu.com") or "movie.douban." in host:
        return False
    if host == "jjwxc.net" or host.endswith(".jjwxc.net"):
        if not (path.endswith("/onebook.php") or path.endswith("/androidapi/novelbasicinfo")):
            return False
    if "/film/" in path and re.search(r"/film/\d{4}/", path):
        return False
    if re.search(r"/(?:writer|author|zuojia)/\d+\.html?$", path):
        return False
    allowed_api_book_detail = (
        re.search(r"/api/(?:book|books)/?$", path) is not None
        and any(token in query for token in ["bookid=", "book_id=", "bookId=".lower(), "id=", "resourceid=", "resource_id=", "novelid=", "novel_id="])
    )
    if path.endswith(("/member.php", "/user.php")) and any(token in query for token in ["mod=register", "mod=logging", "mod=login", "do=login"]):
        return False
    if path.endswith("/forum.php"):
        if not query or "mod=forumdisplay" in query:
            return False
        if "mod=viewthread" not in query and "tid=" not in query:
            return False
    if path.endswith("/misc.php") and "mod=ranklist" in query:
        return False
    if path.endswith("/home.php") and "do=favorite" in query:
        return False
    if path.endswith("/newmessage.php") and ("tosys=" in query or "title=" in query or "content=" in query):
        return False
    if path.endswith("/message.php") and ("box=inbox" in query or "box=outbox" in query or "action=pm" in query):
        return False
    if path.endswith("/wap.php") and any(token in query for token in ["action=top", "action=search"]):
        return False
    if path.startswith("/channel/"):
        return False
    if path.startswith("/chapter/read/"):
        return False
    if path == "/e/doinfo/" or path.startswith("/e/doinfo/") or path.startswith("/e/member/") or path.startswith("/e/web/"):
        return False
    if path.startswith("/search.php/"):
        return False
    if path.startswith("/mcsearch/"):
        return False
    if path.startswith("/mctype/"):
        return False
    allowed_author_book_detail = path.startswith("/author/xiangqing") and "bookid=" in query
    if re.match(r"^/list/\d+\.(?:html?|shtml)$", path):
        return False
    if re.match(r"^/list[-_]\d+/\d+\.(?:html?|shtml)$", path):
        return False
    if re.match(r"^/all/\d+(?:\.\d+)?\.(?:html?|shtml)$", path):
        return False
    if re.match(r"^/sort\d*/\d+\.(?:html?|shtml)$", path):
        return False
    if re.search(r"/(?:allvisit|monthvisit|weekvisit|dayvisit|allvote|monthvote|weekvote|dayvote|postdate|goodnum|goodvote|size|lastupdate|over|full)-\d+\.(?:html?|shtml)$", path):
        return False
    if re.search(r"/ph/(?:allvisit|monthvisit|weekvisit|dayvisit|allvote|monthvote|weekvote|dayvote|postdate|goodnum|goodvote|size|lastupdate|over|full)_\d+\.(?:html?|shtml)$", path):
        return False
    if re.match(r"^/top_(?:allvisit|monthvisit|weekvisit|dayvisit|allvote|monthvote|weekvote|dayvote|postdate|goodnum|goodvote|size|lastupdate|over|full)/?$", path):
        return False
    if re.match(r"^/a\d+/b\d+/c\d+/d\d+/?$", path):
        return False
    utility_exact = {
        "", "m", "bookcase", "bookcase.php", "mybook", "mybook.php", "bookshelf", "bookshelf.php", "shelf", "shelf.html", "shelf.php",
        "login", "login.php", "register", "register.php", "passport", "passport.php",
        "help", "help.html", "about", "about.html", "contact", "contact.html", "support", "support.html", "advertise", "advertise.html", "newmessage.php", "home", "home.html", "menu", "menu.html", "activity", "copyright", "privacy", "terms", "policy", "mark", "mark.html", "search", "search.php",
        "search.htm", "search.html", "s", "s.php", "so", "so.php",
        "top", "top.html", "top.php", "toplist", "toplist.html", "rank", "rank.html", "hot", "hot.html", "hot.php", "topic", "status",
        "category", "category.html", "class", "sort", "shuku", "history", "history.html", "myhistory", "myhistory.html", "allbook", "banquan", "authorwelfare", "shortart", "zs.html", "update", "update.html", "free", "meinv", "library",
        "download", "download.html", "over", "over.html", "shortcut.php", "news", "news.html",
        "picture", "pictures", "movie", "movies", "article", "article.html", "cate", "flash", "photo", "fav",
        "sitemap", "sitemap.xml", "sitemap.html", "sitemap.aspx", "robots.txt",
        "book", "books", "novel", "novels", "info", "detail", "cat", "cart", "cart.html", "shoppingcart", "shop", "shop.html", "store", "store.html", "taoge", "genres", "url", "url_list",
        "list", "list.html", "zzlist", "zzlist.html", "sns_list", "app_list", "thread_index", "administrationrecords", "all", "all.html", "writer", "writer.html", "new", "new.html", "endnovel", "wanben", "quanben", "forum",
    }
    if last in utility_exact and not allowed_api_book_detail:
        return False
    if re.fullmatch(r"search\d*\.(?:html?|shtml)", last):
        return False
    utility_fragments = [
        "/bookcase", "/mybook", "/bookshelf", "/shelf", "/cart/", "/shoppingcart", "/shop/", "/store/", "/login", "/register", "/passport", "/password/", "/locale/", "/activity",
        "/user/", "/member/", "/help/", "/about/", "/support/", "/search/", "/so/",
        "/top/", "/topic/", "/txttop/", "/rank/", "/paihang/", "/category/", "/class/", "/sort/", "/shuku/", "/cat/", "/history/", "/myhistory", "/allbook", "/banquan", "/authorwelfare", "/shortart", "/pindao/",
        "/list/update/",
        "/wanben/", "/quanben/", "/news/", "/cate/", "/flash/", "/photo/", "/fav/",
        "/article/bcview", "/booknot/", "/taoge/", "/genres/", "/sound/player", "/vod/detail", "/film/", "/movie/", "/movies/",
        "/writer/", "/author/", "/authorlist/", "/zuojia/",
        "/index/app", "/book_list",
        "/cdn-cgi/", "/sitemap/",
        "/mstackslistright/",
        "/endnovel",
        "/helpcenter/", "/commercialization/", "/businessinquire/", "/useradd/", "/site/", "/nolist/", "/userlist/", "/favnovelmain/",
    ]
    if any(fragment in path for fragment in utility_fragments if not (allowed_author_book_detail and fragment == "/author/")):
        return False
    if segments and segments[0] == "s":
        return False
    if len(segments) == 2 and segments[0] == "search":
        return False
    genre_segments = {
        "xuanhuan", "qihuan", "yanqing", "dushi", "wuxia", "xianxia", "lishi", "wangyou", "kehuan",
        "kongbu", "jingsong", "chuanyue", "wenxue", "jiqing", "qita", "junshi", "xuanyi", "lingyi",
        "danmei", "lawen", "full", "fuwen", "nvsheng", "nansheng", "nanpin", "nvpin",
        "xuanhuanxiaoshuo", "yanqingxiaoshuo", "dushixiaoshuo", "wuxiaxiaoshuo",
    }
    if re.match(r"^/(?:xuanhuan|qihuan|yanqing|dushi|wuxia|xianxia|lishi|wangyou|kehuan|kongbu|jingsong|chuanyue|wenxue|danmei|qita|nvsheng|nansheng)[-_]\d+\.(?:html?|shtml)$", path):
        return False
    if last in genre_segments:
        return False
    if segments and segments[-1].endswith("xiaoshuo") and len(segments[-1]) <= 24:
        return False
    if len(segments) >= 2 and segments[-2] in genre_segments:
        page = re.sub(r"\.(?:html?|shtml)$", "", segments[-1])
        if page.isdigit() and int(page) <= 20:
            return False
    return True


def is_usable_rule_extracted_book_url(url: str, item: str, name: str, runtime: RuleRuntime) -> bool:
    # Explicit ruleSearch/ruleExplore.bookUrl output is authoritative in Legado.
    # Aggressive utility-link heuristics belong to recovered/fallback anchors only;
    # they reject valid detail shapes such as /book/<id>/<id>.html.
    # Stateful aggregation sources intentionally return a typed ``data:``
    # carrier here. It is not a browser URL, but AnalyzeUrl consumes it as an
    # in-memory hexadecimal StrResponse before ruleBookInfo runs.
    if decode_data_url_as_hex_response(url) is not None:
        return True
    if not is_plausible_source_url(url) or is_static_asset_url(url):
        return False
    return True


def is_likely_reader_page_url(url: str) -> bool:
    text = (url or "").strip()
    if not text:
        return False
    url_part, _ = parse_url_options(text)
    path = (urlparse(url_part).path or "").lower()
    return bool(
        re.search(r"/(?:chapter|read|reader|readchapter)(?:[-_/]|\d)", path)
        or re.search(r"/index\.php/chapter-\d+\.html?$", path)
        or re.search(r"/\d+/\d+\.html?$", path)
    )


def is_likely_reader_catalog_detail_link(candidate_url: str, current_url: str, link_text: str) -> bool:
    compact_text = re.sub(r"\s+", "", (link_text or "").strip()).lower()
    if not any(marker in compact_text for marker in ["\u8fd4\u56de\u76ee\u5f55", "\u76ee\u5f55", "\u76ee\u9304", "catalog", "contents"]):
        return False
    candidate_path = (urlparse(parse_url_options(candidate_url)[0]).path or "").lower()
    current_path = (urlparse(parse_url_options(current_url)[0]).path or "").lower()
    if not candidate_path or not current_path:
        return False
    candidate_ids = set(re.findall(r"\d{2,}", candidate_path))
    current_ids = set(re.findall(r"\d{2,}", current_path))
    if not candidate_ids or not (candidate_ids & current_ids):
        return False
    return bool(
        re.search(r"^/\d{1,4}/\d{2,}\.(?:html?|shtml)$", candidate_path)
        and re.search(r"^/\d{1,4}/\d{2,}/\d{2,}\.(?:html?|shtml)$", current_path)
    )


def recover_book_detail_url_from_reader_html(html: str, final_url: str, current_url: str) -> str:
    if not html.strip() or is_json_content(html):
        return ""
    if not (is_likely_reader_page_url(current_url) or is_likely_reader_page_url(final_url)):
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""
    selectors = [
        "a.j-comic-title[href]",
        ".read__crumb a[href*='/comic/']",
        ".read-crumb a[href*='/comic/']",
        ".breadcrumb a[href*='/comic/']",
        "a[href*='/index.php/comic/']",
        "a[href*='/comic/']",
        ".crumb a[href]",
        ".breadcrumb a[href]",
        ".read__crumb a[href]",
        ".read-crumb a[href]",
        "a[href]",
    ]
    current_key = normalized_book_candidate_url_for_comparison(current_url or final_url)
    current_path = (urlparse(parse_url_options(current_url or final_url)[0]).path or "").rstrip("/").lower()
    for selector in selectors:
        for node in soup.select(selector):
            href = (node.get("href") or "").strip()
            if not href:
                continue
            resolved = resolve_plausible_url(href, final_url, same_host_as=final_url)
            if not resolved:
                continue
            if normalized_book_candidate_url_for_comparison(resolved) == current_key:
                continue
            text = node.get_text(" ", strip=True)
            is_reader_catalog_detail = is_likely_reader_catalog_detail_link(resolved, current_url or final_url, text)
            if is_likely_reader_page_url(resolved) and not is_reader_catalog_detail:
                continue
            resolved_path = (urlparse(parse_url_options(resolved)[0]).path or "").rstrip("/").lower()
            resolved_parent_path = re.sub(r"\.(?:html?|shtml)$", "", resolved_path)
            is_reader_parent = bool(
                current_path
                and resolved_path
                and (
                    current_path.startswith(resolved_path + "/")
                    or (
                        resolved_parent_path != resolved_path
                        and current_path.startswith(resolved_parent_path + "/")
                    )
                )
                and re.search(r"\d{2,}", resolved_path)
            )
            if is_usable_search_book_url(resolved) and ("/comic/" in resolved_path or is_reader_parent or is_reader_catalog_detail):
                return resolved
    return ""


def recover_book_detail_url_from_reader_candidate(session: requests.Session, runtime: RuleRuntime, book_url: str) -> str:
    if not is_likely_reader_page_url(book_url):
        return ""
    abs_url, request_options = resolve_url_with_options(book_url, runtime.base_url)
    if not abs_url:
        abs_url = urljoin(runtime.base_url, book_url)
    if not abs_url:
        return ""
    request_headers = dict(runtime.headers or {})
    if isinstance(request_options.get("headers"), dict):
        request_headers.update({str(k): str(v) for k, v in request_options["headers"].items() if v is not None})
    html, final_url, _ = fetch_text(
        session,
        abs_url,
        method=str(request_options.get("method") or "GET"),
        headers=request_headers,
        body=request_options.get("body") if isinstance(request_options.get("body"), str) else None,
        charset=request_options.get("charset") if isinstance(request_options.get("charset"), str) else None,
    )
    return recover_book_detail_url_from_reader_html(html, final_url or abs_url, book_url)


def matches_book_url_pattern(src: dict[str, Any], url: str) -> bool:
    pattern = str(src.get("bookUrlPattern") or "").strip()
    if not pattern:
        return False
    text = (url or "").strip()
    # This check is only for an HTTP search response that is already a detail
    # page. Typed data carriers remain list-parser input and must not take this
    # branch.
    if decode_data_url_as_hex_response(text) is not None:
        return False
    if not text or text.startswith(("{", "[")):
        return False
    url_part, _ = parse_url_options(text)
    try:
        return re.fullmatch(pattern, url_part) is not None
    except re.error:
        return False


def is_usable_search_book_title(title: str) -> bool:
    compact = re.sub(r"\s+", "", (title or "").strip()).lower()
    if not compact:
        return False
    exact_noise = {
        "\u9996\u9875", "\u5206\u7c7b", "\u5168\u672c", "\u6700\u65b0", "\u6392\u884c", "\u6d3b\u52a8", "\u6d3b\u52d5", "\u4e13\u9898", "\u5c08\u984c", "\u7248\u6743\u58f0\u660e", "\u7248\u6b0a\u8072\u660e", "\u7528\u6237\u9690\u79c1\u6743\u653f\u7b56\u6761\u6b3e", "\u7528\u6236\u96b1\u79c1\u6b0a\u653f\u7b56\u689d\u6b3e",
        "\u8bba\u575b", "\u8ad6\u58c7", "\u7559\u8a00\u677f", "\u95ee\u4e0e\u7b54", "\u554f\u8207\u7b54",
        "\u767b\u5f55", "\u767b\u9304", "\u767b\u9646", "\u767b\u9678", "\u6ce8\u518c", "\u8a3b\u518a", "\u7528\u6237\u767b\u5f55", "\u7528\u6236\u767b\u9304",
        "book_name", "bookname", "booktitle", "chaptername", "chaptertitle",
        "\u5c01\u9762\u63a8\u8350", "\u8df3\u8f6c", "\u8fd4\u56de\u9996\u9875", "\u8fd4\u56de", "\u4e0a\u4e00\u9875", "\u4e0a\u4e00\u9801", "\u9519\u8bef\u63d0\u793a",
        "command+d", "\u5206\u4eab\u5230qq\u7a7a\u95f4", "\u5206\u4eab\u5230qzone",
        "尾页", "网页", "展开", "搜索", "-", "第01集", "第1集",
        "servererror", "服务器错误", "404-notfound·语雀", "404-notfound", "404",
        "we’regettingthingsready", "we'regettingthingsready",
    }
    if compact in {str(x).lower() for x in exact_noise}:
        return False
    noise_fragments = [
        "\u4e66\u5e93", "\u66f8\u5eab", "\u5c0f\u8bf4\u5206\u7c7b", "\u5c0f\u8aaa\u5206\u985e", "\u5c0f\u8aaa\u5206\u7c7b",
        "\u5206\u4eab\u7ed9\u597d\u53cb", "\u5206\u4eab\u7d66\u597d\u53cb",
        "\u767b\u5f55", "\u767b\u9304", "\u767b\u9646", "\u767b\u9678", "\u7528\u6237\u767b\u5f55", "\u7528\u6236\u767b\u9304", "\u4f1a\u5458\u767b\u5f55", "\u6703\u54e1\u767b\u9304",
        "cms\u7ba1\u7406\u7cfb\u7edf",
        "音乐网", "影视", "在线播放", "漫画", "notfound", "servererror",
        "gettingthingsready",
    ]
    return not any(fragment in compact for fragment in noise_fragments)


def same_host(lhs: str, rhs: str) -> bool:
    left = urlparse(lhs)
    right = urlparse(rhs)
    return bool(left.netloc and right.netloc and left.netloc.lower() == right.netloc.lower())


def resolve_plausible_url(raw_url: str, base_url: str, same_host_as: str = "") -> str:
    resolved, _ = resolve_url_with_options(raw_url, base_url)
    if not is_plausible_source_url(resolved):
        return ""
    if same_host_as and not same_host(resolved, same_host_as):
        return ""
    return resolved


def resolve_plausible_url_preserving_options(raw_url: str, base_url: str, same_host_as: str = "") -> str:
    url_part, opts = resolve_url_with_options(raw_url, base_url)
    # R157 (js G7 maoyan, tocContent D mowan): a `data:` carrier (`data:;base64,<payload>,{"type":"mw"}`) is a synthetic request
    # (Legado AnalyzeUrl.getByteArrayIfDataUri / getStrResponseAwait), not a data asset. The App accepts every `data:` URL
    # up front (BookSourceEngine.swift isPlausibleBookSourceUrl :4089), so the validator does too; it has no host to compare.
    data_carrier = (url_part or "").strip().lower().startswith("data:")
    if not data_carrier and not is_plausible_request_url(url_part):
        return ""
    if same_host_as and not data_carrier and not same_host(url_part, same_host_as):
        return ""
    if not opts:
        return url_part
    return f"{url_part},{json.dumps(opts, ensure_ascii=False, separators=(',', ':'))}"


def resolve_url_preserving_options(raw_url: str, base_url: str) -> str:
    url_part, opts = resolve_url_with_options(raw_url, base_url)
    if not url_part:
        return ""
    if not opts:
        return url_part
    return f"{url_part},{json.dumps(opts, ensure_ascii=False, separators=(',', ':'))}"


def is_likely_single_chapter_toc_fallback(candidate_url: str, book_url: str) -> bool:
    candidate = urlparse(candidate_url or "")
    book = urlparse(book_url or "")
    candidate_path = (candidate.path or "").lower()
    book_path = (book.path or "").lower()
    if candidate_path == book_path and candidate_path.endswith("/onebook.php"):
        candidate_query = parse_qs(candidate.query or "")
        book_query = parse_qs(book.query or "")
        candidate_novel_id = (candidate_query.get("novelid") or [""])[0]
        book_novel_id = (book_query.get("novelid") or [""])[0]
        if candidate_novel_id and candidate_novel_id == book_novel_id and (candidate_query.get("chapterid") or [""])[0]:
            return True
    if "/chapter/" in candidate_path and "/book/" in book_path:
        book_id = re.search(r"/book/(\d+)", book_path)
        if (book_id is None or book_id.group(1) in candidate_path) and re.search(r"/chapter/(?:\d+/\d+|\d+[_-]\d+)(?:/|\.html?$|$)", candidate_path):
            return True
    if "/chapter/" in candidate_path:
        book_ids = set(re.findall(r"\d{2,}", book_path))
        candidate_ids = set(re.findall(r"\d{2,}", candidate_path))
        has_shared_book_id = bool(book_ids & candidate_ids)
        has_single_chapter_shape = (
            re.search(r"/chapter/(?:index/)?(?:id/)?\d{2,}(?:/cid/|/seqno/|/)\d{1,}(?:/|\.html?$|$)", candidate_path) is not None
            or re.search(r"/chapter/\d{2,}/\d{2,}\.html?$", candidate_path) is not None
        )
        if has_shared_book_id and has_single_chapter_shape:
            return True
    if not (candidate_path.endswith(".html") or candidate_path.endswith(".htm")):
        return False
    book_ids = set(re.findall(r"\d{2,}", book_path))
    candidate_ids = set(re.findall(r"\d{2,}", candidate_path))
    if book_ids and book_ids & candidate_ids and re.search(r"/\d{2,}/\d{2,}/\d{2,}\.html?$", candidate_path):
        return True
    book_slug = re.search(r"/fu/([^/]+)", book_path)
    if book_slug is not None and f"/furead/{book_slug.group(1)}/" in candidate_path:
        return True
    if book_path.endswith("/"):
        book_base = book_path
    else:
        book_base = book_path.rsplit("/", 1)[0] + "/" if "/" in book_path else ""
    if len(book_base) <= 1 or not candidate_path.startswith(book_base):
        return False
    last = candidate_path.rsplit("/", 1)[-1]
    return re.search(r"\d{3,}", last) is not None


def normalize_chapter_url_to_book_host(chapter_url: str, book_url: str) -> str:
    chapter = urlparse(chapter_url or "")
    book = urlparse(book_url or "")
    if not chapter.netloc or not book.netloc:
        return chapter_url
    recovered = recover_80zw_mobile_chapter_url(chapter_url, book_url)
    if recovered:
        return recovered
    if chapter.netloc.lower() == book.netloc.lower():
        return chapter_url
    book_path = book.path or "/"
    if book_path.endswith("/"):
        book_base_path = book_path
    else:
        book_base_path = book_path.rsplit("/", 1)[0] + "/" if "/" in book_path else "/"
    if len(book_base_path) <= 1 or not (chapter.path or "").startswith(book_base_path):
        return chapter_url
    return urlunparse((
        book.scheme or chapter.scheme,
        book.netloc,
        chapter.path,
        chapter.params,
        chapter.query,
        chapter.fragment,
    ))


def recover_80zw_mobile_chapter_url(chapter_url: str, book_url: str) -> str:
    chapter = urlparse(chapter_url or "")
    book = urlparse(book_url or "")
    chapter_host = chapter.netloc.lower()
    book_host = book.netloc.lower()
    if "qiushu.info" not in chapter_host or not book_host.endswith("80zw.la"):
        return ""
    match = re.match(r"^/t/(\d+)/(\d+(?:_\d+)?)\.html?$", chapter.path or "", flags=re.I)
    if not match:
        return ""
    book_id, chapter_id = match.groups()
    scheme = book.scheme or chapter.scheme or "http"
    return urlunparse((scheme, "wap.80zw.la", f"/{book_id}/{chapter_id}.html", "", "", ""))


def numeric_chapter_stem(url_string: str) -> str:
    path = urlparse(url_string or "").path or ""
    file_name = path.rsplit("/", 1)[-1]
    stem = re.sub(r"\.html?$", "", file_name, flags=re.I)
    match = re.match(r"^\d+", stem)
    return match.group(0) if match else ""


def is_same_chapter_content_page(candidate_url: str, current_url: str, first_url: str) -> bool:
    candidate_stem = numeric_chapter_stem(candidate_url)
    if not candidate_stem:
        return True
    first_stem = numeric_chapter_stem(first_url)
    current_stem = numeric_chapter_stem(current_url)
    if first_stem and candidate_stem != first_stem:
        return False
    if current_stem and candidate_stem != current_stem:
        return False
    return True


def derive_paged_chapter_urls(current_url: str, html: str, page_content: str) -> list[str]:
    combined = f"{page_content}\n{html}"
    match = re.search(r"第\s*\(?\s*(\d+)\s*/\s*(\d+)\s*\)?\s*页", combined)
    if not match:
        return []
    cur = int(match.group(1))
    total = int(match.group(2))
    if total <= cur or total > 20:
        return []
    trimmed = (current_url or "").strip()
    if not trimmed:
        return []
    suffix_match = re.search(r"_([0-9]+)\.html$", trimmed, flags=re.I)
    if suffix_match:
        prefix = trimmed[: suffix_match.start()]
        return [f"{prefix}_{idx}.html" for idx in range(cur + 1, total + 1)]
    if trimmed.lower().endswith(".html"):
        base = trimmed[:-5]
        return [f"{base}_{idx}.html" for idx in range(cur + 1, total + 1)]
    return []


def build_url_with_options(template: str, keyword: str, page: int, runtime: RuleRuntime, session: requests.Session | None = None) -> tuple[str, dict[str, Any]]:
    # Legado AnalyzeUrl for searchUrl/exploreUrl runs on RuleData (book == null).
    previous_binding = getattr(runtime, "book_binding", "automatic")
    runtime.book_binding = "unbound"
    try:
        return _build_url_with_options(template, keyword, page, runtime, session)
    finally:
        runtime.book_binding = previous_binding


def _leading_js_prefix_url_text(template: str, runtime: RuleRuntime, js_vars: dict[str, Any]) -> str | None:
    """R158 (D2): a template that STARTS with `<js>..</js>` and continues with URL text. Legado AnalyzeUrl.analyzeJs (:153-176) walks the
    `<js>` matches: the text before a match replaces `result` (its `@result` is substituted), the code runs with that `result`, and the text after the
    last match replaces `result` again (`@result` -> the last JS value); so the JS value survives only through an explicit `@result` and
    `{{key}}` in the text is evaluated afterwards. App: BookSourceEngine `legadoAnalyzeUrlRule`. Returns the resulting text (still holding its
    `{{..}}`), or None when the template has no text after the last `</js>` (a whole-template script)."""
    blocks = list(re.finditer(r"<js>([\s\S]*?)</js>", template, re.I))
    if not blocks:
        return None
    tail = template[blocks[-1].end():].strip()
    if not tail:
        return None
    result = template  # Legado `var result = ruleUrl`: the first script sees the whole rule text as `result`
    position = 0
    for block in blocks:
        segment = template[position:block.start()].strip()
        if segment:
            result = segment.replace("@result", result)
        code = block.group(1).strip()
        result = evaluate_js(code, result, runtime, extra_vars=js_vars).strip() if code else result
        position = block.end()
    return tail.replace("@result", result)


def _build_url_with_options(template: str, keyword: str, page: int, runtime: RuleRuntime, session: requests.Session | None = None) -> tuple[str, dict[str, Any]]:
    # R156 (network): a plain URL template binds the keyword already in this first pass, so a `{{js}}` expression that uses
    # `key` (`data:;base64,{{java.base64Encode("/search?keyword="+key)}}`) sees it. Script URLs get it through `js_vars`.
    # R158 (D2, both profiles; ndlib `.cn`): a `<js>..</js>` prefix followed by plain URL text is NOT a whole-template script. Legado
    # AnalyzeUrl.analyzeJs (:153-176) drops the JS value when URL text follows and `{{key}}` is evaluated afterwards (replaceKeyPageJs);
    # the App does the same (BookSourceEngine.legadoAnalyzeUrlRule: the tail text replaces `result`, then every `{{..}}` is evaluated with
    # key / page bound). Only `@js:` or a template that is `<js>..</js>` as a whole keeps the keyword out of this first pass.
    _template_low = template.strip().lower()
    js_vars = {
        "key": keyword,
        "searchKey": encode_keyword(keyword),
        "keyword": keyword,
        "rawKeyword": keyword,
        "page": page,
        "pageIndex": page - 1,
        "pagePlus1": page + 1,
    }
    _leading_js_tail = None
    if _template_low.startswith("<js>") and not _template_low.endswith("</js>"):
        _leading_js_tail = _leading_js_prefix_url_text(template, runtime, js_vars)
    if _template_low.startswith("@js:") or (_template_low.startswith("<js>") and _template_low.endswith("</js>")):
        pre_resolved = resolve_template(template, runtime, page=page)
    elif _leading_js_tail is not None:
        pre_resolved = resolve_template(_leading_js_tail, runtime, keyword=keyword, page=page, raw_keyword=keyword)
    else:
        pre_resolved = resolve_template(template, runtime, keyword=keyword, page=page, raw_keyword=keyword)
    raw = normalize_legacy_template(strip_leading_cookie_side_effect_template(pre_resolved))
    raw = apply_page_expression_templates(raw, page)
    raw_stripped = raw.strip()
    if raw_stripped.lower().startswith("@js:") or (raw_stripped.lower().startswith("<js>") and raw_stripped.lower().endswith("</js>")):
        raw = execute_js_block_if_needed(raw, runtime, extra_vars=js_vars, session=session)
    else:
        raw = execute_embedded_js_blocks_if_needed(raw, runtime)
        raw = execute_js_block_if_needed(raw, runtime, extra_vars=js_vars, session=session)
    raw = execute_trailing_url_js_block_if_needed(raw, runtime, js_vars)
    raw = raw.split("||", 1)[0].strip()
    raw = resolve_template(raw, runtime, keyword=keyword, page=page, raw_keyword=keyword)
    raw, opts = parse_url_options(raw)
    # Page segments belong to the URL only. Applying them to the complete
    # `url,{options}` string strips XML body tags such as <search>/<key>.
    raw = apply_legado_page_segments(raw, page).strip()
    if app_profiles.current_app_profile().page_segment_in_post_body and isinstance(opts.get("body"), str):
        # R156 (searchjs Q, App profile 1.1.2): Legado AnalyzeUrl.replaceKeyPageJs (:196-208) replaces `<a,b>` in the WHOLE rule,
        # `,{options}` included, so `<,&page=2>` in a POST body is chosen by page. Comma segments only: an XML body keeps
        # `<search>` / `<key>` (BookSourceEngine.swift applies the same restriction).
        opts["body"] = apply_legado_page_segments(opts["body"], page, requires_comma=True)
    option_js = opts.get("js")
    if isinstance(option_js, str) and option_js.strip():
        evaluated_url = evaluate_js(
            option_js,
            raw,
            runtime,
            extra_vars=js_vars,
        ).strip()
        if evaluated_url:
            raw = evaluated_url
    charset = opts.get("charset") if isinstance(opts.get("charset"), str) else None
    if charset and keyword:
        default_encoded = encode_keyword(keyword)
        charset_encoded = encode_keyword(keyword, charset)
        if default_encoded != charset_encoded:
            raw = raw.replace(default_encoded, charset_encoded)
        for ch in set(keyword):
            default_ch = encode_keyword(ch)
            charset_ch = encode_keyword(ch, charset)
            if default_ch != charset_ch:
                raw = raw.replace(default_ch, charset_ch)
    if isinstance(opts.get("body"), str):
        body = opts["body"]
        body = resolve_template(body, runtime, keyword=keyword, page=page, raw_keyword=keyword)
        default_encoded = encode_keyword(keyword) if keyword else ""
        if keyword and default_encoded != keyword:
            body = body.replace(default_encoded, keyword)
        if charset and keyword:
            charset_encoded = encode_keyword(keyword, charset)
            if charset_encoded != keyword:
                body = body.replace(charset_encoded, keyword)
        merged_headers = dict(runtime.headers or {})
        merged_headers.update(opts.get("headers") or {})
        if not _has_explicit_content_type(merged_headers) and not _is_json_or_xml_request_body(body):
            body = encode_legado_form_body(body, charset)
        opts["body"] = body
    return raw, opts


def split_trailing_js_block(text: str) -> tuple[str, str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return text, ""
    first = lines[0]
    tail = "\n".join(lines[1:]).strip()
    low_tail = tail.lower()
    if first.lower().startswith(("@js:", "<js>")):
        return text, ""
    if low_tail.startswith("@js:") or low_tail.startswith("<js>"):
        return first, tail
    return text, ""


def execute_trailing_url_js_block_if_needed(text: str, runtime: RuleRuntime, extra_vars: dict[str, Any]) -> str:
    url_part, js_part = split_trailing_js_block(text)
    if not js_part:
        return text
    low = js_part.lower()
    if low.startswith("@js:"):
        result = evaluate_js(js_part[4:], url_part, runtime, extra_vars=extra_vars).strip()
        return result or url_part
    if low.startswith("<js>") and low.endswith("</js>"):
        result = evaluate_js(js_part[4:-5], url_part, runtime, extra_vars=extra_vars).strip()
        return result or url_part
    return url_part


def split_explore_url_entries(text: str) -> list[str]:
    """BookSource.splitExploreUrlEntries (BookSource.swift:1190-1196): split on newlines first, then on `&&`."""
    return [
        piece.strip()
        for line in (text or "").splitlines()
        for piece in line.split("&&")
        if piece.strip()
    ]


def is_explore_action_url(text: str) -> bool:
    """BookSource.isExploreActionURL: `{{js}}` entries are button actions, never a requestable category."""
    value = (text or "").strip()
    return value.startswith("{{") and value.endswith("}}")


def is_script_explore_text(text: str) -> bool:
    """BookSource.isScriptExploreUrlField: the first line starts with `@js:` / `<js>` / is exactly `@js`."""
    stripped = (text or "").strip()
    first_line = (stripped.splitlines() or [""])[0].strip().lower()
    return first_line.startswith(("@js:", "<js>")) or first_line == "@js"


def parse_explore_kinds_app(text: str, source_name: str) -> list[tuple[str, str]]:
    """BookSource.parseExploreUrlField (BookSource.swift:1104-1180) for the OUTPUT of an `@js:` / `<js>` category script.

    Unlike `parse_explore_urls` a plain segment without `::` is a title-only header (url ""), and `action` objects become
    `{{action}}` entries; the caller picks the first entry whose url is neither empty nor an action."""
    trimmed = (text or "").strip()
    if not trimmed:
        return []
    if trimmed.startswith("["):
        try:
            array = json.loads(trimmed, strict=False)  # R156: Gson is lenient - literal newlines inside strings are fine
        except Exception:
            array = None
        if isinstance(array, list) and all(isinstance(item, dict) for item in array):
            kinds: list[tuple[str, str]] = []
            for item in array:
                title = explore_object_value(item, ["title", "name", "label", "group", "type"])
                url = explore_object_value(item, ["url", "href", "link", "value"])
                if url:
                    kinds.append((title or source_name, url))
                    continue
                action = explore_object_value(item, ["action"])
                if action:
                    kinds.append((title or source_name, "{{" + action + "}}"))
                    continue
                header = explore_object_value(item, ["title"])
                if header:
                    kinds.append((header, ""))
            return kinds
        loose = parse_loose_explore_objects(trimmed, source_name)
        if loose:
            return loose
    if is_script_explore_text(trimmed):
        return [(source_name, trimmed)]
    kinds = []
    for line in split_explore_url_entries(trimmed):
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                url = explore_object_value(obj, ["url", "href", "link", "value"])
                if url:
                    kinds.append((explore_object_value(obj, ["title", "name", "label", "group", "type"]) or source_name, url))
                    continue
            loose = parse_loose_explore_objects(line, source_name)
            if loose:
                kinds.append(loose[0])
                continue
        parts = line.split("::")
        title = parts[0].strip()
        url = parts[1].strip() if len(parts) >= 2 else ""
        if not url:
            if title:
                kinds.append((title, ""))
        else:
            kinds.append((title or source_name, url))
    return kinds


def extract_explore_kinds_script(text: str) -> str | None:
    """BookSourceEngine+Flagship.swift extractExploreKindsScript."""
    trimmed = (text or "").strip()
    lowered = trimmed.lower()
    if lowered.startswith("@js:"):
        return trimmed[4:]
    if lowered.startswith("@js"):
        return trimmed[3:]
    if lowered.startswith("<js>") and lowered.endswith("</js>"):
        return trimmed[4:-5]
    return None


def run_explore_kinds_script(explore_text: str, runtime: RuleRuntime) -> tuple[str | None, bool]:
    """BookSourceEngine+Flagship.resolveExploreCategories (32-94): run the category script with no book and input "".

    Returns (generated text, failed). A script exception is not a validation failure in the App - it becomes an
    unclickable `ERROR:` header, i.e. `发现规则为空` - so errors are captured here instead of reaching the stage sink.
    Validator-polyfill gaps (`compat` entries) are forwarded to the stage's sink as warnings."""
    script = extract_explore_kinds_script(explore_text)
    if script is None:
        return None, False
    outer_sink = getattr(runtime, "script_error_sink", None)
    outer_error = getattr(runtime, "script_error", "")
    local_sink: list = []
    runtime.script_error_sink = local_sink
    runtime.script_error = ""
    try:
        generated = evaluate_js(script, "", runtime).strip()
    finally:
        runtime.script_error_sink = outer_sink
        runtime.script_error = outer_error
    failed = any(entry.get("kind") == "error" for entry in local_sink)
    if outer_sink is not None:
        outer_sink.extend(entry for entry in local_sink if entry.get("kind") == "compat")
    return generated, failed


def parse_explore_urls(text: str, source_name: str) -> list[tuple[str, str]]:
    raw = (text or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            arr = json.loads(raw, strict=False)  # R156: Gson is lenient - literal newlines inside strings are fine
            if isinstance(arr, list):
                out = []
                for item in arr:
                    if isinstance(item, dict):
                        url = explore_object_value(item, ["url", "href", "link", "value"])
                        if url:
                            title = explore_object_value(item, ["title", "name", "label", "group", "type"]) or source_name
                            out.append((title, url))
                if out:
                    return out
        except Exception:
            pass
        loose = parse_loose_explore_objects(raw, source_name)
        if loose:
            return loose
    first_line = raw.splitlines()[0].strip()
    if first_line.lower().startswith("@js:") or first_line.lower().startswith("<js>") or first_line.lower() == "@js":
        return [(source_name, raw)]
    results: list[tuple[str, str]] = []
    for line in split_explore_url_entries(raw):
        try:
            if line.startswith("{") and line.endswith("}"):
                obj = json.loads(line, strict=False)
                if isinstance(obj, dict):
                    url = explore_object_value(obj, ["url", "href", "link", "value"])
                    if url:
                        title = explore_object_value(obj, ["title", "name", "label", "group", "type"]) or source_name
                        results.append((title, url))
                        continue
        except Exception:
            pass
        if line.startswith("{"):
            loose = parse_loose_explore_objects(line, source_name)
            if loose:
                results.append(loose[0])
                continue
        if "||" in line:
            title, url = line.split("||", 1)
            results.append((title.strip() or source_name, url.strip()))
            continue
        if "::" in line:
            title, url = line.split("::", 1)
            results.append((title.strip() or source_name, url.strip()))
            continue
        results.append((source_name, line))
    return results


def parse_loose_explore_objects(text: str, source_name: str) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for body in loose_explore_object_bodies(text):
        url = loose_explore_object_value_any(body, ["url", "href", "link", "value"])
        if not url:
            continue
        title = loose_explore_object_value_any(body, ["title", "name", "label", "group", "type"]) or source_name
        results.append((title.strip() or source_name, url.strip()))
    return results


def explore_object_value(item: dict[str, Any], keys: list[str]) -> str:
    lowered = {str(k).lower(): v for k, v in item.items()}
    for key in keys:
        value = item.get(key)
        if value is None:
            value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def loose_explore_object_bodies(text: str) -> list[str]:
    bodies: list[str] = []
    depth = 0
    start: int | None = None
    quote: str | None = None
    escaped = False
    for idx, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
        elif ch == "{":
            if depth == 0:
                start = idx + 1
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                bodies.append(text[start:idx])
                start = None
    return bodies


def loose_explore_object_value(body: str, key: str) -> str | None:
    pattern = rf"""(?i)(?:^|[,\s])["']?{re.escape(key)}["']?\s*:\s*(?:"([^"]*)"|'([^']*)'|([^,\}}\n]+))"""
    match = re.search(pattern, body)
    if not match:
        return None
    for group in match.groups():
        if group is not None:
            return str(group).strip()
    return None


def loose_explore_object_value_any(body: str, keys: list[str]) -> str | None:
    for key in keys:
        value = loose_explore_object_value(body, key)
        if value:
            return value
    return None


def split_on_at(text: str) -> list[str]:
    parts: list[str] = []
    current = ""
    for ch in text:
        if ch == "@":
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return parts


def looks_like_bare_jsonpath(text: str) -> bool:
    t = text.strip()
    if not t or t[0] in ".#/":
        return False
    if any(c in t for c in " \n\t:@>,"):
        return False
    # Legado CSS prefix patterns like "class.block", "tag.h2", "id.foo", "text.xxx"
    # are NOT JSONPath — they are CSS rule shorthand
    tl = t.lower()
    if tl.startswith(("class.", "tag.", "id.", "text.")):
        return False
    return ("." in t or "[" in t) and re.fullmatch(r"[A-Za-z0-9_\-\.\[\]\*]+", t) is not None


def looks_like_css_selector(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    # Use only the base segment (before first @) for detection,
    # because legado uses @ to chain CSS selectors like "h2@text", ".cover@p"
    base = t.split("@")[0].strip()
    base_lower = base.lower()
    if base_lower.startswith(("class.", "tag.", "id.", "text.")):
        return True
    # CSS class selector (e.g. .book-list, .cover) or id selector
    if base.startswith(".") or base.startswith("#"):
        return True
    # Contains structural CSS characters
    if any(ch in base for ch in (" ", "#", "[", "]", ">", "~", "+", ":")):
        return True
    html_tags = [
        "div", "span", "li", "ul", "ol", "a", "p", "h1", "h2", "h3",
        "h4", "h5", "h6", "td", "tr", "table", "article", "section",
        "tbody", "thead", "tfoot", "th",
        "img", "body", "header", "footer", "nav", "main", "aside",
        "dl", "dt", "dd", "form", "input", "button", "select", "option",
        "strong", "em", "b", "i", "font", "small", "figure", "figcaption",
        "center",
    ]
    # R156 (searchjs P): `a!1@text` - Legado ElementsSingle exclusion index (AnalyzeByJSoup.kt:294-345) after a bare tag name,
    # in the middle of an `@` chain (R155 only recognised the whole-rule form `tr!0`).
    exclusion = re.fullmatch(r"([a-z][a-z0-9]*)![-\d:,]+", base_lower)
    if exclusion is not None and exclusion.group(1) in html_tags:
        return True
    return any(
        base_lower == tag or base_lower.startswith(f"{tag}.") or base_lower.startswith(f"{tag} ") or base_lower.startswith(f"{tag}>")
        for tag in html_tags
    )


def parse_css_rule(inner: str) -> tuple[str, str | None, int | None]:
    known_attrs = {
        "text", "html", "innerhtml", "outerhtml", "textnodes", "owntext",
        "href", "src", "data-src", "data-href", "alt", "class", "id", "content",
        "name", "value", "title", "type", "onclick", "action", "style", "all",
    }
    parts = [p.strip() for p in split_on_at(inner) if p.strip()]
    if not parts:
        return "*", None, None

    final_attr: str | None = None
    final_index: int | None = None

    if len(parts) >= 2:
        last = parts[-1]
        if last.startswith("!") and last[1:].isdigit():
            final_index = -(1000 + int(last[1:]))
            parts.pop()
        elif re.fullmatch(r"-?\d+", last):
            final_index = int(last)
            parts.pop()

    if parts:
        last = parts[-1]
        if last.lower() in known_attrs or last.lower().startswith("data-"):
            final_attr = last
            parts.pop()

    selector_parts: list[str] = []
    for part in parts:
        if part.startswith("class."):
            class_name = part[6:]
            if class_name:
                if "." in class_name:
                    tail = class_name.rsplit(".", 1)[1]
                    if re.fullmatch(r"-?\d+", tail):
                        class_name = class_name.rsplit(".", 1)[0]
                        if final_index is None:
                            final_index = int(tail)
                selector_parts.append("".join(f".{x}" for x in class_name.split()))
                continue
        if part.startswith("text."):
            body = part[5:].strip()
            if body:
                selector_parts.append(f"*:contains({body})")
                continue
        if part.startswith("tag."):
            body = part[4:]
            if body:
                if "." in body:
                    tail = body.rsplit(".", 1)[1]
                    if re.fullmatch(r"-?\d+", tail):
                        body = body.rsplit(".", 1)[0]
                        if final_index is None:
                            final_index = int(tail)
                selector_parts.append(body)
                continue
        if part.startswith("id."):
            body = part[3:]
            if body and not body.isdigit():
                selector_parts.append(f"#{body}")
                continue
        if "!" in part:
            left, right = part.rsplit("!", 1)
            nums = [n.strip() for n in right.split(":") if n.strip().isdigit()]
            if nums:
                if left:
                    selector_parts.append(left)
                if final_index is None:
                    final_index = -(1000 + int(nums[0]))
                continue
        if "." in part:
            left, right = part.rsplit(".", 1)
            if re.fullmatch(r"-?\d+", right):
                selector_parts.append(left or "*")
                if final_index is None:
                    final_index = int(right)
                continue
        selector_parts.append(part)

    selector = " ".join(s for s in selector_parts if s) or "*"
    return selector, final_attr, final_index


_JSOUP_INDEX_PSEUDO_RE = re.compile(r":(lt|gt|eq)\(\s*(\d+)\s*\)")


def _jsoup_index_pseudo_to_nth_child(match: re.Match[str]) -> str:
    """Jsoup `:lt(n)` / `:gt(n)` / `:eq(n)` compare the ELEMENT SIBLING INDEX (0-based, any tag among the parent's
    element children) with n - exactly `:nth-child` (1-based): lt -> positions 1..n, gt -> n+2.., eq -> n+1."""
    kind = match.group(1)
    number = int(match.group(2))
    if kind == "lt":
        return ":nth-child(-n+%d)" % number if number > 0 else ":not(*)"
    if kind == "gt":
        return ":nth-child(n+%d)" % (number + 2)
    return ":nth-child(%d)" % (number + 1)


_JSOUP_UNQUOTED_ATTR_VALUE_RE = re.compile(r"\[\s*([A-Za-z_][-\w:.]*)\s*(\^=|\$=|\*=|\|=|!=|=)\s*([^\]\"']*?)\s*\]")


def _quote_jsoup_attribute_value(match: re.Match[str]) -> str:
    value = match.group(3)
    if not value or re.fullmatch(r"-?[A-Za-z_][-\w]*", value):
        return match.group(0)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '[%s%s"%s"]' % (match.group(1), match.group(2), escaped)


def normalize_selector(selector: str) -> str:
    selector = selector.replace(":contains(", ":-soup-contains(")
    if "[" in selector:
        # R155 (R): Jsoup's QueryParser takes everything up to `]` as the raw value of `[attr=value]` (`div[style=text-indent: 2em;]`,
        # sfacg's content rule); soupsieve only accepts an identifier or a quoted string there.
        selector = _JSOUP_UNQUOTED_ATTR_VALUE_RE.sub(_quote_jsoup_attribute_value, selector)
    if ":lt(" in selector or ":gt(" in selector or ":eq(" in selector:
        # R155 (M): soupsieve has no Jsoup index pseudo selectors.
        selector = _JSOUP_INDEX_PSEUDO_RE.sub(_jsoup_index_pseudo_to_nth_child, selector)
    return selector


_JSOUP_REGEX_ATTR_RE = re.compile(r"\[\s*([A-Za-z_][-\w:.]*)\s*~=\s*([^\]]*?)\s*\]")


_JSOUP_TEXT_REGEX_PSEUDO_RE = re.compile(r":(matchesOwn|matches)\(", re.IGNORECASE)


def _jsoup_text_regex_pseudo_prepare(root: Any, selector: str) -> tuple[str, list[tuple[Any, str]]]:
    """R158 (xs8): Jsoup `:matches(regex)` / `:matchesOwn(regex)` (Evaluator.Matches / MatchesOwn) test the element's text() / ownText() with
    Pattern.find; the App passes the selector straight to SwiftSoup (CSSEngine.swift `select`, which implements both), while soupsieve reads
    `:matches` as an alias of `:is` and rejects the regex. Mark the matching elements with a synthetic attribute and replace the pseudo with
    `[marker]` (same technique as `_jsoup_regex_attr_prepare`); the caller strips the markers."""
    if ":match" not in selector.lower():
        return selector, []
    marked: list[tuple[Any, str]] = []
    out: list[str] = []
    pos = 0
    counter = 0
    while True:
        found = _JSOUP_TEXT_REGEX_PSEUDO_RE.search(selector, pos)
        if not found:
            out.append(selector[pos:])
            break
        depth = 1
        i = found.end()
        while i < len(selector) and depth:
            ch = selector[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        if depth:
            out.append(selector[pos:])
            break
        raw = selector[found.end():i - 1]
        try:
            pattern = re.compile(raw)
        except re.error:
            out.append(selector[pos:i])
            pos = i
            continue
        counter += 1
        marker = "data-jstm%d" % counter
        own = found.group(1).lower() == "matchesown"
        nodes = list(root.find_all(True)) if hasattr(root, "find_all") else []
        # Jsoup Evaluator.Matches / MatchesOwn test element.text() / ownText() (whitespace collapsed, blocks spaced, comments / script / style
        # ignored): `_jsoup_text` is the text `@text` uses; `_jsoup_own_text` is Jsoup ownText() (direct text nodes, no separator).
        for node in nodes:
            node_text = _jsoup_own_text(node) if own else _jsoup_text(node)
            if regex_find(pattern, node_text):
                node[marker] = "1"
                marked.append((node, marker))
        out.append(selector[pos:found.start()])
        out.append("[%s]" % marker)
        pos = i
    return "".join(out), marked


def _jsoup_regex_attr_prepare(root: Any, selector: str) -> tuple[str, list[tuple[Any, str]]]:
    """R156 (tocContent F / searchjs E): Jsoup `[attr~=regex]` is Evaluator.AttributeWithValueMatching (a regex `find` on the
    attribute value); soupsieve's `~=` is the whitespace-separated word operator and rejects most regexes. Every element under
    `root` (and `root` itself) whose attribute matches gets a synthetic marker attribute and the clause becomes `[marker]`.
    Returns (selector, [(element, marker)]); the caller strips the markers with `_jsoup_regex_attr_cleanup` (try / finally)."""
    selector, text_marked = _jsoup_text_regex_pseudo_prepare(root, selector)
    if "~=" not in selector:
        return selector, text_marked
    marked: list[tuple[Any, str]] = list(text_marked)
    counter = [0]

    def rewrite(match: re.Match[str]) -> str:
        name, raw = match.group(1), match.group(2)
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        try:
            pattern = re.compile(raw)
        except re.error:
            return match.group(0)
        counter[0] += 1
        marker = "data-jsrx%d" % counter[0]
        nodes = list(root.find_all(True)) if hasattr(root, "find_all") else []
        if getattr(root, "name", None) not in (None, "[document]"):
            nodes.append(root)
        for node in nodes:
            value = node.get(name)
            if isinstance(value, (list, tuple)):
                value = " ".join(value)
            if value is not None and regex_find(pattern, str(value)):
                node[marker] = "1"
                marked.append((node, marker))
        return "[%s]" % marker

    return _JSOUP_REGEX_ATTR_RE.sub(rewrite, selector), marked


def _jsoup_regex_attr_cleanup(marked: list[tuple[Any, str]]) -> None:
    for node, marker in marked:
        try:
            del node.attrs[marker]
        except Exception:
            pass


def css_extract(html: str, selector: str, attr: str | None, index: int | None, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    if "tbody" in selector.lower():
        _insert_implicit_tbody(soup)  # R149 (B4): Jsoup's HTML5 tree builder always puts <tr> into a <tbody>
    try:
        _css_selector, _regex_marked = _jsoup_regex_attr_prepare(soup, normalize_selector(selector))
        try:
            elems = soup.select(_css_selector)
        finally:
            _jsoup_regex_attr_cleanup(_regex_marked)
    except Exception as _swallowed:
        note_debug_error("css selector", _swallowed)
        return []
    if index is not None:
        if index <= -1000:
            exclude = -(index + 1000)
            elems = [e for i, e in enumerate(elems) if i != exclude]
        else:
            actual = index if index >= 0 else len(elems) + index
            if actual < 0 or actual >= len(elems):
                return []
            elems = [elems[actual]]
    results: list[str] = []
    # R156: the explicit `@css:sel@textNodes` path hands the attribute over unlowered (Legado's last-rule keywords are `text`,
    # `textNodes`, `ownText`, `html`, `all`); compare the keywords case-insensitively, real attribute names stay as written.
    raw_attr = attr
    attr = attr.lower() if isinstance(attr, str) and attr.lower() in _CSS_LAST_RULE_KEYWORDS else attr
    for el in elems:
        if attr is None or attr == "outerhtml":
            results.append(_serialize_html(el))
        elif attr == "all":
            results.append(_serialize_html(el))
        elif attr == "html" or attr == "innerhtml":
            results.append("".join(_serialize_html(c) for c in el.contents))
        elif attr == "text":
            results.append(el.get_text(strip=True))
        elif attr == "owntext":
            results.append("".join(s.strip() for s in el.find_all(string=True, recursive=False)))
        elif attr == "textnodes":
            results.append("\n".join(s.strip() for s in el.find_all(string=True, recursive=False) if s.strip()))
        else:
            value = _css_raw_attr_value(el, raw_attr or attr, base_url)
            if value:
                results.append(value)
    return [x for x in results if x]


_CSS_LAST_RULE_KEYWORDS = frozenset({"outerhtml", "all", "html", "innerhtml", "text", "owntext", "textnodes"})


def _css_raw_attr_value(el: Any, attr: str, base_url: str) -> str:
    """Attribute value for the `@css:` / text-search paths.

    Legado AnalyzeByJSoup and the Swift CSSEngine (resolveURLAttributes == false) return the RAW
    attribute value (`@css:a@href` -> "/book/1", not "https://host/book/1"). URLs are made absolute
    only by the URL-consuming call sites (bookUrl/tocUrl/chapterUrl/nextTocUrl/nextContentUrl/
    cover via resolve_url_with_options / legado_absolute_url). An explicit `abs:` prefix still
    resolves against the base URL, like Jsoup Node.attr("abs:href").
    """
    if attr.lower().startswith("abs:"):
        raw = el.get(attr[4:], "")
        if isinstance(raw, list):
            raw = " ".join(str(item) for item in raw)
        raw = str(raw or "").strip()
        return urljoin(base_url, raw) if raw else ""
    value = el.get(attr, "")
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return str(value) if value else ""


def select_elements_json(selector: str, html: str, base_url: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    try:
        _css_selector, _regex_marked = _jsoup_regex_attr_prepare(soup, normalize_selector(selector or "*"))
        try:
            elems = soup.select(_css_selector)
        finally:
            _jsoup_regex_attr_cleanup(_regex_marked)
    except Exception:
        elems = []
    if not elems and (selector or "").strip().lower() == "body":
        elems = [soup]

    return json.dumps([_element_row(el, base_url) for el in elems], ensure_ascii=False)


def _element_row(el: Any, base_url: str) -> dict[str, str]:
    outer = _serialize_html(el)
    inner = "".join(_serialize_html(c) for c in getattr(el, "contents", [])).strip()
    text = el.get_text(" ", strip=True) if hasattr(el, "get_text") else str(el)
    own_text = ""
    if hasattr(el, "find_all"):
        own_text = " ".join(s.strip() for s in el.find_all(string=True, recursive=False) if s.strip())
    row: dict[str, str] = {
        "outerHtml": outer,
        "html": inner,
        "text": text,
        "ownText": own_text,
    }
    attrs = getattr(el, "attrs", {}) or {}
    for key, value in attrs.items():
        attr_key = str(key).lower()
        if isinstance(value, list):
            attr_value = " ".join(str(v) for v in value)
        else:
            attr_value = str(value)
        # Jsoup Element.attr(name) is the raw value (Legado / iOS semantics); only an explicit "abs:" resolves.
        row[attr_key] = attr_value
        if attr_key in URL_ATTRS:
            row["abs:" + attr_key] = urljoin(base_url, attr_value)
    return row


_LEGADO_ELEMENT_RULE_PREFIX_RE = re.compile(r"^(?:class|tag|id|text)\.|^children(?:$|[.!\[])|^@@|^@(?:css|xpath|json):|^/", re.I)
_LEGADO_ELEMENT_RULE_INDEX_RE = re.compile(r"(?:[.!]\s*-?\d+(?:\s*:\s*-?\d+)*|\[\s*!?[\d\s,:\-]+\])\s*$")


def is_legado_element_rule(selector: str) -> bool:
    """Whether `java.getElements/getElement` input is a Legado rule (AnalyzeRule.getElements) rather than a plain CSS
    selector: `tag.`/`id.`/`class.`/`text.`/`children` steps, `@@`, `@css:`/`@xpath:`/`@json:`, an `@` chain, a `||`/`&&`/`%%`
    combination, or an ElementsSingle index suffix (`.N`, `!N`, `[..]`). Script rules are not element rules."""
    text = (selector or "").strip()
    if not text:
        return False
    low = text.lower()
    if "@js:" in low or "<js>" in low:
        return False
    # R155b (P3-8): `{{...}}` template rules would evaluate nested JS from inside a QuickJS callback; keep the CSS path.
    if "{{" in text:
        return False
    if _LEGADO_ELEMENT_RULE_PREFIX_RE.search(text):
        return True
    if "@" in text or "||" in text or "&&" in text or "%%" in text:
        return True
    return bool(_LEGADO_ELEMENT_RULE_INDEX_RE.search(text))


def select_elements_by_rule_json(rule: str, html: str, runtime: RuleRuntime) -> str:
    """`java.getElements(rule, html)` / `java.getElement`: the validator's own Legado rule evaluator (the same one that
    evaluates a list rule, i.e. AnalyzeRule.getElements) and every hit re-parsed into a bridge row.

    Returns the JSON text `null` when `rule` is not a Legado element rule (or the content is JSON): the JS side then keeps
    its CSS selection."""
    text = (rule or "").strip()
    if not is_legado_element_rule(text) or is_json_content(html or ""):
        return "null"
    if text.startswith("@@"):
        text = text[2:].strip()
    try:
        items = evaluate_book_list(text, html or "", runtime)
    except Exception as _swallowed:
        note_debug_error("java.getElements rule", _swallowed)
        return "[]"
    rows: list[dict[str, str]] = []
    for item in items:
        element = BeautifulSoup(item or "", "html.parser").find(True)
        if element is None:
            continue
        rows.append(_element_row(element, runtime.base_url))
    return json.dumps(rows, ensure_ascii=False)


# 与 Swift XPathEngine.htmlToXHTML() 保持一致的自闭合标签集合
_XHTML_SELF_CLOSING = {
    "br", "hr", "img", "input", "meta", "link",
    "area", "base", "col", "embed", "param", "source", "track", "wbr",
}


def html_to_xhtml(html: str) -> str:
    """将 HTML 转为 XHTML，与 Swift XPathEngine.htmlToXHTML() 行为完全一致：
    1. 自闭合标签补 />
    2. 转义裸 &
    3. 包裹 XML 根元素
    """
    xhtml = html
    for tag in _XHTML_SELF_CLOSING:
        xhtml = re.sub(
            rf"<{tag}(\s[^>]*)?>",
            lambda m, t=tag: f"<{t}{m.group(1) or ''}/>" ,
            xhtml,
            flags=re.I,
        )
    # 转义不属于实体引用的裸 &（与 Swift 正则 &(?![a-zA-Z#][a-zA-Z0-9]*;) 一致）
    xhtml = re.sub(r"&(?![a-zA-Z#][a-zA-Z0-9]*;)", "&amp;", xhtml)
    if not xhtml.lstrip().startswith("<?xml"):
        xhtml = f'<?xml version="1.0" encoding="UTF-8"?><root>{xhtml}</root>'
    return xhtml


_XPATH_FULL_DOCUMENT_MARKERS = ("<html", "<body", "<!doctype", "<?xml")


def xpath_extract(html: str, xpath: str, attr: str | None, base_url: str) -> list[str]:
    """R156 (searchjs X): a list item is a bare HTML fragment and Legado evaluates its field rules with the ELEMENT as context
    (AnalyzeByXPath.kt:12-21), so `/a/p[1]/text()` means `./a/p[1]`. Mirrors the App's `XPathEngine.select` single-slash fragment fallback: when a single-slash
    path finds nothing in a fragment, it is evaluated again relative to the fragment's root element."""
    results = _xpath_extract_document(html, xpath, attr, base_url)
    if results:
        return results
    path = (xpath or "").strip()
    head = (html or "").lstrip()[:2048].lower()
    is_fragment = head.startswith("<") and not any(marker in head for marker in _XPATH_FULL_DOCUMENT_MARKERS)
    if path.startswith("/") and not path.startswith("//") and is_fragment:
        return _xpath_extract_document(html, "." + path, attr, base_url)
    return results


def _xpath_extract_document(html: str, xpath: str, attr: str | None, base_url: str) -> list[str]:
    """XPath 提取 — 与 Swift XPathEngine 双策略完全对齐：
    策略1: HTML→XHTML + lxml.etree（对应 Foundation.XMLParser 严格 XML 路径）
    策略2: lxml.html 降级（对应 SwiftSoup CSS 降级方向）
    """
    try:
        from lxml import etree, html as lxml_html
    except Exception as _swallowed:
        note_debug_error("xpath lxml import", _swallowed)
        return []

    def extract_items(items: list) -> list[str]:
        results: list[str] = []

        def node_text(item: Any) -> str:
            if hasattr(item, "text_content"):
                return item.text_content()
            if hasattr(item, "itertext"):
                try:
                    return "".join(item.itertext())
                except Exception:
                    return str(item)
            return str(item)

        def node_outer_html(item: Any) -> str:
            if isinstance(item, str):
                return item
            try:
                return etree.tostring(item, encoding="unicode", method="html")
            except Exception:
                return str(item)

        def node_inner_html(item: Any) -> str:
            if isinstance(item, str):
                return item
            try:
                return "".join(etree.tostring(child, encoding="unicode", method="html") for child in item)
            except Exception:
                return str(item)

        for item_index, item in enumerate(items):
            if isinstance(item, str) and (getattr(item, "is_text", False) or getattr(item, "is_tail", False)):
                # JsoupXpath 2.x text(): each direct child text node, whitespace
                # normalised like Jsoup TextNode.text(); blank nodes are skipped.
                text_node = re.sub(r"\s+", " ", str(item)).strip()
                if text_node:
                    results.append(text_node)
                continue
            if attr is None:
                results.append(node_text(item))
                continue
            if attr in {"text", "owntext", "textnodes"}:
                results.append(node_text(item))
                continue
            if attr in {"html", "innerhtml"}:
                results.append(node_inner_html(item))
                continue
            if attr == "outerhtml":
                results.append(node_outer_html(item))
                continue
            if hasattr(item, "get"):
                value = item.get(attr)
                if value:
                    # Legado AnalyzeByXPath returns attribute text verbatim; only
                    # AnalyzeRule.getString(isUrl = true) absolutises URLs.
                    results.append(str(value))
        return [x for x in results if x]

    # 策略1：XHTML + lxml.etree（与 Swift Foundation.XMLParser 路径一致）
    try:
        xhtml = html_to_xhtml(html)
        doc_xml = etree.fromstring(xhtml.encode("utf-8", errors="replace"))
        items_xml = doc_xml.xpath(xpath)
        results_xml = extract_items(items_xml)
        if results_xml:
            return results_xml
    except Exception:
        # Strategy 1 failing is expected (XHTML conversion); only a failure of strategy 2 below is reported.
        pass

    # 策略2：lxml.html 降级（与 Swift SwiftSoup CSS 降级方向一致）
    try:
        doc_html = lxml_html.fromstring(html)
        items_html = doc_html.xpath(xpath)
        return extract_items(items_html)
    except Exception as _swallowed:
        note_debug_error("xpath strategy 2 (html)", _swallowed)
        return []


def jsonpath_extract(content: str, path: str, preserve_objects: bool = False) -> list[str]:
    json_text = extract_json(content)
    if not json_text:
        return []
    try:
        obj = json.loads(json_text)
    except Exception as _swallowed:
        note_debug_error("jsonpath json parse", _swallowed)
        return []
    norm_path = path.strip()
    if norm_path and not norm_path.startswith("$") and not norm_path.startswith("["):
        norm_path = "$." + norm_path
    norm_path = re.sub(r"\.\*", "[*]", norm_path)
    try:
        expr = parse_jsonpath_expression(norm_path)
        matches = [m.value for m in expr.find(obj)]
    except Exception as _swallowed:
        note_debug_error("jsonpath expression", _swallowed)
        return []
    results: list[str] = []
    for item in matches:
        results.append(json_container_to_string(item))
    return [x for x in results if x]


def extract_json(content: str) -> str:
    text = content.strip()
    if text.startswith("{") or text.startswith("["):
        return text
    m = re.search(r"(\{[\s\S]+\}|\[[\s\S]+\])", content)
    return m.group(1) if m else ""


def extract_next_data_json(content: str) -> str:
    if not content or "__NEXT_DATA__" not in content:
        return ""
    match = re.search(
        r"""<script\b[^>]*\bid=["']__NEXT_DATA__["'][^>]*>([\s\S]*?)</script>""",
        content,
        flags=re.I,
    )
    if not match:
        return ""
    text = html_unescape(match.group(1)).strip()
    if not is_json_content(text):
        return ""
    try:
        obj = json.loads(text)
    except Exception:
        return text
    if isinstance(obj, dict) and isinstance(obj.get("props"), dict):
        try:
            return json.dumps(obj["props"], ensure_ascii=False)
        except Exception:
            return text
    return text


_JSOUP_SHIM_ELEMENT_KEYS = frozenset({"_html", "_innerHtml", "_parent"})


def json_container_to_string(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("_html"), str) and set(value) <= _JSOUP_SHIM_ELEMENT_KEYS:
        # R156 (searchjs D): a JS-side Jsoup element (the `_readoriJsoupElement` shim) is Legado's Element, whose toString() is its
        # outerHtml - not a JSON object `{"_html": ...}` that the next rule would then parse as JSON.
        return value["_html"]
    if isinstance(value, (dict, list)):
        if not value:
            return ""
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return java_double_string(value)
    return str(value)


def java_double_string(value: float) -> str:
    """Java ``Double.toString`` (json-smart parses decimals/exponents as Double).

    ``9.0`` → "9.0", ``0.0001`` → "1.0E-4", ``1e20`` → "1.0E20"; integers are
    ints in Python and keep their plain text.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "-Infinity" if value < 0 else "Infinity"
    if value == 0:
        return "-0.0" if math.copysign(1.0, value) < 0 else "0.0"
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    decimal_value = Decimal(repr(magnitude))
    if 1e-3 <= magnitude < 1e7:
        text = format(decimal_value.normalize(), "f")
        return sign + (text if "." in text else text + ".0")
    _sign, digit_tuple, exponent = decimal_value.normalize().as_tuple()
    digits = "".join(str(d) for d in digit_tuple) or "0"
    point_position = len(digits) + int(exponent)
    rest = digits[1:] or "0"
    return f"{sign}{digits[0]}.{rest}E{point_position - 1}"


_LEGADO_HTML4_ENTITY_RE = re.compile(r"&(?:#([0-9]+)|#[xX]([0-9a-fA-F]+)|([A-Za-z][A-Za-z0-9]*));")


def _legado_html4_entity(match: re.Match[str]) -> str:
    decimal, hexadecimal, name = match.group(1), match.group(2), match.group(3)
    if name is not None:
        code = _html_entities.name2codepoint.get(name)
        return chr(code) if code is not None else match.group(0)
    try:
        code = int(decimal, 10) if decimal is not None else int(hexadecimal, 16)
        return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else match.group(0)
    except (TypeError, ValueError, OverflowError):
        return match.group(0)


def legado_unescape_html4(text: str) -> str:
    """commons-text `StringEscapeUtils.unescapeHtml4` (Legado AnalyzeRule.getString :314-316 and every `getString` caller):
    only the HTML 4 entity names (`html.entities.name2codepoint`) and numeric references, and every entity needs the
    trailing `;`. Python's `html.unescape` also decodes the HTML5 names and the legacy no-semicolon forms (`&times`,
    `&copy`, `&sect`, `&para`, `&not`...), which turned `...&timestamp=1` into `...×tamp=1` (R157 tocContent A, lc1001).
    The validator follows Legado here. The App's own ContentCleaner.decodeHTMLEntities decodes a smaller table of named entities
    (it does need the `;` as well), so an entity outside that table is decoded by Legado and the validator but not by the App."""
    if "&" not in text:
        return text
    return _LEGADO_HTML4_ENTITY_RE.sub(_legado_html4_entity, text)


def legado_get_string_unescape(value: Any) -> str:
    text = str(value or "")
    if "&" not in text:
        return text
    return legado_unescape_html4(text)


def legado_get_string_value(values: list[str]) -> str:
    text = "\n".join(str(value or "") for value in values)
    if "&" not in text:
        return text
    return legado_unescape_html4(text)


def legado_string_list_values(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        parts = text.splitlines() if "\n" in text or "\r" in text else [text]
        out.extend(part.strip() for part in parts if part.strip())
    return out


def legado_joined_string_list(values: list[str]) -> str:
    return ",".join(legado_string_list_values(values))


def legado_word_count_format(raw: Any) -> str:
    text = str(raw or "")
    if not re.fullmatch(r"-?[0-9]+", text):
        return text
    words = int(text)
    if words <= 0:
        return ""
    if words > 10000:
        value = round(words / 10000, 1)
        formatted = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}\u4e07\u5b57"
    return f"{words}\u5b57"


def field_name_variants(rule: str) -> list[str]:
    trimmed = rule.strip()
    if not trimmed or trimmed.startswith(("$", "@")) or "/" in trimmed or " " in trimmed:
        return []
    lower = trimmed.lower()
    groups = [
        ["name", "bookName", "book_name", "title", "bookTitle", "book_title", "novelName", "novel_name", "novelTitle", "novel_title", "articleName", "article_name", "v_book", "书名", "名称"],
        ["author", "authorName", "author_name", "pen_name", "penName", "bookAuthor", "book_author", "作者", "笔名"],
        ["url", "bookUrl", "book_url", "link", "href", "detailUrl", "detail_url", "bookLink", "book_link", "id", "bookId", "book_id", "链接"],
        ["coverUrl", "cover_url", "cover", "img", "image", "coverImg", "cover_img", "imgUrl", "img_url", "封面", "图片"],
        ["intro", "introduction", "desc", "description", "summary", "brief", "synopsis", "简介", "内容简介", "摘要"],
        ["lastChapter", "last_chapter", "lastChapterName", "last_chapter_name", "latest", "latestChapter", "latest_chapter", "latestChapterName", "latest_chapter_name", "latestChapterTitle", "latest_chapter_title", "latestUpdate", "latest_update", "newChapter", "new_chapter", "newChapterName", "new_chapter_name", "chapterTitle", "chapter_title", "最新章节", "最新"],
        ["tocUrl", "toc_url"],
    ]
    for group in groups:
        if any(x.lower() == lower for x in group):
            return [x for x in group if x != trimmed]
    return []


def _split_rule_operator(rule: str, operator: str) -> list[str]:
    """Split one Legado sequence operator outside balanced or JavaScript regions."""
    parts: list[str] = []
    current = ""
    i = 0
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0
    in_single_quote = False
    in_double_quote = False
    in_js_block = False
    in_at_js = False
    op_len = len(operator)

    while i < len(rule):
        ch = rule[i]

        # Track JS blocks and @js: tails. Everything after a top-level @js:
        # belongs to that SourceRule script, including logical && / ||.
        if not in_single_quote and not in_double_quote and not in_at_js:
            if rule[i:i+4].lower() == "<js>" and not in_js_block:
                in_js_block = True
                current += rule[i:i+4]
                i += 4
                continue
            if rule[i:i+5].lower() == "</js>" and in_js_block:
                in_js_block = False
                current += rule[i:i+5]
                i += 5
                continue
            if (
                not in_js_block
                and depth_paren == 0
                and depth_bracket == 0
                and depth_brace == 0
                and rule[i:i+4].lower() == "@js:"
            ):
                in_at_js = True

        if in_js_block or in_at_js:
            current += ch
            i += 1
            continue

        # Track quotes
        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote

        if not in_single_quote and not in_double_quote:
            if ch == "(":
                depth_paren += 1
            elif ch == ")":
                depth_paren = max(0, depth_paren - 1)
            elif ch == "[":
                depth_bracket += 1
            elif ch == "]":
                depth_bracket = max(0, depth_bracket - 1)
            elif ch == "{":
                depth_brace += 1
            elif ch == "}":
                depth_brace = max(0, depth_brace - 1)

            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                if rule[i:i+op_len] == operator:
                    parts.append(current)
                    current = ""
                    i += op_len
                    continue

        current += ch
        i += 1

    parts.append(current)
    return parts


def _first_rule_sequence(rule: str) -> tuple[str, list[str]] | None:
    """Match RuleAnalyzer: the first top-level separator selects the sequence type."""

    candidates: list[tuple[int, str, list[str]]] = []
    for operator in ("&&", "||", "%%"):
        parts = _split_rule_operator(rule, operator)
        if len(parts) > 1:
            candidates.append((len(parts[0].strip()), operator, parts))
    if not candidates:
        return None
    _offset, operator, parts = min(candidates, key=lambda item: item[0])
    cleaned = [part.strip() for part in parts if part.strip()]
    return (operator, cleaned) if len(cleaned) > 1 else None


# Legado AppPattern.JS_PATTERN / AnalyzeRule.putPattern / evalPattern.
_LEGADO_JS_PATTERN = re.compile(r"<js>([\w\W]*?)</js>|@js:([\w\W]*)", re.I)
_LEGADO_PUT_PATTERN = re.compile(r"@put:(\{[^}]+?\})", re.I)
_LEGADO_RULE_TEMPLATE_PATTERN = re.compile(r"\{\{([\w\W]*?)\}\}")
_LEGADO_RULE_TEMPLATE_INNER = re.compile(r"\{\{\s*(?:@(?!get:)|//)", re.I)


def _split_legado_js_segments(rule: str) -> list[tuple[str, bool, bool]]:
    """Mirror AnalyzeRule.splitSourceRule / Swift RuleParser.splitJavaScriptSegments.

    Every ``<js>…</js>`` and ``@js:…`` cuts the rule; non-JS fragments are
    trimmed and dropped when blank. Returns ``(text, is_js, is_at_js)`` tuples.
    """
    segments: list[tuple[str, bool, bool]] = []
    start = 0
    for match in _LEGADO_JS_PATTERN.finditer(rule):
        if match.start() > start:
            text = rule[start:match.start()].strip()
            if text:
                segments.append((text, False, False))
        if match.group(2) is not None:
            segments.append((match.group(2), True, True))
        else:
            segments.append((match.group(1), True, False))
        start = match.end()
    if len(rule) > start:
        tail = rule[start:].strip()
        if tail:
            segments.append((tail, False, False))
    if not segments:
        return [(rule, False, False)]
    return segments


def _split_top_level_replace(text: str) -> tuple[str, str] | None:
    """First ``##`` outside ``{{…}}`` (Swift RuleParser.rangeOfTopLevelReplace)."""
    if "##" not in text:
        return None
    depth = 0
    i = 0
    while i < len(text) - 1:
        pair = text[i:i + 2]
        if pair == "{{":
            depth += 1
            i += 2
            continue
        if pair == "}}" and depth > 0:
            depth -= 1
            i += 2
            continue
        if pair == "##" and depth == 0:
            return text[:i], text[i + 2:]
        i += 1
    return None


def _extract_legado_put_rules(rule: str) -> tuple[str, list[str]]:
    """Legado splitPutRule: strip every ``@put:{…}`` from any position."""
    if "@put:" not in rule.lower():
        return rule, []
    remaining = rule
    bodies: list[str] = []
    for match in _LEGADO_PUT_PATTERN.finditer(rule):
        remaining = remaining.replace(match.group(0), "")
        bodies.append(match.group(1)[1:-1])
    return remaining, bodies


def is_literal_rule_template(text: str) -> bool:
    """Whether a non-JS SourceRule is a makeUpRule template with inner rules.

    Legado switches such a SourceRule to Mode.Regex: ``{{@…}}`` / ``{{//…}}``
    placeholders are evaluated with getString and the composed text is the
    result. ``@put`` is stripped and ``##`` split first (Swift parseSourceRule).
    """
    raw, _bodies = _extract_legado_put_rules(text or "")
    split = _split_top_level_replace(raw)
    base = split[0] if split is not None else raw
    return _LEGADO_RULE_TEMPLATE_INNER.search(base) is not None or has_legado_expression_template(base)


def has_legado_expression_template(text: str) -> bool:
    """R150 (B11): a SourceRule holding any `{{expr}}` is a makeUpRule TEMPLATE in the App (RuleType.swift:533-536
    templateParts -> `.template`, evaluated by RuleEvaluator.swift:665-722 expandTemplate): literal text plus the value of
    each `{{…}}`, and that whole string is the rule's result — it is never fed to the CSS / XPath / JSONPath engines
    afterwards. `{{$.x}}` / `{{$[…]}}` placeholders keep their dedicated JSON-template handling
    (`is_literal_json_template_rule`), `{{@…}}` / `{{//…}}` are covered by `_LEGADO_RULE_TEMPLATE_INNER`."""
    if "{{" not in (text or ""):
        return False
    for match in _LEGADO_RULE_TEMPLATE_PATTERN.finditer(text):
        if not match.group(1).strip().startswith(("$.", "$[")):
            return True
    return False


def substitute_rule_templates(rule: str, content: str, runtime: RuleRuntime, content_type: str = "html") -> str:
    """Resolve ``{{@rule}}`` / ``{{//xpath}}`` with AnalyzeRule.getString semantics.

    ``{{@@…}}`` keeps its dedicated handler (substitute_makeup_rule_templates).
    """
    text = rule or ""
    if "{{" not in text or _LEGADO_RULE_TEMPLATE_INNER.search(text) is None:
        return text

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if inner.startswith("@@") or inner.lower().startswith("@get:") or not (inner.startswith("@") or inner.startswith("//")):
            return match.group(0)
        values = evaluate_rule(inner, content, runtime, content_type)
        return legado_get_string_value(values)

    return _LEGADO_RULE_TEMPLATE_PATTERN.sub(replace, text)


def _legado_all_in_one_values(patterns: list[str], content: str) -> list[str]:
    """Swift evaluateAllInOne: chained regexes (no DOTALL), whole matches."""
    source = content or ""
    for offset, pattern in enumerate(patterns):
        try:
            regex = re.compile(pattern)
        except re.error:
            return []
        if offset == len(patterns) - 1:
            return [m.group(0) for m in regex.finditer(source) if m.group(0)]
        joined = "".join(m.group(0) for m in regex.finditer(source))
        if not joined:
            return []
        source = joined
    return []


def _legado_all_in_one_patterns(rule: str) -> list[str] | None:
    text = (rule or "").strip()
    if not text.startswith(":") or text[1:].startswith("/"):
        return None
    patterns = [part.strip() for part in text[1:].split("&&") if part.strip()]
    return patterns or None


def _legado_src_binding(src_content: str | None, js_input: str) -> dict[str, Any] | None:
    """R155: Legado binds `src` to the rule's CONTENT (AnalyzeRule.evalJS `bindings["src"] = content`), not to the
    previous segment's result (`result`). evaluate_js defaults `src` to its input, which is only right for the first segment."""
    if src_content is None or src_content == js_input:
        return None
    return {"src": src_content}


def _evaluate_legado_js_segment(
    code: str,
    is_at_js: bool,
    js_input: str,
    runtime: RuleRuntime,
    is_last: bool,
    src_content: str | None = None,
) -> list[str]:
    # R155 (B): Legado SourceRule.makeUpRule (AnalyzeRule.kt:659-705) expands `@get:{key}` in EVERY rule mode, JS
    # included (`'...&bookId=@get:{book}&...'` inside an `@js:` tail is the stored value before the script runs).
    js_code = substitute_variable_references(code.strip(), runtime)
    replace_suffix: str | None = None
    if app_profiles.current_app_profile().js_rule_replace_suffix:
        # R157 (js G5 enjing, 1.1.2): Legado SourceRule.makeUpRule (AnalyzeRule.kt:690-703) cuts `##regex##replacement` off EVERY
        # rule mode, JS included, and getString applies it to the script result (a blank script before it is not run:
        # `<js>##、##,</js>` only replaces in the previous result).
        cut = _split_js_rule_replace_suffix(js_code)
        if cut is not None:
            js_code, replace_suffix = cut[0].strip(), cut[1]
    json_tail = ""
    if is_at_js:
        # Readori compatibility: @js:…@json:… (Legado's @js: swallows the tail).
        at_json = re.search(r"@json:", js_code, flags=re.I)
        if at_json and js_code[: at_json.start()].strip() and js_code[at_json.end():].strip():
            json_tail = js_code[at_json.end():].strip()
            js_code = js_code[: at_json.start()].strip()
    if not js_code and replace_suffix is not None and replace_suffix.split("##", 1)[0]:
        js_result = js_input
    else:
        js_result = try_evaluate_fast_java_aes(js_code, js_input)
        if js_result is None and is_at_js:
            js_result = try_evaluate_fast_toc_item_js(js_code, js_input, runtime)
        if js_result is None:
            js_result = evaluate_js(js_code, js_input, runtime, extra_vars=_legado_src_binding(src_content, js_input))
    if replace_suffix is not None and replace_suffix.split("##", 1)[0]:
        js_result = legado_replace_regex(js_result or "", replace_suffix)
    if json_tail:
        return evaluate_rule(json_tail, js_result, runtime, "json")
    if not js_result:
        return []
    if is_last and is_at_js:
        return js_result_to_list(js_result)
    return [js_result]


_JS_REGEX_KEYWORDS = frozenset(
    ("return", "typeof", "case", "in", "of", "delete", "void", "throw", "new", "else", "do", "instanceof", "yield", "await")
)


def _split_js_rule_replace_suffix(code: str) -> tuple[str, str] | None:
    """Port of the Swift `RuleEvaluator.legadoSplitJavaScriptReplace`: the first `##` outside string / template / regex literals
    and comments -> (stripped script, text after `##`), or None.

    Legado splits the rule text with a plain `rule.split("##")` (AnalyzeRule.kt:690-701), so a `##` inside a JS literal also cuts
    the script there; the literal-aware cut is a superset for working sources and never truncates a valid script. Mirrors the App
    exactly: a regex literal is recognised by the previous significant token (not after `)` / `]` / `}` and not after an
    identifier unless it is a keyword such as `return`), a quote ends at a newline when unterminated, template `${ ... }`
    interpolation nests (a `##` inside an interpolation is code of the template, never a cut)."""
    if "##" not in code:
        return None
    chars = code
    count = len(chars)
    index = 0
    template_depth: list[int] = []
    previous_significant = ""
    previous_word = ""
    last_was_whitespace = False

    def is_word(c: str) -> bool:
        return c.isalpha() or c.isdigit() or c in "_$" or (c.isalnum())

    def regex_allowed_here() -> bool:
        if not previous_significant:
            return True
        if is_word(previous_significant):
            return previous_word in _JS_REGEX_KEYWORDS
        return previous_significant not in ")]}"

    def skip_quoted(quote: str) -> None:
        nonlocal index
        index += 1
        while index < count:
            c = chars[index]
            if c == chr(92):
                index += 2
                continue
            if c == quote:
                index += 1
                return
            if c == chr(10):
                return
            index += 1

    def skip_template_text() -> None:
        nonlocal index
        while index < count:
            c = chars[index]
            if c == chr(92):
                index += 2
                continue
            if c == "`":
                index += 1
                return
            if c == "$" and index + 1 < count and chars[index + 1] == "{":
                index += 2
                template_depth.append(0)
                return
            index += 1

    while index < count:
        c = chars[index]
        after_whitespace = last_was_whitespace
        last_was_whitespace = False
        if not template_depth and c == "#" and index + 1 < count and chars[index + 1] == "#":
            return chars[:index].strip(), chars[index + 2:]
        if c in "'\"":
            skip_quoted(c)
            previous_significant = c
            previous_word = ""
        elif c == "`":
            index += 1
            skip_template_text()
            previous_significant = "`"
            previous_word = ""
        elif c == "/":
            if index + 1 < count and chars[index + 1] == "/":
                while index < count and chars[index] != chr(10):
                    index += 1
            elif index + 1 < count and chars[index + 1] == "*":
                index += 2
                while index + 1 < count and not (chars[index] == "*" and chars[index + 1] == "/"):
                    index += 1
                index = min(count, index + 2)
            elif regex_allowed_here():
                index += 1
                inside_class = False
                while index < count:
                    rc = chars[index]
                    if rc == chr(92):
                        index += 2
                        continue
                    if rc == chr(10):
                        break
                    if rc == "[":
                        inside_class = True
                    elif rc == "]":
                        inside_class = False
                    elif rc == "/" and not inside_class:
                        index += 1
                        break
                    index += 1
                previous_significant = "/"
                previous_word = "x"
            else:
                previous_significant = "/"
                previous_word = ""
                index += 1
        elif c == "{":
            if template_depth:
                template_depth[-1] += 1
            previous_significant = c
            previous_word = ""
            index += 1
        elif c == "}":
            if template_depth:
                depth = template_depth[-1]
                if depth == 0:
                    template_depth.pop()
                    index += 1
                    skip_template_text()
                    previous_significant = "`"
                    previous_word = ""
                    continue
                template_depth[-1] = depth - 1
            previous_significant = c
            previous_word = ""
            index += 1
        else:
            if c.isspace():
                last_was_whitespace = True
                index += 1
                continue
            if is_word(c):
                if not after_whitespace and previous_significant and is_word(previous_significant) and previous_word:
                    previous_word += c
                else:
                    previous_word = c
            else:
                previous_word = ""
            previous_significant = c
            index += 1
    return None


def _list_js_code(code: str) -> str:
    """App RuleEvaluator `.javascript` list case (:1826): the `##regex##replacement` tail of a JS list rule is cut off and the
    replacement is not applied to a list (1.1.2, `js_rule_replace_suffix`)."""
    if app_profiles.current_app_profile().js_rule_replace_suffix:
        cut = _split_js_rule_replace_suffix(code)
        if cut is not None:
            return cut[0]
    return code


_NON_URL_STRING_FIELD: "contextvars.ContextVar[bool]" = contextvars.ContextVar("readori_non_url_string_field", default=False)


@contextlib.contextmanager
def _evaluating_non_url_string_field():
    """The rules evaluated inside are Legado getString(non-isUrl) fields (author, wordCount, lastChapter, intro, coverUrl,
    chapterUrl, ...): the validator reads them with the list-mode evaluator, but a Jsoup miss must behave as getString does."""
    token = _NON_URL_STRING_FIELD.set(True)
    try:
        yield
    finally:
        _NON_URL_STRING_FIELD.reset(token)


_APP_BARE_ATTRIBUTE_RULES = frozenset({
    "href", "src", "text", "html", "innerhtml", "outerhtml", "all", "textnodes", "owntext",
    "name", "title", "id", "class", "value", "content", "alt", "data-src", "data-href", "type", "style", "action", "onclick",
})


def _app_segment_yields_empty_for_next_js(text: str, content_is_json: bool, list_mode: bool) -> bool:
    """RuleEvaluator `segmentYieldsEmptyStringForNextJavaScript`: JSON path / XPath segments always answer "" on a miss; an explicit
    `@css:` / bare-attribute / CSS-shaped segment only in list mode (Jsoup getString answers null, so the JS is skipped - also on
    JSON content, the parser cannot tell them from Legado's isJSON switch); the content-dependent shapes (bare JSONPath such as
    `data.list`, and the Jsoup fallback) in list mode or on JSON content; regex / text / template / variable segments never."""
    base = text.split("##", 1)[0].strip()
    put = base.lower().find("@put:{")
    if put > 0:
        base = base[:put].strip()
    low = base.lower()
    if not base:
        return False
    if base.startswith(("$.", "$[")) or low.startswith(("@json:", "@xpath:")) or base.startswith("/"):
        return True
    if low.startswith(("@css:", "@@")):
        return list_mode
    if low.startswith(("@get:", ":", "@put:")) or "{{" in base:
        return False
    if looks_like_bare_jsonpath(base):
        return list_mode or content_is_json
    if low.startswith("@") and low[1:] in _APP_BARE_ATTRIBUTE_RULES:
        return list_mode
    if low in _APP_BARE_ATTRIBUTE_RULES or looks_like_css_selector(base):
        return list_mode
    return list_mode or content_is_json


def _selector_miss_continues_into_js(
    segments: list[tuple[str, bool, bool]], offset: int, content_is_json: bool, list_mode: bool
) -> bool:
    """R156 (tocContent D, App profile 1.1.2): Legado AnalyzeRule.getString / getStringList only `continue` on a null result, so the
    JS segment after a segment that matched nothing still runs, with an empty input, when that segment answered "" / an empty list
    instead of null. Mirrors the App's `segmentYieldsEmptyStringForNextJavaScript` classification (`_app_segment_yields_empty_for_next_js`);
    a getString field of a list item (`_NON_URL_STRING_FIELD`) is evaluated with `list_mode` False."""
    if not app_profiles.current_app_profile().json_miss_runs_following_js:
        return False
    if offset + 1 >= len(segments) or not segments[offset + 1][1]:
        return False
    text, is_js, _is_at_js = segments[offset]
    if is_js:
        return False
    effective_list_mode = bool(list_mode) and not _NON_URL_STRING_FIELD.get()
    return _app_segment_yields_empty_for_next_js(text, content_is_json, effective_list_mode)


def _evaluate_legado_js_pipeline(
    segments: list[tuple[str, bool, bool]],
    literal_flags: list[bool],
    content: str,
    runtime: RuleRuntime,
    content_type: str,
    string_mode: bool = False,
) -> list[str]:
    """Swift RuleType.combine: each segment's output feeds the next.

    A JavaScript segment after another segment receives the previous result
    as one ``\\n``-joined string (AnalyzeRule.getString semantics). In
    getString mode every segment yields one joined string (Swift
    evaluateRuleForString).
    """
    # Intermediate stages keep handing parseable JSON to the next stage; only
    # the last stage renders JSONPath objects as Legado getString text.
    object_mode = legado_object_string_mode()
    if string_mode:
        text_value = content
        for offset, (text, is_js, is_at_js) in enumerate(segments):
            is_last = offset == len(segments) - 1
            previous_mode = set_legado_object_string_mode(object_mode and is_last)
            try:
                if is_js:
                    values = _evaluate_legado_js_segment(text, is_at_js, text_value, runtime, is_last, content)
                else:
                    if offset == 0:
                        item_type = content_type
                    else:
                        item_type = "json" if is_json_content(text_value) else content_type
                    if literal_flags[offset]:
                        values = _evaluate_legado_source_rule(text, text_value, runtime, item_type, True, True)
                    else:
                        values = evaluate_rule(text, text_value, runtime, item_type, string_mode=True)
            finally:
                set_legado_object_string_mode(previous_mode)
            if not values:
                if _selector_miss_continues_into_js(segments, offset, content_type == "json" or is_json_content(content), False):
                    text_value = ""
                    continue
                return []
            text_value = "\n".join(values)
        return [text_value]
    current = [content]
    for offset, (text, is_js, is_at_js) in enumerate(segments):
        is_last = offset == len(segments) - 1
        previous_mode = set_legado_object_string_mode(object_mode and is_last)
        try:
            if is_js:
                js_input = content if offset == 0 else "\n".join(current)
                current = _evaluate_legado_js_segment(text, is_at_js, js_input, runtime, is_last, content)
            else:
                next_values: list[str] = []
                for item in current:
                    if offset == 0:
                        item_type = content_type
                    elif offset == 1 and segments[0][1]:
                        item_type = "json" if is_json_content(item) or extract_json(item) else content_type
                    else:
                        item_type = "json" if is_json_content(item) else content_type
                    if literal_flags[offset]:
                        next_values.extend(_evaluate_legado_source_rule(text, item, runtime, item_type, True))
                    else:
                        next_values.extend(evaluate_rule(text, item, runtime, item_type))
                current = next_values
        finally:
            set_legado_object_string_mode(previous_mode)
        if not current:
            if _selector_miss_continues_into_js(segments, offset, content_type == "json" or is_json_content(content), True):
                current = [""]
                continue
            break
    return current


def _evaluate_legado_source_rule(
    rule: str,
    content: str,
    runtime: RuleRuntime,
    content_type: str,
    literal_template: bool,
    string_mode: bool = False,
) -> list[str]:
    """One non-JS SourceRule: AllInOne → @put → ``##`` → template → engine.

    ``string_mode`` is AnalyzeRule.getString: ``##`` acts once on the
    ``\\n``-joined base result; list mode (getStringList) replaces per item.
    """
    t = rule.strip()
    patterns = _legado_all_in_one_patterns(t)
    if patterns is not None:
        return _legado_all_in_one_values(patterns, content)

    remaining, put_bodies = _extract_legado_put_rules(t)
    if put_bodies:
        base = remaining.strip()
        if base or len(put_bodies) > 1:
            first_values: list[str] | None = None
            for body in put_bodies:
                values = evaluate_single_rule("@put:{" + body + "}", content, runtime, content_type)
                if first_values is None:
                    first_values = values
            if not base:
                return first_values or []
            return _evaluate_legado_source_rule(base, content, runtime, content_type, literal_template, string_mode)

    split = _split_top_level_replace(t)
    if split is not None:
        base, chain = split
        base = base.strip()
        if not base:
            base_values = [content]
        elif literal_template:
            base_values = [_literal_template_with_result(base, content)]
        else:
            base_values = evaluate_rule(base, content, runtime, content_type, string_mode=string_mode)
        if not chain:
            return base_values
        if string_mode:
            return [legado_replace_regex("\n".join(base_values), chain)] if base_values else []
        return [legado_replace_regex(value, chain) for value in base_values]

    if literal_template:
        t = _literal_template_with_result(t, content)
        return [t] if t else []
    return _evaluate_rule_sequence(t, content, runtime, content_type)


def _literal_template_with_result(text: str, content: str) -> str:
    """RuleEvaluator.swift:689-692 evaluateTemplateExpression: `{{result}}` is the template's input (the previous
    segment's output / the item), the one placeholder the whole-rule pre-pass cannot know."""
    if "{{" not in text:
        return text
    return _LEGADO_RULE_TEMPLATE_PATTERN.sub(
        lambda match: content if match.group(1).strip() == "result" else match.group(0), text
    )


def legado_absolute_url(relative_path: str, base_url: str) -> str:
    """NetworkUtils.getAbsoluteURL(redirectUrl, url) as used by getString(isUrl = true)."""
    relative = (relative_path or "").strip()
    if not base_url:
        return relative
    base_text = base_url.split(",", 1)[0].strip()
    if not urlparse(base_text).scheme:
        return relative
    lower = relative.lower()
    if lower.startswith(("http://", "https://", "data:")):
        return relative
    if lower.startswith("javascript"):
        return ""
    try:
        return urljoin(base_text, relative)
    except Exception:
        return relative


def evaluate_is_url_string(rule: str, content: str, runtime: RuleRuntime, content_type: str = "html") -> str:
    """AnalyzeRule.getString(rule, isUrl = true): first value; blank -> baseUrl."""
    values = evaluate_rule(rule, content, runtime, content_type)
    raw = legado_get_string_unescape(values[0]) if values else ""
    if not raw.strip():
        return runtime.base_url
    return legado_absolute_url(raw, runtime.base_url)


def evaluate_is_url_list(rule: str, content: str, runtime: RuleRuntime, content_type: str = "html") -> list[str]:
    """AnalyzeRule.getStringList(rule, isUrl = true): absolutise, drop empty, dedupe."""
    urls: list[str] = []
    for raw in evaluate_rule(rule, content, runtime, content_type):
        absolute = legado_absolute_url(raw, runtime.base_url)
        if absolute and absolute not in urls:
            urls.append(absolute)
    return urls


def evaluate_get_string(rule: str, content: str, runtime: RuleRuntime, content_type: str = "html") -> str:
    """AnalyzeRule.getString (non-isUrl) / Swift RuleEvaluator.evaluateGetString.

    The base rule is evaluated as a list and joined with ``\\n``; ``##`` runs
    once on the joined text (anchors and ``###`` act on the whole string);
    HTML entities are unescaped last.
    """
    return legado_get_string(rule, content, runtime, content_type, unescape=True)


def legado_get_string(
    rule: str,
    content: str,
    runtime: RuleRuntime,
    content_type: str = "html",
    unescape: bool = True,
) -> str:
    """AnalyzeRule.getString(ruleList, unescape): list joined with "\\n", `##`
    once, JSONPath objects rendered as ``{k=v}``; optional HTML unescape."""
    if not (rule or "").strip():
        return ""
    previous_mode = set_legado_object_string_mode(True)
    try:
        values = evaluate_rule(rule, content, runtime, content_type, string_mode=True)
    finally:
        set_legado_object_string_mode(previous_mode)
    text = "\n".join(str(value or "") for value in values)
    if unescape and "&" in text:
        return legado_unescape_html4(text)
    return text


def evaluate_rule(
    rule: str,
    content: str,
    runtime: RuleRuntime,
    content_type: str = "html",
    string_mode: bool = False,
) -> list[str]:
    raw_rule = rule or ""
    # R156 (searchjs A / tocContent JS##): Legado SourceRule.makeUpRule (AnalyzeRule.kt:708-719) cuts `##regex##replacement` off
    # EVERY source rule - JS included - and applies it to the result: `@js:code##regex##to`. The validator used to hand the
    # whole text to the JS engine ("invalid first character of private name").
    _js_start = re.search(r"@js:", raw_rule, re.I)
    if _js_start is not None and "##" in raw_rule[_js_start.end():]:
        _js_replace = _split_top_level_replace(raw_rule[_js_start.end():])
        if _js_replace is not None and _js_replace[0].strip():
            _js_values = evaluate_rule(raw_rule[:_js_start.end()] + _js_replace[0], content, runtime, content_type, string_mode)
            if not _js_values:
                return []
            if string_mode:
                return [legado_replace_regex("\n".join(_js_values), _js_replace[1])]
            return [legado_replace_regex(_value, _js_replace[1]) for _value in _js_values]
    # R156 (tocContent N): `@Json:` only selects the JSONPath mode and AnalyzeByJSonPath.getString then expands every inline
    # `{$.x}` (innerRule), so `@Json:{$.a}|{$.b}` / `@json:/b/{$.name}` is the text template `a|b` / `/b/name`.
    _json_prefix = re.match(r"\s*@json:", raw_rule, re.I)
    if _json_prefix is not None and is_literal_json_template_rule(raw_rule[_json_prefix.end():]):
        raw_rule = raw_rule[_json_prefix.end():]
    literal_json_template = is_literal_json_template_rule(raw_rule)
    raw_segments = _split_legado_js_segments(raw_rule.strip())
    # Legado makeUpRule / RuleEvaluator resolves JSONPath-shaped placeholders
    # against the current item before generic {{...}} JavaScript/template
    # expansion. Doing this after resolve_template causes multi-placeholder
    # strings such as "{{$.source}} {{$.last_chapter_title}}" to be consumed as
    # invalid JS expressions and collapse to an empty rule.
    rule = substitute_json_placeholders(raw_rule, content)
    rule = substitute_rule_templates(rule, content, runtime, content_type)
    rule = resolve_template(rule, runtime, rule_template=True, input_text=content)
    rule = substitute_makeup_rule_templates(rule, content, runtime, content_type)
    if literal_json_template:
        rule = substitute_variable_references(rule, runtime)
        return [rule] if rule else []
    if not rule.strip():
        return []
    stripped = rule.strip()
    segments = _split_legado_js_segments(stripped)
    if len(segments) == len(raw_segments):
        # R157 (tocContent F): Legado SourceRule.init (AnalyzeRule.kt:587-593) turns ANY `{{..}}` SourceRule, JSONPath
        # placeholders `{{$.x}}` included, into Mode.Regex, i.e. the expanded text is the segment's result.
        literal_flags = [
            (not raw_is_js) and (is_literal_rule_template(raw_text) or _has_double_brace_json_placeholder(raw_text))
            for raw_text, raw_is_js, _raw_at in raw_segments
        ]
    else:
        literal_flags = [False] * len(segments)
    has_js = any(is_js for _text, is_js, _at in segments)
    if (
        has_js
        and len(segments) == 1
        and not stripped.startswith("+")
        and app_profiles.current_app_profile().js_rule_replace_suffix
        and _split_js_rule_replace_suffix(segments[0][0]) is not None
    ):
        # R157 (js G5, 1.1.2): a lone `@js:` / `<js>` rule with a `##regex##replacement` suffix goes through the segment pipeline,
        # which cuts the suffix off and applies it to the script result.
        return _evaluate_legado_js_pipeline(segments, literal_flags, content, runtime, content_type, string_mode)
    if has_js and (len(segments) == 1 or stripped.startswith("+")):
        # A lone JavaScript rule (or Legado's "+" list prefix) keeps its
        # dedicated fast paths.
        return evaluate_single_rule(rule, content, runtime, content_type)
    if len(segments) > 1:
        return _evaluate_legado_js_pipeline(segments, literal_flags, content, runtime, content_type, string_mode)
    return _evaluate_legado_source_rule(stripped, content, runtime, content_type, literal_flags[0], string_mode)


def _evaluate_rule_sequence(rule: str, content: str, runtime: RuleRuntime, content_type: str = "html") -> list[str]:
    sequence = _first_rule_sequence(rule)
    if sequence is not None:
        operator, parts = sequence
        groups = [evaluate_rule(part, content, runtime, content_type) for part in parts]
        if operator == "||":
            return next((values for values in groups if values), [])
        if operator == "&&":
            return [value for values in groups for value in values]
        nonempty = [values for values in groups if values]
        if not nonempty:
            return []
        return [
            values[index]
            for index in range(len(nonempty[0]))
            for values in nonempty
            if index < len(values)
        ]
    return evaluate_single_rule(rule, content, runtime, content_type)


_BARE_HTML_ATTRIBUTE_RULES = frozenset({
    "href", "src", "text", "html", "innerhtml", "outerhtml", "textnodes",
    "owntext", "name", "title", "id", "class", "value", "content",
    "alt", "data-src", "data-href", "type", "style", "action", "onclick",
})


def is_legado_json_text(content: str) -> bool:
    """Legado String.isJson(): trimmed text is `{…}` or `[…]` (AnalyzeRule.isJSON)."""
    t = (content or "").strip()
    return (t.startswith("{") and t.endswith("}")) or (t.startswith("[") and t.endswith("]"))


def evaluate_single_rule(rule: str, content: str, runtime: RuleRuntime, content_type: str = "html") -> list[str]:
    t = rule.strip()
    # A rule that carries `@get:{key}` is a makeUpRule template (RuleType `.template`): its substituted text is the
    # result, exactly like Legado (differential fixture rule.template.get.variable: `x@get:{k}` -> `xv`).
    template_literal = False
    if "@get:{" in t and not (t.startswith("@get:{") and t.endswith("}")):
        substituted_rule = substitute_variable_references(t, runtime)
        template_literal = substituted_rule != t
        t = substituted_rule
    if not t:
        return []

    # Legado allows a leading cleanup JS block followed by another rule, e.g.
    # <js>stripJsonp(result)</js>@json:$..bookinfo[*]
    # The JS output becomes the input for the tail rule.
    if t.lower().startswith("<js>"):
        js_match = re.match(r"<js>([\s\S]*?)</js>([\s\S]*)$", t, re.I)
        if js_match:
            js_code = js_match.group(1).strip()
            tail = js_match.group(2).strip()
            js_result = evaluate_js(js_code, content, runtime)
            if tail:
                return evaluate_rule(tail, js_result, runtime, "json" if is_json_content(js_result) or extract_json(js_result) else content_type)
            return js_result_to_list(js_result)

    # Handle inline <js>...</js> blocks that appear AFTER a base rule
    # e.g. "$.chapter.htmlContent<js>result.replace(/<.*?>/g,'')</js>"
    if "<js>" in t and "</js>" in t and not t.lower().startswith(("<js>", "+<js>")):
        js_match = re.search(r"<js>([\s\S]*?)</js>", t, re.I)
        if js_match:
            base = t[:js_match.start()].strip()
            js_code = js_match.group(1).strip()
            after_js = t[js_match.end():].strip()
            # Evaluate base rule first
            if base:
                base_results = evaluate_rule(base, content, runtime, content_type)
                out: list[str] = []
                for item in base_results:
                    js_result = try_evaluate_fast_java_aes(js_code, item)
                    if js_result is None:
                        js_result = evaluate_js(js_code, item, runtime, extra_vars=_legado_src_binding(content, item))
                    if js_result:
                        if after_js:
                            out.extend(evaluate_rule(after_js, js_result, runtime, content_type))
                        else:
                            out.append(js_result)
                return out
            else:
                return js_result_to_list(evaluate_js(js_code, content, runtime))

    if t.lower().startswith("+<js>") and t.lower().endswith("</js>"):
        return js_result_to_list(evaluate_js(t[5:-5], content, runtime))

    low = t.lower()
    if low.startswith("+@js:"):
        body = t[5:]
        at_json = re.search(r"@json:", body, flags=re.I)
        if at_json:
            js_part = body[: at_json.start()].strip()
            tail = body[at_json.end() :].strip()
            intermediate = evaluate_js(js_part, content, runtime)
            return evaluate_rule(tail, intermediate, runtime, "json")
        fast = try_evaluate_fast_toc_item_js(body, content, runtime)
        return js_result_to_list(fast if fast is not None else evaluate_js(body, content, runtime))
    if low.startswith("@js:"):
        body = t[4:]
        at_json = re.search(r"@json:", body, flags=re.I)
        if at_json:
            js_part = body[: at_json.start()].strip()
            tail = body[at_json.end() :].strip()
            intermediate = evaluate_js(js_part, content, runtime)
            return evaluate_rule(tail, intermediate, runtime, "json")
        fast = try_evaluate_fast_toc_item_js(body, content, runtime)
        return js_result_to_list(fast if fast is not None else evaluate_js(body, content, runtime))

    if "@js:" in low and not low.startswith("@js:"):
        idx = low.index("@js:")
        base = t[:idx].strip()
        js_tail = t[idx + 4 :].strip()
        intermediate = evaluate_rule(base, content, runtime, content_type)
        out: list[str] = []
        for item in intermediate:
            js_result = try_evaluate_fast_java_aes(js_tail, item)
            if js_result is None:
                js_result = evaluate_js(js_tail, item, runtime, extra_vars=_legado_src_binding(content, item))
            out.extend(js_result_to_list(js_result))
        return out

    if low.startswith("@css:"):
        return evaluate_explicit_css_rule(t[5:], content, runtime)
    if low.startswith("@xpath:"):
        return evaluate_xpath_rule(t[7:], content, runtime, content_type)
    if low.startswith("@json:"):
        return evaluate_json_rule(t[6:], content, runtime)
    if t.startswith("@get:{") and t.endswith("}"):
        return [runtime.variables.get(t[6:-1], "")] if (runtime.variables or {}).get(t[6:-1]) else []

    if t.startswith("@put:{") and t.endswith("}"):
        body = t[6:-1]
        def normalize_put_subrule(raw: str) -> str:
            value = raw.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                return value[1:-1]
            return value

        if ":" in body:
            if body.lstrip().startswith(("{", "'\"", '"')):
                try:
                    parsed = json.loads("{" + body + "}")
                    out: list[str] = []
                    for key, subrule in parsed.items():
                        values = evaluate_rule(normalize_put_subrule(str(subrule)), content, runtime, content_type)
                        value = values[0] if values else ""
                        runtime.variables = runtime.variables or {}
                        runtime.variables[key] = value
                        out.append(value)
                    return out
                except Exception as _swallowed:
                    note_debug_error("rule @put", _swallowed)
                    pass
            pairs = [part.strip() for part in body.split(",") if part.strip()]
            if len(pairs) > 1 and all(":" in part for part in pairs):
                out: list[str] = []
                runtime.variables = runtime.variables or {}
                for part in pairs:
                    key, subrule = part.split(":", 1)
                    key = key.strip().strip("'\"")
                    values = evaluate_rule(normalize_put_subrule(subrule), content, runtime, content_type)
                    value = values[0] if values else ""
                    runtime.variables[key] = value
                    out.append(value)
                return [value for value in out if value]
            key, subrule = body.split(":", 1)
            values = evaluate_rule(normalize_put_subrule(subrule), content, runtime, content_type)
            value = values[0] if values else ""
            runtime.variables = runtime.variables or {}
            runtime.variables[key] = value
            return [value] if value else []
        runtime.variables = runtime.variables or {}
        runtime.variables[body] = content
        return [content]

    if t.startswith("@") and not t.startswith("@@"):
        inner = t[1:].strip()
        if inner:
            return evaluate_rule(inner, content, runtime, content_type)
    # Legado makeUpRule expands only double-brace placeholders; `{result}` is literal.
    if "{{result}}" in t:
        t = t.replace("{{result}}", content)
    if low.startswith("text."):
        return evaluate_css_rule(t, content, runtime, content_type)
    # A newline-delimited nextContentUrl/nextTocUrl value is a URL list, not
    # one XPath expression. Preserve it as one result so the caller can apply
    # Legado getStringList newline splitting before resolving each URL.
    # R144a (F6/#13): the App never returns rule text itself for HTML content. A default-mode rule that matches
    # nothing is a JSoup selector with no result (RuleEvaluator `.contentDependent`: "字面量形态绝不把规则文本当
    # 结果"); only JSON content keeps the literal (`.text` rule, bare field names go through JSONPath).
    literal_allowed = template_literal or is_json_content(content) or (
        content_type == "json" and not (content or "").lstrip().startswith("<")
    )
    if "\n" in t and literal_allowed:
        literal_lines = [line.strip() for line in t.splitlines() if line.strip()]
        if literal_lines and all(
            line.startswith(("http://", "https://", "/", "./", "../"))
            for line in literal_lines
        ):
            return [t]
    # Legado SourceRule dispatches every single leading slash rule to XPath,
    # except that the isJSON check comes first: on JSON content a bare `/`
    # rule (not @XPath:) is a Jayway path, which practically never matches.
    if t.startswith("/"):
        # R144a (F14): the App splits `##` off every rule before choosing the mode (RuleType.swift parseSourceRule),
        # so `/xpath##regex##replace` runs the XPath on the part before `##` and the replace chain on its result.
        xpath_split = _split_top_level_replace(t)
        if xpath_split is not None:
            xpath_base, xpath_chain = xpath_split
            xpath_base = xpath_base.strip()
            if xpath_base.startswith("/"):
                if is_legado_json_text(content):
                    xpath_values = evaluate_json_rule(xpath_base, content, runtime)
                else:
                    xpath_values = evaluate_xpath_rule(xpath_base, content, runtime, content_type)
                if not xpath_chain:
                    return xpath_values
                return [legado_replace_regex(value, xpath_chain) for value in xpath_values]
        if is_legado_json_text(content):
            return evaluate_json_rule(t, content, runtime)
        return evaluate_xpath_rule(t, content, runtime, content_type)
    if contains_json_placeholder(t) and t.startswith(("http://", "https://", "/", "./", "../")):
        substituted = substitute_json_placeholders(t, content)
        return [substituted] if substituted else []
    if t.startswith(("http://", "https://", "/", "./", "../")) and "##" not in t:
        return [t] if literal_allowed else []
    # ## separates rule from regex replacement (legado: ruleStr.split("##"))
    # Must check before CSS to avoid CSS evaluating the ## fragment
    if "##" in t:
        base, replace_chain = t.split("##", 1)
        base_values = [content] if not base.strip() else evaluate_rule(base, content, runtime, content_type)
        return [legado_replace_regex(value, replace_chain) for value in base_values]
    if "@put:{" in t and not t.startswith("@put:{"):
        base, put_rule = t.split("@put:{", 1)
        base = base.strip()
        put_rule = "@put:{" + put_rule.strip()
        if base and put_rule.endswith("}"):
            evaluate_rule(put_rule, content, runtime, content_type)
            return evaluate_rule(base, content, runtime, content_type)
    if is_json_content(content):
        json_values = evaluate_json_rule(t, content, runtime)
        if json_values:
            return json_values
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t) and t.lower() not in _BARE_HTML_ATTRIBUTE_RULES:
            # Legado Json mode: a missing bare field is empty, never the
            # field name itself and never an alias.
            return []
    if looks_like_css_selector(t):
        return evaluate_css_rule(t, content, runtime, content_type)
    if t.startswith(("$.", "$[")) or t.startswith("$.."):
        return evaluate_json_rule(t, content, runtime)
    if looks_like_bare_jsonpath(t):
        if not is_json_content(content):
            # R149 (B2): RuleType.swift:911-925 makes this `.contentDependent(json:, css:)` - on HTML the rule is a
            # JSoup selector (`ul.xbk`, `a.title`); JSONPath is only the fallback for markup that embeds JSON.
            css_values = evaluate_css_rule(t, content, runtime, content_type)
            if css_values:
                return css_values
        return evaluate_json_rule(t, content, runtime)

    if t.startswith("{{") and t.endswith("}}") and "\n" not in t:
        inner = t[2:-2]
        if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", inner):
            val = (runtime.variables or {}).get(inner, "")
            return [val] if val else []
        result = evaluate_js(f"return ({inner});", content, runtime)
        return [result] if result else []

    if t.startswith(":") and not t.startswith("://"):
        inner = t[1:]
        parts = inner.split(":")
        pattern = parts[0] if parts else ""
        group = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return regex_extract(pattern, group, content)
    if t.startswith("@@"):
        return evaluate_rule(t[2:], content, runtime, content_type)

    bare_attrs = _BARE_HTML_ATTRIBUTE_RULES
    if t.startswith("@"):
        attr = t[1:].strip()
        if attr.lower() in bare_attrs:
            return _evaluate_bare_html_attribute(content, attr, runtime)
    if t.lower() in bare_attrs:
        return _evaluate_bare_html_attribute(content, t, runtime)

    # R158 (huarenv5): Legado Default mode on non-JSON content hands EVERY remaining rule to AnalyzeByJSoup (App RuleType.parseSingle:
    # "标签白名单只是快速通道，不再是 CSS 的门槛"; `.contentDependent(json:, css:)`), so `title@ownText` is `select("title")` + ownText even
    # though `title` is not in the tag fast-path list. Only `@` chains are routed here; the first / middle segments are selectors.
    if "@" in t and not t.startswith("@") and not is_json_content(content):
        css_values = evaluate_css_rule(t, content, runtime, content_type)
        if css_values:
            return css_values

    return [t] if literal_allowed else []


def _apply_css_index(elems: list[Any], index: int | None) -> list[Any]:
    if index is None:
        return elems
    if index <= -1000:
        exclude = -(index + 1000)
        return [e for i, e in enumerate(elems) if i != exclude]
    actual = index if index >= 0 else len(elems) + index
    if actual < 0 or actual >= len(elems):
        return []
    return [elems[actual]]


def _parse_css_index_selection(
    part: str,
) -> tuple[str, bool, list[tuple[str, int | None, int | None, int]]] | None:
    text = (part or "").strip()
    bracket = re.search(r"\[(!?)([\d\s,:\-]*)\]$", text)
    if bracket:
        excludes = bracket.group(1) == "!"
        terms: list[tuple[str, int | None, int | None, int]] = []
        for raw_token in bracket.group(2).split(","):
            token = raw_token.strip()
            if not token:
                continue
            if ":" in token:
                fields = token.split(":")
                if len(fields) not in (2, 3):
                    return None
                try:
                    start = int(fields[0].strip()) if fields[0].strip() else None
                    end = int(fields[1].strip()) if fields[1].strip() else None
                    step = int(fields[2].strip()) if len(fields) == 3 and fields[2].strip() else 1
                except ValueError:
                    return None
                terms.append(("range", start, end, step))
            elif re.fullmatch(r"-?\d+", token):
                terms.append(("index", int(token), None, 1))
            else:
                return None
        if terms:
            return text[: bracket.start()].strip(), excludes, terms

    legacy = re.search(r"([.!])\s*(-?\d+(?:\s*:\s*-?\d+\s*)*)$", text)
    if legacy:
        indexes = [int(value.strip()) for value in legacy.group(2).split(":")]
        return (
            text[: legacy.start()].strip(),
            legacy.group(1) == "!",
            [("index", index, None, 1) for index in indexes],
        )
    return None


def _apply_css_index_selection(
    elems: list[Any],
    selection: tuple[bool, list[tuple[str, int | None, int | None, int]]] | None,
) -> list[Any]:
    if selection is None:
        return elems
    excludes, terms = selection
    length = len(elems)
    if length == 0:
        return []
    ordered: list[int] = []
    seen: set[int] = set()

    def append(index: int) -> None:
        if 0 <= index < length and index not in seen:
            seen.add(index)
            ordered.append(index)

    for kind, raw_start, raw_end, raw_step in terms:
        if kind == "index":
            assert raw_start is not None
            append(raw_start + length if raw_start < 0 else raw_start)
            continue
        start = 0 if raw_start is None else raw_start
        end = length - 1 if raw_end is None else raw_end
        if start < 0:
            start += length
        if end < 0:
            end += length
        if (start < 0 and end < 0) or (start >= length and end >= length):
            continue
        start = min(max(start, 0), length - 1)
        end = min(max(end, 0), length - 1)
        if start == end or raw_step >= length:
            append(start)
            continue
        if raw_step > 0:
            step = raw_step
        elif -raw_step < length:
            step = raw_step + length
        else:
            step = 1
        step = max(1, step)
        if end > start:
            index = start
            while index <= end:
                append(index)
                index += step
        else:
            index = start
            while index >= end:
                append(index)
                index -= step

    if excludes:
        excluded = set(ordered)
        return [elem for index, elem in enumerate(elems) if index not in excluded]
    return [elems[index] for index in ordered]


_CSS_RESULT_ATTRIBUTE_WORDS = frozenset({
    "text", "html", "innerhtml", "outerhtml", "textnodes", "owntext",
    "href", "src", "data-src", "data-href", "alt", "class", "id", "content",
    "name", "value", "title", "type", "onclick", "action", "style", "all",
})


def parse_css_chain_part(
    part: str,
) -> tuple[tuple[str, str], tuple[bool, list[tuple[str, int | None, int | None, int]]] | None]:
    text = (part or "").strip()
    parsed = _parse_css_index_selection(text)
    selection = None
    if parsed is not None:
        text, excludes, terms = parsed
        selection = (excludes, terms)

    if not text or text == "children" or text.startswith("children."):
        return ("children", ""), selection

    # AnalyzeByJSoup.ElementsSingle dispatches these prefixes
    # case-sensitively and consumes only rules[1] from split(".").
    fields = text.split(".")
    if len(fields) > 1 and fields[0] in {"class", "tag", "id", "text"}:
        return (fields[0], fields[1]), selection

    # R158 (huarenv5): a chain segment is always a Jsoup `select(segment)` (AnalyzeByJSoup.getResultList: only the LAST `@` segment is a
    # result name; App `RuleType.isSelectorLikeSegment` is only asked about the last one). `parse_css_rule` pops a trailing
    # attribute-named part as the result attribute, which turned a lone `title` / `name` / `content` segment into `*` plus an attribute.
    if "@" not in text and re.fullmatch(r"[A-Za-z][\w-]*", text) and (text.lower() in _CSS_RESULT_ATTRIBUTE_WORDS or text.lower().startswith("data-")):
        return ("css", text), selection

    selector, _attr, _index = parse_css_rule(text)
    return ("css", selector), selection


def _css_elements_including_self(node: Any) -> list[Any]:
    elements: list[Any] = []
    if getattr(node, "name", None) not in (None, "[document]"):
        elements.append(node)
    elements.extend(node.find_all(True))
    return elements


def _css_element_has_class(element: Any, wanted_casefolded: str) -> bool:
    """Jsoup/SwiftSoup `Element.hasClass(className)` as used by `getElementsByClass` (RuleEvaluator.swift:1005-1010).

    A class name that itself contains whitespace (`class.a b c`) is compared with the WHOLE `class` attribute
    (case-insensitive equality); a single-token name matches any whitespace separated token. It is never split into
    several tokens that must all be present. SwiftSoup's source is not available offline; this follows Jsoup's
    `hasClass` (equal length -> `equalsIgnoreCase(classAttr)`, otherwise whitespace-delimited region scan). BeautifulSoup
    keeps `class` as a token list, so the attribute is rebuilt with single spaces (irregular whitespace inside the
    original attribute is therefore treated as a single space)."""
    classes = element.get("class", [])
    if isinstance(classes, str):
        classes = classes.split()
    if not classes or not wanted_casefolded:
        return False
    tokens = [str(name) for name in classes]
    if any(token.casefold() == wanted_casefolded for token in tokens):
        return True
    if any(ch.isspace() for ch in wanted_casefolded):
        return " ".join(tokens).casefold() == wanted_casefolded
    return False


def _css_node_matches_selector(node: Any, selector: str) -> bool:
    """Whether the context element itself matches `selector` (Jsoup select() includes the root element)."""
    if getattr(node, "name", None) in (None, "[document]"):
        return False
    try:
        css_namespace = getattr(node, "css", None)  # bs4 >= 4.12
        if css_namespace is not None:
            return bool(css_namespace.match(selector))
        import soupsieve
        return bool(soupsieve.match(selector, node))
    except Exception as _swallowed:
        note_debug_error("css self match", _swallowed)
        return False


def _insert_implicit_tbody(soup: Any) -> None:
    """Local stand-in for the HTML5 tree builder's implicit `<tbody>` (Jsoup/SwiftSoup wrap the `<tr>` children of a
    `<table>` in a `<tbody>`; html.parser does not). Only called when the rule mentions `tbody`, so the common path
    pays nothing; each run of direct `<tr>` children of a table is wrapped in a new `<tbody>`."""
    try:
        tables = list(soup.find_all("table"))
        for table in tables:
            run: list[Any] = []
            runs: list[list[Any]] = []
            for child in list(table.children):
                child_name = getattr(child, "name", None)
                if child_name == "tr":
                    run.append(child)
                    continue
                if child_name is None and not str(child).strip():
                    # whitespace between rows keeps the current run open
                    if run:
                        run.append(child)
                    continue
                if run:
                    runs.append(run)
                    run = []
            if run:
                runs.append(run)
            for rows in runs:
                while rows and getattr(rows[-1], "name", None) is None:
                    rows.pop()
                if not rows or not any(getattr(item, "name", None) == "tr" for item in rows):
                    continue
                wrapper = soup.new_tag("tbody")
                rows[0].insert_before(wrapper)
                for item in rows:
                    wrapper.append(item.extract())
    except Exception as _swallowed:
        note_debug_error("implicit tbody", _swallowed)


def _css_own_text(node: Any) -> str:
    direct = " ".join(
        str(value).strip()
        for value in node.find_all(string=True, recursive=False)
        if str(value).strip()
    )
    return re.sub(r"\s+", " ", direct).strip()


_JSOUP_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "caption", "center",
    "dd", "details", "dir", "div", "dl", "dt", "fieldset", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hgroup", "hr", "html", "li", "main", "menu", "nav", "ol",
    "p", "pre", "section", "summary", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "ul",
}


# Jsoup / SwiftSoup StringUtil.isActuallyWhitespace: space, \t, \n, \f, \r and U+00A0 - NOT Python's wider `\s` (U+3000 ideographic space, the U+2000
# series, U+0085 and U+1680 survive, so an ideographic paragraph indent stays in `text()`).
_JSOUP_WHITESPACE_RE = re.compile(r"[ \t\n\f\r\u00a0]+")


def _jsoup_text(node: Any) -> str:
    output: list[str] = []

    def append_space() -> None:
        if output and not output[-1].endswith(" "):
            output.append(" ")

    def append_text(value: str) -> None:
        normalized = _JSOUP_WHITESPACE_RE.sub(" ", value)
        if not normalized:
            return
        if normalized.startswith(" "):
            append_space()
            normalized = normalized.lstrip(" ")
        if normalized:
            output.append(normalized)

    def visit(value: Any) -> None:
        name = str(getattr(value, "name", "") or "").lower()
        if not name:
            # Jsoup text() skips comments (Comment / CDATA / doctype / declarations are not TextNodes); bs4 keeps them as string nodes.
            if type(value).__name__ in {"Comment", "CData", "Doctype", "Declaration", "ProcessingInstruction"}:
                return
            append_text(str(value))
            return
        # JSoup stores script/style payloads as DataNode rather than TextNode,
        # so Element.text() excludes their source. BeautifulSoup exposes the
        # same payload as string-like nodes; skip those containers to preserve
        # bare `text` behavior (notably when a TOC item is `<head>`).
        if name in {"script", "style", "noscript", "template"}:
            return
        if name == "br":
            append_space()
            return
        if name in _JSOUP_BLOCK_TAGS:
            append_space()
        for child in getattr(value, "children", []):
            visit(child)

    visit(node)
    return "".join(output).strip(" ")


def _jsoup_own_text(node: Any) -> str:
    """Jsoup / SwiftSoup `Element.ownText()`: the element's DIRECT text nodes concatenated with no separator (`a<!--c-->b<i>x</i>d` -> `abd`),
    a direct `<br>` adds a space, whitespace collapsed like appendNormalisedWhitespace, comments / CDATA / doctype skipped. Used by `:matchesOwn`;
    the `@ownText` result keeps `_css_own_text`."""
    output: list[str] = []
    for child in getattr(node, "children", []):
        name = str(getattr(child, "name", "") or "").lower()
        if not name:
            if type(child).__name__ in {"Comment", "CData", "Doctype", "Declaration", "ProcessingInstruction"}:
                continue
            normalized = _JSOUP_WHITESPACE_RE.sub(" ", str(child))
            if normalized.startswith(" ") and output and output[-1].endswith(" "):
                normalized = normalized.lstrip(" ")
            output.append(normalized)
        elif name == "br" and output and not output[-1].endswith(" "):
            output.append(" ")
    return "".join(output).strip(" ")


def _evaluate_bare_html_attribute(content: str, attr: str, runtime: RuleRuntime) -> list[str]:
    """Apply a terminal attribute to the current Legado element itself."""
    soup = BeautifulSoup(content, "html.parser")
    root = next((node for node in soup.contents if getattr(node, "name", None)), soup)
    return _extract_css_values([root], attr, runtime.base_url)


def _is_current_element_url_rule(rule: str) -> bool:
    """Whether every alternative is a bare URL attribute on the list item."""
    text = (rule or "").strip()
    if not text:
        return False
    parts = [part.strip().lstrip("@").lower() for part in re.split(r"\|\||&&|%%", text)]
    return bool(parts) and all(part in {"href", "src", "data-href", "data-src"} for part in parts)


class _SourceOrderHTMLFormatter(HTMLFormatter):
    """JSoup's Element.outerHtml() attribute serialisation: source attribute order, every value in DOUBLE quotes with
    `&` -> `&amp;`, `"` -> `&quot;` and U+00A0 -> `&nbsp;` (Jsoup Entities.escape, attribute mode: `<` / `>` stay raw).

    R155 (F): BeautifulSoup's default picks single quotes for a value that contains `"` (`class='"g"'`), so a rule that
    matches the element text with `/class="\\&quot;g\\&quot;"/` (52PO) never matched."""

    def attributes(self, tag: Any) -> Iterable[tuple[str, Any]]:
        return [] if tag.attrs is None else tag.attrs.items()

    def attribute_value(self, value: str) -> str:
        return str(value).replace("&", "&amp;").replace('"', "&quot;").replace(" ", "&nbsp;")

    def quoted_attribute_value(self, value: str) -> str:
        return '"' + str(value) + '"'

    def substitute(self, ns: str) -> str:
        """R155b (P2-3): text nodes are escaped like Jsoup Entities.escape (base mode): `&` -> `&amp;`, `<` -> `&lt;`,
        `>` -> `&gt;`, U+00A0 -> `&nbsp;`. Text under `<script>` / `<style>` is a Jsoup DataNode and stays raw."""
        from bs4.element import NavigableString

        parent = getattr(ns, "parent", None)
        if isinstance(ns, NavigableString) and parent is not None and getattr(parent, "name", None) in ("script", "style"):
            return ns
        return str(ns).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\u00a0", "&nbsp;")


_SOURCE_ORDER_HTML_FORMATTER = _SourceOrderHTMLFormatter()


def _serialize_html(value: Any) -> str:
    if hasattr(value, "decode"):
        return value.decode(formatter=_SOURCE_ORDER_HTML_FORMATTER)
    if type(value).__name__ == "NavigableString":
        # R155b (P2-3): a bare text-node child (innerHtml) is escaped like the text nodes inside an element.
        return _SOURCE_ORDER_HTML_FORMATTER.substitute(value)
    return str(value)


def _extract_css_values(elems: list[Any], attr: str | None, base_url: str) -> list[str]:
    normalized_attr = (attr or "").lower()
    if normalized_attr == "html":
        for el in elems:
            for blocked in el.select("script, style"):
                blocked.decompose()
        value = "\n".join(_serialize_html(el) for el in elems)
        return [value] if value else []
    if normalized_attr == "all":
        return ["\n".join(_serialize_html(el) for el in elems)]

    results: list[str] = []
    seen_attributes: set[str] = set()
    for el in elems:
        if attr is None or normalized_attr == "outerhtml":
            results.append(_serialize_html(el))
        elif normalized_attr == "innerhtml":
            results.append("".join(_serialize_html(c) for c in el.contents))
        elif normalized_attr == "text":
            results.append(_jsoup_text(el))
        elif normalized_attr == "owntext":
            results.append(" ".join(s.strip() for s in el.find_all(string=True, recursive=False) if s.strip()))
        elif normalized_attr == "textnodes":
            results.append("\n".join(s.strip() for s in el.find_all(string=True, recursive=False) if s.strip()))
        else:
            attr_name = attr or ""
            if attr_name.lower().startswith("abs:"):
                # Jsoup/SwiftSoup Node.attr("abs:…") resolves against the base URI.
                raw_value = el.get(attr_name[4:], "")
                if isinstance(raw_value, list):
                    raw_value = " ".join(str(item) for item in raw_value)
                raw_value = str(raw_value or "").strip()
                value = urljoin(base_url, raw_value) if raw_value else ""
            else:
                value = el.get(attr_name, "")
            if isinstance(value, list):
                value = " ".join(str(item) for item in value)
            value = str(value)
            if value.strip() and value not in seen_attributes:
                seen_attributes.add(value)
                results.append(value)
    return [x for x in results if x]


_LEGADO_RESULT_ATTRIBUTE_NAMES = {
    "text", "html", "innerhtml", "outerhtml", "all", "textnodes", "owntext",
    "href", "src", "alt", "class", "id", "content", "name", "value", "title",
    "type", "onclick", "action", "style",
}
_LEGADO_SELECTOR_TAG_NAMES = {
    "div", "span", "li", "ul", "ol", "a", "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "td", "tr", "table", "article", "section", "tbody", "thead", "tfoot", "th",
    "img", "body", "header", "footer", "nav", "main", "aside", "dl", "dt", "dd",
    "input", "button", "select", "option", "strong", "em", "b", "i", "u", "s",
    "font", "small", "figure", "figcaption", "center", "br", "hr", "iframe",
    "video", "audio", "source", "picture", "script", "noscript", "pre", "code",
    "blockquote", "sub", "sup", "meta", "link", "head", "html", "time", "textarea",
}


def _is_selector_like_segment(segment: str) -> bool:
    """Swift RuleParser.isSelectorLikeSegment: keep element semantics for selectors."""
    lower = (segment or "").strip().lower()
    if not lower:
        return True
    if lower.startswith("data-"):
        return False
    if lower in _LEGADO_RESULT_ATTRIBUTE_NAMES:
        return False
    if lower.startswith("children"):
        return True
    if any(ch in ".#[]()> ~+*,!" for ch in lower):
        return True
    return lower in _LEGADO_SELECTOR_TAG_NAMES


def evaluate_css_chain_rule(rule: str, content: str, runtime: RuleRuntime, attr_override: str | None = None) -> list[str]:
    known_attrs = {
        "text", "html", "innerhtml", "outerhtml", "textnodes", "owntext",
        "href", "src", "data-src", "data-href", "alt", "class", "id", "content",
        "name", "value", "title", "type", "onclick", "action", "style", "all",
    }
    parts = [p.strip() for p in split_on_at(rule) if p.strip()]
    if not parts:
        return []

    final_attr: str | None = None
    final_index: int | None = None

    if len(parts) >= 2:
        last = parts[-1]
        if last.startswith("!") and last[1:].isdigit():
            final_index = -(1000 + int(last[1:]))
            parts.pop()
        elif re.fullmatch(r"-?\d+", last):
            final_index = int(last)
            parts.pop()

    if attr_override is None and len(parts) >= 2 and not _is_selector_like_segment(parts[-1]):
        # Legado AnalyzeByJSoup.getResultList/getResultLast: the last `@`
        # segment of a string rule is the result (text/html/… or any
        # attribute name such as _src, lay-src, original, abs:href).
        final_attr = parts.pop()
    elif parts:
        last = parts[-1]
        if last.lower() in known_attrs or last.lower().startswith("data-"):
            final_attr = last
            parts.pop()

    if attr_override is not None:
        final_attr = attr_override

    soup = BeautifulSoup(content, "html.parser")
    if "tbody" in rule.lower():
        _insert_implicit_tbody(soup)
    current: list[Any] = [soup]
    for part in parts:
        (selector_kind, selector_value), step_selection = parse_css_chain_part(part)
        selected: list[Any] = []
        try:
            if selector_kind == "children":
                # AnalyzeByJSoup.ElementsSingle treats an index-only step as
                # a selection over the current element's direct children.
                for node in current:
                    selected.extend(node.find_all(recursive=False))
            elif selector_kind == "class":
                wanted = selector_value.casefold()
                for node in current:
                    for element in _css_elements_including_self(node):
                        if _css_element_has_class(element, wanted):
                            selected.append(element)
            elif selector_kind == "tag":
                wanted = selector_value.casefold()
                for node in current:
                    selected.extend(
                        element for element in _css_elements_including_self(node)
                        if str(getattr(element, "name", "")).casefold() == wanted
                    )
            elif selector_kind == "id":
                for node in current:
                    selected.extend(
                        element for element in _css_elements_including_self(node)
                        if str(element.get("id", "")) == selector_value
                    )
            elif selector_kind == "text":
                wanted = selector_value.casefold()
                for node in current:
                    selected.extend(
                        element for element in _css_elements_including_self(node)
                        if wanted in _css_own_text(element).casefold()
                    )
            else:
                if not selector_value:
                    return []
                normalized = normalize_selector(selector_value)
                for node in current:
                    # R149 (B3): Jsoup Element.select(css) collects from the element itself as well
                    # (RuleEvaluator.swift:1034-1041 -> SwiftSoup select), so `.item-link@a@href` on an `<a class=item-link>`
                    # item matches the item. soupsieve's select() only visits descendants.
                    node_selector, node_regex_marked = _jsoup_regex_attr_prepare(node, normalized)
                    try:
                        if _css_node_matches_selector(node, node_selector):
                            selected.append(node)
                        selected.extend(node.select(node_selector))
                    finally:
                        _jsoup_regex_attr_cleanup(node_regex_marked)
        except Exception as _swallowed:
            note_debug_error("css chain selector", _swallowed)
            return []
        selected = _apply_css_index_selection(selected, step_selection)
        if not selected:
            return []
        current = selected

    current = _apply_css_index(current, final_index)
    if not current:
        return []
    return _extract_css_values(current, final_attr, runtime.base_url)


def evaluate_css_rule(rule: str, content: str, runtime: RuleRuntime, content_type: str, attr_override: str | None = None) -> list[str]:
    if "@" in rule or _parse_css_index_selection(rule) is not None:
        chained = evaluate_css_chain_rule(rule, content, runtime, attr_override=attr_override)
        if chained:
            return chained
    selector, attr, index = parse_css_rule(rule)
    if attr_override is not None:
        attr = attr_override
    if selector.startswith("*:contains(") and selector.endswith(")"):
        needle = selector[len("*:contains("):-1].strip().strip("\"'")
        if needle:
            soup = BeautifulSoup(content, "html.parser")
            results: list[str] = []
            for el in soup.find_all(True):
                text = el.get_text(" ", strip=True)
                if needle not in text:
                    continue
                if attr is None or attr == "outerhtml":
                    results.append(str(el))
                elif attr == "html" or attr == "innerhtml":
                    results.append("".join(str(c) for c in el.contents))
                elif attr == "text":
                    results.append(text)
                elif attr == "owntext":
                    results.append("".join(s.strip() for s in el.find_all(string=True, recursive=False)))
                elif attr == "textnodes":
                    results.append("\n".join(s.strip() for s in el.find_all(string=True, recursive=False) if s.strip()))
                else:
                    # Raw attribute value (Legado/iOS semantics); `abs:` still resolves.
                    value = _css_raw_attr_value(el, attr, runtime.base_url)
                    if value:
                        results.append(value)
                if results:
                    return [x for x in results if x]
    results = css_extract(content, selector, attr, index, runtime.base_url)
    if not results and is_json_content(content):
        fallback = evaluate_json_rule(selector, content, runtime)
        if fallback:
            return fallback
    return results


def evaluate_explicit_css_rule(rule: str, content: str, runtime: RuleRuntime, attr_override: str | None = None) -> list[str]:
    """Execute Legado's @CSS path without ElementsSingle shorthand dispatch."""
    text = (rule or "").strip()
    attr = attr_override
    selector = text
    if attr_override is None and "@" in text:
        selector, attr = text.rsplit("@", 1)
        selector = selector.strip()
        attr = attr.strip()
    return css_extract(content, selector or "*", attr, None, runtime.base_url)


def evaluate_xpath_rule(rule: str, content: str, runtime: RuleRuntime, content_type: str) -> list[str]:
    text = rule.strip()
    attr_match = re.search(r"/@([A-Za-z_:][-A-Za-z0-9_:.]*)$", text)
    if attr_match:
        path = text[: attr_match.start()]
        # R156 (tocContent B): `//@href` leaves the remainder "/" (and `//div//@href` leaves "//div/"): the attribute of
        # EVERY element (descendant axis) - Swift parseXPathRule keeps `//` + the attribute (`XPathEngine.select`).
        if path == "/":
            path = "//*"
        elif path.endswith("/"):
            # R157 (tocContent G): `a//@href` is `a/descendant-or-self::node()/@href`; the element ITSELF is included
            # (`a//*` skipped it, so the `<a href>` of a link list was never read). App XPathEngine includes self.
            path += "descendant-or-self::*"
        return xpath_extract(content, path, attr_match.group(1), runtime.base_url)
    if "@" in text and not text.startswith("/"):
        path, attr = text.rsplit("@", 1)
        if "/" not in attr and "[" not in attr:
            return xpath_extract(content, path, attr or None, runtime.base_url)
    return xpath_extract(content, text, None, runtime.base_url)


_LEGADO_STRING_STATE = threading.local()


def legado_object_string_mode() -> bool:
    """Whether JSONPath hits render as Legado getString text (``{k=v}``)."""
    return bool(getattr(_LEGADO_STRING_STATE, "object_mode", False))


def set_legado_object_string_mode(enabled: bool) -> bool:
    previous = legado_object_string_mode()
    _LEGADO_STRING_STATE.object_mode = bool(enabled)
    return previous


def jsonpath_is_indefinite(path: str) -> bool:
    """Jayway indefinite path: wildcard, deep scan, filter, slice or multi index/key."""
    text = (path or "").strip()
    return jsonpath_is_collection_expression(text) or re.search(r"\.\*", text) is not None


def _json_smart_string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "/":
            out.append("\\/")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif code < 0x20 or 0x7F <= code <= 0x9F or 0x2000 <= code <= 0x20FF:
            out.append("\\u%04X" % code)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def json_smart_json(value: Any) -> str:
    """json-smart ``JSONValue.toJSONString(value, NO_COMPRESS)`` (escapes ``/``)."""
    if isinstance(value, str):
        return _json_smart_string(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return java_double_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(f"{_json_smart_string(str(k))}:{json_smart_json(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(json_smart_json(v) for v in value) + "]"
    return _json_smart_string(str(value))


def legado_json_to_string(value: Any) -> str:
    """Java ``String.valueOf`` of a json-smart model value (LinkedHashMap/JSONArray)."""
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key}={legado_json_to_string(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return json_smart_json(value)
    return json_container_to_string(value)


def legado_get_string_json_values(matches: list[Any], indefinite: bool) -> list[str]:
    """AnalyzeByJSonPath getString/getStringList text for each JSONPath hit."""
    out: list[str] = []
    for item in matches:
        if indefinite:
            text = legado_json_to_string(item)
        elif item is None:
            text = ""  # ob.toString() on JSON null throws inside Legado's try
        elif isinstance(item, list):
            text = "\n".join(legado_json_to_string(element) for element in item)
        else:
            text = legado_json_to_string(item)
        if text:
            out.append(text)
    return out


def evaluate_json_whole_value(rule: str, content: str) -> str:
    """R158 (G6, 1.1.2 `js_get_element_json_whole_value`): Legado AnalyzeRule.getElement in JSON mode -> AnalyzeByJSonPath.getObject (`ctx.read(rule)`): the WHOLE JSONPath
    value as JSON text, not the rows a list rule flattens. A definite path (`$.data.list`) answers its value (the whole array); an indefinite path
    (wildcard, deep scan, filter, slice, union) answers the array of every hit. Empty text when nothing matches (App
    `JSONPathEngine.selectWholeValueJSON`, `JSEngine` getJSONValue_raw)."""
    json_text = extract_json(content)
    if not json_text:
        return ""
    try:
        obj = json.loads(json_text)
    except Exception as _swallowed:
        note_debug_error("json whole value parse", _swallowed)
        return ""
    path = rule.strip()
    if path.lower().startswith("@json:"):
        path = path[6:].strip()
    if not path:
        return ""
    if not path.startswith(("$", "[")):
        path = "$." + path
    if path.startswith("$.["):
        path = "$" + path[2:]
    property_union = evaluate_legado_jsonpath_property_union(obj, path)
    if property_union is not None:
        return json.dumps(property_union, ensure_ascii=False) if property_union else ""
    for special in (evaluate_legado_jsonpath_filter, evaluate_legado_jsonpath_dot_wildcard):
        hits = special(obj, path)
        if hits is not None:
            return json.dumps(hits, ensure_ascii=False) if hits else ""
    try:
        matches = [m.value for m in parse_jsonpath_expression(path).find(obj)]
    except Exception as _swallowed:
        note_debug_error("json whole value path", _swallowed)
        return ""
    if not matches:
        return ""
    return json.dumps(matches if jsonpath_is_indefinite(path) else matches[0], ensure_ascii=False)


def evaluate_json_rule(rule: str, content: str, runtime: RuleRuntime) -> list[str]:
    text = rule.strip()
    if not text:
        return []
    json_text = extract_json(content)
    if not json_text:
        return []
    try:
        obj = json.loads(json_text)
    except Exception as _swallowed:
        note_debug_error("json rule parse", _swallowed)
        return []
    path = text
    if path and not path.startswith(("$", "[")):
        if (
            path.startswith(".")
            and app_profiles.current_app_profile().json_leading_dot_deep_scan
            and (path.startswith(("..", ".[")) or path in {".", ".*"})
        ):
            # R155b (P3-3), App profile 1.1.2: JSONPathEngine.normalizePath deep-scans a single leading `.` only; `..x`, `.[`,
            # `.` and `.*` keep their own meaning (same exclusions as evaluate_json_object_list). Every other
            # path (and the whole of 1.1.1) keeps the Jayway `"$." + path`.
            path = "$" + path
        else:
            path = "$." + path
    if path.startswith("$.["):
        path = "$" + path[2:]
    property_union = evaluate_legado_jsonpath_property_union(obj, path)
    if property_union is not None:
        return property_union
    object_mode = legado_object_string_mode()
    legado_filter = evaluate_legado_jsonpath_filter(obj, path)
    if legado_filter is not None:
        if object_mode:
            return legado_get_string_json_values(legado_filter, indefinite=True)
        return [json_container_to_string(item) for item in legado_filter if json_container_to_string(item)]
    dot_wildcard = evaluate_legado_jsonpath_dot_wildcard(obj, path)
    if dot_wildcard is not None:
        if object_mode:
            return legado_get_string_json_values(dot_wildcard, indefinite=True)
        return [json_container_to_string(item) for item in dot_wildcard if json_container_to_string(item)]
    try:
        expr = parse_jsonpath_expression(path)
        matches = [m.value for m in expr.find(obj)]
    except Exception as _swallowed:
        note_debug_error("json rule path", _swallowed)
        matches = []
    if object_mode:
        return legado_get_string_json_values(matches, indefinite=jsonpath_is_indefinite(path))
    results: list[str] = []
    for item in matches:
        results.append(json_container_to_string(item))
    # Legado never guesses: Jayway only prefixes `$.` (no `$..key` deep scan)
    # and AnalyzeRule has no camelCase/snake_case/alias fallback, so a missing
    # field (e.g. `url` with only `id` present) stays empty.
    return [x for x in results if x]


def _json_key_path_value(value: Any, key_path: str) -> Any | None:
    current = value
    for key in key_path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _first_jsonpath_dot_wildcard(path: str) -> int | None:
    quote: str | None = None
    escaped = False
    bracket_depth = 0
    index = 0
    while index + 1 < len(path):
        char = path[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote is not None:
            escaped = True
        elif quote is not None:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "[":
            bracket_depth += 1
        elif char == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif bracket_depth == 0 and path[index:index + 2] == ".*":
            return index
        index += 1
    return None


def evaluate_legado_jsonpath_dot_wildcard(root: Any, path: str) -> list[Any] | None:
    wildcard_index = _first_jsonpath_dot_wildcard(path)
    if wildcard_index is None:
        return None
    base_path = path[:wildcard_index] or "$"
    tail = path[wildcard_index + 2:]
    try:
        nested_base = evaluate_legado_jsonpath_dot_wildcard(root, base_path)
        if nested_base is None:
            base_values = [match.value for match in parse_jsonpath_expression(base_path).find(root)]
        else:
            base_values = nested_base
    except Exception:
        return []

    children: list[Any] = []
    for value in base_values:
        if isinstance(value, dict):
            children.extend(value.values())
        elif isinstance(value, list):
            children.extend(value)
    if not tail:
        return children

    relative_path = "$" + tail
    output: list[Any] = []
    for child in children:
        try:
            nested = evaluate_legado_jsonpath_dot_wildcard(child, relative_path)
            if nested is None:
                output.extend(
                    match.value
                    for match in parse_jsonpath_expression(relative_path).find(child)
                )
            else:
                output.extend(nested)
        except Exception:
            continue
    return output


def _java_collection_string(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key}={_java_collection_string(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_java_collection_string(item) for item in value) + "]"
    if isinstance(value, float):
        return java_double_string(value)
    return str(value)


def evaluate_legado_jsonpath_property_union(root: Any, path: str) -> list[str] | None:
    match = re.fullmatch(r"(.*)\[([^\]]*,[^\]]*)\]", path or "")
    if match is None:
        return None
    raw_fields = [field.strip() for field in match.group(2).split(",")]
    if not raw_fields or not all(
        len(field) >= 2 and field[0] == field[-1] and field[0] in {"'", '"'}
        for field in raw_fields
    ):
        return None
    keys = [field[1:-1] for field in raw_fields]
    try:
        base_matches = [item.value for item in parse_jsonpath_expression(match.group(1)).find(root)]
    except Exception:
        return []
    if not base_matches or not isinstance(base_matches[0], dict):
        return []
    selected = [(key, base_matches[0][key]) for key in keys if key in base_matches[0]]
    if not selected:
        return []
    return ["{" + ", ".join(f"{key}={_java_collection_string(value)}" for key, value in selected) + "}"]


_JSONPATH_MISSING = object()


def _jsonpath_filter_rhs(raw: str) -> Any:
    text = raw.strip()
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return ast.literal_eval(text)
    except Exception:
        return text.strip("'\"")


def _jsonpath_filter_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is _JSONPATH_MISSING or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _jsonpath_filter_compare(lhs: Any, operator: str, rhs: Any) -> bool:
    if lhs is _JSONPATH_MISSING:
        return operator == "!="
    if rhs is None:
        return (lhs is None) if operator == "==" else (lhs is not None) if operator == "!=" else False
    left_number = _jsonpath_filter_number(lhs)
    right_number = _jsonpath_filter_number(rhs)
    if left_number is not None and right_number is not None:
        return {
            "==": left_number == right_number,
            "!=": left_number != right_number,
            ">": left_number > right_number,
            ">=": left_number >= right_number,
            "<": left_number < right_number,
            "<=": left_number <= right_number,
        }.get(operator, False)
    left = _java_collection_string(lhs)
    right = _java_collection_string(rhs)
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    return False


def _matches_legado_jsonpath_filter(item: Any, expression: str) -> bool:
    expr = expression.strip()
    or_parts = _split_rule_operator(expr, "||")
    if len(or_parts) > 1:
        return any(_matches_legado_jsonpath_filter(item, part) for part in or_parts)
    and_parts = _split_rule_operator(expr, "&&")
    if len(and_parts) > 1:
        return all(_matches_legado_jsonpath_filter(item, part) for part in and_parts)
    if expr.startswith("(") and expr.endswith(")"):
        return _matches_legado_jsonpath_filter(item, expr[1:-1])
    if expr.startswith("!@."):
        return _json_key_path_value(item, expr[3:].strip()) is None

    regex_match = re.fullmatch(r"@\.([A-Za-z0-9_.\-]+)\s*=~\s*/(.*)/([A-Za-z]*)", expr)
    if regex_match is not None:
        key_path, pattern, flag_text = regex_match.groups()
        value = _json_key_path_value(item, key_path)
        if value is None:
            return False
        flags = 0
        if "i" in flag_text.lower(): flags |= re.IGNORECASE
        if "m" in flag_text.lower(): flags |= re.MULTILINE
        if "s" in flag_text.lower(): flags |= re.DOTALL
        if "x" in flag_text.lower(): flags |= re.VERBOSE
        try:
            return re.search(pattern.replace(r"\/", "/"), _java_collection_string(value), flags) is not None
        except re.error:
            return False

    membership = re.fullmatch(r"@\.([A-Za-z0-9_.\-]+)\s+(in|nin)\s+(\[.*\])", expr, re.I)
    if membership is not None:
        key_path, operator, raw_values = membership.groups()
        lhs = _json_key_path_value(item, key_path)
        if lhs is None:
            lhs = _JSONPATH_MISSING
        try:
            values = ast.literal_eval(raw_values)
        except Exception:
            return False
        contains = any(_jsonpath_filter_compare(lhs, "==", value) for value in values)
        return contains if operator.lower() == "in" else not contains

    set_operation = re.fullmatch(
        r"@\.([A-Za-z0-9_.\-]+)\s+(subsetof|anyof|noneof)\s+(\[.*\])",
        expr,
        re.I,
    )
    if set_operation is not None:
        key_path, operator, raw_values = set_operation.groups()
        lhs = _json_key_path_value(item, key_path)
        if not isinstance(lhs, list):
            return False
        try:
            rhs = ast.literal_eval(raw_values)
        except Exception:
            return False
        if not isinstance(rhs, list):
            return False
        membership = [
            any(_jsonpath_filter_compare(value, "==", candidate) for candidate in rhs)
            for value in lhs
        ]
        if operator.lower() == "subsetof":
            return all(membership)
        if operator.lower() == "anyof":
            return any(membership)
        return not any(membership)

    size_match = re.fullmatch(r"@\.([A-Za-z0-9_.\-]+)\s+size\s+(\d+)", expr, re.I)
    if size_match is not None:
        key_path, raw_size = size_match.groups()
        lhs = _json_key_path_value(item, key_path)
        return isinstance(lhs, (list, str)) and len(lhs) == int(raw_size)

    empty_match = re.fullmatch(r"@\.([A-Za-z0-9_.\-]+)\s+empty\s+(true|false)", expr, re.I)
    if empty_match is not None:
        key_path, raw_expected = empty_match.groups()
        lhs = _json_key_path_value(item, key_path)
        if not isinstance(lhs, (list, str)):
            return False
        return (len(lhs) == 0) == (raw_expected.lower() == "true")

    contains_match = re.fullmatch(r"@\.([A-Za-z0-9_.\-]+)\s+contains\s+(.+)", expr, re.I)
    if contains_match is not None:
        key_path, raw_value = contains_match.groups()
        lhs = _json_key_path_value(item, key_path)
        if lhs is None:
            return False
        rhs = _jsonpath_filter_rhs(raw_value)
        if isinstance(lhs, list):
            return any(_jsonpath_filter_compare(value, "==", rhs) for value in lhs)
        return _java_collection_string(rhs) in _java_collection_string(lhs)

    comparison = re.fullmatch(
        r"@\.([A-Za-z0-9_.\-]+)\s*(==|!=|>=|<=|>|<)\s*(null|true|false|'[^']*'|\"[^\"]*\"|-?\d+(?:\.\d+)?)",
        expr,
        re.I,
    )
    if comparison is not None:
        key_path, operator, raw_rhs = comparison.groups()
        lhs = _json_key_path_value(item, key_path)
        if lhs is None and not (isinstance(item, dict) and key_path in item):
            lhs = _JSONPATH_MISSING
        return _jsonpath_filter_compare(lhs, operator, _jsonpath_filter_rhs(raw_rhs))
    if expr.startswith("@."):
        return _json_key_path_value(item, expr[2:].strip()) is not None
    return False


def _has_invalid_legado_jsonpath_collection_operand(item: Any, expression: str) -> bool:
    if not isinstance(item, dict):
        return False
    expr = expression.strip()
    if expr.startswith("(") and expr.endswith(")"):
        return _has_invalid_legado_jsonpath_collection_operand(item, expr[1:-1])
    for operator in ("||", "&&"):
        parts = _split_rule_operator(expr, operator)
        if len(parts) > 1:
            return any(
                _has_invalid_legado_jsonpath_collection_operand(item, part)
                for part in parts
            )
    match = re.fullmatch(
        r"@\.([A-Za-z0-9_.\-]+)\s+(?:size\s+\d+|empty\s+(?:true|false))",
        expr,
        re.I,
    )
    if match is None:
        return False
    return isinstance(_json_key_path_value(item, match.group(1)), dict)


def _evaluate_jsonpath_deep_scan_filter(root: Any, scan_root_path: str, expression: str, tail: str) -> list[Any]:
    """R158 G5 (1.1.2 `jsonpath_deep_scan_filter`; jjwxc `$..[?(@.novelName)]`): Jayway `$..[?(filter)]` walks the document in pre-order and
    every OBJECT node that passes the filter is a result (a list is only reported when the filter passes on the list itself, which a
    property filter never does). `scan_root_path` is what precedes `..` (`$` for the whole document). The invalid base `$..` made this
    return nothing before."""
    try:
        roots = [root] if scan_root_path == "$" else [item.value for item in parse_jsonpath_expression(scan_root_path).find(root)]
    except Exception:
        return []
    selected: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if _matches_legado_jsonpath_filter(node, expression):
                selected.append(node)
            for child in node.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                if isinstance(child, (dict, list)):
                    walk(child)

    for scan_root in roots:
        walk(scan_root)
    if not tail:
        return selected
    output: list[Any] = []
    tail_path = "$" + tail if tail.startswith(".") else tail
    for item in selected:
        try:
            output.extend(match.value for match in parse_jsonpath_expression(tail_path).find(item))
        except Exception:
            continue
    return output


def evaluate_legado_jsonpath_filter(root: Any, path: str) -> list[Any] | None:
    match = re.fullmatch(r"(.*?)\[\?\((.*)\)\](.*)", path or "")
    if match is None:
        return None
    base_path, expression, tail = match.groups()
    if app_profiles.current_app_profile().jsonpath_deep_scan_filter and base_path.endswith(".."):
        return _evaluate_jsonpath_deep_scan_filter(root, base_path[:-2] or "$", expression, tail)
    try:
        base_matches = [item.value for item in parse_jsonpath_expression(base_path).find(root)]
    except Exception:
        return []
    candidates: list[Any] = []
    for value in base_matches:
        candidates.extend(value if isinstance(value, list) else [value])
    if any(
        _has_invalid_legado_jsonpath_collection_operand(item, expression)
        for item in candidates
    ):
        return []
    selected = [item for item in candidates if _matches_legado_jsonpath_filter(item, expression)]
    if not tail:
        return selected
    output: list[Any] = []
    tail_path = "$" + tail if tail.startswith(".") else tail
    for item in selected:
        try:
            output.extend(match.value for match in parse_jsonpath_expression(tail_path).find(item))
        except Exception:
            continue
    return output


def find_json_field(obj: Any, key: str) -> Any | None:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        lower = key.lower()
        for k, v in obj.items():
            if k.lower() == lower:
                return v
    return None


def is_json_content(content: str) -> bool:
    t = content.strip()
    return t.startswith("{") or t.startswith("[")


def js_result_to_list(result: str) -> list[str]:
    text = (result or "").strip()
    if not text:
        return []
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            out = []
            for item in obj:
                out.append(json_container_to_string(item))
            return [x for x in out if x]
    except Exception:
        pass
    if re.match(r"^https?://[\s\S]*,\s*\{[\s\S]*\}\s*$", text, flags=re.I):
        return [text]
    if "\n" in text:
        return [x.strip() for x in text.splitlines() if x.strip()]
    return [text]


def is_javascript_rule_text(rule: str) -> bool:
    text = (rule or "").strip().lower()
    if text.startswith(("@js:", "+@js:")):
        return True
    return re.fullmatch(r"\+?<js>[\s\S]*?</js>", text) is not None


def try_evaluate_fast_java_aes(js_code: str, input_text: str) -> str | None:
    if Cipher is None or algorithms is None or modes is None:
        return None
    match = re.fullmatch(
        r"""java\.aesBase64DecodeToString\(\s*(?:result|src|input)\s*,\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]*)['"]\s*\)\s*;?""",
        js_code.strip(),
    )
    if not match:
        return None
    key_text, mode_text, iv_text = match.groups()
    parts = [part.upper() for part in mode_text.split("/") if part]
    if not parts or parts[0] != "AES":
        return None
    mode_name = parts[1] if len(parts) > 1 else "CBC"
    if mode_name not in {"CBC", "ECB"}:
        return None
    key = key_text.encode("utf-8")
    if len(key) not in {16, 24, 32}:
        return None
    try:
        encrypted = base64.b64decode((input_text or "").strip())
    except Exception:
        return ""
    iv = iv_text.encode("utf-8")
    if mode_name == "CBC":
        iv = (iv + bytes(16))[:16]
        cipher_mode = modes.CBC(iv)
    else:
        cipher_mode = modes.ECB()
    try:
        decryptor = Cipher(algorithms.AES(key), cipher_mode).decryptor()
        raw = decryptor.update(encrypted) + decryptor.finalize()
    except Exception:
        return ""
    if raw:
        pad = raw[-1]
        if 0 < pad <= 16 and raw.endswith(bytes([pad]) * pad):
            raw = raw[:-pad]
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def try_evaluate_fast_toc_item_js(js_code: str, input_text: str, runtime: RuleRuntime) -> str | None:
    if "result.serialName" not in js_code and "ContentAnchorBatch" not in js_code:
        return None
    try:
        item = json.loads(extract_json(input_text) or input_text)
    except Exception:
        return None
    if not isinstance(item, dict):
        return None
    if "result.serialName" in js_code:
        title = str(item.get("serialName") or item.get("chapterName") or item.get("title") or "").strip()
        if not title:
            return None
        if "chargeStatus" in js_code or "isFree" in js_code:
            is_free = item.get("isFree") in (1, True, "1", "true", "True") or item.get("chargeStatus") in (0, "0")
            if "【" in js_code:
                return ("【👀】" if is_free else "【收💰费】") + title
        return title
    if "ContentAnchorBatch" in js_code and "ChapterSeqNo" in js_code and "ads-read" in js_code:
        book_id = (runtime.book_kind or "").strip()
        if not book_id:
            seed_runtime_book_kind_from_variables(runtime)
            book_id = (runtime.book_kind or "").strip()
        serial_id = item.get("serialID") or item.get("serialId") or item.get("chapterId") or item.get("id")
        if not book_id or serial_id in (None, ""):
            return None
        body = {
            "ContentAnchorBatch": [{"BookID": str(book_id), "ChapterSeqNo": [serial_id]}],
            "Scene": "chapter",
        }
        option = {
            "method": "POST",
            "body": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        }
        return "https://novel.html5.qq.com/be-api/content/ads-read," + json.dumps(option, ensure_ascii=False, separators=(",", ":"))
    return None


def regex_extract(pattern: str, group: int, content: str) -> list[str]:
    if not pattern:
        return [content]
    try:
        regex = re.compile(pattern, re.S)
    except re.error:
        return []
    results = []
    for match in regex.finditer(content):
        last_index = match.lastindex or 0
        idx = group if group <= last_index else 0
        try:
            results.append(match.group(idx))
        except Exception:
            continue
    return [x for x in results if x]


def expand_regex_group_template(template: str, list_rule: str, item: str) -> str:
    text = str(template or "")
    if "$" not in text:
        return text
    patterns = _legado_all_in_one_patterns(str(list_rule or ""))
    if not patterns:
        return text
    # The list item is the whole match of the last AllInOne pattern.
    pattern = patterns[-1]
    try:
        match = re.search(pattern, item or "")
    except re.error:
        return text
    if match is None:
        return text

    def repl(m: re.Match[str]) -> str:
        index = int(m.group(1))
        try:
            return match.group(index) or ""
        except Exception:
            return ""

    return re.sub(r"\$(\d+)", repl, text)


def apply_replace_chain(value: str, chain: str) -> str:
    parts = chain.split("##")
    out = value
    i = 1 if parts and parts[0] == "" else 0
    while i < len(parts):
        pattern = parts[i]
        replacement = parts[i + 1] if i + 1 < len(parts) else ""
        has_third_part = i + 2 < len(parts)
        third_part = parts[i + 2] if i + 2 < len(parts) else ""
        replace_first_marker = has_third_part
        flags = ""
        count = 1 if "1" in flags else 0
        if pattern:
            if (
                pattern == "^"
                and (re.match(r"https?://", replacement, flags=re.I) or "{{" in replacement)
                and re.match(r"https?://", out, flags=re.I)
            ):
                i += 3 if flags or replace_first_marker else 2
                continue
            try:
                if replace_first_marker:
                    match = re.search(pattern, out, flags=re.S)
                    if match:
                        matched_text = match.group(0)
                        out = re.sub(
                            pattern,
                            lambda m, repl=replacement: apply_legado_replacement_template(m, repl),
                            matched_text,
                            count=1,
                            flags=re.S,
                        )
                    else:
                        out = ""
                else:
                    out = re.sub(
                        pattern,
                        lambda m, repl=replacement: apply_legado_replacement_template(m, repl),
                        out,
                        count=count,
                        flags=re.S,
                    )
            except re.error:
                if replace_first_marker:
                    out = replacement
        i += 3 if flags or replace_first_marker else 2
    return out


# --------------------------------------------------------------------------------------------------------------------------------------------------
# R158c: user regexes over whole pages must neither be O(n^2) nor freeze every other worker.
#
# `re` holds the GIL for the whole call, so one catastrophic `re.sub` (m.hongxiu.com ruleContent.replaceRegex `##(\s|\S)*?阅读上一章|目录.*\s|-/-` on a
# 100K-char chapter whose literals are absent: ~150 s) freezes the batch's idle-timeout loop and every other worker. Two measures:
#   1. an EXACT literal prefilter: a top-level alternation branch that provably needs a literal run absent from the text can never match, so it is
#      dropped (all dropped = no match, instantly). Semantics stay those of `re`.
#   2. for inputs over REGEX_LARGE_INPUT_CHARS the optional `regex` module (GIL released while matching, honours `timeout=`) runs the search; its
#      TimeoutError becomes SourceValidationDeadlineExceeded. Without `regex` the code uses `re` as before.
REGEX_LARGE_INPUT_CHARS = 20000
REGEX_LARGE_INPUT_TIMEOUT_CAP_SECONDS = 120.0
REGEX_PREFILTER_MIN_LITERAL = 2

try:  # optional: requirements-validate-sources.txt lists it; the validator still works without it
    import regex as _regex_module  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - depends on the environment
    _regex_module = None

try:  # Python 3.11+: `re._parser`; older versions had `sre_parse`
    import re._parser as _re_parser  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    _re_parser = None


def _literal_runs_of_branch(items: Any, parser: Any) -> list[str]:
    """Literal runs (consecutive LITERAL opcodes at the branch's TOP level, plus those of plain non-alternating groups) every match of the branch
    must contain. Anything else (repeats, optionals, alternations, lookarounds, flag groups, back-references) contributes nothing."""
    runs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            runs.append("".join(current))
            current.clear()

    for opcode, argument in items:
        name = str(opcode)
        if name == "LITERAL":
            current.append(chr(argument))
            continue
        flush()
        if name == "SUBPATTERN":
            # (group, add_flags, del_flags, subpattern): a plain group is matched exactly once, so its own top-level runs are required too
            group_flags = argument[1] | argument[2]
            inner = list(argument[3])
            if not group_flags and not any(str(op) == "BRANCH" for op, _arg in inner):
                runs.extend(_literal_runs_of_branch(inner, parser))
    flush()
    return runs


def _has_unsafe_opcode(items: Any) -> bool:
    """Back-references / conditionals anywhere make group renumbering or branch dropping unsafe."""
    stack = [items]
    while stack:
        current = stack.pop()
        for opcode, argument in current:
            name = str(opcode)
            if name in {"GROUPREF", "GROUPREF_EXISTS", "GROUPREF_IGNORE", "GROUPREF_LOC_IGNORE", "GROUPREF_UNI_IGNORE"}:
                return True
            if name == "BRANCH":
                for branch in argument[1]:
                    stack.append(branch)
            elif name == "SUBPATTERN":
                stack.append(argument[3])
            elif name in {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}:
                stack.append(argument[2])
            elif name in {"ASSERT", "ASSERT_NOT"}:
                stack.append(argument[1])
            elif name in {"ATOMIC_GROUP"}:
                stack.append(argument)
    return False


def _split_top_level_alternation(pattern: str) -> list[str] | None:
    """The textual top-level `|` branches of `pattern` (escapes, character classes and groups respected); None when the text carries anything that
    makes a re-join unsafe (an inline flag group other than at the very start would change what a lone branch means)."""
    branches: list[str] = []
    start = 0
    depth = 0
    in_class = False
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
            index += 1
            continue
        if char == "[":
            in_class = True
            index += 1
            if index < length and pattern[index] == "^":
                index += 1
            if index < length and pattern[index] == "]":
                index += 1
            continue
        if char == "(":
            if pattern.startswith("(?", index) and index + 2 < length and pattern[index + 2] in "aiLmsux-":
                return None  # inline flags (global or scoped): keep the whole pattern
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            branches.append(pattern[start:index])
            start = index + 1
        index += 1
    branches.append(pattern[start:])
    return branches


@functools.lru_cache(maxsize=256)
def _analyze_regex_for_prefilter(pattern: str, flags: int) -> tuple[int, int, tuple[tuple[str, tuple[str, ...]], ...]] | None:
    """(compile flags, capturing group count, ((branch text | "", required literal runs of length >= 2), ...)) or None when nothing can be said.
    A single-branch pattern is reported as one entry whose text is the whole pattern."""
    if _re_parser is None:
        return None
    try:
        parsed = _re_parser.parse(pattern, flags)
    except Exception:
        return None
    try:
        state_flags = int(parsed.state.flags)
        group_count = int(parsed.state.groups) - 1
        items = list(parsed)
        if _has_unsafe_opcode(items):
            return None
        if len(items) == 1 and str(items[0][0]) == "BRANCH":
            branch_items = [list(branch) for branch in items[0][1][1]]
            texts = _split_top_level_alternation(pattern)
            if texts is None or len(texts) != len(branch_items):
                texts = [""] * len(branch_items)  # runs are still usable for the all-branches-dropped case
        else:
            branch_items = [items]
            texts = [pattern]
        entries = []
        for text, branch in zip(texts, branch_items):
            runs = tuple(run for run in _literal_runs_of_branch(branch, _re_parser) if len(run) >= REGEX_PREFILTER_MIN_LITERAL)
            entries.append((text, runs))
        return state_flags, group_count, tuple(entries)
    except Exception:
        return None


@functools.lru_cache(maxsize=256)
def _ignore_case_literal_pattern(literal: str, flags: int) -> "re.Pattern[str]":
    return re.compile(re.escape(literal), flags)


def _text_contains_literal(text: str, literal: str, flags: int) -> bool:
    """Whether a match of the literal is possible in `text`: a plain substring test, or `re` itself under IGNORECASE (the same per-character case
    folding the compiled pattern uses)."""
    if not flags & re.IGNORECASE:
        return literal in text
    return _ignore_case_literal_pattern(literal, flags & (re.IGNORECASE | re.ASCII | re.UNICODE)).search(text) is not None


def prefiltered_regex(compiled: "re.Pattern[str]", text: str, replacement_uses_groups: bool) -> tuple[bool, "re.Pattern[str]"]:
    """R158c exact prefilter. Returns (can_match, pattern_to_run): `can_match` False means NO match is possible in `text` (every top-level
    alternation branch needs a literal run that `text` lacks); otherwise the surviving branches (when some were dropped and renumbering is
    harmless) or the original pattern. Results are identical to running `compiled` on `text`."""
    analysis = _analyze_regex_for_prefilter(compiled.pattern, compiled.flags)
    if analysis is None:
        return True, compiled
    state_flags, group_count, entries = analysis
    survivors: list[str] = []
    dropped = 0
    for branch_text, runs in entries:
        if any(not _text_contains_literal(text, run, state_flags) for run in runs):
            dropped += 1
        else:
            survivors.append(branch_text)
    if dropped == len(entries):
        return False, compiled
    if dropped == 0 or len(entries) == 1:
        return True, compiled
    if (group_count and replacement_uses_groups) or any(not branch for branch in survivors) or ("" in survivors):
        return True, compiled
    try:
        rebuilt = re.compile("|".join(survivors), compiled.flags)
    except re.error:
        return True, compiled
    return True, rebuilt


def _regex_large_timeout() -> float:
    return remaining_source_validation_seconds(REGEX_LARGE_INPUT_TIMEOUT_CAP_SECONDS)


@functools.lru_cache(maxsize=128)
def _regex_module_pattern(pattern: str, flags: int) -> Any:
    return _regex_module.compile(pattern, flags=flags | _regex_module.VERSION0)


def large_input_pattern(compiled: "re.Pattern[str]", text: str) -> Any:
    """The `regex`-module twin of `compiled` for a large `text` (GIL-free, timeout-capable); `compiled` itself for small inputs, when `regex` is
    missing, or when `regex` cannot compile the pattern."""
    if _regex_module is None or len(text) <= REGEX_LARGE_INPUT_CHARS:
        return compiled
    try:
        return _regex_module_pattern(compiled.pattern, compiled.flags)
    except Exception:
        return compiled


def regex_search(compiled: "re.Pattern[str]", text: str) -> Any:
    """`compiled.search(text)`; over REGEX_LARGE_INPUT_CHARS with the `regex` module and the source's remaining budget as its timeout."""
    runner = large_input_pattern(compiled, text)
    if runner is compiled:
        return compiled.search(text)
    timeout = _regex_large_timeout()
    try:
        return runner.search(text, timeout=timeout, concurrent=True)
    except TimeoutError as error:
        raise SourceValidationDeadlineExceeded("regex search exceeded the source validation budget") from error


def regex_sub(compiled: "re.Pattern[str]", replacer: Any, text: str, count: int = 0) -> str:
    """`compiled.sub(replacer, text, count)` with the same large-input handling as `regex_search`."""
    runner = large_input_pattern(compiled, text)
    if runner is compiled:
        return compiled.sub(replacer, text, count=count)
    timeout = _regex_large_timeout()
    try:
        return runner.sub(replacer, text, count=count, timeout=timeout, concurrent=True)
    except TimeoutError as error:
        raise SourceValidationDeadlineExceeded("regex substitution exceeded the source validation budget") from error


def regex_find(compiled: "re.Pattern[str]", text: str) -> Any:
    """`compiled.search(text)` through the prefilter and the large-input path (the Jsoup `:matches` / `[attr~=regex]` marker searches)."""
    can_match, runner = prefiltered_regex(compiled, text, False)
    if not can_match:
        return None
    return regex_search(runner, text)


def _java_regex_replacement(match: re.Match[str], replacement: str) -> str:
    """Expand a Java Matcher replacement string (``$n``, ``${name}``, ``\\x``)."""
    out: list[str] = []
    group_count = len(match.groups())
    i = 0
    while i < len(replacement):
        ch = replacement[i]
        if ch == "\\" and i + 1 < len(replacement):
            out.append(replacement[i + 1])
            i += 2
            continue
        if ch == "$" and i + 1 < len(replacement):
            nxt = replacement[i + 1]
            if nxt.isdigit():
                number = int(nxt)
                j = i + 2
                while j < len(replacement) and replacement[j].isdigit():
                    candidate = number * 10 + int(replacement[j])
                    if candidate > group_count:
                        break
                    number = candidate
                    j += 1
                if number <= group_count:
                    out.append(match.group(number) or "")
                i = j
                continue
            if nxt == "{":
                end = replacement.find("}", i + 2)
                if end > i + 2:
                    name = replacement[i + 2:end]
                    try:
                        out.append(match.group(name) or "")
                    except (IndexError, KeyError):
                        pass
                    i = end + 1
                    continue
        out.append(ch)
        i += 1
    return "".join(out)


_JAVA_NON_LINE_TERMINATOR_CLASS = "[^\\n\\r\\u0085\\u2028\\u2029]"
_JAVA_DOTALL_FLAG_RE = re.compile(r"\(\?[a-zA-Z]*s[a-zA-Z-]*[:)]")


def java_regex_dot_to_python(pattern: str) -> str:
    """R155 (E): Java / ICU `.` (no DOTALL) excludes every line terminator - `\\n \\r \\u0085 \\u2028 \\u2029` - while Python's
    only excludes `\\n`, so a page that breaks lines with a lone `\\r` made `##第(.*)页` swallow the whole text. Every
    unescaped `.` outside a character class becomes `[^\\n\\r\\u0085\\u2028\\u2029]`. Escapes (`\\.`, `\\\\`), character
    classes (including nested `[a[b]]` and a leading literal `]`), and patterns carrying an inline DOTALL flag are left alone."""
    if "." not in pattern or _JAVA_DOTALL_FLAG_RE.search(pattern):
        return pattern
    out: list[str] = []
    index = 0
    length = len(pattern)
    class_depth = 0
    while index < length:
        char = pattern[index]
        if char == "\\":
            out.append(pattern[index:index + 2])
            index += 2
            continue
        if class_depth:
            if char == "[":
                class_depth += 1
            elif char == "]":
                class_depth -= 1
            out.append(char)
            index += 1
            continue
        if char == "[":
            class_depth = 1
            out.append(char)
            index += 1
            # `[^]...]` / `[]...]`: a `]` right after the opening is a literal member
            if index < length and pattern[index] == "^":
                out.append("^")
                index += 1
            if index < length and pattern[index] == "]":
                out.append("]")
                index += 1
            continue
        if char == ".":
            out.append(_JAVA_NON_LINE_TERMINATOR_CLASS)
        else:
            out.append(char)
        index += 1
    return "".join(out)


def legado_replace_regex(value: str, chain: str) -> str:
    """AnalyzeRule.replaceRegex for a SourceRule ``##`` suffix.

    ``regex##replacement[###]``: only the first regex/replacement pair is used;
    a third segment selects replaceFirst (the replaced first match, or "" when
    nothing matches). An invalid regex falls back to a literal replacement
    (replaceFirst returns the replacement). No DOTALL, like Java's default.
    """
    parts = chain.split("##")
    pattern = parts[0]
    if not pattern:
        return value
    replacement = parts[1] if len(parts) > 1 else ""
    replace_first = len(parts) > 2
    # Readori compatibility: `^##https://…` must not prefix an absolute URL twice.
    if pattern == "^":
        repl = replacement.strip().lower()
        current = value.strip().lower()
        if (repl.startswith(("http://", "https://")) or "{{" in replacement) and current.startswith(("http://", "https://")):
            return value
    try:
        try:
            regex = re.compile(java_regex_dot_to_python(pattern))
        except re.error:
            regex = re.compile(pattern)
    except re.error:
        return replacement if replace_first else value.replace(pattern, replacement)
    # R158c: the exact literal prefilter (no match possible -> no scan at all) and the GIL-free large-input path; results equal plain `re`.
    uses_groups = "$" in replacement
    can_match, runner = prefiltered_regex(regex, value, uses_groups)
    if replace_first:
        if not can_match:
            return ""
        first = regex_search(runner, value)
        if first is None:
            return ""
        matched = first.group(0)
        can_match_inner, inner_runner = prefiltered_regex(regex, matched, uses_groups)
        if not can_match_inner:
            return matched
        return regex_sub(inner_runner, lambda m: _java_regex_replacement(m, replacement), matched, count=1)
    if not can_match:
        return value
    return regex_sub(runner, lambda m: _java_regex_replacement(m, replacement), value)


def apply_legado_content_replace_indent(value: str) -> str:
    return "\n".join("\u3000\u3000" + line.strip() for line in str(value or "").splitlines())


def apply_legado_replacement_template(match: re.Match[str], replacement: str) -> str:
    out = replacement
    max_group = len(match.groups())
    for idx in range(max_group, -1, -1):
        try:
            value = match.group(idx) or ""
        except IndexError:
            value = ""
        out = out.replace(f"${idx}", value)
    return out


def candidate_validation_keywords(primary: str) -> list[str]:
    # Legado BookSource.getCheckKeyword returns a non-blank checkKeyWord
    # verbatim. Keep it as the first automation candidate as well; changing a
    # one-character, long or whitespace-bearing value can invalidate signed or
    # language-specific search requests. Additional candidates are recovery
    # probes only and never replace the source-authored primary value.
    # R147: NOT used by batch validation any more (the App searches one keyword, see app_validation_keywords);
    # kept for offline recovery probes and the scripts/tests contract.
    base = primary if primary and primary.strip() else "夜无疆"
    out = [base]
    for kw in FALLBACK_KEYWORDS:
        if kw not in out:
            out.append(kw)
        if len(out) >= 8:
            break
    return out


def normalized_validation_keyword(raw: str | None) -> str:
    return candidate_validation_keywords(raw or "")[0]


# R147 (task 1): the App's batch validation searches exactly ONE keyword per source
# (BookSourceValidator.validationSearchKeyword, BookSourceValidator.swift:2682-2691): ruleSearch.checkKeyWord verbatim
# when it is not blank, otherwise "我的". It never rotates through other keywords while validating; the other keywords
# (`repairCheckKeywords`) are only tried by the user-triggered one-click repair after a failed validation.
APP_DEFAULT_CHECK_KEYWORD = "我的"


def app_validation_keywords(raw: Any) -> list[str]:
    """The keyword list the App's batch validation uses: one entry, `checkKeyWord` verbatim or the App default."""
    if isinstance(raw, str) and raw.strip():
        return [raw]
    return [APP_DEFAULT_CHECK_KEYWORD]


def _candidate_retry_urls(url: str) -> list[str]:
    text = (url or "").strip()
    if not text:
        return []
    # Keep validation aligned with the iOS engine and Legado: request the
    # protocol written by the source, and only follow server redirects.
    return [text]


def _encoding_from_charset(charset: str | None) -> str | None:
    if not charset:
        return None
    normalized = charset.strip().lower().replace("-", "").replace("_", "")
    if normalized in {"utf8"}:
        return "utf-8"
    if normalized in {"gbk", "gb2312", "gb18030"}:
        return "gb18030"
    if normalized in {"big5", "big5hkscs"}:
        return "big5"
    if normalized in {"latin1", "iso88591"}:
        return "iso-8859-1"
    return charset.strip()


def _charset_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    match = re.search(r"charset\s*=\s*['\"]?([a-zA-Z0-9_\-]+)", content_type, flags=re.I)
    return match.group(1) if match else None


def _charset_from_html_meta(text: str) -> str | None:
    if not text:
        return None
    patterns = [
        r"charset\s*=\s*['\"]?([a-zA-Z0-9_\-]+)",
        r"content\s*=\s*['\"][^'\"]*charset=([a-zA-Z0-9_\-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1)
    return None


def _looks_mojibake(text: str) -> bool:
    if not text:
        return False
    sample = text[:4000]
    markers = ["Ã", "Â", "â€", "ã€", "ï¼", "æ", "ç", "ä¸"]
    marker_count = sum(sample.count(marker) for marker in markers)
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", sample))
    return marker_count >= 8 and cjk_count < marker_count


def decode_response_text(resp: requests.Response, charset_override: str | None = None) -> str:
    data = resp.content or b""
    candidates: list[str] = []
    for raw in [
        charset_override,
        _charset_from_content_type(resp.headers.get("Content-Type")),
    ]:
        enc = _encoding_from_charset(raw)
        if enc and enc not in candidates:
            candidates.append(enc)

    for enc in candidates:
        try:
            return data.decode(enc, errors="replace")
        except Exception:
            pass

    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data.decode("utf-8-sig", errors="replace")
        except Exception:
            pass

    try:
        utf8 = data.decode("utf-8")
        meta_cs = _charset_from_html_meta(utf8)
        meta_enc = _encoding_from_charset(meta_cs)
        if meta_enc and meta_enc.lower() not in {"utf-8", "utf8"}:
            try:
                return data.decode(meta_enc, errors="replace")
            except Exception:
                pass
        if not _looks_mojibake(utf8):
            return utf8
    except Exception:
        pass

    try:
        preview = data[:2048].decode("iso-8859-1", errors="replace")
        meta_enc = _encoding_from_charset(_charset_from_html_meta(preview))
        if meta_enc:
            decoded = data.decode(meta_enc, errors="replace")
            if decoded:
                return decoded
    except Exception:
        pass

    for enc in ["gb18030", "big5", "iso-8859-1"]:
        try:
            decoded = data.decode(enc, errors="replace")
            if decoded and not _looks_mojibake(decoded):
                return decoded
        except Exception:
            pass
    return data.decode("utf-8", errors="replace")


# ---- R144a network diagnostics + App HTTP status semantics (F16) ----------------------------------------------
# fetch_text used to swallow every transport error into ("", url, 0), so a DNS / TLS / timeout failure looked like an
# empty search result. The cause is now recorded per thread (record_network_event) and surfaced by the validation
# stages as detail["networkError"] + a `network error: <category>` failureReason. The verdict itself is unchanged:
# like the App, a failed request is a failed stage.

NETWORK_ERROR_REASON_PREFIX = "network error: "


def classify_network_error(exc: BaseException | str) -> tuple[str, str]:
    """Map a transport exception to (category, message). Categories: dns, tls, timed out, connection refused,
    connection reset, too many redirects, connection failed, other, and `blocked: non-public address` (R145b SSRF
    guard; the request was never sent). (Never contains the substring "timeout":
    app_verdict() reserves that word for the App's whole-chain 校验超时.)"""
    message = str(exc) if not isinstance(exc, str) else exc
    lowered = message.lower()
    exc_name = type(exc).__name__.lower() if not isinstance(exc, str) else ""
    # R145b: net_guard refused the target (or a redirect hop / resolved address) before any request was sent.
    if net_guard.BLOCKED_REASON_TEXT in lowered:
        return net_guard.BLOCKED_REASON_TEXT, message
    if isinstance(exc, requests.exceptions.SSLError) or "ssl" in exc_name or "certificate verify" in lowered or "sslerror" in lowered:
        return "tls", message
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in lowered or "timeout" in lowered:
        return "timed out", message
    if isinstance(exc, requests.exceptions.TooManyRedirects) or "too many redirects" in lowered or "exceeded 30 redirects" in lowered:
        return "too many redirects", message
    if any(token in lowered for token in (
        "name or service not known", "getaddrinfo failed", "nameresolutionerror", "nodename nor servname",
        "temporary failure in name resolution", "failed to resolve", "no address associated",
    )):
        return "dns", message
    if "refused" in lowered or "10061" in lowered:
        return "connection refused", message
    if "reset" in lowered or "10054" in lowered or "aborted" in lowered or "remotedisconnected" in lowered or "connection broken" in lowered:
        return "connection reset", message
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection failed", message
    return "other", message


def begin_network_stage() -> None:
    """Forget the transport errors of the previous stage (per thread)."""
    _thread_local.network_events = []


# ---- R147 (L7): swallowed exceptions on the verdict path --------------------------------------------------------------
# Many `except Exception:` blocks turn a failure into an empty result on purpose (that is the App's behaviour too).
# They used to leave no trace. `note_debug_error` records the most important of them per source in
# `detail["debugErrors"]` (at most DEBUG_ERROR_LIMIT entries, each truncated) for diagnosis. It never raises and never
# changes a verdict.
DEBUG_ERROR_LIMIT = 20
DEBUG_ERROR_TEXT_LIMIT = 240


def begin_debug_errors() -> None:
    """Forget the swallowed exceptions of the previous source (per thread)."""
    _thread_local.debug_errors = []


def note_debug_error(where: str, exc: BaseException | str) -> None:
    try:
        if isinstance(exc, SourceValidationDeadlineExceeded):
            return
        events = getattr(_thread_local, "debug_errors", None)
        if events is None:
            events = []
            _thread_local.debug_errors = events
        if len(events) >= DEBUG_ERROR_LIMIT:
            return
        reason = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
        entry = f"{where}: {' '.join(reason.split())}"[:DEBUG_ERROR_TEXT_LIMIT]
        if events and events[-1] == entry:
            return
        events.append(entry)
    except Exception:  # noqa: BLE001 - diagnostics must never affect validation
        return


def attach_debug_errors(detail: dict[str, Any]) -> None:
    """Copy the swallowed exceptions of the source that just ran into ``detail["debugErrors"]``."""
    events = getattr(_thread_local, "debug_errors", None)
    if events and isinstance(detail, dict):
        detail["debugErrors"] = list(events[:DEBUG_ERROR_LIMIT])


def record_network_event(url: str, exc: BaseException | str, status: int = 0) -> dict[str, Any]:
    category, message = classify_network_error(exc)
    event = {"category": category, "message": message[:300], "url": str(url or ""), "status": int(status or 0)}
    events = getattr(_thread_local, "network_events", None)
    if events is None:
        events = []
        _thread_local.network_events = events
    events.append(event)
    return event


def record_http_status_event(url: str, status: int) -> dict[str, Any]:
    event = {"category": f"http {int(status)}", "message": f"HTTP {int(status)}", "url": str(url or ""), "status": int(status)}
    events = getattr(_thread_local, "network_events", None)
    if events is None:
        events = []
        _thread_local.network_events = events
    events.append(event)
    return event


def first_network_event() -> dict[str, Any] | None:
    events = getattr(_thread_local, "network_events", None) or []
    return dict(events[0]) if events else None


def attach_network_error(detail: dict[str, Any], stage: str, reason_is_generic: bool = True) -> bool:
    """If the stage that just failed recorded a transport/HTTP error, put it into `detail` and make the
    failureReason `network error: <category>`. Returns whether an event was attached."""
    event = first_network_event()
    if not event:
        return False
    detail["networkError"] = dict(event, stage=stage)
    if reason_is_generic:
        detail["failureReason"] = NETWORK_ERROR_REASON_PREFIX + str(event["category"])
    if not detail.get("error"):
        detail["error"] = f"{event['category']}: {event['message']}"
    return True


def app_http_error_blocks_rules(status: int, body: str, operation: str, has_login_check: bool = False) -> bool:
    """Whether the App refuses to hand an HTTP response to the rules (BookSourceEngine.executeRequestWithRetry
    :571-577 + shouldPassHTTPErrorBodyToRules :10353): any non-2xx status throws httpError unless the source has a
    loginCheckJs, or the status is a 4xx other than 401/403/407/408/429 whose body is non-empty without error-page
    signals and is JSON (HTML is accepted for search/explore only). status 0 = transport failure (handled apart).

    This is the F3 rule that App 1.1.1 applies unchanged. App 1.1.2 (R156 G2) additionally hands the body to the rules for a
    search / explore 404 or 410 (error pages included), and for any 4xx (except 401 / 403 / 407 / 408 / 429) when every rule of the
    operation is a pure-JS script; anti-bot pages and all 5xx still fail - that layer is `app_http_error_passes_to_rules_legado_parity`,
    applied by `app_http_failure` under the 1.1.2 profile flag `http_error_body_to_rules`."""
    status = int(status or 0)
    # Only real HTTP status codes (100-599) are judged: 0 is a transport failure (recorded apart) and test stubs /
    # synthetic responses use small placeholder numbers.
    if status < 100 or 200 <= status < 300 or has_login_check:
        return False
    if not (400 <= status < 500) or status in (401, 403, 407, 408, 429):
        return True
    trimmed = (body or "").strip()
    if not trimmed or contains_http_error_page_signals(trimmed):
        return True
    if (trimmed.startswith("{") and trimmed.endswith("}")) or (trimmed.startswith("[") and trimmed.endswith("]")):
        try:
            json.loads(trimmed)
            return False
        except Exception:
            return True
    return operation not in ("search", "explore")


_HTTP_ANTI_BOT_MARKERS = (
    "cf-chl", "cf_chl_opt", "cf-turnstile", "challenge-platform", "challenges.cloudflare.com",
    "just a moment", "verifying you are human", "ddos-guard", "hcaptcha", "cf-browser-verification",
)
_PURE_JS_READS_CONTENT = re.compile(
    r"\b(result|src)\b|java\s*\.\s*(getString|getStringList|getElements?|setContent|getStrResponse)\b"
)


def rules_are_pure_javascript(rules: list[Any]) -> bool:
    """R156 (G2): BookSourceEngine.rulesArePureJavaScript - every non-empty rule is a whole `@js:` / `<js>…</js>` script that does
    not read the response (no result / src, no java.getString / getElements / setContent / getStrResponse)."""
    present = [str(rule).strip() for rule in rules if rule is not None and str(rule).strip()]
    if not present:
        return False
    for rule in present:
        lowered = rule.lower()
        if lowered.startswith("@js:"):
            script = rule[4:]
        elif lowered.startswith("<js>") and lowered.endswith("</js>"):
            script = rule[4:-5]
        else:
            return False
        if _PURE_JS_READS_CONTENT.search(script):
            return False
    return True


def pure_javascript_rules_for_operation(src: dict[str, Any] | None, operation: str) -> bool:
    """BookSourceEngine.pureJavaScriptRulesForHTTPOperation: the rules the request's result feeds."""
    if not isinstance(src, dict):
        return False

    def rule_dict(key: str) -> dict[str, Any]:
        value = src.get(key)
        return value if isinstance(value, dict) else {}

    if operation == "search":
        rule = rule_dict("ruleSearch")
        fields = ("init", "bookList", "name", "author", "kind", "wordCount", "lastChapter", "intro", "coverUrl", "bookUrl")
    elif operation == "explore":
        rule = rule_dict("ruleExplore")
        fields = ("bookList", "name", "author", "kind", "wordCount", "lastChapter", "intro", "coverUrl", "bookUrl")
    elif operation == "toc":
        rule = rule_dict("ruleToc")
        fields = ("chapterList", "chapterName", "chapterUrl")
    elif operation == "content":
        rule = rule_dict("ruleContent")
        fields = ("content",)
    else:
        return False
    if not rule:
        return False
    # The search rule's init script is stored under the JSON key `init` (BookSource `SearchRule.initJs` CodingKey); accept `initJs` too.
    return rules_are_pure_javascript([(rule.get(field) or rule.get("initJs")) if field == "init" else rule.get(field) for field in fields])


def app_http_error_passes_to_rules_legado_parity(
    status: int, body: str, operation: str, pure_javascript_rules: bool
) -> bool:
    """R156 G2 (App profile 1.1.2), BookSourceEngine.passesLegadoParityHTTPErrorBody: Legado's AnalyzeUrl never looks at the
    status, so rules always get the body. Beyond the F3 rule, the body also goes to the rules for a search / explore 404 or 410
    (a non-empty error page included) and, whatever the body, for rules that are pure JavaScript. 401 / 403 / 407 / 408 / 429,
    every 5xx and anti-bot / Cloudflare challenge pages stay errors."""
    status = int(status or 0)
    if not (400 <= status < 500) or status in (401, 403, 407, 408, 429):
        return False
    trimmed = (body or "").strip()
    if trimmed and any(marker in trimmed[:200_000].lower() for marker in _HTTP_ANTI_BOT_MARKERS):
        return False
    if pure_javascript_rules:
        return True
    return status in (404, 410) and bool(trimmed) and operation in ("search", "explore")


def app_http_failure(url: str, status: int, body: str, operation: str, src: dict[str, Any] | None = None) -> bool:
    """app_http_error_blocks_rules + record the HTTP status for the report. True = the stage's request failed."""
    has_login_check = bool(str((src or {}).get("loginCheckJs") or "").strip())
    if app_http_error_blocks_rules(status, body, operation, has_login_check):
        if app_profiles.current_app_profile().http_error_body_to_rules and app_http_error_passes_to_rules_legado_parity(
            status, body, operation, pure_javascript_rules_for_operation(src, operation)
        ):
            if operation in ("search", "explore"):
                # The App probe (PassedHTTPErrorProbe) keeps the error visible: it is attached to the stage only when the
                # rules then find nothing (`attach_network_error` runs on a failed stage).
                record_http_status_event(url, status)
            return False
        record_http_status_event(url, status)
        return True
    return False


def make_source_session(src: dict[str, Any] | None = None) -> requests.Session:
    """A fresh Session for one source (R144a F11): nothing is inherited from another source, and
    `enabledCookieJar: false` stops Set-Cookie from being stored (App: the flag only controls saving)."""
    session = requests.Session()
    if isinstance(src, dict) and src.get("enabledCookieJar") is False:
        session.cookies.set_policy(_NoStoreCookiePolicy())
    return session


def reset_thread_session(src: dict[str, Any] | None = None) -> requests.Session:
    """R146: a clean per-thread Session (no cookies of an earlier run) for debug / single-source validation.
    Same as `make_source_session`, additionally remembered on the thread for callers that want to inspect it."""
    session = make_source_session(src)
    _thread_local.session = session
    return session


class _NoStoreCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    """Cookie policy that refuses to store any cookie a response sets (cookies sent explicitly still work)."""

    def set_ok(self, cookie: Any, request: Any) -> bool:  # noqa: D401 - cookielib hook
        return False


def fetch_text(
    session: requests.Session,
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    charset: str | None = None,
    timeout: int = REQUEST_TIMEOUT,
    allow_redirects: bool = True,
    response_meta: dict[str, Any] | None = None,
    retry_with_default_headers: bool = True,
) -> tuple[str, str, int]:
    timeout = remaining_source_validation_seconds(timeout)
    if _dbg.network_blocked():
        # R146: offline replay (rule tester) - answer like a transport failure, never touch the network.
        if _dbg.active():
            _dbg.emit("blocked", url=url)
        return "", url or "", 0
    data_response = decode_data_url_as_hex_response(url)
    if data_response is not None:
        hex_body, final_url, _response_type = data_response
        return hex_body, final_url, 200

    method = (method or "GET").upper()
    base_headers = dict(headers or {})
    request_timeout = (min(REQUEST_CONNECT_TIMEOUT, max(1, timeout)), timeout)
    structured_default_body = False
    if method == "POST" and body is not None and not any(k.lower() == "content-type" for k in base_headers):
        # R157 (searchExplore G4): without an explicit Content-Type a JSON / XML-looking body is sent as
        # `application/json; charset=UTF-8` (Legado AnalyzeUrl postJson; App NetworkManager `bodyLooksStructured`), not as a form.
        if _request_body_looks_structured(body):
            structured_default_body = True
            base_headers["Content-Type"] = "application/json; charset=UTF-8"
        else:
            base_headers["Content-Type"] = f"application/x-www-form-urlencoded; charset={charset or 'utf-8'}"

    data = body
    if method == "POST" and isinstance(body, str):
        enc = "utf-8" if structured_default_body else (charset or "utf-8")
        try:
            data = body.encode(enc, errors="ignore")
        except Exception:
            data = body.encode("utf-8", errors="ignore")

    last_error: Exception | None = None
    for _dbg_attempt, candidate_url in enumerate(_candidate_retry_urls(url)):
        # R144a (F9): exactly one attempt with the headers the source configured. The App never retries with a
        # replacement browser header set, so the validator no longer "rescues" a source whose headers are wrong
        # (`retry_with_default_headers` is accepted for call compatibility and ignored).
        req_headers = dict(base_headers)
        _dbg_started = time.perf_counter() if _dbg.active() else 0.0
        try:
            if method == "POST":
                resp = session.post(
                    candidate_url,
                    headers=req_headers,
                    data=data,
                    timeout=request_timeout,
                    allow_redirects=allow_redirects,
                )
            elif method == "HEAD":
                resp = session.head(
                    candidate_url,
                    headers=req_headers,
                    timeout=request_timeout,
                    allow_redirects=allow_redirects,
                )
            else:
                resp = session.get(
                    candidate_url,
                    headers=req_headers,
                    timeout=request_timeout,
                    allow_redirects=allow_redirects,
                )
            text = decode_response_text(resp, charset_override=charset)
            if _dbg.active():
                _dbg.emit_fetch(resp, url=candidate_url, method=method, attempt=_dbg_attempt, started=_dbg_started,
                                request_headers=req_headers, request_body=body, charset=charset, text=text)
            if response_meta is not None:
                response_meta["headers"] = {
                    str(key): str(value)
                    for key, value in dict(resp.headers or {}).items()
                }
                response_meta["cookies"] = {
                    str(key): str(value)
                    for key, value in resp.cookies.get_dict().items()
                }
                # R157 (js baidu): the raw payload (bounded) for Node replay fixtures, see materialize_js_request_log_fixtures.
                raw_content = getattr(resp, "content", b"") or b""
                if (
                    isinstance(raw_content, (bytes, bytearray))
                    and 0 < len(raw_content) <= RESPONSE_RAW_BYTES_LIMIT
                    and _response_needs_raw_bytes(str(resp.headers.get("Content-Type") or ""), bytes(raw_content))
                ):
                    response_meta["bodyBase64"] = base64.b64encode(bytes(raw_content)).decode("ascii")
            verify_redirect_url = loading_verify_redirect_url(text, str(resp.url))
            if allow_redirects and verify_redirect_url and method == "GET":
                try:
                    _dbg_verify_started = time.perf_counter() if _dbg.active() else 0.0
                    verify_resp = session.get(verify_redirect_url, headers=req_headers, timeout=request_timeout, allow_redirects=True)
                    verify_text = decode_response_text(verify_resp, charset_override=charset)
                    if _dbg.active():
                        _dbg.emit_fetch(verify_resp, url=verify_redirect_url, method="GET", attempt="verify",
                                        started=_dbg_verify_started, request_headers=req_headers, charset=charset,
                                        text=verify_text)
                    if verify_text and not contains_http_error_page_signals(verify_text):
                        if response_meta is not None:
                            response_meta["headers"] = {
                                str(key): str(value)
                                for key, value in dict(verify_resp.headers or {}).items()
                            }
                            response_meta["cookies"] = {
                                str(key): str(value)
                                for key, value in verify_resp.cookies.get_dict().items()
                            }
                        return verify_text, str(verify_resp.url), int(verify_resp.status_code)
                except Exception as _swallowed:
                    note_debug_error("fetch_text verify redirect", _swallowed)
                    pass
            return text, str(resp.url), int(resp.status_code)
        except SourceValidationDeadlineExceeded:
            raise
        except requests.RequestException as exc:
            last_error = exc
            if _dbg.active():
                _dbg.emit_fetch_error(url=candidate_url, method=method, attempt=_dbg_attempt, exc=exc)
            continue
        except Exception as exc:
            last_error = exc
            if _dbg.active():
                _dbg.emit_fetch_error(url=candidate_url, method=method, attempt=_dbg_attempt, exc=exc)
            continue

    if last_error is not None:
        # R144a (F16): a transport failure is no longer swallowed silently; the cause is kept for the report.
        record_network_event(url, last_error)
    return "", (url or ""), 0


def fetch_bytes(session: requests.Session, url: str, headers: dict[str, str] | None = None, timeout: int = REQUEST_TIMEOUT) -> tuple[bytes, str, int]:
    timeout = remaining_source_validation_seconds(timeout)
    if _dbg.network_blocked():
        if _dbg.active():
            _dbg.emit("blocked", url=url)
        return b"", (url or ""), 0
    request_timeout = (min(REQUEST_CONNECT_TIMEOUT, max(1, timeout)), timeout)
    base_headers = dict(headers or {})
    last_error: Exception | None = None
    for _dbg_attempt, candidate_url in enumerate(_candidate_retry_urls(url)):
        # R144a (F9): single attempt with the source's own headers (no replacement browser header retry).
        req_headers = dict(base_headers)
        _dbg_started = time.perf_counter() if _dbg.active() else 0.0
        try:
            resp = session.get(candidate_url, headers=req_headers, timeout=request_timeout, allow_redirects=True)
            if _dbg.active():
                _dbg.emit_fetch(resp, url=candidate_url, method="GET", attempt=_dbg_attempt, started=_dbg_started,
                                request_headers=req_headers, content=bytes(resp.content or b""))
            return bytes(resp.content or b""), str(resp.url), int(resp.status_code)
        except SourceValidationDeadlineExceeded:
            raise
        except requests.RequestException as exc:
            last_error = exc
            if _dbg.active():
                _dbg.emit_fetch_error(url=candidate_url, method="GET", attempt=_dbg_attempt, exc=exc)
            continue
        except Exception as exc:
            last_error = exc
            if _dbg.active():
                _dbg.emit_fetch_error(url=candidate_url, method="GET", attempt=_dbg_attempt, exc=exc)
            continue
    if last_error is not None:
        record_network_event(url, last_error)
    return b"", (url or ""), 0


def has_request_header(headers: dict[str, str], name: str) -> bool:
    target = name.lower()
    return any(str(k).lower() == target for k in headers)


def parent_directory_referer(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = parsed.path or "/"
    if path == "/":
        return ""
    if path.endswith("/"):
        parent = path
    else:
        parent = path.rsplit("/", 1)[0] + "/"
    if not parent:
        parent = "/"
    return urlunparse((parsed.scheme, parsed.netloc, parent, "", "", ""))


def loading_verify_redirect_url(html: str, final_url: str) -> str:
    if not html or not final_url:
        return ""
    if not contains_http_error_page_signals(html):
        return ""
    compact = re.sub(r"\s+", " ", html.lower())
    if "/userverify" not in compact or "/user/verify" not in final_url.lower():
        return ""
    try:
        parsed = urlparse(final_url)
    except Exception:
        return ""
    fragment = unquote(parsed.fragment or "").strip()
    if not fragment:
        return ""
    if not fragment.startswith("/"):
        fragment = "/" + fragment
    if not re.match(r"^/[\w./%-]+(?:[?#][^\\s]*)?$", fragment):
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, "/userverify" + fragment, "", "", ""))


def build_first_explore_url(
    explore_text: str,
    runtime: RuleRuntime,
    *,
    resolve_categories: bool = False,
    raw_template: bool = False,
) -> str | None:
    """First requestable discovery category. `resolve_categories=True` (the validation / debug request paths) follows the
    App's resolveExploreCategories + BookSourceValidator.swift:2357-2360: an `@js:` / `<js>` exploreUrl is run to get its
    category list and a title-only segment (no `::`) is a header, never a URL. The default keeps the lenient reading
    that static checks (`source_has_absolute_entry_point`, `fallback_base_url_from_entry_point`) rely on.
    `raw_template=True` (R150) returns the category's URL TEMPLATE untouched (`url,{options}`, `{{page}}`, `{{js}}` all
    unresolved) for `build_url_with_options`, which is what the App's explore() does with it
    (BookSourceEngine+Flagship.swift:149-150 buildUrlWithOptionsThrowing)."""
    previous_binding = getattr(runtime, "book_binding", "automatic")
    runtime.book_binding = "unbound"
    try:
        return _build_first_explore_url(explore_text, runtime, resolve_categories=resolve_categories, raw_template=raw_template)
    finally:
        runtime.book_binding = previous_binding


def _build_first_explore_url(
    explore_text: str,
    runtime: RuleRuntime,
    *,
    resolve_categories: bool = False,
    raw_template: bool = False,
) -> str | None:
    entries = parse_explore_urls(explore_text, runtime.source_url)
    if resolve_categories:
        if is_script_explore_text(explore_text):
            # R149 (B6): the App runs the script and parses its OUTPUT as the category list (JSON array / `title::url`
            # lines split on `&&` and newlines); only an empty output falls back to the script itself.
            generated, failed = run_explore_kinds_script(explore_text, runtime)
            if failed:
                return None
            generated_entries = parse_explore_kinds_app(generated or "", runtime.source_url)
            if generated_entries:
                entries = generated_entries
        else:
            # R149 (B6): a title-only first line (潇湘书院 starts with `排行`, then `title::url` lines) is a header in the
            # App, not `GET /排行`.
            entries = parse_explore_kinds_app(explore_text, runtime.source_url)
    for _, url in entries:
        if not url or not url.strip():
            continue  # title-only header (App: isHeader)
        if is_explore_action_url(url):
            continue  # `{{js}}` button, never a category (BookSourceValidator.swift:2357-2360)
        if url.lower().startswith("@js:") or (url.lower().startswith("<js>") and url.lower().endswith("</js>")):
            return url
        if raw_template:
            return url.strip()
        # Resolve templates like {{page}} in explore URL
        resolved = resolve_template(url, runtime, page=1)
        resolved = apply_page_expression_templates(resolved, 1)
        resolved = apply_legado_page_segments(resolved, 1)
        return urljoin(runtime.base_url, resolved)
    return None


def make_runtime(src: dict[str, Any]) -> RuleRuntime:
    url = str(src.get("bookSourceUrl") or "")
    # The App keeps an object-shaped header as a [String: String] map; parse_headers takes the JSON text.
    header_raw = src.get("header")
    if isinstance(header_raw, dict):
        header_raw = json.dumps(header_raw, ensure_ascii=False)
    base = fallback_base_url_from_entry_point(src, effective_base_url(url))
    try:
        source_type = int(src.get("bookSourceType") or 0)
    except Exception:
        source_type = 0
    headers = parse_headers(header_raw, RuleRuntime(
        source_url=base,
        base_url=base,
        book_source_url=url,
        headers={},
        js_lib=src.get("jsLib"),
        source_book_source_name=str(src.get("bookSourceName") or ""),
        source_book_source_comment=str(src.get("bookSourceComment") or ""),
        source_variable_comment=str(src.get("variableComment") or ""),
        source_book_source_type=source_type,
        source_last_update_time=src.get("lastUpdateTime") or 0,
        source_header=str(header_raw or ""),
        source_login_url=str(src.get("loginUrl") or ""),
        source_login_ui=str(src.get("loginUi") or ""),
        source_enabled_cookie_jar=src.get("enabledCookieJar") is not False,
        source_book_source_group=str(src.get("bookSourceGroup") or ""),
        source_explore_url=str(src.get("exploreUrl") or ""),
        source_search_url=str(src.get("searchUrl") or ""),
        source_concurrent_rate=str(src.get("concurrentRate") or ""),
    ))
    merged_headers = dict(DEFAULT_BROWSER_HEADERS)
    merged_headers.update(headers)
    return RuleRuntime(
        source_url=base,
        base_url=base,
        book_source_url=url,
        headers=merged_headers,
        js_lib=src.get("jsLib"),
        source_book_source_name=str(src.get("bookSourceName") or ""),
        source_book_source_comment=str(src.get("bookSourceComment") or ""),
        source_variable_comment=str(src.get("variableComment") or ""),
        source_book_source_type=source_type,
        source_last_update_time=src.get("lastUpdateTime") or 0,
        source_header=str(header_raw or ""),
        source_login_url=str(src.get("loginUrl") or ""),
        source_login_ui=str(src.get("loginUi") or ""),
        source_enabled_cookie_jar=src.get("enabledCookieJar") is not False,
        source_book_source_group=str(src.get("bookSourceGroup") or ""),
        source_explore_url=str(src.get("exploreUrl") or ""),
        source_search_url=str(src.get("searchUrl") or ""),
        source_concurrent_rate=str(src.get("concurrentRate") or ""),
        allows_user_interaction=False,
        interaction_state=new_interaction_state(),
    )


def request_search_or_explore(
    session: requests.Session,
    src: dict[str, Any],
    runtime: RuleRuntime,
    keyword: str,
    page: int = 1,
    mode: str = "search",
    template: str | None = None,
) -> tuple[str, str, dict[str, Any]] | None:
    """The request of a search (`searchUrl`) or discovery (first `exploreUrl` category, or `template`) page: the template goes
    through `build_url_with_options` (page / key templates, `{{js}}`, `url,{method/body/headers/charset}` options) exactly
    like AnalyzeUrl, for BOTH modes (explore: BookSourceEngine+Flagship.swift:149-150, keyword "" and the given page)."""
    if template is not None:
        pass
    elif mode == "search":
        template = app_profiles.app_search_url_template(src)
    else:
        template = build_first_explore_url(
            str(src.get("exploreUrl") or ""), runtime, resolve_categories=True, raw_template=True
        ) or ""
    if not template:
        return None
    url, opts = build_url_with_options(template, keyword=keyword, page=page, runtime=runtime, session=session)
    url = url.strip()
    if not url:
        return None
    abs_url = urljoin(runtime.base_url, url) if not url.lower().startswith(("http://", "https://", "@js:", "<js>")) else url
    # R156 (network): a `data:` carrier keeps its typed options (`,{"type":"x"}`), which choose the synthetic response type.
    abs_url = request_url_preserving_typed_data_options(abs_url, opts)
    charset = opts.get("charset") if isinstance(opts.get("charset"), str) else None
    method = opts.get("method", "GET")
    body = opts.get("body") if isinstance(opts.get("body"), str) else None
    extra_headers = opts.get("headers") if isinstance(opts.get("headers"), dict) else {}
    headers = dict(runtime.headers)
    headers.update({str(k): str(v) for k, v in extra_headers.items() if v is not None})
    return abs_url, method, {"headers": headers, "body": body, "charset": charset}


def trailing_put_split(rule: str) -> tuple[str, list[str]] | None:
    """`base@put:{…}[@put:{…}]` with every put at the END of a non-script rule -> (base, put bodies); otherwise None."""
    text = (rule or "").strip()
    if "@put:" not in text.lower() or "<js>" in text.lower() or "@js:" in text.lower():
        return None
    match = re.fullmatch(r"([\s\S]*?)((?:\s*@put:\{[^}]+?\})+)\s*", text, re.I)
    if match is None or not match.group(1).strip():
        return None
    bodies = [found.group(1)[1:-1] for found in _LEGADO_PUT_PATTERN.finditer(match.group(2))]
    return match.group(1).strip(), bodies


def is_bare_css_exclusion_rule(rule: str) -> bool:
    """True for a single-step bare selector rule (no `@` chain, no JSON/XPath/JS marker) whose tail is an ElementsSingle
    exclusion index: `sel!N[:M...]` or `sel[!N, A:B]`."""
    text = (rule or "").strip()
    if not text or "@" in text or "##" in text or "<js>" in text.lower():
        return False
    if text.startswith(("$", "/", "[", "{", "<", "+", "-")):
        return False
    parsed = _parse_css_index_selection(text)
    if parsed is None:
        return False
    selector_text, excludes, _terms = parsed
    # R157 (searchExplore G3): the bracket form `sel[a:b]` / `sel[1,3]` / `sel[-1]` is the same ElementsSingle selection
    # (AnalyzeByJSoup.kt:282-345: ranges, negative indexes, steps) with the listed indexes KEPT, not only removed.
    bracket_form = re.search(r"\[\s*!?\s*[\d\s,:\-]*\]\s*$", text) is not None
    return bool((excludes or bracket_form) and selector_text.strip())


def evaluate_book_list(rule: str, html: str, runtime: RuleRuntime) -> list[str]:
    # Legado list rules run on RuleData: JS `book` is null unless a book is bound.
    runtime.book_list_depth = getattr(runtime, "book_list_depth", 0) + 1
    try:
        return _evaluate_book_list(rule, html, runtime)
    finally:
        runtime.book_list_depth -= 1


def _evaluate_book_list(rule: str, html: str, runtime: RuleRuntime) -> list[str]:
    text = resolve_template(rule or "", runtime, rule_template=True).strip()
    if not text:
        return []
    if text.startswith("-") and len(text) > 1:
        results = evaluate_book_list(text[1:].strip(), html, runtime)
        return list(reversed(results))
    if text.startswith("+") and len(text) > 1:
        return evaluate_book_list(text[1:].strip(), html, runtime)

    # Legado AllInOne (AnalyzeByRegex): everything after ':' is regex; only
    # `&&` chains patterns, so it must win over the generic sequence split.
    all_in_one = _legado_all_in_one_patterns(text)
    if all_in_one is not None:
        return _legado_all_in_one_values(all_in_one, html)

    # R155 (C), App profile 1.1.2 only: `data.chapters@put:{total_page:data.total_page}` - Legado AnalyzeRule.getElements
    # (:365-395) calls putRule(sourceRule.putMap) on the WHOLE content for each source rule and then evaluates the rule
    # without its put as a list. 1.1.1 hands the whole text to the engines and ends up with a single bogus item.
    if app_profiles.current_app_profile().list_put_on_whole_content and trailing_put_split(text) is not None:
        put_base, put_bodies = trailing_put_split(text)  # type: ignore[misc]
        put_content_type = "json" if is_json_content(html) else "html"
        for put_body in put_bodies:
            evaluate_single_rule("@put:{" + put_body + "}", html, runtime, put_content_type)
        return evaluate_book_list(put_base, html, runtime)

    base_rule, trailing_js = split_trailing_js_block(text)
    if trailing_js:
        base_items = evaluate_book_list(base_rule, html, runtime)
        filtered = evaluate_book_list_trailing_js_filter(trailing_js, base_items, runtime)
        if filtered is not None:
            return filtered

    # R157 (searchExplore G1): Legado AnalyzeRule.splitSourceRule (:545-620) cuts the rule at every `<js>` / `@js:` FIRST; `&&` / `||` /
    # `%%` only apply inside a non-JS segment (the analyzers split them), and a JS segment receives the previous segment's whole
    # result. The `||` / `&&` that sat inside the JS code (or between a selector and the JS tail) used to be taken as the
    # rule operator, and a JS tail ran per list item.
    js_segments = _split_legado_js_segments(text)
    if len(js_segments) > 1 and any(is_js for _text, is_js, _at in js_segments):
        return _evaluate_book_list_js_segments(js_segments, html, runtime)

    sequence = _first_rule_sequence(text) if not (len(js_segments) == 1 and js_segments[0][1]) else None
    if sequence is not None:
        operator, parts = sequence
        groups = [evaluate_book_list(part, html, runtime) for part in parts]
        if operator == "||":
            return next((values for values in groups if values), [])
        if operator == "&&":
            return [value for values in groups for value in values]
        nonempty = [values for values in groups if values]
        if not nonempty:
            return []
        return [
            group[index]
            for index in range(len(nonempty[0]))
            for group in nonempty
            if index < len(group)
        ]

    low = text.lower()
    if low.startswith("<js>"):
        js_match = re.match(r"<js>([\s\S]*?)</js>([\s\S]*)$", text, re.I)
        if js_match:
            js_code = _list_js_code(js_match.group(1).strip())
            tail = js_match.group(2).strip()
            js_result = evaluate_js(js_code, html, runtime)
            out = js_result_to_list(js_result)
            if tail:
                next_values: list[str] = []
                for item in out:
                    next_values.extend(evaluate_book_list(tail, item, runtime))
                return next_values
            return out
    if low.startswith("<js>") and low.endswith("</js>"):
        return js_result_to_list(evaluate_js(_list_js_code(text[4:-5]), html, runtime))
    if low.startswith("+<js>") and low.endswith("</js>"):
        return js_result_to_list(evaluate_js(_list_js_code(text[5:-5]), html, runtime))
    if low.startswith("+@js:"):
        return js_result_to_list(evaluate_js(_list_js_code(text[5:]), html, runtime))
    if low.startswith("@js:"):
        return js_result_to_list(evaluate_js(_list_js_code(text[4:]), html, runtime))

    if "<js>" in text and "</js>" in text and not low.startswith(("<js>", "+<js>")):
        js_match = re.search(r"<js>([\s\S]*?)</js>", text, re.I)
        if js_match:
            base = text[:js_match.start()].strip()
            js_code = js_match.group(1).strip()
            after_js = text[js_match.end():].strip()
            if base:
                base_results = evaluate_book_list(base, html, runtime)
                if not base_results:
                    return []
                js_result = evaluate_js_with_node(
                    js_code,
                    html,
                    runtime,
                    extra_vars={"result": base_results, "src": html},
                ) or evaluate_js(
                    js_code,
                    html,
                    runtime,
                    extra_vars={"result": base_results, "src": html},
                )
                out = js_result_to_list(js_result)
                if after_js:
                    next_values: list[str] = []
                    for item in out:
                        next_values.extend(evaluate_book_list(after_js, item, runtime))
                    return next_values
                return out
            return js_result_to_list(evaluate_js(js_code, html, runtime))

    if low.startswith("@json:"):
        return evaluate_json_object_list(text[6:], html)
    if re.fullmatch(r"[A-Za-z][\w:-]*", text) and re.search(rf"<{re.escape(text)}(?:\s|/|>)", html, flags=re.I):
        results = css_extract(html, text, "outerhtml", None, runtime.base_url)
        if results:
            return results
    # R155 (D): a bare Jsoup selector with a trailing exclusion index (`tr!0`, `.mySearch ul!-1`, `li!0:3`) is
    # AnalyzeByJSoup.ElementsSingle (:303-322): `select(beforeRule)` then the listed indexes (negatives count from the
    # end) are REMOVED. parse_css_rule only knows the `@`-chained `!N`, so the whole rule used to be one bogus selector.
    if not is_json_content(html) and is_bare_css_exclusion_rule(text):
        excluded_items = evaluate_css_chain_rule(text, html, runtime, attr_override="outerhtml")
        if excluded_items:
            return excluded_items
    if text.startswith(("$.", "$[")) or text.startswith("$..") or looks_like_bare_jsonpath(text):
        if not text.startswith("$") and not is_json_content(html):
            # R149 (B2): RuleEvaluator.swift:1534-1539 (.contentDependent in a list rule): JSON content -> JSONPath,
            # HTML -> the JSoup selector (`ul.xbk`, `div.bookbox`), JSONPath only when the selector finds nothing.
            selector, _attr, css_index = parse_css_rule(text)
            css_items = css_extract(html, selector, "outerhtml", css_index, runtime.base_url)
            if css_items:
                return css_items
        return evaluate_json_object_list(text, html)

    if low.startswith("@xpath:"):
        xpath = text[7:].strip()
        return xpath_extract(html, xpath, "outerhtml", runtime.base_url)
    if text.startswith("/"):
        return xpath_extract(html, text, "outerhtml", runtime.base_url)

    if low.startswith("@css:"):
        return evaluate_explicit_css_rule(text[5:], html, runtime, attr_override="outerhtml")

    # JSON content: try JSON path first (legado: bare field name e.g. "data" → $.data[*])
    if is_json_content(html):
        json_results = evaluate_json_object_list(text, html)
        if json_results:
            return json_results

    if "@" in text:
        chained = evaluate_css_chain_rule(text, html, runtime, attr_override="outerhtml")
        if chained:
            return chained

    if re.match(r"^(?:class|tag|id|text)\.", text):
        return evaluate_css_chain_rule(text, html, runtime, attr_override="outerhtml")

    if looks_like_css_selector(text):
        selector, _attr, index = parse_css_rule(text)
        results = css_extract(html, selector, "outerhtml", index, runtime.base_url)
        if not results and is_json_content(html):
            results = evaluate_json_object_list(selector, html)
        return results

    return evaluate_rule(text, html, runtime, "json" if is_json_content(html) else "html")


def _app_js_array(result: str) -> list[str]:
    """App `JSEngine.evaluateToArray`: a JSON array result is its elements, any other non-empty result ONE item (never split on
    newlines)."""
    text = (result or "").strip()
    if not text:
        return []
    try:
        obj = json.loads(text)
    except Exception:
        return [text]
    if isinstance(obj, list):
        return [x for x in (json_container_to_string(item) for item in obj) if x]
    return [text]


def _evaluate_book_list_js_segments(segments: list[tuple[str, bool, bool]], html: str, runtime: RuleRuntime) -> list[str]:
    """Legado getElements over a rule with JS segments (R157 searchExplore G1).

    The rule is cut at every `<js>` / `@js:` first on both profiles (App RuleType.parseUncached == Legado splitSourceRule); `&&` /
    `||` / `%%` only apply inside a non-JS segment. What a JS segment receives differs:

    * 1.1.2 (`list_rule_js_tail_whole_list`, Legado AnalyzeRule.getElements / E1): a JS segment after the first segment runs ONCE on
      the whole current list (`result`), page text is `src`; its return is the new list. Every segment that FOLLOWS a JS segment runs on
      the whole current list too (JSON mode: the list's JSON-array text; HTML mode: the items' outerHTML joined); selector ->
      selector chains that never passed through JS stay per item.
    * 1.1.1 (App RuleType `.combine`): every segment is evaluated PER ITEM of the previous segment's output; a JS segment sees the
      item string as `result` / `src` and its result becomes an array (`evaluateToArray`: JSON array or one item)."""
    if not app_profiles.current_app_profile().list_rule_js_tail_whole_list:
        current = [html]
        for segment_text, is_js, _at in segments:
            produced: list[str] = []
            for item in current:
                if is_js:
                    produced.extend(
                        _app_js_array(evaluate_js(_list_js_code(segment_text.strip()), item, runtime, extra_vars={"src": item}))
                    )
                else:
                    produced.extend(evaluate_book_list(segment_text, item, runtime))
            current = produced
            if not current:
                break
        return current
    # 1.1.2 (E1 final): a JS segment AFTER the first segment runs once on the whole current list (`result` = the items: JSON objects
    # parsed back into objects in JSON mode, HTML items as outerHTML strings, with the Elements-like helpers) and what it returns
    # becomes the new list (JSON array -> items, any other non-empty result -> one item; an empty array or an exception -> empty);
    # selector segments still run per item; a leading JS segment sees the page.
    current = [html]
    passed_js = False
    # Legado getElements: a JS segment that returned a String hands that raw string to the next segment as its content; one that returned a
    # list/array hands the list. `pending_string` is the former (consumed by the next segment).
    pending_string: str | None = None
    for index, (segment_text, is_js, _at) in enumerate(segments):
        if is_js:
            code = _list_js_code(segment_text.strip())
            if index > 0 and not code.strip():
                continue  # a blank tail script leaves the list unchanged (Swift `tailScript.isEmpty`)
            returned_string: str | None = None
            if index == 0:
                output = evaluate_js(code, html, runtime, extra_vars={"src": html})
                if output.strip() and not output.strip().startswith("["):
                    returned_string = output
            elif pending_string is not None:
                # the previous JS returned a string: `result` is that string, not a list
                outer_sink = getattr(runtime, "script_error_sink", None)
                tail_sink = []
                runtime.script_error_sink = tail_sink
                try:
                    output = evaluate_js(
                        "__readoriListTailFinish(__readoriListTailRun(" + json.dumps(code) + "))", pending_string, runtime,
                        extra_vars={"src": html, "result": pending_string},
                    )
                finally:
                    runtime.script_error_sink = outer_sink
                    if outer_sink is not None:
                        outer_sink.extend(tail_sink)
                if tail_sink or output.strip() == "__readoriEmptyList__":
                    return []
            else:
                # Only container items (`{...}` / `[...]`) are parsed back into objects; every other item stays a string.
                result_items: list[Any] = []
                result_raws: list[str] = []
                json_mode = is_json_content(html)  # Swift `listResultIsJSON: isJSONContent(html)`: the PAGE decides, even for an empty list
                for item in current:
                    result_raws.append(item)
                    stripped = item.strip()
                    if stripped[:1] in ("{", "[") and is_json_content(item):
                        try:
                            parsed = json.loads(stripped)
                        except Exception:
                            parsed = None
                        if isinstance(parsed, (dict, list)):
                            result_items.append(parsed)
                            continue
                    result_items.append(item)
                # An exception in the tail yields an empty list (E1 final: no per-item fallback, no leftover `result`). The tail also
                # runs for an empty list (Legado only skips null) and a tail returning nothing is an empty list, not the bound list.
                outer_sink = getattr(runtime, "script_error_sink", None)
                tail_sink: list = []
                runtime.script_error_sink = tail_sink
                try:
                    output = evaluate_js(
                        "result = __readoriListResult(result, __readoriListRaw, __readoriListJson);\n"
                        "__readoriListTailFinish(__readoriListTailRun(" + json.dumps(code) + "))",
                        html, runtime,
                        extra_vars={"src": html, "result": result_items, "__readoriListRaw": result_raws, "__readoriListJson": json_mode},
                    )
                finally:
                    runtime.script_error_sink = outer_sink
                    if outer_sink is not None:
                        outer_sink.extend(tail_sink)
                if tail_sink or output.strip() == "__readoriEmptyList__":
                    return []
            if index > 0:
                if output.startswith("__readoriTail:str:"):
                    output = output[len("__readoriTail:str:"):]
                    returned_string = output
                elif output.startswith("__readoriTail:arr:"):
                    output = output[len("__readoriTail:arr:"):]
            current = [item for item in _app_js_array(output) if item.strip()]
            pending_string = returned_string if current else None
            passed_js = True
        elif passed_js:
            # E1 final / Legado getElements (`AnalyzeByJSonPath(result).getElements(rule)`): a segment that FOLLOWS a JS segment runs on the
            # whole current list - JSON mode on the list's JSON-array text (raw JSON for container items), HTML mode on the items'
            # outerHTML joined (Jsoup Elements semantics). Selector -> selector chains that never passed through JS stay per item.
            if not current:
                continue
            if pending_string is not None:
                # the JS returned a String: the next segment parses that raw string as its content
                content_string, pending_string = pending_string, None
                current = evaluate_book_list(segment_text, content_string, runtime)
                continue
            if is_json_content(html):
                parts: list[str] = []
                for item in current:
                    stripped = item.strip()
                    container = False
                    if stripped[:1] in ("{", "["):
                        try:
                            container = isinstance(json.loads(stripped), (dict, list))
                        except Exception:
                            container = False
                    parts.append(stripped if container else json.dumps(item, ensure_ascii=False))
                list_text = "[" + ",".join(parts) + "]"
            else:
                list_text = "\n".join(current)
            current = evaluate_book_list(segment_text, list_text, runtime)
        else:
            pending_string = None
            produced = []
            for item in current:
                produced.extend(evaluate_book_list(segment_text, item, runtime))
            current = produced
    return current


def evaluate_book_list_trailing_js_filter(js_part: str, items: list[str], runtime: RuleRuntime) -> list[str] | None:
    if not items:
        return []
    compact = re.sub(r"\s+", "", js_part or "")
    if "result.toArray()" not in compact or ".text().indexOf(java.get(" not in compact:
        return None
    key_match = re.search(r"java\.get\(['\"]([^'\"]+)['\"]\)", js_part)
    key_name = key_match.group(1) if key_match else "key"
    needle = (runtime.variables or {}).get(key_name, "")
    if not needle:
        return items
    out: list[str] = []
    for item in items:
        try:
            text = BeautifulSoup(item or "", "html.parser").get_text("", strip=True)
        except Exception:
            text = item or ""
        if needle in text:
            out.append(item)
    return out


def jsonpath_is_collection_expression(path: str) -> bool:
    """Match Jayway/Legado indefinite-path list semantics.

    A definite path that ends at a primitive scalar is not a book/chapter list.
    Wildcards, recursive descent, filters, slices, and unions always return a
    collection even when only one item matches. A definite structured object is
    retained as a single item for public rules that expose one chapter digest.
    """
    text = (path or "").strip()
    if text in {"*", "[*]", "$[*]", "$.*"}:
        return True
    return bool(
        ".." in text
        or re.search(r"\[\s*\*\s*\]", text)
        or re.search(r"\[\s*\?\(", text)
        or re.search(r"\[[^\]]*:[^\]]*\]", text)
        or re.search(r"\[[^\]]*,[^\]]*\]", text)
    )


def evaluate_json_object_list(path: str, content: str) -> list[str]:
    json_text = extract_json(content)
    if not json_text:
        return []
    try:
        obj = json.loads(json_text)
    except Exception:
        return []
    path_text = (path or "").strip()
    if not path_text or path_text in {"[*]", "*", "$[*]"}:
        if isinstance(obj, list):
            return [x for x in (json_container_to_string(item) for item in obj) if x]
    if path_text.startswith("."):
        if app_profiles.current_app_profile().json_leading_dot_deep_scan and not path_text.startswith(("..", ".[")) and path_text not in {".", ".*"}:
            # R155 (N), App profile 1.1.2: Jayway compiles a path that does not start with `$` / `@` as `"$." + path`, so
            # `.bookList[*]` is `$..bookList[*]` (deep scan; AnalyzeByJSonPath hands the rule to JsonPath.read as is).
            path_text = "$." + path_text
        else:
            path_text = "$" + path_text
    elif not path_text.startswith(("$", "[")):
        path_text = "$." + path_text
    if path_text.startswith("$.["):
        path_text = "$" + path_text[2:]
    legado_filter = evaluate_legado_jsonpath_filter(obj, path_text)
    if legado_filter is not None:
        return [json_container_to_string(item) for item in legado_filter if json_container_to_string(item)]
    dot_wildcard = evaluate_legado_jsonpath_dot_wildcard(obj, path_text)
    if dot_wildcard is not None:
        matches = dot_wildcard
    else:
        try:
            expr = parse_jsonpath_expression(path_text)
            matches = [m.value for m in expr.find(obj)]
        except Exception:
            matches = []
    is_collection_path = jsonpath_is_collection_expression(path_text)
    out: list[str] = []
    for item in matches:
        if isinstance(item, list):
            # legado getResultList: when a path yields a list value, expand each element
            for sub in item:
                out.append(json_container_to_string(sub))
        elif is_collection_path or isinstance(item, dict):
            out.append(json_container_to_string(item))
    return [x for x in out if x]


def first_result_to_book_url(
    source: dict[str, Any], item: str, rule_field: str, runtime: RuleRuntime, kind_rule: str = ""
) -> str:
    url = evaluate_rule(rule_field, item, runtime, "json" if is_json_content(item) else "html")
    raw_url = legado_get_string_unescape(url[0]) if url else ""
    if not raw_url and is_json_content(item) and contains_json_placeholder(rule_field):
        raw_url = substitute_json_placeholders(resolve_template(rule_field, runtime), item)
    if not raw_url:
        return urljoin(runtime.base_url, "")
    resolved, opts = resolve_url_with_options(raw_url, runtime.base_url)
    out = compose_url_with_options(
        normalize_book_candidate_url_to_source_mirror(resolved.strip(), runtime.book_source_url),
        opts,
    )

    kind = ""
    if kind_rule.strip():
        kind_values = evaluate_rule(kind_rule, item, runtime, "json" if is_json_content(item) else "html")
        kind = legado_joined_string_list(kind_values).strip()

    if not kind and is_json_content(item):
        try:
            obj = json.loads(extract_json(item) or item)
            if isinstance(obj, dict):
                for key in ["resourceID", "resourceId", "bookId", "book_id", "id", "novelId", "bookid", "bid"]:
                    value = obj.get(key)
                    if value is not None and str(value).strip():
                        kind = str(value).strip()
                        break
        except Exception:
            pass

    if kind:
        out = (
            out.replace("{{book.kind}}", kind)
            .replace("{book.kind}", kind)
            .replace("{{kind}}", kind)
            .replace("{kind}", kind)
            .replace("{}", kind)
        )

    if "{{" in out or "}}" in out or contains_json_placeholder(out):
        return ""
    return out


def is_legado_blank_base_book_url(url: str, runtime: RuleRuntime) -> bool:
    return bool(url and normalized_book_candidate_url_for_comparison(url) == normalized_book_candidate_url_for_comparison(runtime.base_url))


def first_result_to_text(item: str, rule_field: str, runtime: RuleRuntime) -> str:
    # Legado getString field semantics (join "\n", `##` once, `{k=v}` objects, unescape).
    return evaluate_get_string(rule_field, item, runtime, "json" if is_json_content(item) else "html")


def capture_search_item_rule_variables(rule: dict[str, Any], item: str, runtime: RuleRuntime) -> str:
    """Evaluate selected search-item side-effect fields before detail.

    Returns the item's ``author`` rule result purely as validation metadata
    (book/author aggregation); the runtime's Book binding is left untouched.
    """
    content_type = "json" if is_json_content(item) else "html"
    author = ""
    bind_fields = app_profiles.current_app_profile().search_item_fields_visible_to_book_url
    # R156 (J, 1.1.2): Legado BookList.kt:225-269 order (+ Readori's updateTime right after lastChapter, as the Swift engine does).
    fields = (
        ("author", "kind", "wordCount", "lastChapter", "updateTime", "intro", "coverUrl")
        if app_profiles.current_app_profile().list_item_legado_field_order
        else ("author", "coverUrl", "intro", "kind", "lastChapter", "wordCount", "updateTime")
    )
    for field in fields:
        rule_text = str(rule.get(field) or "").strip()
        if not rule_text:
            continue
        try:
            if field == "kind":  # getStringList
                values = evaluate_rule(rule_text, item, runtime, content_type)
            else:  # getString (non-isUrl): a Jsoup miss skips the following JS (R156 D)
                with _evaluating_non_url_string_field():
                    values = evaluate_rule(rule_text, item, runtime, content_type)
        except Exception:
            continue
        if field == "author" and values:
            try:
                author = legado_joined_string_list(values).strip()
            except Exception:
                author = ""
        if bind_fields and field in ("kind", "lastChapter", "intro", "coverUrl") and values:
            # R157 (tocContent H qq, 1.1.2): BookList.kt:220-281 sets each evaluated field on the SearchBook before bookUrl is
            # evaluated, so `book.kind` / `{{book.kind}}` (and the other fields) are visible to the bookUrl rule. wordCount has
            # no JS binding in the validator's `book` shim, so only its `@put` / `java.put` effect is kept.
            try:
                text = (
                    legado_joined_string_list(values).strip() if field == "kind" else legado_get_string_value(values).strip()
                )
            except Exception:
                text = ""
            if text:
                if field == "kind":
                    # The Swift engine's syncBookState(kind:) sets the Book binding only; Legado's variable map has no `book.kind`
                    # key, so nothing is written to runtime.variables (templates and JS read runtime.book_kind).
                    runtime.book_kind = text
                elif field == "lastChapter":
                    runtime.book_last_chapter = text
                elif field == "intro":
                    runtime.book_intro = text
                else:
                    runtime.book_cover_url = text
    return author


_SELECTED_BOOK_STATE_KEYS = ("book_kind", "book_last_chapter", "book_intro", "book_cover_url")


def _apply_selected_book_state(runtime: RuleRuntime, selected_variables: dict[str, str]) -> dict[str, str]:
    """R158 (H): split the `__book_*` carrier keys written by `collect_app_book_candidates` back onto the runtime Book binding."""
    rest: dict[str, str] = {}
    for key, value in selected_variables.items():
        if key.startswith("__") and key[2:] in _SELECTED_BOOK_STATE_KEYS:
            setattr(runtime, key[2:], value)
        else:
            rest[key] = value
    return rest


def fallback_name_from_item(item: str) -> str:
    if is_json_content(item):
        try:
            obj = json.loads(extract_json(item) or item)
            if isinstance(obj, dict):
                for key in [
                    "chapterName", "chapter_name", "chapterTitle", "chapter_title", "title", "name",
                    "text", "label", "caption", "volumeName", "volume_name", "sectionName", "section_name",
                    "bookName", "book_name", "novelName", "novel_name", "novelTitle", "novel_title",
                    "bookTitle", "book_title", "v_book",
                ]:
                    value = obj.get(key)
                    if value is not None and str(value).strip():
                        return str(value).strip()
        except Exception:
            pass
        return ""

    try:
        soup = BeautifulSoup(item, "html.parser")
        for selector in ["a[title]", "h1", "h2", "h3", "a"]:
            node = soup.select_one(selector)
            if not node:
                continue
            if selector == "a[title]":
                title = (node.get("title") or "").strip()
                if title:
                    return title
            text = node.get_text(" ", strip=True)
            if text:
                return text
    except Exception:
        pass
    return ""


def fallback_json_chapter_title_from_item(item: str) -> str:
    if not is_json_content(item):
        return ""
    try:
        obj = json.loads(extract_json(item) or item)
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    title = json_first_text(obj, [
        "chapterName", "chapter_name", "chapterTitle", "chapter_title", "mainTitle", "main_title", "title", "name",
        "text", "label", "caption", "volumeName", "volume_name", "sectionName", "section_name",
        "attributes.title", "attributes.intro", "data.attributes.title", "result.attributes.title",
    ])
    if title:
        return title
    number = json_first_text(obj, [
        "attributes.chapterIndex", "attributes.cidx", "attributes.chapter_id", "attributes.chapid",
        "chapterIndex", "chapter_index", "cidx", "chapterId", "chapter_id", "chapid", "id",
    ])
    return f"Chapter {number}" if number else ""


def is_redundant_json_digest_toc_item(item: str, all_items: list[str]) -> bool:
    if not is_json_content(item):
        return False
    try:
        obj = json.loads(extract_json(item) or item)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    item_type = json_first_text(obj, ["type"]).lower()
    if "digest" not in item_type:
        return False
    chapid = json_first_text(obj, ["attributes.chapid", "chapid", "chapterId", "chapter_id"])
    if not chapid:
        return False
    for other in all_items:
        if other == item or not is_json_content(other):
            continue
        try:
            other_obj = json.loads(extract_json(other) or other)
        except Exception:
            continue
        if not isinstance(other_obj, dict):
            continue
        other_type = json_first_text(other_obj, ["type"]).lower()
        other_id = json_first_text(other_obj, ["id", "attributes.id"])
        if other_id == chapid and "digest" not in other_type:
            return True
    return False


def is_likely_content_error_json(content: str) -> bool:
    if not is_json_content(content):
        return False
    try:
        obj = json.loads(extract_json(content) or content)
    except Exception:
        return False
    if isinstance(obj, dict):
        payload_keys = [
            "data", "result", "content", "chapterContent", "chapter_content",
            "body", "text", "html", "article", "images", "list", "items",
        ]
        has_non_empty_payload = any(key in obj and not is_empty_json_payload(obj.get(key)) for key in payload_keys)
        if not has_non_empty_payload and "error" in obj and not is_empty_json_payload(obj.get("error")):
            return True
    return is_likely_detail_error_json(obj)


JSON_SEARCH_NAME_KEYS = [
    "name", "bookName", "book_name", "title", "bookTitle", "book_title",
    "novelName", "novel_name", "novelTitle", "novel_title", "articleName", "article_name",
    "v_book",
    "resourceName", "resource_name", "text",
]

JSON_SEARCH_CONTAINER_KEYS = [
    "books", "bookList", "book_list", "novels", "novelList",
    "items", "records", "rows", "list", "sections", "data", "result",
]


def nested_json_book_item(item: str) -> str:
    if not is_json_content(item):
        return ""
    try:
        obj = json.loads(extract_json(item) or item)
    except Exception:
        return ""
    found = nested_json_book_object(obj)
    if not isinstance(found, dict) or found is obj:
        return ""
    try:
        return json.dumps(found, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return ""


def select_json_search_field_item(item: str, rule: dict[str, Any], runtime: RuleRuntime) -> str:
    nested = nested_json_book_item(item)
    if not nested:
        return item

    fields = [
        str(rule.get("name") or ""),
        str(rule.get("bookUrl") or ""),
        str(rule.get("author") or ""),
        str(rule.get("coverUrl") or ""),
    ]

    def score(candidate: str) -> int:
        content_type = "json" if is_json_content(candidate) else "html"
        total = 0
        for field in fields:
            if not field.strip():
                continue
            probe = RuleRuntime(**{**runtime.__dict__, "variables": dict(runtime.variables or {})})
            try:
                if evaluate_rule(field, candidate, probe, content_type):
                    total += 1
            except Exception:
                continue
        return total

    return item if score(item) >= score(nested) else nested


def nested_json_book_object(value: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth >= 5 or value is None:
        return None
    if isinstance(value, list):
        for element in value:
            found = nested_json_book_object(element, depth + 1)
            if found:
                return found
        return None
    if not isinstance(value, dict):
        return None
    for key in JSON_SEARCH_CONTAINER_KEYS:
        found = nested_json_book_object(value.get(key), depth + 1)
        if found:
            return merge_nested_json_book_object(value, found)
    if json_first_text(value, JSON_SEARCH_NAME_KEYS):
        return value
    for nested_value in value.values():
        found = nested_json_book_object(nested_value, depth + 1)
        if found:
            return merge_nested_json_book_object(value, found)
    return None


def merge_nested_json_book_object(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    merged = dict(child)
    for key, value in parent.items():
        if key in merged or key in JSON_SEARCH_CONTAINER_KEYS or isinstance(value, (dict, list)) or value is None:
            continue
        merged[key] = value
    return merged


def recover_json_search_items_if_wrapper_noise(items: list[str], html: str) -> list[str]:
    expanded = expand_json_search_item_wrappers(items)
    if expanded:
        return expanded
    if not items or not is_json_content(html) or not json_search_items_look_like_wrapper_noise(items):
        return items
    recovered = fallback_search_items_from_content(html)
    return recovered or items


def expand_json_search_item_wrappers(items: list[str]) -> list[str]:
    out: list[str] = []
    did_expand = False
    for item in items:
        text = (item or "").strip()
        if not text.startswith("{"):
            out.append(item)
            continue
        try:
            obj = json.loads(extract_json(text) or text)
        except Exception:
            out.append(item)
            continue
        if not isinstance(obj, dict):
            out.append(item)
            continue
        if json_first_text(obj, JSON_SEARCH_NAME_KEYS):
            out.append(item)
            continue
        nested = first_json_book_list(obj)
        if not nested:
            out.append(item)
            continue
        serialized = [
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, (dict, list))
            else str(value)
            for value in nested
            if value is not None and str(value).strip()
        ]
        if serialized:
            out.extend(serialized)
            did_expand = True
        else:
            out.append(item)
    return out if did_expand else []


def first_json_book_list(value: Any, depth: int = 0) -> list[Any]:
    if depth >= 5 or value is None:
        return []
    if isinstance(value, list):
        if any(isinstance(item, dict) and json_first_text(item, JSON_SEARCH_NAME_KEYS) for item in value):
            return value
        for item in value:
            nested = first_json_book_list(item, depth + 1)
            if nested:
                return nested
        return []
    if not isinstance(value, dict):
        return []
    for key in JSON_SEARCH_CONTAINER_KEYS:
        nested = first_json_book_list(json_nested_value(value, key), depth + 1)
        if nested:
            return nested
    for nested_value in value.values():
        nested = first_json_book_list(nested_value, depth + 1)
        if nested:
            return nested
    return []


def json_search_items_look_like_wrapper_noise(items: list[str]) -> bool:
    sample = items[:12]
    if not sample:
        return False
    usable = 0
    for item in sample:
        text = (item or "").strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(extract_json(text) or text)
        except Exception:
            continue
        if isinstance(obj, dict) and (json_first_text(obj, JSON_SEARCH_NAME_KEYS) or nested_json_book_item(text)):
            usable += 1
    return usable * 2 < len(sample)


def fallback_url_from_item(item: str, base_url: str, runtime: RuleRuntime | None = None) -> str:
    def usable_candidate(raw: str) -> str:
        resolved, opts = resolve_url_with_options(raw, base_url)
        candidate = compose_url_with_options(resolved.strip(), opts)
        if runtime is not None:
            candidate = normalize_book_candidate_url_to_source_mirror(candidate, runtime.book_source_url)
        if not candidate or not is_usable_search_book_url(candidate):
            return ""
        if runtime is not None and is_likely_search_or_explore_landing_url(candidate, runtime, reject_search_endpoint=True):
            return ""
        return candidate

    if is_json_content(item):
        try:
            obj = json.loads(extract_json(item) or item)
            if isinstance(obj, dict):
                for key in [
                    "chapterUrl", "chapter_url", "contentUrl", "content_url", "readUrl", "read_url",
                    "url", "href", "link", "path", "webUrl", "web_url", "pageUrl", "page_url",
                    "bookUrl", "book_url", "detailUrl", "detail_url",
                ]:
                    value = obj.get(key)
                    if value is not None and str(value).strip():
                        candidate = usable_candidate(str(value).strip())
                        if candidate:
                            return candidate
        except Exception:
            pass
        return ""

    try:
        soup = BeautifulSoup(item, "html.parser")
        for selector in ["a[href]", "link[href]"]:
            for node in soup.select(selector):
                href = (node.get("href") or "").strip()
                if href:
                    candidate = usable_candidate(href)
                    if candidate:
                        return candidate
        for node in soup.select("[onclick]"):
            onclick = str(node.get("onclick") or "")
            for pattern in [
                r"newWebView\(\s*['\"]([^'\"]+)['\"]",
                r"(?:location\.href|window\.location)\s*=\s*['\"]([^'\"]+)['\"]",
            ]:
                match = re.search(pattern, onclick, flags=re.I)
                if not match:
                    continue
                candidate = usable_candidate(match.group(1))
                if candidate:
                    return candidate
    except Exception:
        pass
    return ""


def fallback_chapter_url_from_item(item: str, base_url: str) -> str:
    def usable_candidate(raw: str) -> str:
        resolved, opts = resolve_url_with_options(raw, base_url)
        candidate = compose_url_with_options(resolved.strip(), opts)
        url_part, _ = parse_url_options(candidate)
        low = url_part.strip().lower()
        if not low or low == "#" or low.startswith(("javascript:", "mailto:", "tel:", "about:")):
            return ""
        if is_static_asset_url(url_part):
            return ""
        parsed = urlparse(url_part)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return candidate

    if is_json_content(item):
        try:
            obj = json.loads(extract_json(item) or item)
            if isinstance(obj, dict):
                for key in [
                    "chapterUrl", "chapter_url", "contentUrl", "content_url", "readUrl", "read_url",
                    "url", "href", "link", "path", "webUrl", "web_url", "pageUrl", "page_url",
                ]:
                    value = obj.get(key)
                    if value is not None and str(value).strip():
                        candidate = usable_candidate(str(value).strip())
                        if candidate:
                            return candidate
        except Exception:
            pass
        return ""

    try:
        soup = BeautifulSoup(item, "html.parser")
        for selector in ["a[href]", "link[href]"]:
            for node in soup.select(selector):
                href = (node.get("href") or "").strip()
                if href:
                    candidate = usable_candidate(href)
                    if candidate:
                        return candidate
        for node in soup.select("[onclick]"):
            onclick = str(node.get("onclick") or "")
            for pattern in [
                r"newWebView\(\s*['\"]([^'\"]+)['\"]",
                r"(?:location\.href|window\.location)\s*=\s*['\"]([^'\"]+)['\"]",
            ]:
                match = re.search(pattern, onclick, flags=re.I)
                if not match:
                    continue
                candidate = usable_candidate(match.group(1))
                if candidate:
                    return candidate
    except Exception:
        pass
    return ""


def decode_base64_url_candidate(value: str) -> str:
    text = (value or "").strip()
    if not text or len(text) > 2048:
        return ""
    if text.startswith(("http://", "https://", "/", "//")):
        return text
    compact = text.replace("-", "+").replace("_", "/")
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
        return ""
    compact += "=" * ((4 - len(compact) % 4) % 4)
    try:
        decoded = base64.b64decode(compact, validate=False).decode("utf-8", errors="replace").strip()
    except Exception:
        return ""
    return decoded if decoded.startswith(("http://", "https://", "/", "//")) else ""


def recover_encoded_chapter_url_from_item(item: str, base_url: str) -> str:
    if is_json_content(item):
        return ""
    try:
        soup = BeautifulSoup(item, "html.parser")
        for node in soup.select("[data-gdx1], [data-gdx], [data-rubru], [data-url], [data-href], [data-link], [data-chapter-url], [data-content-url], [data-read-url]"):
            for key, value in node.attrs.items():
                key_low = str(key).lower()
                if not (
                    key_low.startswith("data-gdx")
                    or key_low in {"data-rubru", "data-url", "data-href", "data-link", "data-chapter-url", "data-content-url", "data-read-url"}
                ):
                    continue
                raw = value[0] if isinstance(value, list) and value else value
                decoded = decode_base64_url_candidate(str(raw or ""))
                if decoded:
                    resolved, _ = resolve_url_with_options(decoded, base_url)
                    if resolved:
                        return resolved.strip()
    except Exception:
        pass
    return ""


def is_same_book_or_toc_url(candidate_url: str, book_url: str, toc_url: str) -> bool:
    def norm(value: str) -> str:
        parsed = urlparse(value or "")
        path = parsed.path.rstrip("/")
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"

    candidate = norm(candidate_url)
    return bool(candidate) and candidate in {norm(book_url), norm(toc_url)}


def is_direct_text_document_url(url: str) -> bool:
    try:
        path = unquote(urlparse(parse_url_options(url or "")[0]).path or "").lower()
    except Exception:
        return False
    return path.endswith((".txt", ".text"))


def json_nested_value(obj: Any, key: str) -> Any:
    current = obj
    for part in key.split("."):
        if not isinstance(current, dict):
            return None
        container = current
        current = container.get(part)
        if current is None:
            lower = part.lower()
            for candidate_key, candidate_value in container.items():
                if str(candidate_key).lower() == lower:
                    current = candidate_value
                    break
        if current is None:
            return None
    return current


def json_scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return legado_get_string_unescape(value).strip()
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    if isinstance(value, list):
        return "\n".join(text for text in (json_scalar_text(v) for v in value) if text).strip()
    if isinstance(value, dict):
        return json_first_text(value, ["content", "chapterContent", "chapter_content", "body", "text", "html", "value"])
    return ""


def json_first_text(obj: Any, keys: list[str]) -> str:
    if not isinstance(obj, dict):
        return ""
    for key in keys:
        text = json_scalar_text(json_nested_value(obj, key))
        if text:
            return text
    return ""


def parse_deferred_chapter_api_content(payload: str) -> str:
    try:
        obj = json.loads(payload)
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    data = obj.get("data")
    if not isinstance(data, dict):
        return ""
    content_data = data.get("content_data")
    if isinstance(content_data, str):
        text = content_data.strip()
        if text.startswith(("[", "{")):
            try:
                return json_scalar_text(json.loads(text)).strip()
            except Exception:
                return text
        return text
    return json_scalar_text(content_data).strip()


def extract_deferred_chapter_page_id(html: str) -> str:
    if not html or "data-pageid" not in html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one("[data-pageid]")
        if node:
            page_id = (node.get("data-pageid") or "").strip()
            if page_id:
                return page_id
    except Exception:
        pass
    match = re.search(r"""data-pageid\s*=\s*["']([^"']+)["']""", html, flags=re.I)
    return match.group(1).strip() if match else ""


def recover_deferred_chapter_api_content(
    session: requests.Session,
    final_url: str,
    request_headers: dict[str, str],
    html: str,
) -> str:
    if not html or "nologin" not in html.lower():
        return ""
    if "\u52aa\u529b\u52a0\u8f7d\u4e2d" not in html and "loading" not in html.lower():
        return ""
    page_id = extract_deferred_chapter_page_id(html)
    if not page_id:
        return ""
    api_url = urljoin(final_url, "/api/chapter/detail")
    headers = dict(request_headers or {})
    headers["Referer"] = final_url
    headers.setdefault("X-Requested-With", "XMLHttpRequest")
    headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    payload, _, status = fetch_text(
        session,
        api_url,
        method="POST",
        headers=headers,
        body=f"id={quote_plus(page_id)}",
    )
    if status >= 400:
        return ""
    return parse_deferred_chapter_api_content(payload)


def is_empty_json_payload(value: Any) -> bool:
    """BookSourceEngine.swift:1588-1596 isEmptyJSONPayload: null, a blank string (trimmed), an empty array / object."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def _app_json_fallback_string(obj: dict[str, Any], keys: list[str]) -> str:
    """BookSourceEngine.swift:1560-1586 jsonSearchFallbackString: the first key whose value is a non-blank string, a
    number / bool (NSNumber.stringValue: true -> "1") or an array whose FIRST element is a string / number. Objects and
    other arrays are skipped (unlike json_first_text, which digs into objects and joins arrays)."""
    for key in keys:
        value = json_nested_value(obj, key)
        if value is None:
            continue
        token: str | None = None
        if isinstance(value, str):
            token = value
        elif isinstance(value, (bool, int, float)):
            token = _app_ns_number_string(value)
        elif isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, str):
                token = first
            elif isinstance(first, (bool, int, float)):
                token = _app_ns_number_string(first)
        if token is None:
            continue
        trimmed = token.strip()
        if trimmed:
            return trimmed
    return ""


def _app_ns_number_string(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


_APP_DETAIL_ERROR_MESSAGE_MARKERS = (
    "认证失败", "未授权", "未登录", "请先登录", "token失效", "token过期",
    "unauthorized", "forbidden", "not logged in", "login required",
    "参数错误", "请求失败", "请求参数错误",
)


def is_likely_detail_error_json(obj: Any) -> bool:
    """BookSourceEngine.swift:1598-1636 isLikelyDetailErrorJSON (R150: the old port was stricter than the App).

    1. any of data / result / book / novel / info / item non-empty -> not an error;
    2. an explicit ``success: false`` -> error;
    3. otherwise ONLY the combination "explicit failure message AND an error-looking code" is an error. A code that is
       merely not 0 / 1 / 200 with no (or an unrelated) message is NOT an error (the App dropped that guess because
       aggregators return 10000 / "OK" and the like for success), and neither is a bare message such as "error" /
       "login" / "token" (marker substrings the old port matched inside ordinary text).
    """
    if not isinstance(obj, dict):
        return False
    payload_keys = ["data", "result", "book", "novel", "info", "item"]
    if any(key in obj and not is_empty_json_payload(obj.get(key)) for key in payload_keys):
        return False
    success = obj.get("success")
    # Swift `as? Bool` on a JSON number bridges 0 / 1, so `success: 0` is false as well.
    if success is False or (isinstance(success, (int, float)) and not isinstance(success, bool) and success == 0):
        return True
    message = _app_json_fallback_string(obj, ["msg", "message", "error", "errorMsg", "reason"]).lower()
    if not any(marker in message for marker in _APP_DETAIL_ERROR_MESSAGE_MARKERS):
        return False
    # `json["code"] ?? json["status"] ?? json["errCode"] ?? json["errorCode"]`: the first key that is present (even a
    # JSON null, which is neither a number nor a string and so ends in "not an error").
    code: Any = None
    for key in ("code", "status", "errCode", "errorCode"):
        if key in obj:
            code = obj[key]
            break
    if isinstance(code, (bool, int, float)):
        return int(code) not in {0, 1, 200}
    if isinstance(code, str):
        normalized = code.strip().lower()
        return bool(normalized) and normalized not in {"0", "1", "200", "ok", "success"}
    return False


def is_likely_empty_search_json(content: str) -> bool:
    if not is_json_content(content):
        return False
    try:
        obj = json.loads(extract_json(content) or content)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    payload_keys = [
        "data", "result", "list", "items", "books", "novels", "records", "rows",
        "bookList", "comicList",
    ]
    has_non_empty_payload = any(key in obj and not is_empty_json_payload(obj.get(key)) for key in payload_keys)
    if has_non_empty_payload:
        return False
    status = json_first_text(obj, ["status", "code", "resultCode"]).lower()
    message = json_first_text(obj, ["msg", "message", "error", "errorMsg", "reason", "value"]).lower()
    empty_markers = [
        "failed", "fail", "no data", "nodata", "no result", "no results", "not found",
        "empty", "没有更多", "无数据", "暂无", "未找到", "没有找到", "没有结果",
    ]
    return any(marker in status for marker in empty_markers) or any(marker in message for marker in empty_markers)


def html_meta_content(html: str, keys: list[str]) -> str:
    if not html:
        return ""
    for key in keys:
        escaped = re.escape(key)
        patterns = [
            rf"<meta[^>]+(?:property|name|itemprop)\s*=\s*['\"]{escaped}['\"][^>]+content\s*=\s*['\"]([^'\"]+)['\"]",
            rf"<meta[^>]+content\s*=\s*['\"]([^'\"]+)['\"][^>]+(?:property|name|itemprop)\s*=\s*['\"]{escaped}['\"]",
        ]
        for pattern in patterns:
            m = re.search(pattern, html, flags=re.I)
            if m and m.group(1).strip():
                return m.group(1).strip()
    try:
        soup = BeautifulSoup(html, "html.parser")
        for key in keys:
            for attr in ("property", "name", "itemprop"):
                node = soup.find("meta", attrs={attr: key})
                if node:
                    content = str(node.get("content") or "").strip()
                    if content:
                        return content
    except Exception:
        pass
    return ""


def clean_recovered_book_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", (raw or "").strip())
    if not name:
        return ""
    name = re.sub(r"^\s*(?:\d{1,3}[\u3001\.\uff0e\)\uff09]|[\(\uff08]\d{1,3}[\)\uff09])\s*", "", name).strip()
    leading_wrapped = re.match(r"^[\u300a\u300c\u300e\u201c\"']([^{}\u300b\u300d\u300f\u201d\"']{2,80})[\u300b\u300d\u300f\u201d\"']", name)
    if leading_wrapped:
        return leading_wrapped.group(1).strip()
    for left, right in [("\u300a", "\u300b"), ("\u300c", "\u300d"), ("\u300e", "\u300f"), ("\u201c", "\u201d"), ('"', '"'), ("'", "'")]:
        if name.startswith(left) and name.endswith(right) and len(name) > len(left) + len(right):
            name = name[len(left):-len(right)].strip()
            break
    name = re.sub(r"\s+(?:\u4f5c\u8005|Author)\s*[:\uff1a].*$", "", name, flags=re.I).strip()
    for separator in ["_", " | ", " / ", " - ", " \u2014 ", " \u2013 "]:
        if separator in name:
            head = name.split(separator, 1)[0].strip()
            if head:
                name = head
                break
    return name


def html_title_book_name(html: str) -> str:
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
    except Exception:
        title = ""
    name = clean_recovered_book_name(title)
    if name and is_usable_search_book_title(name):
        return name
    return ""


def contains_http_error_page_signals(html: str) -> bool:
    if not html:
        return False
    compact = re.sub(r"\s+", " ", html.lower())
    if re.search(r"<title>\s*\u52a0\u8f7d\u4e2d[\.。…．\uff0e]*\s*</title>", compact) and (
        "location.href" in compact
        or "document.cookie" in compact
        or "/userverify" in compact
        or "/user/verify" in compact
    ):
        return True
    if "var buid" in compact and "/probe.js" in compact:
        return True
    title_patterns = [
        r"<title>\s*(404|403|500|502|503|520|521|522|523|524|525|526)\b",
        r"<title>[^<]*(not found|page not found|页面未找到|网页未找到|未找到页面|页面找不到了)",
        r"<title>\s*verification required\s*</title>",
        r"<title>\s*loading\.{0,3}\s*</title>",
        r"<title>\s*redirecting\.{0,3}\s*</title>",
        r"\u5171\u6709\s*(?:<[^>]+>\s*)*0\s*(?:</[^>]+>\s*)*\u6761\s*\u641c\u7d22\u7ed3\u679c",
        r"0\s*(?:</?[^>]+>\s*)*\u6761\s*\u641c\u7d22\u7ed3\u679c",
    ]
    if any(re.search(pattern, compact, flags=re.I) for pattern in title_patterns):
        return True
    markers = [
        "404 not found",
        "page not found",
        "\u8bbf\u95ee\u7684\u9875\u9762\u5df2\u4e22\u5931",
        "\u9875\u9762\u5df2\u4e22\u5931",
        "页面未找到",
        "页面找不到了",
        "页面暂时无法访问",
        "网页未找到",
        "未找到页面",
        "error code 520",
        "error code 521",
        "error code 522",
        "error code 523",
        "error code 524",
        "error code: 520",
        "error code: 521",
        "error code: 522",
        "error code: 523",
        "error code: 524",
        "origin is unreachable",
        "connection timed out",
        "web server is down",
        "cloudflare ray id",
        "just a moment",
        "cf_chl_opt",
        "cf_chl_tk",
        "cf-chl",
        "challenges.cloudflare.com",
        "cloudflare.com/5xx-error-landing",
        "/probe.js",
        "access temporarily blocked",
        "verification required",
        "suspicious site blocked",
        "block.charter-prod.hosted.cujo.io",
        "hosted.cujo.io/warn.html",
        "managing security shield",
        "www.4.cn/search/detail/",
        "4.cn/search/detail/",
        "4.cn/help/list/cid/",
        "domain is available for purchase",
        "this domain is for sale",
        "this domain name is for sale",
        "buy this domain",
        "domain parking",
        "sedo domain parking",
        "parkingcrew",
        "\u641c\u7d22\u5230 0 \u6761",
        "\u641c\u7d220\u6761",
        "\u641c\u7d22\u7ed3\u679c\u4e3a0",
        "\u5171\u6709 0 \u6761\u641c\u7d22\u7ed3\u679c",
        "\u5171\u67090\u6761\u641c\u7d22\u7ed3\u679c",
        "0 \u6761\u641c\u7d22\u7ed3\u679c",
        "0 results found",
        "found 0 results",
        "godaddy.com/forsale",
        "hugedomains.com",
        "dan.com/buy-domain",
        "afternic.com",
        "域名出售",
        "域名转让",
        "购买此域名",
        "高端域名",
        "域名已过期",
        "正在出售",
    ]
    return any(marker in compact for marker in markers)


def contains_browser_deferred_content_signals(html: str) -> bool:
    if not html:
        return False
    compact = re.sub(r"\s+", " ", html.lower())
    if "var pagedata=" in compact and '"wpnrt"' in compact and 'id="main"' in compact:
        return True
    if "window.__initial" in compact and not re.search(r"<(article|p|div)[^>]+(content|article|chapter)", compact):
        return True
    return False


def guess_all_chapters_url_from_html(html: str, page_base_url: str) -> str:
    """Return a strong full-catalog link from an initial TOC/detail page."""
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return ""
    current = urljoin(page_base_url, page_base_url)
    keywords = [
        "\u5168\u90e8\u7ae0\u8282",
        "\u67e5\u770b\u5168\u90e8\u7ae0\u8282",
        "\u5b8c\u6574\u76ee\u5f55",
        "\u5c55\u5f00\u5168\u90e8\u7ae0\u8282",
        "\u5168\u90e8\u76ee\u5f55",
        "\u67e5\u770b\u66f4\u591a\u7ae0\u8282",
        "\u6240\u6709\u7ae0\u8282",
        "\u5b8c\u6574\u7ae0\u8282\u5217\u8868",
        "\u663e\u793a\u5168\u90e8\u7ae0\u8282",
        "\u5c55\u5f00\u76ee\u5f55",
    ]
    class_tokens = {"all-chapters", "all_chapters", "allchapters", "full-toc", "full_toc"}
    for node in soup.select("a[href]"):
        text = node.get_text(" ", strip=True)
        node_id = str(node.get("id") or "").strip().lower()
        node_classes = {str(x).strip().lower() for x in (node.get("class") or [])}
        has_text_signal = any(keyword in text for keyword in keywords)
        has_attr_signal = node_id in class_tokens or bool(node_classes & class_tokens)
        if not has_text_signal and not has_attr_signal:
            continue
        href = str(node.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        resolved, _ = resolve_url_with_options(href, page_base_url)
        if resolved and resolved != current and is_plausible_source_url(resolved):
            return resolved
    return ""


def is_likely_toc_range_option_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", (text or "").replace("\u3000", "")).lower()
    if "\u7ae0" not in compact and "chapter" not in compact:
        return False
    patterns = [
        r"\u7b2c?\d{1,6}\u7ae0?[-~\uff0d\u2014\u81f3\u5230]\d{1,6}\u7ae0",
        r"chapter\d{1,6}[-~\uff0d\u2014\u81f3\u5230]chapter?\d{1,6}",
        r"\d{1,6}[-~\uff0d\u2014\u81f3\u5230]\d{1,6}\u7ae0",
    ]
    return any(re.search(pattern, compact) for pattern in patterns)


def toc_pagination_sort_key(url: str) -> int:
    parsed = urlparse(url or "")
    path = parsed.path.lower()
    for pattern in [r"index_(\d+)", r"page[_-]?(\d+)", r"_(\d+)(?:/|$)"]:
        m = re.search(pattern, path)
        if m:
            return int(m.group(1))
    last = path.rsplit("/", 1)[-1]
    if re.match(r"^\d+\.html$", last):
        return int(re.sub(r"\D+", "", last) or "0")
    query = parse_qs(parsed.query)
    for key in ("page", "p", "pageindex", "pageno", "page_no"):
        if key in query and query[key]:
            digits = re.sub(r"\D+", "", query[key][0])
            if digits:
                return int(digits)
    if "offset" in query and query["offset"]:
        digits = re.sub(r"\D+", "", query["offset"][0])
        if digits:
            return 1_000_000 + int(digits)
    return 2_147_483_647


def guess_toc_range_option_urls_from_html(html: str, page_base_url: str) -> list[str]:
    if not html:
        return []
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for node in soup.select("option[value]"):
        if not is_likely_toc_range_option_text(node.get_text(" ", strip=True)):
            continue
        raw = str(node.get("value") or "").strip()
        if not raw or raw.startswith("#") or raw.lower().startswith("javascript:"):
            continue
        resolved, _ = resolve_url_with_options(raw, page_base_url)
        if resolved and resolved not in seen and is_plausible_source_url(resolved):
            seen.add(resolved)
            urls.append(resolved)
    return sorted(urls, key=toc_pagination_sort_key)


_TOC_PAGINATION_LIST_RULES = (
    ".pages a@href", ".pagination a@href", ".listpage a@href",
    ".pages option@value", ".pagination option@value", ".listpage option@value", "select option@value",
)


def is_likely_toc_option_value_url(raw: str) -> bool:
    """App `isLikelyTocOptionValueURL`: an <option value> that is a request URL, not a bare page number for the page's own JS."""
    value = (raw or "").strip()
    if not value:
        return False
    if value.lower().startswith(("http://", "https://", "//")):
        return True
    return any(token in value for token in ("/", ".", "?", "="))


def discover_toc_pagination_candidates(html: str, page_base_url: str, content_type: str, runtime: RuleRuntime) -> list[str]:
    """App `discoverTocPaginationCandidates`: pagination links / selects of the page, ordered by `toc_pagination_sort_key`."""
    candidates: list[str] = []
    seen: set[str] = set()
    for rule in _TOC_PAGINATION_LIST_RULES:
        is_option_rule = rule.endswith("option@value")
        try:
            values = evaluate_rule(rule, html, runtime, content_type)
        except Exception as _swallowed:
            note_debug_error("toc pagination candidates", _swallowed)
            continue
        for raw in values:
            raw = (raw or "").strip()
            if not raw or raw.startswith("#") or raw.lower().startswith("javascript:"):
                continue
            if is_option_rule and not is_likely_toc_option_value_url(raw):
                continue
            resolved, _options = resolve_url_with_options(raw, page_base_url)
            if not resolved or resolved in seen or not is_plausible_source_url(resolved):
                continue
            seen.add(resolved)
            candidates.append(resolved)
    return sorted(candidates, key=lambda url: (toc_pagination_sort_key(url), url))


def heuristic_book_items_from_html(html: str, base_url: str, keyword: str = "") -> list[str]:
    if contains_http_error_page_signals(html):
        return []
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return []

    key = (keyword or "").strip()
    keyword_hit_seen = False
    structured_hit_seen = False
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()

    for selector in (".list tr.book", "tr.book", ".grid tr", ".result tr"):
        for row in soup.select(selector):
            href = ""
            for link in row.select("a[href]"):
                raw_href = (link.get("href") or "").strip()
                if not raw_href or raw_href.startswith(("#", "javascript:")):
                    continue
                resolved, _ = resolve_url_with_options(raw_href, base_url)
                if resolved and is_usable_search_book_url(resolved):
                    href = resolved
                    break
            if not href or href in seen:
                continue
            text = row.get_text(" ", strip=True)
            if not text or len(text) < 2:
                continue
            score = 6
            if key and key in text:
                score += 4
                keyword_hit_seen = True
            structured_hit_seen = True
            seen.add(href)
            hits.append((score, str(row)))

    for node in soup.select("a[href]"):
        href = (node.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        text = node.get_text(" ", strip=True)
        title = (node.get("title") or "").strip()
        label = text or title
        if not label or len(label) > 64:
            continue

        resolved, _ = resolve_url_with_options(href, base_url)
        candidate_url = resolved.strip()
        if not candidate_url or candidate_url in seen:
            continue
        if not is_usable_search_book_url(candidate_url):
            continue
        seen.add(candidate_url)

        score = 0
        low_href = candidate_url.lower()
        low_label = label.lower()
        if re.search(r"/(book|novel|info|detail|read|xiaoshuo)/", low_href):
            score += 4
        if re.search(r"\d{2,}", low_href):
            score += 2
        if key and key in label:
            score += 6
            keyword_hit_seen = True
        if re.search(r"(第\s*\d+\s*[章话节回]|小说|书|novel|chapter)", low_label, flags=re.I):
            score += 3

        hits.append((score, str(node)))

    if key and hits and not keyword_hit_seen:
        if html_declares_search_results_for_keyword(html, key):
            return []
        if is_search_endpoint_url(base_url) and not structured_hit_seen:
            return []

    hits.sort(key=lambda x: x[0], reverse=True)
    out = [raw for score, raw in hits if score >= 3]
    if not out:
        out = [raw for _, raw in hits]
    return out[:120]


def html_declares_search_results_for_keyword(html: str, keyword: str) -> bool:
    key = (keyword or "").strip()
    if not key:
        return False
    try:
        text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    except Exception:
        text = html or ""
    compact = re.sub(r"\s+", "", text)
    if key not in compact:
        return False
    low = compact.lower()
    markers = [
        "搜索结果", "搜尋結果", "搜寻结果", "searchresults", "searchresult",
        "搜索：", "搜尋：", "搜寻：", "搜索:", "搜尋:", "搜寻:",
    ]
    return any(marker.lower() in low for marker in markers)


def evaluate_init_rule_nonthrowing(rule: str, html: str, runtime: RuleRuntime, content_type: str) -> str:
    """`ruleSearch.init` (BookSourceEngine.swift:238-244): evaluateString, never throwing; a non-empty result
    replaces the response body. Script errors inside the init rule do not fail the stage."""
    sink = getattr(runtime, "script_error_sink", None)
    mark = len(sink) if sink is not None else 0
    previous_error = getattr(runtime, "script_error", "")
    try:
        # R158 (huarenv5): a lone `<js>..</js>` / `@js:` init answers its WHOLE string (App JSEngine.evaluateToArray returns `[s]` for a
        # string result, and `evaluateString` takes that first value; Legado getElement(init) is the script value as it is). The generic
        # rule path splits a multi-line JS result into trimmed lines (`js_result_to_list`), which cut an init that returns the page
        # (`...;result`) down to its first line.
        lone_js = re.fullmatch(r"\s*<js>([\s\S]*?)</js>\s*", rule, re.I) or re.fullmatch(r"\s*@js:([\s\S]*)", rule, re.I)
        if (lone_js is not None and "##" not in lone_js.group(1) and lone_js.group(1).strip()
                and "</js>" not in lone_js.group(1).lower() and "<js>" not in lone_js.group(1).lower()):
            whole = evaluate_js(lone_js.group(1).strip(), html, runtime)
            if str(whole or "").lstrip().startswith("["):
                # `evaluate_js` hands back TEXT, so a real JS array result and a string that merely looks like a JSON array (java.ajax output)
                # cannot be told apart here. The App keeps a string whole ([s]) and takes the first item of an array; the generic path (first
                # item of a JSON array) is the stricter choice for the string case - a documented limitation.
                values = evaluate_rule(rule, html, runtime, content_type)
            else:
                values = [whole] if str(whole or "").strip() else []
        else:
            values = evaluate_rule(rule, html, runtime, content_type)
    finally:
        if sink is not None:
            sink[mark:] = [entry for entry in sink[mark:] if entry.get("kind") != "error"]
        runtime.script_error = previous_error
    text = values[0] if values else ""
    return text if str(text).strip() else ""


def candidate_url_key(book_url: str) -> str:
    """BookSourceValidator.appendCandidate key: `UrlWithOptions.parse(bookUrl).url` trimmed and lower-cased (R151)."""
    return parse_url_options(str(book_url or ""))[0].strip().lower()


def explore_js_url_script(built_url: str) -> str | None:
    """App explore() `isJsExplore` (BookSourceEngine+Flagship.swift:151-152): after the URL build the trimmed URL still
    starts with `@js:` (case-sensitive), or is a `<js>…</js>` block. Returns the script (`dropFirst(4)` / `dropFirst(4)
    .dropLast(5)`), else None (R151, E2)."""
    trimmed = (built_url or "").strip()
    if trimmed.startswith("@js:"):
        return trimmed[4:]
    if trimmed.startswith("<js>") and trimmed.endswith("</js>"):
        return trimmed[4:-5]
    return None


def evaluate_explore_js_url(runtime: RuleRuntime, script: str, headers: dict[str, str]) -> tuple[str, str, int]:
    """Run an `@js:` / `<js>` explore URL locally (no request): input "", the merged request headers, a response of
    (script output, the source's effective base URL, 200). Variables the script puts stay visible to the list parse; a
    script error is recorded like every other source-script error (the stage's finish_script_error_stage throws it,
    App: RuleScriptError -> js失效)."""
    base = runtime.base_url
    script_runtime = RuleRuntime(**{
        **runtime.__dict__,
        "headers": dict(headers or {}),
        "source_url": base,
        "base_url": base,
        "book_binding": "unbound",
    })
    result = evaluate_js(script, "", script_runtime)
    if script_runtime.variables is not None and script_runtime.variables is not runtime.variables:
        runtime.variables = script_runtime.variables
    if script_runtime.script_error and not runtime.script_error:
        runtime.script_error = script_runtime.script_error
    return str(result if result is not None else ""), base, 200


def detect_content_is_json(content: str) -> bool:
    """BookSourceEngine.detectContentType == .json: a `{` / `[` body, or JSONP `callback({…})` (R151)."""
    text = (content or "")
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.strip()
    if text.startswith(("{", "[")):
        return True
    match = re.match(r"\w+\s*\(", text)
    if match:
        return text[match.end():].lstrip(" \t\u00a0\u3000").startswith(("{", "["))
    return False


def extract_jsonp_body(content: str) -> str | None:
    """BookSourceEngine.extractJSONPBody (BookSourceEngine.swift:2120): the text between the first `(` and the last `)`
    when it is a JSON object / array; None for a plain JSON body or anything else (R151)."""
    text = (content or "").strip()
    if text.startswith(("{", "[")):
        return None
    open_index = text.find("(")
    close_index = text.rfind(")")
    if open_index < 0 or close_index < open_index:
        return None
    inner = text[open_index + 1:close_index].strip()
    return inner if inner.startswith(("{", "[")) else None


def collect_app_book_candidates(
    src: dict[str, Any],
    rule: dict[str, Any],
    items: list[str],
    runtime2: RuleRuntime,
    *,
    first_only: bool = False,
) -> tuple[list[str], dict[str, str] | None, str]:
    """Per-item extraction exactly like BookSourceEngine.parseSearchResult / parseExploreResult (R144a F4).

    R156 (network T): `first_only` (the batch validation passes it) stops after the first acceptable item, like the App's validation search
    `engine.search(maximumResults: 1)` / `explore(maximumResults: 1)` (`BookSourceValidator` search / explore stages); every
    further item would cost a full set of rule / JS evaluations (3000+ items on some sources). The workbench passes
    False to list every candidate.

    * name: the rule result; the `<a>` / title / JSON-key recovery runs only when the name rule is blank
      (App E:931-957: `lacksRule`). A non-blank rule that yields nothing skips the item. No placeholder name.
    * bookUrl: the rule result; an empty result is the response URL (Legado isUrl fallback, E:1083-1085); the
      heuristic `href` recovery runs only when the bookUrl rule is blank. A URL that is not usable
      (`isUsableRuleExtractedBookUrl`) skips the item - there is no second guess from the item's own links.
    Returns (book_urls, variables of the first selected item, that item's author).
    """
    name_rule = str(rule.get("name") or "")
    url_rule = str(rule.get("bookUrl") or "")
    name_rule_blank = not name_rule.strip()
    url_rule_blank = not url_rule.strip()
    book_urls: list[str] = []
    selected_variables: dict[str, str] | None = None
    selected_author = ""
    dbg_skipped = 0
    legado_field_order = app_profiles.current_app_profile().list_item_legado_field_order
    for item in items:
        item_runtime = RuleRuntime(**{**runtime2.__dict__, "variables": dict(runtime2.variables or {})})
        name = "" if name_rule_blank else first_result_to_text(item, name_rule, item_runtime)
        name_recovered = False
        if not name.strip() and name_rule_blank:
            name = fallback_name_from_item(item)
            name_recovered = bool(name.strip())
        if _dbg.active() and item is items[0]:
            _dbg.emit("field", field="name", rule=name_rule, value=name)
        if not name.strip():
            dbg_skipped += 1
            continue
        if name_recovered and not is_usable_search_book_title(name):
            dbg_skipped += 1
            continue
        item_author = ""
        if legado_field_order:
            # R156 (J, App profile 1.1.2): BookList.kt:220-281 evaluates name, author, kind, wordCount, lastChapter, intro,
            # coverUrl and only then bookUrl, so an `@put` / `java.put` in any of them is visible to bookUrl.
            item_author = capture_search_item_rule_variables(rule, item, item_runtime)
        if url_rule_blank:
            book_url = fallback_url_from_item(item, runtime2.base_url, item_runtime)
        else:
            book_url = first_result_to_book_url(src, item, url_rule, item_runtime, str(rule.get("kind") or ""))
        if not str(book_url or "").strip():
            book_url = runtime2.base_url
        if _dbg.active() and item is items[0]:
            _dbg.emit("field", field="bookUrl", rule=url_rule, value=book_url)
        if not str(book_url or "").strip() or not is_usable_rule_extracted_book_url(book_url, item, name, item_runtime):
            dbg_skipped += 1
            continue
        if not legado_field_order:
            item_author = capture_search_item_rule_variables(rule, item, item_runtime)
        book_urls.append(book_url)
        if _dbg.active() and len(book_urls) <= 50:
            _dbg.emit("item", index=len(book_urls) - 1, name=name, value=book_url)
        if selected_variables is None:
            selected_variables = dict(item_runtime.variables or {})
            # R158 (H, qq `novel.html5.qq.com/`; both profiles): SearchBook.toBook() carries kind / lastChapter / intro / coverUrl into the Book that
            # the detail / toc stages see (`{{book.kind}}` in ruleBookInfo.tocUrl when the detail JSON has no kind of its own; App
            # SearchBook.kind -> Book.kind, BookSourceEngine.swift). Reserved `__book_*` keys carry them through `runtime.variables`.
            for _state_key in _SELECTED_BOOK_STATE_KEYS:
                _state_value = str(getattr(item_runtime, _state_key, "") or "")
                if _state_value:
                    selected_variables["__" + _state_key] = _state_value
            selected_author = item_author
        if first_only:
            break
    if _dbg.active() and dbg_skipped:
        _dbg.emit("warn", code="items_skipped", count=dbg_skipped,
                  value=f"列表 {len(items)} 项中 {dbg_skipped} 项因书名为空或详情链接不可用被跳过")
    return book_urls, selected_variables, selected_author


_FIRST_CANDIDATE_ONLY: "contextvars.ContextVar[bool]" = contextvars.ContextVar("readori_first_candidate_only", default=False)


def has_effective_explore_book_list(src: dict[str, Any]) -> bool:
    """R157 (searchExplore G5): an explore entry list needs a `bookList`, but a blank `ruleExplore.bookList` falls back to the
    search rule (Legado BookSource.getExploreRule :217 / CheckSourceService.kt:154-205; App `effectiveExploreRule`,
    BookSourceEngine+Flagship.swift:293-312). The pre-check used to reject such a source as "missing search/explore rule"
    before `fetch_book_candidates` (which already had the fallback) ran. The Accept header is NOT part of this."""
    explore_rule = src.get("ruleExplore")
    if isinstance(explore_rule, dict) and str(explore_rule.get("bookList") or "").strip():
        return True
    search_rule = src.get("ruleSearch")
    return isinstance(search_rule, dict) and bool(str(search_rule.get("bookList") or "").strip())


def _fetch_first_book_candidates(session: requests.Session, src: dict[str, Any], runtime: RuleRuntime) -> tuple[list[str], str]:
    """Batch validation search / discovery stage (R156 network T): `fetch_book_candidates` with the App's one-result window."""
    token = _FIRST_CANDIDATE_ONLY.set(True)
    try:
        return fetch_book_candidates(session, src, runtime)
    finally:
        _FIRST_CANDIDATE_ONLY.reset(token)


def search_book_from_detail_rules(src: dict[str, Any], runtime: RuleRuntime, body: str, final_url: str) -> tuple[str, str, dict[str, str]] | None:
    """R158 (G8, both profiles; huarenv5): Legado BookList.analyzeBookList (:59-87) parses the response as a DETAIL page (`getInfoItem`,
    ruleBookInfo init / name / author) when the list is empty and `bookUrlPattern` is empty; a non-blank name makes a book whose url is the
    response url. App: `extractSearchBookFromDetailRules` (BookSourceEngine.swift ~1317; search at `bookItems.isEmpty`, explore in
    BookSourceEngine+Flagship.swift) - rule driven only, no heuristics. Returns (book url, author, variables) or None: `variables` are the
    `@put` values the info rules stored (App `capturedVariables = detailContext.serializedVariables()`, kept on the SearchBook)."""
    if str(src.get("bookUrlPattern") or "").strip():
        return None
    info_rule = src.get("ruleBookInfo")
    if not isinstance(info_rule, dict):
        return None
    name_rule = str(info_rule.get("name") or "").strip()
    if not name_rule or not final_url.strip():
        return None
    info_runtime = RuleRuntime(**{**runtime.__dict__, "source_url": final_url, "base_url": final_url, "book_url": final_url,
                                  "book_toc_url": final_url, "variables": dict(runtime.variables or {})})
    content = body
    try:
        init_rule = str(info_rule.get("init") or info_rule.get("initJs") or "").strip()
        if init_rule:
            transformed = evaluate_init_rule_nonthrowing(init_rule, content, info_runtime, "json" if is_json_content(content) else "html")
            if transformed:
                content = transformed
        content_type = "json" if is_json_content(content) else "html"
        name = legado_get_string(name_rule, content, info_runtime, content_type).strip()
        if not name:
            return None
        author = legado_get_string(str(info_rule.get("author") or ""), content, info_runtime, content_type).strip()
        # The App also evaluates intro / kind / lastChapter / updateTime / wordCount / coverUrl before capturing the variables, so an `@put`
        # inside any of them reaches the later stages.
        for field in ("intro", "kind", "lastChapter", "updateTime", "wordCount", "coverUrl"):
            field_rule = str(info_rule.get(field) or "").strip()
            if not field_rule:
                continue
            try:
                evaluate_rule(field_rule, content, info_runtime, content_type)
            except SourceValidationDeadlineExceeded:
                raise
            except Exception:
                continue
    except SourceValidationDeadlineExceeded:
        raise
    except Exception:
        return None
    before = dict(runtime.variables or {})
    captured = {key: value for key, value in (info_runtime.variables or {}).items()
                if not str(key).startswith("__") and before.get(key) != value}
    return final_url, author, captured


def fetch_book_candidates(
    session: requests.Session,
    src: dict[str, Any],
    runtime: RuleRuntime,
    *,
    keywords: list[str] | None = None,
    modes: tuple[str, ...] = ("search", "explore"),
    explore_url: str | None = None,
) -> tuple[list[str], str]:
    """Search / discovery stage. R146 (debug): `keywords` replaces the validation keyword list, `modes` limits the
    stage to search and / or explore, `explore_url` replaces the first `exploreUrl` entry. Defaults = batch behavior.
    R156 (network T): inside `_fetch_first_book_candidates` (the batch validation) every entry stops at its first acceptable
    item like the App's `maximumResults: 1`; called directly it lists every candidate (workbench, tests)."""
    begin_network_stage()
    runtime.explore_candidate = None
    search_rule = src.get("ruleSearch") or {}
    explore_rule = src.get("ruleExplore") or {}
    if isinstance(search_rule, dict) and isinstance(explore_rule, dict) and not str(explore_rule.get("bookList") or "").strip():
        explore_rule = search_rule
    has_search = bool(str(src.get("searchUrl") or "").strip()) and isinstance(search_rule, dict) and bool(str(search_rule.get("bookList") or "").strip())
    has_explore = bool(str(src.get("exploreUrl") or "").strip()) and isinstance(explore_rule, dict) and bool(str(explore_rule.get("bookList") or "").strip())
    # R147 (task 1): exactly the App's single validation keyword (checkKeyWord verbatim, else "我的"), no rotation.
    custom_keywords = [str(item) for item in (keywords or []) if str(item) != ""]
    keywords = custom_keywords or app_validation_keywords(search_rule.get("checkKeyWord") if isinstance(search_rule, dict) else None)
    if explore_url and isinstance(explore_rule, dict) and str(explore_rule.get("bookList") or "").strip():
        has_explore = True

    # R147 (task 2): App doCheckSource records `发现规则为空` (an info label, NOT a failure) when the exploreUrl yields no
    # category link; only a link that returns no book is `发现失效`.
    explore_link_empty = [False]
    # R151 (E1): the source-level variable state (before any search / discovery item bound its own `@put` values).
    source_level_variables = dict(runtime.variables or {})
    # R151 (E1): variables / author / base of the first discovery item, filled by try_mode("explore") (the explore pass
    # that follows a passing search turns it into `runtime.explore_candidate`).
    explore_capture: dict[str, Any] = {}

    def try_mode(mode: str) -> tuple[list[str], str]:
        if mode not in modes:
            return [], ""
        if mode == "search" and not has_search:
            return [], ""
        if mode == "explore" and not has_explore:
            return [], ""
        if _dbg.active():
            _dbg.set_stage(mode)
        if mode == "explore":
            template = explore_url or build_first_explore_url(
                str(src.get("exploreUrl") or ""), runtime, resolve_categories=True, raw_template=True
            )
            if not template:
                explore_link_empty[0] = True
                return [], ""
            # R150: the category URL is a full AnalyzeUrl template (App explore(): buildUrlWithOptionsThrowing with keyword ""
            # and page 1), so `url,{method, body, headers, charset}` options and `{{js}}` / `{{page}}` apply to the discovery
            # request just as they do to search. It used to be fetched with a bare GET of the unresolved string.
            req = request_search_or_explore(session, src, runtime, "", page=1, mode="explore", template=template)
            if not req:
                explore_link_empty[0] = True
                return [], ""
            base_url, explore_method, explore_params = req
            explore_js = explore_js_url_script(base_url)
            if explore_js is not None:
                # R151 (E2): the built URL is still `@js:` / `<js>…</js>` (App explore(): isJsExplore) - the script is
                # evaluated locally, there is no request, no HTTP-status check and no login check.
                html, final_url, status = evaluate_explore_js_url(runtime, explore_js, explore_params["headers"])
                if _dbg.active():
                    _dbg.emit("info", code="explore_js", url=final_url, count=len(html),
                              value="发现链接构建后仍是 @js:/<js> 脚本：本地执行，不发请求，结果当作发现页内容")
            else:
                response_meta: dict[str, Any] = {}
                html, final_url, status = fetch_text(
                    session,
                    base_url,
                    method=explore_method,
                    headers=explore_params["headers"],
                    body=explore_params["body"],
                    charset=explore_params["charset"],
                    response_meta=response_meta,
                )
                if status == 0 and not html:
                    return [], final_url
                if app_http_failure(base_url, status, html, "explore", src):
                    if _dbg.active():
                        _dbg.emit("warn", code="http_error", url=base_url, status=status)
                    return [], final_url
                login_ok, html, final_url = apply_login_check_js(
                    src, runtime, html, final_url, base_url, status,
                    request_method=explore_method,
                    request_body=str(explore_params["body"] or ""),
                    request_headers=explore_params["headers"],
                    response_headers=response_meta.get("headers", {}),
                    response_cookies=response_meta.get("cookies", {}),
                )
                if not login_ok:
                    if _dbg.active():
                        _dbg.emit("warn", code="login_check", url=final_url)
                    return [], final_url
            # R149 (B7): no landing-page guard here. The App's explore() (BookSourceEngine+Flagship.swift:100-263) only
            # applies it to search (BookSourceEngine.swift:250-268), so a category such as "首页" (/) is judged by
            # whether the explore bookList rule finds books.
            # Match Legado BookList: explicit rules see the fetched body before
            # generic marker classification. Search pages often bundle dormant
            # captcha/404/empty-state templates while still containing books.
            error_page = contains_http_error_page_signals(html)
            if is_likely_empty_search_json(html):
                if _dbg.active():
                    _dbg.emit("warn", code="empty_json", url=final_url)
                return [], final_url
            runtime2 = RuleRuntime(**{**runtime.__dict__, "source_url": final_url, "base_url": final_url})
            # R144a (F4): bookList has a rule here (has_explore), and the App's list heuristics (JSON wrapper
            # recovery, JSON key lists, HTML selector guesses) run only when the bookList rule is blank.
            # R151 (E2): App explore() unwraps a JSONP body (`callback({...})`) before parseExploreResult, for both the
            # request and the `@js:` branch (BookSourceEngine+Flagship.swift:224-227, BookSourceEngine.extractJSONPBody).
            explore_body = html
            if detect_content_is_json(html):
                explore_body = extract_jsonp_body(html) or html
            items = evaluate_book_list(str(explore_rule.get("bookList") or ""), explore_body, runtime2)
            if _dbg.active():
                _dbg.emit("list", field="bookList", rule=str(explore_rule.get("bookList") or ""), count=len(items),
                          value=items[0][:2000] if items else "")
            if not items:
                detail_book = search_book_from_detail_rules(src, runtime2, explore_body, final_url)
                if detail_book is not None:
                    if _dbg.active():
                        _dbg.emit("info", code="detail_page", url=final_url, value="bookList 为空：按 ruleBookInfo 把响应当作详情页（Legado BookList getInfoItem）")
                    explore_capture.clear()
                    explore_capture.update({"base": final_url, "variables": dict(detail_book[2]), "author": detail_book[1]})
                    runtime.validation_book_author = detail_book[1]
                    if detail_book[2]:
                        runtime.variables = dict(runtime.variables or {})
                        runtime.variables.update(_apply_selected_book_state(runtime, detail_book[2]))
                    return [detail_book[0]], final_url
                if error_page:
                    runtime.variables = dict(runtime.variables or {})
                    runtime.variables["__searchChallenge"] = "1"
                    if _dbg.active():
                        _dbg.emit("warn", code="challenge", url=final_url)
                return [], ""
            book_urls, selected_variables, selected_author = collect_app_book_candidates(src, explore_rule, items, runtime2, first_only=_FIRST_CANDIDATE_ONLY.get())
            if book_urls:
                # R151 (E1): the first selected item is the App's `.discovery` candidate (its own variables / author).
                explore_capture.clear()
                explore_capture.update({"base": final_url, "variables": dict(selected_variables or {}), "author": selected_author})
            if selected_variables:
                runtime.variables = dict(runtime.variables or {})
                runtime.variables.update(_apply_selected_book_state(runtime, selected_variables))
            if book_urls:
                runtime.validation_book_author = selected_author
            if not book_urls and error_page:
                runtime.variables = dict(runtime.variables or {})
                runtime.variables["__searchChallenge"] = "1"
                if _dbg.active():
                    _dbg.emit("warn", code="challenge", url=final_url)
            return book_urls, final_url

        # search
        for kw in keywords:
            if _dbg.active():
                _dbg.emit("keyword", value=kw)
            req = request_search_or_explore(session, src, runtime, kw, mode="search")
            if not req:
                if _dbg.active():
                    _dbg.emit("warn", code="no_request", value=kw)
                continue
            url, method, params = req
            response_meta = {}
            html, final_url, status = fetch_text(
                session,
                url,
                method=method,
                headers=params["headers"],
                body=params["body"],
                charset=params["charset"],
                response_meta=response_meta,
            )
            if status == 0 and not html:
                return [], final_url
            if app_http_failure(url, status, html, "search", src):
                if _dbg.active():
                    _dbg.emit("warn", code="http_error", url=url, status=status)
                return [], final_url
            login_ok, html, final_url = apply_login_check_js(
                src, runtime, html, final_url, url, status,
                request_method=method,
                request_body=str(params["body"] or ""),
                request_headers=params["headers"],
                response_headers=response_meta.get("headers", {}),
                response_cookies=response_meta.get("cookies", {}),
            )
            if not login_ok:
                if _dbg.active():
                    _dbg.emit("warn", code="login_check", url=final_url)
                continue
            # R144a (#12): ruleSearch.init runs after the login check and before the redirect guard (App E:236-244).
            search_init_rule = str(search_rule.get("init") or search_rule.get("initJs") or "").strip()
            if search_init_rule:
                init_runtime = RuleRuntime(**{**runtime.__dict__, "source_url": final_url, "base_url": final_url})
                transformed_html = evaluate_init_rule_nonthrowing(
                    search_init_rule, html, init_runtime, "json" if is_json_content(html) else "html"
                )
                if transformed_html:
                    html = transformed_html
                if _dbg.active():
                    _dbg.emit("field", field="init", rule=search_init_rule,
                              value=transformed_html[:2000] if transformed_html else "")
            # R155 (I): BookSourceEngine.swift:246-262 consults the landing guard only after a real redirect
            # (finalUrl != searchUrl); Legado has no such guard. A search URL that merely looks like a landing page
            # (e.g. `wap/index.php?keyword=...` answering with results directly) must not be dropped.
            if response_url_differs_from_request(url, final_url) and is_likely_search_or_explore_landing_url(final_url, runtime):
                if _dbg.active():
                    _dbg.emit("warn", code="landing", url=final_url)
                continue
            # Rule-first parity with the iOS runtime and Legado. Only classify
            # the response as a challenge/error page if no usable rule result
            # survives below.
            error_page = contains_http_error_page_signals(html)
            if is_likely_empty_search_json(html):
                if _dbg.active():
                    _dbg.emit("warn", code="empty_json", url=final_url)
                continue
            if matches_book_url_pattern(src, final_url):
                if _dbg.active():
                    _dbg.emit("info", code="detail_page", url=final_url)
                return [final_url], final_url
            runtime2 = RuleRuntime(**{**runtime.__dict__, "source_url": final_url, "base_url": final_url})
            # R144a (F4): no list heuristics after a rule-driven bookList (App: blank bookList only).
            items = evaluate_book_list(str(search_rule.get("bookList") or ""), html, runtime2)
            if _dbg.active():
                _dbg.emit("list", field="bookList", rule=str(search_rule.get("bookList") or ""), count=len(items),
                          value=items[0][:2000] if items else "")
            if not items:
                detail_book = search_book_from_detail_rules(src, runtime2, html, final_url)
                if detail_book is not None:
                    if _dbg.active():
                        _dbg.emit("info", code="detail_page", url=final_url, value="bookList 为空：按 ruleBookInfo 把响应当作详情页（Legado BookList getInfoItem）")
                    runtime.validation_book_author = detail_book[1]
                    if detail_book[2]:
                        runtime.variables = dict(runtime.variables or {})
                        runtime.variables.update(_apply_selected_book_state(runtime, detail_book[2]))
                    return [detail_book[0]], final_url
                if error_page:
                    runtime.variables = dict(runtime.variables or {})
                    runtime.variables["__searchChallenge"] = "1"
                    if _dbg.active():
                        _dbg.emit("warn", code="challenge", url=final_url)
                continue
            book_urls, selected_variables, selected_author = collect_app_book_candidates(src, search_rule, items, runtime2, first_only=_FIRST_CANDIDATE_ONLY.get())
            if book_urls:
                if selected_variables:
                    runtime.variables = dict(runtime.variables or {})
                    runtime.variables.update(_apply_selected_book_state(runtime, selected_variables))
                # R157 (tocContent E mowan): the author of the selected item is kept even when it has no variables (the
                # assignment used to sit inside the `if selected_variables` guard).
                runtime.validation_book_author = selected_author
                return book_urls, final_url
            if error_page:
                runtime.variables = dict(runtime.variables or {})
                runtime.variables["__searchChallenge"] = "1"
                if _dbg.active():
                    _dbg.emit("warn", code="challenge", url=final_url)
        return [], ""

    search_urls, search_base = try_mode("search")
    if search_urls:
        # R147 (task 2): the App also runs discovery after a passing search (BookSourceValidator.doCheckSource
        # 2342-2392: "Discovery must still run after a successful search when the user selected both stages") and
        # an exploreUrl whose first category returns no book is `发现失效` (2373), which fails the whole source; a
        # category list without any link is only the info label `发现规则为空` (2391).
        if has_explore and "search" in modes and "explore" in modes and explore_url is None:
            # `has_explore` (exploreUrl set + a list rule; a section without its own bookList uses ruleSearch's, like the
            # App's effectiveExploreRule()). The explore candidate must not replace the search candidate's variables.
            saved_variables = dict(runtime.variables or {})
            saved_author = runtime.validation_book_author
            explore_sink = runtime.script_error_sink
            explore_sink_mark = len(explore_sink) if explore_sink is not None else 0
            # R151 (E1): the App's discovery candidate is an independent SearchBook (own request context), so the explore
            # pass starts from the source-level variables, not from the search item's `@put` state.
            runtime.variables = dict(source_level_variables)
            explore_capture.clear()
            try:
                explore_ok_urls, _explore_ok_base = try_mode("explore")
            finally:
                runtime.variables = saved_variables
                runtime.validation_book_author = saved_author
            if explore_ok_urls:
                # BookSourceValidator.appendCandidate: candidates are keyed by the lower-cased trimmed URL part of
                # `url,{options}`; a discovery book equal to the search book reuses its result (no second chain).
                explore_book_url = str(explore_ok_urls[0])
                runtime.explore_candidate = {
                    "bookUrl": explore_book_url,
                    "base": str(explore_capture.get("base") or _explore_ok_base or ""),
                    "variables": dict(explore_capture.get("variables") or {}),
                    "author": str(explore_capture.get("author") or ""),
                    "reused": bool(candidate_url_key(explore_book_url))
                    and candidate_url_key(explore_book_url) == candidate_url_key(str(search_urls[0])),
                }
            if not explore_ok_urls and not explore_link_empty[0]:
                note_app_validation_failure(runtime)
                runtime.variables = dict(runtime.variables or {})
                runtime.variables["__exploreFailed"] = "1"
                # R151 (E2): a script error thrown while building / evaluating the discovery request is the App's
                # `js失效` (explore() rethrows RuleScriptError), not a silent empty list.
                explore_script_errors = [
                    str(entry.get("message") or "")
                    for entry in (explore_sink[explore_sink_mark:] if explore_sink is not None else [])
                    if entry.get("kind") == "error"
                ]
                if explore_script_errors:
                    runtime.variables["__exploreScriptError"] = explore_script_errors[0]
                if _dbg.active():
                    _dbg.set_stage("explore")
                    _dbg.emit("warn", code="explore_failed", value="发现失效：搜索通过，但发现页没有可用的书（App 判整个书源失效）")
        return search_urls, search_base
    if has_search and "search" in modes:
        # R144a (F4): the App's batch validation runs search and discovery as independent checks
        # (BookSourceValidator.doCheckSource 2226-2317); an empty search is a `搜索失效` failure on its own and
        # a working discovery does not rescue it. A real search failure also means any interaction blocked from
        # here on is not the cause of the failure group the App already recorded
        # (BookSourceSuppressedInteractionRecorder.failureCountAtFirstRecord).
        note_app_validation_failure(runtime)
        # Discovery still runs like in the App (it can block an interaction, which BookSourceValidator records),
        # but its result never turns the failed search into a pass.
        rescued_urls, _rescued_base = try_mode("explore") if has_explore else ([], "")
        if rescued_urls:
            runtime.variables = dict(runtime.variables or {})
            runtime.variables["__exploreOnlyRescue"] = "1"
        return [], ""
    explore_urls, explore_base = try_mode("explore")
    return explore_urls, explore_base


def rule_text_is_blank(value: Any) -> bool:
    """App `lacksRule` / `isEmpty` after trimming: nil, non-string containers and whitespace are blank."""
    if value is None or isinstance(value, (dict, list)):
        return True
    return not str(value).strip()


def detail_rules_are_blank(info_rule: dict[str, Any]) -> bool:
    """BookSourceEngine `detailRulesAreBlank` (E:9207-9214): every ruleBookInfo field is blank. The App's detail
    heuristics (error / landing page rejection, cookie-free retry) exist only for such sources."""
    return all(rule_text_is_blank(value) for value in (info_rule or {}).values())


def is_heuristic_http_error_page(html: str, status: int) -> bool:
    """Swift `isHeuristicHttpErrorPage` (BookSourceEngine.swift:2363): non-2xx -> body signals; 2xx -> `<title>` only."""
    if not html:
        return False
    if status > 0 and not (200 <= status < 300):
        return contains_http_error_page_signals(html)
    match = re.search(r"<title[^>]*>([\s\S]*?)</title>", html[:65536], flags=re.I)
    if not match:
        return False
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return bool(title) and contains_http_error_page_signals(f"<title>{title}</title>")


def fetch_book_info(session: requests.Session, src: dict[str, Any], runtime: RuleRuntime, book_url: str) -> tuple[bool, str, str, str]:
    if _dbg.active():
        _dbg.set_stage("info")
    result = _fetch_book_info(session, src, runtime, book_url)
    if _dbg.active() and not result[0]:
        _dbg.emit("error", value=result[3] or "详情页没有解析出书名，也没有目录链接")
    return result


def _fetch_book_info(session: requests.Session, src: dict[str, Any], runtime: RuleRuntime, book_url: str) -> tuple[bool, str, str, str]:
    begin_network_stage()
    info_rule = src.get("ruleBookInfo") or {}
    if not isinstance(info_rule, dict):
        info_rule = {}
    # R144a: every detail heuristic is gated by the same "rule is blank" test the App uses (F3 / F9 / F10).
    detail_blank = detail_rules_are_blank(info_rule)
    name_rule_blank = rule_text_is_blank(info_rule.get("name"))
    kind_rule_blank = rule_text_is_blank(info_rule.get("kind"))
    toc_rule_blank = rule_text_is_blank(info_rule.get("tocUrl"))
    toc_ruleset = src.get("ruleToc") if isinstance(src.get("ruleToc"), dict) else {}
    chapter_list_blank = rule_text_is_blank((toc_ruleset or {}).get("chapterList"))
    # App E:9147-9190: the tocUrl guesses (JSON keys, og:novel:read_url, links in the page) run only when neither
    # a tocUrl rule nor a chapterList rule exists; otherwise tocUrl falls back to the book URL.
    toc_heuristics_allowed = toc_rule_blank and chapter_list_blank
    abs_url, request_options = resolve_url_with_options(book_url, runtime.base_url, runtime.headers or {})
    request_url = request_url_preserving_typed_data_options(abs_url, request_options)
    request_method = str(request_options.get("method") or "GET")
    request_body = request_options.get("body") if isinstance(request_options.get("body"), str) else None
    request_charset = request_options.get("charset") if isinstance(request_options.get("charset"), str) else None
    request_headers = dict(runtime.headers or {})
    if isinstance(request_options.get("headers"), dict):
        request_headers.update({str(k): str(v) for k, v in request_options["headers"].items() if v is not None})
    response_meta: dict[str, Any] = {}
    html, final_url, status = fetch_text(
        session,
        request_url,
        method=request_method,
        headers=request_headers,
        body=request_body,
        charset=request_charset,
        response_meta=response_meta,
    )
    # App E:8759-8773: the cookie-free retry also needs `source.cookieJarEnabled`.
    if detail_blank and request_method != "POST" and src.get("enabledCookieJar") is not False and not html.strip():
        retry_meta: dict[str, Any] = {}
        retry_html, retry_final_url, retry_status = fetch_text(
            requests.Session(),
            request_url,
            method=request_method,
            headers=request_headers,
            body=request_body,
            charset=request_charset,
            response_meta=retry_meta,
        )
        if retry_html.strip() or retry_status:
            html = retry_html
            final_url = retry_final_url
            status = retry_status
            response_meta = retry_meta
    if app_http_failure(request_url, status, html, "book_info", src):
        return False, "", "", f"HTTP {int(status)}"
    login_ok, html, final_url = apply_login_check_js(
        src, runtime, html, final_url, request_url, status,
        request_method=request_method,
        request_body=str(request_body or ""),
        request_headers=request_headers,
        response_headers=response_meta.get("headers", {}),
        response_cookies=response_meta.get("cookies", {}),
    )
    if not login_ok:
        return False, "", "", "loginCheckJs did not return StrResponse"
    if not html.strip():
        return False, "", "", "detail empty response"
    if detail_blank and final_url and normalized_book_candidate_url_for_comparison(final_url) != normalized_book_candidate_url_for_comparison(abs_url):
        probe_runtime = RuleRuntime(**{**runtime.__dict__, "source_url": request_url, "book_url": book_url})
        if is_likely_search_or_explore_landing_url(final_url, probe_runtime):
            return False, "", "", "detail redirected to source landing page"
    original_html = html
    next_data_json = extract_next_data_json(html) if not is_json_content(html) else ""
    if detail_blank and html.strip() and not is_json_content(html) and not next_data_json and is_heuristic_http_error_page(html, status):
        return False, "", "", "detail http error page"
    # Legado analyzes the response with baseUrl=book.bookUrl and keeps the
    # redirect URL separately. For typed aggregation carriers the response URL
    # is localhost, but rules deliberately test baseUrl.startsWith("data:") to
    # decode the state and dispatch the real detail request.
    runtime2 = RuleRuntime(**{
        **runtime.__dict__,
        "source_url": final_url,
        "base_url": book_url,
        "book_url": book_url,
        "variables": dict(runtime.variables or {}),
        # Legado BookInfo: analyzeRule.setRuleData(book) -> JS `book` is bound.
        "book_binding": "bound",
    })
    seed_runtime_variables_from_url(runtime2, book_url)
    seed_runtime_variables_from_url(runtime2, final_url)
    init_rule = str(info_rule.get("init") or info_rule.get("initJs") or "").strip()
    if init_rule:
        transformed = evaluate_rule(init_rule, html, runtime2, "json" if is_json_content(html) else "html")
        if not transformed and next_data_json:
            transformed = evaluate_rule(init_rule, next_data_json, runtime2, "json")
        if transformed:
            html = transformed[0]
        if _dbg.active():
            _dbg.emit("field", field="init", rule=init_rule, value=transformed[0] if transformed else "")
    name = ""
    toc_url = ""
    if str(info_rule.get("name") or "").strip():
        name_values = evaluate_rule(str(info_rule.get("name")), html, runtime2, "json" if is_json_content(html) else "html")
        if name_values:
            name = clean_recovered_book_name(legado_get_string_value(name_values))
            if name and not is_usable_search_book_title(name):
                name = ""
            runtime2.book_name = name
    if _dbg.active() and str(info_rule.get("name") or "").strip():
        _dbg.emit("field", field="name", rule=str(info_rule.get("name")), value=name)
    # R155 (Q) / R155b (P3-9): Legado BookInfo.kt:68-140 evaluates EVERY info field in order (name, author, kind, wordCount,
    # lastChapter, intro, coverUrl, then tocUrl) and the App does the same, so the `@put:{k:rule}` / `java.put` of a field the
    # validator otherwise ignores still runs before the TOC / content stages ask for it (漫客栈 `wordCount:
    # "@put:{p:$.comic_id}"` feeds `comic_id=@get:{p}` of chapterUrl). Only fields whose rule can have such a side effect are
    # evaluated (once each, in Legado order, interleaved with the fields the validator evaluates itself: name above, kind
    # below); their errors never fail the detail stage (Legado catches them).
    evaluated_info_side_effect_fields: set[str] = set()

    bind_detail_fields = app_profiles.current_app_profile().search_item_fields_visible_to_book_url

    def run_info_side_effect_field(side_effect_field: str) -> None:
        if side_effect_field in evaluated_info_side_effect_fields:
            return
        side_effect_rule = str(info_rule.get(side_effect_field) or "").strip()
        side_effect_low = side_effect_rule.lower()
        bind_field = bind_detail_fields and side_effect_field in ("lastChapter", "intro", "coverUrl") and bool(side_effect_rule)
        if not bind_field and "@put:" not in side_effect_low and "java.put(" not in side_effect_low:
            return
        evaluated_info_side_effect_fields.add(side_effect_field)
        saved_script_error = runtime2.script_error
        try:
            field_text = evaluate_get_string(side_effect_rule, html, runtime2, "json" if is_json_content(html) else "html")
            if bind_field and (field_text or "").strip():
                # R157 REV (1.1.2): BookInfo.kt:86-134 / Swift extractSearchBookFromDetailRules set each evaluated field on the Book
                # before the tocUrl rule runs, so `book.intro` / `book.lastChapter` / `book.coverUrl` are visible to it.
                field_text = field_text.strip()
                if side_effect_field == "lastChapter":
                    runtime2.book_last_chapter = field_text
                elif side_effect_field == "intro":
                    runtime2.book_intro = field_text
                else:
                    runtime2.book_cover_url = legado_absolute_url(field_text, runtime2.base_url)
        except Exception as _swallowed:
            note_debug_error("detail field side effects", _swallowed)
        finally:
            runtime2.script_error = saved_script_error

    run_info_side_effect_field("author")
    kind = ""
    if str(info_rule.get("kind") or "").strip():
        kind_values = evaluate_rule(str(info_rule.get("kind")), html, runtime2, "json" if is_json_content(html) else "html")
        if kind_values:
            kind = legado_joined_string_list(kind_values).strip()
    if not kind and kind_rule_blank and is_json_content(html):
        try:
            detail_json = json.loads(extract_json(html) or html)
        except Exception:
            detail_json = None
        if isinstance(detail_json, dict):
            kind = json_first_text(detail_json, [
                "kind", "category", "cat", "type", "className", "resourceID", "resourceId",
                "data.kind", "data.category", "data.resourceID", "data.resourceId",
                "result.kind", "result.category", "result.resourceID", "result.resourceId",
            ])
    if _dbg.active() and str(info_rule.get("kind") or "").strip():
        _dbg.emit("field", field="kind", rule=str(info_rule.get("kind")), value=kind)
    if kind:
        runtime2.book_kind = kind
        runtime2.variables = runtime2.variables or {}
        runtime2.variables.update({"book.kind": kind, "bookKind": kind, "kind": kind})
    for side_effect_field in ("wordCount", "lastChapter", "intro", "coverUrl"):
        run_info_side_effect_field(side_effect_field)
    if str(info_rule.get("tocUrl") or "").strip():
        toc_rule_text = str(info_rule.get("tocUrl"))
        content_type = "json" if is_json_content(html) else "html"
        toc_values = evaluate_rule(toc_rule_text, html, runtime2, content_type)
        if not any(str(value).strip() for value in toc_values):
            literal_toc = substitute_makeup_rule_templates(toc_rule_text, html, runtime2, content_type)
            literal_toc = substitute_json_placeholders(literal_toc, html)
            if literal_toc.strip().lower().startswith(("http://", "https://", "/", "@js:", "<js>")):
                toc_values = [literal_toc]
        for toc_value in toc_values:
            toc_raw = normalize_book_linked_template_url(
                substitute_variable_references(str(toc_value), runtime2),
                book_url=book_url,
            )
            candidate_toc_url = resolve_plausible_url_preserving_options(toc_raw, final_url)
            # R155 (H): with a tocUrl rule only the pagination-plausibility check applies (BookSourceEngine.swift
            # :9120-9150); the category / list / single-chapter heuristics (is_unusable_toc_url_candidate,
            # is_likely_single_chapter_toc_fallback) are for a source with neither a tocUrl nor a chapterList rule and run
            # on the heuristic paths below. A static asset (`.css`, `.js`, images) stays rejected: it can never be a TOC.
            if candidate_toc_url and not (is_static_asset_url(candidate_toc_url) and not is_likely_toc_data_asset_url(candidate_toc_url)):
                toc_url = candidate_toc_url
                break
        if _dbg.active():
            _dbg.emit("field", field="tocUrl", rule=toc_rule_text, value=toc_url)

    json_obj: Any | None = None
    if is_json_content(html) or extract_json(html):
        try:
            json_obj = json.loads(extract_json(html) or html)
        except Exception:
            json_obj = None

    if isinstance(json_obj, dict):
        if name_rule_blank and not name.strip():
            name = json_first_text(json_obj, [
                "name", "bookName", "book_name", "title", "bookTitle", "novelName", "novel_name",
                "data.name", "data.bookName", "data.title", "result.name", "result.bookName", "result.title",
                "attributes.title", "data.attributes.title", "result.attributes.title",
            ])
            name = clean_recovered_book_name(name)
            if name and not is_usable_search_book_title(name):
                name = ""
        if toc_heuristics_allowed and not toc_url.strip():
            raw_toc = json_first_text(json_obj, [
                "tocUrl", "toc_url", "chapterListUrl", "chapter_list_url", "catalogUrl", "catalog_url",
                "chaptersUrl", "chapters_url", "listUrl", "list_url", "readUrl", "read_url",
                "data.tocUrl", "data.chapterListUrl", "data.catalogUrl", "data.chaptersUrl",
                "result.tocUrl", "result.chapterListUrl", "result.catalogUrl", "result.chaptersUrl",
            ])
            if raw_toc:
                toc_raw = normalize_book_linked_template_url(raw_toc, book_url=book_url, toc_url=toc_url)
                resolved_toc = resolve_plausible_url(toc_raw, final_url)
                if resolved_toc and not is_unusable_toc_url_candidate(resolved_toc, book_url) and not is_likely_single_chapter_toc_fallback(resolved_toc, book_url):
                    toc_url = resolved_toc

    # Legado 兼容兜底：规则未提取到 name/tocUrl 时，尽量从详情页面推断
    if name_rule_blank and not name.strip() and original_html.strip() and not is_json_content(original_html):
        name = (
            clean_recovered_book_name(html_meta_content(original_html, ["og:novel:book_name", "og:title", "book_name", "title"]))
            or html_title_book_name(original_html)
            or clean_recovered_book_name(fallback_name_from_item(original_html))
        )

    if name_rule_blank and not name.strip() and html.strip():
        name = (
            clean_recovered_book_name(html_meta_content(html, ["og:novel:book_name", "og:title", "book_name", "title"]))
            or html_title_book_name(html)
            or clean_recovered_book_name(fallback_name_from_item(html))
        )

    if _dbg.active() and name_rule_blank and name.strip():
        _dbg.emit("fallback", what="书名规则为空，由详情页 JSON 键 / meta / title 兜底取得", value=name)
    rejected_recovered_name = False
    if name.strip() and not is_usable_search_book_title(name):
        name = ""
        rejected_recovered_name = True

    # R150: BookSourceEngine.swift:8895-8898 throws parseError(message or "detail api error") for ANY JSON detail response that
    # isLikelyDetailErrorJSON, before and regardless of what the rules extract (the old gate "name and tocUrl both empty"
    # let an error body pass whenever a rule happened to pull a value from it). HTML pages with embedded JSON are not JSON
    # responses, so the check is limited to a body that is JSON itself.
    if isinstance(json_obj, dict) and not html.lstrip().startswith("<") and is_likely_detail_error_json(json_obj):
        detail_error_text = _app_json_fallback_string(json_obj, ["msg", "message", "error", "errorMsg", "reason"]) or "detail api error"
        if not app_profiles.current_app_profile().detail_error_json_requires_empty_info:
            return False, "", "", detail_error_text
        # R155b J (1.1.2): Legado BookInfo never rejects a body; the App only throws when nothing usable was extracted
        # (no book name and no tocUrl distinct from the book URL; BookSourceEngine.swift getBookInfo).
        stripped_toc_url = toc_url.strip()
        if not name.strip() and (not stripped_toc_url or stripped_toc_url in {book_url, abs_url}):
            return False, "", "", detail_error_text

    should_recover_toc_from_html = toc_heuristics_allowed and (not toc_url.strip() or is_likely_search_or_explore_landing_url(toc_url, runtime2))
    html_for_toc_recovery = original_html if original_html.strip() and not is_json_content(original_html) else html
    if should_recover_toc_from_html and html_for_toc_recovery.strip() and not is_json_content(html_for_toc_recovery):
        raw_toc = html_meta_content(html_for_toc_recovery, ["og:novel:read_url", "read_url", "tocUrl", "chapterListUrl"])
        if raw_toc:
            resolved_toc = resolve_plausible_url(raw_toc, final_url, same_host_as=final_url)
            if resolved_toc and not is_unusable_toc_url_candidate(resolved_toc, book_url) and not is_likely_single_chapter_toc_fallback(resolved_toc, book_url):
                toc_url = resolved_toc
        try:
            soup = BeautifulSoup(html_for_toc_recovery, "html.parser")
            nodes = [] if toc_url.strip() and not is_likely_search_or_explore_landing_url(toc_url, runtime2) else list(soup.select("a[href*='bookcatalog'], a[href*='catalog'], a[href*='chapter'], a[href*='list'], a[href*='mulu'], a[href*='toc']"))
            if not toc_url.strip() or is_likely_search_or_explore_landing_url(toc_url, runtime2):
                for a in soup.select("a[href]"):
                    text = a.get_text(" ", strip=True)
                    read_signal = re.search(r"(\u70b9\u51fb\u9605\u8bfb|\u9ede\u64ca\u95b1\u8b80|\u95b1\u8b80|read)", text, flags=re.I)
                    catalog_signal = re.search(r"(\u76ee\u5f55|\u7ae0\u8282|chapter|contents)", text, flags=re.I)
                    if read_signal or (not nodes and catalog_signal):
                        nodes.append(a)
            for node in nodes:
                href = (node.get("href") or "").strip()
                if href:
                    resolved = resolve_plausible_url(href, final_url, same_host_as=final_url)
                    if resolved and not is_unusable_toc_url_candidate(resolved, book_url) and not is_likely_single_chapter_toc_fallback(resolved, book_url):
                        toc_url = resolved
                        break
        except Exception:
            pass
        if _dbg.active() and toc_url.strip():
            _dbg.emit("fallback", what="目录规则缺失，由详情页链接兜底取得目录地址", value=toc_url)

    # R144a (F3): the book source's own name is never a book name (the App has no such fallback).

    if name.strip() and not is_usable_search_book_title(name):
        name = ""

    # R149 (B5): the App's detail stage (BookSourceValidator.validateCandidate 2455-2480 -> getBookInfo) only fails when
    # the request or a script throws. An unresolved name is the info detail "（书名未解析）" and tocUrl falls back to
    # bookUrl (BookSourceEngine.swift:9123-9150), so `name` and `tocUrl` both empty is not a detail failure; a toc /
    # content failure is judged by those stages. `toc_url` stays "" here and the caller already walks
    # `normalized_toc_url or first_book_url`.
    if runtime.variables is None:
        runtime.variables = {}
    runtime.variables.update(runtime2.variables or {})
    return True, name, toc_url, ""


def html_to_visible_text(html: str) -> str:
    """提取可见文本，与引擎侧解析器选择策略保持一致：
    - RSS/Atom/XML 内容 → xml.etree（对应 Swift RSSSourceEngine.XMLParser 路径）
    - HTML 内容 → BeautifulSoup html.parser（对应 Swift SwiftSoup 路径）
    """
    stripped = html.lstrip()
    # 检测 RSS/Atom/XML 内容（与 Swift RSSSourceEngine 的 XMLParser 路径对应）
    if stripped.startswith("<?xml") or re.match(r"<(?:rss|feed|atom)\b", stripped, re.I):
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(html)
            text = " ".join(root.itertext())
            return re.sub(r"\s{3,}", "\n\n", text).strip()
        except Exception:
            pass  # fallthrough to html.parser
    # HTML 内容（与 Swift SwiftSoup.parse() 行为等价）
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def apply_html_content_fallback(page_content: str, html: str) -> str:
    if not html or len(html) <= 2000:
        return page_content
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return page_content
    selectors = [
        "#content", "#nr1", "#chaptercontent", "#chapterContent", "#BookText",
        ".content", ".readcontent", ".readContent", ".read-content", ".article-content", ".chapter-content",
        "article", "main",
    ]
    best = page_content or ""
    for selector in selectors:
        try:
            node = soup.select_one(selector)
        except Exception:
            node = None
        if not node:
            continue
        text = node.get_text("\n", strip=True)
        if len(text) > len(best):
            best = text
    if len(best) >= max(400, int(len(page_content or "") * 1.3)):
        return best
    return page_content


def _normalized_text_for_quality(text: str) -> str:
    """Swift `normalizedTextForQuality` (BookSourceEngine.swift:6935-6949): drop `[图片:…]` markers and navigation
    words, collapse whitespace. No HTML tag stripping (the App feeds it already cleaned text)."""
    t = text or ""
    t = re.sub(r"\[图片:[^\]]*\]", "", t)
    t = re.sub(r"(?i)(上一页|下一页|返回目录|目录|章节目录|加入书签|收藏本站|推荐本书|chapter list)", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def is_usable_chapter_text(text: str) -> bool:
    """The validator's own final "does the chapter hold readable text" gate (unchanged since before R144a: 30 characters,
    signal 15, digits x0.5). The App's validation (`BookSourceValidator` content stage) passes any non-empty trimmed text or
    any image, so this gate is stricter than the App (it only rejects blank / navigation-like leftovers). It is kept
    separate from `is_meaningful_content` (the App's fallback gate, ported exactly) so that short real chapters are not
    rejected by the stricter 80 / 40 fallback thresholds."""
    normalized = re.sub(r"<[^>]+>", " ", text or "")
    normalized = re.sub(r"\[图片:[^\]]*\]", " ", normalized)
    normalized = re.sub(r"(?i)(上一页|下一页|返回目录|目录|章节目录|加入书签|收藏本站|推荐本书|chapter list)", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return False
    if len(normalized) < 30:
        return False
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", normalized))
    latin_count = len(re.findall(r"[A-Za-z]", normalized))
    digit_count = len(re.findall(r"\d", normalized))
    punct_count = len(re.findall(r"[，。！？；：、,.!?;:]", normalized))
    signal = cjk_count + latin_count + punct_count
    if signal < 15:
        return False
    if signal > 0 and digit_count > 0 and signal <= digit_count * 0.5:
        return False
    return True


def is_meaningful_content(text: str) -> bool:
    """Swift `isMeaningfulContent` (BookSourceEngine.swift:6955-6978), ported line for line: at least 80
    characters, at least 40 signal characters (CJK + Latin + punctuation), signal must exceed 1.2x the digit count,
    and mostly-CJK text needs a punctuation density of at least 1.5% (navigation menus / tag lists have none).
    R144a: this used to be 30 characters / signal 15 / digits x0.5 and had no density check."""
    normalized = _normalized_text_for_quality(text)
    if not normalized or len(normalized) < 80:
        return False
    cjk = len(re.findall(r"[\u4e00-\u9fff]", normalized))
    latin = len(re.findall(r"[A-Za-z]", normalized))
    digits = len(re.findall(r"\d", normalized))
    punct = len(re.findall(r"[，。！？；：、,.!?;:]", normalized))
    signal = cjk + latin + punct
    if signal < 40:
        return False
    if signal <= digits * 1.2:
        return False
    if cjk >= 60 and cjk / max(1, cjk + latin) > 0.6:
        if punct / cjk < 0.015:
            return False
    return True


def has_meaningful_image_content(text: str) -> bool:
    if not text or "<img" not in text.lower():
        return False
    sources = re.findall(r"""<img\b[^>]*(?:src|data-src)\s*=\s*["']?([^"'\s>]+)""", text, flags=re.I)
    return any(src and not src.lower().startswith(("javascript:", "#")) for src in sources)


_LEGADO_FORMAT_WS = "[ \\t\\n\\x0B\\f\\r]"
# Swift `legadoFormatKeepImgSteps` (BookSourceEngine.swift): Legado
# HtmlFormatter.formatKeepImg text part. Java `\s` is ASCII-only, so the
# whitespace class is spelled out; the patterns are case-sensitive like Swift.
_LEGADO_FORMAT_KEEP_IMG_STEPS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pattern), replacement)
    for pattern, replacement in [
        (r"<script[^>]*>[\s\S]*?</script>", ""),
        (r"<style[^>]*>[\s\S]*?</style>", ""),
        (r"<!\[CDATA\[[\s\S]*?\]\]>", ""),
        (r"<\?xml[^>]*\?>", ""),
        ("[﻿​⁠]", ""),
        (r"(&nbsp;)+", " "),
        (r"(&ensp;|&emsp;)", " "),
        ("(&thinsp;|&zwnj;|&zwj;| |‌|‍)", ""),
        (r"</?(?:div|p|br|hr|h\d|article|dd|dl)[^>]*>", "\n"),
        (r"<!--[^>]*-->", ""),
        (r"</?(?!img)[a-zA-Z]+(?=[ >])[^<>]*>", ""),
        (_LEGADO_FORMAT_WS + r"*\n+" + _LEGADO_FORMAT_WS + "*", "\n"),
        (r"^[\n \t\x0B\f\r]+", ""),
        (r"[\n \t\x0B\f\r]+$", ""),
    ]
]
_LEGADO_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)

# Swift `replacingInlineImageTagsWithMarkers` / `legadoOptionImageURL` /
# `extractImages` / `resolveEmbeddedImageURL`: every `<img>` is presented to
# the replaceRegex as Legado's normalized `<img src="ABSOLUTE_URL">`.
_LEGADO_INLINE_IMG_TAG_RE = re.compile(r"""<img\b(?:[^>"']|"[^"<]*"|'[^'<]*')*>""", re.I)
_LEGADO_OPTION_IMG_SRC_RE = re.compile(
    r"""<img[^>]*\ssrc\s*=\s*['"]([^'"{>]*\{(?:[^{}]|\{[^}>]+\})+\})['"][^>]*>""",
    re.I,
)
_LEGADO_LAZY_IMG_ATTRS = (
    "data-original", "data-src", "data-lazy-src", "data-echo",
    "data-url", "data-image", "data-img", "src",
)


def legado_resolve_embedded_image_url(raw: str, base_url: str) -> str:
    """Swift ``resolveEmbeddedImageURL``: absolutize the URL part against the
    base, keep a Legado ``,{options}`` suffix byte-exact."""
    text = (raw or "").strip()
    if not text:
        return ""
    url_part, _opts = parse_url_options(text)
    url_part = url_part.strip()
    if not url_part:
        return ""
    if url_part.lower().startswith("data:image/"):
        resolved = url_part
    elif url_part.startswith("//"):
        resolved = f"{urlparse(base_url or '').scheme or 'https'}:{url_part}"
    else:
        resolved = urljoin(base_url, url_part) if base_url else url_part
    if len(text) > len(url_part):
        return resolved + text[len(url_part):]
    return resolved


def _legado_inline_image_url(tag: str, base_url: str) -> str:
    # Legado formatImagePattern first branch: `src="url,{...}"` (checked first,
    # raw attribute text, no entity decoding -- same as Swift).
    if "{" in tag:
        match = _LEGADO_OPTION_IMG_SRC_RE.search(tag)
        if match:
            resolved = legado_resolve_embedded_image_url(match.group(1).strip(), base_url)
            if resolved:
                return resolved
    try:
        node = BeautifulSoup(tag, "html.parser").find("img")
    except Exception:
        return ""
    if node is None:
        return ""
    for attribute in _LEGADO_LAZY_IMG_ATTRS:
        raw = node.get(attribute)
        if isinstance(raw, list):
            raw = " ".join(raw)
        raw = str(raw or "").strip()
        if not raw:
            continue
        resolved = legado_resolve_embedded_image_url(raw, base_url)
        if resolved:
            return resolved
    return ""


def legado_normalize_inline_image_tags(html: str, base_url: str) -> str:
    """Rewrite every ``<img>`` that has a usable URL to ``<img src="ABS_URL">``
    (lazy attribute priority data-original > data-src > data-lazy-src >
    data-echo > data-url > data-image > data-img > src, relative URLs resolved
    against ``base_url``, option suffix kept). Tags without a URL stay as-is.
    Idempotent."""
    if not html or "<img" not in html.lower():
        return html

    def replace(match: re.Match[str]) -> str:
        tag = match.group(0)
        url = _legado_inline_image_url(tag, base_url)
        return f'<img src="{url}">' if url else tag

    return _LEGADO_INLINE_IMG_TAG_RE.sub(replace, html)


def legado_format_content_keeping_images(html: str) -> str:
    """Legado HtmlFormatter.formatKeepImg text part + unescapeHtml4 (Swift
    ``legadoFormatContentKeepingImages``).

    Block tags and ``<br>`` become newlines; comments and every tag except
    ``<img>`` are removed; entities are decoded. ``<img>`` tags are shielded
    from the whitespace folding / entity decoding so their URLs and request
    options stay byte-exact (Swift does the same with ``[图片:URL]`` markers).
    """
    if not html:
        return ""
    images: list[str] = []

    def placeholder(index: int) -> str:
        digits = "".join(chr(0xE010 + int(ch, 16)) for ch in format(index, "x"))
        return "" + digits + ""

    def shield(match: re.Match[str]) -> str:
        images.append(match.group(0))
        return placeholder(len(images) - 1)

    result = _LEGADO_IMG_TAG_RE.sub(shield, html)
    for regex, replacement in _LEGADO_FORMAT_KEEP_IMG_STEPS:
        result = regex.sub(replacement, result)
    if "&" in result:
        result = html_unescape(result)
    result = result.replace(" ", " ")
    for index in range(len(images) - 1, -1, -1):
        result = result.replace(placeholder(index), images[index])
    return result


def apply_legado_content_replace_regex(
    content: str,
    replace_rule: str,
    runtime: RuleRuntime,
) -> tuple[str, str]:
    """Legado BookContent ``ruleContent.replaceRegex`` (Swift
    ``applyLegadoContentReplaceRegex``).

    Order: formatKeepImg + unescape the merged page text, trim every line, and
    only then run ``analyzeRule.getString(replaceRegex)`` on it, i.e. the rule
    is a normal Legado rule: ``##regex##replacement`` is a plain regex (no
    DOTALL, ``$1`` group references, ``###`` = replaceFirst) and ``<js>`` /
    ``@js:`` / ``{{}}`` segments are evaluated like in any other rule (JS is
    run by the validator's usual evaluate_js fallback).

    Images are presented to the rule as Legado's normalized
    ``<img src="ABSOLUTE_URL">`` (lazy attributes, relative URLs and option
    suffixes resolved exactly like Swift), so rules such as
    ``##src="(.*?)"##src="$1,{'webView':true}"`` or ``##<img\\ssrc="[^"]*ad[^"]*">``
    behave the same. The result is normalized again (a rule may emit relative
    or single-quoted ``<img>``), which keeps it consumable by
    ``has_meaningful_image_content``.

    Returns ``(formatted_text, replaced_text)``; the caller applies the
    validator's quality guard between the two.
    """
    base_url = runtime.base_url or runtime.source_url or ""
    formatted = legado_format_content_keeping_images(legado_normalize_inline_image_tags(content, base_url))
    formatted = "\n".join(line.strip() for line in formatted.splitlines()).strip()
    replaced = legado_get_string(replace_rule, formatted, runtime, "html", unescape=True)
    return formatted, legado_normalize_inline_image_tags(replaced, base_url)


def evaluate_content_rule_with_inline_replace_guard(
    content_expr: str,
    page_html: str,
    runtime: RuleRuntime,
    content_type: str,
) -> str:
    # Legado BookContent: analyzeRule.getString(contentRule.content, unescape = false)
    # -- values joined with "\n", `##` applied once to the joined text, no unescape.
    # R144a (F8): the App's evaluateContentWithInlineReplaceGuard (BookSourceEngine.swift:7008-7016) is a plain
    # evaluateContent - "规则内 `##` 替换的结果总是生效，不再按质量回退到替换前文本". An inline replacement that
    # empties the content therefore leaves the chapter empty (and the content stage failed).
    return legado_get_string(content_expr, page_html, runtime, content_type, unescape=False).strip() if content_expr else ""


def fallback_chapter_items_from_content(html: str, final_url: str) -> list[str]:
    if is_json_content(html):
        json_fallback_paths = [
            "$.chapters[*]", "$.chapterList[*]", "$.chapter_list[*]", "$.catalog[*]", "$.catalogs[*]",
            "$.data.chapters[*]", "$.data.chapterList[*]", "$.data.chapter_list[*]", "$.data.catalog[*]", "$.data.catalogs[*]",
            "$.result.chapters[*]", "$.result.chapterList[*]", "$.result.chapter_list[*]",
            "$.book.chapters[*]", "$.book.chapterList[*]", "$.novel.chapters[*]", "$.novel.chapterList[*]",
            "$.info.episodes.episode[*]", "$.info.episodes.music[*]", "$..info.episodes.episode[*]", "$..info.episodes.music[*]",
            "$.data.list[*]", "$.data.items[*]", "$.data.rows[*]",
            "$.result[*]", "$.result.list[*]", "$.result.items[*]", "$.result.rows[*]",
            "$.list[*]", "$.items[*]", "$.rows[*]", "$[*]", "[*]", "$.data[*]",
        ]
        for path in json_fallback_paths:
            items = evaluate_json_object_list(path, html)
            if items:
                return items
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
        json_ld_items = extract_json_ld_chapter_items_from_soup(soup)
        if json_ld_items:
            return json_ld_items
        app_cover_items = extract_app_cover_data_chapter_items_from_soup(soup)
        if app_cover_items:
            return app_cover_items
        jiuhuai_items = extract_jiuhuai_lists_chapter_items(html)
        if jiuhuai_items:
            return jiuhuai_items
        encoded_items = extract_encoded_data_chapter_items_from_soup(soup, final_url)
        if encoded_items:
            return encoded_items
        nodes = soup.select("#chapter-list a[href], #list a[href], .listmain a[href], .chapter a[href], [class*='chapter'] a[href], dd a[href], li a[href], a[href]")
        out: list[str] = []
        for node in nodes[:240]:
            text = node.get_text(" ", strip=True)
            compact = re.sub(r"\s+", "", text.replace("\u3000", "")).lower()
            has_chapter_signal = (
                re.search(r"\u7b2c.{0,8}[\u7ae0\u8282\u56de\u5377\u8bdd\u8a71]", compact) is not None
                or re.search(r"\d+\s*(\u7ae0|\u8282|\u56de|\u8bdd|\u8a71)", compact) is not None
                or re.search(r"\u90e8\u5206\s*\d+", compact) is not None
                or re.search(r"chapter\s*\d+", compact) is not None
            )
            if has_chapter_signal:
                out.append(str(node))
        return out
    except Exception:
        return []


def extract_encoded_data_chapter_items_from_soup(soup: BeautifulSoup, final_url: str) -> list[str]:
    selectors = "[data-gdx1], [data-gdx], [data-rubru], [data-chapter-url], [data-content-url], [data-read-url]"
    items: list[str] = []
    seen: set[str] = set()
    try:
        for node in soup.select(selectors):
            decoded_url = ""
            for key, value in node.attrs.items():
                key_low = str(key).lower()
                if not (
                    key_low.startswith("data-gdx")
                    or key_low in {"data-rubru", "data-chapter-url", "data-content-url", "data-read-url"}
                ):
                    continue
                raw = value[0] if isinstance(value, list) and value else value
                decoded = decode_base64_url_candidate(str(raw or ""))
                if decoded:
                    resolved, _ = resolve_url_with_options(decoded, final_url)
                    if resolved:
                        decoded_url = resolved.strip()
                        break
            if not decoded_url or decoded_url in seen:
                continue
            title = node.get_text(" ", strip=True)
            for key, value in node.attrs.items():
                if str(key).lower() in {"href", "class"}:
                    continue
                raw = value[0] if isinstance(value, list) and value else value
                candidate = str(raw or "").strip()
                if not candidate or decode_base64_url_candidate(candidate):
                    continue
                candidate = re.sub(r"^\d+", "", candidate).strip()
                if len(candidate) > len(title):
                    title = candidate
            if not title:
                title = "chapter"
            seen.add(decoded_url)
            items.append(
                f'<a href="{html_escape(decoded_url, quote=True)}" data-real="{html_escape(title, quote=True)}">{html_escape(title)}</a>'
            )
            if len(items) >= 240:
                break
    except Exception:
        return []
    return items


def extract_jiuhuai_lists_chapter_items(html: str) -> list[str]:
    match = re.search(r"\bvar\s+lists\s*=\s*(\{.*?\})\s*;", html, flags=re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for chapters in data.values():
        if not isinstance(chapters, list):
            continue
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            book_id = str(chapter.get("bookId") or chapter.get("bookid") or "").strip()
            chapter_id = str(chapter.get("id") or chapter.get("chapterid") or chapter.get("chapterId") or "").strip()
            title = str(chapter.get("chaptername") or chapter.get("chapterName") or chapter.get("name") or "").strip()
            if not book_id or not chapter_id or not title:
                continue
            key = f"{book_id}:{chapter_id}"
            if key in seen:
                continue
            seen.add(key)
            href = f"/zhangjie?bookid={html_escape(book_id, quote=True)}&chapterid={html_escape(chapter_id, quote=True)}"
            items.append(f'<td><a href="{href}">{html_escape(title)}</a></td>')
            if len(items) >= 240:
                return items
    return items


def extract_app_cover_data_chapter_items_from_soup(soup: BeautifulSoup) -> list[str]:
    script = soup.select_one("script#app-cover-data")
    if script is None:
        return []
    raw = script.string or script.get_text("", strip=False)
    if not raw or "chapterEpisodes" not in raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    groups = data.get("chapterEpisodes") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return []
    items: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        episodes = group.get("episodes")
        if not isinstance(episodes, list):
            continue
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            if episode.get("isPublic") is False:
                continue
            if not str(episode.get("url") or "").strip():
                continue
            items.append(json.dumps(episode, ensure_ascii=False))
    return items


def extract_json_ld_chapter_items_from_soup(soup: BeautifulSoup) -> list[str]:
    def type_values(value: Any) -> set[str]:
        if isinstance(value, list):
            return {str(v).lower() for v in value}
        return {str(value).lower()} if value else set()

    def chapter_payload(entry: Any) -> tuple[str, str] | None:
        if not isinstance(entry, dict):
            return None
        item = entry.get("item")
        payload = item if isinstance(item, dict) else entry
        if not isinstance(payload, dict):
            return None
        url = str(payload.get("url") or payload.get("@id") or payload.get("item") or "").strip()
        name = str(payload.get("name") or payload.get("headline") or entry.get("name") or "").strip()
        payload_types = type_values(payload.get("@type"))
        if "chapter" not in payload_types and "/chapter/" not in url.lower():
            return None
        if not url or not name:
            return None
        return name, url

    def collect(obj: Any, out: list[tuple[str, str]]) -> None:
        if isinstance(obj, list):
            for child in obj:
                collect(child, out)
            return
        if not isinstance(obj, dict):
            return
        list_type = "itemlist" in type_values(obj.get("@type"))
        elements = obj.get("itemListElement")
        if list_type and isinstance(elements, list):
            for entry in elements:
                payload = chapter_payload(entry)
                if payload is not None:
                    out.append(payload)
            return
        has_part = obj.get("hasPart")
        if isinstance(has_part, list):
            for entry in has_part:
                payload = chapter_payload(entry)
                if payload is not None:
                    out.append(payload)
        elif isinstance(has_part, dict):
            payload = chapter_payload(has_part)
            if payload is not None:
                out.append(payload)

    pairs: list[tuple[str, str]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.get_text("", strip=True)
        if not text:
            continue
        try:
            obj = json.loads(text)
        except Exception:
            continue
        collect(obj, pairs)
    items: list[str] = []
    seen: set[str] = set()
    for name, url in pairs[:240]:
        if url in seen:
            continue
        seen.add(url)
        items.append(f'<a href="{html_escape(url, quote=True)}">{html_escape(name)}</a>')
    return items


def fetch_dynamic_catalog_items_from_html(session: requests.Session, html: str, final_url: str, runtime: RuleRuntime) -> list[str]:
    has_dynamic_catalog_signal = (
        re.search(r"bookcatalog(?:_\d+)?\.js", html, flags=re.I) is not None
        or re.search(r"bookcatalog_json\.aspx", html, flags=re.I) is not None
    )
    if not has_dynamic_catalog_signal:
        return []
    book_id = ""
    for pattern in [
        r"""bookId\s*=\s*["']?(\d+)["']?""",
        r"""/bookcatalog/(\d+)\.html""",
        r"""/catalog/(\d+)\.html""",
    ]:
        match = re.search(pattern, html, flags=re.I)
        if match:
            book_id = match.group(1)
            break
    if not book_id:
        return []
    api_url = urljoin(final_url, f"/json/bookcatalog_json.aspx?bookid={book_id}")
    json_text, _, status = fetch_text(session, api_url, headers=runtime.headers)
    if status == 0 or not json_text.strip():
        return []
    try:
        obj = json.loads(extract_json(json_text) or json_text)
    except Exception:
        return []
    volumes = obj.get("volumecoll") if isinstance(obj, dict) else None
    if not isinstance(volumes, list):
        return []

    def esc(value: Any) -> str:
        return (
            str(value if value is not None else "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    items: list[str] = []
    for volume in volumes:
        chapters = volume.get("chaptercoll") if isinstance(volume, dict) else None
        if not isinstance(chapters, list):
            continue
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            index = chapter.get("chapterindex")
            name = str(chapter.get("name") or "").strip()
            if index is None or not name:
                continue
            vip = "VIP" if str(chapter.get("isVip") or "") == "1" else ""
            href = f"/book/{book_id}/{index}.html"
            items.append(f'<a href="{href}" class="ui_catalog"><span class="title">{esc(name)}</span><span class="icon">{esc(vip)}</span></a>')
    return items


def is_likely_volume_title(raw_title: str) -> bool:
    title = (raw_title or "").strip()
    if not title:
        return False
    compact = re.sub(r"\s+", "", title.replace("\u3000", ""))
    if re.match(r"^\u7b2c[0-9\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u58f9\u8d30\u53c1\u8086\u4f0d\u9646\u67d2\u634c\u7396\u62fe\u4f70\u4edf]{1,8}[\u5377\u96c6\u7bc7][|/\uff0f:：-].+", compact):
        return False
    if re.search(r"第.{0,8}[章节回]", compact):
        return False
    exact_volumes = {"正文卷", "作品相关", "免费卷", "VIP卷", "vip卷", "上卷", "中卷", "下卷", "外篇", "番外卷"}
    if compact in exact_volumes:
        return True
    patterns = [
        r"^第[0-9零〇一二三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟]{1,8}[卷集篇].{0,20}$",
        r"^[卷集篇][0-9零〇一二三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟]{1,8}.{0,20}$",
        r"^[上中下]卷.{0,20}$",
    ]
    return any(re.search(pattern, compact) for pattern in patterns)


def is_likely_toc_noise_title(raw_title: str, chapter_url: str = "", toc_page_url: str = "") -> bool:
    title = (raw_title or "").strip()
    if not title:
        return True
    compact = re.sub(r"\s+", "", title.replace("\u3000", "")).lower()
    if compact.startswith("{") or compact.startswith("["):
        return True
    if "{$." in compact or "{{" in compact or "}}" in compact:
        return True
    if compact in {"titlename", "chaptername", "chaptertitle", "bookname", "errorlog"}:
        return True
    exact_noise = {
        "上一页", "下一页", "上一章", "下一章", "上章", "下章", "上页", "下页", "首页", "末页", "尾页", "返回目录",
        "目录", "章节目录", "点击阅读", "加入书架", "直达底部", "read", "next", "prev",
        "没有了", "无", "返回", "登录", "登陆", "登录注册", "注册", "我的书架", "书架",
        "电脑版", "手机版", "全部作品", "全部章节", "阅读记录", "版权展示", "就去看",
        "排行榜", "排行", "热门小说", "热门小说搜索", "全部小说", "分类", "搜索", "网站地图", "作家福利", "移动端",
        "正序", "倒序", "反序", "逆序", "顺序",
        ">", "<", ">>", "<<", "»", "«", "›", "‹",
    }
    if compact in exact_noise:
        return True
    if compact in {"\u300e\u6536\u85cf\u5230\u6d4f\u89c8\u5668\u300f", "\u6536\u85cf\u5230\u6d4f\u89c8\u5668", "\u8bbe\u4e3a\u4e3b\u9875", "\u52a0\u5165\u6536\u85cf"}:
        return True
    if compact.lower() in {"\u4e0b\u8f7dapp", "app\u4e0b\u8f7d", "\u5ba2\u6237\u7aef\u4e0b\u8f7d"}:
        return True
    if compact == "\u5168\u4e66":
        return True
    category_noise = {
        "\u7384\u5e7b\u5c0f\u8bf4", "\u4fee\u771f\u5c0f\u8bf4", "\u8a00\u60c5\u5c0f\u8bf4", "\u7a7f\u8d8a\u5c0f\u8bf4",
        "\u4fa6\u63a2\u5c0f\u8bf4", "\u7f51\u6e38\u5c0f\u8bf4", "\u79d1\u5e7b\u5c0f\u8bf4", "\u7075\u5f02\u5c0f\u8bf4",
        "\u5176\u4ed6\u5c0f\u8bf4",
    }
    if compact in category_noise:
        return True
    if re.fullmatch(r"[<>«»›‹]+", compact):
        return True
    if "点击阅读" in compact or "直达底部" in compact:
        return True
    has_chapter_signal = (
        re.search(r"第.{0,8}[章节回卷]", compact) is not None
        or re.search(r"\d+\s*(章|节|回)", compact) is not None
        or re.search(r"\u7b2c.{0,8}[\u8bdd\u8a71]", compact) is not None
        or re.search(r"\d+\s*(\u8bdd|\u8a71)", compact) is not None
        or re.search(r"chapter\s*\d+", compact) is not None
    )
    latest_prefixes = ("\u6700\u65b0", "\u6700\u65b0\u7ae0\u8282", "\u6700\u65b0\u66f4\u65b0", "æœ€æ–°", "æœ€æ–°ç« èŠ‚")
    if has_chapter_signal and compact.startswith(latest_prefixes):
        return True
    if not has_chapter_signal and len(compact) <= 8 and compact.endswith("\u699c"):
        return True
    if not has_chapter_signal and len(compact) <= 8 and "\u6392\u884c" in compact:
        return True
    if not has_chapter_signal and ">" in title and len(compact) <= 40:
        return True
    if "最新章节列表" in compact:
        return True
    noisy_fragments = [
        "加入书签", "方便阅读", "点击注册", "我要创作", "登录", "登陆", "注册", "bookcase",
        "感谢书友评论打赏投票支持", "感謝書友評論打賞投票支持",
    ]
    if not has_chapter_signal and len(compact) <= 24 and any(fragment in compact for fragment in noisy_fragments):
        return True
    if not has_chapter_signal and chapter_url:
        path = (urlparse(chapter_url).path or "").lower()
        if re.search(r"/(book|books|novel|novels|info)/\d+/?$", path):
            return True
    if re.fullmatch(r"(第)?\d{1,3}", compact):
        lower_url = (chapter_url or "").lower()
        lower_toc = (toc_page_url or "").lower()
        if "page=" in lower_url or "offset=" in lower_url or "index_" in lower_url or lower_url == lower_toc:
            return True
    return False


def fallback_search_items_from_content(html: str) -> list[str]:
    if not is_json_content(html):
        return []
    json_fallback_paths = [
        "$.list[*]", "$.items[*]", "$.books[*]", "$.novels[*]", "$.records[*]", "$.rows[*]",
        "$.data.list[*]", "$.data.data[*]", "$.data.items[*]", "$.data.books[*]", "$.data.novels[*]",
        "$.data.bookList[*]", "$.data.records[*]", "$.data.rows[*]",
        "$.result.list[*]", "$.result.items[*]", "$.result.bookList[*]", "$.result.books[*]", "$.result.novels[*]", "$.result.records[*]", "$.result.rows[*]",
        "$[*]", "[*]", "$.data[*]", "$.result[*]",
    ]
    for path in json_fallback_paths:
        items = evaluate_json_object_list(path, html)
        if items and not json_search_items_look_like_wrapper_noise(items):
            return items
    return []


# R156 (network T): BookSourceValidator.validateCandidate asks for `engine.fetchFirstTocPage(book:, chapterLimit: 16)`
# (`BookSourceValidator` TOC stage, `fetchFirstTocPage(chapterLimit: 16)`) = getChapterList(firstPageOnly: true, firstPageChapterLimit: 16).
APP_VALIDATION_TOC_CHAPTER_LIMIT = 16
# R158 (1.1.2 `content_sample_dedups_whole_toc_page`): the rows past the 16-chapter window are scanned for repeated chapter urls only when
# there are at most this many and the scan takes at most TOC_TAIL_SECONDS (the App's first-page tail urls use the same limits); beyond
# either limit the window-only approximation stays.
TOC_TAIL_URL_ROW_CAP = 5000
TOC_TAIL_SECONDS = 3.0
# Deep inspection switch: READORI_VALIDATOR_FULL_TOC=1 makes the batch validation walk the whole table of contents again
# (variant aggregation compares `tocUniqueChapterCount`, which is capped by the window otherwise).
FULL_TOC_WALK = os.environ.get("READORI_VALIDATOR_FULL_TOC", "").strip() == "1"


def _is_usable_content_page_url(url: str) -> bool:
    """App `BookSourceEngine.isUsableContentPageURL`: a plausible http(s) page URL (a `data:` pseudo-URL carrying script parameters counts)."""
    text = (url or "").strip()
    if not text:
        return False
    if text.lower().startswith("data:"):
        return True
    if not (is_plausible_source_url(text) or is_plausible_request_url(text)):
        return False
    url_part = parse_url_options(text)[0].strip()
    low = url_part.lower()
    if not url_part or low.startswith(("javascript:", "mailto:")) or low == "#":
        return False
    scheme = urlparse(url_part).scheme.lower()
    return scheme in {"http", "https"}


_TOC_TAIL_INLINE_TEMPLATE_RE = re.compile(r"\{\{([\s\S]*?)\}\}")


def _toc_tail_url_rule_has_inline_script(rule: str) -> bool:
    """App `looksLikeInlineTemplateScript` over every `{{body}}` of the chapterUrl rule."""
    for match in _TOC_TAIL_INLINE_TEMPLATE_RE.finditer(rule):
        body = match.group(1)
        low = body.lower()
        if "java." in low or "source." in low or "cookie" in low or ";" in body:
            return True
        if re.search(r"[A-Za-z_$][A-Za-z0-9_$]*\s*\(", body):
            return True
    return False


def _toc_tail_chapter_urls(
    tail_items: list[str],
    first_index: int,
    toc_rule: dict[str, Any],
    runtime: RuleRuntime,
    content_type: str,
    final_url: str,
    *,
    reversed_list: bool,
    heuristic_toc: bool,
    name_rule_blank: bool,
    all_items: list[str] | None = None,
    audio_source: bool = False,
) -> set[str]:
    """R158: the chapter urls of the toc rows after the validation window (see `content_sample_dedups_whole_toc_page`). Mirrors App
    `BookSourceEngine.collectWindowOverflowChapterURLs` (BookSourceEngine.swift ~3797; called from `getChapterList` when the 16-row window is full).

    Only a chapterUrl rule without a JS segment or an inline script `{{java..}}` is evaluated (otherwise the window-only approximation stays, an
    empty set); no request is made; a list-less toc (`heuristic_toc`) and a reversed list (it keeps the FIRST occurrence, BookChapterList.kt:119-128)
    skip the scan. Row filters as in Swift: a redundant JSON digest row is skipped; a name rule without JS must evaluate to a non-empty title; a
    url holding `option@` / `@put` / `@get` is skipped; an empty url is the page url (BookChapterList.kt:230-241) but a volume / audio row with an
    empty url (or a volume rule holding JS) is skipped; `#` / `javascript:` / `mailto:` links are the page url; a non-volume url must be
    `_is_usable_content_page_url`. Limits: at most TOC_TAIL_URL_ROW_CAP rows (the FIRST rows are scanned) and TOC_TAIL_SECONDS (checked every
    64 rows); on either limit the urls collected so far are KEPT (Swift `break`), not dropped. A validation deadline is re-raised.
    """
    chapter_url_rule = str(toc_rule.get("chapterUrl") or "").strip()
    chapter_name_rule = str(toc_rule.get("chapterName") or "").strip()
    chapter_list_rule = str(toc_rule.get("chapterList") or "").strip()
    is_volume_rule = str(toc_rule.get("isVolume") or "").strip()

    def has_js_segment(rule: str) -> bool:
        low_rule = rule.lower()
        return "@js:" in low_rule or ("<js>" in low_rule and "</js>" in low_rule)

    # A blank chapterUrl rule is still scanned (Swift: evaluateString("") is "", every row's url becomes the page url).
    if (heuristic_toc or reversed_list or has_js_segment(chapter_url_rule)
            or _toc_tail_url_rule_has_inline_script(chapter_url_rule)):
        return set()
    name_rule_is_checkable = bool(chapter_name_rule) and not has_js_segment(chapter_name_rule)
    volume_rule_blank = not is_volume_rule
    volume_rule_has_js = has_js_segment(is_volume_rule)
    non_digest_ids: set[str] | None = None
    deadline = time.monotonic() + TOC_TAIL_SECONDS
    urls: set[str] = set()
    for offset, tail_item in enumerate(tail_items):
        if offset >= TOC_TAIL_URL_ROW_CAP:
            break
        if offset % 64 == 0 and time.monotonic() > deadline:
            break
        try:
            tail_type = "json" if is_json_content(tail_item) else content_type
            if tail_type == "json":
                if non_digest_ids is None:
                    non_digest_ids = set()
                    for other in (all_items if all_items is not None else tail_items):
                        if not is_json_content(other):
                            continue
                        try:
                            other_obj = json.loads(extract_json(other) or other)
                        except Exception:
                            continue
                        if isinstance(other_obj, dict) and "digest" not in json_first_text(other_obj, ["type"]).lower():
                            other_id = json_first_text(other_obj, ["id", "attributes.id"])
                            if other_id:
                                non_digest_ids.add(other_id)
                try:
                    tail_obj = json.loads(extract_json(tail_item) or tail_item)
                except Exception:
                    tail_obj = None
                if isinstance(tail_obj, dict) and "digest" in json_first_text(tail_obj, ["type"]).lower():
                    chapid = json_first_text(tail_obj, ["attributes.chapid", "chapid", "chapterId", "chapter_id"])
                    if chapid and chapid in non_digest_ids:
                        continue
            tail_runtime = RuleRuntime(**{**runtime.__dict__, "variables": dict(runtime.variables or {}), "chapter_active": True})
            if name_rule_is_checkable:
                with _evaluating_non_url_string_field():
                    title_values = evaluate_rule(chapter_name_rule, tail_item, tail_runtime, tail_type)
                if not (legado_get_string_value(title_values).strip() if title_values else ""):
                    continue
            if chapter_url_rule:
                expanded = expand_regex_group_template(chapter_url_rule, chapter_list_rule, tail_item)
                if expanded != chapter_url_rule and "$" not in expanded:
                    raw_values = [expanded]
                else:
                    with _evaluating_non_url_string_field():
                        raw_values = evaluate_rule(chapter_url_rule, tail_item, tail_runtime, tail_type)
            else:
                raw_values = []
            raw = raw_values[0] if raw_values and raw_values[0].strip() else ""
            raw = substitute_variable_references(raw, tail_runtime)
            if contains_json_placeholder(raw):
                raw = substitute_json_placeholders(raw, tail_item)
            if "{{" in raw and "@css:" in raw:
                raw = substitute_css_placeholders(raw, tail_item, tail_runtime, tail_type)
            if "{{" in raw and "}}" in raw:
                raw = _resolve_js_template_expressions(raw, tail_runtime, input_text=tail_item)
            if contains_json_placeholder(chapter_url_rule) and (not raw.strip() or contains_json_placeholder(raw)):
                literal = substitute_json_placeholders(chapter_url_rule, tail_item)
                literal = substitute_css_placeholders(literal, tail_item, tail_runtime, tail_type)
                literal = substitute_variable_references(literal, tail_runtime)
                literal = _resolve_js_template_expressions(literal, tail_runtime, input_text=tail_item)
                if literal.strip() and not contains_json_placeholder(literal):
                    raw = literal
            if "option@" in raw or "@put" in raw or "@get" in raw:
                continue
            url = resolve_url_preserving_options(raw, final_url) if raw.strip() else ""
            if raw.strip().lower().startswith(("#", "javascript:", "mailto:")):
                url = final_url
            is_volume = False
            if not volume_rule_blank and not volume_rule_has_js:
                with _evaluating_non_url_string_field():
                    volume_values = evaluate_rule(is_volume_rule, tail_item, tail_runtime, tail_type)
                is_volume = legado_is_true(legado_get_string_value(volume_values))
            if not url:
                if audio_source or is_volume or (not volume_rule_blank and volume_rule_has_js):
                    continue
                url = final_url
            if not is_volume and not _is_usable_content_page_url(url):
                continue
            urls.add(url)
        except SourceValidationDeadlineExceeded:
            raise
        except Exception:
            continue
    return urls


def resolve_toc_source(
    session: requests.Session,
    src: dict[str, Any],
    runtime: RuleRuntime,
    book_url: str,
    toc_url: str,
    *,
    first_page_only: bool = False,
) -> tuple[bool, str, str, str, int, int, int]:
    """返回: ok, toc_final_url, first_chapter_url, first_chapter_title, toc_pages_walked, first_page_item_count

    `first_page_only` (R156 network T; the batch validation stage `_run_toc_stage` passes True) mirrors the App's validation window: chapter items are evaluated only until
    APP_VALIDATION_TOC_CHAPTER_LIMIT (16) chapters were collected (`getChapterList` `firstPageChapterLimit`) and the walk stops
    after the first page that produced a chapter (:3568); it is off for a reversed list with a nextTocUrl rule (:2684).
    The default (workbench / debug engine, unit tests) walks the whole table of contents.
    """
    begin_network_stage()
    if _dbg.active():
        _dbg.set_stage("toc")
    # App getChapterList: `ruleToc == nil` throws noTocRule (E:2576-2578); an existing ruleToc without a
    # chapterList rule is NOT an error - the page is read by the raw-link / dynamic-catalog heuristics (R144a F15).
    toc_rule = src.get("ruleToc")
    if not isinstance(toc_rule, dict):
        return False, "", "", "", 0, 0, 0
    chapter_list_rule = str(toc_rule.get("chapterList") or "").strip()
    chapter_name_rule = str(toc_rule.get("chapterName") or "").strip()
    chapter_url_rule = str(toc_rule.get("chapterUrl") or "").strip()
    # BookSourceEngine `tocItemsAreHeuristic` / `tocLacksRule` (E:3018-3023): field heuristics, error-page
    # rejection and the raw-link fallback exist only for sources without a chapterList rule, or for a field whose
    # own rule is blank. A non-blank rule result is never replaced.
    heuristic_toc = not chapter_list_rule
    name_rule_blank = heuristic_toc or not chapter_name_rule
    # R156 (E, App profile 1.1.2): Legado BookChapterList.kt:230-241 - an empty chapterUrl result is the page URL (baseUrl),
    # never the item's first link; that recovery stays only for sources without a chapterList rule.
    url_rule_blank = heuristic_toc or (
        not chapter_url_rule and not app_profiles.current_app_profile().empty_chapter_url_rule_uses_base_url
    )
    has_next_toc_rule = bool(str(toc_rule.get("nextTocUrl") or "").strip())
    toc_list_reversed = len(chapter_list_rule) > 1 and chapter_list_rule.startswith("-")
    window_only = bool(first_page_only) and not (toc_list_reversed and has_next_toc_rule)
    window_chapter_count = 0
    current_profile = app_profiles.current_app_profile()

    normalized_toc_url = (toc_url or "").strip()
    effective_toc_url = normalized_toc_url or book_url
    # R156 (A, App profile 1.1.2): Legado BookChapterList has no `.txt` shortcut - a source with a chapterList rule is read
    # through its rule (the App's `.txt` shortcut in `getChapterList` applies only without a chapterList rule).
    txt_shortcut_allowed = heuristic_toc or not current_profile.txt_shortcut_only_without_toc_rule
    if txt_shortcut_allowed and is_direct_text_document_url(effective_toc_url):
        return True, effective_toc_url, effective_toc_url, "正文", 1, 1, 1

    runtime_seed = RuleRuntime(**{**runtime.__dict__, "variables": dict(runtime.variables or {})})
    seed_runtime_variables_from_url(runtime_seed, book_url)
    seed_runtime_variables_from_url(runtime_seed, effective_toc_url)
    seed_runtime_book_kind_from_variables(runtime_seed)

    start_url = normalize_book_linked_template_url(
        substitute_variable_references(effective_toc_url, runtime_seed),
        book_url=book_url,
        toc_url=normalized_toc_url,
    )
    if not start_url:
        return False, "", "", "", 0, 0, 0

    visited: set[str] = set()
    queue: list[str] = [start_url]
    pages = 0
    first_page_item_count = 0
    first_chapter_final_url = ""
    first_chapter_url = ""
    first_chapter_title = ""
    # R157 REV (1.1.2 `content_sample_dedups_by_url`): the rows of the first 16-chapter window, duplicates included; the sample is
    # chosen after the page loop (keepLastChapterPerURL, then the first remaining chapter).
    window_rows: list[tuple[str, str, RuleRuntime, int, int, dict[str, str]]] = []
    dedup_window = window_only and app_profiles.current_app_profile().content_sample_dedups_by_url
    seen_chapter_urls: set[str] = set()
    # R156 (network G1 / H): the "all chapters" link and the pagination guesses mirror the App's getChapterList: with no nextTocUrl rule
    # (a chapterList rule does not disable them) the first page gets `discoverTocRangeOptionCandidates`, then (>= 15 items)
    # `discoverTocPaginationCandidates`, and only when both found nothing `guessAllChaptersUrlFromHTML`. The App backs the first page's
    # chapters up before the jump (`chaptersBeforeAllChaptersJump`) and restores them when the jump page yields no new chapter (the
    # `newChaptersThisPage == 0` branch) or, App 1.1.2 only, when its request fails; a jump page with fewer but > 0 new chapters keeps its
    # result (the backup is dropped). `guess_origin` remembers, for a guessed page that REPLACES the unprocessed first page, how to
    # re-queue it; `all_chapters_jump_urls` marks the "all chapters" pages.
    guessed_toc_urls: set[str] = set()
    guess_origin: dict[str, tuple[str, str]] = {}
    all_chapters_jump_urls: set[str] = set()
    # R155 (A): Legado BookChapterList:77-104 / :160-175 - a first page whose nextTocUrl yields several URLs fetches them
    # as a one-shot batch and those pages never evaluate nextTocUrl again; a single URL is followed serially (only the
    # first URL of each page). BookSourceEngine.swift:3326-3350 `nextTocRuleBatchUrls` is the App mirror.
    next_toc_batch_urls: set[str] = set()
    while queue and pages < 80:
        current_url = queue.pop(0)
        abs_url, request_options = resolve_url_with_options(current_url, runtime_seed.base_url, runtime_seed.headers or {})
        if not abs_url:
            continue
        request_url = request_url_preserving_typed_data_options(abs_url, request_options)
        request_method = str(request_options.get("method") or "GET")
        request_body = request_options.get("body") if isinstance(request_options.get("body"), str) else None
        request_charset = request_options.get("charset") if isinstance(request_options.get("charset"), str) else None
        request_headers = dict(runtime_seed.headers or {})
        if isinstance(request_options.get("headers"), dict):
            request_headers.update({str(k): str(v) for k, v in request_options["headers"].items() if v is not None})
        visit_key = f"{request_method.upper()} {request_url} {request_body or ''}"
        if visit_key in visited:
            continue
        visited.add(visit_key)

        response_meta: dict[str, Any] = {}
        html, final_url, status = fetch_text(
            session,
            request_url,
            method=request_method,
            headers=request_headers,
            body=request_body,
            charset=request_charset,
            response_meta=response_meta,
        )
        if heuristic_toc and request_method != "POST" and src.get("enabledCookieJar") is not False and not html.strip():
            retry_meta: dict[str, Any] = {}
            retry_html, retry_final_url, retry_status = fetch_text(
                requests.Session(),
                request_url,
                method=request_method,
                headers=request_headers,
                body=request_body,
                charset=request_charset,
                response_meta=retry_meta,
            )
            if retry_html.strip() or retry_status:
                html = retry_html
                final_url = retry_final_url
                status = retry_status
                response_meta = retry_meta
        jump_transport_failure = current_url in all_chapters_jump_urls and not status and not html.strip()
        if jump_transport_failure or app_http_failure(request_url, status, html, "toc", src):
            # App getChapterList catch block: a page reached through a nextTocUrl rule that answers with an HTTP error counts as an
            # empty page; any other request error fails the whole table of contents.
            if _dbg.active():
                _dbg.emit("warn", code="http_error", url=request_url, status=status)
            if current_url in all_chapters_jump_urls and not app_profiles.current_app_profile().all_chapters_jump_failure_restores:
                # App 1.1.1: the first page's chapters were cleared for the jump and a failed jump page does not restore them.
                return False, "", "", "", pages + 1, first_page_item_count, len(seen_chapter_urls)
            if current_url in guessed_toc_urls:
                pages += 1
                origin = guess_origin.pop(current_url, None)
                if origin is not None:
                    visited.discard(origin[1])
                    queue.insert(0, origin[0])
                continue
            if pages == 0 or not has_next_toc_rule:
                return False, "", "", "", pages, first_page_item_count, len(seen_chapter_urls)
            pages += 1
            continue
        login_ok, html, final_url = apply_login_check_js(
            src, runtime_seed, html, final_url, request_url, status,
            request_method=request_method,
            request_body=str(request_body or ""),
            request_headers=request_headers,
            response_headers=response_meta.get("headers", {}),
            response_cookies=response_meta.get("cookies", {}),
        )
        if not login_ok:
            if _dbg.active():
                _dbg.emit("warn", code="login_check", url=final_url)
            pages += 1
            continue
        if heuristic_toc and not is_json_content(html) and is_heuristic_http_error_page(html, status):
            book_abs = urljoin(runtime_seed.base_url, book_url)
            book_key = f"GET {book_abs} "
            if book_abs and book_key not in visited and book_abs not in queue and book_abs != abs_url:
                queue.insert(0, book_abs)
                continue
            pages += 1
            continue
        runtime2 = RuleRuntime(**{
            **runtime_seed.__dict__,
            "source_url": final_url,
            # BookChapterList receives the requested absolute book.tocUrl, not
            # the redirected/synthetic response URL.
            "base_url": request_url,
            "book_url": book_url,
            "book_toc_url": effective_toc_url,
            "variables": dict(runtime_seed.variables or {}),
            # Legado BookChapterList: setRuleData(book) binds `book` (also for chapterList).
            "book_binding": "bound",
        })
        seed_runtime_variables_from_url(runtime2, final_url)
        content_type = "json" if is_json_content(html) else "html"
        if chapter_list_rule:
            items = evaluate_book_list(chapter_list_rule, html, runtime2)
        else:
            items = []
        # Legado passes the same mutable Book entity through every TOC page.
        # Preserve chapterList JS side effects (including upCustomIntro) in
        # both the caller-visible runtime and the seed for the next page.
        merge_runtime_book_state(runtime_seed, runtime2)
        merge_runtime_book_state(runtime, runtime2)
        # R144a (F2): "原始链接/动态目录兜底仅服务于没有 chapterList 规则的书源，绝不替换规则结果"
        # (BookSourceEngine.swift:2933). A chapterList rule that matches nothing leaves the page empty.
        if heuristic_toc:
            items = fallback_chapter_items_from_content(html, final_url)
            if not items:
                items = fetch_dynamic_catalog_items_from_html(session, html, final_url, runtime2)
        if pages == 0:
            first_page_item_count = len(items)
        pages += 1
        if not items and current_url in guess_origin:
            origin = guess_origin.pop(current_url)
            visited.discard(origin[1])
            queue.insert(0, origin[0])
            continue
        if _dbg.active():
            _dbg.emit("page", index=pages, url=final_url, count=len(items))
            _dbg.emit("list", field="chapterList", rule=chapter_list_rule or "(无规则，自动识别)", count=len(items),
                      value=items[0][:2000] if items else "", page=pages)

        current_toc_keys = {
            normalized_book_candidate_url_for_comparison(abs_url),
            normalized_book_candidate_url_for_comparison(final_url),
        }

        # App getChapterList, `if !hasNextTocRule`: range-option candidates (discoverTocRangeOptionCandidates), then - from 15 items on -
        # the pagination-list candidates (discoverTocPaginationCandidates); only when both found nothing, on the first page, the
        # "all chapters" link (guessAllChaptersUrlFromHTML; a chapterList rule does not disable any of these). A guessed page that
        # fails / yields nothing re-queues this page (the App's backup restore).
        heuristic_page_candidates: list[str] = []
        range_page_candidates: list[str] = []
        if not has_next_toc_rule:
            def _fresh_candidate(candidate_url: str) -> bool:
                candidate_key = normalized_book_candidate_url_for_comparison(candidate_url)
                return (
                    candidate_key not in current_toc_keys
                    and f"GET {candidate_url} " not in visited
                    and candidate_url not in queue
                )

            range_page_candidates = [url for url in guess_toc_range_option_urls_from_html(html, final_url) if _fresh_candidate(url)]
            if len(items) >= 15:
                heuristic_page_candidates = [
                    url
                    for url in discover_toc_pagination_candidates(html, final_url, content_type, runtime2)
                    if _fresh_candidate(url) and url not in range_page_candidates
                ]
        if pages == 1 and not has_next_toc_rule:
            if not range_page_candidates and not heuristic_page_candidates:
                all_chapters_url = guess_all_chapters_url_from_html(html, final_url)
                all_chapters_key = normalized_book_candidate_url_for_comparison(all_chapters_url)
                if all_chapters_url and all_chapters_key not in current_toc_keys and f"GET {all_chapters_url} " not in visited and all_chapters_url not in queue:
                    queue.insert(0, all_chapters_url)
                    guessed_toc_urls.add(all_chapters_url)
                    all_chapters_jump_urls.add(all_chapters_url)
                    guess_origin[all_chapters_url] = (current_url, visit_key)
                    if _dbg.active():
                        _dbg.emit("fallback", what="guess_all_chapters_url_from_html（对应 App guessAllChaptersUrlFromHTML 的「全部章节」猜测；失败 / 无新章节时回退首页）", value=all_chapters_url)
                    continue
            if len(items) < 15:
                for range_url in range_page_candidates:
                    queue.append(range_url)
                    guessed_toc_urls.add(range_url)
                    guess_origin[range_url] = (current_url, visit_key)
                    if _dbg.active():
                        _dbg.emit("fallback", what="guess_toc_range_option_urls_from_html（对应 App discoverTocRangeOptionCandidates 的分页下拉猜测）", value=range_url)
                if queue:
                    continue

        tail_urls: set[str] = set()
        for item_index, item in enumerate(items):
            # R156 (network T): firstPageChapterLimit - stop evaluating items once the App's 16-chapter window is full
            # (`getChapterList` firstPageChapterLimit); every item costs several rule / JS evaluations.
            if window_only and window_chapter_count >= APP_VALIDATION_TOC_CHAPTER_LIMIT:
                # R158 (1.1.2 `content_sample_dedups_whole_toc_page`; ciyuanji / xxsy): BookChapterList.kt:114-125 dedups the WHOLE list
                # (the last duplicate wins), so a pinned "latest chapter" row that repeats after the window is never the first chapter.
                # Evaluate only the chapterUrl rule for the rest of the page (no JS rule, no network, at most TOC_TAIL_URL_ROW_CAP rows / TOC_TAIL_SECONDS).
                if dedup_window and current_profile.content_sample_dedups_whole_toc_page:
                    tail_urls = _toc_tail_chapter_urls(
                        items[item_index:], item_index, toc_rule, runtime2, content_type, final_url,
                        reversed_list=toc_list_reversed, heuristic_toc=heuristic_toc, name_rule_blank=name_rule_blank,
                        all_items=items, audio_source=int(src.get("bookSourceType") or 0) == 1,
                    )
                break
            item_type = "json" if is_json_content(item) else content_type
            if item_type == "json" and is_redundant_json_digest_toc_item(item, items):
                continue
            if item_type == "json":
                try:
                    decoded_toc_item = json.loads(extract_json(item) or item)
                except Exception:
                    decoded_toc_item = {}
                toc_item_object = decoded_toc_item if isinstance(decoded_toc_item, dict) else {}
            else:
                toc_item_object = {}
            item_runtime = RuleRuntime(**{
                **runtime2.__dict__,
                "chapter_url": "",
                "chapter_title": "",
                "chapter_tag": "",
                "chapter_is_volume": False,
                "chapter_is_vip": False,
                "chapter_is_pay": False,
                "chapter_index": len(seen_chapter_urls),
                "chapter_count": len(items),
                "chapter_variable": "",
                "chapter_active": True,
                "variables": dict(runtime2.variables or {}),
            })
            # Extract title - use rule or fallback
            expanded_chapter_name_rule = expand_regex_group_template(chapter_name_rule, chapter_list_rule, item)
            if chapter_name_rule:
                if expanded_chapter_name_rule != chapter_name_rule and "$" not in expanded_chapter_name_rule:
                    title_values = [expanded_chapter_name_rule]
                else:
                    with _evaluating_non_url_string_field():  # App evaluateGetString (listMode false)
                        title_values = evaluate_rule(chapter_name_rule, item, item_runtime, item_type)
                title = legado_get_string_value(title_values).strip() if title_values else ""
            else:
                title = ""
            if not title and name_rule_blank:
                title = fallback_name_from_item(item)
            if not title and item_type == "json" and name_rule_blank:
                title = fallback_json_chapter_title_from_item(item)
            if (not title or title == chapter_name_rule) and re.fullmatch(r"[A-Za-z_][\w:-]*@text", chapter_name_rule):
                attr_or_tag = chapter_name_rule.split("@", 1)[0]
                title = child_tag_text_from_item(item, attr_or_tag) or root_attr_from_item(item, attr_or_tag)
            item_runtime.chapter_title = title
            if _dbg.active() and pages == 1 and item_index == 0:
                _dbg.emit("field", field="chapterName", rule=chapter_name_rule, value=title)
            item_runtime.variables = dict(item_runtime.variables or {})
            item_runtime.variables["__chapter.title"] = title
            # Extract chapter URL - use rule or fallback
            current_element_url_rule = _is_current_element_url_rule(chapter_url_rule)
            if chapter_url_rule:
                expanded_chapter_url_rule = expand_regex_group_template(chapter_url_rule, chapter_list_rule, item)
                if expanded_chapter_url_rule != chapter_url_rule and "$" not in expanded_chapter_url_rule:
                    raw_urls = [expanded_chapter_url_rule]
                else:
                    with _evaluating_non_url_string_field():  # Legado: getString(urlRule), not isUrl
                        raw_urls = evaluate_rule(chapter_url_rule, item, item_runtime, item_type)
                if len(raw_urls) > 1 and raw_urls[0].strip().lower().startswith(("http://", "https://")) and ",{" in "\n".join(raw_urls):
                    raw_chapter_url = "\n".join(raw_urls)
                else:
                    raw_chapter_url = raw_urls[0] if raw_urls and raw_urls[0].strip() else ""
                # R155 (B): a JS-produced chapter URL may still carry `@get:{key}` (BookSourceEngine.swift:3102 expands it).
                raw_chapter_url = substitute_variable_references(raw_chapter_url, item_runtime)
                if contains_json_placeholder(raw_chapter_url):
                    raw_chapter_url = substitute_json_placeholders(raw_chapter_url, item)
                if "{{" in raw_chapter_url and "@css:" in raw_chapter_url:
                    raw_chapter_url = substitute_css_placeholders(raw_chapter_url, item, item_runtime, item_type)
                if "{{" in raw_chapter_url and "}}" in raw_chapter_url:
                    raw_chapter_url = _resolve_js_template_expressions(raw_chapter_url, item_runtime, input_text=item)
                # R157 (tocContent J, 1.1.2): a JS rule that returned "" is an empty URL (Legado -> title + index for a volume, baseUrl
                # otherwise, BookChapterList.kt:230-241), never its own source text (the template fallback injected the script).
                # Swift `ruleContainsJavaScriptSegment`: `@js:` or `<js>` + `</js>`; with a JS segment the whole literal-template branch is skipped.
                chapter_url_rule_low = chapter_url_rule.lower()
                js_chapter_url_rule = "@js:" in chapter_url_rule_low or ("<js>" in chapter_url_rule_low and "</js>" in chapter_url_rule_low)
                if contains_json_placeholder(chapter_url_rule) and not (
                    js_chapter_url_rule and app_profiles.current_app_profile().js_chapter_url_empty_no_literal_fallback
                ) and (
                    not raw_chapter_url.strip() or contains_json_placeholder(raw_chapter_url)
                ):
                    literal_chapter_url = substitute_json_placeholders(chapter_url_rule, item)
                    literal_chapter_url = substitute_css_placeholders(literal_chapter_url, item, item_runtime, item_type)
                    literal_chapter_url = substitute_variable_references(literal_chapter_url, item_runtime)
                    literal_chapter_url = _resolve_js_template_expressions(literal_chapter_url, item_runtime, input_text=item)
                    if literal_chapter_url.strip() and not contains_json_placeholder(literal_chapter_url):
                        raw_chapter_url = literal_chapter_url
                chapter_url = resolve_url_preserving_options(raw_chapter_url, final_url) if raw_chapter_url.strip() else ""
            else:
                chapter_url = ""
            # A bare HTML attribute rule (href/src) cannot address a JSON item
            # (e.g. validator-synthesised app-cover-data chapters); the rule
            # engine no longer guesses JSON aliases, so recover the URL here.
            if url_rule_blank and not chapter_url and (not current_element_url_rule or is_json_content(item)):
                chapter_url = fallback_chapter_url_from_item(item, final_url) or fallback_url_from_item(item, final_url)
            if heuristic_toc and chapter_url and not current_element_url_rule and is_same_book_or_toc_url(chapter_url, book_url, final_url):
                recovered_url = recover_encoded_chapter_url_from_item(item, final_url)
                chapter_url = recovered_url or fallback_chapter_url_from_item(item, final_url) or chapter_url
            if heuristic_toc and chapter_url:
                chapter_url = normalize_chapter_url_to_book_host(chapter_url, book_url)
            item_runtime.chapter_url = chapter_url
            if _dbg.active() and pages == 1 and item_index == 0:
                _dbg.emit("field", field="chapterUrl", rule=chapter_url_rule, value=chapter_url)
            item_runtime.variables = dict(item_runtime.variables or {})
            item_runtime.variables["__chapter.url"] = chapter_url

            update_time_rule = str(toc_rule.get("updateTime") or "").strip()
            chapter_tag = ""
            if update_time_rule:
                with _evaluating_non_url_string_field():
                    tag_values = evaluate_rule(update_time_rule, item, item_runtime, item_type)
                chapter_tag = legado_get_string_value(tag_values).strip() if tag_values else ""
            if not chapter_tag and toc_item_object and (heuristic_toc or not update_time_rule):
                chapter_tag = json_first_text(
                    toc_item_object,
                    [
                        "updateTime", "update_time", "updatedAt", "updated_at", "time",
                        "tag", "info", "chapterTime", "chapter_time",
                    ],
                )
            item_runtime.chapter_tag = chapter_tag
            item_runtime.variables["__chapter.tag"] = chapter_tag

            is_volume_rule = str(toc_rule.get("isVolume") or "").strip()
            with _evaluating_non_url_string_field():
                volume_values = evaluate_rule(is_volume_rule, item, item_runtime, item_type) if is_volume_rule else []
            is_volume_text = legado_get_string_value(volume_values)
            if not is_volume_text and toc_item_object and (heuristic_toc or not is_volume_rule):
                # R155b (1.1.2): "volume" is a volume number in many APIs, not a boolean (BookSourceEngine.swift isVolume fallback).
                is_volume_keys = ["isVolume", "is_volume", "isPart"] if app_profiles.current_app_profile().toc_volume_key_excluded else ["isVolume", "is_volume", "volume", "isPart"]
                is_volume_text = json_first_text(toc_item_object, is_volume_keys)
            is_volume = legado_is_true(is_volume_text) or (heuristic_toc and is_likely_volume_title(title))
            item_runtime.chapter_is_volume = is_volume
            item_runtime.variables["__chapter.isVolume"] = is_volume

            if not chapter_url:
                chapter_url = f"{title}{item_index}" if is_volume else final_url
                item_runtime.chapter_url = chapter_url
                item_runtime.variables["__chapter.url"] = chapter_url
            if not title:
                continue
            window_chapter_count += 1

            is_vip_rule = str(toc_rule.get("isVip") or "").strip()
            with _evaluating_non_url_string_field():
                vip_values = evaluate_rule(is_vip_rule, item, item_runtime, item_type) if is_vip_rule else []
            is_vip_text = legado_get_string_value(vip_values)
            if not is_vip_text and toc_item_object and (heuristic_toc or not is_vip_rule):
                is_vip_text = json_first_text(toc_item_object, ["isVip", "is_vip", "vip"])
            is_vip = legado_is_true(is_vip_text)
            item_runtime.chapter_is_vip = is_vip
            item_runtime.variables["__chapter.isVip"] = is_vip

            is_pay_rule = str(toc_rule.get("isPay") or "").strip()
            with _evaluating_non_url_string_field():
                pay_values = evaluate_rule(is_pay_rule, item, item_runtime, item_type) if is_pay_rule else []
            is_pay_text = legado_get_string_value(pay_values)
            if not is_pay_text and toc_item_object and (heuristic_toc or not is_pay_rule):
                is_pay_text = json_first_text(toc_item_object, ["isPay", "is_pay", "pay", "paid"])
            is_pay = legado_is_true(is_pay_text)
            item_runtime.chapter_is_pay = is_pay
            item_runtime.variables["__chapter.isPay"] = is_pay

            # Volume rows are valid Legado chapters, but the validator selects
            # the first fetchable text chapter for its content-stage probe.
            # App batch validation (BookSourceValidator `fetchFirstTocPage`): only volume rows whose URL is the synthetic `title + index`
            # are dropped from the sample; a volume with a real URL stays a chapter. The full walk keeps skipping every volume row.
            if is_volume and (not window_only or chapter_url.startswith(title)):
                continue
            if heuristic_toc and is_likely_toc_noise_title(title, chapter_url, final_url):
                continue
            low_raw_chapter_url = raw_chapter_url.strip().lower() if chapter_url_rule else ""
            if chapter_url and low_raw_chapter_url.startswith(("#", "javascript:", "mailto:")):
                chapter_url = final_url
            if chapter_url:
                if dedup_window:
                    window_rows.append((chapter_url, title, item_runtime, item_index, len(items), dict(runtime2.variables or {})))
                if chapter_url not in seen_chapter_urls:
                    seen_chapter_urls.add(chapter_url)
                    if _dbg.active() and len(seen_chapter_urls) <= 200:
                        _dbg.emit("item", index=len(seen_chapter_urls) - 1, name=title, value=chapter_url, volume=False)
                    if not first_chapter_url and not dedup_window:
                        first_chapter_final_url = final_url
                        first_chapter_url = chapter_url
                        first_chapter_title = title
                        _adopt_first_chapter_runtime(runtime, runtime2.variables or {}, item_runtime, chapter_url, title, item_index, len(items))

        if dedup_window and window_rows and not first_chapter_url:
            # R157 REV / tocContent C xxsy: Legado BookChapterList.kt:119-128 (App keepLastChapterPerURL): of the window's chapters only the
            # LAST one per url stays (a reversed toc keeps the first), then the first remaining chapter is the content sample. A
            # baseUrl placeholder row (empty link rule) that repeats later in the window therefore never is the sample.
            kept_rows = window_rows
            if toc_list_reversed:
                seen_urls: set[str] = set()
                kept_rows = [row for row in window_rows if not (row[0] in seen_urls or seen_urls.add(row[0]))]
            else:
                def _keep_last_per_url(later_urls: set[str]) -> list[tuple[str, str, RuleRuntime, int, int, dict[str, str]]]:
                    survivors: list[tuple[str, str, RuleRuntime, int, int, dict[str, str]]] = []
                    for row in reversed(window_rows):
                        if row[0] not in later_urls:
                            later_urls.add(row[0])
                            survivors.append(row)
                    return list(reversed(survivors))

                kept_rows = _keep_last_per_url(set(tail_urls))
                # R158c (dbxsd / dbxsn, App `validationChapterSample`): a toc page that lists its chapters more than once puts every window
                # row into `tail_urls`, which would empty the sample ("no usable chapter"). Legado keeps the last occurrence of a url (the
                # repeated list), whose page is the very one the window row points at, so an EMPTY sample falls back to the window-only
                # dedup (the pre-R158 behaviour). `tail_urls` is empty unless 1.1.2 `content_sample_dedups_whole_toc_page` collected it.
                if not kept_rows and tail_urls:
                    kept_rows = _keep_last_per_url(set())
            # Nothing left to sample (App `guard let firstChapter = chapters.first else` fails the stage) is the ordinary "no usable
            # chapter" outcome below.
            if kept_rows:
                pick_url, pick_title, pick_runtime, pick_index, pick_count, pick_base = kept_rows[0]
                first_chapter_final_url = final_url
                first_chapter_url = pick_url
                first_chapter_title = pick_title
                _adopt_first_chapter_runtime(runtime, pick_base, pick_runtime, pick_url, pick_title, pick_index, pick_count)
        window_rows.clear()

        # App `newChaptersThisPage == 0` branch on a jump page: the guessed page produced no chapter at all (items without a usable
        # chapter, all duplicates) -> restore the first page like a failed / empty jump page.
        if current_url in guess_origin and not seen_chapter_urls:
            origin = guess_origin.pop(current_url)
            visited.discard(origin[1])
            queue.insert(0, origin[0])
            continue
        # R156 (network T): firstPageOnly - the first page that produced a chapter is the whole validation sample
        # (`getChapterList` `firstPageOnly && !chapters.isEmpty`; volume rows count), the nextTocUrl rule is not evaluated and no further page is requested.
        if window_only and window_chapter_count > 0:
            break
        next_rule = str(toc_rule.get("nextTocUrl") or "").strip()
        if next_rule and current_url not in next_toc_batch_urls:
            next_values = legado_string_list_values(evaluate_rule(next_rule, html, runtime2, content_type))
            if _dbg.active():
                _dbg.emit("field", field="nextTocUrl", rule=next_rule, value="\n".join(next_values))
            resolved_next_urls: list[str] = []
            for raw in next_values:
                if not raw or not raw.strip():
                    continue
                next_url, next_options = resolve_url_with_options(raw, final_url)
                # Legado `if (item != redirectUrl)`: a next page equal to the page just fetched is dropped.
                if not next_url or next_url == final_url:
                    continue
                # R150: a nextTocUrl page keeps its `url,{options}` (the App resolves each queued page with resolveRequest,
                # BookSourceEngine.swift:2743-2752); the loop above splits them again when the page is requested.
                next_url = compose_url_with_options(next_url, next_options)
                resolved_next_urls.append(next_url)
            if pages == 1 and len(resolved_next_urls) > 1:
                batch_urls: list[str] = []
                for next_url in resolved_next_urls:
                    if next_url in visited or next_url in queue or next_url in batch_urls:
                        continue
                    batch_urls.append(next_url)
                next_toc_batch_urls.update(batch_urls)
                queue.extend(batch_urls)
            elif resolved_next_urls:
                first_next_url = resolved_next_urls[0]
                if first_next_url not in visited and first_next_url not in queue:
                    queue.append(first_next_url)

        if not next_rule and len(items) >= 15:
            for range_url in [*range_page_candidates, *heuristic_page_candidates]:
                if f"GET {range_url} " not in visited and range_url not in queue:
                    queue.append(range_url)
                    guessed_toc_urls.add(range_url)
        if (
            not first_chapter_url
            and not queue
            and normalized_book_candidate_url_for_comparison(abs_url) != normalized_book_candidate_url_for_comparison(book_url)
        ):
            # Retry bookUrl when an explicit tocUrl resolves to a noisy non-catalog page.
            book_abs = urljoin(runtime_seed.base_url, book_url)
            book_key = f"GET {book_abs} "
            if book_abs and book_key not in visited:
                queue.insert(0, book_abs)

    return bool(first_chapter_url), first_chapter_final_url, first_chapter_url, first_chapter_title, pages, first_page_item_count, len(seen_chapter_urls)


def _adopt_first_chapter_runtime(
    runtime: RuleRuntime,
    toc_variables: dict[str, str],
    item_runtime: RuleRuntime,
    chapter_url: str,
    title: str,
    item_index: int,
    item_count: int,
) -> None:
    """The first sampled chapter becomes the runtime the content stage copies (chapter binding + chapter-stage variables)."""
    runtime.chapter_url = chapter_url
    runtime.chapter_title = title
    runtime.chapter_tag = item_runtime.chapter_tag
    runtime.chapter_is_volume = item_runtime.chapter_is_volume
    runtime.chapter_is_vip = item_runtime.chapter_is_vip
    runtime.chapter_is_pay = item_runtime.chapter_is_pay
    runtime.chapter_index = item_index
    runtime.chapter_count = item_count
    runtime.chapter_variable = item_runtime.chapter_variable
    runtime.chapter_active = True
    if app_profiles.current_app_profile().chapter_put_reaches_content:
        # R157 (tocContent B image.baidu, js G3 goway): Legado AnalyzeRule.put (:740-749) writes `@put` / java.put into the
        # chapter (BookChapter.putVariable) while a TOC item is evaluated and BookContent reads it back with `@get` /
        # java.get. The variables this item wrote (delta vs the toc-page runtime) go to the content-stage runtime; the
        # synthetic `__chapter.*` binding keys are set above and stay out of it. App 1.1.2: RuleEvaluator `.putVariable`
        # also calls setChapterVariable while a chapter is bound.
        baseline = toc_variables
        delta = {
            key: value
            for key, value in (item_runtime.variables or {}).items()
            if not str(key).startswith("__chapter.") and baseline.get(key) != value
        }
        if delta:
            runtime.variables = {**(runtime.variables or {}), **delta}


def resolve_chapter_content(session: requests.Session, src: dict[str, Any], runtime: RuleRuntime, chapter_url: str, chapter_title: str, book_name: str) -> tuple[bool, str, int]:
    begin_network_stage()
    # R155 (B): the content request URL expands any leftover `@get:{key}` (BookSourceEngine.swift:5397 / :8149).
    chapter_url = substitute_variable_references(chapter_url or "", runtime)
    if _dbg.active():
        _dbg.set_stage("content")
    content_rule = src.get("ruleContent")
    content_expr_raw = str(content_rule.get("content") or "") if isinstance(content_rule, dict) else ""
    if not isinstance(content_rule, dict) or content_expr_raw == "":
        # App getContent (BookSourceEngine.swift:9420-9436): without a ruleContent / content rule the chapter URL
        # itself is returned as the "text". R144a (F7, user decision): for a text source that is a failure - the
        # reader would show a bare URL. Sources whose chapter URL really is the content (audio = 1, image = 2)
        # still pass.
        try:
            content_source_type = int(src.get("bookSourceType") or 0)
        except Exception:
            content_source_type = 0
        if content_source_type in (1, 2):
            return True, chapter_url, 0
        runtime.variables = dict(runtime.variables or {})
        runtime.variables["__contentRuleEmpty"] = "1"
        if _dbg.active():
            _dbg.emit("error", value="ruleContent.content 为空：App 会把章节链接本身当作正文显示")
        return False, chapter_url, 0
    content_expr = content_expr_raw.strip()
    # Only a whitespace-only content rule reaches the App's fallbacks (`contentRuleIsBlank`, E:9726-9751); a rule
    # that returns nothing is an empty chapter (F1).
    content_rule_blank = not content_expr

    current_url = chapter_url
    visited: set[str] = set()
    # Legado follows nextContentUrl recursively only when the rule returns one
    # URL. Multiple URLs are fetched as a one-shot page batch.
    pending_urls: list[tuple[str, bool]] = [(chapter_url, True)]
    page_count = 0
    chunks: list[str] = []
    last_html = ""
    runtime2 = RuleRuntime(**{
        **runtime.__dict__,
        "book_name": book_name,
        "chapter_url": chapter_url,
        "chapter_title": chapter_title,
        "chapter_active": True,
        "variables": dict(runtime.variables or {}),
        # Legado BookContent: setRuleData(book) + chapter.
        "book_binding": "bound",
    })
    resolved_chapter_title = chapter_title
    did_resolve_content_title = False
    used_source_regex_resource = False
    seed_runtime_variables_from_url(runtime2, chapter_url)
    first_abs_url, first_options = resolve_url_with_options(chapter_url, runtime2.base_url, runtime2.headers or {})
    if not first_abs_url:
        first_abs_url = urljoin(runtime2.base_url, chapter_url)

    while pending_urls and page_count < 12:
        current_url, should_follow_next = pending_urls.pop(0)
        abs_url, request_options = resolve_url_with_options(current_url, runtime2.base_url, runtime2.headers or {})
        if not abs_url:
            continue
        request_url = request_url_preserving_typed_data_options(abs_url, request_options)
        request_method = str(request_options.get("method") or "GET")
        request_body = request_options.get("body") if isinstance(request_options.get("body"), str) else None
        request_charset = request_options.get("charset") if isinstance(request_options.get("charset"), str) else None
        request_headers = dict(runtime2.headers or {})
        if isinstance(request_options.get("headers"), dict):
            request_headers.update({str(k): str(v) for k, v in request_options["headers"].items() if v is not None})
        visit_key = f"{request_method.upper()} {request_url} {request_body or ''}"
        if visit_key in visited:
            continue
        visited.add(visit_key)

        response_meta: dict[str, Any] = {}
        html, final_url, status = fetch_text(
            session,
            request_url,
            method=request_method,
            headers=request_headers,
            body=request_body,
            charset=request_charset,
            response_meta=response_meta,
        )
        if content_rule_blank and request_method != "POST" and src.get("enabledCookieJar") is not False and not html.strip():
            retry_meta: dict[str, Any] = {}
            retry_html, retry_final_url, retry_status = fetch_text(
                requests.Session(),
                request_url,
                method=request_method,
                headers=request_headers,
                body=request_body,
                charset=request_charset,
                response_meta=retry_meta,
            )
            if retry_html.strip() or retry_status:
                html = retry_html
                final_url = retry_final_url
                status = retry_status
                response_meta = retry_meta
        if app_http_failure(request_url, status, html, "content", src):
            # App E:9601-9638: a failed request throws when nothing has been collected yet, otherwise the parsed
            # pages are kept.
            if _dbg.active():
                _dbg.emit("warn", code="http_error", url=request_url, status=status)
            if chunks:
                break
            return False, "", page_count
        if request_method.upper() == "GET" and not has_request_header(request_headers, "Referer"):
            # App E:9583-9600 (equivalent, kept): a hot-link-protection error page is retried once with the parent
            # directory as Referer; only a `ref=blocked` URL or a title / non-2xx error page triggers it.
            blocked_by_referer = "ref=blocked" in final_url.lower() or is_heuristic_http_error_page(html, status)
            if blocked_by_referer:
                referer = parent_directory_referer(abs_url)
                if referer:
                    retry_headers = dict(request_headers)
                    retry_headers["Referer"] = referer
                    referer_meta: dict[str, Any] = {}
                    retry_html, retry_final_url, retry_status = fetch_text(
                        session,
                        abs_url,
                        method=request_method,
                        headers=retry_headers,
                        body=request_body,
                        charset=request_charset,
                        response_meta=referer_meta,
                    )
                    if (
                        retry_html
                        and not app_http_error_blocks_rules(retry_status, retry_html, "content", bool(str(src.get("loginCheckJs") or "").strip()))
                        and not is_heuristic_http_error_page(retry_html, retry_status)
                    ):
                        html, final_url, status = retry_html, retry_final_url, retry_status
                        response_meta = referer_meta
        login_ok, html, final_url = apply_login_check_js(
            src, runtime2, html, final_url, request_url, status,
            request_method=request_method,
            request_body=str(request_body or ""),
            request_headers=request_headers,
            response_headers=response_meta.get("headers", {}),
            response_cookies=response_meta.get("cookies", {}),
        )
        if not login_ok:
            if _dbg.active():
                _dbg.emit("warn", code="login_check", url=final_url)
            return False, final_url, page_count
        runtime3 = RuleRuntime(**{
            **runtime2.__dict__,
            "source_url": final_url,
            # BookContent receives baseUrl/chapter.url from the requested
            # absolute chapter URL while redirectUrl remains separate.
            "base_url": request_url,
            "chapter_url": request_url,
            "chapter_title": resolved_chapter_title,
            "variables": dict(runtime2.variables or {}),
        })
        seed_runtime_variables_from_url(runtime3, final_url)
        page_html = html
        source_regex_body = sniff_source_regex_response_body(
            page_html,
            final_url,
            str(content_rule.get("sourceRegex") or ""),
        )
        if source_regex_body:
            page_html = source_regex_body
            used_source_regex_resource = True
        if not did_resolve_content_title:
            did_resolve_content_title = True
            title_expr = str(content_rule.get("title") or "").strip()
            if title_expr:
                title_content_type = "json" if is_json_content(page_html) else "html"
                title_values = evaluate_rule(title_expr, page_html, runtime3, title_content_type)
                parsed_title = legado_get_string_value(title_values).strip()
                if parsed_title:
                    resolved_chapter_title = parsed_title
                    runtime2.chapter_title = parsed_title
                    runtime3.chapter_title = parsed_title
                if _dbg.active():
                    _dbg.emit("field", field="title", rule=title_expr, value=parsed_title)
        if str(content_rule.get("webJs") or "").strip():
            transformed = evaluate_rule(str(content_rule.get("webJs") or ""), page_html, runtime3, "json" if is_json_content(page_html) else "html")
            if transformed:
                page_html = transformed[0]
            if _dbg.active():
                _dbg.emit("field", field="webJs", rule=str(content_rule.get("webJs") or ""),
                          value=transformed[0][:2000] if transformed else "")
        content_type = "json" if is_json_content(page_html) else "html"
        if content_type == "json" and is_likely_content_error_json(page_html):
            if _dbg.active():
                _dbg.emit("warn", code="content_error_json", url=final_url)
            page_count += 1
            break
        if content_type == "html" and contains_browser_deferred_content_signals(page_html):
            if _dbg.active():
                _dbg.emit("warn", code="webview_deferred", url=final_url)
            page_count += 1
            if not chunks:
                return False, "webView deferred content", page_count
            break
        page_content = evaluate_content_rule_with_inline_replace_guard(content_expr, page_html, runtime3, content_type)
        # R144a (F1 / F10): "非空规则结果原样使用；以下兜底仅用于缺少 content 规则的书源"
        # (BookSourceEngine.swift:9723-9751). A rule that is present but yields nothing is an empty chapter - no
        # JSON key guessing, no container fallback, no whole-page text.
        if content_rule_blank:
            json_text = extract_json(page_html)
            if not page_content and json_text:
                try:
                    obj = json.loads(json_text)
                    if isinstance(obj, dict):
                        page_content = json_first_text(obj, [
                            "content", "chapterContent", "chapter_content", "body", "text", "html", "article", "articleContent",
                            "data.content", "data.chapterContent", "data.chapter_content", "data.body", "data.text", "data.html",
                            "result.content", "result.chapterContent", "result.chapter_content", "result.body", "result.text", "result.html",
                            "chapter.content", "chapter.text", "chapter.body", "book.content", "novel.content",
                        ])
                    else:
                        page_content = json_scalar_text(obj)
                except Exception:
                    pass
            if not page_content and content_type == "html" and is_meaningful_content(page_html):
                head = page_html[:500].lower()
                if not any(marker in head for marker in ("<html", "<body", "<script", "<!doctype")):
                    if len(re.findall(r"<[A-Za-z][^>]{0,80}>", page_html[:4000])) < 3:
                        page_content = page_html.strip()
            if content_type == "html":
                page_content = apply_html_content_fallback(page_content, page_html)
            if not page_content and content_type == "html":
                page_content = html_to_visible_text(page_html)
            if content_type == "html":
                if not is_meaningful_content(page_content):
                    recovered_content = recover_deferred_chapter_api_content(session, final_url, request_headers, page_html)
                    if recovered_content:
                        page_content = recovered_content
        if _dbg.active():
            _dbg.emit("field", field="content", rule=content_expr, value=page_content[:2000], count=len(page_content))
        if is_likely_access_gated_content_preview(page_content):
            if _dbg.active():
                _dbg.emit("warn", code="access_gated", url=final_url)
            page_count += 1
            if not chunks:
                return False, page_content, page_count
            break
        if page_content:
            # Swift replacingInlineImageTagsWithMarkers(pageContent, ctx.baseUrl): resolve
            # each page's images against that page's request URL (not the source base).
            page_content = legado_normalize_inline_image_tags(page_content, runtime3.base_url)
            chunks.append(page_content)
        last_html = page_html
        page_count += 1
        if _dbg.active():
            _dbg.emit("page", index=page_count, url=final_url)

        if not should_follow_next:
            continue

        next_rule = str(content_rule.get("nextContentUrl") or "").strip()
        if next_rule:
            next_values = legado_string_list_values(evaluate_rule(next_rule, page_html, runtime3, content_type))
            if _dbg.active():
                _dbg.emit("field", field="nextContentUrl", rule=next_rule, value="\n".join(next_values))
        else:
            next_values = derive_paged_chapter_urls(abs_url, page_html, page_content)
        pending_url_values = {url for url, _ in pending_urls}
        next_candidates: list[str] = []
        for raw in next_values:
            if not raw or not raw.strip():
                continue
            next_url, next_options = resolve_url_with_options(raw, final_url)
            if not next_url:
                continue
            # R150: the page keeps its `url,{options}` (the fetch loop splits them again).
            next_entry = compose_url_with_options(next_url, next_options)
            if next_url in visited or next_entry in pending_url_values or next_entry in next_candidates:
                continue
            if not is_same_chapter_content_page(next_url, abs_url, first_abs_url):
                continue
            next_candidates.append(next_entry)

        if next_rule and not next_candidates:
            for raw in derive_paged_chapter_urls(abs_url, page_html, page_content):
                next_url, _ = resolve_url_with_options(raw, final_url)
                if not next_url:
                    continue
                if next_url in visited or next_url in pending_url_values or next_url in next_candidates:
                    continue
                if not is_same_chapter_content_page(next_url, abs_url, first_abs_url):
                    continue
                next_candidates.append(next_url)

        if not next_candidates:
            if pending_urls:
                continue
            break
        batch_fetch_without_nested_next = bool(next_rule and len(next_candidates) > 1)
        pending_urls.extend((next_url, not batch_fetch_without_nested_next) for next_url in next_candidates)

    merged = "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    did_apply_replace_regex = False
    if merged:
        if str(content_rule.get("replaceRegex") or "").strip():
            did_apply_replace_regex = True
            # Swift applyLegadoContentReplaceRegex: format (formatKeepImg +
            # unescape) -> per-line trim -> getString(replaceRegex); a plain
            # regex without DOTALL, JS/{{}} evaluated like any other rule.
            pre_replace, replaced = apply_legado_content_replace_regex(
                merged, str(content_rule.get("replaceRegex") or ""), runtime2
            )
            replaced = replaced.strip()
            if _dbg.active():
                _dbg.emit("field", field="replaceRegex", rule=str(content_rule.get("replaceRegex") or ""),
                          value=replaced[:2000], count=len(replaced),
                          data={"before": len(pre_replace), "after": len(replaced)})
            # R144a (F8): the App shows the replaced text (BookSourceEngine.swift:9947-9952); a replaceRegex that
            # empties the chapter leaves it empty instead of silently falling back to the pre-replace text.
            merged = replaced
    if content_rule_blank and not merged and last_html:
        merged = html_to_visible_text(last_html)
    merged = merged.strip()
    if did_apply_replace_regex:
        merged = apply_legado_content_replace_indent(merged).strip("\r\n")
    if is_likely_access_gated_content_preview(merged):
        return False, merged, page_count
    if (
        not merged
        and runtime.chapter_is_volume
        and not content_rule_blank
        and app_profiles.current_app_profile().validation_content_sample_skips_volumes
    ):
        # R157 (sfacg, 1.1.2): Legado BookContent.kt:149 throws ContentEmptyException only for a chapter that is NOT a volume, so a
        # volume sample (CheckSourceService.kt:211-215 keeps volumes whose URL is real) with an empty body still passes.
        return True, merged, page_count
    # R156 (tocContent C): Legado BookContent.kt:149 only rejects a blank content and the App's validation accepts any
    # non-empty trimmed text (`BookSourceValidator` content stage), so a non-blank result of a NON-EMPTY content rule is a
    # usable chapter; the stricter `is_usable_chapter_text` heuristics stay for rule-less content. `_run_content_stage`
    # flags such near-empty passes in the details (`contentNearEmpty`).
    legado_nonblank = (not content_rule_blank) and bool(re.sub(r"<(?!img)[^>]+>", "", merged).strip())
    return is_usable_chapter_text(merged) or legado_nonblank or has_meaningful_image_content(merged) or (used_source_regex_resource and bool(merged)), merged, page_count


def _empty_detail(src: dict[str, Any]) -> dict[str, Any]:
    return {
        "bookSourceName": str(src.get("bookSourceName") or ""),
        "bookSourceUrl": normalize_book_source_url(str(src.get("bookSourceUrl") or "")),
        "sourceSiteKey": canonical_source_site_key(src),
        "ruleFingerprint": source_rule_fingerprint(src),
        "enabled": bool(src.get("enabled", True)),
        "ok": False,
        "stages": {"search": False, "detail": False, "toc": False, "content": False},
        "failureReason": "",
        "detailBookName": "",
        "detailBookAuthor": "",
        "sampleBookUrl": "",
        "detailTocUrl": "",
        "tocFirstPageItemCount": 0,
        "tocPagesWalked": 0,
        "tocUniqueChapterCount": 0,
        "firstChapterTitle": "",
        "contentPreviewChars": 0,
        "contentPages": 0,
        "pipelineStage": "",
        "quickScan": {
            "connectivity": False,
            "searchEntry": False,
            "statusCode": 0,
            "sampleBookUrl": "",
        },
        "stabilityPassCount": 0,
        "responseTimeMs": 0,
        "error": "",
        # R143 App profile bookkeeping (appVerdict is filled when the report is assembled).
        "appVersion": app_profiles.current_app_version(),
        "appProfileFeatures": app_profiles.app_profile_sensitivity(src),
        "appVerdict": "",
        "suppressedInteractions": [],
        "failureBeforeInteraction": False,
    }


def _validated_group_value(src: dict[str, Any], version: str, proven_on_111: bool | None = None) -> str:
    """bookSourceGroup written for a passed source: MMDD, plus `需1.1.2` for 1.1.2-only passes (R143 S2)."""
    sensitive = bool(app_profiles.app_profile_sensitivity(src))
    return ",".join(
        app_profiles.pass_group_tokens(version, sensitive, proven_on_111, mmdd=current_validation_group_tag())
    )


def _group_source_url(group_key: str, candidates: list[dict[str, Any]]) -> str:
    """Serialized source URL for a group key (which may be a composite dedupe key)."""
    for candidate in candidates:
        value = normalize_book_source_url(str(candidate.get("bookSourceUrl") or ""))
        if value:
            return value
    return normalize_book_source_url(group_key)


def is_download_only_file_source(src: dict[str, Any]) -> bool:
    info_rule = src.get("ruleBookInfo") if isinstance(src.get("ruleBookInfo"), dict) else {}
    toc_rule = src.get("ruleToc") if isinstance(src.get("ruleToc"), dict) else {}
    content_rule = src.get("ruleContent") if isinstance(src.get("ruleContent"), dict) else {}
    has_download_urls = bool(str((info_rule or {}).get("downloadUrls") or "").strip())
    has_toc_rule = bool(str((toc_rule or {}).get("chapterList") or "").strip())
    has_content_rule = bool(str((content_rule or {}).get("content") or "").strip())
    try:
        source_type = int(src.get("bookSourceType", 0) or 0)
    except Exception:
        source_type = 0
    return (source_type == 3 or has_download_urls) and has_download_urls and not has_toc_rule and not has_content_rule


def source_requires_interactive_verification(src: dict[str, Any]) -> bool:
    """Return true when full validation requires user-driven browser/captcha UI.

    The CLI cannot safely invent a captcha answer or claim a browser challenge
    was completed. Such a source may be valid in Readori, but it must not enter
    the automatically certified output until its complete chain is replayed
    with an explicit verification result.
    """

    # Only inspect fields executed by the automatic search -> content replay.
    # loginUrl/loginUi describe optional, user-invoked account and settings
    # actions. Including them made aggregation sources with browser login
    # buttons fail before their public rule chain was attempted at all.
    executable_fields: list[Any] = [
        src.get("searchUrl"),
        src.get("exploreUrl"),
        src.get("loginCheckJs"),
        src.get("ruleSearch"),
        src.get("ruleExplore"),
        src.get("ruleBookInfo"),
        src.get("ruleToc"),
        src.get("ruleContent"),
    ]

    def strings(value: Any) -> Iterable[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for nested in value.values():
                yield from strings(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from strings(nested)

    script = "\n".join(text for field in executable_fields for text in strings(field)).lower()
    if re.search(r"\b(?:getverificationcode|openurl)\s*\(", script) is not None:
        return True

    # loginCheckJs is executed for every response, so a browser await there is
    # an unconditional validation dependency. Other rule fields frequently
    # contain guarded browser fallbacks for comments, paid chapters, or a
    # specific provider; classifying the whole source from their mere presence
    # prevented ordinary public branches from ever being replayed. Those paths
    # are instead rejected later when the selected result actually resolves to
    # browser-deferred content.
    login_check_script = "\n".join(strings(src.get("loginCheckJs"))).lower()
    return re.search(r"\bstartbrowserawait\s*\(", login_check_script) is not None


def is_webview_or_paid_content_source(src: dict[str, Any], chapter_url: str) -> bool:
    content_rule = src.get("ruleContent") if isinstance(src.get("ruleContent"), dict) else {}
    low_url = (chapter_url or "").lower()
    if '"webview":true' in low_url or '"webview": true' in low_url:
        return True
    if str((content_rule or {}).get("payAction") or "").strip():
        return True
    content_expr = str((content_rule or {}).get("content") or "").lower()
    web_js_expr = str((content_rule or {}).get("webJs") or "").lower()
    if "startbrowser" in content_expr or "startbrowser" in web_js_expr:
        return True
    return False


def is_likely_browser_challenge_preview(text: str) -> bool:
    compact = re.sub(r"\s+", "", (text or "").strip().lower())
    if not compact or len(compact) > 80:
        return False
    if "我的书架" in compact and "联系我们" in compact:
        return True
    markers = [
        "加载中", "努力加载中", "安全验证", "人机验证", "验证中",
        "redirecting", "loading", "justamoment", "verificationrequired", "webviewdeferredcontent",
    ]
    return any(marker in compact for marker in markers)


def is_likely_access_gated_content_preview(text: str) -> bool:
    compact = re.sub(r"\s+", "", (text or "").strip().lower())
    if not compact or len(compact) > 120:
        return False
    markers = [
        "当前app版本过低", "请升级后再进行阅读", "请升级app", "app版本过低",
        "appversiontoolow", "pleaseupgradetheapp",
    ]
    return any(marker in compact for marker in markers)


def _js_code_skeleton(prefix: str) -> tuple[str, bool]:
    """R156 (tocContent Q): `prefix` with comments removed and string / template-literal contents emptied (`"a{b"` -> `""`), so
    braces that are text no longer count. Returns (skeleton, ends_inside_a_string)."""
    out: list[str] = []
    index = 0
    length = len(prefix)
    while index < length:
        char = prefix[index]
        following = prefix[index + 1] if index + 1 < length else ""
        if char == "/" and following == "/":
            end = prefix.find("\n", index)
            index = length if end < 0 else end
            out.append(" ")
            continue
        if char == "/" and following == "*":
            end = prefix.find("*/", index + 2)
            index = length if end < 0 else end + 2
            out.append(" ")
            continue
        if char in "\"'`":
            quote = char
            index += 1
            closed = False
            while index < length:
                current = prefix[index]
                if current == "\\":
                    index += 2
                    continue
                if current == quote:
                    closed = True
                    index += 1
                    break
                index += 1
            if not closed:
                return "".join(out), True
            out.append(quote + quote)
            continue
        out.append(char)
        index += 1
    return "".join(out), False


_JS_BLOCK_GUARD_HEADER = re.compile(r"(?:\bif\s*\([^{}]*\)|\belse(?:\s+if\s*\([^{}]*\))?)\s*$", re.I | re.S)


def _browser_call_is_guarded(prefix: str) -> bool:
    """True when the call that follows `prefix` sits in a block opened by `if (...) {` / `else {` / `else if (...) {`, or is the
    single statement of an `if (...)` (brace stack over the string / comment-free skeleton, R156 tocContent Q)."""
    skeleton, inside_string = _js_code_skeleton(prefix)
    if inside_string:
        return False
    stack: list[bool] = []
    for position, char in enumerate(skeleton):
        if char == "{":
            stack.append(bool(_JS_BLOCK_GUARD_HEADER.search(skeleton[max(0, position - 600):position])))
        elif char == "}" and stack:
            stack.pop()
    if any(stack):
        return True
    return bool(re.search(r"\bif\s*\([^;{}]*\)\s*$", skeleton[-600:], flags=re.I | re.S))


def is_browser_deferred_content_source(src: dict[str, Any]) -> bool:
    content_rule = src.get("ruleContent") if isinstance(src.get("ruleContent"), dict) else {}
    expressions = [
        str((content_rule or {}).get("content") or ""),
        str((content_rule or {}).get("webJs") or ""),
    ]
    for expression in expressions:
        for match in re.finditer(r"\b(?:java\s*\.\s*)?startBrowser(?:Await|Dp)?\s*\(", expression, flags=re.I):
            if _browser_call_is_guarded(expression[:match.start()]):
                continue
            return True
    # A guarded browser call is a challenge/paywall fallback, not proof that
    # the normal content path itself depends on an interactive WebView.
    return False


def _quick_scan_detail(src: dict[str, Any], stage: str = "quick-scan") -> dict[str, Any]:
    detail = _empty_detail(src)
    detail["pipelineStage"] = stage
    return detail


def quick_scan_single_source(src: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, dict[str, Any]]:
    holder: dict[str, Any] = {}
    if _dbg.active():
        _dbg.set_stage("quick-scan")
        _dbg.emit("begin")
    begin_debug_errors()
    passed, seed, detail = _quick_scan_single_source(src, holder)
    attach_debug_errors(detail)
    if _dbg.active():
        _dbg.set_stage("quick-scan")
        _dbg.emit("end", ok=bool(passed), value=str(detail.get("failureReason") or ""),
                  data={"failureReason": str(detail.get("failureReason") or ""), "error": str(detail.get("error") or "")})
    state = holder.get("state")
    apply_interaction_state_to_detail(detail, state)
    if seed is not None and isinstance(state, dict):
        # validate_single_source restores it, so interactions blocked during quick scan survive into full validation.
        seed["interactionState"] = copy.deepcopy(state)
    return passed, seed, detail


def _quick_scan_single_source(
    src: dict[str, Any],
    holder: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None, dict[str, Any]]:
    """Run only connectivity and search/explore entry checks for one source.

    The returned seed is intentionally small and serialisable. It lets the
    full validator reuse the selected book URL and rule variables instead of
    issuing the same search request a second time.
    """
    ensure_source_validation_time_remaining()
    original = src
    detail = _quick_scan_detail(original)
    # R143: rules are evaluated on the App's decoded view of the record (A2); output keeps the original record.
    src, decode_error = app_profiles.app_decode_view(original)
    started = time.perf_counter()
    if not src.get("enabled", True):
        detail["failureReason"] = "disabled"
        return False, None, detail
    if decode_error:
        detail["failureReason"] = app_profiles.APP_IMPORT_REJECTED_REASON
        detail["error"] = decode_error
        return False, None, detail
    if not app_profiles.current_app_profile().interaction_runtime_verdict_only and source_requires_interactive_verification(src):
        detail["failureReason"] = "interactive verification required"
        detail["error"] = "source requires getVerificationCode/startBrowserAwait/openUrl user input"
        detail["staticInteractionKinds"] = app_profiles.static_interaction_kinds(original)
        return False, None, detail
    base_url = effective_base_url(str(src.get("bookSourceUrl") or ""))
    if not has_valid_base_url(base_url) and not source_has_absolute_entry_point(src):
        detail["failureReason"] = "invalid base url"
        return False, None, detail
    search_rule = src.get("ruleSearch") if isinstance(src.get("ruleSearch"), dict) else {}
    explore_rule = src.get("ruleExplore") if isinstance(src.get("ruleExplore"), dict) else {}
    has_search = bool(str(src.get("searchUrl") or "").strip()) and bool(str(search_rule.get("bookList") or "").strip())
    has_explore = bool(str(src.get("exploreUrl") or "").strip()) and has_effective_explore_book_list(src)
    if not has_search and not has_explore:
        detail["failureReason"] = "missing search/explore rule"
        return False, None, detail

    runtime = make_runtime(src)
    holder["state"] = runtime.interaction_state
    # R144a (F11): one fresh Session per source - no cookie of another source (or of an earlier round) leaks in, and
    # `enabledCookieJar: false` stops Set-Cookie from being stored.
    session = make_source_session(src)
    # R149 (B8): no home page probe. The App's batch check (BookSourceValidator.doCheckSource) goes straight to search;
    # the probe used to spend up to the whole 8s quick-scan quota (a slow home page left the real search ~1s) and
    # could fail a source whose search endpoint works. Reachability is now judged by the search / explore request
    # itself (`connectivity` is set when it returns a book).
    try:
        ensure_source_validation_time_remaining()
        # Quick scan does not turn source-script errors into failures (the full chain does), with one exception:
        # a startBrowserAwait the App's bulk validation blocks throws "验证结果为空" out of the search stage, so the
        # search must fail here as well; otherwise heuristic book detection would pass a source the App marks
        # js失效 / needs interaction.
        stage_sink = begin_script_error_stage(runtime)
        book_urls, search_base = _fetch_first_book_candidates(session, src, runtime)
        runtime.script_error_sink = None
        blocked_error = next(
            (
                str(entry.get("message") or "")
                for entry in stage_sink
                if entry.get("kind") == "error" and BLOCKED_BROWSER_AWAIT_MESSAGE in str(entry.get("message") or "")
            ),
            "",
        )
        if blocked_error:
            detail["error"] = blocked_error
            detail["scriptError"] = blocked_error
            detail["failureReason"] = "js error"
            detail["failureGroup"] = "js失效"
            detail["failedStage"] = "search"
            detail["responseTimeMs"] = int((time.perf_counter() - started) * 1000)
            return False, None, detail
        if book_urls and (runtime.variables or {}).get("__exploreFailed") == "1":
            explore_script_error = str((runtime.variables or {}).get("__exploreScriptError") or "")
            if explore_script_error:
                detail["error"] = explore_script_error
                detail["scriptError"] = explore_script_error
                detail["failureReason"] = "js error"
                detail["failureGroup"] = "js失效"
                detail["failedStage"] = "explore"
            else:
                _mark_explore_failed(detail)
            detail["responseTimeMs"] = int((time.perf_counter() - started) * 1000)
            return False, None, detail
        if not book_urls:
            if (runtime.variables or {}).get("__searchChallenge") == "1":
                detail["failureReason"] = "webView/paid content"
                detail["error"] = "search page requires browser challenge"
            else:
                detail["failureReason"] = "no search/explore result"
                attach_network_error(detail, "search")
                if (runtime.variables or {}).get("__exploreOnlyRescue") == "1":
                    detail["error"] = "search returned no usable result (App marks 搜索失效); a working discovery entry does not rescue it"
            detail["responseTimeMs"] = int((time.perf_counter() - started) * 1000)
            return False, None, detail
        first_book_url = str(book_urls[0])
        detail["stages"]["search"] = True
        detail["quickScan"]["searchEntry"] = True
        detail["quickScan"]["connectivity"] = True
        detail["quickScan"]["sampleBookUrl"] = first_book_url
        detail["sampleBookUrl"] = first_book_url
        detail["responseTimeMs"] = int((time.perf_counter() - started) * 1000)
        seed = {
            "source": dict(original),
            "bookUrls": [str(value) for value in book_urls[:20] if str(value).strip()],
            "searchBase": str(search_base or ""),
            "variables": dict(runtime.variables or {}),
            "cookies": requests.utils.dict_from_cookiejar(session.cookies),
            "searchBookAuthor": runtime.validation_book_author,
            # R158 (H): the selected search item's SearchBook fields reach the full chain's Book binding
            "bookState": {key: str(getattr(runtime, key, "") or "") for key in _SELECTED_BOOK_STATE_KEYS},
            # R151 (E1): the discovery candidate (App `.discovery`), so the full chain runs it without re-fetching discovery.
            "exploreCandidate": copy.deepcopy(runtime.explore_candidate) if isinstance(runtime.explore_candidate, dict) else None,
            "quickDetail": detail,
        }
        detail["ok"] = True
        return True, seed, detail
    except SourceValidationDeadlineExceeded:
        raise
    except Exception as exc:
        detail["error"] = str(exc)
        detail["failureReason"] = "exception"
        detail["responseTimeMs"] = int((time.perf_counter() - started) * 1000)
        return False, None, detail


def _run_detail_stage(
    session: requests.Session,
    src: dict[str, Any],
    runtime: RuleRuntime,
    detail: dict[str, Any],
    book_url: str,
    *,
    stage: str = "detail",
    label: str = "",
) -> tuple[bool, str, str, str]:
    """Detail stage of one candidate: deadline check, debug trace, per-stage script-error sink (`stage` names it)."""
    ensure_source_validation_time_remaining()
    begin_extra = {"value": label} if label else {}
    if _dbg.active():
        _dbg.set_stage("info")
        _dbg.emit("begin", url=book_url, **begin_extra)
    stage_sink = begin_script_error_stage(runtime)
    result = fetch_book_info(session, src, runtime, book_url)
    finish_script_error_stage(runtime, stage_sink, detail, stage)
    if _dbg.active():
        _dbg.emit("end", ok=bool(result[0]), value=result[1] or "")
    return result


def _run_toc_stage(
    session: requests.Session,
    src: dict[str, Any],
    runtime: RuleRuntime,
    detail: dict[str, Any],
    book_url: str,
    toc_url: str,
    *,
    stage: str = "toc",
    label: str = "",
) -> tuple[bool, str, str, str, int, int, int]:
    """First-TOC stage of one candidate (same wrapper as `_run_detail_stage`)."""
    ensure_source_validation_time_remaining()
    begin_extra = {"value": label} if label else {}
    if _dbg.active():
        _dbg.set_stage("toc")
        _dbg.emit("begin", url=toc_url or book_url, **begin_extra)
    stage_sink = begin_script_error_stage(runtime)
    result = resolve_toc_source(session, src, runtime, book_url, toc_url or book_url, first_page_only=not FULL_TOC_WALK)
    finish_script_error_stage(runtime, stage_sink, detail, stage)
    if _dbg.active():
        _dbg.emit("end", ok=bool(result[0]), count=result[6], value=result[3] or "")
    return result


def _run_content_stage(
    session: requests.Session,
    src: dict[str, Any],
    runtime: RuleRuntime,
    detail: dict[str, Any],
    chapter_url: str,
    chapter_title: str,
    book_name: str,
    *,
    stage: str = "content",
    label: str = "",
) -> tuple[bool, str, int]:
    """First-chapter content stage of one candidate (same wrapper as `_run_detail_stage`)."""
    ensure_source_validation_time_remaining()
    begin_extra = {"value": label} if label else {}
    if _dbg.active():
        _dbg.set_stage("content")
        _dbg.emit("begin", url=chapter_url, **begin_extra)
    stage_sink = begin_script_error_stage(runtime)
    result = resolve_chapter_content(session, src, runtime, chapter_url, chapter_title, book_name)
    finish_script_error_stage(runtime, stage_sink, detail, stage)
    if result[0] and not is_usable_chapter_text(result[1] or "") and not has_meaningful_image_content(result[1] or ""):
        # R156 (tocContent C): accepted only because the content rule produced non-blank text (Legado / App accept any).
        detail["contentNearEmpty"] = True
        detail["contentNearEmptyChars"] = len(re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", result[1] or "")))
    if _dbg.active():
        _dbg.emit("end", ok=bool(result[0]), count=len(result[1] or ""))
    return result


EXPLORE_CANDIDATE_LABEL = "发现候选"


def _run_explore_candidate_chain(
    session: requests.Session,
    src: dict[str, Any],
    runtime: RuleRuntime,
    candidate: dict[str, Any],
    detail: dict[str, Any],
) -> bool:
    """R151 (E1): the App validates EVERY candidate (search AND discovery) through detail -> first TOC -> first chapter
    (BookSourceValidator.validateCandidate); a failing discovery candidate fails the whole source. Returns True when the
    discovery candidate FAILED (the failure is already written into `detail`).

    * Dedupe (appendCandidate): a discovery book whose lower-cased URL part equals the search book reuses its result.
    * The candidate runs on its own runtime built from the source (not the search book's bound state) with the
      variables / author its list item produced; the session (cookies) and the interaction state are shared.
    * Failures: detail -> `explore detail failed` (App classifyError: script error `js失效`, else `网站失效`);
      empty TOC -> `explore toc failed` / `发现目录失效`; empty content -> `explore content failed` / `发现正文失效`.
      A request-level network error replaces the reason the way the search chain does (`network error: …`).
    """
    book_url = str(candidate.get("bookUrl") or "").strip()
    if not book_url:
        return False
    if candidate.get("reused"):
        detail["exploreCandidate"] = {"bookUrl": book_url, "reused": True}
        return False
    info: dict[str, Any] = {
        "bookUrl": book_url,
        "stages": {"detail": False, "toc": False, "content": False},
        "failureReason": "",
        "detailBookName": "",
        "firstChapterTitle": "",
        "contentPreviewChars": 0,
    }
    detail["exploreCandidate"] = info
    ex_runtime = make_runtime(src)
    ex_runtime.interaction_state = runtime.interaction_state
    variables = candidate.get("variables")
    ex_runtime.variables = (
        _apply_selected_book_state(ex_runtime, {str(key): str(value) for key, value in variables.items()})
        if isinstance(variables, dict) else {}
    )
    ex_runtime.validation_book_author = str(candidate.get("author") or "")
    ensure_source_validation_time_remaining()
    recovered_url = recover_book_detail_url_from_reader_candidate(session, ex_runtime, book_url)
    if recovered_url:
        info["originalBookUrl"] = book_url
        book_url = recovered_url
        info["bookUrl"] = book_url

    def fail(reason: str, group: str, stage: str, error: str = "") -> bool:
        info["failureReason"] = reason
        detail["failureReason"] = reason
        detail["failureGroup"] = group
        if error:
            detail["error"] = error
        if attach_network_error(detail, stage):
            # a transport / HTTP error is a site failure (App classifyError), not the discovery TOC / content group
            detail.pop("failureGroup", None)
            info["failureReason"] = str(detail.get("failureReason") or reason)
        return True

    ok, name, toc_url, detail_error = _run_detail_stage(
        session, src, ex_runtime, detail, book_url, stage="explore detail", label=EXPLORE_CANDIDATE_LABEL
    )
    if not ok:
        return fail("explore detail failed", "网站失效", "explore detail", detail_error or "")
    info["stages"]["detail"] = True
    info["detailBookName"] = name
    normalized_toc_url = (toc_url or "").strip()
    ex_runtime.book_name = name
    if not (ex_runtime.book_author or "").strip():
        # R157 (tocContent E): Legado BookInfo.kt:76-78 keeps the search / explore author unless the detail author is non-empty.
        ex_runtime.book_author = (ex_runtime.validation_book_author or "").strip()
    ex_runtime.book_url = book_url
    toc_ok, _toc_final_url, chapter_url, chapter_title, _pages, _first_page, _unique = _run_toc_stage(
        session, src, ex_runtime, detail, book_url, normalized_toc_url, stage="explore toc", label=EXPLORE_CANDIDATE_LABEL
    )
    if not toc_ok:
        return fail("explore toc failed", "发现目录失效", "explore toc")
    info["stages"]["toc"] = True
    info["firstChapterTitle"] = chapter_title
    content_ok, content_preview, _content_pages = _run_content_stage(
        session, src, ex_runtime, detail, chapter_url, chapter_title, name or str(src.get("bookSourceName") or ""),
        stage="explore content", label=EXPLORE_CANDIDATE_LABEL,
    )
    info["contentPreviewChars"] = min(len(content_preview), 50_000) if content_preview else 0
    if not content_ok:
        return fail("explore content failed", "发现正文失效", "explore content")
    info["stages"]["content"] = True
    return False


def _mark_explore_failed(detail: dict[str, Any]) -> None:
    """R147 (task 2): search passed but the discovery entry returned no book - the App's `发现失效` (a failure group)."""
    detail["failureReason"] = "explore failed"
    detail["failureGroup"] = "发现失效"
    detail["error"] = "search passed but the discovery entry returned no book (App marks 发现失效 and the source fails)"


def quick_scan_group(url: str, candidates: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    last_detail: dict[str, Any] | None = None
    for src in candidates:
        ensure_source_validation_time_remaining()
        passed, seed, detail = quick_scan_single_source(src)
        last_detail = detail
        if passed and seed is not None:
            normalized = normalize_book_source_url(str(src.get("bookSourceUrl") or url))
            detail["bookSourceUrl"] = normalized
            return normalized, seed, detail
    if last_detail is not None:
        last_detail["bookSourceUrl"] = _group_source_url(url, candidates)
        return url, None, last_detail
    fallback_name = str(candidates[0].get("bookSourceName") or "") if candidates else ""
    return url, None, _quick_scan_detail({"bookSourceName": fallback_name, "bookSourceUrl": _group_source_url(url, candidates)})


def validate_single_source(
    src: dict[str, Any],
    quick_seed: dict[str, Any] | None = None,
) -> tuple[ValidationOutcome, dict[str, Any] | None, dict[str, Any]]:
    holder: dict[str, Any] = {}
    if _dbg.active():
        _dbg.set_stage("full-validation")
        _dbg.emit("begin")
    begin_debug_errors()
    outcome, merged, detail = _validate_single_source(src, quick_seed, holder)
    attach_debug_errors(detail)
    if _dbg.active():
        _dbg.set_stage("full-validation")
        _dbg.emit("end", ok=bool(outcome.passed), value=str(detail.get("failureReason") or ""),
                  data={"failureReason": str(detail.get("failureReason") or ""), "error": str(detail.get("error") or "")})
    apply_interaction_state_to_detail(detail, holder.get("state"))
    return outcome, merged, detail


def _validate_single_source(
    src: dict[str, Any],
    quick_seed: dict[str, Any] | None,
    holder: dict[str, Any],
) -> tuple[ValidationOutcome, dict[str, Any] | None, dict[str, Any]]:
    ensure_source_validation_time_remaining()
    original = src
    detail = _empty_detail(original)
    detail["pipelineStage"] = "full-validation"
    # R143: rules are evaluated on the App's decoded view of the record (A2); output keeps the original record.
    src, decode_error = app_profiles.app_decode_view(original)
    if not src.get("enabled", True):
        detail["failureReason"] = "disabled"
        return ValidationOutcome(False, reason="disabled"), None, detail
    if decode_error:
        detail["failureReason"] = app_profiles.APP_IMPORT_REJECTED_REASON
        detail["error"] = decode_error
        return ValidationOutcome(False, reason=app_profiles.APP_IMPORT_REJECTED_REASON), None, detail
    if not app_profiles.current_app_profile().interaction_runtime_verdict_only and source_requires_interactive_verification(src):
        detail["failureReason"] = "interactive verification required"
        detail["error"] = "source requires getVerificationCode/startBrowserAwait/openUrl user input"
        detail["staticInteractionKinds"] = app_profiles.static_interaction_kinds(original)
        return (
            ValidationOutcome(False, reason="interactive verification required"),
            None,
            detail,
        )
    base_url = effective_base_url(str(src.get("bookSourceUrl") or ""))
    if not has_valid_base_url(base_url) and not source_has_absolute_entry_point(src):
        detail["failureReason"] = "invalid base url"
        return ValidationOutcome(False, reason="invalid base url"), None, detail
    has_search = bool(str(src.get("searchUrl") or "").strip()) and isinstance(src.get("ruleSearch"), dict) and bool(str((src.get("ruleSearch") or {}).get("bookList") or "").strip())
    has_explore = bool(str(src.get("exploreUrl") or "").strip()) and has_effective_explore_book_list(src)
    if not has_search and not has_explore:
        detail["failureReason"] = "missing search/explore rule"
        return ValidationOutcome(False, reason="missing search/explore rule"), None, detail

    runtime = make_runtime(src)
    holder["state"] = runtime.interaction_state
    # R144a (F11): one fresh Session per source - no cookie of another source (or of an earlier round) leaks in, and
    # `enabledCookieJar: false` stops Set-Cookie from being stored.
    session = make_source_session(src)
    if quick_seed:
        seeded_interactions = quick_seed.get("interactionState")
        if isinstance(seeded_interactions, dict) and isinstance(runtime.interaction_state, dict):
            runtime.interaction_state.update(copy.deepcopy(seeded_interactions))
        variables = quick_seed.get("variables")
        if isinstance(variables, dict):
            runtime.variables = {str(key): str(value) for key, value in variables.items()}
        cookies = quick_seed.get("cookies")
        if isinstance(cookies, dict):
            try:
                session.cookies.update(requests.utils.cookiejar_from_dict({str(k): str(v) for k, v in cookies.items()}))
            except Exception as _swallowed:
                note_debug_error("seed cookies", _swallowed)
                pass
        runtime.validation_book_author = str(quick_seed.get("searchBookAuthor") or "")
        book_state = quick_seed.get("bookState")
        if isinstance(book_state, dict):
            for state_key, state_value in book_state.items():
                if state_value and state_key in _SELECTED_BOOK_STATE_KEYS:
                    setattr(runtime, state_key, str(state_value))
    start = time.perf_counter()

    try:
        ensure_source_validation_time_remaining()
        seed_urls = quick_seed.get("bookUrls") if isinstance(quick_seed, dict) else None
        if isinstance(seed_urls, list) and seed_urls:
            book_urls = [str(value) for value in seed_urls if str(value).strip()]
            search_base = str(quick_seed.get("searchBase") or "")
            detail["pipelineStage"] = "full-validation"
            quick_detail = quick_seed.get("quickDetail")
            if isinstance(quick_detail, dict):
                quick_scan = quick_detail.get("quickScan")
                if isinstance(quick_scan, dict):
                    detail["quickScan"] = dict(quick_scan)
            detail["stages"]["search"] = True
            seeded_explore = quick_seed.get("exploreCandidate")
            explore_candidate = seeded_explore if isinstance(seeded_explore, dict) else None
            if _dbg.active():
                _dbg.set_stage("search")
                _dbg.emit("info", code="seeded", count=len(book_urls),
                          value=f"沿用快速扫描的搜索结果（{len(book_urls)} 本），不再重复搜索")
        else:
            # R149 (B8): no home page probe (the App has none; it only burned the per-source budget).
            # Legado ScriptException in searchUrl/exploreUrl/bookList/item rules fails the stage.
            if _dbg.active():
                _dbg.set_stage("search")
                _dbg.emit("begin")
            stage_sink = begin_script_error_stage(runtime)
            book_urls, search_base = _fetch_first_book_candidates(session, src, runtime)
            finish_script_error_stage(runtime, stage_sink, detail, "search")
            explore_candidate = runtime.explore_candidate if isinstance(runtime.explore_candidate, dict) else None
            if _dbg.active():
                _dbg.emit("end", ok=bool(book_urls), count=len(book_urls))
        if book_urls and (runtime.variables or {}).get("__exploreFailed") == "1":
            _mark_explore_failed(detail)
            detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
            return ValidationOutcome(False, reason="explore failed", response_time_ms=detail["responseTimeMs"]), None, detail
        if not book_urls:
            if (runtime.variables or {}).get("__searchChallenge") == "1":
                detail["failureReason"] = "webView/paid content"
                detail["error"] = "search page requires browser challenge"
                detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
                return ValidationOutcome(False, reason="webView/paid content", response_time_ms=detail["responseTimeMs"]), None, detail
            detail["failureReason"] = "no search/explore result"
            attach_network_error(detail, "search")
            if (runtime.variables or {}).get("__exploreOnlyRescue") == "1":
                detail["error"] = "search returned no usable result (App marks 搜索失效); a working discovery entry does not rescue it"
            detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
            return ValidationOutcome(False, reason=str(detail["failureReason"]), response_time_ms=detail["responseTimeMs"]), None, detail
        detail["stages"]["search"] = True
        first_book_url = book_urls[0]
        detail["sampleBookUrl"] = first_book_url
        recovered_detail_url = recover_book_detail_url_from_reader_candidate(session, runtime, first_book_url)
        if recovered_detail_url:
            detail["originalSampleBookUrl"] = first_book_url
            first_book_url = recovered_detail_url
            detail["sampleBookUrl"] = first_book_url
        ok, name, toc_url, detail_error = _run_detail_stage(session, src, runtime, detail, first_book_url)
        normalized_toc_url = (toc_url or "").strip()
        if not ok:
            detail["failureReason"] = "detail failed"
            detail["error"] = detail_error or ""
            attach_network_error(detail, "detail")
            detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
            return ValidationOutcome(False, reason=str(detail["failureReason"]), response_time_ms=detail["responseTimeMs"], book_url=first_book_url), None, detail
        detail["stages"]["detail"] = True
        detail["detailBookName"] = name
        detail["detailBookAuthor"] = (runtime.book_author or runtime.validation_book_author or "").strip()
        detail["detailTocUrl"] = normalized_toc_url
        runtime.book_name = name
        if not (runtime.book_author or "").strip():
            # R157 (tocContent E mowan): Legado BookInfo.kt:76-78 keeps the search author unless the detail author is non-empty.
            runtime.book_author = (runtime.validation_book_author or "").strip()
        runtime.book_url = first_book_url
        if is_download_only_file_source(src):
            detail["failureReason"] = "download-only source"
            detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
            return ValidationOutcome(False, reason="download-only source", response_time_ms=detail["responseTimeMs"], book_url=first_book_url, detail_name=name, detail_toc_url=normalized_toc_url), None, detail
        toc_ok, toc_final_url, first_chapter_url, first_chapter_title, toc_pages, toc_first_page, toc_unique_chapters = _run_toc_stage(
            session, src, runtime, detail, first_book_url, normalized_toc_url
        )
        detail["tocFirstPageItemCount"] = toc_first_page
        detail["tocPagesWalked"] = toc_pages
        detail["tocUniqueChapterCount"] = toc_unique_chapters
        if not toc_ok:
            detail["failureReason"] = "toc failed"
            attach_network_error(detail, "toc")
            detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
            return ValidationOutcome(False, reason=str(detail["failureReason"]), response_time_ms=detail["responseTimeMs"], book_url=first_book_url, detail_name=name, detail_toc_url=normalized_toc_url), None, detail
        detail["stages"]["toc"] = True
        detail["firstChapterTitle"] = first_chapter_title
        content_ok, content_preview, content_pages = _run_content_stage(
            session, src, runtime, detail, first_chapter_url, first_chapter_title, name or str(src.get("bookSourceName") or "")
        )
        detail["contentPages"] = content_pages
        detail["contentPreviewChars"] = min(len(content_preview), 50_000) if content_preview else 0
        if content_ok and is_browser_deferred_content_source(src):
            detail["failureReason"] = "webView/paid content"
            detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
            return ValidationOutcome(False, reason="webView/paid content", response_time_ms=detail["responseTimeMs"], book_url=first_book_url, detail_name=name, detail_toc_url=normalized_toc_url), None, detail
        if not content_ok and (runtime.variables or {}).get("__contentRuleEmpty") == "1":
            # R144a (F7): ruleContent.content is empty, so the App would show the chapter URL itself.
            detail["failureReason"] = "content rule empty"
            detail["error"] = "ruleContent.content is empty: the App returns the chapter URL as the text"
            detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
            return ValidationOutcome(False, reason="content rule empty", response_time_ms=detail["responseTimeMs"], book_url=first_book_url, detail_name=name, detail_toc_url=normalized_toc_url), None, detail
        if not content_ok:
            if (
                is_webview_or_paid_content_source(src, first_chapter_url)
                or is_likely_browser_challenge_preview(content_preview)
                or is_likely_access_gated_content_preview(content_preview)
            ):
                detail["failureReason"] = "webView/paid content"
                detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
                return ValidationOutcome(False, reason="webView/paid content", response_time_ms=detail["responseTimeMs"], book_url=first_book_url, detail_name=name, detail_toc_url=normalized_toc_url), None, detail
            detail["failureReason"] = "content failed"
            attach_network_error(detail, "content")
            detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
            return ValidationOutcome(False, reason=str(detail["failureReason"]), response_time_ms=detail["responseTimeMs"], book_url=first_book_url, detail_name=name, detail_toc_url=normalized_toc_url), None, detail
        detail["stages"]["content"] = True
        # R151 (E1): the discovery candidate runs the same chain (App validateCandidate for `.discovery`); `stages` keeps
        # describing the search candidate, the discovery result is in `detail["exploreCandidate"]`.
        if explore_candidate and _run_explore_candidate_chain(session, src, runtime, explore_candidate, detail):
            detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
            return ValidationOutcome(False, reason=str(detail["failureReason"]), response_time_ms=detail["responseTimeMs"], book_url=first_book_url, detail_name=name, detail_toc_url=normalized_toc_url), None, detail
        elapsed = int((time.perf_counter() - start) * 1000)
        detail["ok"] = True
        detail["failureReason"] = ""
        detail["responseTimeMs"] = elapsed
        merged = dict(original)
        merged["customTag"] = app_profiles.VALIDATED_PASS_TAG
        merged["respondTime"] = elapsed
        merged["bookSourceGroup"] = _validated_group_value(original, app_profiles.current_app_version())
        # Internal aggregation metadata; stripped before any output is written.
        merged["__readoriValidation"] = {
            "bookTitle": name,
            "bookAuthor": detail["detailBookAuthor"],
            "sampleBookUrl": first_book_url,
            "sourceSiteKey": canonical_source_site_key(original),
            "ruleFingerprint": source_rule_fingerprint(original),
            # R147 (F13): measured quality for the same-book aggregation (see candidate_quality_key).
            "stagesPassed": sum(1 for stage_name in _QUALITY_STAGES if detail["stages"].get(stage_name)),
            "contentChars": detail["contentPreviewChars"],
            "tocChapters": toc_unique_chapters,
        }
        if content_preview and not merged.get("bookSourceComment"):
            merged["bookSourceComment"] = f"tocPages={toc_pages};tocFirstPageItems={toc_first_page};tocUniqueChapters={toc_unique_chapters};contentPages={content_pages}"
        return ValidationOutcome(True, search_mode="search_or_explore", source_name=str(src.get("bookSourceName") or ""), book_url=first_book_url, detail_name=name, detail_toc_url=toc_final_url or normalized_toc_url, response_time_ms=elapsed), merged, detail
    except SourceValidationDeadlineExceeded:
        raise
    except RuleScriptError as exc:
        # Uncaught source-script exception (Legado ScriptException) -> js失效.
        runtime.script_error_sink = None
        detail["error"] = str(exc)
        detail["scriptError"] = str(exc)
        detail["failureReason"] = "js error"
        detail["failureGroup"] = "js失效"
        detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
        return ValidationOutcome(False, reason="js error", response_time_ms=detail["responseTimeMs"]), None, detail
    except Exception as exc:
        detail["error"] = str(exc)
        detail["failureReason"] = "exception"
        detail["responseTimeMs"] = int((time.perf_counter() - start) * 1000)
        return ValidationOutcome(False, reason=str(exc), response_time_ms=detail["responseTimeMs"]), None, detail


def validate_group(
    url: str,
    candidates: list[dict[str, Any]],
    quick_seed: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """Validate one URL group, preferring the candidate selected by quick scan."""
    last_detail: dict[str, Any] | None = None
    ordered_candidates = list(candidates)
    preferred_source = quick_seed.get("source") if isinstance(quick_seed, dict) else None
    preferred_key: str | None = None
    # R147 (L10): every candidate is serialized at most once (the fingerprint is cached by object id); the sort key
    # and the per-candidate seed check used to serialize each candidate and the preferred source again each time.
    fingerprints: dict[int, str] = {}
    if isinstance(preferred_source, dict):
        preferred_key = json.dumps(preferred_source, ensure_ascii=False, sort_keys=True)
        for candidate in ordered_candidates:
            if candidate is preferred_source:
                fingerprints[id(candidate)] = preferred_key
            else:
                fingerprints[id(candidate)] = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
        ordered_candidates.sort(key=lambda candidate: 0 if fingerprints[id(candidate)] == preferred_key else 1)
    for src in ordered_candidates:
        ensure_source_validation_time_remaining()
        seed_for_candidate = quick_seed if src is preferred_source or (
            preferred_key is not None and fingerprints.get(id(src)) == preferred_key
        ) else None
        outcome, record, detail = validate_single_source(src, quick_seed=seed_for_candidate)
        last_detail = detail
        if outcome.passed and record is not None:
            normalized = normalize_book_source_url(str(record.get("bookSourceUrl") or ""))
            merged = dict(record)
            merged["bookSourceUrl"] = normalized
            merged["customTag"] = app_profiles.VALIDATED_PASS_TAG
            merged["bookSourceGroup"] = _validated_group_value(record, app_profiles.current_app_version())
            detail["bookSourceUrl"] = normalized
            return normalized, merged, detail
    if last_detail is not None:
        last_detail["bookSourceUrl"] = _group_source_url(url, candidates)
        return url, None, last_detail
    fallback_name = str(candidates[0].get("bookSourceName") or "") if candidates else ""
    return url, None, _empty_detail({"bookSourceName": fallback_name, "bookSourceUrl": _group_source_url(url, candidates)})


def timeout_detail(url: str, candidates: list[dict[str, Any]], round_num: int, idle_timeout: int) -> dict[str, Any]:
    fallback_name = str(candidates[0].get("bookSourceName") or "") if candidates else ""
    detail = _empty_detail({"bookSourceName": fallback_name, "bookSourceUrl": _group_source_url(url, candidates)})
    detail["failureReason"] = "validation idle timeout"
    detail["error"] = f"round {round_num} had no completed source for {idle_timeout}s; remaining source marked failed"
    detail["responseTimeMs"] = idle_timeout * 1000
    return detail


def source_timeout_detail(url: str, candidates: list[dict[str, Any]], source_timeout: int) -> dict[str, Any]:
    fallback_name = str(candidates[0].get("bookSourceName") or "") if candidates else ""
    detail = _empty_detail({"bookSourceName": fallback_name, "bookSourceUrl": _group_source_url(url, candidates)})
    detail["failureReason"] = "source validation timeout"
    detail["error"] = f"complete search/detail/toc/content chain exceeded {source_timeout}s"
    detail["responseTimeMs"] = source_timeout * 1000
    return detail


# ---- R147 (L2): abandoned worker threads ---------------------------------------------------------------------------
# After an idle timeout a worker that is still inside a validation cannot be killed (the source deadline is
# cooperative, and killing a thread mid-request is not safe). It keeps using network / CPU while the next stage
# starts, so the real concurrency would exceed `--workers`. The stage that gives up on such a thread registers it
# here; every later stage subtracts the still-running ones from its worker budget (waiting a bounded time first) and
# the report summary records how many workers were abandoned (`abandonedWorkers`).
ABANDONED_WORKER_WAIT_SECONDS = 15.0
_abandoned_lock = threading.Lock()
_abandoned_threads: list[threading.Thread] = []
_abandoned_total = 0


def reset_abandoned_worker_count() -> None:
    """Start counting abandoned workers for a new pipeline run (threads still running stay in the budget)."""
    global _abandoned_total
    with _abandoned_lock:
        _abandoned_total = 0


def note_abandoned_workers(threads: Iterable[threading.Thread]) -> int:
    """Register worker threads a stage gave up on while they were still validating. Returns how many."""
    global _abandoned_total
    alive = [thread for thread in threads if thread.is_alive()]
    with _abandoned_lock:
        for thread in alive:
            if thread not in _abandoned_threads:
                _abandoned_threads.append(thread)
                _abandoned_total += 1
    return len(alive)


def abandoned_workers_alive() -> int:
    """Abandoned workers that are still running (finished ones are forgotten)."""
    with _abandoned_lock:
        _abandoned_threads[:] = [thread for thread in _abandoned_threads if thread.is_alive()]
        return len(_abandoned_threads)


def abandoned_workers_total() -> int:
    with _abandoned_lock:
        return _abandoned_total


def stage_worker_budget(max_workers: int, total: int, wait_seconds: float | None = None) -> int:
    """Workers a new stage may start: ``max_workers`` minus abandoned workers that are still running.

    Waits up to ``wait_seconds`` (default ``ABANDONED_WORKER_WAIT_SECONDS``) for abandoned workers to finish when
    they would use the whole budget; a stage always gets at least one worker so the pipeline keeps moving (that one
    worker may briefly exceed the limit by one).
    """
    limit = max(1, int(max_workers))
    wait = ABANDONED_WORKER_WAIT_SECONDS if wait_seconds is None else wait_seconds
    deadline = time.monotonic() + max(0.0, wait)
    alive = abandoned_workers_alive()
    while alive >= limit and time.monotonic() < deadline:
        time.sleep(0.2)
        alive = abandoned_workers_alive()
    granted = max(1, min(limit - alive, max(1, total)))
    if alive:
        print(f"Abandoned workers still running: {alive}; new stage uses {granted} of {limit} workers", flush=True)
    return granted


def run_round_with_idle_timeout(
    round_url_list: list[str],
    groups: dict[str, list[dict[str, Any]]],
    max_workers: int,
    round_num: int,
    idle_timeout: int,
    source_timeout: int = 60,
) -> tuple[set[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]], int]:
    total_r = len(round_url_list)
    work_q: queue.Queue[str] = queue.Queue()
    result_q: queue.Queue[tuple[str, dict[str, Any] | None, dict[str, Any]]] = queue.Queue()
    stop_event = threading.Event()
    remaining: set[str] = set(round_url_list)
    round_passed: set[str] = set()
    round_records: dict[str, dict[str, Any]] = {}
    round_details: dict[str, dict[str, Any]] = {}
    timed_out = 0
    busy: set[int] = set()
    threads: list[threading.Thread] = []

    for url in round_url_list:
        work_q.put(url)

    def worker() -> None:
        while not stop_event.is_set():
            try:
                url = work_q.get_nowait()
            except queue.Empty:
                return
            busy.add(threading.get_ident())
            try:
                begin_source_validation_deadline(source_timeout)
                # Preserve the scheduled group key. The validator may return a
                # candidate's serialized URL (another cosmetic variant of the
                # same dedupe group), but the queue and remaining-set are keyed
                # by the scheduled key.
                _, record, detail = validate_group(url, groups[url])
                result_q.put((url, record, detail))
            except SourceValidationDeadlineExceeded:
                detail = source_timeout_detail(url, groups.get(url, []), source_timeout)
                result_q.put((url, None, detail))
            except Exception as exc:
                detail = timeout_detail(url, groups.get(url, []), round_num, idle_timeout)
                detail["failureReason"] = "worker exception"
                detail["error"] = str(exc)
                result_q.put((url, None, detail))
            finally:
                clear_source_validation_deadline()
                busy.discard(threading.get_ident())
                try:
                    work_q.task_done()
                except Exception:
                    pass

    worker_count = stage_worker_budget(max_workers, total_r)
    for _ in range(worker_count):
        # Threads do not inherit contextvars: bind the App profile once per thread (R143).
        thread = threading.Thread(target=app_profiles.bind_current_profile(worker), daemon=True)
        threads.append(thread)
        thread.start()

    done_r = 0
    last_result_at = time.monotonic()
    while done_r < total_r:
        try:
            key, record, detail = result_q.get(timeout=1.0)
        except queue.Empty:
            if idle_timeout > 0 and (time.monotonic() - last_result_at) >= idle_timeout:
                stop_event.set()
                note_abandoned_workers([thread for thread in threads if thread.ident in busy])
                timed_out = len(remaining)
                for url in sorted(remaining):
                    round_details[url] = timeout_detail(url, groups.get(url, []), round_num, idle_timeout)
                done_r = total_r
                print(f"Progress: {done_r}/{total_r}, passed={len(round_passed)}, timedOut={timed_out}", flush=True)
                break
            continue

        if key not in remaining:
            continue
        remaining.remove(key)
        done_r += 1
        last_result_at = time.monotonic()
        round_details[key] = detail
        if record is not None:
            round_passed.add(key)
            round_records[key] = record
        if done_r % 20 == 0 or done_r == total_r:
            print(f"Progress: {done_r}/{total_r}, passed={len(round_passed)}", flush=True)

    return round_passed, round_records, round_details, timed_out


def pipeline_timeout_detail(
    url: str,
    candidates: list[dict[str, Any]],
    stage: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    fallback_name = str(candidates[0].get("bookSourceName") or "") if candidates else ""
    detail = _empty_detail({"bookSourceName": fallback_name, "bookSourceUrl": _group_source_url(url, candidates)})
    detail["pipelineStage"] = stage
    detail["failureReason"] = f"{stage} timeout"
    detail["error"] = f"{stage} exceeded {timeout_seconds}s; source marked failed and the batch continued"
    detail["responseTimeMs"] = max(0, int(timeout_seconds)) * 1000
    return detail


def run_parallel_stage(
    stage: str,
    url_list: list[str],
    groups: dict[str, list[dict[str, Any]]],
    max_workers: int,
    idle_timeout: int,
    source_timeout: int,
    handler: Callable[[str], tuple[str, dict[str, Any] | None, dict[str, Any]]],
) -> tuple[set[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]], int]:
    """Run one bounded pipeline stage with a dedicated worker pool.

    Stages are intentionally sequential at the pipeline level, while sources
    within a stage run in parallel. This keeps CPU/network pressure bounded and
    guarantees that a slow source cannot block later stages.
    """
    total = len(url_list)
    if total == 0:
        return set(), {}, {}, 0
    work_q: queue.Queue[str] = queue.Queue()
    result_q: queue.Queue[tuple[str, dict[str, Any] | None, dict[str, Any]]] = queue.Queue()
    stop_event = threading.Event()
    remaining: set[str] = set(url_list)
    passed: set[str] = set()
    records: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, Any]] = {}
    timed_out = 0
    busy: set[int] = set()
    threads: list[threading.Thread] = []
    for url in url_list:
        work_q.put(url)

    def worker() -> None:
        while not stop_event.is_set():
            try:
                url = work_q.get_nowait()
            except queue.Empty:
                return
            busy.add(threading.get_ident())
            try:
                begin_source_validation_deadline(source_timeout)
                _, record, detail = handler(url)
                # The queue key is the scheduled group URL, not a normalized
                # URL returned by a candidate. This prevents a harmless
                # trailing-slash normalization from being treated as a lost
                # result and waiting until the idle timeout.
                result_q.put((url, record, detail))
            except SourceValidationDeadlineExceeded:
                result_q.put((url, None, pipeline_timeout_detail(url, groups.get(url, []), stage, source_timeout)))
            except Exception as exc:
                detail = _empty_detail({
                    "bookSourceName": str(groups.get(url, [{}])[0].get("bookSourceName") or "") if groups.get(url) else "",
                    "bookSourceUrl": _group_source_url(url, groups.get(url, [])),
                })
                detail["pipelineStage"] = stage
                detail["failureReason"] = "worker exception"
                detail["error"] = str(exc)
                result_q.put((url, None, detail))
            finally:
                clear_source_validation_deadline()
                busy.discard(threading.get_ident())
                try:
                    work_q.task_done()
                except Exception:
                    pass

    worker_count = stage_worker_budget(max_workers, total)
    for _ in range(worker_count):
        # Threads do not inherit contextvars: bind the App profile once per thread (R143).
        thread = threading.Thread(
            target=app_profiles.bind_current_profile(worker),
            daemon=True,
            name=f"source-validator-{stage}",
        )
        threads.append(thread)
        thread.start()

    done = 0
    last_result_at = time.monotonic()
    while done < total:
        try:
            key, record, detail = result_q.get(timeout=1.0)
        except queue.Empty:
            if idle_timeout > 0 and time.monotonic() - last_result_at >= idle_timeout:
                stop_event.set()
                note_abandoned_workers([thread for thread in threads if thread.ident in busy])
                timed_out = len(remaining)
                for pending_url in sorted(remaining):
                    details[pending_url] = pipeline_timeout_detail(pending_url, groups.get(pending_url, []), stage, idle_timeout)
                done = total
                print(f"Progress: {done}/{total}, passed={len(passed)}, timedOut={timed_out}, stage={stage}", flush=True)
                break
            continue
        if key not in remaining:
            continue
        remaining.remove(key)
        done += 1
        last_result_at = time.monotonic()
        details[key] = detail
        if record is not None:
            passed.add(key)
            records[key] = record
        if done % 10 == 0 or done == total:
            print(f"Progress: {done}/{total}, passed={len(passed)}, stage={stage}", flush=True)
    return passed, records, details, timed_out


@dataclass
class _StagedOutcome:
    """Everything the staged stages produced for one App profile, before report assembly."""

    version: str
    urls: list[str]
    groups: dict[str, list[dict[str, Any]]]
    max_workers: int
    quick_timeout: int
    source_timeout: int
    rounds: int
    min_pass_rounds: int
    quick_passed: set[str]
    full_passed: set[str]
    all_details: dict[str, dict[str, Any]]
    best_records: dict[str, dict[str, Any]]
    pass_counts: dict[str, int]
    final_urls: set[str]
    stability_timed_out: int = 0
    stability_completed_rounds: int = 1


def _run_staged_stages(
    urls: list[str],
    groups: dict[str, list[dict[str, Any]]],
    max_workers: int,
    quick_timeout: int,
    source_timeout: int,
    rounds: int,
    min_pass_rounds: int,
    idle_timeout: int,
) -> _StagedOutcome:
    """Execute dedupe → quick scan → full chain → stability re-test under the current App profile."""
    print(f"Pipeline dedupe: {len(urls)} unique source groups (canonical site + rule fingerprint).", flush=True)
    quick_passed, quick_seeds, quick_details, quick_timed_out = run_parallel_stage(
        "quick-scan",
        sorted(urls),
        groups,
        max_workers,
        idle_timeout,
        max(0, quick_timeout),
        lambda url: quick_scan_group(url, groups[url]),
    )
    print(
        f"Pipeline quick-scan: {len(quick_passed)}/{len(urls)} passed; "
        f"timeout={max(0, quick_timeout)}s, timedOut={quick_timed_out}.",
        flush=True,
    )

    all_details: dict[str, dict[str, Any]] = dict(quick_details)
    full_passed, full_records, full_details, full_timed_out = run_parallel_stage(
        "full-validation",
        sorted(quick_passed),
        groups,
        max_workers,
        idle_timeout,
        max(0, source_timeout),
        lambda url: validate_group(url, groups[url], quick_seed=quick_seeds.get(url)),
    )
    all_details.update(full_details)
    print(
        f"Pipeline full-validation: {len(full_passed)}/{len(quick_passed)} passed; "
        f"timeout={max(0, source_timeout)}s, timedOut={full_timed_out}.",
        flush=True,
    )

    rounds = max(1, rounds)
    min_pass_rounds = max(1, min(min_pass_rounds, rounds))
    pass_counts: dict[str, int] = {url: 1 for url in full_passed}
    best_records: dict[str, dict[str, Any]] = dict(full_records)
    candidate_urls: set[str] = set(full_passed)
    stability_timed_out = 0
    stability_completed_rounds = 1
    for round_num in range(2, rounds + 1):
        if not candidate_urls:
            break
        round_urls = sorted(candidate_urls)
        print(f"=== stability re-test {round_num}/{rounds}: {len(round_urls)} sources ===", flush=True)
        round_passed, round_records, round_details, timed_out = run_parallel_stage(
            f"stability-{round_num}",
            round_urls,
            groups,
            max_workers,
            idle_timeout,
            max(0, source_timeout),
            # Stability deliberately performs a fresh search and does not use
            # the quick seed, catching transient search/detail drift.
            lambda url: validate_group(url, groups[url]),
        )
        stability_completed_rounds = round_num
        stability_timed_out += timed_out
        all_details.update(round_details)
        for url, record in round_records.items():
            pass_counts[url] = pass_counts.get(url, 0) + 1
            best_records[url] = record
        candidate_urls = round_passed
        print(f"  stability round {round_num}: {len(round_passed)}/{len(round_urls)} passed", flush=True)

    for url, detail in all_details.items():
        detail["stabilityPassCount"] = pass_counts.get(url, 0)
    if min_pass_rounds < rounds:
        # R147 (L1): only sources that passed the previous round are retested (`candidate_urls = round_passed`), so a
        # source that fails round K is never tried again. `count >= min_pass_rounds` therefore means "passed the
        # first min_pass_rounds rounds in a row"; failures in the rounds after that are tolerated. A source that fails
        # mid-way and "recovers" later is NOT counted (retesting every source every round would multiply the
        # requests, and a flaky source is not what the App should be shipped).
        final_urls = {url for url, count in pass_counts.items() if count >= min_pass_rounds}
    else:
        final_urls = candidate_urls
    return _StagedOutcome(
        version=app_profiles.current_app_version(),
        urls=list(urls),
        groups=groups,
        max_workers=max_workers,
        quick_timeout=quick_timeout,
        source_timeout=source_timeout,
        rounds=rounds,
        min_pass_rounds=min_pass_rounds,
        quick_passed=set(quick_passed),
        full_passed=set(full_passed),
        all_details=all_details,
        best_records=best_records,
        pass_counts=pass_counts,
        final_urls=set(final_urls),
        stability_timed_out=stability_timed_out,
        stability_completed_rounds=stability_completed_rounds,
    )


def _finalize_staged(
    outcome: _StagedOutcome,
    total_records: int,
    proven_on_111: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Aggregate by book, assemble the report document and compute each source's App verdict.

    ``proven_on_111`` is only given for the 1.1.2 half of ``both`` mode: groups that already passed on 1.1.1 do
    not get the `需1.1.2` group token. The outcome itself is never mutated apart from detail verdict fields.
    """
    urls = outcome.urls
    groups = outcome.groups
    final_urls = outcome.final_urls
    version = outcome.version
    verdict_counts: dict[str, int] = {"pass": 0, "invalid": 0, "needsInteraction": 0, "timeout": 0}
    for url, detail in outcome.all_details.items():
        verdict = "pass" if url in final_urls else app_profiles.app_verdict(detail, False, version)
        detail["appVerdict"] = verdict
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    final_entries: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
    for url in urls:
        if url in final_urls and url in outcome.best_records:
            record = dict(outcome.best_records[url])
            if proven_on_111 is not None:
                record["bookSourceGroup"] = _validated_group_value(record, version, url in proven_on_111)
            final_entries.append((url, record, outcome.all_details.get(url)))
    passed, book_aggregation = aggregate_validated_sources(final_entries)
    for record in passed:
        record.pop("__readoriValidation", None)
    passed.sort(key=lambda item: (normalize_book_source_url(str(item.get("bookSourceUrl") or "")), str(item.get("bookSourceName") or "")))
    report_results = list(outcome.all_details.values())
    report_results.sort(key=lambda item: (normalize_book_source_url(str(item.get("bookSourceUrl") or "")), str(item.get("bookSourceName") or "")))
    canonical_sites = {canonical_source_site_key(candidates[0]) for candidates in groups.values() if candidates and canonical_source_site_key(candidates[0])}
    rule_fingerprints = {source_rule_fingerprint(candidates[0]) for candidates in groups.values() if candidates}
    max_workers = outcome.max_workers
    quick_timeout = outcome.quick_timeout
    source_timeout = outcome.source_timeout
    pipeline = {
        "dedupe": {"records": total_records, "uniqueSourceUrls": len(urls), "uniqueSourceGroups": len(urls), "canonicalSites": len(canonical_sites), "ruleFingerprints": len(rule_fingerprints)},
        "quickScan": {"candidates": len(urls), "passed": len(outcome.quick_passed), "timeoutSeconds": max(0, quick_timeout), "workers": max_workers},
        "fullValidation": {"candidates": len(outcome.quick_passed), "passed": len(outcome.full_passed), "timeoutSeconds": max(0, source_timeout), "workers": max_workers},
        "stabilityRetest": {"roundsRequested": outcome.rounds, "roundsCompleted": outcome.stability_completed_rounds, "initialCandidates": len(outcome.full_passed), "finalPassed": len(final_urls), "timedOut": outcome.stability_timed_out, "workers": max_workers},
        "bookAggregation": book_aggregation,
    }
    summary = {
        "uniqueSourceUrls": len(urls),
        "uniqueSourceGroups": len(urls),
        "canonicalSites": len(canonical_sites),
        "totalRecordsLoaded": total_records,
        "passed": len(passed),
        "sourceGroupsPassed": len(final_urls),
        "failed": len(urls) - len(final_urls),
        "deduplicatedAfterValidation": book_aggregation["removed"],
        "workers": max_workers,
        # R147 (L2): workers a stage gave up on after the idle timeout (not killed); `...StillRunning` is how many
        # of them were still busy when the report was written.
        "abandonedWorkers": abandoned_workers_total(),
        "abandonedWorkersStillRunning": abandoned_workers_alive(),
        "quickScanTimeoutSeconds": max(0, quick_timeout),
        "sourceTimeoutSeconds": max(0, source_timeout),
        "stabilityRounds": outcome.rounds,
        "minPassRounds": outcome.min_pass_rounds,
        "pipeline": pipeline,
        "checks": ["connectivity", "search_or_explore", "book_detail_name_or_tocUrl", "toc_first_chapter", "chapter_content_non_empty"],
        "appVersion": version,
        "appProfile": asdict(app_profiles.APP_PROFILES[version]),
        "appVerdicts": verdict_counts,
    }
    return {"summary": summary, "results": report_results, "passedSources": passed}, passed


def run_staged_pipeline(
    urls: list[str],
    groups: dict[str, list[dict[str, Any]]],
    total_records: int,
    max_workers: int,
    quick_timeout: int,
    source_timeout: int,
    rounds: int,
    min_pass_rounds: int,
    idle_timeout: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Execute dedupe → quick scan → full chain → stability re-test under the current App profile."""
    reset_abandoned_worker_count()
    outcome = _run_staged_stages(
        urls, groups, max_workers, quick_timeout, source_timeout, rounds, min_pass_rounds, idle_timeout
    )
    return _finalize_staged(outcome, total_records)


def _combine_with_reused_111(
    base: _StagedOutcome,
    rerun: _StagedOutcome | None,
    rerun_urls: set[str],
) -> _StagedOutcome:
    """1.1.2 view of a ``both`` run: re-validated groups come from ``rerun``, every other group reuses 1.1.1."""
    reused = [url for url in base.urls if url not in rerun_urls]
    reused_set = set(reused)
    details: dict[str, dict[str, Any]] = {}
    for url in base.urls:
        if url in reused_set:
            source_detail = base.all_details.get(url)
            if source_detail is None:
                continue
            detail = copy.deepcopy(source_detail)
            detail["appVersion"] = "1.1.2"
            detail["reusedFrom"] = "1.1.1"
            details[url] = detail
        elif rerun is not None and url in rerun.all_details:
            details[url] = rerun.all_details[url]
    best_records = {url: dict(base.best_records[url]) for url in reused if url in base.best_records}
    pass_counts = {url: base.pass_counts[url] for url in reused if url in base.pass_counts}
    quick_passed = {url for url in base.quick_passed if url in reused_set}
    full_passed = {url for url in base.full_passed if url in reused_set}
    final_urls = {url for url in base.final_urls if url in reused_set}
    stability_timed_out = 0
    stability_rounds = base.stability_completed_rounds
    if rerun is not None:
        best_records.update(rerun.best_records)
        pass_counts.update(rerun.pass_counts)
        quick_passed |= rerun.quick_passed
        full_passed |= rerun.full_passed
        final_urls |= rerun.final_urls
        stability_timed_out = rerun.stability_timed_out
        stability_rounds = max(stability_rounds, rerun.stability_completed_rounds)
    return _StagedOutcome(
        version="1.1.2",
        urls=list(base.urls),
        groups=base.groups,
        max_workers=base.max_workers,
        quick_timeout=base.quick_timeout,
        source_timeout=base.source_timeout,
        rounds=base.rounds,
        min_pass_rounds=base.min_pass_rounds,
        quick_passed=quick_passed,
        full_passed=full_passed,
        all_details=details,
        best_records=best_records,
        pass_counts=pass_counts,
        final_urls=final_urls,
        stability_timed_out=stability_timed_out,
        stability_completed_rounds=stability_rounds,
    )


def run_staged_pipeline_both(
    urls: list[str],
    groups: dict[str, list[dict[str, Any]]],
    total_records: int,
    max_workers: int,
    quick_timeout: int,
    source_timeout: int,
    rounds: int,
    min_pass_rounds: int,
    idle_timeout: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate under App 1.1.1, re-validate only the profile-sensitive groups under 1.1.2, and compare.

    Returns ``(report_doc, passed_on_111, passed_on_112)``. ``report_doc`` keeps the 1.1.1 structure and only adds
    fields (``summary.profiles`` / ``summary.compatibility`` / ``results[].compatibility`` /
    ``passedSourcesByAppVersion``).
    """
    stage_args = (max_workers, quick_timeout, source_timeout, rounds, min_pass_rounds, idle_timeout)
    reset_abandoned_worker_count()
    with app_profiles.app_profile_scope("1.1.1"):
        print(f"=== App 1.1.1: validating {len(urls)} source groups ===", flush=True)
        outcome_111 = _run_staged_stages(urls, groups, *stage_args)
    sensitive_urls = [
        url for url in urls if any(app_profiles.app_profile_sensitivity(candidate) for candidate in groups.get(url, []))
    ]
    sensitive_set = set(sensitive_urls)
    outcome_112_rerun: _StagedOutcome | None = None
    with app_profiles.app_profile_scope("1.1.2"):
        print(
            f"=== App 1.1.2: re-validating {len(sensitive_urls)}/{len(urls)} profile-sensitive groups ===",
            flush=True,
        )
        if sensitive_urls:
            outcome_112_rerun = _run_staged_stages(
                sensitive_urls, {url: groups[url] for url in sensitive_urls}, *stage_args
            )
    outcome_112 = _combine_with_reused_111(outcome_111, outcome_112_rerun, sensitive_set)

    report_doc, passed_111 = _finalize_staged(outcome_111, total_records)
    report_112, passed_112 = _finalize_staged(outcome_112, total_records, proven_on_111=outcome_111.final_urls)

    labels: dict[str, int] = {}
    for url, detail in outcome_111.all_details.items():
        detail_112 = outcome_112.all_details.get(url) or {}
        verdict_111 = str(detail.get("appVerdict") or "invalid")
        verdict_112 = str(detail_112.get("appVerdict") or "invalid")
        label = app_profiles.compatibility_label(verdict_111, verdict_112)
        labels[label] = labels.get(label, 0) + 1
        detail["compatibility"] = {
            "1.1.1": verdict_111,
            "1.1.2": verdict_112,
            "label": label,
            "app112": {
                "failureReason": str(detail_112.get("failureReason") or ""),
                "error": str(detail_112.get("error") or ""),
                "appVerdict": verdict_112,
                "suppressedInteractions": list(detail_112.get("suppressedInteractions") or []),
                "reusedFrom": str(detail_112.get("reusedFrom") or ""),
            },
        }

    def profile_summary(summary: dict[str, Any], **extra: Any) -> dict[str, Any]:
        keys = ("passed", "sourceGroupsPassed", "failed", "appVerdicts", "appProfile")
        out = {key: summary[key] for key in keys if key in summary}
        out.update(extra)
        return out

    summary = report_doc["summary"]
    summary["appVersion"] = "both"
    summary["profiles"] = {
        "1.1.1": profile_summary(summary, rerunGroups=len(urls)),
        "1.1.2": profile_summary(
            report_112["summary"],
            rerunGroups=len(sensitive_urls),
            reusedFrom111Groups=len(urls) - len(sensitive_urls),
        ),
    }
    summary["compatibility"] = labels
    report_doc["passedSourcesByAppVersion"] = {"1.1.2": passed_112}
    return report_doc, passed_111, passed_112


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证书源：搜索/发现 → 详情 → 目录 → 正文（与 iOS BookSourceValidator 四阶段一致）")
    parser.add_argument("--json-dir", type=Path, default=JSON_DIR, help="默认书源 JSON 目录（默认 docs/Json）")
    parser.add_argument("--input", type=Path, action="append", default=[], help="额外输入文件或目录；可重复传入，传入后不再自动扫描 --json-dir")
    parser.add_argument("--report-path", type=Path, default=None, help="汇总报告输出路径；默认写到 json-dir/validation_report_YYYY-MM-DD.json")
    parser.add_argument("--validated-output", type=Path, default=None, help="通过书源输出路径；默认写到 json-dir/bookinfo_validated_sources.json")
    parser.add_argument("--validated-output-full", type=Path, default=None, help="完整通过书源输出路径；默认写到 json-dir/bookinfo_validated_sources_full.json")
    parser.add_argument("--report-only", action="store_true", help="只写汇总报告，不写 validated sources 输出")
    parser.add_argument("--no-mirror", action="store_true", help="不再写入仓库 reports/ 镜像报告")
    parser.add_argument("--mirror-dir", type=Path, default=None, help="报告镜像目录；默认仓库根 reports/")
    parser.add_argument("--workers", type=int, default=0, help="并发线程数（0=自动）")
    parser.add_argument("--limit", type=int, default=0, help="仅验证前 N 个唯一书源 URL（0=全部，用于调试）")
    parser.add_argument("--pipeline", choices=("staged", "legacy"), default="staged", help="验证流水线；staged=去重→快速扫描→完整验证→稳定性复测（默认），legacy=旧版完整轮次")
    parser.add_argument("--quick-timeout", type=int, default=8, help="快速扫描单源硬截止秒数，建议 5-10 秒；0 表示不限制")
    parser.add_argument("--rounds", type=int, default=3, help="稳定性复测总轮次（含首次完整验证），默认 3 轮")
    parser.add_argument("--min-pass-rounds", type=int, default=3, help="最终交付至少需连续通过前几轮（默认 3，不超过 --rounds）。每轮只复测上一轮通过的书源；小于 --rounds 时，前 N 轮连续通过即可交付，之后的复测失败不再扣除，中途失败的书源不会再复测")
    parser.add_argument("--idle-timeout", type=int, default=180, help="单轮无任何书源完成的最长等待秒数；超时后剩余源判失败，避免坏源拖死整批")
    parser.add_argument("--source-timeout", type=int, default=60, help="单个书源完整搜索/详情/目录/正文链路的硬截止秒数；0 表示不限制")
    parser.add_argument("--app-version", choices=("1.1.1", "1.1.2", "both"), default=DEFAULT_APP_VERSION, help="模拟的 App 版本：1.1.1=当前公开版（默认）；1.1.2=下一版；both=两版对比（只重跑受版本影响的书源，额外输出 *.app-1.1.2.json）")
    parser.add_argument("--block-private-network", action="store_true", help="拒绝向私网 / 回环 / 链路本地 / 云元数据地址发请求（含重定向每一跳）；默认允许，因为本机运行时书源可能在局域网内")
    args = parser.parse_args(argv)

    if args.app_version == "both":
        if args.pipeline == "legacy":
            print("--app-version both requires --pipeline staged.", file=sys.stderr, flush=True)
            return 2
    previous_allow_private = net_guard.network_allows_private()
    if args.block_private_network:
        net_guard.set_network_policy(False)
    try:
        if args.app_version == "both":
            return _run_main(args, "both")
        with app_profiles.app_profile_scope(args.app_version):
            return _run_main(args, args.app_version)
    finally:
        if args.block_private_network:
            net_guard.set_network_policy(previous_allow_private)


def _app_112_output_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.app-1.1.2{path.suffix}")


def _run_main(args: argparse.Namespace, app_version: str) -> int:
    root_resolved = ROOT.resolve()
    json_dir = Path(args.json_dir).expanduser().resolve()
    if args.input:
        sources, input_files = load_sources_from_paths(args.input)
    else:
        sources, input_files = load_sources_from_dir(json_dir)
    if not sources:
        print("No sources found.", flush=True)
        return 1

    report_path = Path(args.report_path).expanduser().resolve() if args.report_path else default_report_path(json_dir)
    validated_output, validated_output_full = default_validated_output_paths(json_dir)
    if args.validated_output:
        validated_output = Path(args.validated_output).expanduser().resolve()
    if args.validated_output_full:
        validated_output_full = Path(args.validated_output_full).expanduser().resolve()
    mirror_dir = None if args.no_mirror else Path(args.mirror_dir).expanduser().resolve() if args.mirror_dir else (root_resolved / "reports")

    groups = group_sources(sources)
    urls = list(groups.keys())
    if args.limit and args.limit > 0:
        urls = urls[: args.limit]
        groups = {u: groups[u] for u in urls}

    canonical_sites = {canonical_source_site_key(candidates[0]) for candidates in groups.values() if candidates and canonical_source_site_key(candidates[0])}
    print(f"Loaded {len(sources)} records from {len(input_files)} file(s), {len(urls)} unique source URLs ({len(canonical_sites)} canonical sites; deduped by site + rule fingerprint).", flush=True)
    print(f"App profile: {app_version}", flush=True)
    if input_files:
        for name in input_files[:12]:
            print(f"  - {name}", flush=True)
        if len(input_files) > 12:
            print(f"  ... +{len(input_files) - 12} more", flush=True)

    cpu = os.cpu_count() or 4
    max_workers = args.workers if args.workers > 0 else min(20, max(8, cpu * 2))

    if args.pipeline == "staged":
        passed_112: list[dict[str, Any]] | None = None
        pipeline_args = (
            urls,
            groups,
            len(sources),
            max_workers,
            max(0, args.quick_timeout),
            max(0, args.source_timeout),
            max(1, args.rounds),
            max(1, args.min_pass_rounds),
            max(0, args.idle_timeout),
        )
        if app_version == "both":
            report_doc, passed, passed_112 = run_staged_pipeline_both(*pipeline_args)
        else:
            report_doc, passed = run_staged_pipeline(*pipeline_args)
        try:
            json_dir_rel = str(json_dir.relative_to(root_resolved))
        except ValueError:
            json_dir_rel = str(json_dir)
        report_doc["summary"].update({
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "jsonDir": json_dir_rel,
            "inputFiles": input_files,
        })
        report_text = json.dumps(report_doc, ensure_ascii=False, indent=2)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")
        print(f"Wrote validation report ({len(report_doc.get('results') or [])} entries) to {report_path}", flush=True)
        if mirror_dir is not None:
            try:
                mirror_dir.mkdir(parents=True, exist_ok=True)
                mirror_path = mirror_dir / report_path.name
                mirror_path.write_text(report_text, encoding="utf-8")
                print(f"Mirror report for git: {mirror_path}", flush=True)
            except OSError as exc:
                print(f"Could not write reports mirror: {exc}", flush=True)
        if not args.report_only:
            payload = json.dumps(passed, ensure_ascii=False, indent=2)
            validated_output.parent.mkdir(parents=True, exist_ok=True)
            validated_output_full.parent.mkdir(parents=True, exist_ok=True)
            validated_output.write_text(payload, encoding="utf-8")
            validated_output_full.write_text(payload, encoding="utf-8")
            print(f"Wrote {len(passed)} validated sources to {validated_output}", flush=True)
            print(f"Wrote {len(passed)} validated sources to {validated_output_full}", flush=True)
            if passed_112 is not None:
                payload_112 = json.dumps(passed_112, ensure_ascii=False, indent=2)
                for target in (validated_output, validated_output_full):
                    target_112 = _app_112_output_path(target)
                    target_112.write_text(payload_112, encoding="utf-8")
                    print(f"Wrote {len(passed_112)} validated sources (App 1.1.2) to {target_112}", flush=True)
        return 0

    # ─── 多轮验证：只有连续通过的书源才视为生产可用 ───────────────────────────
    num_rounds = max(1, args.rounds)
    min_pass   = max(1, min(args.min_pass_rounds, num_rounds))
    reset_abandoned_worker_count()

    round_pass_counts: dict[str, int]           = {}  # url → 累计通过轮次
    round_best_record: dict[str, dict[str, Any]] = {}  # url → 最新通过时的完整记录
    all_round_details: dict[str, dict[str, Any]] = {}  # url → 最新一次明细（用于报告）

    candidate_urls: set[str] = set(urls)

    for round_num in range(1, num_rounds + 1):
        if not candidate_urls:
            print(f"  第 {round_num} 轮前无剩余书源，提前结束。", flush=True)
            break
        round_url_list = sorted(candidate_urls)
        total_r = len(round_url_list)
        done_r  = 0
        round_passed: set[str] = set()
        print(f"\n=== 第 {round_num}/{num_rounds} 轮 — 验证 {total_r} 个书源 ===", flush=True)

        round_passed, round_records, round_details, timed_out = run_round_with_idle_timeout(
            round_url_list,
            groups,
            max_workers,
            round_num,
            max(0, args.idle_timeout),
            max(0, args.source_timeout),
        )
        all_round_details.update(round_details)
        for key, record in round_records.items():
            round_pass_counts[key] = round_pass_counts.get(key, 0) + 1
            round_best_record[key] = record

        timeout_suffix = f"，{timed_out} 个超时判失败" if timed_out else ""
        print(f"  第 {round_num} 轮结果：{len(round_passed)}/{total_r} 通过{timeout_suffix} → 进入下一轮", flush=True)
        candidate_urls = round_passed  # 下一轮只验证本轮通过的书源

    # 最终交付：通过轮次 >= min_pass 的书源
    if min_pass < num_rounds:
        # R147（L1）：每轮只复测上一轮通过的书源，中途失败的不会再测，所以这里的含义是“连续通过前 min_pass 轮”，
        # 之后的复测失败不扣除；中途失败后“恢复”的书源不会计入。
        final_url_set: set[str] = {url for url, cnt in round_pass_counts.items() if cnt >= min_pass}
    else:
        # 严格模式（默认 min_pass == num_rounds）：必须通过所有轮次
        final_url_set = candidate_urls

    report_results: list[dict[str, Any]] = list(all_round_details.values())
    passed = [round_best_record[url] for url in urls if url in final_url_set]
    for record in passed:
        record.pop("__readoriValidation", None)
    pass_count = len(passed)
    # ───────────────────────────────────────────────────────────────────────────

    passed.sort(key=lambda x: (normalize_book_source_url(str(x.get("bookSourceUrl") or "")), str(x.get("bookSourceName") or "")))
    report_results.sort(key=lambda x: (normalize_book_source_url(str(x.get("bookSourceUrl") or "")), str(x.get("bookSourceName") or "")))

    try:
        json_dir_rel = str(json_dir.relative_to(root_resolved))
    except ValueError:
        json_dir_rel = str(json_dir)
    summary = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "jsonDir": json_dir_rel,
        "inputFiles": input_files,
        "appVersion": app_version,
        "uniqueSourceUrls": len(urls),
        "totalRecordsLoaded": len(sources),
        "passed": pass_count,
        "failed": len(urls) - pass_count,
        "workers": max_workers,
        "abandonedWorkers": abandoned_workers_total(),
        "abandonedWorkersStillRunning": abandoned_workers_alive(),
        "sourceTimeoutSeconds": max(0, args.source_timeout),
        "checks": ["search_or_explore", "book_detail_name_or_tocUrl", "toc_first_chapter", "chapter_content_non_empty"],
    }
    report_doc = {
        "summary": summary,
        "results": report_results,
        "passedSources": passed,
    }
    report_text = json.dumps(report_doc, ensure_ascii=False, indent=2)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    print(f"Wrote validation report ({len(report_results)} entries) to {report_path}", flush=True)
    if mirror_dir is not None:
        try:
            mirror_dir.mkdir(parents=True, exist_ok=True)
            mirror_path = mirror_dir / report_path.name
            mirror_path.write_text(report_text, encoding="utf-8")
            print(f"Mirror report for git: {mirror_path}", flush=True)
        except OSError as exc:
            print(f"Could not write reports mirror: {exc}", flush=True)

    if not args.report_only:
        payload = json.dumps(passed, ensure_ascii=False, indent=2)
        validated_output.parent.mkdir(parents=True, exist_ok=True)
        validated_output_full.parent.mkdir(parents=True, exist_ok=True)
        validated_output.write_text(payload, encoding="utf-8")
        validated_output_full.write_text(payload, encoding="utf-8")
        print(f"Wrote {len(passed)} validated sources to {validated_output}", flush=True)
        print(f"Wrote {len(passed)} validated sources to {validated_output_full}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
