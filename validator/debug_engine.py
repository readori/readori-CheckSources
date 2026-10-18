"""R146 book-source debug engine: single-step debugging, single-source re-validation, before/after comparison.

* ``validate_one`` - the batch pipeline (quick scan -> full chain -> optional stability rounds) for exactly one
  source, with the same timeouts, App-profile handling and verdict logic as ``_run_staged_stages``.
* ``debug_source`` - Legado / App style debug run for one key (keyword, discovery URL, detail / toc / content URL, or
  empty = the full validation chain) that streams events and keeps every raw response.
* ``compare_sources`` - run ``validate_one`` on the original and the edited source, one after the other.

Every event goes through ``DebugSession.sink``: it numbers the event, moves response bodies into the ``RawStore``,
decides the ``state`` (1 normal, -1 failed end, 1000 success end, 10/20/30/40 = a response of search / info / toc /
content was saved), and attaches the human readable ``text`` (``render_event``; every front end only shows ``text``).
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urljoin

try:  # package import (CLI / PyInstaller) or top-level import (scripts / tests)
    from . import app_profiles, debug_trace, rule_tester  # type: ignore[attr-defined]
    from . import validate_source_packages as vsp  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised only outside the package
    import app_profiles  # type: ignore[no-redef]
    import debug_trace  # type: ignore[no-redef]
    import rule_tester  # type: ignore[no-redef]
    import validate_source_packages as vsp  # type: ignore[no-redef]

log = logging.getLogger(__name__)

DebugCancelled = debug_trace.DebugCancelled
DEFAULT_APP_VERSION = app_profiles.DEFAULT_APP_VERSION

VALUE_LIMIT = 2000
PICK_LIST_LIMIT = 50
PAUSE_WAIT_SECONDS = 300
STAGE_STATE = {"search": 10, "explore": 10, "info": 20, "toc": 30, "content": 40}

FIELD_ZH = {
    "bookList": "书籍列表", "name": "书名", "author": "作者", "kind": "分类", "wordCount": "字数",
    "lastChapter": "最新章节", "intro": "简介", "coverUrl": "封面链接", "bookUrl": "详情页链接",
    "init": "详情页初始化", "tocUrl": "目录链接", "chapterList": "目录列表", "chapterName": "章节名称",
    "chapterUrl": "章节链接", "nextTocUrl": "目录下一页", "content": "正文内容", "title": "章节名称（正文）",
    "nextContentUrl": "正文下一页", "replaceRegex": "正文净化", "webJs": "webJs",
    "updateTime": "更新时间", "isVolume": "是否卷名", "isVip": "是否 VIP", "isPay": "是否付费",
}
VERDICT_ZH = {"pass": "可用", "invalid": "失效", "needsInteraction": "需要登录/验证", "timeout": "超时"}
KEY_KIND_ZH = {"auto": "完整校验", "search": "关键词", "explore": "发现", "info": "详情", "toc": "目录", "content": "正文"}
STAGE_ZH = {"search": "搜索页", "explore": "发现页", "info": "详情页", "toc": "目录页", "content": "正文页"}

_FAILURE_GROUPS: list[tuple[str, str]] = [
    ("no search/explore result", "搜索失效"),
    ("detail failed", "网站失效"),
    ("toc failed", "目录失效"),
    ("content failed", "正文失效"),
    ("content rule empty", "正文失效"),
    ("js error", "js失效"),
    ("webview/paid content", "需要登录/验证"),
    ("interactive verification required", "需要登录/验证"),
    ("app import rejects source", "App 导入拒绝"),
    ("missing search/explore rule", "搜索链接规则为空"),
    ("connectivity failed", "网站失效"),
    ("invalid base url", "书源地址无效"),
    ("disabled", "书源已禁用"),
    ("download-only source", "文件类书源"),
    ("worker exception", "校验异常"),
    ("exception", "校验异常"),
]


def failure_group_zh(reason: str, detail: dict[str, Any] | None = None) -> str:
    """Chinese failure group of a batch ``failureReason`` (the App's group names)."""
    detail = detail or {}
    if detail.get("failureGroup"):
        return str(detail["failureGroup"])
    text = str(reason or "").strip()
    if not text:
        return ""
    low = text.lower()
    if "timeout" in low:
        return "校验超时"
    if low.startswith("network error"):
        return "网站失效"
    for needle, group in _FAILURE_GROUPS:
        if low == needle or needle in low:
            return group
    return ""


# ---------------------------------------------------------------------------
# Debug key
# ---------------------------------------------------------------------------

@dataclass
class DebugKey:
    kind: Literal["auto", "search", "explore", "info", "toc", "content"]
    value: str
    title: str = ""


def parse_debug_key(raw: str, base_url: str) -> DebugKey:
    """Same decision order as the App's ``BookSourceDebugInput.parse`` / Legado ``Debug.startDebug``.

    empty -> auto; absolute http(s) URL -> detail page; ``title::url`` -> discovery; ``++url`` -> toc; ``--url`` ->
    content; anything else is a search keyword.  Relative URLs after ``::`` / ``++`` / ``--`` are joined with
    ``base_url``.  A ``::`` / ``++`` / ``--`` without a URL raises ``ValueError``.
    """
    key = (raw or "").strip()
    if not key:
        return DebugKey("auto", "")
    lower = key.lower()
    if lower.startswith(("http://", "https://")):
        return DebugKey("info", key)
    if "::" in key:
        title, _, url = key.partition("::")
        url = url.strip()
        if not url:
            raise ValueError("发现调试的格式是「标题::链接」，链接不能为空")
        return DebugKey("explore", urljoin(base_url, url) if base_url else url, title.strip())
    if key.startswith("++"):
        url = key[2:].strip()
        if not url:
            raise ValueError("目录调试的格式是「++目录链接」，链接不能为空")
        return DebugKey("toc", urljoin(base_url, url) if base_url else url)
    if key.startswith("--"):
        url = key[2:].strip()
        if not url:
            raise ValueError("正文调试的格式是「--正文链接」，链接不能为空")
        return DebugKey("content", urljoin(base_url, url) if base_url else url)
    return DebugKey("search", key)


# ---------------------------------------------------------------------------
# Raw responses
# ---------------------------------------------------------------------------

@dataclass
class RawEntry:
    raw_id: int
    stage: str
    url: str
    final_url: str
    method: str
    status: int
    request_headers: dict[str, str]
    request_body: str | None
    response_headers: dict[str, str]
    history: list[Any]
    charset: str
    body: str
    size: int
    truncated: bool
    elapsed_ms: int
    attempt: Any = 0
    binary: bool = False

    def to_dict(self, *, include_body: bool = True, max_bytes: int | None = None) -> dict[str, Any]:
        out = {
            "raw_id": self.raw_id, "stage": self.stage, "url": self.url, "final_url": self.final_url,
            "method": self.method, "status": self.status, "request_headers": dict(self.request_headers),
            "request_body": self.request_body, "response_headers": dict(self.response_headers),
            "history": [list(item) if isinstance(item, (tuple, list)) else item for item in self.history],
            "charset": self.charset, "size": self.size, "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms, "attempt": self.attempt, "binary": self.binary,
        }
        if include_body:
            body = self.body
            truncated = self.truncated
            if max_bytes is not None:
                body, cut = _truncate_bytes(body, max_bytes)
                truncated = truncated or cut
                out["truncated"] = truncated
            out["body"] = body
        return out


def _truncate_bytes(text: str, limit: int) -> tuple[str, bool]:
    if limit < 0:
        limit = 0
    if len(text) * 4 <= limit:  # cannot exceed the limit even at 4 bytes per character
        return text, False
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


class RawStore:
    """Saved responses of one debug run.  Bodies are cut at ``per_entry_limit`` bytes, the whole store at
    ``total_limit`` bytes and ``max_entries`` entries (the entry itself is still listed, ``truncated=True``)."""

    def __init__(self, *, per_entry_limit: int = 5 * 1024 * 1024, total_limit: int = 64 * 1024 * 1024,
                 max_entries: int | None = None) -> None:
        self.per_entry_limit = per_entry_limit
        self.total_limit = total_limit
        self.max_entries = max_entries
        self.entries: list[RawEntry] = []
        self.total_bytes = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self.entries)

    def add(self, *, stage: str, url: str, final_url: str = "", method: str = "GET", status: int = 0,
            request_headers: dict | None = None, request_body: str | None = None,
            response_headers: dict | None = None, history: list | None = None, charset: str = "",
            body: str | None = None, size: int | None = None, elapsed_ms: int = 0, attempt: Any = 0,
            binary: bool = False) -> RawEntry:
        text = body if isinstance(body, str) else ""
        real_size = size if size is not None else len(text.encode("utf-8", errors="replace"))
        with self._lock:
            truncated = False
            if self.max_entries is not None and len(self.entries) >= self.max_entries:
                text, truncated = "", bool(real_size)
            else:
                text, cut = _truncate_bytes(text, self.per_entry_limit)
                truncated = cut
                room = max(0, self.total_limit - self.total_bytes)
                text, cut = _truncate_bytes(text, room)
                truncated = truncated or cut
            self.total_bytes += len(text.encode("utf-8", errors="replace")) if text else 0
            entry = RawEntry(
                raw_id=len(self.entries) + 1, stage=stage, url=url, final_url=final_url or url, method=method,
                status=int(status or 0), request_headers=dict(request_headers or {}), request_body=request_body,
                response_headers=dict(response_headers or {}),
                history=[tuple(item) if isinstance(item, list) else item for item in (history or [])],
                charset=charset or "", body=text, size=int(real_size), truncated=truncated,
                elapsed_ms=int(elapsed_ms or 0), attempt=attempt, binary=binary,
            )
            self.entries.append(entry)
        return entry

    def get(self, raw_id: Any) -> RawEntry | None:
        try:
            wanted = int(raw_id)
        except (TypeError, ValueError):
            return None
        for entry in self.entries:
            if entry.raw_id == wanted:
                return entry
        return None

    def latest(self, stage: str) -> RawEntry | None:
        """Last successful (200-399, non-empty body) response saved for ``stage``."""
        for entry in reversed(list(self.entries)):
            if entry.stage == stage and 200 <= entry.status < 400 and entry.body:
                return entry
        return None

    def summaries(self) -> list[dict[str, Any]]:
        return [entry.to_dict(include_body=False) for entry in list(self.entries)]

    def save(self, directory: str | Path) -> Path:
        """``index.json`` + one body file per entry (``001-search.html`` ...)."""
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        index: list[dict[str, Any]] = []
        for entry in list(self.entries):
            row = entry.to_dict(include_body=False)
            row["file"] = ""
            if entry.body:
                head = entry.body.lstrip()[:1]
                ext = "json" if head in ("{", "[") else ("html" if head == "<" else "txt")
                row["file"] = f"{entry.raw_id:03d}-{entry.stage or 'x'}.{ext}"
                (target / row["file"]).write_text(entry.body, encoding="utf-8", newline="")
            index.append(row)
        (target / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, directory: str | Path, **limits: Any) -> "RawStore":
        source = Path(directory)
        index = json.loads((source / "index.json").read_text(encoding="utf-8"))
        store = cls(per_entry_limit=1 << 62, total_limit=1 << 62, **limits)
        for row in index:
            body = ""
            if row.get("file"):
                body = (source / row["file"]).read_text(encoding="utf-8")
            entry = store.add(
                stage=row.get("stage", ""), url=row.get("url", ""), final_url=row.get("final_url", ""),
                method=row.get("method", "GET"), status=row.get("status", 0),
                request_headers=row.get("request_headers"), request_body=row.get("request_body"),
                response_headers=row.get("response_headers"), history=row.get("history"),
                charset=row.get("charset", ""), body=body, size=row.get("size"), elapsed_ms=row.get("elapsed_ms", 0),
                attempt=row.get("attempt", 0), binary=bool(row.get("binary")),
            )
            entry.raw_id = int(row.get("raw_id") or entry.raw_id)
            entry.truncated = bool(row.get("truncated"))
        return store


