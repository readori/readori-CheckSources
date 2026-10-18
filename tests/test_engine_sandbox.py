"""R145b: validation-engine SSRF filter + Node execution sandbox for source JS (offline, loopback only).

Network: every outbound request of the engine goes through net_guard's GuardedSession; with the policy "block private
networks" the request is refused before anything is sent (every redirect hop re-checked, connection pinned to the
address that was validated).  Node: source scripts run under `--permission`, a minimal environment and a runner that
strips the host realm, so `constructor.constructor('return process')()` no longer reaches the host.
"""
from __future__ import annotations

import contextvars
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import requests

from validator import net_guard
from validator import validate_source_packages as validator

NODE = validator.node_binary_path()
needs_node = pytest.mark.skipif(not NODE, reason="node is not available")
needs_permission = pytest.mark.skipif(
    not NODE or not validator.node_supports_permission_model(NODE),
    reason="node without --permission support",
)

_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture(autouse=True)
def _isolated_policy(monkeypatch) -> Any:
    """Every test starts from the default policy (private allowed) and cannot leak a changed one."""

    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    previous = net_guard.network_allows_private()
    net_guard.set_network_policy(True)
    yield
    net_guard.set_network_policy(previous)


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list[tuple[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).hits.append((self.path, self.headers.get("Host", "")))
        if self.path.startswith("/redir?to="):
            self.send_response(302)
            self.send_header("Location", self.path[len("/redir?to="):])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture()
def local_server() -> Any:
    handler = type("Handler", (_Handler,), {"hits": []})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], handler.hits
    finally:
        httpd.shutdown()
        httpd.server_close()


def _treat_loopback_as_public(monkeypatch) -> None:
    """Pretend 127.0.0.1 is public so a loopback fixture can play the 'allowed first hop'."""

    original = net_guard.is_public_ip

    def fake(ip: Any) -> bool:
        return True if str(ip) == "127.0.0.1" else original(ip)

    monkeypatch.setattr(net_guard, "is_public_ip", fake)


def _blocked_text(exc: BaseException) -> bool:
    return net_guard.BLOCKED_REASON_TEXT in str(exc)


# ------------------------------------------------------------------------------------------- policy switch


def test_default_policy_allows_private_and_scope_overrides_it() -> None:
    assert net_guard.network_allows_private() is True
    with net_guard.network_policy_scope(False):
        assert net_guard.network_allows_private() is False
        with net_guard.network_policy_scope(True):
            assert net_guard.network_allows_private() is True
        assert net_guard.network_allows_private() is False
    assert net_guard.network_allows_private() is True


def test_set_network_policy_changes_the_process_default() -> None:
    net_guard.set_network_policy(False)
    assert net_guard.network_allows_private() is False
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(net_guard.network_allows_private()))
    thread.start()
    thread.join()
    assert result == [False]  # a thread that never bound a context still sees the process default
    assert validator.network_allows_private() is False  # re-exported by the engine module


def test_scope_travels_with_a_bound_context_into_worker_threads() -> None:
    seen: list[bool] = []
    with net_guard.network_policy_scope(False):
        ctx = contextvars.copy_context()
    thread = threading.Thread(target=lambda: ctx.run(lambda: seen.append(net_guard.network_allows_private())))
    thread.start()
    thread.join()
    assert seen == [False]
    assert net_guard.network_allows_private() is True


# ------------------------------------------------------------------------------------- blocked connections


def test_private_target_is_refused_before_any_request_is_sent(local_server) -> None:
    port, hits = local_server
    with net_guard.network_policy_scope(False):
        with pytest.raises(requests.exceptions.ConnectionError) as info:
            validator.requests.Session().get(f"http://127.0.0.1:{port}/secret", timeout=3)
    assert _blocked_text(info.value)
    assert hits == []


