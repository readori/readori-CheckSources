"""R145a: server-side security and robustness fixes.

Covers input_path confinement, streamed upload limits, the SSRF gate on source hosts, queued-job cancellation,
the per-domain limiter, and the ``redact`` regex.  The validation core is always replaced by fakes: nothing here
touches the network.
"""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from server import source_validator_server as server
from validator import net_guard

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402  (after importorskip)
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402


def _source(url: str, name: str = "Example") -> dict:
    return {
        "bookSourceName": name,
        "bookSourceUrl": url,
        "searchUrl": "/search?q={{key}}",
        "ruleSearch": {"bookList": "//book", "name": "//title"},
    }


def _wait_for_status(service: "server.ValidationService", job_id: str, wanted: set[str], seconds: float = 8.0) -> dict:
    deadline = time.monotonic() + seconds
    job = service.store.job(job_id)
    while time.monotonic() < deadline:
        job = service.store.job(job_id)
        if job and job["status"] in wanted:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} stayed {job and job['status']}, wanted {wanted}")


def _drain(service: "server.ValidationService", seconds: float = 8.0) -> None:
    """Wait for background job threads; a leftover thread would call the *next* test's fake engine."""

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with service._active_lock:
            if not service._active:
                return
        time.sleep(0.02)


def _fake_core(monkeypatch, calls: list[str] | None = None, gate: threading.Event | None = None):
    """Replace the engine: quick scan fails fast (or waits on ``gate``), so jobs finish without any network."""

    def fake_quick(url: str, candidates: list[dict]) -> tuple[str, None, dict]:
        if calls is not None:
            calls.append(url)
        if gate is not None:
            gate.wait(10)
        return url, None, {"pipelineStage": "quick-scan", "failureReason": "no search/explore result"}

    monkeypatch.setattr(server.core, "quick_scan_group", fake_quick)
    monkeypatch.setattr(server.core, "validate_group", lambda *a, **k: (a[0], None, {}))


def _settings(tmp_path: Path, **overrides) -> "server.ServerSettings":
    values = dict(db_path=tmp_path / "data" / "validator.sqlite3", max_workers=1, max_jobs=1, default_domain_concurrency=1)
    values.update(overrides)
    return server.ServerSettings(**values)


@pytest.fixture()
def client_factory(monkeypatch, tmp_path):
    _fake_core(monkeypatch)
    clients: list[TestClient] = []

    def make(**overrides) -> TestClient:
        # The SSRF gate resolves hostnames; keep the suite offline and deterministic.
        monkeypatch.setattr(net_guard, "default_resolver", lambda host: ["93.184.216.34"])
        app = server.create_app(_settings(tmp_path, **overrides))
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        _drain(client.app.state.validation_service)
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------- redact


def test_redact_keeps_author_but_hides_secrets() -> None:
    payload = {
        "author": "某作者",
        "authors": ["a"],
        "bookAuthor": "b",
        "authority": "c",
        "nested": {"author": "d", "token": "t", "Authorization": "Bearer x", "auth": "z", "auth_token": "y"},
        "cookie": "c=1",
        "apiKey": "k",
        "api_key": "k",
        "Password": "p",
        "sessionId": "s",
    }
    out = server.redact(payload)
    assert out["author"] == "某作者"
    assert out["authors"] == ["a"]
    assert out["bookAuthor"] == "b"
    assert out["authority"] == "c"
    assert out["nested"]["author"] == "d"
    for hidden in (out["nested"]["token"], out["nested"]["Authorization"], out["nested"]["auth"], out["nested"]["auth_token"],
                   out["cookie"], out["apiKey"], out["api_key"], out["Password"], out["sessionId"]):
        assert hidden == "[redacted]"


# ---------------------------------------------------------------------------------------- input_path


def test_input_root_defaults_to_inputs_under_data_dir(tmp_path: Path) -> None:
    settings = server.ServerSettings(db_path=tmp_path / "data" / "validator.sqlite3")
    assert settings.allowed_input_root is None
    assert settings.input_root() == (tmp_path / "data" / "inputs").resolve()
    configured = server.ServerSettings(db_path=tmp_path / "x.sqlite3", allowed_input_root=tmp_path / "custom")
    assert configured.input_root() == (tmp_path / "custom").resolve()


