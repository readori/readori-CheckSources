"""R146 WP-D: web workbench API - single-source debug sessions, rule tests, repair suggestions, diff and export.

Everything lives in memory (nothing is written to disk and the SQLite schema is untouched): a session disappears
``READORI_VALIDATOR_DEBUG_TTL`` seconds after it ended, or when the process restarts.  All routes sit behind the
same ``authorize`` dependency as the batch API (``x-api-key`` or ``Authorization: Bearer``).

Routes (``/v1/debug/...``)::

    POST /sessions                      create a debug / validate / compare run          -> 201 {sessionId, status, expiresAt}
    GET  /sessions/{id}                 status
    GET  /sessions/{id}/stream?after=   Server-Sent Events: trace / pause / done (``: ping`` every 15 s)
    GET  /sessions/{id}/events?after=   polling fallback -> {items, done, status, nextAfter}
    POST /sessions/{id}/resume          {choice}       (step mode: continue after a ``pause`` event)
    POST /sessions/{id}/cancel
    GET  /sessions/{id}/raw             saved responses (no body)
    GET  /sessions/{id}/raw/{rawId}     one response, body cut at ``maxBytes`` (default 256 KB), secrets redacted
    GET|POST /sessions/{id}/suggestions repair suggestions (POST evaluates an edited ``source``)
    POST /test-rule                     replay one rule on a saved / pasted body (offline, 10 s)
    POST /preview                       preview one suggestion (online previews take a session slot)
    POST /diff                          {original, working} -> {changes, unified}
    POST /export                        {entries} -> {sources, fixReport}

Network safety: every session thread runs inside ``network_policy_scope(settings.allow_private_network)`` so the
engine's own SSRF guard (private / loopback / metadata addresses, every redirect hop, pinned connection) applies
exactly as for batch jobs.  Raw responses are only reachable with the API key and are capped (256 KB per response,
60 per session, 8 MB per session).
"""

from __future__ import annotations

import asyncio
import bisect
import copy
import json
import logging
import os
import re
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from validator import app_profiles
from validator import debug_engine, debug_trace, fix_suggester, rule_tester, source_edit
from validator import validate_source_packages as core

try:  # FastAPI is optional for the Windows one-file validator; only register_debug_routes needs it.
    from fastapi import Depends, HTTPException, Query, Request
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError:  # pragma: no cover - exercised only without server extras
    Depends = HTTPException = Query = Request = None  # type: ignore[assignment]
    JSONResponse = StreamingResponse = None  # type: ignore[assignment,misc]

LOG = logging.getLogger("readori.validator.debug")

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
FIELD_PATH_RE = re.compile(r"^rule(?:Search|Explore|BookInfo|Toc|Content)\.[A-Za-z]{2,32}$")
DEFAULT_RAW_MAX_BYTES = 256 * 1024
BUSY_RETRY_AFTER = 10
SSE_POLL_SECONDS = 0.1
SSE_BATCH = 200
EVENT_PAGE_MAX = 1000
MAX_EXPORT_ENTRIES = 100
EXPORT_BODY_BYTES = 2 * 1024 * 1024  # same ceiling as the Cloudflare Worker's debug body limit
ENVELOPE_BYTES = 64 * 1024  # request body = sources / bodies + a little JSON framing
_STAGE_FLOW = {"pass": "pass", "needsInteraction": "needsInteraction", "timeout": "timeout"}

# Same rule as source_validator_server.redact (``author`` / ``authority`` are not secrets).
_SENSITIVE_KEY_RE = re.compile(
    r"(?:cookie|token|password|passwd|secret|api[-_]?key|authorization|auth(?!or)(?!ority)|credential|session)", re.I
)


def _default_redact(value: Any, key: str = "") -> Any:
    if _SENSITIVE_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _default_redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_default_redact(item, key) for item in value]
    return value