def test_private_policy_off_by_default_allows_loopback_fixture(local_server) -> None:
    port, hits = local_server
    response = validator.requests.Session().get(f"http://127.0.0.1:{port}/ok", timeout=3)
    assert response.status_code == 200 and response.text == "hello"
    assert hits == [("/ok", f"127.0.0.1:{port}")]


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "[::1]", "2130706433", "0x7f.1", "017700000001", "127.1", "0.0.0.0", "metadata.internal"],
)
def test_loopback_spellings_are_blocked(host: str, local_server) -> None:
    port, hits = local_server
    with net_guard.network_policy_scope(False):
        _, _, status = validator.fetch_text(validator.requests.Session(), f"http://{host}:{port}/x", timeout=3)
        event = validator.first_network_event()
    assert status == 0
    assert hits == []
    assert event is not None and event["category"] == net_guard.BLOCKED_REASON_TEXT


def test_cloud_metadata_address_is_blocked_without_dns() -> None:
    validator.begin_network_stage()
    with net_guard.network_policy_scope(False):
        text, final_url, status = validator.fetch_text(validator.requests.Session(), "http://169.254.169.254/latest/meta-data/")
    assert (text, status) == ("", 0)
    assert final_url == "http://169.254.169.254/latest/meta-data/"
    event = validator.first_network_event()
    assert event is not None and event["category"] == net_guard.BLOCKED_REASON_TEXT


def test_blocked_fetch_is_reported_as_network_error_with_reason(local_server) -> None:
    port, hits = local_server
    validator.begin_network_stage()
    with net_guard.network_policy_scope(False):
        validator.fetch_text(validator.requests.Session(), f"http://127.0.0.1:{port}/x", timeout=3)
        validator.fetch_bytes(validator.requests.Session(), f"http://127.0.0.1:{port}/y", timeout=3)
    detail: dict[str, Any] = {}
    assert validator.attach_network_error(detail, "search") is True
    assert net_guard.BLOCKED_REASON_TEXT in detail["failureReason"]
    assert detail["networkError"]["category"] == net_guard.BLOCKED_REASON_TEXT
    assert net_guard.BLOCKED_REASON_TEXT in detail["error"]
    assert hits == []


def test_fetch_bytes_and_module_level_helpers_are_guarded(local_server) -> None:
    port, hits = local_server
    with net_guard.network_policy_scope(False):
        assert validator.fetch_bytes(validator.requests.Session(), f"http://127.0.0.1:{port}/b", timeout=3)[2] == 0
        for call in (validator.requests.get, validator.requests.head, validator.requests.post):
            with pytest.raises(requests.exceptions.ConnectionError) as info:
                call(f"http://127.0.0.1:{port}/h", timeout=3)
            assert _blocked_text(info.value)
    assert hits == []


def test_guard_covers_the_engine_not_just_one_call_site() -> None:
    """Every `requests.Session()` inside the engine module is the guarded one, including ones added later."""

    session = validator.requests.Session()
    assert isinstance(session, requests.Session)
    assert type(session) is net_guard.guarded_session_class()
    assert validator.make_source_session({"enabledCookieJar": False}).__class__ is net_guard.guarded_session_class()
    for prefix in ("http://", "https://"):
        assert isinstance(session.get_adapter(prefix + "example.test/"), net_guard.guarded_adapter_class())


# -------------------------------------------------------------------- redirects, pinning, DNS rebinding


def test_every_redirect_hop_is_rechecked_and_the_private_hop_is_never_requested(monkeypatch, local_server) -> None:
    port, hits = local_server
    _treat_loopback_as_public(monkeypatch)
    monkeypatch.setattr(
        net_guard, "default_resolver",
        lambda host: {"allowed.test": ["127.0.0.1"], "internal-hop.test": ["10.9.9.9"]}[host],
    )
    target = f"http://internal-hop.test:{port}/stolen"
    start = f"http://allowed.test:{port}/redir?to={target}"
    with net_guard.network_policy_scope(False):
        validator.begin_network_stage()
        text, _, status = validator.fetch_text(validator.requests.Session(), start, timeout=3)
        event = validator.first_network_event()
    assert (text, status) == ("", 0)
    assert event is not None and event["category"] == net_guard.BLOCKED_REASON_TEXT
    assert "10.9.9.9" in event["message"]
    assert hits == [(f"/redir?to={target}", f"allowed.test:{port}")]  # hop 1 only


