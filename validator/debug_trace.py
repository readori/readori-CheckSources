"""R146 debug trace channel for the source validator (standard library only).

The validator's rule / network code calls ``emit(...)`` at a few well-defined points.  Nothing happens unless a
caller opened ``tracing(sink)``: the sink lives in a ``ContextVar``, so one debug run never leaks into another
thread's batch validation and a normal run only pays a single ``ContextVar.get()`` per hook (hooks are written as
``if _dbg.active(): _dbg.emit(...)`` so even the argument construction is skipped).

``DebugCancelled`` derives from ``BaseException`` on purpose: the validator is full of ``except Exception``
blocks, and a user pressing "stop" (or an online preview that has seen enough) must unwind through all of them.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

_SINK: ContextVar[Callable[[dict], None] | None] = ContextVar("readori_debug_sink", default=None)
_STAGE: ContextVar[str] = ContextVar("readori_debug_stage", default="")
_NET: ContextVar[str] = ContextVar("readori_debug_net", default="")  # "" | "offline"


class DebugCancelled(BaseException):
    """Raised by a sink (or the engine) to stop a debug run; passes through the validator's ``except Exception``."""


def active() -> bool:
    return _SINK.get() is not None


def emit(kind: str, **fields: Any) -> None:
    """Hand one event to the active sink; a no-op without one.  Sink errors never break validation."""
    sink = _SINK.get()
    if sink is None:
        return
    event: dict[str, Any] = {"kind": kind, "stage": _STAGE.get()}
    event.update(fields)
    try:
        sink(event)
    except Exception:  # noqa: BLE001 - a broken sink must not change validation behaviour
        # DebugCancelled is a BaseException and is deliberately not caught here.
        pass


def set_stage(stage: str) -> None:
    if _SINK.get() is not None:
        _STAGE.set(stage)


def current_stage() -> str:
    return _STAGE.get()


def network_blocked() -> bool:
    return _NET.get() == "offline"


@contextmanager
def tracing(sink: Callable[[dict], None], *, offline: bool = False) -> Iterator[None]:
    sink_token = _SINK.set(sink)
    stage_token = _STAGE.set("")
    net_token = _NET.set("offline" if offline else "")
    try:
        yield
    finally:
        _NET.reset(net_token)
        _STAGE.reset(stage_token)
        _SINK.reset(sink_token)


@contextmanager
def offline_only() -> Iterator[None]:
    """Block network access (``fetch_text`` answers ``("", url, 0)``) without touching the sink."""
    token = _NET.set("offline")
    try:
        yield
    finally:
        _NET.reset(token)


# ---------------------------------------------------------------------------
# Helpers the network hooks of the validator call (all of them guarded by ``active()`` at the call site)
# ---------------------------------------------------------------------------

_CHARSET_HEADER_RE = re.compile(r"charset\s*=\s*['\"]?([A-Za-z0-9_\-]+)", re.I)
_CHARSET_META_RE = re.compile(r"<meta[^>]+charset\s*=\s*['\"]?([A-Za-z0-9_\-]+)", re.I)


def _detect_charset(resp: Any, text: str | None, override: str | None) -> str:
    """Best-effort name of the charset the response was decoded with (header > meta > override > '')."""
    if override:
        return str(override).strip().lower()
    try:
        content_type = str(resp.headers.get("Content-Type") or "")
    except Exception:  # noqa: BLE001
        content_type = ""
    match = _CHARSET_HEADER_RE.search(content_type)
    if match:
        return match.group(1).lower()
    if text:
        match = _CHARSET_META_RE.search(text[:4096])
        if match:
            return match.group(1).lower()
    return ""


def emit_fetch(
    resp: Any,
    *,
    url: str,
    method: str,
    attempt: Any,
    started: float,
    request_headers: dict | None = None,
    request_body: Any = None,
    charset: str | None = None,
    text: str | None = None,
    content: bytes | None = None,
) -> None:
    """Emit one ``fetch`` event for a finished ``requests`` response (text responses carry ``body``)."""
    if _SINK.get() is None:
        return
    try:
        _emit_fetch(resp, url, method, attempt, started, request_headers, request_body, charset, text, content)
    except Exception:  # noqa: BLE001 - describing a response must never break the request it describes
        pass


def _emit_fetch(resp: Any, url: str, method: str, attempt: Any, started: float, request_headers: dict | None,
                request_body: Any, charset: str | None, text: str | None, content: bytes | None) -> None:
    try:
        sent = dict(getattr(getattr(resp, "request", None), "headers", None) or request_headers or {})
    except Exception:  # noqa: BLE001
        sent = dict(request_headers or {})
    try:
        history = [(int(h.status_code), str(h.url)) for h in (resp.history or [])]
    except Exception:  # noqa: BLE001
        history = []
    fields: dict[str, Any] = {
        "url": url,
        "method": method,
        "attempt": attempt,
        "status": int(getattr(resp, "status_code", 0) or 0),
        "final_url": str(getattr(resp, "url", "") or url),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "request_headers": {str(k): str(v) for k, v in sent.items()},
        "request_body": request_body if isinstance(request_body, str) else None,
        "response_headers": {str(k): str(v) for k, v in dict(getattr(resp, "headers", None) or {}).items()},
        "history": history,
        "charset": _detect_charset(resp, text, charset),
    }
    if content is not None:
        fields.update(body=None, size=len(content), binary=True)
    else:
        fields.update(body=text, size=len((text or "").encode("utf-8", errors="ignore")), binary=False)
    emit("fetch", **fields)


def emit_fetch_error(*, url: str, method: str, attempt: Any, exc: BaseException) -> None:
    emit("fetch_error", url=url, method=method, attempt=attempt, error=f"{type(exc).__name__}: {exc}")