def test_from_env_leaves_root_unset_and_reads_private_network_switch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("READORI_VALIDATOR_INPUT_ROOT", raising=False)
    monkeypatch.delenv(net_guard.ALLOW_PRIVATE_ENV, raising=False)
    monkeypatch.setenv("READORI_VALIDATOR_DB", str(tmp_path / "env" / "v.sqlite3"))
    settings = server.ServerSettings.from_env()
    assert settings.allowed_input_root is None
    assert settings.input_root() == (tmp_path / "env" / "inputs").resolve()
    assert settings.allow_private_network is False
    monkeypatch.setenv(net_guard.ALLOW_PRIVATE_ENV, "1")
    assert server.ServerSettings.from_env().allow_private_network is True


def test_input_path_outside_default_root_is_refused(client_factory, tmp_path: Path) -> None:
    client = client_factory()
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps([_source("https://example.com")]), encoding="utf-8")
    for path in (str(outside), str(tmp_path / "data" / "validator.sqlite3"), "/etc/passwd", "C:/Windows/win.ini"):
        response = client.post("/v1/jobs", json={"input_path": path})
        assert response.status_code == 403, path


def test_input_path_dotdot_traversal_is_refused(client_factory, tmp_path: Path) -> None:
    client = client_factory()
    inputs = tmp_path / "data" / "inputs"
    inputs.mkdir(parents=True)
    (tmp_path / "secret.json").write_text(json.dumps([_source("https://example.com")]), encoding="utf-8")
    for path in ("../../secret.json", str(inputs / ".." / ".." / "secret.json"), "..\\..\\secret.json"):
        response = client.post("/v1/jobs", json={"input_path": path})
        assert response.status_code in {400, 403}, path
        assert response.status_code != 200


def test_input_path_symlink_escape_is_refused(client_factory, tmp_path: Path) -> None:
    client = client_factory()
    inputs = tmp_path / "data" / "inputs"
    inputs.mkdir(parents=True)
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps([_source("https://example.com")]), encoding="utf-8")
    link = inputs / "link.json"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")
    response = client.post("/v1/jobs", json={"input_path": str(link)})
    assert response.status_code == 403
    assert client.post("/v1/jobs", json={"input_path": "link.json"}).status_code == 403


def test_input_path_junction_escape_is_refused(client_factory, tmp_path: Path) -> None:
    """Windows: a directory junction (no privilege needed) is the symlink-equivalent escape."""

    import os
    import subprocess

    if os.name != "nt":
        pytest.skip("junctions exist on Windows only; the symlink test covers POSIX")
    client = client_factory()
    inputs = tmp_path / "data" / "inputs"
    inputs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text(json.dumps([_source("https://example.com")]), encoding="utf-8")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(inputs / "jx"), str(outside)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("could not create a junction")
    assert client.post("/v1/jobs", json={"input_path": "jx/secret.json"}).status_code == 403
    assert client.post("/v1/jobs", json={"input_path": str(inputs / "jx" / "secret.json")}).status_code == 403


def test_input_path_inside_root_is_accepted(client_factory, tmp_path: Path) -> None:
    client = client_factory()
    inputs = tmp_path / "data" / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "ok.json").write_text(json.dumps([_source("https://example.com", "A")]), encoding="utf-8")
    absolute = client.post("/v1/jobs", json={"input_path": str(inputs / "ok.json"), "config": {"rounds": 1, "min_pass_rounds": 1}})
    assert absolute.status_code == 200, absolute.text
    assert absolute.json()["job"]["source_count"] == 1
    relative = client.post("/v1/jobs", json={"input_path": "ok.json", "config": {"rounds": 1, "min_pass_rounds": 1}})
    assert relative.status_code == 200, relative.text


def test_input_path_directory_and_missing_file_are_400(client_factory, tmp_path: Path) -> None:
    client = client_factory()
    inputs = tmp_path / "data" / "inputs"
    (inputs / "sub").mkdir(parents=True)
    assert client.post("/v1/jobs", json={"input_path": str(inputs / "sub")}).status_code == 400
    assert client.post("/v1/jobs", json={"input_path": "nope.json"}).status_code == 400