def _env_int(name: str, default: int, minimum: int, maximum: int, environ: Any = None) -> int:
    source = os.environ if environ is None else environ
    try:
        value = int(source.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class DebugSettings:
    """Limits of the debug API (``READORI_VALIDATOR_DEBUG_*``); independent from the batch ``ServerSettings``."""

    sessions: int = 2  # concurrent runs (running debug sessions + online previews); 1 on amd-micro
    timeout_default: int = 60
    timeout_max: int = 90
    ttl_seconds: int = 900  # how long a finished session stays readable
    retain: int = 8  # sessions kept in memory (running + finished, not yet expired)
    max_source_bytes: int = 256 * 1024
    max_body_bytes: int = 1024 * 1024  # a pasted response body for /test-rule
    max_events: int = 3000
    raw_entry_bytes: int = DEFAULT_RAW_MAX_BYTES
    raw_total_bytes: int = 8 * 1024 * 1024
    raw_max_entries: int = 60
    rule_test_timeout: int = 10
    suggest_timeout: int = 25
    heartbeat_seconds: float = 15.0
    allow_private_network: bool = False

    @classmethod
    def from_env(cls, *, amd_micro: bool = False, allow_private_network: bool = False, environ: Any = None) -> "DebugSettings":
        sessions = _env_int("READORI_VALIDATOR_DEBUG_SESSIONS", 1 if amd_micro else 2, 1, 4, environ)
        if amd_micro:
            sessions = 1  # 1 GB / fractional vCPU: never more than one interactive run next to the batch worker
        timeout_max = _env_int("READORI_VALIDATOR_DEBUG_TIMEOUT_MAX", 90, 10, 180, environ)
        timeout_default = _env_int("READORI_VALIDATOR_DEBUG_TIMEOUT", 60, 5, timeout_max, environ)
        retain = _env_int("READORI_VALIDATOR_DEBUG_RETAIN", 4 if amd_micro else 8, sessions + 1, 32, environ)
        return cls(
            sessions=sessions,
            timeout_default=timeout_default,
            timeout_max=timeout_max,
            ttl_seconds=_env_int("READORI_VALIDATOR_DEBUG_TTL", 900, 1, 86_400, environ),
            retain=retain,
            raw_entry_bytes=_env_int("READORI_VALIDATOR_DEBUG_RAW_BYTES", DEFAULT_RAW_MAX_BYTES, 4096, 1024 * 1024, environ),
            allow_private_network=bool(allow_private_network),
        )


class DebugBusy(Exception):
    """No free debug slot (or no room to keep another session)."""


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    """Round-trip through JSON so dataclasses / tuples / odd objects become plain data."""

    def default(item: Any) -> Any:
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        if isinstance(item, (set, frozenset)):
            return sorted(item, key=str)
        return str(item)

    return json.loads(json.dumps(value, ensure_ascii=False, default=default))


def _slim_validation(result: dict[str, Any]) -> dict[str, Any]:
    """A ``validate_one`` result without the heavy ``detail`` / ``record`` blobs."""

    return {key: copy.deepcopy(value) for key, value in result.items() if key not in ("detail", "record")}


class DebugRun:
    """One debug / validate / compare session (state is shared with the worker thread, so access is locked)."""

    def __init__(self, manager: "DebugManager", session_id: str, mode: str, source: dict[str, Any],
                 original: dict[str, Any] | None, key: str, app_version: str, step: bool, timeout: int) -> None:
        settings = manager.settings
        self.manager = manager
        self.id = session_id
        self.mode = mode
        self.source = source
        self.original = original
        self.key = key
        self.app_version = app_version
        self.step = step
        self.timeout = timeout
        self.created = manager.now()
        self.controller = debug_engine.DebugController()
        self.session = debug_engine.DebugSession(
            on_event=self._on_event,
            controller=self.controller,
            max_events=settings.max_events,
            raw=debug_engine.RawStore(
                per_entry_limit=settings.raw_entry_bytes,
                total_limit=settings.raw_total_bytes,
                max_entries=settings.raw_max_entries,
            ),
            app_version=app_version,
        )
        self._lock = threading.Lock()
        self.done = False
        self.outcome = ""
        self.ok = False
        self.verdict: str | None = None
        self.result: dict[str, Any] | None = None
        self.finished_at: float | None = None
        self.pending_pause: dict[str, Any] | None = None
        self.future: Future | None = None
        self._released = False
        self._suggestions: tuple[str, dict[str, Any]] | None = None

    # -- events -------------------------------------------------------------------------------------------
    def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("kind") == "pause":
            data = event.get("data") or {}
            with self._lock:
                self.pending_pause = {"what": data.get("what"), "total": int(data.get("total") or 0)}

    def public_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """The event as the browser sees it: request / response headers stay in ``/raw`` (redacted)."""

        out = dict(event)
        data = dict(out.get("data") or {})
        for name in ("request_headers", "response_headers", "request_body"):
            data.pop(name, None)
        out["data"] = self.manager.redact(data)
        return out

    def events_after(self, after: int, limit: int) -> list[dict[str, Any]]:
        snapshot = self.session.events[:]  # a list slice is atomic; the worker only appends
        start = bisect.bisect_right(snapshot, after, key=lambda item: int(item.get("seq") or 0))
        return [self.public_event(item) for item in snapshot[start:start + max(1, limit)]]

    # -- state --------------------------------------------------------------------------------------------
    def status(self) -> str:
        with self._lock:
            if self.done:
                return self.outcome or "error"
            if self.pending_pause is not None:
                return "paused"
        return "running"

    def release_slot(self) -> None:
        """Give the concurrency slot back exactly once (as soon as the outcome is known, not when the thread exits)."""

        with self._lock:
            if self._released:
                return
            self._released = True
        self.manager.release()

    def finish(self, outcome: str, *, ok: bool, verdict: str | None, result: dict[str, Any] | None) -> None:
        self.release_slot()
        with self._lock:
            self.outcome = outcome
            self.ok = bool(ok)
            self.verdict = verdict
            self.result = result
            self.pending_pause = None
            self.finished_at = self.manager.now()
            self.done = True  # last: readers check ``done`` first and then drain the events

    def done_payload(self) -> dict[str, Any]:
        with self._lock:
            return {"status": self.outcome or "error", "ok": self.ok, "verdict": self.verdict, "result": self.result}

    def summary(self) -> dict[str, Any]:
        expires = None if self.finished_at is None else _iso(self.finished_at + self.manager.settings.ttl_seconds)
        return {
            "sessionId": self.id, "mode": self.mode, "status": self.status(), "done": self.done,
            "appVersion": self.app_version, "key": self.key, "step": self.step, "timeoutSeconds": self.timeout,
            "eventCount": len(self.session.events), "rawCount": len(self.session.raw),
            "createdAt": _iso(self.created), "expiresAt": expires,
        }

    # -- suggestions cache (the preview endpoint looks a suggestion up by id) -----------------------------
    def suggestions_for(self, source: dict[str, Any]) -> dict[str, Any]:
        fingerprint = json.dumps(source, sort_keys=True, ensure_ascii=False, default=str)
        with self._lock:
            if self._suggestions is not None and self._suggestions[0] == fingerprint:
                return self._suggestions[1]
        items = fix_suggester.suggest(copy.deepcopy(source), self.session, app_version=self.app_version)
        by_id = {item.id: item for item in items}
        with self._lock:
            self._suggestions = (fingerprint, by_id)
        return by_id

    # -- worker thread ------------------------------------------------------------------------------------
    def run(self) -> None:
        """Executed on a pool thread.  Never raises; always ends with ``finish`` and a released slot."""

        settings = self.manager.settings
        try:
            with core.network_policy_scope(settings.allow_private_network):
                self._execute()
        except debug_trace.DebugCancelled:
            self._finish_cancelled()
        except BaseException as exc:  # noqa: BLE001 - the thread must always report an outcome
            LOG.exception("debug session %s failed", self.id)
            self.session.closing = True
            try:
                self.session.sink({"kind": "verdict", "stage": "", "state": -1,
                                   "value": f"✗ {type(exc).__name__}: {exc}", "data": {"scope": "debug", "ok": False}})
            except BaseException:  # noqa: BLE001
                pass
            self.finish("error", ok=False, verdict=None, result={"message": f"{type(exc).__name__}: {exc}"})
        finally:
            self.release_slot()

    def _finish_cancelled(self) -> None:
        self.session.closing = True
        try:
            self.session.sink({"kind": "verdict", "stage": "", "state": -1, "value": "■ 已停止",
                               "data": {"scope": "cancel", "ok": False}})
        except BaseException:  # noqa: BLE001
            pass
        self.finish("cancelled", ok=False, verdict=None, result={"message": "■ 已停止"})

    def _execute(self) -> None:
        timeout = self.timeout
        if self.mode == "debug":
            outcome = debug_engine.debug_source(
                self.source, self.key, None, step=self.step, app_version=self.app_version, timeout=timeout,
                controller=self.controller, session=self.session,
            )
            verdict = outcome.verdict.get("verdict") if isinstance(outcome.verdict, dict) else None
            result = self.manager.redact(_jsonable(outcome.to_dict()))
            self.finish(outcome.status, ok=outcome.ok, verdict=verdict, result=result)
        elif self.mode == "validate":
            result = debug_engine.validate_one(
                self.source, app_version=self.app_version, quick_timeout=min(8, timeout), source_timeout=timeout,
                rounds=1, trace=self.session,
            )
            self.session.closing = True
            slim = _slim_validation(result)
            self.finish(_STAGE_FLOW.get(result["verdict"], "fail"), ok=bool(result["passed"]),
                        verdict=result["verdict"], result=self.manager.redact(_jsonable(slim)))
        else:  # compare
            self._execute_compare()

    def _execute_compare(self) -> None:
        timeout = self.timeout
        base_sink = self.session.sink
        starts = {"count": 0}

        def sink(event: dict[str, Any]) -> None:
            # Both runs share one log: put a header line in front of each ``quick-scan`` start.
            if event.get("kind") == "begin" and event.get("stage") == "quick-scan":
                starts["count"] += 1
                label = "── 校验原书源 ──" if starts["count"] == 1 else "── 校验修改后的书源 ──"
                base_sink({"kind": "info", "stage": "", "value": label, "data": {"code": "header"}})
            base_sink(event)

        with debug_trace.tracing(sink):
            comparison = debug_engine.compare_sources(
                self.original or self.source, self.source, app_version=self.app_version,
                quick_timeout=min(8, timeout), source_timeout=timeout, rounds=1,
            )
            self.session.closing = True
            after = comparison["after"]
            debug_trace.set_stage("")
            summary = {
                "scope": "validation", "ok": bool(after.get("passed")), "version": self.app_version,
                "verdictZh": after.get("verdictZh"), "failureReason": after.get("failureReason"),
                "failureGroupZh": after.get("failureGroupZh"),
            }
            debug_trace.emit("verdict", state=1000 if after.get("passed") else -1,
                             value=f"对比完成：{comparison['before'].get('verdictZh')} → {after.get('verdictZh')}",
                             data=summary)
        slim = {
            "before": _slim_validation(comparison["before"]), "after": _slim_validation(after),
            "rows": comparison["rows"], "elapsedMs": comparison["elapsedMs"], "changed": comparison["changed"],
        }
        self.finish("pass" if after.get("passed") else "fail", ok=bool(after.get("passed")),
                    verdict=str(after.get("verdict") or ""), result=self.manager.redact(_jsonable(slim)))


class DebugManager:
    """In-memory sessions, the concurrency gate and the worker pools."""

    def __init__(self, settings: DebugSettings, *, redact: Callable[[Any], Any] | None = None) -> None:
        self.settings = settings
        self._redact = redact or _default_redact
        self._lock = threading.Lock()
        self._runs: dict[str, DebugRun] = {}
        self._active = 0
        self._pool = ThreadPoolExecutor(max_workers=settings.sessions, thread_name_prefix="readori-debug")
        # Offline work (rule tests, suggestions, offline previews) is short; keep it off the session slots.
        self._light = ThreadPoolExecutor(max_workers=2, thread_name_prefix="readori-debug-light")
        self.now: Callable[[], float] = time.time
        self._closed = False

    def redact(self, value: Any) -> Any:
        return self._redact(value)

    # -- slots --------------------------------------------------------------------------------------------
    def try_acquire(self) -> bool:
        with self._lock:
            if self._closed or self._active >= self.settings.sessions:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    # -- sessions -----------------------------------------------------------------------------------------
    def _purge_locked(self) -> None:
        now = self.now()
        ttl = self.settings.ttl_seconds
        for session_id, run in list(self._runs.items()):
            if run.done and run.finished_at is not None and run.finished_at + ttl <= now:
                del self._runs[session_id]

    def create(self, mode: str, source: dict[str, Any], original: dict[str, Any] | None, key: str, app_version: str,
               step: bool, timeout: int) -> DebugRun:
        with self._lock:
            self._purge_locked()
            if len(self._runs) >= self.settings.retain:
                # drop the oldest finished session to make room; running ones are never evicted
                finished = sorted((run for run in self._runs.values() if run.done), key=lambda run: run.finished_at or 0)
                if finished:
                    del self._runs[finished[0].id]
            if len(self._runs) >= self.settings.retain or self._closed or self._active >= self.settings.sessions:
                raise DebugBusy()
            self._active += 1
            run = DebugRun(self, secrets.token_urlsafe(16), mode, source, original, key, app_version, step, timeout)
            self._runs[run.id] = run
        try:
            run.future = self._pool.submit(run.run)
        except BaseException:  # noqa: BLE001 - e.g. executor already shut down
            self.release()
            with self._lock:
                self._runs.pop(run.id, None)
            raise DebugBusy() from None
        return run

    def get(self, session_id: str) -> DebugRun | None:
        if not SESSION_ID_RE.match(session_id or ""):
            return None
        with self._lock:
            self._purge_locked()
            return self._runs.get(session_id)

    def expires_hint(self, timeout: int) -> str:
        return _iso(self.now() + timeout + self.settings.ttl_seconds)

    # -- short jobs ---------------------------------------------------------------------------------------
    def _guarded(self, fn: Callable[[], Any], timeout: float) -> Callable[[], Any]:
        allow_private = self.settings.allow_private_network

        def runner() -> Any:
            with core.network_policy_scope(allow_private):
                core.begin_source_validation_deadline(max(1, int(timeout)))
                try:
                    return fn()
                finally:
                    core.clear_source_validation_deadline()

        return runner

    async def run_light(self, fn: Callable[[], Any], timeout: float) -> Any:
        """Run offline work on the light pool with a wall-clock limit (HTTP 504 when it overruns)."""

        future = self._light.submit(self._guarded(fn, timeout))
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout + 2)
        except (asyncio.TimeoutError, core.SourceValidationDeadlineExceeded):
            future.cancel()
            raise HTTPException(status_code=504, detail="timed out") from None

    async def run_slot(self, fn: Callable[[], Any], timeout: float) -> Any:
        """Run work that talks to the network (online preview): needs a free slot, else ``DebugBusy``."""

        if not self.try_acquire():
            raise DebugBusy()

        def job() -> Any:
            try:
                return self._guarded(fn, timeout)()
            finally:
                self.release()

        try:
            future = self._pool.submit(job)
        except BaseException:  # noqa: BLE001
            self.release()
            raise DebugBusy() from None
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout + 5)
        except (asyncio.TimeoutError, core.SourceValidationDeadlineExceeded):
            future.cancel()
            raise HTTPException(status_code=504, detail="timed out") from None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            runs = list(self._runs.values())
        for run in runs:
            run.controller.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._light.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------------------------------------
