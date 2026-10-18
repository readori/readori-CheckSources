"""R146 WP-D: the web workbench API (``server/debug_api.py``).

Runs the real FastAPI app through ``TestClient`` against the local fixture site (``debug_fixture_site``): create a
session, read the SSE stream to ``done``, poll as a fallback, step / cancel / limits, raw responses, rule tests,
suggestions, previews, diff and export.  No external network; nothing touches the batch queue.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import debug_fixture_site as fixture_site

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import debug_api  # noqa: E402
from server import source_validator_server as server  # noqa: E402

KEY = "test-api-key"
HEADERS = {"x-api-key": KEY}


@pytest.fixture(scope="module")
def site():
    patch = pytest.MonkeyPatch()
    patch.setenv("READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK", "1")
    base, alt, stop = fixture_site.start()
    try:
        yield SimpleNamespace(base=base, alt=alt, sources=fixture_site.sources(base))
    finally:
        stop()
        patch.undo()


@pytest.fixture()
def make_app(monkeypatch, tmp_path: Path):
    """Factory for the real server app (SQLite in ``tmp_path``); every app is shut down at teardown."""

    apps: list = []

    def factory(*, allow_private: bool = True, api_key: str = KEY, **env: str):
        for name, value in env.items():
            monkeypatch.setenv(f"READORI_VALIDATOR_DEBUG_{name.upper()}", str(value))
        settings = server.ServerSettings(
            db_path=tmp_path / f"data{len(apps)}" / "validator.sqlite3", api_key=api_key, max_workers=1, max_jobs=1,
            default_domain_concurrency=1, allow_private_network=allow_private,
        )
        app = server.create_app(settings)
        apps.append(app)
        return app

    yield factory
    for app in apps:
        manager = app.state.debug_manager
        for run in list(manager._runs.values()):
            run.controller.cancel()
        deadline = time.monotonic() + 15
        while manager.active and time.monotonic() < deadline:
            time.sleep(0.05)
        manager.close()
        app.state.validation_service.close()


@pytest.fixture()
def client(make_app) -> TestClient:
    return TestClient(make_app())


def create(client: TestClient, source: dict, **extra) -> dict:
    response = client.post("/v1/debug/sessions", json={"source": source, **extra}, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


def parse_sse(text: str) -> list[dict]:
    frames = []
    for block in text.split("\n\n"):
        frame: dict = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                frame.setdefault("comments", []).append(line)
            elif line.startswith("id: "):
                frame["id"] = line[4:]
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                frame["data"] = json.loads(line[6:])
        if "event" in frame:
            frames.append(frame)
    return frames


def read_stream(client: TestClient, session_id: str, after: int = 0, headers: dict | None = None) -> tuple[list[dict], object]:
    with client.stream("GET", f"/v1/debug/sessions/{session_id}/stream?after={after}", headers={**HEADERS, **(headers or {})}) as response:
        assert response.status_code == 200
        content_type = response.headers["content-type"]
        body = "".join(response.iter_text())
    return parse_sse(body), content_type


def wait_done(client: TestClient, session_id: str, seconds: float = 40.0) -> dict:
    deadline = time.monotonic() + seconds
    after, items = 0, []
    while time.monotonic() < deadline:
        data = client.get(f"/v1/debug/sessions/{session_id}/events?after={after}", headers=HEADERS).json()
        items += data["items"]
        after = data["nextAfter"]
        if data["done"]:
            data["all"] = items
            return data
        time.sleep(0.05)
    raise AssertionError(f"session {session_id} did not finish")


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------

ROUTES = [
    ("POST", "/v1/debug/sessions"),
    ("GET", "/v1/debug/sessions/abcdefgh12345678"),
    ("GET", "/v1/debug/sessions/abcdefgh12345678/stream"),
    ("GET", "/v1/debug/sessions/abcdefgh12345678/events"),
    ("POST", "/v1/debug/sessions/abcdefgh12345678/resume"),
    ("POST", "/v1/debug/sessions/abcdefgh12345678/cancel"),
    ("GET", "/v1/debug/sessions/abcdefgh12345678/raw"),
    ("GET", "/v1/debug/sessions/abcdefgh12345678/raw/1"),
    ("GET", "/v1/debug/sessions/abcdefgh12345678/suggestions"),
    ("POST", "/v1/debug/sessions/abcdefgh12345678/suggestions"),
    ("POST", "/v1/debug/test-rule"),
    ("POST", "/v1/debug/preview"),
    ("POST", "/v1/debug/diff"),
    ("POST", "/v1/debug/export"),
]


@pytest.mark.parametrize("method, path", ROUTES)
def test_every_route_requires_the_api_key(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path, json={}).status_code == 401
    assert client.request(method, path, json={}, headers={"x-api-key": "wrong"}).status_code == 401
    assert client.request(method, path, json={}, headers={"authorization": "Bearer wrong"}).status_code == 401


def test_bearer_token_is_accepted_like_the_batch_api(client: TestClient, site) -> None:
    response = client.post("/v1/debug/sessions", json={"source": site.sources["good"], "mode": "validate"},
                           headers={"authorization": f"Bearer {KEY}"})
    assert response.status_code == 201
    client.post(f"/v1/debug/sessions/{response.json()['sessionId']}/cancel", headers=HEADERS)


def test_unknown_session_is_404_and_bad_ids_never_reach_the_store(client: TestClient) -> None:
    for session_id in ("abcdefgh12345678", "short", "x" * 100, "..%2f..%2fetc"):
        assert client.get(f"/v1/debug/sessions/{session_id}/events", headers=HEADERS).status_code == 404


# ---------------------------------------------------------------------------
# end to end: create -> stream -> raw -> test-rule -> suggestions -> preview -> diff -> export
# ---------------------------------------------------------------------------

def test_end_to_end_session_stream_raw_rule_suggest_diff_export(client: TestClient, site) -> None:
    source = copy.deepcopy(site.sources["good"])
    created = create(client, source, key="我的", appVersion="1.1.1")
    assert created["status"] == "running" and created["sessionId"] and created["expiresAt"]
    session_id = created["sessionId"]

    frames, content_type = read_stream(client, session_id)
    assert "text/event-stream" in content_type
    assert frames[-1]["event"] == "done"
    done = frames[-1]["data"]
    assert done["status"] == "pass" and done["ok"] is True
    assert done["result"]["chapterTitle"]
    traces = [f for f in frames if f["event"] == "trace"]
    assert traces and all(f["id"] == str(f["data"]["seq"]) for f in traces)
    seqs = [f["data"]["seq"] for f in traces]
    assert seqs == sorted(seqs)
    texts = [f["data"]["text"] for f in traces if f["data"]["text"]]
    assert any("开始搜索关键字:我的" in t for t in texts)
    assert any("正文" in t or "调试" in t for t in texts)
    assert all("request_headers" not in f["data"]["data"] for f in traces)  # headers only via /raw (redacted)
    assert traces[-1]["data"]["kind"] == "verdict" and traces[-1]["data"]["state"] == 1000

    # resuming the stream from a later position only replays the tail (Last-Event-ID and ?after= agree)
    tail, _ = read_stream(client, session_id, headers={"Last-Event-ID": str(seqs[-3])})
    assert [f["data"]["seq"] for f in tail if f["event"] == "trace"] == seqs[-2:]
    assert tail[-1]["event"] == "done"

    summary = client.get(f"/v1/debug/sessions/{session_id}", headers=HEADERS).json()
    assert summary["status"] == "pass" and summary["done"] is True and summary["expiresAt"]

    # raw responses: list without bodies, one entry with a byte cap and redacted secrets
    raw_list = client.get(f"/v1/debug/sessions/{session_id}/raw", headers=HEADERS).json()
    assert raw_list["items"] and all("body" not in item for item in raw_list["items"])
    assert {item["stage"] for item in raw_list["items"]} >= {"search", "info", "toc", "content"}
    search_raw = next(item for item in raw_list["items"] if item["stage"] == "search")
    full = client.get(f"/v1/debug/sessions/{session_id}/raw/{search_raw['raw_id']}", headers=HEADERS).json()
    assert "我的老婆是女帝" in full["body"] and full["truncated"] is False
    cut = client.get(f"/v1/debug/sessions/{session_id}/raw/{search_raw['raw_id']}?maxBytes=40", headers=HEADERS).json()
    assert cut["truncated"] is True and len(cut["body"].encode("utf-8")) <= 40
    assert client.get(f"/v1/debug/sessions/{session_id}/raw/9999", headers=HEADERS).status_code == 404

    # rule test on the saved response of the field's stage, with an override rule, and on a pasted body
    tested = client.post("/v1/debug/test-rule", headers=HEADERS,
                         json={"sessionId": session_id, "source": source, "field": "ruleSearch.name"}).json()
    assert tested["ok"] is True and tested["value"] == "我的老婆是女帝" and tested["data"]["rawId"] == search_raw["raw_id"]
    second = client.post("/v1/debug/test-rule", headers=HEADERS,
                         json={"sessionId": session_id, "source": source, "field": "ruleSearch.name", "itemIndex": 1}).json()
    assert second["value"] == "夜无疆"
    override = client.post("/v1/debug/test-rule", headers=HEADERS,
                           json={"sessionId": session_id, "rawId": search_raw["raw_id"], "source": source,
                                 "field": "ruleSearch.author", "rule": "class.author@text"}).json()
    assert override["value"] == "天命书生"
    pasted = client.post("/v1/debug/test-rule", headers=HEADERS,
                         json={"source": source, "field": "ruleSearch.bookList", "baseUrl": site.base,
                               "body": "<ul class='result-list'><li><h3>甲</h3></li><li><h3>乙</h3></li></ul>"}).json()
    assert pasted["ok"] is True and pasted["count"] == 2
    empty_rule = client.post("/v1/debug/test-rule", headers=HEADERS,
                                json={"sessionId": session_id, "source": source, "field": "ruleBookInfo.kind"}).json()
    assert empty_rule["ok"] is False and empty_rule["error"]

    # suggestions (the fixture chapter text carries ad lines -> F7) and an offline preview
    suggestions = client.get(f"/v1/debug/sessions/{session_id}/suggestions?source=working", headers=HEADERS).json()["items"]
    codes = {item["code"] for item in suggestions}
    assert "F7" in codes, codes
    f7 = next(item for item in suggestions if item["code"] == "F7")
    assert f7["patch"] and f7["preview_kind"] == "offline"
    preview = client.post("/v1/debug/preview", headers=HEADERS,
                          json={"sessionId": session_id, "suggestionId": f7["id"], "source": source}).json()
    assert preview["ok"] is True and preview["summary"] and preview["apply_allowed"] is f7["apply_allowed"]  # 37% of the fixture chapter is ads: both say "do not apply"
    assert client.post("/v1/debug/preview", headers=HEADERS,
                       json={"sessionId": session_id, "suggestionId": "nope", "source": source}).status_code == 404

    # apply the patch on the client, diff and export it
    working = copy.deepcopy(source)
    for op in f7["patch"]:
        node = working
        parts = op["path"].split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = op["new"]
    diff = client.post("/v1/debug/diff", headers=HEADERS, json={"original": source, "working": working}).json()
    assert [c["path"] for c in diff["changes"]] == [op["path"] for op in f7["patch"]]
    assert diff["unified"].startswith("--- original")
    exported = client.post("/v1/debug/export", headers=HEADERS, json={"entries": [
        {"original": source, "working": working, "changes": [{**op, "reason": f7["title"], "origin": "suggestion:F7"} for op in f7["patch"]],
         "codes": ["F7"], "after": {"verdict": "pass", "passed": True, "stages": {}}},
        {"original": source, "working": source, "changes": [], "codes": []},
    ], "onlyChanged": True}).json()
    assert exported["count"] == 1 and len(exported["sources"]) == 1
    assert exported["sources"][0]["bookSourceComment"].startswith("// Fixed: ")
    report = exported["fixReport"]
    assert report["schema"] == "readori.fix-report/1" and report["sources"][0]["changes"][0]["origin"] == "suggestion:F7"
    assert "__readoriValidation" not in exported["sources"][0]


def test_suggestions_for_an_edited_source_and_a_running_session(client: TestClient, site) -> None:
    created = create(client, site.sources["slow"], mode="validate")
    session_id = created["sessionId"]
    assert client.get(f"/v1/debug/sessions/{session_id}/suggestions", headers=HEADERS).status_code == 409
    assert client.get(f"/v1/debug/sessions/{session_id}/suggestions?source=original", headers=HEADERS).status_code == 400
    client.post(f"/v1/debug/sessions/{session_id}/cancel", headers=HEADERS)
    wait_done(client, session_id)
    edited = client.post(f"/v1/debug/sessions/{session_id}/suggestions", headers=HEADERS,
                         json={"source": site.sources["slow"]})
    assert edited.status_code == 200 and isinstance(edited.json()["items"], list)


def test_polling_fallback_pages_events_and_reports_the_end(client: TestClient, site) -> None:
    created = create(client, site.sources["good"], key="我的")
    session_id = created["sessionId"]
    first = client.get(f"/v1/debug/sessions/{session_id}/events?after=0&limit=3", headers=HEADERS).json()
    assert set(first) >= {"items", "done", "status", "nextAfter"}
    assert len(first["items"]) <= 3
    data = wait_done(client, session_id)
    items = data["all"]
    assert items[-1]["kind"] == "verdict"
    assert [i["seq"] for i in items] == sorted(set(i["seq"] for i in items))  # no duplicates when paging
    assert data["status"] == "pass" and data["final"]["ok"] is True
    again = client.get(f"/v1/debug/sessions/{session_id}/events?after={data['nextAfter']}", headers=HEADERS).json()
    assert again["items"] == [] and again["done"] is True and again["nextAfter"] == data["nextAfter"]


def test_validate_and_compare_modes(client: TestClient, site) -> None:
    validated = create(client, site.sources["good"], mode="validate")
    frames, _ = read_stream(client, validated["sessionId"])
    done = frames[-1]["data"]
    assert done["status"] == "pass" and done["verdict"] == "pass"
    assert done["result"]["passed"] is True and "record" not in done["result"] and "detail" not in done["result"]
    assert any("校验结论：可用" in f["data"].get("text", "") for f in frames if f["event"] == "trace")

    broken = site.sources["toc_broken"]
    compared = create(client, site.sources["good"], mode="compare", original=broken)
    frames, _ = read_stream(client, compared["sessionId"])
    done = frames[-1]["data"]
    assert done["status"] == "pass"
    assert done["result"]["before"]["passed"] is False and done["result"]["after"]["passed"] is True
    assert done["result"]["changed"] is True
    assert {row["stage"] for row in done["result"]["rows"]} >= {"toc", "verdict"}
    texts = "\n".join(f["data"].get("text", "") for f in frames if f["event"] == "trace")
    assert "校验原书源" in texts and "校验修改后的书源" in texts

    assert client.post("/v1/debug/sessions", json={"source": broken, "mode": "compare"}, headers=HEADERS).status_code == 400


# ---------------------------------------------------------------------------
# step mode, cancel, concurrency, TTL
# ---------------------------------------------------------------------------

def test_step_mode_pause_resume_and_validation(client: TestClient, site) -> None:
    created = create(client, site.sources["good"], key="我的", step=True)
    session_id = created["sessionId"]
    deadline = time.monotonic() + 30
    pause = None
    while pause is None and time.monotonic() < deadline:
        data = client.get(f"/v1/debug/sessions/{session_id}/events?after=0&limit=1000", headers=HEADERS).json()
        pause = next((item for item in data["items"] if item["kind"] == "pause"), None)
        time.sleep(0.05)
    assert pause is not None, "no pause event"
    assert pause["data"]["what"] == "book" and len(pause["data"]["choices"]) == 2
    status = client.get(f"/v1/debug/sessions/{session_id}/events?after=0&limit=1", headers=HEADERS).json()["status"]
    assert status == "paused"
    assert client.post(f"/v1/debug/sessions/{session_id}/resume", json={"choice": 99}, headers=HEADERS).status_code == 400
    assert client.post(f"/v1/debug/sessions/{session_id}/resume", json={"choice": "x"}, headers=HEADERS).status_code == 400
    resumed = client.post(f"/v1/debug/sessions/{session_id}/resume", json={"choice": 1}, headers=HEADERS)
    assert resumed.status_code == 200
    # there is a second pause (the chapter) - answer it too
    deadline = time.monotonic() + 30
    answered = False
    while time.monotonic() < deadline:
        data = client.get(f"/v1/debug/sessions/{session_id}/events?after=0&limit=1000", headers=HEADERS).json()
        if data["done"]:
            break
        if sum(1 for item in data["items"] if item["kind"] == "pause") >= 2 and not answered:
            if client.post(f"/v1/debug/sessions/{session_id}/resume", json={"choice": 0}, headers=HEADERS).status_code == 200:
                answered = True
        time.sleep(0.05)
    final = wait_done(client, session_id)
    assert final["status"] == "pass"
    assert client.post(f"/v1/debug/sessions/{session_id}/resume", json={"choice": 0}, headers=HEADERS).status_code == 409


def test_cancel_stops_a_running_session(client: TestClient, site) -> None:
    created = create(client, site.sources["slow"], key="我的")
    session_id = created["sessionId"]
    time.sleep(0.3)
    cancelled = client.post(f"/v1/debug/sessions/{session_id}/cancel", headers=HEADERS)
    assert cancelled.status_code == 200 and cancelled.json()["cancelRequested"] is True
    final = wait_done(client, session_id, seconds=15)
    assert final["status"] == "cancelled"
    assert any("已停止" in (item.get("text") or "") for item in final["all"])


def test_concurrency_limit_returns_429_and_frees_up_after_the_run(make_app, site) -> None:
    client = TestClient(make_app(sessions="1"))
    first = create(client, site.sources["slow"], mode="validate")
    busy = client.post("/v1/debug/sessions", json={"source": site.sources["good"]}, headers=HEADERS)
    assert busy.status_code == 429
    assert busy.json() == {"error": "debug busy", "retryAfter": 10}
    assert busy.headers["retry-after"] == "10"
    client.post(f"/v1/debug/sessions/{first['sessionId']}/cancel", headers=HEADERS)
    wait_done(client, first["sessionId"], seconds=15)
    again = client.post("/v1/debug/sessions", json={"source": site.sources["good"], "mode": "validate"}, headers=HEADERS)
    assert again.status_code == 201
    wait_done(client, again.json()["sessionId"])


def test_amd_micro_allows_exactly_one_session(monkeypatch) -> None:
    monkeypatch.setenv("READORI_VALIDATOR_DEBUG_SESSIONS", "4")
    assert debug_api.DebugSettings.from_env(amd_micro=True).sessions == 1
    assert debug_api.DebugSettings.from_env(amd_micro=True).retain >= 2
    assert debug_api.DebugSettings.from_env(amd_micro=False).sessions == 4
    monkeypatch.delenv("READORI_VALIDATOR_DEBUG_SESSIONS")
    assert debug_api.DebugSettings.from_env(amd_micro=False).sessions == 2
    assert debug_api.DebugSettings.from_env(amd_micro=False).timeout_max == 90


def test_finished_sessions_expire_after_the_ttl(make_app, site) -> None:
    app = make_app(ttl="60")
    client = TestClient(app)
    created = create(client, site.sources["good"], mode="validate")
    session_id = created["sessionId"]
    wait_done(client, session_id)
    assert client.get(f"/v1/debug/sessions/{session_id}/raw", headers=HEADERS).status_code == 200
    manager = app.state.debug_manager
    real_now = manager.now
    manager.now = lambda: real_now() + 61
    for path in ("", "/events", "/raw", "/stream", "/suggestions"):
        assert client.get(f"/v1/debug/sessions/{session_id}{path}", headers=HEADERS).status_code == 404, path
    assert session_id not in manager._runs


def test_retention_cap_evicts_the_oldest_finished_session(make_app, site) -> None:
    app = make_app(sessions="1", retain="2")
    client = TestClient(app)
    ids = []
    for _ in range(3):
        created = create(client, site.sources["good"], mode="validate")
        ids.append(created["sessionId"])
        wait_done(client, created["sessionId"])
    assert client.get(f"/v1/debug/sessions/{ids[0]}", headers=HEADERS).status_code == 404
    assert client.get(f"/v1/debug/sessions/{ids[2]}", headers=HEADERS).status_code == 200


def test_private_network_is_refused_unless_the_operator_allows_it(make_app, site) -> None:
    client = TestClient(make_app(allow_private=False))
    fixture_site.REQUEST_LOG.clear()
    created = create(client, site.sources["good"], key="我的")
    final = wait_done(client, created["sessionId"])
    assert final["status"] == "fail" and final["final"]["ok"] is False
    errors = [item for item in final["all"] if item["kind"] == "fetch_error"]
    assert errors and "blocked: non-public address" in errors[0]["text"]
    assert fixture_site.REQUEST_LOG == []  # the guard refused before a single byte reached the site
    raw = client.get(f"/v1/debug/sessions/{created['sessionId']}/raw", headers=HEADERS).json()
    assert raw["items"] == []


def test_online_preview_takes_a_session_slot(make_app, site) -> None:
    app = make_app(sessions="1")
    client = TestClient(app)
    created = create(client, site.sources["moved"], key="我的")
    session_id = created["sessionId"]
    wait_done(client, session_id)
    items = client.post(f"/v1/debug/sessions/{session_id}/suggestions", headers=HEADERS,
                        json={"source": site.sources["moved"]}).json()["items"]
    online = [item for item in items if item["preview_kind"] == "online" and item["patch"]]
    assert online, [item["code"] for item in items]
    manager = app.state.debug_manager
    assert manager.try_acquire() is True  # simulate a busy slot: an online preview must now be refused with 429
    try:
        busy = client.post("/v1/debug/preview", headers=HEADERS,
                           json={"sessionId": session_id, "suggestionId": online[0]["id"],
                                 "source": site.sources["moved"], "online": True})
        assert busy.status_code == 429 and busy.json()["error"] == "debug busy"
    finally:
        manager.release()
    ran = client.post("/v1/debug/preview", headers=HEADERS,
                      json={"sessionId": session_id, "suggestionId": online[0]["id"],
                            "source": site.sources["moved"], "online": True})
    assert ran.status_code == 200 and ran.json()["kind"] == "online"
    assert manager.active == 0  # the slot is returned after the preview


def test_lifespan_shutdown_stops_the_debug_manager(make_app, site) -> None:
    app = make_app()
    with TestClient(app) as client:
        created = create(client, site.sources["slow"], mode="validate")
        assert client.get(f"/v1/debug/sessions/{created['sessionId']}", headers=HEADERS).status_code == 200
    assert app.state.debug_manager._closed is True
    assert app.state.debug_manager._runs[created["sessionId"]].controller.cancelled is True


# ---------------------------------------------------------------------------
# validation and size limits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {},
    {"source": "text"},
    {"source": {"bookSourceName": "no url"}},
    {"source": {"bookSourceUrl": "https://a.example"}, "mode": "bogus"},
    {"source": {"bookSourceUrl": "https://a.example"}, "step": "yes"},
    {"source": {"bookSourceUrl": "https://a.example"}, "key": 5},
    {"source": {"bookSourceUrl": "https://a.example"}, "appVersion": "9.9.9"},
    {"source": {"bookSourceUrl": "https://a.example"}, "timeoutSeconds": 1},
    {"source": {"bookSourceUrl": "https://a.example"}, "timeoutSeconds": 100000},
    {"source": {"bookSourceUrl": "https://a.example"}, "timeoutSeconds": "abc"},
])
def test_create_session_rejects_bad_input_with_400(client: TestClient, payload: dict) -> None:
    assert client.post("/v1/debug/sessions", json=payload, headers=HEADERS).status_code == 400


def test_invalid_json_and_non_object_bodies_are_400(client: TestClient) -> None:
    assert client.post("/v1/debug/sessions", content=b"{not json", headers={**HEADERS, "content-type": "application/json"}).status_code == 400
    assert client.post("/v1/debug/sessions", content=b"[1,2]", headers={**HEADERS, "content-type": "application/json"}).status_code == 400


def test_oversized_source_and_bodies_are_413(client: TestClient) -> None:
    big_source = {"bookSourceUrl": "https://a.example", "bookSourceComment": "x" * (300 * 1024)}
    assert client.post("/v1/debug/sessions", json={"source": big_source}, headers=HEADERS).status_code == 413
    assert client.post("/v1/debug/diff", json={"original": big_source, "working": big_source}, headers=HEADERS).status_code == 413
    # more bytes than any debug request may carry: refused while reading (also without a content-length header)
    huge = b'{"source": {"bookSourceUrl": "https://a.example", "bookSourceComment": "' + b"y" * (800 * 1024) + b'"}}'
    assert client.post("/v1/debug/sessions", content=huge, headers={**HEADERS, "content-type": "application/json"}).status_code == 413

    def chunks():
        for index in range(0, len(huge), 65536):
            yield huge[index:index + 65536]

    assert client.post("/v1/debug/sessions", content=chunks(), headers={**HEADERS, "content-type": "application/json"}).status_code == 413
    body_too_big = {"source": {"bookSourceUrl": "https://a.example", "ruleSearch": {"bookList": "tag.li"}},
                    "field": "ruleSearch.bookList", "body": "z" * (1024 * 1024 + 10)}
    assert client.post("/v1/debug/test-rule", json=body_too_big, headers=HEADERS).status_code == 413
    export_big = {"entries": [{"working": {"bookSourceUrl": "https://a.example", "c": "q" * (2 * 1024 * 1024)}}]}
    assert client.post("/v1/debug/export", json=export_big, headers=HEADERS).status_code == 413


def test_test_rule_validates_its_arguments(client: TestClient) -> None:
    source = {"bookSourceUrl": "https://a.example", "ruleSearch": {"bookList": "tag.li"}}
    post = lambda body: client.post("/v1/debug/test-rule", json=body, headers=HEADERS)  # noqa: E731
    assert post({"source": source, "field": "ruleSearch.bookList"}).status_code == 400  # no body, no session
    assert post({"source": source, "field": "../etc/passwd", "body": "<li/>"}).status_code == 400
    assert post({"source": source, "field": "ruleSearch.bookList", "body": 5}).status_code == 400
    assert post({"source": source, "field": "ruleSearch.bookList", "body": "<li/>", "itemIndex": -1}).status_code == 400
    assert post({"source": source, "field": "ruleSearch.bookList", "body": "<li/>", "sessionId": "abcdefgh12345678"}).status_code == 404
    unsupported = post({"source": source, "field": "ruleSearch.nonsense", "body": "<li/>"})
    assert unsupported.status_code == 200 and unsupported.json()["ok"] is False


def test_export_validates_entries(client: TestClient) -> None:
    for body in ({}, {"entries": []}, {"entries": "x"}, {"entries": [{"working": "x"}]}, {"entries": [{}]}):
        assert client.post("/v1/debug/export", json=body, headers=HEADERS).status_code == 400
    many = {"entries": [{"working": {"bookSourceUrl": "https://a.example"}}] * (debug_api.MAX_EXPORT_ENTRIES + 1)}
    assert client.post("/v1/debug/export", json=many, headers=HEADERS).status_code == 413


def test_export_keeps_key_order_and_can_keep_the_source_url(client: TestClient) -> None:
    original = {"bookSourceName": "A", "bookSourceUrl": "https://old.example", "searchUrl": "/s?q={{key}}",
                "__readoriValidation": {"x": 1}}
    working = {**original, "bookSourceUrl": "https://new.example", "extra": 1}
    out = client.post("/v1/debug/export", headers=HEADERS, json={"keepSourceUrl": True, "entries": [{
        "original": original, "working": working,
        "changes": [{"path": "bookSourceUrl", "old": "https://old.example", "new": "https://new.example", "reason": "域名迁移", "origin": "suggestion:F1"}],
        "codes": ["F1"]}]}).json()
    source = out["sources"][0]
    assert source["bookSourceUrl"] == "https://old.example" and "__readoriValidation" not in source
    assert list(source)[:3] == ["bookSourceName", "bookSourceUrl", "searchUrl"] and source["extra"] == 1
    assert out["fixReport"]["sources"][0]["bookSourceUrl"] == "https://old.example"
    assert out["fixReport"]["sources"][0]["originalBookSourceUrl"] == "https://old.example"


def test_secrets_in_headers_and_variables_are_redacted(client: TestClient, site) -> None:
    source = copy.deepcopy(site.sources["good"])
    source["header"] = json.dumps({"Cookie": "sid=SECRET-COOKIE", "Authorization": "Bearer SECRET-AUTH", "X-Plain": "ok"})
    created = create(client, source, key="我的")
    session_id = created["sessionId"]
    final = wait_done(client, session_id)
    wire = json.dumps(final["all"], ensure_ascii=False)
    assert "SECRET-COOKIE" not in wire and "SECRET-AUTH" not in wire
    raw = client.get(f"/v1/debug/sessions/{session_id}/raw", headers=HEADERS).json()["items"]
    entry = client.get(f"/v1/debug/sessions/{session_id}/raw/{raw[0]['raw_id']}", headers=HEADERS).json()
    headers_text = json.dumps(entry["request_headers"]) + json.dumps(entry["response_headers"])
    assert "SECRET-COOKIE" not in headers_text and "SECRET-AUTH" not in headers_text
    assert any("redacted" in value for value in entry["request_headers"].values()), entry["request_headers"]


# ---------------------------------------------------------------------------
# slow offline work answers 504 instead of hanging the request
# ---------------------------------------------------------------------------

def test_rule_test_that_overruns_its_deadline_is_504(monkeypatch) -> None:
    from validator import rule_tester

    release = threading.Event()

    def slow(*args, **kwargs):
        release.wait(6)
        raise AssertionError("must not matter")

    monkeypatch.setattr(rule_tester, "test_rule", slow)
    app = FastAPI()
    settings = server.ServerSettings(allow_private_network=True)
    manager = debug_api.register_debug_routes(
        app, settings, lambda: None, debug_settings=debug_api.DebugSettings(rule_test_timeout=1, allow_private_network=True))
    try:
        client = TestClient(app)
        started = time.monotonic()
        response = client.post("/v1/debug/test-rule", json={
            "source": {"bookSourceUrl": "https://a.example", "ruleSearch": {"bookList": "tag.li"}},
            "field": "ruleSearch.bookList", "body": "<li/>"})
        assert response.status_code == 504
        assert time.monotonic() - started < 5
    finally:
        release.set()
        manager.close()


# ---------------------------------------------------------------------------
# real streaming (TestClient buffers a response until it ends; talk ASGI directly)
# ---------------------------------------------------------------------------

def test_sse_frames_leave_the_server_while_the_session_is_still_running(make_app, site) -> None:
    import asyncio

    app = make_app()
    client = TestClient(app)
    created = create(client, site.sources["slow"], mode="validate")  # every request of this source answers 0.7 s late
    session_id = created["sessionId"]
    run = app.state.debug_manager.get(session_id)
    assert run is not None

    async def scenario() -> dict:
        sent: list[dict] = []
        first_chunk_seen = asyncio.Event()
        disconnect = asyncio.Event()
        request_sent = False

        async def receive() -> dict:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append({"message": message, "done_at_send": run.done})
            if message["type"] == "http.response.body" and message.get("body"):
                first_chunk_seen.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": f"/v1/debug/sessions/{session_id}/stream", "raw_path": f"/v1/debug/sessions/{session_id}/stream".encode(),
            "query_string": b"after=0", "root_path": "", "client": ("127.0.0.1", 1), "server": ("testserver", 80),
            "headers": [(b"host", b"testserver"), (b"x-api-key", KEY.encode())],
        }
        task = asyncio.ensure_future(app(scope, receive, send))
        await asyncio.wait_for(first_chunk_seen.wait(), 10)
        early = [item for item in sent if item["message"]["type"] == "http.response.body" and item["message"].get("body")]
        assert early and early[0]["done_at_send"] is False, "first bytes must arrive before the session ends"
        assert b": connected" in early[0]["message"]["body"]
        run.controller.cancel()
        await asyncio.wait_for(task, 20)
        return {"sent": sent, "early": len(early)}

    result = asyncio.run(scenario())
    start = next(item["message"] for item in result["sent"] if item["message"]["type"] == "http.response.start")
    headers = {name.decode().lower(): value.decode() for name, value in start["headers"]}
    assert start["status"] == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["x-accel-buffering"] == "no" and headers["cache-control"] == "no-store"
    body = b"".join(item["message"].get("body", b"") for item in result["sent"] if item["message"]["type"] == "http.response.body")
    frames = parse_sse(body.decode("utf-8"))
    assert frames[-1]["event"] == "done" and frames[-1]["data"]["status"] == "cancelled"