# ---------------------------------------------------------------------------
# Controller (pause / resume / cancel; thread safe)
# ---------------------------------------------------------------------------

class DebugController:
    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._cond = threading.Condition()
        self._choice: int | None = None
        self.waiting = False

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()
        with self._cond:
            self._cond.notify_all()

    def resume(self, choice: int = 0) -> None:
        with self._cond:
            self._choice = int(choice)
            self._cond.notify_all()

    def wait_choice(self, timeout: float = PAUSE_WAIT_SECONDS) -> int | None:
        """Block until ``resume(choice)``; ``None`` on timeout; raises ``DebugCancelled`` after ``cancel()``."""
        deadline = time.monotonic() + timeout
        with self._cond:
            self.waiting = True
            try:
                while self._choice is None and not self._cancel.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(min(remaining, 0.5))
                if self._cancel.is_set():
                    raise DebugCancelled()
                choice, self._choice = self._choice, None
                return choice
            finally:
                self.waiting = False


# ---------------------------------------------------------------------------
# Event rendering
# ---------------------------------------------------------------------------

def _stamp(t: Any) -> str:
    try:
        seconds = max(0.0, float(t or 0.0))
    except (TypeError, ValueError):
        seconds = 0.0
    return f"[{int(seconds // 60):02d}:{seconds % 60:06.3f}]"


def _one_line(value: Any, limit: int = 300) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.replace("\r", "\n").split("\n")).strip()
    if not text:
        return "∅"
    return text if len(text) <= limit else text[:limit] + "…"


def _fmt_size(size: Any) -> str:
    try:
        number = float(size or 0)
    except (TypeError, ValueError):
        return "-"
    if number < 1024:
        return f"{int(number)}B"
    if number < 1024 * 1024:
        return f"{number / 1024:.1f}KB"
    return f"{number / 1024 / 1024:.1f}MB"


