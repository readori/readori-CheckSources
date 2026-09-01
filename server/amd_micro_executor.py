"""Single-concurrency Cloudflare Queue Pull executor for an AMD Micro VM.

The control plane owns durable task state and artifacts.  This process only
leases one job, runs the existing local validation service with the AMD-safe
profile, periodically reports bounded summaries, and acknowledges or retries
the Queue message.  It deliberately does not start FastAPI or a GUI.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import gc
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Iterable

import requests

from .source_validator_server import JobStore, ServerSettings, ValidationService, prepare_source_groups


LOG = logging.getLogger("readori.validator.amd_micro")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _chunks(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _decode_queue_body(value: Any) -> dict[str, Any] | None:
    """Decode JSON/bytes pull messages, including Cloudflare's base64 form."""

    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value).decode("utf-8", errors="replace")
    elif isinstance(value, str):
        raw = value
    else:
        return None
    try:
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, dict) else None
    except json.JSONDecodeError:
        pass
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = json.loads(base64.b64decode(padded, validate=True).decode("utf-8"))
        return decoded if isinstance(decoded, dict) else None
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None


class QueueClient:
    def __init__(self, account_id: str, queue_id: str, token: str, session: requests.Session | None = None):
        if not account_id or not queue_id or not token:
            raise ValueError("READORI_CF_ACCOUNT_ID, READORI_CF_QUEUE_ID and READORI_CF_QUEUE_API_TOKEN are required")
        self.endpoint = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/queues/{queue_id}/messages"
        self.session = session or requests.Session()
        self.headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}

    def pull(self, visibility_timeout_ms: int, batch_size: int = 1) -> list[dict[str, Any]]:
        response = self.session.post(
            f"{self.endpoint}/pull",
            headers=self.headers,
            json={"visibility_timeout_ms": visibility_timeout_ms, "batch_size": max(1, min(1, batch_size))},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("success") is False:
            raise RuntimeError(f"queue pull failed: {payload.get('errors')}")
        result = payload.get("result") or {}
        messages = result.get("messages") or []
        return [message for message in messages if isinstance(message, dict)]

    def acknowledge(self, lease_ids: list[str]) -> None:
        if not lease_ids:
            return
        response = self.session.post(
            f"{self.endpoint}/ack",
            headers=self.headers,
            json={"acks": [{"lease_id": lease_id} for lease_id in lease_ids], "retries": []},
            timeout=30,
        )
        response.raise_for_status()

    def retry(self, lease_ids: list[str], delay_seconds: int = 30) -> None:
        if not lease_ids:
            return
        response = self.session.post(
            f"{self.endpoint}/ack",
            headers=self.headers,
            json={
                "acks": [],
                "retries": [{"lease_id": lease_id, "delay_seconds": max(1, min(86400, delay_seconds))} for lease_id in lease_ids],
            },
            timeout=30,
        )
        response.raise_for_status()


class ControlPlaneClient:
    def __init__(self, base_url: str, token: str, executor_id: str, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.executor_id = executor_id
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}", "x-executor-token": self.token, "x-executor-id": self.executor_id}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        headers.update(self._headers())
        response = self.session.request(method, f"{self.base_url}{path}", headers=headers, timeout=kwargs.pop("timeout", 30), **kwargs)
        if not response.ok:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:500]
            raise RuntimeError(f"control plane {method} {path} failed ({response.status_code}): {detail}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return response.text

    def job(self, job_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/internal/jobs/{job_id}")
        return payload if isinstance(payload, dict) else {}

    def claim(self, job_id: str) -> dict[str, Any]:
        try:
            payload = self._request("POST", f"/internal/jobs/{job_id}/claim", json={})
        except RuntimeError as error:
            # A redelivered Queue message can point at a job already leased by
            # another executor or completed after a network retry.  Treat the
            # conflict as a safe no-op instead of retrying a poison message.
            if "(409)" in str(error):
                return {"claimed": False}
            raise
        return payload if isinstance(payload, dict) else {}

    def input_sources(self, job_id: str) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/internal/jobs/{job_id}/input", timeout=60)
        if isinstance(payload, dict):
            payload = payload.get("sources") or payload.get("bookSources") or payload.get("data")
        if not isinstance(payload, list):
            raise ValueError("cloud input must be an array or an object containing sources")
        return [item for item in payload if isinstance(item, dict)]

    def progress(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", f"/internal/jobs/{job_id}/progress", json=payload, timeout=30)
        return result if isinstance(result, dict) else {}

    def upload_result(self, job_id: str, path: Path, count: int) -> dict[str, Any]:
        with path.open("rb") as stream:
            result = self._request(
                "POST",
                f"/internal/jobs/{job_id}/result",
                data=stream,
                headers={"content-type": "application/json", "x-source-count": str(count)},
                timeout=120,
            )
        return result if isinstance(result, dict) else {}

    def fail(self, job_id: str, error: str, retryable: bool) -> dict[str, Any]:
        result = self._request("POST", f"/internal/jobs/{job_id}/fail", json={"error": error[:500], "retryable": retryable})
        return result if isinstance(result, dict) else {}

    def cancelled(self, job_id: str) -> dict[str, Any]:
        result = self._request("POST", f"/internal/jobs/{job_id}/cancelled", json={})
        return result if isinstance(result, dict) else {}


def amd_config(config: Any) -> dict[str, Any]:
    """Clamp a browser-provided config to the AMD Micro safety profile."""

    requested = dict(config) if isinstance(config, dict) else {}
    rounds = max(1, min(2, int(requested.get("rounds") or 2)))
    return {
        "workers": 1,
        "domain_concurrency": 1,
        "quick_timeout": max(1.0, min(15.0, float(requested.get("quick_timeout") or 8.0))),
        "source_timeout": max(5.0, min(45.0, float(requested.get("source_timeout") or 30.0))),
        "rounds": rounds,
        "min_pass_rounds": max(1, min(rounds, int(requested.get("min_pass_rounds") or rounds))),
        "idle_timeout": max(15.0, min(300.0, float(requested.get("idle_timeout") or 180.0))),
        "max_retries": max(0, min(1, int(requested.get("max_retries") or 1))),
        "profile": "device",
        "executor_profile": "amd-micro",
    }


def source_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sourceKey": str(row.get("source_key") or "")[:512],
        "sourceName": str(row.get("source_name") or "")[:300],
        "sourceUrl": str(row.get("source_url") or "")[:2048],
        "duplicateCount": int(row.get("duplicate_count") or 0),
        "quickStatus": str(row.get("quick_status") or "pending"),
        "fullStatus": str(row.get("full_status") or "pending"),
        "stabilityPassCount": int(row.get("stability_pass_count") or 0),
        "finalStatus": str(row.get("final_status") or "pending"),
        "lastError": str(row.get("last_error") or "")[:500],
    }


class AMDMicroExecutor:
    def __init__(self, control: ControlPlaneClient, queue: QueueClient, work_dir: Path, poll_seconds: float = 2.0, retain_completed: bool = True):
        self.control = control
        self.queue = queue
        self.work_dir = work_dir.expanduser().resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.poll_seconds = max(1.0, min(15.0, poll_seconds))
        self.retain_completed = retain_completed

    def _report(self, job_id: str, store: JobStore, last_event: int, force: bool = False) -> tuple[int, dict[str, Any]]:
        local_job = store.job(job_id) or {}
        rows = store.all_sources(job_id)
        events = store.events(job_id, after_id=last_event, limit=1000)
        payload = {
            "status": "running" if local_job.get("status") in {"queued", "running", "resuming"} else local_job.get("status", "running"),
            "stage": local_job.get("stage", "running"),
            "progress": max(0.0, min(0.99, float(local_job.get("progress") or 0.0))),
            "totalSources": int(local_job.get("source_count") or len(rows)),
            "duplicateCount": int(local_job.get("duplicate_count") or 0),
            "completedCount": int(local_job.get("completed_count") or 0),
            "passedCount": int(local_job.get("passed_count") or 0),
            "sources": [source_summary(row) for row in rows],
            "events": [
                {"id": event.get("id"), "level": event.get("level"), "stage": event.get("stage"), "message": event.get("message"), "payload": event.get("payload", {})}
                for event in events
            ],
        }
        if force or events or rows or local_job:
            # The Worker caps each request to 300 sources.  Send multiple
            # bounded requests so a 1 GB executor never needs a giant cloud
            # progress payload.
            source_batches = list(_chunks(payload["sources"], 250)) or [[]]
            for index, batch in enumerate(source_batches):
                piece = dict(payload)
                piece["sources"] = batch
                piece["events"] = payload["events"] if index == 0 else []
                self.control.progress(job_id, piece)
        if events:
            last_event = max(last_event, max(int(event.get("id") or 0) for event in events))
        return last_event, local_job

    def _run_local_job(self, job_id: str, cloud_job: dict[str, Any], sources: list[dict[str, Any]]) -> tuple[JobStore, ValidationService]:
        db_path = self.work_dir / "jobs" / f"{job_id}.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        settings = ServerSettings(
            db_path=db_path,
            max_sources=20000,
            max_workers=1,
            max_jobs=1,
            default_domain_concurrency=1,
            max_upload_bytes=64 * 1024 * 1024,
            amd_micro=True,
        )
        store = JobStore(db_path)
        service = ValidationService(settings, store)
        local_job = store.job(job_id)
        if not local_job:
            groups = prepare_source_groups(sources)
            if not groups:
                service.close()
                raise ValueError("no usable bookSourceUrl records found")
            config = amd_config(cloud_job.get("config"))
            store.create_job(job_id, groups, config, str(cloud_job.get("inputName") or "cloud-input.json"))
            service.submit(job_id)
        elif local_job.get("status") != "completed":
            service.submit(job_id)
        return store, service

    def process_message(self, message: dict[str, Any]) -> str:
        lease_id = str(message.get("lease_id") or "")
        job_payload = _decode_queue_body(message.get("body"))
        if not lease_id or not job_payload:
            if lease_id:
                self.queue.acknowledge([lease_id])
            return "discarded-invalid-message"
        job_id = str(job_payload.get("jobId") or "").strip()
        if not job_id:
            self.queue.acknowledge([lease_id])
            return "discarded-missing-job"
        attempts = max(1, int(message.get("attempts") or 1))
        max_attempts = _env_int("READORI_AMD_MAX_ATTEMPTS", 3, 1, 10)
        store: JobStore | None = None
        service: ValidationService | None = None
        try:
            cloud_job = self.control.claim(job_id)
            if cloud_job.get("claimed") is False:
                self.queue.acknowledge([lease_id])
                return "skipped-already-leased"
            sources = self.control.input_sources(job_id)
            store, service = self._run_local_job(job_id, cloud_job, sources)
            last_event = 0
            last_signature: tuple[Any, ...] | None = None
            last_report = 0.0
            while True:
                current = self.control.job(job_id)
                if current.get("cancelRequested") and not bool((store.job(job_id) or {}).get("cancel_requested")):
                    service.cancel(job_id)
                local = store.job(job_id) or {}
                signature = (local.get("status"), local.get("stage"), local.get("completed_count"), local.get("passed_count"))
                if signature != last_signature or time.monotonic() - last_report >= 20:
                    last_event, _ = self._report(job_id, store, last_event, force=True)
                    last_signature = signature
                    last_report = time.monotonic()
                status = str(local.get("status") or "")
                if status == "completed":
                    result = self._write_result(job_id, service, store)
                    self.control.upload_result(job_id, result, self._result_count(result))
                    self.queue.acknowledge([lease_id])
                    return "completed"
                if status == "cancelled":
                    self.control.cancelled(job_id)
                    self.queue.acknowledge([lease_id])
                    return "cancelled"
                if status == "failed":
                    error = str(local.get("error") or "local validator failed")
                    retryable = attempts < max_attempts
                    self.control.fail(job_id, error, retryable=retryable)
                    if retryable:
                        self.queue.retry([lease_id], delay_seconds=min(300, 30 * attempts))
                    else:
                        self.queue.acknowledge([lease_id])
                    return "failed-retry" if retryable else "failed-final"
                time.sleep(self.poll_seconds)
        except Exception as error:
            LOG.exception("AMD Micro job %s failed", job_id)
            try:
                self.control.fail(job_id, str(error), retryable=attempts < max_attempts)
            except Exception:
                LOG.exception("could not report failure for %s", job_id)
            try:
                if attempts < max_attempts:
                    self.queue.retry([lease_id], delay_seconds=min(300, 30 * attempts))
                else:
                    self.queue.acknowledge([lease_id])
            except Exception:
                LOG.exception("could not settle Queue lease for %s", job_id)
            return "exception-retry" if attempts < max_attempts else "exception-final"
        finally:
            if service is not None:
                service.close()
            gc.collect()

    def _write_result(self, job_id: str, service: ValidationService, store: JobStore) -> Path:
        result_path = self.work_dir / "results" / f"{job_id}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        sources = service.result(job_id)
        with result_path.open("w", encoding="utf-8") as stream:
            json.dump({"jobId": job_id, "sourceCount": len(sources), "sources": sources}, stream, ensure_ascii=False, separators=(",", ":"))
        return result_path

    @staticmethod
    def _result_count(path: Path) -> int:
        try:
            with path.open("r", encoding="utf-8") as stream:
                return max(0, int(json.load(stream).get("sourceCount") or 0))
        except (OSError, ValueError, TypeError):
            return 0

    def run_forever(self) -> None:
        visibility = _env_int("READORI_CF_QUEUE_VISIBILITY_TIMEOUT_MS", 43200000, 60000, 43200000)
        idle_sleep = max(1.0, min(30.0, float(os.environ.get("READORI_AMD_IDLE_SLEEP", "3"))))
        LOG.info("AMD Micro executor %s started; batch_size=1 workers=1", self.control.executor_id)
        while True:
            try:
                messages = self.queue.pull(visibility, batch_size=1)
            except Exception:
                LOG.exception("Queue pull failed; retrying after %.1fs", idle_sleep)
                time.sleep(idle_sleep)
                continue
            if not messages:
                time.sleep(idle_sleep)
                continue
            # The queue client enforces batch_size=1.  Keep the loop explicit
            # so a future config change cannot create parallel work on 1 GB.
            self.process_message(messages[0])


def build_executor_from_env(work_dir: Path, poll_seconds: float, retain_completed: bool) -> AMDMicroExecutor:
    executor_id = os.environ.get("READORI_AMD_EXECUTOR_ID", "").strip() or f"amd-micro-{socket.gethostname()}"
    control = ControlPlaneClient(
        os.environ.get("READORI_AMD_EXECUTOR_BASE_URL", ""),
        os.environ.get("READORI_AMD_EXECUTOR_TOKEN", ""),
        executor_id,
    )
    queue = QueueClient(
        os.environ.get("READORI_CF_ACCOUNT_ID", ""),
        os.environ.get("READORI_CF_QUEUE_ID", ""),
        os.environ.get("READORI_CF_QUEUE_API_TOKEN", ""),
        control.session,
    )
    return AMDMicroExecutor(control, queue, work_dir, poll_seconds=poll_seconds, retain_completed=retain_completed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Cloudflare Queue task at a time on an AMD Micro VM")
    parser.add_argument("--work-dir", type=Path, default=Path(os.environ.get("READORI_AMD_WORK_DIR", "/var/lib/readori-validator")))
    parser.add_argument("--poll-seconds", type=float, default=float(os.environ.get("READORI_AMD_POLL_SECONDS", "2")))
    parser.add_argument("--retain-completed", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--log-level", default=os.environ.get("READORI_AMD_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    try:
        build_executor_from_env(args.work_dir, args.poll_seconds, args.retain_completed).run_forever()
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        LOG.error("executor could not start: %s", error)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