def test_redirect_to_a_literal_private_address_is_blocked_on_the_second_hop(monkeypatch, local_server) -> None:
    port, hits = local_server
    _treat_loopback_as_public(monkeypatch)
    monkeypatch.setattr(net_guard, "default_resolver", lambda host: ["127.0.0.1"])
    start = f"http://allowed.test:{port}/redir?to=http://169.254.169.254/latest/"
    with net_guard.network_policy_scope(False):
        validator.begin_network_stage()
        _, _, status = validator.fetch_text(validator.requests.Session(), start, timeout=3)
        event = validator.first_network_event()
    assert status == 0
    assert event is not None and event["category"] == net_guard.BLOCKED_REASON_TEXT
    assert len(hits) == 1


def test_allowed_redirect_chain_still_works_and_the_connection_is_pinned(monkeypatch, local_server) -> None:
    """`allowed.test` does not exist in real DNS; the request can only succeed by dialling the validated address."""

    port, hits = local_server
    _treat_loopback_as_public(monkeypatch)
    monkeypatch.setattr(net_guard, "default_resolver", lambda host: ["127.0.0.1"])
    start = f"http://allowed.test:{port}/redir?to=http://other.test:{port}/final"
    with net_guard.network_policy_scope(False):
        text, final_url, status = validator.fetch_text(validator.requests.Session(), start, timeout=3)
    assert (text, status) == ("hello", 200)
    assert final_url == f"http://other.test:{port}/final"
    assert [host for _, host in hits] == [f"allowed.test:{port}", f"other.test:{port}"]  # Host header untouched


def test_dns_answer_changing_after_validation_cannot_change_the_dialled_address(monkeypatch, local_server) -> None:
    """The address that passed validation is the address that is dialled: the name is resolved exactly once per
    connection, so a rebinding resolver (public first, private afterwards) cannot redirect the connect."""

    port, hits = local_server
    _treat_loopback_as_public(monkeypatch)
    calls: list[str] = []

    def rebinding(host: str) -> list[str]:
        calls.append(host)
        return ["127.0.0.1"] if len(calls) == 1 else ["10.0.0.5"]

    monkeypatch.setattr(net_guard, "default_resolver", rebinding)
    with net_guard.network_policy_scope(False):
        text, _, status = validator.fetch_text(validator.requests.Session(), f"http://rebind.test:{port}/x", timeout=3)
    assert (text, status) == ("hello", 200)
    assert calls == ["rebind.test"]
    assert len(hits) == 1


def test_private_answer_at_connect_time_is_refused(monkeypatch, local_server) -> None:
    port, hits = local_server
    monkeypatch.setattr(net_guard, "default_resolver", lambda host: ["127.0.0.1"])
    with net_guard.network_policy_scope(False):
        validator.begin_network_stage()
        _, _, status = validator.fetch_text(validator.requests.Session(), f"http://rebind.test:{port}/x", timeout=3)
        event = validator.first_network_event()
    assert status == 0
    assert event is not None and event["category"] == net_guard.BLOCKED_REASON_TEXT
    assert hits == []


def test_mixed_public_and_private_answers_are_refused(monkeypatch, local_server) -> None:
    port, hits = local_server
    monkeypatch.setattr(net_guard, "default_resolver", lambda host: ["93.184.216.34", "127.0.0.1"])
    with net_guard.network_policy_scope(False):
        validator.begin_network_stage()
        _, _, status = validator.fetch_text(validator.requests.Session(), f"http://mixed.test:{port}/x", timeout=3)
    assert status == 0 and hits == []
    assert validator.first_network_event()["category"] == net_guard.BLOCKED_REASON_TEXT


def test_unresolvable_name_is_an_ordinary_dns_failure_not_a_block(monkeypatch) -> None:
    def failing(host: str) -> list[str]:
        raise OSError("no such host")

    monkeypatch.setattr(net_guard, "default_resolver", failing)
    with net_guard.network_policy_scope(False):
        validator.begin_network_stage()
        _, _, status = validator.fetch_text(validator.requests.Session(), "http://nowhere.test/", timeout=3)
    event = validator.first_network_event()
    assert status == 0
    assert event is not None and event["category"] != net_guard.BLOCKED_REASON_TEXT