def render_header(src: dict[str, Any], version: str, key: DebugKey | None) -> str:
    name = str(src.get("bookSourceName") or "")
    url = str(src.get("bookSourceUrl") or "")
    text = f"书源：{name}（{url}） · App {version}"
    if key is None or key.kind == "auto":
        return text + " · 完整校验"
    return text + f" · {KEY_KIND_ZH.get(key.kind, key.kind)} {key.value}"


_WARN_TEXT = {
    "landing": "⚠ 响应被判为站点首页 / 搜索落地页：{url}（常见于被重定向回首页）",
    "login_check": "⚠ loginCheckJs 没有返回有效响应：{url}",
    "empty_json": "⚠ 响应是「没有结果」的 JSON：{url}",
    "challenge": "⚠ 页面疑似人机验证 / 拦截页，需要在 App 内用网页过盾（需交互）：{url}",
    "http_error": "⚠ HTTP {status} 在 App 里算请求失败，规则拿不到这个响应：{url}",
    "no_request": "⚠ 搜索链接规则没有生成请求（关键词 {value}）",
    "webview_deferred": "⚠ 正文页要靠浏览器渲染后才有内容：{url}",
    "content_error_json": "⚠ 正文接口返回了错误 JSON：{url}",
    "access_gated": "⚠ 正文只是「请升级 App / 需要权限」之类的提示：{url}",
}


def render_event(event: dict[str, Any], session: Any = None) -> str:
    """Human readable text of one event (every line prefixed with ``[mm:ss.SSS]``; header has no prefix)."""
    kind = event.get("kind")
    data = event.get("data") or {}
    stage = event.get("stage") or ""
    url = event.get("url") or ""
    value = event.get("value")
    lines: list[str] = []

    if kind == "info" and data.get("code") == "header":
        return str(value or "")
    if kind == "begin":
        zh = STAGE_ZH.get(stage, "")
        if stage == "quick-scan":
            lines = ["▶ 快速扫描（连通性 + 搜索入口）"]
        elif stage == "full-validation":
            lines = ["▶ 完整校验（搜索 → 详情 → 目录 → 正文）"]
        elif stage == "explore" and url:
            lines = [f"⇒开始访问发现页:{url}"]
        elif stage == "info":
            lines = [f"⇒开始访问详情页:{url}" if url else "⇒开始解析详情页"]
        elif stage == "toc":
            lines = [f"⇒开始访目录页:{url}" if url else "⇒开始解析目录页"]
        elif stage == "content":
            lines = [f"⇒开始访正文页:{url}" if url else "⇒开始解析正文页"]
        elif zh:
            lines = []
    elif kind == "keyword":
        lines = [f"⇒开始搜索关键字:{value}"]
    elif kind == "fetch":
        status = int(event.get("status") or 0)
        method = str(event.get("method") or "GET")
        meta = f"{status} · {event.get('elapsed_ms') or 0}ms · {_fmt_size(data.get('size'))} · {data.get('charset') or '-'}"
        label = "获取成功" if 200 <= status < 400 else f"请求失败(HTTP {status})"
        prefix = "" if method == "GET" else f"[{method}] "
        if data.get("attempt") == "verify":
            prefix += "(验证页跳转) "
        lines = [f"≡{prefix}{label}:{url} ({meta})"]
        history = data.get("history") or []
        if history:
            first = history[0]
            first_status = first[0] if isinstance(first, (list, tuple)) else (first.get("status") if isinstance(first, dict) else "")
            lines.append(f"≡检测到重定向({first_status}) ┌重定向后地址 └{data.get('final_url') or ''}")
    elif kind == "fetch_error":
        lines = [f"✗请求失败:{url} — {data.get('error') or value or ''}"]
    elif kind == "blocked":
        lines = [f"✗离线重放：已拦截网络请求 {url}"]
    elif kind == "list":
        name = event.get("field") or "bookList"
        zh = "目录列表" if name == "chapterList" else FIELD_ZH.get(name, name)
        rule = str(event.get("rule") or "")
        page = data.get("page")
        head = f"┌获取{zh}" + (f"（第 {page} 页）" if page and page != 1 else "") + (f"  ‹{rule[:120]}›" if rule else "")
        lines = [head, f"└列表大小:{event.get('count') if event.get('count') is not None else 0}"]
    elif kind == "field":
        name = str(event.get("field") or "")
        zh = FIELD_ZH.get(name, name)
        rule = str(event.get("rule") or "")
        head = ("┌(补充)" if event.get("extra") else "┌") + f"获取{zh}" + (f"  ‹{rule[:120]}›" if rule else "")
        tail = _one_line(value)
        if name == "content" and event.get("count") is not None:
            tail = f"（共 {event.get('count')} 字）{tail}"
        if name == "replaceRegex" and data.get("before") is not None:
            tail = f"（{data.get('before')} → {data.get('after')} 字）{tail}"
        lines = [head, "└" + tail]
    elif kind == "item":
        label = "章节" if stage == "toc" else "书籍"
        index = data.get("index")
        lines = [f"  ▸{label}#{(int(index) + 1) if isinstance(index, int) else ''} {data.get('name') or ''}  {_one_line(value, 200)}"]
    elif kind == "page":
        zh = "目录" if stage == "toc" else "正文"
        count = event.get("count")
        lines = [f"︾{zh}第 {data.get('index')} 页解析完成:{url}" + (f"（{count} 项）" if count is not None else "")]
    elif kind == "fallback":
        what = data.get("what") or ""
        extra = f"（{_one_line(value, 200)}）" if value not in (None, "") else ""
        lines = [f"⇒兜底:{what}{extra}"]
    elif kind == "end":
        ok = bool(data.get("ok"))
        ms = event.get("elapsed_ms")
        timing = f"（{ms} ms）" if ms is not None else ""
        if stage == "quick-scan":
            lines = [("✓ 快速扫描通过" if ok else f"✗ 快速扫描未通过：{data.get('failureReason') or value or ''}") + timing]
        elif stage == "full-validation":
            lines = [("✓ 完整校验通过" if ok else f"✗ 完整校验未通过：{data.get('failureReason') or value or ''}") + timing]
        else:
            zh = STAGE_ZH.get(stage, stage)
            if stage in ("search", "explore") and event.get("count") is not None:
                lines = [f"◇书籍总数:{event.get('count')}"]
            elif stage == "toc" and event.get("count") is not None:
                lines = [f"◇目录总数:{event.get('count')}"]
            lines.append(f"︽{zh}解析完成{timing}" if ok else f"︽{zh}解析未通过{timing}")
    elif kind in ("info", "warn"):
        code = str(data.get("code") or event.get("code") or "")
        template = _WARN_TEXT.get(code)
        if template:
            lines = [template.format(url=url, status=event.get("status") or "", value=value or "")]
        elif code == "detail_page":
            lines = [f"⇒搜索直接命中详情页:{url}"]
        else:
            mark = "⚠ " if kind == "warn" else "ⓘ "
            lines = [mark + str(value or code or "")]
    elif kind == "error":
        lines = [f"✗ {value or ''}"]
    elif kind == "log":
        lines = [f"[log] {_one_line(value, 500)}"]
    elif kind == "pause":
        choices = data.get("choices") or []
        label = "章节" if data.get("what") == "chapter" else "书籍"
        lines = [f"⏸ 请选择要继续调试的{label}（共 {data.get('total', len(choices))} 项，显示前 {len(choices)} 项）"]
        for choice in choices[:10]:
            lines.append(f"   [{choice.get('index')}] {choice.get('name') or ''}  {choice.get('url') or ''}")
    elif kind == "verdict":
        ok = event.get("state") == 1000
        if data.get("scope") == "validation":
            version = data.get("version") or ""
            if ok:
                lines = [f"✓ 校验结论：可用 · App {version}"]
            else:
                reason = data.get("failureReason") or ""
                group = data.get("failureGroupZh") or ""
                zh = data.get("verdictZh") or "失效"
                tail = f" · {reason}" + (f"（{group}）" if group else "") if reason else ""
                lines = [f"✗ 校验结论：{zh}{tail}"]
        elif data.get("scope") == "cancel":
            lines = ["■ 已停止"]
        else:
            text = str(value or "")
            lines = [text if text.startswith("︽") else (("✓ " if ok else "✗ ") + text)]
    elif value not in (None, ""):
        lines = [str(value)]

    if not lines:
        return ""
    stamp = _stamp(event.get("t"))
    return "\n".join(f"{stamp} {line}" for line in lines)