def test_configured_input_root_replaces_the_default(client_factory, tmp_path: Path) -> None:
    root = tmp_path / "custom-root"
    root.mkdir()
    (root / "a.json").write_text(json.dumps([_source("https://example.com")]), encoding="utf-8")
    client = client_factory(allowed_input_root=root)
    assert client.post("/v1/jobs", json={"input_path": str(root / "a.json")}).status_code == 200
    default_dir = tmp_path / "data" / "inputs"
    default_dir.mkdir(parents=True)
    (default_dir / "b.json").write_text(json.dumps([_source("https://example.com")]), encoding="utf-8")
    assert client.post("/v1/jobs", json={"input_path": str(default_dir / "b.json")}).status_code == 403


# ------------------------------------------------------------------------------------------ uploads


def _upload(client: TestClient, payload: bytes, name: str = "s.json"):
    return client.post("/v1/jobs/upload", files={"file": (name, payload, "application/json")})


def test_upload_within_limit_is_accepted(client_factory) -> None:
    client = client_factory(max_upload_bytes=4096)
    body = json.dumps([_source("https://example.com")]).encode()
    assert len(body) < 4096
    response = _upload(client, body)
    assert response.status_code == 200, response.text
    assert response.json()["job"]["source_count"] == 1


def test_upload_over_limit_is_413_from_the_streaming_read(client_factory) -> None:
    client = client_factory(max_upload_bytes=1024)
    big = json.dumps([_source(f"https://example-{i}.com") for i in range(60)]).encode()
    assert 1024 < len(big) < 500_000
    response = _upload(client, big)
    assert response.status_code == 413
    assert "too large" in response.json()["detail"]


def test_upload_far_over_limit_is_rejected_by_the_body_cap(client_factory) -> None:
    client = client_factory(max_upload_bytes=1024)
    response = _upload(client, b"[" + b" " * (3 * 1024 * 1024) + b"]")
    assert response.status_code == 413


def test_chunked_body_without_content_length_is_capped(client_factory) -> None:
    client = client_factory(max_upload_bytes=1024)

    def chunks():
        for _ in range(48):
            yield b" " * 65536

    response = client.post("/v1/jobs", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_normal_json_requests_are_unaffected_by_the_cap(client_factory) -> None:
    client = client_factory(max_upload_bytes=4096)
    response = client.post("/v1/jobs", json={"sources": [_source("https://example.com")], "config": {"rounds": 1, "min_pass_rounds": 1}})
    assert response.status_code == 200, response.text


def test_body_limit_middleware_stops_reading_at_the_limit() -> None:
    """The inner app must see the abort as soon as the cap is crossed, not after the whole body arrived."""

    consumed: list[int] = []
    sent: list[dict] = []

    async def inner(scope, receive, send):
        while True:
            message = await receive()
            consumed.append(len(message.get("body", b"")))
            if not message.get("more_body"):
                break

    middleware = server.BodyLimitMiddleware(inner, max_bytes=100)
    chunks = [{"type": "http.request", "body": b"x" * 40, "more_body": True} for _ in range(10)]
    chunks.append({"type": "http.request", "body": b"", "more_body": False})
    feed = iter(chunks)

    async def receive():
        return next(feed)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    with pytest.raises(StarletteHTTPException) as info:
        asyncio.run(middleware(scope, receive, send))
    assert info.value.status_code == 413
    assert sum(consumed) == 80  # two chunks accepted, the third (120 bytes total) aborted
    assert len(list(feed)) == 8  # the rest of the body was never pulled


def test_body_limit_middleware_rejects_declared_oversize_without_reading() -> None:
    sent: list[dict] = []
    reads: list[int] = []

    async def inner(scope, receive, send):  # pragma: no cover - must not run
        raise AssertionError("inner app must not run")

    async def receive():
        reads.append(1)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"101")]}
    asyncio.run(server.BodyLimitMiddleware(inner, max_bytes=100)(scope, receive, send))
    assert sent[0]["status"] == 413
    assert not reads


# ------------------------------------------------------------------------------------- SSRF gate


def _run_single_job(service: "server.ValidationService", sources: list[dict], job_id: str = "ssrf-job") -> dict:
    groups = server.prepare_source_groups(sources)
    service.store.create_job(job_id, groups, {"rounds": 1, "min_pass_rounds": 1, "profile": "device"}, "t.json")
    assert service.submit(job_id)
    return _wait_for_status(service, job_id, {"completed", "failed", "cancelled"})