def test_resolve_public_addresses_contract() -> None:
    resolver = {"ok.test": ["93.184.216.34", "2606:4700:4700::1111"], "bad.test": ["10.0.0.1"], "empty.test": []}.get
    assert net_guard.resolve_public_addresses("ok.test", resolver=lambda h: resolver(h)) == [
        "93.184.216.34", "2606:4700:4700::1111",
    ]
    assert net_guard.resolve_public_addresses("8.8.8.8") == ["8.8.8.8"]
    with pytest.raises(net_guard.BlockedAddressError):
        net_guard.resolve_public_addresses("bad.test", resolver=lambda h: resolver(h))
    with pytest.raises(net_guard.BlockedAddressError):
        net_guard.resolve_public_addresses("localhost")
    with pytest.raises(net_guard.BlockedAddressError):
        net_guard.resolve_public_addresses("2130706433")
    with pytest.raises(socket.gaierror):
        net_guard.resolve_public_addresses("empty.test", resolver=lambda h: resolver(h))


# ------------------------------------------------------------------------- the JS request replay is guarded


@needs_node
def test_js_ajax_request_replay_is_guarded(local_server) -> None:
    """java.ajax inside a source script is logged by the runner and replayed in Python (guarded session)."""

    port, hits = local_server
    runtime = _runtime()
    script = f"var r = java.ajax('http://127.0.0.1:{port}/from-js'); ''"
    with net_guard.network_policy_scope(False):
        validator.evaluate_js_with_node(script, "x", runtime)
    assert hits == []
    validator.evaluate_js_with_node(script, "x", runtime)  # default policy: the fixture is reachable
    assert any(path == "/from-js" for path, _ in hits)


# ------------------------------------------------------------------------------------------ CLI / server


def test_cli_block_private_network_option(monkeypatch, tmp_path: Path) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(validator, "_run_main", lambda args, version: seen.append(net_guard.network_allows_private()) or 0)
    assert validator.main(["--input", str(tmp_path)]) == 0
    assert validator.main(["--input", str(tmp_path), "--block-private-network"]) == 0
    assert seen == [True, False]
    assert net_guard.network_allows_private() is True  # restored after the run


def test_server_scopes_each_job_with_its_network_setting(monkeypatch, tmp_path: Path) -> None:
    from dataclasses import replace

    from server import source_validator_server as server

    for allow in (False, True):
        settings = replace(
            server.ServerSettings.from_env(),
            db_path=tmp_path / f"jobs-{allow}.sqlite3",
            allow_private_network=allow,
        )
        service = server.ValidationService(settings)
        try:
            source = {"bookSourceName": "A", "bookSourceUrl": "https://example.com", "searchUrl": "/s?q={{key}}", "ruleSearch": {}}
            service.store.create_job("job", server.prepare_source_groups([source]), {}, "x.json")
            seen: list[bool] = []
            monkeypatch.setattr(service, "_run_job_in_profile", lambda job_id: seen.append(net_guard.network_allows_private()))
            service._run_job("job")
            assert seen == [allow]
            assert net_guard.network_allows_private() is True  # scope does not leak out of the job
        finally:
            service.close()


def test_server_main_sets_process_default_from_env(monkeypatch) -> None:
    from server import source_validator_server as server

    class _Uvicorn:
        @staticmethod
        def run(*_a: Any, **_k: Any) -> None:
            seen.append(net_guard.network_allows_private())

    seen: list[bool] = []
    monkeypatch.setitem(sys.modules, "uvicorn", _Uvicorn)
    if server.FastAPI is None:
        pytest.skip("fastapi not installed")
    monkeypatch.setattr(server, "create_app", lambda settings=None: object())
    monkeypatch.delenv(net_guard.ALLOW_PRIVATE_ENV, raising=False)
    assert server.main(["--db", str(Path(os.devnull).parent / "x.sqlite3")]) == 0
    assert seen == [False]


# ====================================================================================== Node sandbox


def _runtime() -> Any:
    return validator.RuleRuntime(
        source_url="https://example.test/", base_url="https://example.test/",
        book_source_url="https://example.test", headers={},
    )