# ---------------------------------------------------------------------------
# Session: numbering, raw storage, state, text
# ---------------------------------------------------------------------------

_CORE_KEYS = ("kind", "stage", "field", "rule", "value", "count", "url", "method", "status", "elapsed_ms", "state",
              "raw_id", "extra", "data")
_KEEP_WHEN_FULL = {"fetch", "fetch_error", "begin", "end", "error", "verdict", "blocked", "pause"}


class DebugSession:
    """Everything a debug run produced: ``events``, ``raw`` (``RawStore``), ``variables`` snapshot."""

    def __init__(
        self,
        *,
        on_event: Callable[[dict], None] | None = None,
        controller: DebugController | None = None,
        max_events: int = 20000,
        raw: RawStore | None = None,
        app_version: str = DEFAULT_APP_VERSION,
    ) -> None:
        self.on_event = on_event
        self.controller = controller
        self.max_events = max_events
        self.raw = raw if raw is not None else RawStore()
        self.app_version = app_version
        self.events: list[dict[str, Any]] = []
        self.variables: dict[str, str] = {}
        self.key: DebugKey | None = None
        self.source_name = ""
        self.started = time.perf_counter()
        self.dropped = 0
        self.closing = False
        self._seq = 0
        self._lock = threading.Lock()
        self._stage_started: dict[str, float] = {}
        self._item_counts: dict[str, int] = {}

    @property
    def raw_store(self) -> RawStore:
        return self.raw

    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    def sink(self, event: dict[str, Any]) -> None:
        kind = str(event.get("kind") or "")
        stage = str(event.get("stage") or "")
        with self._lock:
            hard_cap = self.max_events + 500
            full = len(self.events) >= self.max_events
            if len(self.events) >= hard_cap or (full and kind not in _KEEP_WHEN_FULL):
                self.dropped += 1
                dropped_now = self.dropped
            else:
                dropped_now = 0
            self._seq += 1
            seq = self._seq
        if dropped_now:
            if dropped_now == 1:
                self._append({"kind": "warn", "stage": stage, "value": "事件太多，后续字段 / 条目事件已省略（请求和结论仍会保留）",
                              "data": {"code": "events_truncated"}}, seq)
            self._check_cancel()
            return
        self._append(event, seq)
        self._check_cancel()

    def _check_cancel(self) -> None:
        if not self.closing and self.controller is not None and self.controller.cancelled:
            raise DebugCancelled()

    def _append(self, event: dict[str, Any], seq: int) -> None:
        now = self.elapsed()
        kind = str(event.get("kind") or "")
        stage = str(event.get("stage") or "")
        data = dict(event.get("data") or {})
        for name, item in event.items():
            if name not in _CORE_KEYS:
                data[name] = item
        raw_id = event.get("raw_id")
        state = event.get("state")
        elapsed_ms = event.get("elapsed_ms")
        if kind == "fetch":
            body = data.pop("body", None)
            entry = self.raw.add(
                stage=stage, url=str(event.get("url") or ""), final_url=str(data.get("final_url") or ""),
                method=str(event.get("method") or "GET"), status=int(event.get("status") or 0),
                request_headers=data.get("request_headers"), request_body=data.get("request_body"),
                response_headers=data.get("response_headers"), history=data.get("history"),
                charset=str(data.get("charset") or ""), body=body, size=data.get("size"),
                elapsed_ms=int(elapsed_ms or 0), attempt=data.get("attempt", 0), binary=bool(data.get("binary")),
            )
            raw_id = entry.raw_id
            data["truncated"] = entry.truncated
            data["size"] = entry.size
            if state is None:
                state = STAGE_STATE.get(stage, 1)
        if kind == "begin":
            self._stage_started[stage] = now
        elif kind == "end" and elapsed_ms is None and stage in self._stage_started:
            elapsed_ms = int((now - self._stage_started[stage]) * 1000)
        value = event.get("value")
        if isinstance(value, (list, tuple)):
            value = "\n".join(str(item) for item in value)
        elif value is not None and not isinstance(value, str):
            value = str(value)
        if isinstance(value, str) and len(value) > VALUE_LIMIT:
            value = value[:VALUE_LIMIT] + "…"
        if kind == "end" and "ok" in event:
            data.setdefault("ok", bool(event.get("ok")))
        if kind == "verdict" and state is None:
            state = 1000 if data.get("ok") else -1
        if kind == "item":
            count = self._item_counts.get(stage, 0) + 1
            self._item_counts[stage] = count
        out: dict[str, Any] = {
            "seq": seq, "t": round(now, 3), "stage": stage, "kind": kind,
            "field": event.get("field"), "rule": event.get("rule"), "value": value, "count": event.get("count"),
            "url": event.get("url"), "method": event.get("method"), "status": event.get("status"),
            "elapsed_ms": elapsed_ms, "state": 1 if state is None else state, "raw_id": raw_id,
            "extra": bool(event.get("extra")), "data": data,
        }
        text = render_event(out, self)
        if kind == "item" and self._item_counts.get(stage, 0) > 10:
            text = ""  # the event is kept, but the log only lists the first ten items of a stage
        out["text"] = text
        self.events.append(out)
        if self.on_event is not None:
            try:
                self.on_event(out)
            except DebugCancelled:
                if not self.closing:
                    raise
            except Exception:  # noqa: BLE001 - a broken listener must not stop the run
                log.debug("debug on_event listener failed", exc_info=True)