def test_sources_pointing_at_private_hosts_never_reach_the_engine(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    _fake_core(monkeypatch, calls)
    monkeypatch.setattr(net_guard, "default_resolver", lambda host: ["10.0.0.5"] if host == "rebind.example" else ["93.184.216.34"])
    service = server.ValidationService(_settings(tmp_path))
    try:
        sources = [
            _source("http://127.0.0.1:8787", "loopback"),
            _source("http://169.254.169.254/latest/meta-data", "metadata"),
            _source("http://localhost:9000", "localhost"),
            _source("http://[::1]/", "ipv6"),
            _source("http://192.168.1.10/", "lan"),
            _source("http://2130706433/", "decimal"),
            _source("https://rebind.example", "rebind"),
            {**_source("https://public.example", "search-ssrf"), "searchUrl": "http://10.1.1.1/x?q={{key}}"},
            _source("https://public.example/ok", "ok"),
        ]
        job = _run_single_job(service, sources)
        assert job["status"] == "completed"
        assert calls == ["https://public.example/ok"]
        rows = {row["source_name"]: row for row in service.store.all_sources("ssrf-job")}
        for name in ("loopback", "metadata", "localhost", "ipv6", "lan", "decimal", "rebind", "search-ssrf"):
            assert rows[name]["quick_status"] == "failed", name
            assert "blocked: non-public address" in rows[name]["last_error"], name
        assert "blocked" not in rows["ok"]["last_error"]
    finally:
        service.close()


def test_private_network_switch_lets_local_fixtures_through(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    _fake_core(monkeypatch, calls)
    service = server.ValidationService(_settings(tmp_path, allow_private_network=True))
    try:
        job = _run_single_job(service, [_source("http://127.0.0.1:8765", "local")])
        assert job["status"] == "completed"
        assert calls == ["http://127.0.0.1:8765"]
    finally:
        service.close()


def test_unresolvable_host_is_left_to_the_engine(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    _fake_core(monkeypatch, calls)

    def failing(host: str):
        raise OSError("no such host")

    monkeypatch.setattr(net_guard, "default_resolver", failing)
    service = server.ValidationService(_settings(tmp_path))
    try:
        _run_single_job(service, [_source("https://gone.example")])
        assert calls == ["https://gone.example"]
    finally:
        service.close()


def test_source_hosts_collects_site_search_and_explore_hosts() -> None:
    source = {
        "bookSourceUrl": "https://a.example/path",
        "searchUrl": "https://b.example/s?q={{key}}",
        "exploreUrl": "[{\"title\":\"x\",\"url\":\"http://c.example:81/e\"}]",
    }
    assert server.source_hosts(source) == ["a.example", "b.example", "c.example"]
    assert server.source_hosts({"bookSourceUrl": "no url here"}) == []


# -------------------------------------------------------------------------------- queued cancel


def test_cancel_of_a_queued_job_takes_effect_immediately_and_sticks(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    gate = threading.Event()
    _fake_core(monkeypatch, calls, gate)
    monkeypatch.setattr(net_guard, "default_resolver", lambda host: ["93.184.216.34"])
    service = server.ValidationService(_settings(tmp_path))
    try:
        first = server.prepare_source_groups([_source("https://first.example", "first")])
        second = server.prepare_source_groups([_source("https://second.example", "second")])
        config = {"rounds": 1, "min_pass_rounds": 1, "profile": "device"}
        service.store.create_job("first", first, config, "1.json")
        service.store.create_job("second", second, config, "2.json")
        assert service.submit("first")
        deadline = time.monotonic() + 5
        while "https://first.example" not in calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert service.submit("second")  # max_jobs=1: it waits in the executor queue
        assert service.store.job("second")["status"] == "queued"

        assert service.cancel("second") is True
        job = service.store.job("second")
        assert job["status"] == "cancelled"  # visible right away, not "queued with a flag"
        assert job["cancel_requested"] == 0
        assert any(event["stage"] == "cancelled" for event in service.store.events("second"))

        gate.set()
        _wait_for_status(service, "first", {"completed"})
        time.sleep(0.2)  # give the worker the chance to (wrongly) pick the cancelled job up
        assert service.store.job("second")["status"] == "cancelled"
        assert "https://second.example" not in calls

        # A cancelled queued job can still be resumed explicitly.
        assert service.resume("second") is True
        _wait_for_status(service, "second", {"completed"})
        assert "https://second.example" in calls
    finally:
        gate.set()
        service.close()


def test_cancel_flag_set_before_start_is_honoured_not_cleared(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    _fake_core(monkeypatch, calls)
    service = server.ValidationService(_settings(tmp_path))
    try:
        groups = server.prepare_source_groups([_source("https://flagged.example", "flagged")])
        service.store.create_job("flagged", groups, {"rounds": 1, "min_pass_rounds": 1}, "f.json")
        service.store.update_job("flagged", cancel_requested=1)  # e.g. persisted before a restart
        assert service.store.begin_run("flagged") is False
        job = service.store.job("flagged")
        assert job["status"] == "cancelled"
        assert job["cancel_requested"] == 0
        assert calls == []
    finally:
        service.close()


def test_cancel_queued_ignores_running_and_finished_jobs(tmp_path: Path) -> None:
    store = server.JobStore(tmp_path / "jobs.sqlite3")
    groups = server.prepare_source_groups([_source("https://example.com")])
    store.create_job("j", groups, {}, "j.json")
    store.update_job("j", status="running")
    assert store.cancel_queued("j") is False
    assert store.request_cancel("j") is True  # running jobs still use the polled flag
    assert store.job("j")["cancel_requested"] == 1
    store.update_job("j", status="completed")
    assert store.cancel_queued("j") is False
    assert store.cancel_queued("missing") is False


def test_cancel_endpoint_marks_a_queued_job_cancelled(client_factory, monkeypatch) -> None:
    gate = threading.Event()
    _fake_core(monkeypatch, None, gate)
    client = client_factory()
    try:
        first = client.post("/v1/jobs", json={"sources": [_source("https://one.example")], "config": {"rounds": 1, "min_pass_rounds": 1}}).json()["job"]["id"]
        second = client.post("/v1/jobs", json={"sources": [_source("https://two.example")], "config": {"rounds": 1, "min_pass_rounds": 1}}).json()["job"]["id"]
        response = client.post(f"/v1/jobs/{second}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert client.get(f"/v1/jobs/{second}").json()["status"] == "cancelled"
        assert first != second
    finally:
        gate.set()


# ------------------------------------------------------------------------------ domain limiter


def test_domain_slot_honours_a_lowered_limit_even_while_busy(tmp_path: Path) -> None:
    service = server.ValidationService(_settings(tmp_path))
    try:
        wide = service._domain_slot("busy.example", 2)
        assert wide.acquire(blocking=False)  # one permit in use, one free
        # Same capacity: must be the very same object even though a permit is held.
        assert service._domain_slot("busy.example", 2) is wide
        # Lowered capacity: the old check saw 1 free permit, not > 1, and kept the 2-wide semaphore.
        narrow = service._domain_slot("busy.example", 1)
        assert narrow is not wide
        assert narrow.acquire(blocking=False) is True
        assert narrow.acquire(blocking=False) is False  # capacity really is 1
        # Raising the limit again is honoured too.
        wider = service._domain_slot("busy.example", 3)
        assert [wider.acquire(blocking=False) for _ in range(4)] == [True, True, True, False]
        wide.release()  # holders of a replaced semaphore release into it without error
    finally:
        service.close()


def test_domain_slots_are_independent_per_domain(tmp_path: Path) -> None:
    service = server.ValidationService(_settings(tmp_path))
    try:
        assert service._domain_slot("a.example", 1) is not service._domain_slot("b.example", 1)
        assert service._domain_slot("a.example", 1) is service._domain_slot("a.example", 1)
    finally:
        service.close()


# ---------------------------------------------------------------------------------------- auth


def test_api_key_is_still_enforced(client_factory) -> None:
    client = client_factory(api_key="secret-key")
    assert client.get("/healthz").status_code == 200
    assert client.post("/v1/jobs", json={"sources": [_source("https://example.com")]}).status_code == 401
    ok = client.post("/v1/jobs", json={"sources": [_source("https://example.com")]}, headers={"x-api-key": "secret-key"})
    assert ok.status_code == 200