_ESCAPE_SCRIPT = r"""
var out = {};
function leaked(c) {
  try {
    if (!c) return false;
    if (c.versions && c.versions.node) return true;
    if (typeof c.getBuiltinModule === 'function' || typeof c.execSync === 'function' || typeof c.binding === 'function') return true;
    if (typeof c.fetch === 'function' || typeof c.require === 'function' || c.process) return true;
  } catch (e) {}
  return false;
}
function attempt(name, fn) {
  try { out[name] = leaked(fn()) ? 'LEAK' : 'no-host'; } catch (e) { out[name] = 'blocked'; }
}
attempt('constructor.constructor', function () { return constructor.constructor('return process')(); });
attempt('this.constructor.constructor', function () { return this.constructor.constructor('return this')(); });
attempt('host function ctor', function () { return btoa.constructor('return process')(); });
attempt('host function ctor global', function () { return md5.constructor('return this')(); });
attempt('java bridge ctor', function () { return java.md5Encode.constructor('return process')(); });
attempt('proto chain', function () { return Object.getPrototypeOf(btoa).constructor('return process')(); });
attempt('Reflect proto', function () { return Reflect.getPrototypeOf(btoa).constructor('return process')(); });
attempt('__proto__', function () { return btoa.__proto__.constructor('return process')(); });
attempt('Buffer ctor', function () { return Buffer.constructor('return process')(); });
attempt('URL ctor', function () { return URL.constructor('return process')(); });
attempt('async ctor', function () {
  var pending = Object.getPrototypeOf(async function () {}).constructor('return process')();  // a rejected promise would kill node
  pending.catch(function () {});
  return pending;
});
attempt('generator ctor', function () { return Object.getPrototypeOf(function* () {}).constructor('return process')().next().value; });
attempt('result ctor', function () { return result.constructor.constructor('return process')(); });
attempt('arguments.callee.caller', function () { return (function () { return arguments.callee.caller; })(); });
attempt('prepareStackTrace', function () {
  var captured, HostError;
  try { Buffer.from(Symbol()); } catch (e) { HostError = e.constructor; }
  HostError.prepareStackTrace = function (err, sites) { captured = sites[0].getThis(); return 'x'; };
  var holder = {}; HostError.captureStackTrace(holder); holder.stack;
  return captured;
});
attempt('bare process', function () { return process; });
attempt('bare require', function () { return require('child_process'); });
attempt('globalThis.process', function () { return globalThis.process; });
attempt('global', function () { return global; });
attempt('fetch', function () { return { fetch: fetch }; });
attempt('console stdout', function () { return console._stdout; });
attempt('XMLHttpRequest', function () { return new XMLHttpRequest(); });
attempt('WebSocket', function () { return new WebSocket('ws://127.0.0.1:1'); });
JSON.stringify(out);
"""


def _escape_results(script: str = _ESCAPE_SCRIPT) -> dict[str, str]:
    raw = validator.evaluate_js_with_node(script, "x", _runtime())
    assert raw, "the escape probe script must run to completion (empty result = node failed)"
    return json.loads(raw)


@needs_node
def test_constructor_escape_cannot_reach_process_require_or_the_network() -> None:
    results = _escape_results()
    assert results["constructor.constructor"] == "blocked"
    assert results["host function ctor"] == "blocked"
    leaks = {name: verdict for name, verdict in results.items() if verdict == "LEAK"}
    assert leaks == {}
    assert len(results) >= 20


@needs_node
def test_escape_cannot_read_files_run_programs_or_see_parent_secrets(monkeypatch, tmp_path: Path) -> None:
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("TOP-SECRET-FILE", encoding="utf-8")
    monkeypatch.setenv("READORI_SECRET_TEST", "TOP-SECRET-ENV")
    monkeypatch.setenv("READORI_VALIDATOR_API_KEY", "TOP-SECRET-API-KEY")
    script = r"""
var found = [];
var vectors = [
  function () { return constructor.constructor('return process')().env; },
  function () { return constructor.constructor('return process')().getBuiltinModule('child_process').execSync('echo pwn').toString(); },
  function () { return constructor.constructor('return process')().getBuiltinModule('fs').readFileSync(%s, 'utf8'); },
  function () { return btoa.constructor('return require')()('fs').readFileSync(%s, 'utf8'); },
  function () { return md5.constructor('return process.mainModule.require')()('child_process'); },
  function () { return String(Buffer.constructor('return process.env')()); },
];
for (var i = 0; i < vectors.length; i++) {
  try { var v = vectors[i](); if (v) found.push(i + ':' + String(typeof v === 'object' ? JSON.stringify(v) : v)); } catch (e) {}
}
JSON.stringify(found);
""" % (json.dumps(str(secret_file)), json.dumps(str(secret_file)))
    raw = validator.evaluate_js_with_node(script, "x", _runtime())
    assert raw == "[]"
    for needle in ("TOP-SECRET", "pwn"):
        assert needle not in raw


