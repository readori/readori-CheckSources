"""Server-side Readori/Legado source validation service.

The Windows GUI is intentionally a thin client.  This module keeps the same
validation core used by the GUI, but adds a durable SQLite queue, staged
checkpoints, bounded per-domain concurrency, retries for transient failures,
API-key authentication, and a result endpoint that only returns sources that
passed the complete chain and the device-compatibility gate.

Run locally with::

    python -m server.source_validator_server --api-key change-me

For production, put the service behind TLS/authenticated reverse proxy and
configure ``READORI_VALIDATOR_API_KEY`` and ``READORI_VALIDATOR_DB``.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from validator import validate_source_packages as core

try:  # FastAPI is optional for the Windows one-file validator.
    from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover - exercised only without server extras
    Depends = FastAPI = File = Form = HTTPException = Query = UploadFile = None  # type: ignore[assignment]
    Request = JSONResponse = BaseModel = Field = None  # type: ignore[assignment,misc]


LOG = logging.getLogger("readori.validator.server")
SENSITIVE_KEY_RE = re.compile(
    r"(?:cookie|token|password|passwd|secret|api[-_]?key|authorization|auth|credential|session)",
    re.I,
)


def _safe_count(value: Any) -> int:
    """Read numeric pipeline metrics without letting malformed JSON kill a job."""

    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class ServerSettings:
    """Runtime limits shared by the HTTP API and background worker."""

    db_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "data" / "validator.sqlite3")
    api_key: str = ""
    allowed_input_root: Path | None = None
    max_sources: int = 20_000
    max_workers: int = 16
    max_jobs: int = 1
    default_domain_concurrency: int = 2
    max_upload_bytes: int = 64 * 1024 * 1024
    amd_micro: bool = False

    @classmethod
    def from_env(cls) -> "ServerSettings":
        root_raw = os.environ.get("READORI_VALIDATOR_INPUT_ROOT", "").strip()
        profile = os.environ.get("READORI_VALIDATOR_EXECUTOR_PROFILE", "").strip().lower().replace("_", "-")
        amd_micro = profile in {"amd-micro", "micro"} or os.environ.get("READORI_AMD_MICRO", "").strip().lower() in {"1", "true", "yes", "on"}
        configured_workers = _env_int("READORI_VALIDATOR_MAX_WORKERS", 16, 1, 64)
        configured_jobs = _env_int("READORI_VALIDATOR_MAX_JOBS", 1, 1, 4)
        configured_domain_concurrency = _env_int("READORI_VALIDATOR_DOMAIN_CONCURRENCY", 2, 1, 8)
        if amd_micro:
            configured_workers = 1
            configured_jobs = 1
            configured_domain_concurrency = 1
        return cls(
            db_path=Path(os.environ.get("READORI_VALIDATOR_DB", "server/data/validator.sqlite3")).expanduser().resolve(),
            api_key=os.environ.get("READORI_VALIDATOR_API_KEY", ""),
            allowed_input_root=Path(root_raw).expanduser().resolve() if root_raw else None,
            max_sources=_env_int("READORI_VALIDATOR_MAX_SOURCES", 20_000, 1, 200_000),
            max_workers=configured_workers,
            max_jobs=configured_jobs,
            default_domain_concurrency=configured_domain_concurrency,
            max_upload_bytes=_env_int("READORI_VALIDATOR_MAX_UPLOAD_BYTES", 64 * 1024 * 1024, 1_024, 512 * 1024 * 1024),
            amd_micro=amd_micro,
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return dict(value)


def canonical_source_key(source: dict[str, Any]) -> str:
    """Combine canonical site identity and behavior rule fingerprint."""

    return core.source_dedupe_key(source)


def canonical_source_site_key(source: dict[str, Any]) -> str:
    return core.canonical_source_site_key(source)


def source_rule_fingerprint(source: dict[str, Any]) -> str:
    return core.source_rule_fingerprint(source)


def source_domain(source: dict[str, Any]) -> str:
    """Return a stable host bucket for the per-domain limiter."""

    candidates = [str(source.get("bookSourceUrl") or ""), str(source.get("searchUrl") or ""), str(source.get("exploreUrl") or "")]
    for raw in candidates:
        for match in re.finditer(r"https?://[^\s'\"<>`{},]+", raw, re.I):
            try:
                host = (urlparse(match.group(0)).hostname or "").lower()
            except Exception:
                host = ""
            if host:
                return host
    return "unknown"


def prepare_source_groups(sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe imports by canonical site plus rule fingerprint."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        if not str(source.get("bookSourceUrl") or "").strip():
            continue
        grouped.setdefault(canonical_source_key(source), []).append(dict(source))
    groups: list[dict[str, Any]] = []
    for key, variants in grouped.items():
        variants.sort(key=core.score_candidate, reverse=True)
        selected = variants[0]
        groups.append(
            {
                "source_key": key,
                "source": selected,
                "variants": variants,
                "duplicate_count": max(0, len(variants) - 1),
                "source_name": str(selected.get("bookSourceName") or ""),
                "source_url": str(selected.get("bookSourceUrl") or "").strip(),
                "canonical_site": canonical_source_site_key(selected),
                "rule_fingerprint": source_rule_fingerprint(selected),
            }
        )
    groups.sort(key=lambda item: (str(item["source_name"]).casefold(), str(item["source_url"])))
    return groups


def redact(value: Any, key: str = "") -> Any:
    """Remove secrets from events and progress metadata."""

    if SENSITIVE_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    return value


def detail_is_device_compatible(source: dict[str, Any], detail: dict[str, Any], record: dict[str, Any] | None) -> tuple[bool, str]:
    """Reject common false positives before writing the App-ready output."""

    stages = detail.get("stages") if isinstance(detail, dict) else None
    if not isinstance(stages, dict) or not all(bool(stages.get(name)) for name in ("search", "detail", "toc", "content")):
        return False, "device gate: incomplete search/detail/toc/content chain"
    if not str(detail.get("detailBookName") or "").strip():
        return False, "device gate: empty detail book name"
    if _safe_count(detail.get("tocUniqueChapterCount")) < 1:
        return False, "device gate: empty chapter list"
    if _safe_count(detail.get("contentPreviewChars")) < 20:
        return False, "device gate: content preview is too short"
    if record is None:
        return False, "device gate: missing selected source"
    if core.source_requires_interactive_verification(source):
        return False, "device gate: interactive browser verification required"
    if core.is_browser_deferred_content_source(source):
        return False, "device gate: content is deferred to WebView"
    sample_url = str(detail.get("sampleBookUrl") or "")
    if sample_url and core.is_likely_search_or_explore_landing_url(sample_url, core.make_runtime(source), reject_search_endpoint=True):
        return False, "device gate: sample URL is still a landing/search page"
    return True, ""


class JobStore:
    """Small SQLite persistence layer; every write is serialized explicitly."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterable[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            input_name TEXT NOT NULL DEFAULT '',
            source_count INTEGER NOT NULL,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            stage TEXT NOT NULL DEFAULT 'queued',
            completed_count INTEGER NOT NULL DEFAULT 0,
            passed_count INTEGER NOT NULL DEFAULT 0,
            progress REAL NOT NULL DEFAULT 0,
            config_json TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            cancel_requested INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS job_sources (
            job_id TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_json TEXT NOT NULL,
            variants_json TEXT NOT NULL,
            source_name TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            quick_status TEXT NOT NULL DEFAULT 'pending',
            quick_json TEXT NOT NULL DEFAULT '{}',
            full_status TEXT NOT NULL DEFAULT 'pending',
            result_json TEXT NOT NULL DEFAULT '',
            stability_pass_count INTEGER NOT NULL DEFAULT 0,
            stability_last_round INTEGER NOT NULL DEFAULT 0,
            final_status TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(job_id, source_key),
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_job_sources_quick ON job_sources(job_id, quick_status);
        CREATE INDEX IF NOT EXISTS idx_job_sources_full ON job_sources(job_id, full_status);
        CREATE TABLE IF NOT EXISTS job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            stage TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id);
        """
        with self._lock, self._connection() as connection:
            connection.executescript(schema)

    def create_job(self, job_id: str, groups: list[dict[str, Any]], config: dict[str, Any], input_name: str) -> dict[str, Any]:
        now = utc_now()
        duplicate_count = sum(int(group.get("duplicate_count") or 0) for group in groups)
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO jobs(id,status,created_at,updated_at,input_name,source_count,duplicate_count,config_json) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, "queued", now, now, input_name, len(groups), duplicate_count, json_dumps(config)),
            )
            for group in groups:
                connection.execute(
                    """INSERT INTO job_sources(job_id,source_key,source_json,variants_json,source_name,source_url,duplicate_count,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        group["source_key"],
                        json_dumps(group["source"]),
                        json_dumps(group["variants"]),
                        group["source_name"],
                        group["source_url"],
                        group["duplicate_count"],
                        now,
                    ),
                )
            connection.commit()
        self.event(job_id, "info", "queued", f"queued {len(groups)} unique sources; skipped {duplicate_count} duplicate records")
        return self.job(job_id) or {}

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def jobs_to_resume(self) -> list[str]:
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT id FROM jobs WHERE status IN ('queued','running','resuming') ORDER BY created_at").fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                connection.execute("UPDATE jobs SET status='queued', stage='resuming', updated_at=? WHERE status='running'", (utc_now(),))
                connection.commit()
        return ids

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"status", "stage", "completed_count", "passed_count", "progress", "error", "cancel_requested"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = utc_now()
        clauses = ", ".join(f"{key}=?" for key in updates)
        values = list(updates.values()) + [job_id]
        with self._lock, self._connection() as connection:
            connection.execute(f"UPDATE jobs SET {clauses} WHERE id=?", values)
            connection.commit()

    def event(self, job_id: str, level: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> None:
        safe_payload = redact(payload or {})
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO job_events(job_id,created_at,level,stage,message,payload_json) VALUES(?,?,?,?,?,?)",
                (job_id, utc_now(), level, stage, message, json_dumps(safe_payload)),
            )
            connection.commit()

    def events(self, job_id: str, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                (job_id, max(0, after_id), max(1, min(1000, limit))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            result.append(item)
        return result

    def sources(self, job_id: str, *, stage: str = "", offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        clauses = ["job_id=?"]
        values: list[Any] = [job_id]
        if stage == "quick":
            clauses.append("quick_status=?")
            values.append("passed")
        elif stage == "full":
            clauses.append("full_status=?")
            values.append("passed")
        elif stage in {"passed", "failed", "pending"}:
            clauses.append("final_status=?")
            values.append(stage)
        # SQLite binds LIMIT before OFFSET.  Keep the public arguments in the
        # familiar offset/limit order but bind them in SQL order here; the
        # previous reversal made every default listing return zero rows.
        values.extend([max(1, min(1000, limit)), max(0, offset)])
        query = f"SELECT * FROM job_sources WHERE {' AND '.join(clauses)} ORDER BY source_name, source_url LIMIT ? OFFSET ?"
        with self._lock, self._connection() as connection:
            rows = connection.execute(query, values).fetchall()
        return [dict(row) for row in rows]

    def all_sources(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT * FROM job_sources WHERE job_id=? ORDER BY source_name, source_url", (job_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_source(self, job_id: str, source_key: str, **fields: Any) -> None:
        allowed = {
            "quick_status", "quick_json", "full_status", "result_json", "stability_pass_count",
            "stability_last_round", "final_status", "last_error", "attempts",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = utc_now()
        clauses = ", ".join(f"{key}=?" for key in updates)
        values = list(updates.values()) + [job_id, source_key]
        with self._lock, self._connection() as connection:
            connection.execute(f"UPDATE job_sources SET {clauses} WHERE job_id=? AND source_key=?", values)
            connection.commit()

    def request_cancel(self, job_id: str) -> bool:
        with self._lock, self._connection() as connection:
            result = connection.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=? AND status IN ('queued','running','resuming')", (utc_now(), job_id))
            connection.commit()
            return result.rowcount > 0

    def clear_cancel(self, job_id: str) -> None:
        self.update_job(job_id, cancel_requested=0, error="")

    def is_cancel_requested(self, job_id: str) -> bool:
        job = self.job(job_id)
        return bool(job and job.get("cancel_requested"))

    def finalize_sources(self, job_id: str, rounds: int, min_pass_rounds: int) -> int:
        with self._lock, self._connection() as connection:
            connection.execute(
                """UPDATE job_sources SET final_status=CASE
                    WHEN quick_status!='passed' THEN 'failed'
                    WHEN full_status!='passed' THEN 'failed'
                    WHEN stability_pass_count>=? THEN 'passed'
                    ELSE 'failed' END, updated_at=? WHERE job_id=?""",
                (max(1, min_pass_rounds), utc_now(), job_id),
            )
            row = connection.execute("SELECT COUNT(*) AS count FROM job_sources WHERE job_id=? AND final_status='passed'", (job_id,)).fetchone()
            connection.commit()
        return int(row["count"] if row else 0)


@dataclass
class StageResult:
    passed: bool
    payload: dict[str, Any] | None
    detail: dict[str, Any]
    error: str = ""


class ValidationService:
    """Durable job queue with bounded, restart-safe stage workers."""

    def __init__(self, settings: ServerSettings | None = None, store: JobStore | None = None):
        self.settings = settings or ServerSettings.from_env()
        self.store = store or JobStore(self.settings.db_path)
        self._jobs = ThreadPoolExecutor(max_workers=self.settings.max_jobs, thread_name_prefix="readori-job")
        self._active: set[str] = set()
        self._active_lock = threading.Lock()
        self._domain_limits: dict[str, threading.BoundedSemaphore] = {}
        self._domain_lock = threading.Lock()
        for job_id in self.store.jobs_to_resume():
            self.submit(job_id)

    def close(self) -> None:
        self._jobs.shutdown(wait=False, cancel_futures=True)

    def submit(self, job_id: str) -> bool:
        with self._active_lock:
            if job_id in self._active:
                return False
            self._active.add(job_id)
        self._jobs.submit(self._run_job_guarded, job_id)
        return True

    def _run_job_guarded(self, job_id: str) -> None:
        try:
            self._run_job(job_id)
        except Exception as exc:  # keep the API alive if one job is malformed
            LOG.exception("validation job %s failed", job_id)
            self.store.update_job(job_id, status="failed", stage="failed", error=str(exc))
            self.store.event(job_id, "error", "failed", "worker failed", {"error": str(exc)})
        finally:
            with self._active_lock:
                self._active.discard(job_id)

    def _load_config(self, job: dict[str, Any]) -> dict[str, Any]:
        try:
            config = json.loads(str(job.get("config_json") or "{}"))
        except json.JSONDecodeError:
            config = {}
        config["workers"] = max(1, min(self.settings.max_workers, int(config.get("workers") or min(12, self.settings.max_workers))))
        config["domain_concurrency"] = max(1, min(8, int(config.get("domain_concurrency") or self.settings.default_domain_concurrency)))
        config["quick_timeout"] = max(0.0, min(60.0, float(config.get("quick_timeout") or 8.0)))
        config["source_timeout"] = max(0.0, min(180.0, float(config.get("source_timeout") or 30.0)))
        config["rounds"] = max(1, min(5, int(config.get("rounds") or 2)))
        config["min_pass_rounds"] = max(1, min(config["rounds"], int(config.get("min_pass_rounds") or config["rounds"])))
        config["idle_timeout"] = max(0.0, min(900.0, float(config.get("idle_timeout") or 180.0)))
        config["max_retries"] = max(0, min(3, int(config.get("max_retries") or 1)))
        config["profile"] = "device" if str(config.get("profile") or "device").lower() != "legacy" else "legacy"
        if self.settings.amd_micro:
            # Oracle VM.Standard.E2.1.Micro has 1 GB RAM and a fraction of a
            # vCPU.  Enforce a safe profile even when a remote client sends
            # high-concurrency values intended for a larger executor.
            config["workers"] = 1
            config["domain_concurrency"] = 1
            config["max_retries"] = min(config["max_retries"], 1)
            config["rounds"] = min(config["rounds"], 2)
            config["min_pass_rounds"] = min(config["min_pass_rounds"], config["rounds"])
        return config

    def _domain_slot(self, domain: str, limit: int) -> threading.BoundedSemaphore:
        with self._domain_lock:
            slot = self._domain_limits.get(domain)
            if slot is None or getattr(slot, "_value", limit) > limit:
                slot = threading.BoundedSemaphore(limit)
                self._domain_limits[domain] = slot
            return slot

    @staticmethod
    def _retryable(result: StageResult) -> bool:
        if result.passed:
            return False
        text = f"{result.error} {result.detail.get('failureReason', '')}".lower()
        if "interactive" in text or "webview" in text or "paid" in text or "missing" in text:
            return False
        return any(token in text for token in ("timeout", "connect", "network", "exception", "temporarily", "reset", "502", "503", "504"))

    def _execute_item(
        self,
        row: dict[str, Any],
        stage: str,
        timeout: float,
        retries: int,
        domain_limit: int,
        handler: Callable[[dict[str, Any], list[dict[str, Any]]], StageResult],
    ) -> StageResult:
        try:
            candidates = json.loads(str(row.get("variants_json") or "[]"))
        except json.JSONDecodeError:
            candidates = []
        if not isinstance(candidates, list):
            candidates = []
        selected = row.get("source_json")
        try:
            source = json.loads(str(selected or "{}"))
        except json.JSONDecodeError:
            source = {}
        domain = source_domain(source)
        slot = self._domain_slot(domain, domain_limit)
        with slot:
            last = StageResult(False, None, {"pipelineStage": stage, "failureReason": "worker exception"}, "no attempt")
            for attempt in range(retries + 1):
                if self.store.is_cancel_requested(str(row["job_id"])):
                    return StageResult(False, None, {"pipelineStage": stage, "failureReason": "cancelled"}, "cancel requested")
                try:
                    core.begin_source_validation_deadline(timeout)
                    last = handler(source, candidates)
                except core.SourceValidationDeadlineExceeded as exc:
                    last = StageResult(False, None, {"pipelineStage": stage, "failureReason": f"{stage} timeout"}, str(exc))
                except Exception as exc:
                    last = StageResult(False, None, {"pipelineStage": stage, "failureReason": "worker exception"}, str(exc))
                finally:
                    core.clear_source_validation_deadline()
                if last.passed or attempt >= retries or not self._retryable(last):
                    break
                time.sleep(min(0.25 * (2**attempt), 1.0))
            return last

    def _parallel_stage(
        self,
        job_id: str,
        stage: str,
        rows: list[dict[str, Any]],
        config: dict[str, Any],
        handler: Callable[[dict[str, Any], list[dict[str, Any]]], StageResult],
        on_result: Callable[[dict[str, Any], StageResult], None],
        base_progress: float,
        stage_weight: float,
        timeout: float,
    ) -> bool:
        if not rows:
            return True
        total = len(rows)
        workers = max(1, min(int(config["workers"]), total))
        iterator = iter(rows)
        inflight: dict[Future[StageResult], dict[str, Any]] = {}
        completed = 0
        passed = 0
        cancelled = False
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"readori-{stage}") as executor:
            def submit_next() -> None:
                if self.store.is_cancel_requested(job_id):
                    return
                try:
                    row = next(iterator)
                except StopIteration:
                    return
                future = executor.submit(
                    self._execute_item,
                    row,
                    stage,
                    timeout,
                    int(config["max_retries"]),
                    int(config["domain_concurrency"]),
                    handler,
                )
                inflight[future] = row

            for _ in range(workers):
                submit_next()
            while inflight:
                if self.store.is_cancel_requested(job_id):
                    cancelled = True
                    for future in inflight:
                        future.cancel()
                    break
                done, _ = wait(tuple(inflight), timeout=1.0, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    row = inflight.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = StageResult(False, None, {"pipelineStage": stage, "failureReason": "worker exception"}, str(exc))
                    on_result(row, result)
                    completed += 1
                    passed += 1 if result.passed else 0
                    progress = base_progress + stage_weight * (completed / total)
                    self.store.update_job(job_id, stage=stage, completed_count=completed, passed_count=passed, progress=min(0.99, progress))
                    if completed == 1 or completed % 10 == 0 or completed == total:
                        self.store.event(job_id, "info", stage, f"{stage}: {completed}/{total}, passed={passed}", {"completed": completed, "total": total, "passed": passed})
                    submit_next()
        return not cancelled and not self.store.is_cancel_requested(job_id)

    def _run_job(self, job_id: str) -> None:
        job = self.store.job(job_id)
        if not job:
            return
        if job.get("status") == "completed":
            return
        config = self._load_config(job)
        rows = self.store.all_sources(job_id)
        if not rows:
            self.store.update_job(job_id, status="failed", stage="failed", error="no sources")
            return
        self.store.clear_cancel(job_id)
        self.store.update_job(job_id, status="running", stage="dedupe", progress=0.0, error="")
        self.store.event(job_id, "info", "dedupe", f"dedupe complete: {len(rows)} unique source groups")

        quick_seeds: dict[str, dict[str, Any]] = {}

        def quick_handler(source: dict[str, Any], variants: list[dict[str, Any]]) -> StageResult:
            url = str(source.get("bookSourceUrl") or "")
            _, seed, detail = core.quick_scan_group(url, variants or [source])
            return StageResult(seed is not None, seed, detail, str(detail.get("error") or ""))

        def save_quick(row: dict[str, Any], result: StageResult) -> None:
            safe_detail = redact(result.detail)
            self.store.update_source(
                job_id,
                str(row["source_key"]),
                quick_status="passed" if result.passed else "failed",
                quick_json=json_dumps(safe_detail),
                last_error=str(result.error or result.detail.get("failureReason") or ""),
                attempts=int(row.get("attempts") or 0) + 1,
            )
            if result.passed and isinstance(result.payload, dict):
                quick_seeds[str(row["source_key"])] = result.payload

        quick_rows = [row for row in rows if str(row.get("quick_status")) == "pending"]
        if quick_rows:
            self.store.update_job(job_id, stage="quick-scan")
            if not self._parallel_stage(job_id, "quick-scan", quick_rows, config, quick_handler, save_quick, 0.0, 0.35, config["quick_timeout"]):
                self.store.update_job(job_id, status="cancelled", stage="cancelled")
                self.store.event(job_id, "info", "cancelled", "validation cancelled during quick scan")
                return

        rows = self.store.all_sources(job_id)

        def full_handler(source: dict[str, Any], variants: list[dict[str, Any]]) -> StageResult:
            key = canonical_source_key(source)
            seed = quick_seeds.get(key)
            _, record, detail = core.validate_group(str(source.get("bookSourceUrl") or ""), variants or [source], quick_seed=seed)
            if record is not None and config["profile"] == "device":
                compatible, reason = detail_is_device_compatible(source, detail, record)
                if not compatible:
                    detail = dict(detail)
                    detail["failureReason"] = reason
                    return StageResult(False, None, detail, reason)
            return StageResult(record is not None, record, detail, str(detail.get("error") or detail.get("failureReason") or ""))

        def save_full(row: dict[str, Any], result: StageResult) -> None:
            self.store.update_source(
                job_id,
                str(row["source_key"]),
                full_status="passed" if result.passed else "failed",
                result_json=json_dumps(result.payload) if result.passed and result.payload else "",
                stability_pass_count=1 if result.passed else 0,
                stability_last_round=1 if result.passed else 0,
                last_error=str(result.error or result.detail.get("failureReason") or ""),
                attempts=int(row.get("attempts") or 0) + 1,
            )

        full_rows = [row for row in rows if str(row.get("quick_status")) == "passed" and str(row.get("full_status")) == "pending"]
        if full_rows:
            self.store.update_job(job_id, stage="full-validation")
            if not self._parallel_stage(job_id, "full-validation", full_rows, config, full_handler, save_full, 0.35, 0.45, config["source_timeout"]):
                self.store.update_job(job_id, status="cancelled", stage="cancelled")
                self.store.event(job_id, "info", "cancelled", "validation cancelled during full validation")
                return

        # Stability rounds re-run the complete chain only for full-pass sources.
        for round_number in range(2, int(config["rounds"]) + 1):
            rows = self.store.all_sources(job_id)
            stability_rows = [
                row for row in rows
                if str(row.get("full_status")) == "passed" and int(row.get("stability_last_round") or 0) < round_number
            ]
            if not stability_rows:
                continue

            def stability_handler(source: dict[str, Any], variants: list[dict[str, Any]]) -> StageResult:
                _, record, detail = core.validate_group(str(source.get("bookSourceUrl") or ""), variants or [source])
                if record is not None and config["profile"] == "device":
                    compatible, reason = detail_is_device_compatible(source, detail, record)
                    if not compatible:
                        detail = dict(detail)
                        detail["failureReason"] = reason
                        return StageResult(False, None, detail, reason)
                return StageResult(record is not None, record, detail, str(detail.get("error") or detail.get("failureReason") or ""))

            def save_stability(row: dict[str, Any], result: StageResult) -> None:
                pass_count = int(row.get("stability_pass_count") or 0) + (1 if result.passed else 0)
                fields: dict[str, Any] = {
                    "stability_pass_count": pass_count,
                    "stability_last_round": round_number,
                    "last_error": "" if result.passed else str(result.error or result.detail.get("failureReason") or ""),
                    "attempts": int(row.get("attempts") or 0) + 1,
                }
                if result.passed and result.payload:
                    fields["result_json"] = json_dumps(result.payload)
                self.store.update_source(job_id, str(row["source_key"]), **fields)

            stage_name = f"stability-{round_number}"
            if not self._parallel_stage(job_id, stage_name, stability_rows, config, stability_handler, save_stability, 0.80, 0.20 / max(1, int(config["rounds"]) - 1), config["source_timeout"]):
                self.store.update_job(job_id, status="cancelled", stage="cancelled")
                self.store.event(job_id, "info", "cancelled", f"validation cancelled during {stage_name}")
                return

        if self.store.is_cancel_requested(job_id):
            self.store.update_job(job_id, status="cancelled", stage="cancelled")
            return
        passed = self.store.finalize_sources(job_id, int(config["rounds"]), int(config["min_pass_rounds"]))
        total = len(rows)
        self.store.update_job(job_id, status="completed", stage="completed", completed_count=total, passed_count=passed, progress=1.0, error="", cancel_requested=0)
        self.store.event(job_id, "info", "completed", f"validation completed: {passed}/{total} device-ready sources", {"passed": passed, "total": total})

    def resume(self, job_id: str) -> bool:
        job = self.store.job(job_id)
        if not job or job.get("status") not in {"cancelled", "failed", "queued", "resuming"}:
            return False
        self.store.clear_cancel(job_id)
        self.store.update_job(job_id, status="queued", stage="resuming", error="")
        self.store.event(job_id, "info", "resuming", "queued remaining checkpoints for resume")
        return self.submit(job_id)

    def cancel(self, job_id: str) -> bool:
        changed = self.store.request_cancel(job_id)
        if changed:
            self.store.event(job_id, "info", "cancelling", "cancellation requested; in-flight requests finish at their bounded timeout")
        return changed

    def result(self, job_id: str) -> list[dict[str, Any]]:
        entries: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
        # ``JobStore.sources`` intentionally caps one API page at 1000 rows;
        # result downloads must still include all passed sources in a 1000+
        # source job, so walk pages instead of silently truncating the export.
        offset = 0
        page_size = 1000
        while True:
            page = self.store.sources(job_id, stage="passed", offset=offset, limit=page_size)
            if not page:
                break
            for row in page:
                try:
                    source = json.loads(str(row.get("result_json") or ""))
                except json.JSONDecodeError:
                    continue
                if isinstance(source, dict):
                    entries.append((str(row.get("source_key") or ""), source, None))
            if len(page) < page_size:
                break
            offset += page_size
        output, _ = core.aggregate_validated_sources(entries)
        for source in output:
            source.pop("__readoriValidation", None)
        return output


if BaseModel is not None:
    class JobConfigModel(BaseModel):
        workers: int = Field(default=0, ge=0, le=64)
        domain_concurrency: int = Field(default=2, ge=1, le=8)
        quick_timeout: float = Field(default=8.0, ge=0, le=60)
        source_timeout: float = Field(default=30.0, ge=0, le=180)
        rounds: int = Field(default=2, ge=1, le=5)
        min_pass_rounds: int = Field(default=2, ge=1, le=5)
        idle_timeout: float = Field(default=180.0, ge=0, le=900)
        max_retries: int = Field(default=1, ge=0, le=3)
        profile: str = Field(default="device", pattern="^(device|legacy)$")


    class CreateJobModel(BaseModel):
        sources: list[dict[str, Any]] | None = None
        input_path: str | None = None
        input_name: str = "upload.json"
        config: JobConfigModel = Field(default_factory=JobConfigModel)


def _payload_sources(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("sources") or payload.get("bookSources") or payload.get("data")
    if not isinstance(payload, list):
        raise ValueError("JSON must be an array of Legado sources or an object containing sources")
    return [item for item in payload if isinstance(item, dict)]


def create_app(settings: ServerSettings | None = None) -> Any:
    if FastAPI is None:
        raise RuntimeError("服务器依赖未安装，请执行 pip install -r server/requirements.txt")
    runtime_settings = settings or ServerSettings.from_env()
    service = ValidationService(runtime_settings)
    @asynccontextmanager
    async def lifespan(_app: Any):
        try:
            yield
        finally:
            service.close()

    app = FastAPI(title="Readori Source Validation Service", version="1.0", lifespan=lifespan)
    app.state.validation_service = service
    app.state.validation_settings = runtime_settings

    def authorize(request: Request) -> None:
        configured = runtime_settings.api_key
        # Local development may run without a key; a configured key is always
        # required for non-health endpoints and is compared in constant time.
        if not configured:
            return
        supplied = request.headers.get("x-api-key", "")
        if not supplied:
            auth = request.headers.get("authorization", "")
            supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied, configured):
            raise HTTPException(status_code=401, detail="invalid API key")

    def load_input_path(path_text: str) -> tuple[list[dict[str, Any]], str]:
        path = Path(path_text).expanduser().resolve()
        if runtime_settings.allowed_input_root is not None and not path.is_relative_to(runtime_settings.allowed_input_root):
            raise HTTPException(status_code=403, detail="input path is outside READORI_VALIDATOR_INPUT_ROOT")
        if not path.exists():
            raise HTTPException(status_code=400, detail="input path does not exist")
        try:
            sources, names = core.load_sources_from_paths([path])
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"could not load source JSON: {exc}") from exc
        return sources, ", ".join(names[:3])

    def enqueue(sources: list[dict[str, Any]], config: dict[str, Any], input_name: str) -> dict[str, Any]:
        if len(sources) > runtime_settings.max_sources:
            raise HTTPException(status_code=413, detail=f"source limit exceeded ({runtime_settings.max_sources})")
        groups = prepare_source_groups(sources)
        if not groups:
            raise HTTPException(status_code=400, detail="no usable bookSourceUrl records found")
        job_id = uuid.uuid4().hex
        job = service.store.create_job(job_id, groups, config, input_name)
        service.submit(job_id)
        return {"job": service.store.job(job_id), "deduplicated": len(sources) - len(groups)}

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "readori-source-validator", "time": utc_now()}

    @app.post("/v1/jobs", dependencies=[Depends(authorize)])
    def create_job(request: CreateJobModel) -> dict[str, Any]:
        sources = request.sources or []
        input_name = request.input_name
        if request.input_path:
            sources, input_name = load_input_path(request.input_path)
        config = _model_dump(request.config)
        return enqueue(sources, config, input_name)

    @app.post("/v1/jobs/upload", dependencies=[Depends(authorize)])
    async def upload_job(file: UploadFile = File(...), config: str = Form("{}")) -> dict[str, Any]:
        body = await file.read(runtime_settings.max_upload_bytes + 1)
        if len(body) > runtime_settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="upload is too large")
        try:
            sources = _payload_sources(json.loads(body.decode("utf-8-sig")))
            config_model = JobConfigModel.model_validate(json.loads(config or "{}")) if hasattr(JobConfigModel, "model_validate") else JobConfigModel.parse_obj(json.loads(config or "{}"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid source JSON or config: {exc}") from exc
        return enqueue(sources, _model_dump(config_model), file.filename or "upload.json")

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(authorize)])
    def get_job(job_id: str) -> Any:
        job = service.store.job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.get("/v1/jobs/{job_id}/sources", dependencies=[Depends(authorize)])
    def get_sources(job_id: str, stage: str = Query(""), offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        if not service.store.job(job_id):
            raise HTTPException(status_code=404, detail="job not found")
        rows = service.store.sources(job_id, stage=stage, offset=offset, limit=limit)
        return {
            "items": [
                {
                    "sourceKey": row["source_key"],
                    "sourceName": row["source_name"],
                    "sourceUrl": row["source_url"],
                    "duplicateCount": row["duplicate_count"],
                    "quickStatus": row["quick_status"],
                    "fullStatus": row["full_status"],
                    "stabilityPassCount": row["stability_pass_count"],
                    "finalStatus": row["final_status"],
                    "lastError": row["last_error"],
                }
                for row in rows
            ],
            "offset": offset,
            "limit": limit,
        }

    @app.get("/v1/jobs/{job_id}/events", dependencies=[Depends(authorize)])
    def get_events(job_id: str, after_id: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
        if not service.store.job(job_id):
            raise HTTPException(status_code=404, detail="job not found")
        return {"items": service.store.events(job_id, after_id, limit)}

    @app.get("/v1/jobs/{job_id}/result", dependencies=[Depends(authorize)])
    def get_result(job_id: str) -> Any:
        job = service.store.job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if job.get("status") != "completed":
            raise HTTPException(status_code=409, detail="job is not completed")
        sources = service.result(job_id)
        return {"jobId": job_id, "sourceCount": len(sources), "sourceGroupCount": job["passed_count"], "sources": sources}

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(authorize)])
    def cancel_job(job_id: str) -> Any:
        if not service.store.job(job_id):
            raise HTTPException(status_code=404, detail="job not found")
        service.cancel(job_id)
        return service.store.job(job_id)

    @app.post("/v1/jobs/{job_id}/resume", dependencies=[Depends(authorize)])
    def resume_job(job_id: str) -> Any:
        if not service.resume(job_id):
            raise HTTPException(status_code=409, detail="job cannot be resumed in its current state")
        return service.store.job(job_id)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Readori server-side source validation service")
    parser.add_argument("--host", default=os.environ.get("READORI_VALIDATOR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("READORI_VALIDATOR_PORT", "8787")))
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args(argv)
    settings = ServerSettings.from_env()
    if args.db is not None or args.api_key is not None:
        settings = ServerSettings(
            db_path=(args.db or settings.db_path).expanduser().resolve(),
            api_key=settings.api_key if args.api_key is None else args.api_key,
            allowed_input_root=settings.allowed_input_root,
            max_sources=settings.max_sources,
            max_workers=settings.max_workers,
            max_jobs=settings.max_jobs,
            default_domain_concurrency=settings.default_domain_concurrency,
            max_upload_bytes=settings.max_upload_bytes,
            amd_micro=settings.amd_micro,
        )
    if FastAPI is None:
        print("服务器依赖未安装，请执行 pip install -r server/requirements.txt")
        return 2
    import uvicorn

    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