@dataclass
class DebugResult:
    status: str  # pass | fail | needsInteraction | timeout | cancelled | error
    ok: bool
    state: int
    message: str
    key: DebugKey
    session: DebugSession
    verdict: dict[str, Any] | None = None
    elapsed_ms: int = 0
    book_url: str = ""
    toc_url: str = ""
    chapter_url: str = ""
    chapter_title: str = ""

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status, "ok": self.ok, "state": self.state, "message": self.message,
            "key": {"kind": self.key.kind, "value": self.key.value, "title": self.key.title},
            "elapsedMs": self.elapsed_ms, "bookUrl": self.book_url, "tocUrl": self.toc_url,
            "chapterUrl": self.chapter_url, "chapterTitle": self.chapter_title,
            "rawCount": len(self.session.raw), "eventCount": len(self.session.events),
            "variables": dict(self.session.variables),
        }
        if self.verdict is not None:
            out["verdict"] = {k: v for k, v in self.verdict.items() if k not in ("detail", "record")}
        if include_events:
            out["events"] = list(self.session.events)
        return out


# ---------------------------------------------------------------------------
# validate_one / compare_sources
# ---------------------------------------------------------------------------

def _worker_exception_detail(url: str, candidates: list[dict[str, Any]], stage: str, exc: BaseException) -> dict[str, Any]:
    """What ``run_parallel_stage`` records when a handler raises something else than the hard timeout."""
    detail = vsp._empty_detail({
        "bookSourceName": str(candidates[0].get("bookSourceName") or "") if candidates else "",
        "bookSourceUrl": vsp._group_source_url(url, candidates),
    })
    detail["pipelineStage"] = stage
    detail["failureReason"] = "worker exception"
    detail["error"] = str(exc)
    return detail