@needs_node
def test_runner_hardening_alone_blocks_the_escape_without_the_permission_model() -> None:
    """Defence in depth: the runner script is safe even when node has no permission model (older Node)."""

    script_path = validator.node_runner_script()
    assert script_path
    payload = json.dumps({"code": _ESCAPE_SCRIPT, "inputText": "x"})
    proc = subprocess.run(
        [NODE, script_path], input=payload, capture_output=True, text=True, encoding="utf-8",
        env=validator.node_sandbox_env(), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    results = json.loads(json.loads(proc.stdout)["value"])
    assert "LEAK" not in results.values()
    assert results["constructor.constructor"] == "blocked"


@needs_node
def test_control_the_probe_does_detect_the_escape_when_the_runner_is_not_hardened(tmp_path: Path) -> None:
    """Guards the guard: with the hardening call removed (and no permission model) the same probe leaks."""

    call = "__readoriHardenHostRealm();\n"
    assert validator.NODE_JS_RUNNER.count(call) == 1
    script = tmp_path / "unhardened.cjs"
    script.write_text(validator.NODE_JS_RUNNER.replace(call, "", 1), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(script)], input=json.dumps({"code": _ESCAPE_SCRIPT, "inputText": "x"}),
        capture_output=True, text=True, encoding="utf-8", env=validator.node_sandbox_env(), timeout=30,
    )
    results = json.loads(json.loads(proc.stdout)["value"])
    assert results["constructor.constructor"] == "LEAK"
    assert results["host function ctor"] == "LEAK"