# request parsing helpers
# ---------------------------------------------------------------------------------------------------------

def _bad(detail: str, status: int = 400) -> "HTTPException":
    return HTTPException(status_code=status, detail=detail)


async def read_json_body(request: Any, limit: int) -> dict[str, Any]:
    """Read a JSON object body, refusing (413) as soon as more than ``limit`` bytes have arrived."""

    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _bad(f"request body is too large (limit {limit} bytes)", 413)
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise _bad(f"request body is too large (limit {limit} bytes)", 413)
    try:
        payload = json.loads(bytes(buffer).decode("utf-8-sig") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _bad("invalid JSON") from None
    if not isinstance(payload, dict):
        raise _bad("JSON body must be an object")
    return payload


def _source_of(payload: dict[str, Any], name: str, settings: DebugSettings, *, required: bool = True) -> dict[str, Any] | None:
    value = payload.get(name)
    if value is None:
        if required:
            raise _bad(f"{name} is required")
        return None
    if not isinstance(value, dict):
        raise _bad(f"{name} must be a book source object")
    size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    if size > settings.max_source_bytes:
        raise _bad(f"{name} is too large (limit {settings.max_source_bytes} bytes)", 413)
    return value


def _int_field(payload: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    value = payload.get(name, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _bad(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _bad(f"{name} must be an integer") from None
    if number < minimum or number > maximum:
        raise _bad(f"{name} must be between {minimum} and {maximum}")
    return number


def _app_version(payload: dict[str, Any], default: str | None = None) -> str:
    value = payload.get("appVersion", payload.get("app_version"))
    if value in (None, ""):
        return default or app_profiles.DEFAULT_APP_VERSION
    version = str(value).strip()
    if version not in app_profiles.SUPPORTED_APP_VERSIONS:
        raise _bad(f"unsupported appVersion: {version}")
    return version


def _frame(name: str, payload: Any, event_id: int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {name}")
    lines.append("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def _last_event_id(request: Any) -> int:
    raw = request.headers.get("last-event-id", "").strip()
    return int(raw) if raw.isdigit() else 0


# ---------------------------------------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------------------------------------

def register_debug_routes(app: Any, settings: Any, authorize: Callable[..., None], *, service: Any = None,
                          redact: Callable[[Any], Any] | None = None,
                          debug_settings: DebugSettings | None = None) -> DebugManager:
    """Add the ``/v1/debug/*`` routes to ``app`` (every route requires ``authorize``) and return the manager."""

    if Depends is None:
        raise RuntimeError("服务器依赖未安装，请执行 pip install -r server/requirements.txt")
    cfg = debug_settings or DebugSettings.from_env(
        amd_micro=bool(getattr(settings, "amd_micro", False)),
        allow_private_network=bool(getattr(settings, "allow_private_network", False)),
    )
    manager = DebugManager(cfg, redact=redact)
    app.state.debug_manager = manager
    guard = [Depends(authorize)]

    # The server's lifespan owns shutdown; chain onto it so running debug threads are told to stop.
    try:
        inner = app.router.lifespan_context

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(application: Any):
            async with inner(application) as state:
                try:
                    yield state
                finally:
                    manager.close()

        app.router.lifespan_context = lifespan
    except Exception:  # noqa: BLE001 - a missing hook only costs a slower shutdown
        LOG.debug("could not chain debug shutdown onto the app lifespan", exc_info=True)

    def need_run(session_id: str) -> DebugRun:
        run = manager.get(session_id)
        if run is None:
            raise HTTPException(status_code=404, detail="session not found or expired")
        return run

    def busy() -> Any:
        return JSONResponse({"error": "debug busy", "retryAfter": BUSY_RETRY_AFTER}, status_code=429,
                            headers={"Retry-After": str(BUSY_RETRY_AFTER)})

    # -- create / status -------------------------------------------------------------------------------
    @app.post("/v1/debug/sessions", dependencies=guard, status_code=201)
    async def create_session(request: Request) -> Any:
        payload = await read_json_body(request, cfg.max_source_bytes * 2 + ENVELOPE_BYTES)
        mode = str(payload.get("mode") or "debug").strip().lower()
        if mode not in ("debug", "validate", "compare"):
            raise _bad("mode must be debug, validate or compare")
        source = _source_of(payload, "source", cfg)
        assert source is not None
        if not str(source.get("bookSourceUrl") or "").strip():
            raise _bad("source.bookSourceUrl is required")
        original = _source_of(payload, "original", cfg, required=(mode == "compare"))
        key = payload.get("key", "")
        if key is None:
            key = ""
        if not isinstance(key, str) or len(key) > 2000:
            raise _bad("key must be a string of at most 2000 characters")
        step = payload.get("step", False)
        if not isinstance(step, bool):
            raise _bad("step must be a boolean")
        timeout = _int_field(payload, "timeoutSeconds", cfg.timeout_default, 5, cfg.timeout_max)
        version = _app_version(payload)
        try:
            run = manager.create(mode, copy.deepcopy(source), copy.deepcopy(original) if original else None,
                                 key.strip() if mode == "debug" else "", version, bool(step and mode == "debug"), timeout)
        except DebugBusy:
            return busy()
        return {"sessionId": run.id, "mode": run.mode, "status": "running", "expiresAt": manager.expires_hint(timeout)}

    @app.get("/v1/debug/sessions/{session_id}", dependencies=guard)
    async def get_session(session_id: str) -> Any:
        return need_run(session_id).summary()

    # -- streaming / polling ---------------------------------------------------------------------------
    @app.get("/v1/debug/sessions/{session_id}/stream", dependencies=guard)
    async def stream_session(session_id: str, request: Request, after: int = Query(0, ge=0)) -> Any:
        run = need_run(session_id)
        cursor = max(after, _last_event_id(request))

        async def generate() -> Any:
            nonlocal cursor
            yield ": connected\n\n"  # flushes the response headers right away
            last_write = time.monotonic()
            while True:
                finished = run.done  # read first: once set, every event is already in the list
                batch = run.events_after(cursor, SSE_BATCH)
                for event in batch:
                    cursor = int(event["seq"])
                    name = "pause" if event.get("kind") == "pause" else "trace"
                    yield _frame(name, event, cursor)
                if batch:
                    last_write = time.monotonic()
                    continue
                if finished:
                    yield _frame("done", run.done_payload())
                    return
                if time.monotonic() - last_write >= cfg.heartbeat_seconds:
                    yield ": ping\n\n"
                    last_write = time.monotonic()
                await asyncio.sleep(SSE_POLL_SECONDS)

        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        return StreamingResponse(generate(), media_type="text/event-stream", headers=headers)

    @app.get("/v1/debug/sessions/{session_id}/events", dependencies=guard)
    async def poll_events(session_id: str, after: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=EVENT_PAGE_MAX)) -> Any:
        run = need_run(session_id)
        finished = run.done
        items = run.events_after(after, limit)
        next_after = int(items[-1]["seq"]) if items else after
        complete = finished and not run.events_after(next_after, 1)
        payload: dict[str, Any] = {"items": items, "done": complete, "status": run.status(), "nextAfter": next_after}
        if complete:
            payload["final"] = run.done_payload()
        return payload

    # -- control ---------------------------------------------------------------------------------------
    @app.post("/v1/debug/sessions/{session_id}/resume", dependencies=guard)
    async def resume_session(session_id: str, request: Request) -> Any:
        run = need_run(session_id)
        payload = await read_json_body(request, 4096)
        choice = _int_field(payload, "choice", 0, 0, 1_000_000)
        with run._lock:
            pending = run.pending_pause
            if run.done or pending is None:
                raise HTTPException(status_code=409, detail="session is not waiting for a choice")
            total = int(pending.get("total") or 0)
            if total and choice >= total:
                raise _bad(f"choice must be below {total}")
            run.pending_pause = None
        run.controller.resume(choice)
        return {"status": "running", "choice": choice}

    @app.post("/v1/debug/sessions/{session_id}/cancel", dependencies=guard)
    async def cancel_session(session_id: str) -> Any:
        run = need_run(session_id)
        run.controller.cancel()
        return {"status": run.status(), "cancelRequested": True}

    # -- raw responses ---------------------------------------------------------------------------------
    @app.get("/v1/debug/sessions/{session_id}/raw", dependencies=guard)
    async def list_raw(session_id: str) -> Any:
        run = need_run(session_id)
        keep = ("raw_id", "stage", "url", "final_url", "method", "status", "charset", "size", "truncated",
                "elapsed_ms", "attempt", "binary")
        items = [{name: item.get(name) for name in keep} for item in run.session.raw.summaries()]
        return {"items": items, "limits": {"maxEntries": cfg.raw_max_entries, "entryBytes": cfg.raw_entry_bytes,
                                           "totalBytes": cfg.raw_total_bytes}}

    @app.get("/v1/debug/sessions/{session_id}/raw/{raw_id}", dependencies=guard)
    async def get_raw(session_id: str, raw_id: int, max_bytes: int = Query(DEFAULT_RAW_MAX_BYTES, alias="maxBytes", ge=0, le=1024 * 1024)) -> Any:
        run = need_run(session_id)
        entry = run.session.raw.get(raw_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="raw response not found")
        out = entry.to_dict(include_body=True, max_bytes=min(max_bytes, cfg.raw_entry_bytes))
        out["request_headers"] = manager.redact(out.get("request_headers") or {})
        out["response_headers"] = manager.redact(out.get("response_headers") or {})
        body = out.get("request_body")
        if isinstance(body, str) and len(body) > 4096:
            out["request_body"] = body[:4096] + "…"
        return out

    # -- suggestions -----------------------------------------------------------------------------------
    async def suggestions_response(run: DebugRun, source: dict[str, Any]) -> Any:
        if not run.done:
            raise HTTPException(status_code=409, detail="session is still running")
        by_id = await manager.run_light(lambda: run.suggestions_for(source), cfg.suggest_timeout)
        items = [_jsonable(item.to_dict()) for item in by_id.values()]
        return {"items": items, "sessionId": run.id}

    @app.get("/v1/debug/sessions/{session_id}/suggestions", dependencies=guard)
    async def get_suggestions(session_id: str, source: str = Query("working")) -> Any:
        run = need_run(session_id)
        if source != "working":
            raise _bad("only source=working is supported on GET; POST the edited source instead")
        return await suggestions_response(run, run.source)

    @app.post("/v1/debug/sessions/{session_id}/suggestions", dependencies=guard)
    async def post_suggestions(session_id: str, request: Request) -> Any:
        run = need_run(session_id)
        payload = await read_json_body(request, cfg.max_source_bytes + ENVELOPE_BYTES)
        source = _source_of(payload, "source", cfg)
        assert source is not None
        return await suggestions_response(run, source)

    @app.post("/v1/debug/preview", dependencies=guard)
    async def preview(request: Request) -> Any:
        payload = await read_json_body(request, cfg.max_source_bytes + ENVELOPE_BYTES)
        run = need_run(str(payload.get("sessionId") or ""))
        suggestion_id = str(payload.get("suggestionId") or "")
        if not suggestion_id:
            raise _bad("suggestionId is required")
        source = _source_of(payload, "source", cfg)
        assert source is not None
        online = bool(payload.get("online", False))
        version = _app_version(payload, run.app_version)
        if not run.done:
            raise HTTPException(status_code=409, detail="session is still running")
        by_id = await manager.run_light(lambda: run.suggestions_for(source), cfg.suggest_timeout)
        suggestion = by_id.get(suggestion_id)
        if suggestion is None:
            raise HTTPException(status_code=404, detail="suggestion not found for this source")

        def work() -> Any:
            return fix_suggester.preview(copy.deepcopy(source), suggestion, run.session, online=online, app_version=version)

        try:
            if online and suggestion.preview_kind == "online":
                outcome = await manager.run_slot(work, fix_suggester.ONLINE_PREVIEW_TIMEOUT + 10)
            else:
                outcome = await manager.run_light(work, cfg.suggest_timeout)
        except DebugBusy:
            return busy()
        return _jsonable(outcome)

    # -- rule test -------------------------------------------------------------------------------------
    @app.post("/v1/debug/test-rule", dependencies=guard)
    async def test_rule(request: Request) -> Any:
        payload = await read_json_body(request, cfg.max_source_bytes + cfg.max_body_bytes + ENVELOPE_BYTES)
        source = _source_of(payload, "source", cfg)
        assert source is not None
        field_path = str(payload.get("field") or "")
        if not FIELD_PATH_RE.match(field_path):
            raise _bad("field must look like ruleSearch.name")
        rule = payload.get("rule")
        if rule is not None and (not isinstance(rule, str) or len(rule) > 20_000):
            raise _bad("rule must be a string of at most 20000 characters")
        item_index = _int_field(payload, "itemIndex", 0, 0, 100_000)
        body = payload.get("body")
        if body is not None:
            if not isinstance(body, str):
                raise _bad("body must be a string")
            if len(body.encode("utf-8")) > cfg.max_body_bytes:
                raise _bad(f"body is too large (limit {cfg.max_body_bytes} bytes)", 413)
        base_url = payload.get("baseUrl")
        if base_url is not None and (not isinstance(base_url, str) or len(base_url) > 4096):
            raise _bad("baseUrl must be a string")
        session_id = payload.get("sessionId")
        run = need_run(str(session_id)) if session_id else None
        version = _app_version(payload, run.app_version if run else None)
        raw_id = payload.get("rawId")
        if body is None and run is None:
            raise _bad("either body or sessionId is required")
        entry = None
        if body is None and raw_id not in (None, ""):
            entry = run.session.raw.get(raw_id) if run else None
            if entry is None:
                raise HTTPException(status_code=404, detail="raw response not found")
        variables = dict(run.session.variables) if run else None

        def work() -> Any:
            if body is not None:
                return rule_tester.test_rule(
                    source, field_path, body=body,
                    base_url=base_url or str(source.get("bookSourceUrl") or ""), rule=rule, item_index=item_index,
                    app_version=version, variables=variables, offline=True)
            if entry is not None:
                result = rule_tester.test_rule(
                    source, field_path, body=entry.body or "", base_url=base_url or entry.final_url or entry.url,
                    rule=rule, item_index=item_index, app_version=version, variables=variables, offline=True)
            else:
                result = rule_tester.test_rule_on_session(
                    source, run.session, field_path, rule=rule, item_index=item_index, app_version=version, online=False)
            return result

        result = await manager.run_light(work, cfg.rule_test_timeout)
        out = _jsonable(result.to_dict())
        used = entry or (None if body is not None or run is None else run.session.raw.latest(rule_tester.stage_of_field(field_path)))
        if used is not None:
            out.setdefault("data", {})["rawId"] = used.raw_id
            if used.truncated:
                out.setdefault("warnings", []).append("保存的响应已被截断，结果可能不完整")
        return out

    # -- diff / export ---------------------------------------------------------------------------------
    @app.post("/v1/debug/diff", dependencies=guard)
    async def diff(request: Request) -> Any:
        payload = await read_json_body(request, cfg.max_source_bytes * 2 + ENVELOPE_BYTES)
        original = _source_of(payload, "original", cfg)
        working = _source_of(payload, "working", cfg)
        assert original is not None and working is not None

        def work() -> Any:
            document = source_edit.SourceDocument(original)
            document.working = copy.deepcopy(working)
            return {"changes": document.diff(), "unified": document.unified_diff()}

        return _jsonable(await manager.run_light(work, cfg.rule_test_timeout))

    @app.post("/v1/debug/export", dependencies=guard)
    async def export(request: Request) -> Any:
        payload = await read_json_body(request, EXPORT_BODY_BYTES)
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise _bad("entries must be a non-empty list")
        if len(raw_entries) > MAX_EXPORT_ENTRIES:
            raise _bad(f"too many entries (limit {MAX_EXPORT_ENTRIES})", 413)
        entries: list[dict[str, Any]] = []
        for index, item in enumerate(raw_entries):
            if not isinstance(item, dict) or not isinstance(item.get("working"), dict):
                raise _bad(f"entries[{index}].working must be a book source object")
            original = item.get("original") if isinstance(item.get("original"), dict) else item["working"]
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            codes = item.get("codes") if isinstance(item.get("codes"), list) else []
            entries.append({
                "original": original, "working": item["working"],
                "changes": [change for change in changes if isinstance(change, dict)],
                "codes": [str(code) for code in codes],
                "before": item.get("before") if isinstance(item.get("before"), dict) else None,
                "after": item.get("after") if isinstance(item.get("after"), dict) else None,
            })
        only_changed = bool(payload.get("onlyChanged", False))
        only_passed = bool(payload.get("onlyPassed", False))
        keep_source_url = bool(payload.get("keepSourceUrl", False))
        version = _app_version(payload)

        def work() -> Any:
            now = datetime.now(source_edit.la_zone() or timezone.utc)
            kept = [entry for entry in entries
                    if source_edit.export_sources([entry], only_changed=only_changed, only_passed=only_passed)]
            sources = source_edit.export_sources(kept, keep_source_url=keep_source_url, stamp=True, now=now)
            report = source_edit.build_fix_report(kept, app_version=version, now=now, keep_source_url=keep_source_url)
            return {"sources": sources, "fixReport": report, "count": len(sources)}

        return _jsonable(await manager.run_light(work, cfg.rule_test_timeout * 3))

    return manager