def _run_stage(stage: str, seconds: int, url: str, candidates: list[dict[str, Any]],
               handler: Callable[[], tuple[str, dict[str, Any] | None, dict[str, Any]]]
               ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """One pipeline stage with the hard per-source deadline; returns ``(record_or_seed, detail)`` like the batch."""
    vsp.begin_source_validation_deadline(max(0, seconds))
    try:
        _key, record, detail = handler()
        return record, detail
    except vsp.SourceValidationDeadlineExceeded:
        return None, vsp.pipeline_timeout_detail(url, candidates, stage, seconds)
    except Exception as exc:  # noqa: BLE001 - same as the batch worker
        return None, _worker_exception_detail(url, candidates, stage, exc)
    finally:
        vsp.clear_source_validation_deadline()


def _validate_one_core(src: dict[str, Any], *, app_version: str, quick_timeout: int, source_timeout: int,
                       rounds: int, emit_verdict: bool) -> dict[str, Any]:
    url = vsp.normalize_book_source_url(str(src.get("bookSourceUrl") or ""))
    candidates = [src]
    rounds = max(1, int(rounds or 1))
    passed = False
    record: dict[str, Any] | None = None
    pass_count = 0
    with app_profiles.app_profile_scope(app_version):
        vsp.reset_thread_session(src)
        seed, detail = _run_stage("quick-scan", quick_timeout, url, candidates, lambda: vsp.quick_scan_group(url, candidates))
        if seed is not None:
            record, detail = _run_stage(
                "full-validation", source_timeout, url, candidates,
                lambda: vsp.validate_group(url, candidates, quick_seed=seed),
            )
            passed = record is not None
            pass_count = 1 if passed else 0
            round_num = 2
            while passed and round_num <= rounds:
                # Stability rounds do a fresh search and do not use the quick seed (same as _run_staged_stages).
                vsp.reset_thread_session(src)
                round_record, detail = _run_stage(
                    f"stability-{round_num}", source_timeout, url, candidates, lambda: vsp.validate_group(url, candidates),
                )
                if round_record is None:
                    passed = False
                    break
                record = round_record
                pass_count += 1
                round_num += 1
        detail["stabilityPassCount"] = pass_count
        verdict = "pass" if passed else app_profiles.app_verdict(detail, False, app_version, src)
        reason = "" if passed else str(detail.get("failureReason") or "")
    out_record = None
    if record is not None:
        out_record = {k: v for k, v in record.items() if k != "__readoriValidation"}
    result = {
        "verdict": verdict,
        "verdictZh": VERDICT_ZH.get(verdict, verdict),
        "passed": passed,
        "failureReason": reason,
        "failureGroupZh": "" if passed else failure_group_zh(reason, detail),
        "stages": dict(detail.get("stages") or {}),
        "responseTimeMs": int(detail.get("responseTimeMs") or 0),
        "stabilityPassCount": pass_count,
        "appVersion": app_version,
        "detail": detail,
        "record": out_record,
    }
    if emit_verdict and debug_trace.active():
        debug_trace.set_stage("")
        debug_trace.emit(
            "verdict", state=1000 if passed else -1,
            value=("校验通过" if passed else f"{result['verdictZh']}：{reason}"),
            data={"scope": "validation", "verdict": verdict, "verdictZh": result["verdictZh"], "version": app_version,
                  "failureReason": reason, "failureGroupZh": result["failureGroupZh"], "stages": result["stages"],
                  "responseTimeMs": result["responseTimeMs"], "ok": passed},
        )
    return result


def validate_one(src: dict[str, Any], *, app_version: str = DEFAULT_APP_VERSION, quick_timeout: int = 8,
                 source_timeout: int = 60, rounds: int = 1, trace: DebugSession | None = None) -> dict[str, Any]:
    """The batch pipeline for exactly one source: quick scan -> full chain (seeded) -> optional stability rounds.

    Same stage order, per-stage hard timeouts and ``app_verdict`` as ``_run_staged_stages``.  With ``trace`` the run
    is recorded in that ``DebugSession`` and ends with a ``verdict`` event."""
    if trace is None:
        return _validate_one_core(src, app_version=app_version, quick_timeout=quick_timeout,
                                  source_timeout=source_timeout, rounds=rounds, emit_verdict=False)
    trace.app_version = app_version
    with debug_trace.tracing(trace.sink):
        if not any(e.get("kind") == "info" and (e.get("data") or {}).get("code") == "header" for e in trace.events):
            _emit_header(trace, src, app_version, trace.key or DebugKey("auto", ""))
        return _validate_one_core(src, app_version=app_version, quick_timeout=quick_timeout,
                                  source_timeout=source_timeout, rounds=rounds, emit_verdict=True)


_COMPARE_ROWS = (("quick-scan", "快速扫描"), ("search", "搜索"), ("detail", "详情"), ("toc", "目录"),
                 ("content", "正文"), ("verdict", "结论"))


def _stage_flags(result: dict[str, Any]) -> dict[str, bool]:
    detail = result.get("detail") or {}
    quick = detail.get("quickScan") or {}
    stages = result.get("stages") or {}
    return {
        "quick-scan": bool(quick.get("connectivity") and quick.get("searchEntry")),
        "search": bool(stages.get("search")),
        "detail": bool(stages.get("detail")),
        "toc": bool(stages.get("toc")),
        "content": bool(stages.get("content")),
        "verdict": bool(result.get("passed")),
    }


def compare_sources(original: dict[str, Any], working: dict[str, Any], **validate_kwargs: Any) -> dict[str, Any]:
    """Validate ``original`` then ``working`` (one after the other: no parallel hits on the same site)."""
    before = validate_one(original, **validate_kwargs)
    after = validate_one(working, **validate_kwargs)
    flags_before, flags_after = _stage_flags(before), _stage_flags(after)
    rows = []
    for stage, zh in _COMPARE_ROWS:
        b, a = flags_before[stage], flags_after[stage]
        rows.append({"stage": stage, "label": zh, "before": b, "after": a,
                     "change": "improved" if (a and not b) else ("regressed" if (b and not a) else "same")})
    return {
        "before": before, "after": after, "rows": rows,
        "elapsedMs": {"before": before["responseTimeMs"], "after": after["responseTimeMs"]},
        "changed": before["verdict"] != after["verdict"] or any(r["change"] != "same" for r in rows),
    }


# ---------------------------------------------------------------------------
# debug_source
# ---------------------------------------------------------------------------

class _Stop(Exception):
    """Internal: end the run with an already reported verdict."""

    def __init__(self, status: str, ok: bool, message: str) -> None:
        super().__init__(message)
        self.status, self.ok, self.message = status, ok, message


def _emit_header(session: DebugSession, src: dict[str, Any], version: str, key: DebugKey) -> None:
    session.source_name = str(src.get("bookSourceName") or "")
    debug_trace.set_stage("")
    debug_trace.emit("info", value=render_header(src, version, key), data={"code": "header"}, code="header")


def _items_of(session: DebugSession, stages: tuple[str, ...]) -> list[dict[str, str]]:
    out = []
    for event in session.events:
        if event.get("kind") == "item" and event.get("stage") in stages:
            data = event.get("data") or {}
            out.append({"index": int(data.get("index", len(out))), "name": str(data.get("name") or ""),
                        "url": str(event.get("value") or "")})
    return out


def _pause_for_choice(ctx: "_Ctx", what: str, choices: list[dict[str, Any]], total: int) -> int:
    """Step mode: announce the candidates, wait for ``resume(choice)``; returns the picked index."""
    controller = ctx.session.controller
    debug_trace.emit("pause", data={"what": what, "choices": choices[:PICK_LIST_LIMIT], "total": total})
    started = time.monotonic()
    choice = controller.wait_choice(PAUSE_WAIT_SECONDS)  # type: ignore[union-attr]
    paused = time.monotonic() - started
    deadline = getattr(vsp._thread_local, "source_validation_deadline", None)
    if deadline is not None:  # time spent waiting for the user does not count against the hard timeout
        vsp._thread_local.source_validation_deadline = deadline + paused
    if choice is None:
        raise _Stop("cancelled", False, "等待选择超时（300 秒），已停止")
    return choice


@dataclass
class _Ctx:
    view: dict[str, Any]
    original: dict[str, Any]
    runtime: Any
    http: Any
    session: DebugSession
    key: DebugKey
    version: str
    step: bool
    pick_book: int
    pick_chapter: int
    chain: bool
    result: DebugResult


def _sync_variables(ctx: _Ctx) -> None:
    ctx.session.variables = {str(k): str(v) for k, v in (ctx.runtime.variables or {}).items()}


def _flush_script_errors(ctx: _Ctx, sink: list, stage: str) -> None:
    vsp.finish_script_error_stage(ctx.runtime, sink, {}, stage)  # raises RuleScriptError on a genuine error


def _extra_fields(ctx: _Ctx, stage: str, specs: list[tuple[str, int]]) -> None:
    """Display-only fields the validation chain never reads, evaluated offline on the saved response."""
    store = ctx.session.raw
    entry = store.latest(stage)
    if entry is None:
        return
    base_url = entry.final_url or entry.url
    for path, item_index in specs:
        rule = rule_tester.get_path(ctx.view, path)
        if path.startswith("ruleExplore.") and not str(rule_tester.get_path(ctx.view, "ruleExplore.bookList") or "").strip():
            rule = rule_tester.get_path(ctx.view, path.replace("ruleExplore.", "ruleSearch.", 1))
        if not isinstance(rule, str) or not rule.strip():
            continue
        result = rule_tester.evaluate_field(
            ctx.view, path, body=entry.body, base_url=base_url, item_index=item_index,
            variables=dict(ctx.runtime.variables or {}), app_version=ctx.version,
        )
        debug_trace.set_stage(stage)
        debug_trace.emit("field", field=path.rsplit(".", 1)[1], rule=rule, value=result.value or (result.error if not result.ok else ""),
                         extra=True)


def _kind_specs(prefix: str) -> list[tuple[str, int]]:
    return [(f"{prefix}.{name}", 0) for name in ("author", "kind", "wordCount", "lastChapter", "intro", "coverUrl")]


def _stage_search(ctx: _Ctx) -> list[str]:
    key = ctx.key
    kind = key.kind
    view = ctx.view
    if kind == "search":
        rule_ok = bool(str(view.get("searchUrl") or "").strip()) and bool(str((view.get("ruleSearch") or {}).get("bookList") or "").strip())
        if not rule_ok:
            raise _Stop("fail", False, "搜索链接规则为空（searchUrl 或 ruleSearch.bookList 缺失）")
    else:
        explore_rule = view.get("ruleExplore") if isinstance(view.get("ruleExplore"), dict) else {}
        search_rule = view.get("ruleSearch") if isinstance(view.get("ruleSearch"), dict) else {}
        if not str(explore_rule.get("bookList") or search_rule.get("bookList") or "").strip():
            raise _Stop("fail", False, "发现规则为空（ruleExplore.bookList 与 ruleSearch.bookList 都缺失）")
    vsp.ensure_source_validation_time_remaining()
    debug_trace.set_stage(kind)
    debug_trace.emit("begin", url=key.value if kind == "explore" else None, data={"scope": "debug", "key": key.value})
    sink = vsp.begin_script_error_stage(ctx.runtime)
    if kind == "search":
        urls, _base = vsp.fetch_book_candidates(ctx.http, view, ctx.runtime, keywords=[key.value], modes=("search",))
    else:
        urls, _base = vsp.fetch_book_candidates(ctx.http, view, ctx.runtime, modes=("explore",), explore_url=key.value)
    ctx.runtime.script_error_sink = None
    _sync_variables(ctx)
    _flush_script_errors(ctx, sink, kind)
    debug_trace.set_stage(kind)
    debug_trace.emit("end", ok=bool(urls), count=len(urls))
    if not urls:
        challenge = (ctx.runtime.variables or {}).get("__searchChallenge") == "1"
        raise _Stop("needsInteraction" if challenge else "fail", False,
                    "︽搜索页疑似人机验证 / 拦截页（需要 App 内网页过盾）" if challenge else "︽未获取到书籍")
    prefix = "ruleSearch" if kind == "search" else "ruleExplore"
    _extra_fields(ctx, kind, _kind_specs(prefix))
    return urls


def _stage_info(ctx: _Ctx, book_url: str) -> tuple[str, str]:
    view, runtime = ctx.view, ctx.runtime
    vsp.ensure_source_validation_time_remaining()
    debug_trace.set_stage("info")
    debug_trace.emit("begin", url=book_url, data={"scope": "debug"})
    sink = vsp.begin_script_error_stage(runtime)
    ok, name, toc_url, error = vsp.fetch_book_info(ctx.http, view, runtime, book_url)
    runtime.script_error_sink = None
    _sync_variables(ctx)
    _flush_script_errors(ctx, sink, "detail")
    debug_trace.set_stage("info")
    debug_trace.emit("end", ok=bool(ok), value=name or "")
    if not ok:
        raise _Stop("fail", False, f"︽详情页解析失败：{error}" if error else "︽详情页解析失败：书名与目录链接都没有取到")
    runtime.book_name = name
    runtime.book_url = book_url
    _extra_fields(ctx, "info", [(f"ruleBookInfo.{n}", 0) for n in ("author", "intro", "coverUrl", "lastChapter", "wordCount")])
    ctx.result.book_url = book_url
    ctx.result.toc_url = (toc_url or "").strip()
    return name, (toc_url or "").strip()


def _stage_toc(ctx: _Ctx, book_url: str, toc_url: str) -> tuple[str, str]:
    runtime = ctx.runtime
    vsp.ensure_source_validation_time_remaining()
    debug_trace.set_stage("toc")
    debug_trace.emit("begin", url=toc_url or book_url, data={"scope": "debug"})
    sink = vsp.begin_script_error_stage(runtime)
    ok, _final, first_url, first_title, pages, _first_count, unique = vsp.resolve_toc_source(
        ctx.http, ctx.view, runtime, book_url, toc_url or book_url)
    runtime.script_error_sink = None
    _sync_variables(ctx)
    _flush_script_errors(ctx, sink, "toc")
    debug_trace.set_stage("toc")
    debug_trace.emit("end", ok=bool(ok), count=unique, value=first_title or "", pages=pages)
    if not ok:
        raise _Stop("fail", False, "︽目录解析失败：没有取到任何章节")
    _extra_fields(ctx, "toc", [(f"ruleToc.{n}", 0) for n in ("updateTime", "isVip", "isPay")])
    chapter_url, chapter_title = first_url, first_title
    chapters = _items_of(ctx.session, ("toc",))
    index = ctx.pick_chapter
    if ctx.step and ctx.session.controller is not None and chapters:
        index = _pause_for_choice(ctx, "chapter", [{"index": c["index"], "name": c["name"], "url": c["url"]} for c in chapters],
                                  len(chapters))
    if index and chapters:
        if 0 <= index < len(chapters):
            chapter_url, chapter_title = chapters[index]["url"], chapters[index]["name"]
        else:
            debug_trace.emit("warn", value=f"章节序号 {index} 超出范围（最多记录 {len(chapters)} 项），改用首章",
                             data={"code": "pick_range"})
    ctx.result.chapter_url, ctx.result.chapter_title = chapter_url, chapter_title
    return chapter_url, chapter_title


def _stage_content(ctx: _Ctx, chapter_url: str, chapter_title: str, book_name: str) -> None:
    runtime = ctx.runtime
    vsp.ensure_source_validation_time_remaining()
    debug_trace.set_stage("content")
    debug_trace.emit("begin", url=chapter_url, data={"scope": "debug"})
    sink = vsp.begin_script_error_stage(runtime)
    ok, preview, pages = vsp.resolve_chapter_content(ctx.http, ctx.view, runtime, chapter_url, chapter_title,
                                                     book_name or str(ctx.view.get("bookSourceName") or ""))
    runtime.script_error_sink = None
    _sync_variables(ctx)
    _flush_script_errors(ctx, sink, "content")
    debug_trace.set_stage("content")
    debug_trace.emit("end", ok=bool(ok), count=len(preview or ""), pages=pages)
    if ok and vsp.is_browser_deferred_content_source(ctx.view):
        raise _Stop("needsInteraction", False, "︽正文依赖浏览器渲染（startBrowser），批量验证判为需要登录/验证")
    if not ok and (runtime.variables or {}).get("__contentRuleEmpty") == "1":
        raise _Stop("fail", False, "︽正文失败：ruleContent.content 为空，App 会把章节链接当正文")
    if not ok:
        gated = (vsp.is_webview_or_paid_content_source(ctx.view, chapter_url)
                 or vsp.is_likely_browser_challenge_preview(preview) or vsp.is_likely_access_gated_content_preview(preview))
        if gated:
            raise _Stop("needsInteraction", False, "︽正文是验证 / 付费 / 升级提示，批量验证判为需要登录/验证")
        raise _Stop("fail", False, "︽正文解析失败：没有取到可读正文")
    raise _Stop("pass", True, f"正文调试完成（{len(preview or '')} 字，{pages} 页）")


def _run_chain(ctx: _Ctx) -> None:
    key, runtime = ctx.key, ctx.runtime
    book_name = ""
    if key.kind in ("search", "explore"):
        urls = _stage_search(ctx)
        choice = ctx.pick_book
        if ctx.step and ctx.session.controller is not None:
            items = _items_of(ctx.session, (key.kind,))
            names = {item["index"]: item["name"] for item in items}
            choices = [{"index": i, "name": names.get(i, ""), "url": u} for i, u in enumerate(urls[:PICK_LIST_LIMIT])]
            choice = _pause_for_choice(ctx, "book", choices, len(urls))
        if not 0 <= choice < len(urls):
            debug_trace.emit("warn", value=f"书籍序号 {choice} 超出范围（共 {len(urls)} 本），改用第 1 本", data={"code": "pick_range"})
            choice = 0
        if choice and runtime.variables:
            debug_trace.emit("warn", value="选择的不是第 1 本：搜索项里 @put 写入的变量仍是第 1 本的",
                             data={"code": "pick_variables"})
        book_url = urls[choice]
        recovered = vsp.recover_book_detail_url_from_reader_candidate(ctx.http, runtime, book_url)
        if recovered:
            book_url = recovered
        if not ctx.chain:
            raise _Stop("pass", True, f"搜索调试完成：{len(urls)} 本书")
    elif key.kind == "info":
        book_url = key.value
    elif key.kind == "toc":
        book_url = key.value
        runtime.book_url = book_url
        chapter_url, chapter_title = _stage_toc(ctx, book_url, book_url)
        if not ctx.chain:
            raise _Stop("pass", True, "目录调试完成")
        _stage_content(ctx, chapter_url, chapter_title, "")
        return
    else:  # content
        runtime.book_url = key.value
        _stage_content(ctx, key.value, "调试", "")
        return
    book_name, toc_url = _stage_info(ctx, book_url)
    if vsp.is_download_only_file_source(ctx.view):
        debug_trace.emit("info", value="≡文件类书源跳过解析目录", data={"code": "file_source"})
        raise _Stop("pass", True, "≡文件类书源跳过解析目录")
    if key.kind == "info" and not ctx.chain:
        raise _Stop("pass", True, "详情调试完成")
    chapter_url, chapter_title = _stage_toc(ctx, book_url, toc_url)
    _stage_content(ctx, chapter_url, chapter_title, book_name)


def debug_source(
    src: dict[str, Any],
    key: str,
    on_event: Callable[[dict], None] | None,
    *,
    step: bool = False,
    app_version: str = DEFAULT_APP_VERSION,
    timeout: int = 60,
    controller: DebugController | None = None,
    pick_book: int = 0,
    pick_chapter: int = 0,
    chain: bool = True,
    session: DebugSession | None = None,
) -> DebugResult:
    """Debug ``src`` with ``key`` (see ``parse_debug_key``); every event goes to ``on_event`` and ``result.session``.

    Never raises for source problems: failures, timeouts, script errors and a stop request all end with a
    ``verdict`` event and a ``DebugResult``.  An ``on_event`` that raises ``DebugCancelled`` ends the run early (the
    result is ``cancelled``).  ``step=True`` needs a ``controller`` (otherwise the default picks are used).
    """
    if session is None:
        session = DebugSession(on_event=on_event, controller=controller, app_version=app_version)
    else:
        if on_event is not None:
            session.on_event = on_event
        if controller is not None:
            session.controller = controller
    session.app_version = app_version
    started = time.perf_counter()
    base_url = ""
    try:
        base_url = vsp.effective_base_url(str(src.get("bookSourceUrl") or ""))
    except Exception:  # noqa: BLE001
        base_url = ""
    parse_error = ""
    try:
        parsed = parse_debug_key(key, base_url)
    except ValueError as exc:
        parsed = DebugKey("auto", "")
        parse_error = str(exc)
    session.key = parsed
    result = DebugResult(status="error", ok=False, state=-1, message="", key=parsed, session=session)

    def finish(status: str, ok: bool, message: str, *, scope: str = "debug") -> DebugResult:
        if status == "fail" and _deadline_expired():
            # a request that ate the whole budget surfaces as an ordinary stage failure; report what happened
            status, message = "timeout", f"✗ 校验超时（{timeout} 秒）"
        session.closing = True
        result.status, result.ok, result.message = status, ok, message
        result.state = 1000 if ok else -1
        debug_trace.set_stage("")
        if status == "cancelled":
            debug_trace.emit("verdict", state=-1, value=message or "■ 已停止", data={"scope": "cancel", "ok": False})
        else:
            debug_trace.emit("verdict", state=result.state, value=message, data={"scope": scope, "ok": ok})
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    with app_profiles.app_profile_scope(app_version):
        try:
            with debug_trace.tracing(session.sink):
                try:
                    _emit_header(session, src, app_version, parsed)
                    if parse_error:
                        return finish("error", False, parse_error)
                    view, decode_error = app_profiles.app_decode_view(src)
                    if decode_error:
                        return finish("fail", False, f"App 导入会拒绝这条书源：{decode_error}")
                    if parsed.kind == "auto":
                        outcome = _validate_one_core(src, app_version=app_version, quick_timeout=min(8, max(1, timeout)),
                                                     source_timeout=timeout, rounds=1, emit_verdict=True)
                        session.closing = True
                        result.verdict = outcome
                        result.ok = bool(outcome["passed"])
                        result.state = 1000 if result.ok else -1
                        result.status = {"pass": "pass", "needsInteraction": "needsInteraction",
                                         "timeout": "timeout"}.get(outcome["verdict"], "fail")
                        result.message = outcome["failureReason"] or "校验通过"
                        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
                        return result
                    if not view.get("enabled", True):
                        debug_trace.emit("warn", value="书源已被禁用：批量验证会直接判为失效（调试仍会继续）", data={"code": "disabled"})
                    if vsp.source_requires_interactive_verification(view):
                        debug_trace.emit("warn", value="书源含 getVerificationCode / startBrowserAwait / openUrl：批量验证会判为需要交互",
                                         data={"code": "interactive"})
                    effective = vsp.effective_base_url(str(view.get("bookSourceUrl") or ""))
                    if not vsp.has_valid_base_url(effective) and not vsp.source_has_absolute_entry_point(view):
                        return finish("fail", False, "书源地址无效，且没有绝对地址的搜索 / 发现入口")
                    runtime = vsp.make_runtime(view)
                    http = vsp.reset_thread_session(view)
                    ctx = _Ctx(view=view, original=src, runtime=runtime, http=http, session=session, key=parsed,
                               version=app_version, step=step, pick_book=pick_book, pick_chapter=pick_chapter,
                               chain=chain, result=result)
                    vsp.begin_source_validation_deadline(max(0, timeout))
                    try:
                        _run_chain(ctx)
                        raise _Stop("pass", True, "调试结束")
                    except _Stop as stop:
                        _blocked_interactions(runtime)
                        return finish(stop.status, stop.ok, stop.message)
                    finally:
                        vsp.clear_source_validation_deadline()
                except vsp.SourceValidationDeadlineExceeded:
                    return finish("timeout", False, f"✗ 校验超时（{timeout} 秒）")
                except vsp.RuleScriptError as exc:
                    return finish("fail", False, f"✗ js失效：{exc}")
                except DebugCancelled:
                    return finish("cancelled", False, "■ 已停止")
                except Exception as exc:  # noqa: BLE001 - report, never crash the caller
                    log.debug("debug_source failed", exc_info=True)
                    return finish("error", False, f"✗ {type(exc).__name__}: {exc}")
        finally:
            vsp.clear_source_validation_deadline()
            session.closing = True
    return result  # pragma: no cover - every branch above returns


def _deadline_expired() -> bool:
    deadline = getattr(vsp._thread_local, "source_validation_deadline", None)
    return deadline is not None and time.monotonic() >= deadline


def _blocked_interactions(runtime: Any) -> None:
    state = getattr(runtime, "interaction_state", None)
    kinds = [str(item) for item in ((state or {}).get("kinds") or [])] if isinstance(state, dict) else []
    if kinds:
        debug_trace.emit("warn", value=f"App 批量校验会拦下这些交互：{'、'.join(kinds)}（调试里被当作未完成）",
                         data={"code": "interaction_blocked", "kinds": kinds})