def _node_child(code: str, *, permission: bool = True, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    flags = ["--permission", *(extra or [])] if permission else []
    return subprocess.run(
        [NODE, *flags, "-e", code], capture_output=True, text=True, encoding="utf-8",
        env=validator.node_sandbox_env(), timeout=30,
    )


@needs_permission
def test_permission_model_denies_fs_child_process_and_worker(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("data", encoding="utf-8")
    code = f"""
const out = {{}};
const tryit = (name, fn) => {{ try {{ fn(); out[name] = 'ALLOWED'; }} catch (e) {{ out[name] = e.code || 'ERR'; }} }};
tryit('read', () => process.getBuiltinModule('fs').readFileSync({json.dumps(str(outside))}, 'utf8'));
tryit('write', () => process.getBuiltinModule('fs').writeFileSync({json.dumps(str(tmp_path / 'w.txt'))}, 'x'));
tryit('spawn', () => process.getBuiltinModule('child_process').execSync('echo pwn'));
tryit('worker', () => new (process.getBuiltinModule('worker_threads').Worker)('1', {{ eval: true }}));
tryit('binding', () => process.binding('fs'));
tryit('dlopen', () => process.dlopen({{}}, 'nothing.node'));
console.log(JSON.stringify(out));
"""
    proc = _node_child(code)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert "ALLOWED" not in out.values(), out
    assert out["read"] == "ERR_ACCESS_DENIED"
    assert out["write"] == "ERR_ACCESS_DENIED"
    assert out["spawn"] == "ERR_ACCESS_DENIED"
    assert out["worker"] == "ERR_ACCESS_DENIED"
    assert not (tmp_path / "w.txt").exists()


@needs_node
def test_child_environment_is_minimal(monkeypatch) -> None:
    monkeypatch.setenv("READORI_SECRET_TEST", "TOP-SECRET-ENV")
    monkeypatch.setenv("READORI_VALIDATOR_API_KEY", "TOP-SECRET-API-KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak")
    monkeypatch.setenv("NODE_OPTIONS", "--require ./evil.js")
    env = validator.node_sandbox_env()
    assert "READORI_SECRET_TEST" not in env and "READORI_VALIDATOR_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert env["NODE_OPTIONS"] == validator.NODE_OPENSSL_LEGACY_OPTION
    assert {key.upper() for key in env} <= {name.upper() for name in validator.NODE_ENV_KEEP} | {"NODE_OPTIONS"}
    proc = _node_child("console.log(JSON.stringify(Object.keys(process.env)))", permission=False)
    assert proc.returncode == 0, proc.stderr
    keys = json.loads(proc.stdout)
    assert not any("SECRET" in key or "API_KEY" in key for key in keys)


def test_node_sandbox_env_keeps_windows_system_variables_case_insensitively() -> None:
    env = validator.node_sandbox_env({"SystemRoot": r"C:\Windows", "Path": "x", "Secret": "1", "TZ": "UTC"})
    assert env["SystemRoot"] == r"C:\Windows" and env["Path"] == "x" and env["TZ"] == "UTC"
    assert "Secret" not in env


@needs_node
def test_launch_command_uses_the_runner_file_and_grants_only_that_directory() -> None:
    command = validator.node_launch_command(NODE)
    script = Path(command[-1])
    assert script.is_file() and script.suffix == ".cjs"
    assert script.read_text(encoding="utf-8") == validator.NODE_JS_RUNNER
    assert len(validator.NODE_JS_RUNNER) > 30000  # why it cannot go on the command line with `-e` on Windows
    grants = [part for part in command if part.startswith("--allow-")]
    if validator.node_supports_permission_model(NODE):
        assert "--permission" in command
        assert grants == ["--allow-fs-read=" + str(script.parent)]
    else:
        assert grants == []


@needs_node
def test_old_node_without_permission_model_degrades_with_a_warning(monkeypatch, capsys) -> None:
    monkeypatch.setattr(validator, "_node_permission_support", {})

    class _Rejected:
        returncode = 9

    monkeypatch.setattr(validator.subprocess, "run", lambda *a, **k: _Rejected())
    assert validator.node_supports_permission_model(NODE) is False
    assert "--permission" not in validator.node_launch_command(NODE)
    assert "does not support --permission" in capsys.readouterr().err
    assert validator.node_supports_permission_model(NODE) is False  # cached: probed (and warned) once
    assert capsys.readouterr().err == ""


@needs_node
def test_legitimate_scripts_still_work_under_the_sandbox() -> None:
    runtime = _runtime()
    run = lambda code, text="abc": validator.evaluate_js_with_node(code, text, runtime)  # noqa: E731
    assert run("java.md5Encode(result)") == "900150983cd24fb0d6963f7d28e17f72"
    assert run("java.base64Encode('hi') + '|' + java.base64Decode('aGk=')") == "aGk=|hi"
    assert run("result.toUpperCase() + String(java.md5Encode16('abc').length)") == "ABC16"
    aes = run(
        "java.aesBase64DecodeToString(java.aesEncodeToBase64String('hello readori', 'k1k1k1k1k1k1k1k1', 'AES/CBC/PKCS5Padding', 'v1v1v1v1v1v1v1v1'),"
        " 'k1k1k1k1k1k1k1k1', 'AES/CBC/PKCS5Padding', 'v1v1v1v1v1v1v1v1')"
    )
    assert aes == "hello readori"
    assert run("JSON.stringify({a: [1, 2, 3].map(function (n) { return n * 2; })})") == '{"a":[2,4,6]}'
    assert run("var o = {}; o.constructor.name") == "Object"  # sandbox-realm constructors are untouched
    assert run("(function () { return (function () {}).constructor === Function; })()") == "true"


@needs_node
def test_script_errors_and_infinite_loops_stay_contained() -> None:
    assert validator.evaluate_js_with_node("throw new Error('boom')", "x", _runtime()) == ""
    assert validator.evaluate_js_with_node("while(true){}", "x", _runtime()) == ""
