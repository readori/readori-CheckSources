"""Unit tests for validator.net_guard (no real DNS: every lookup goes through a fake resolver)."""

import pytest

from validator import net_guard


def _resolver(mapping: dict[str, list[str]]):
    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise OSError("name not known")
        return mapping[host]

    return resolve


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "127.255.255.254", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255",
        "192.168.1.1", "169.254.169.254", "169.254.0.1", "0.0.0.0", "100.64.0.1", "100.100.100.200",
        "192.0.0.1", "198.18.0.1", "224.0.0.1", "240.0.0.1", "255.255.255.255",
        "::1", "::", "fc00::1", "fd12:3456::1", "fe80::1", "ff02::1", "fec0::1",
        "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "64:ff9b::7f00:1", "64:ff9b::a00:1",
        "2002:7f00:0001::1", "2002:c0a8:0101::1",
    ],
)
def test_non_public_ip_rejected(address: str) -> None:
    assert net_guard.is_public_ip(address) is False


@pytest.mark.parametrize(
    "address",
    ["8.8.8.8", "1.1.1.1", "93.184.216.34", "172.32.0.1", "172.15.255.255", "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"],
)
def test_public_ip_accepted(address: str) -> None:
    assert net_guard.is_public_ip(address) is True


def test_garbage_is_not_public() -> None:
    assert net_guard.is_public_ip("not-an-ip") is False


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "[::1]", "10.1.2.3", "2130706433", "0x7f.0.0.1", "0177.0.0.1", "127.1", "localhost", "LOCALHOST.", "api.localhost", "printer.local", "db.internal", "169.254.169.254"],
)
def test_private_hosts_blocked_without_dns(host: str) -> None:
    # A resolver that would say "public" must not matter for literals and reserved names.
    ok, reason = net_guard.check_host_allowed(host, resolver=_resolver({}))
    assert ok is False
    assert reason


def test_hostname_resolving_to_private_is_blocked() -> None:
    resolver = _resolver({"evil.example": ["8.8.8.8", "10.0.0.5"]})
    ok, reason = net_guard.check_host_allowed("evil.example", resolver=resolver)
    assert ok is False
    assert reason == "blocked-address:10.0.0.5"


def test_every_resolved_address_must_be_public() -> None:
    resolver = _resolver({"good.example": ["93.184.216.34", "2606:4700:4700::1111"], "bad6.example": ["93.184.216.34", "fe80::1"]})
    assert net_guard.is_public_address("good.example", resolver=resolver) is True
    assert net_guard.is_public_address("bad6.example", resolver=resolver) is False


def test_unresolvable_fails_closed_by_default() -> None:
    ok, reason = net_guard.check_host_allowed("nowhere.example", resolver=_resolver({}))
    assert (ok, reason) == (False, "unresolvable")
    assert net_guard.is_public_address("nowhere.example", resolver=_resolver({})) is False


def test_unresolvable_can_pass_through_for_callers_that_fail_later() -> None:
    ok, reason = net_guard.check_host_allowed("nowhere.example", resolver=_resolver({}), fail_closed_on_dns_error=False)
    assert (ok, reason) == (True, "")
    # ... but a resolvable private address is always blocked.
    ok, _ = net_guard.check_host_allowed("a.example", resolver=_resolver({"a.example": ["127.0.0.1"]}), fail_closed_on_dns_error=False)
    assert ok is False


def test_check_url_allowed_basic() -> None:
    resolver = _resolver({"books.example": ["93.184.216.34"], "intranet.example": ["192.168.0.9"]})
    assert net_guard.check_url_allowed("https://books.example/search?q=1", resolver=resolver) == (True, "")
    assert net_guard.check_url_allowed("http://books.example:8080/x", resolver=resolver) == (True, "")
    ok, reason = net_guard.check_url_allowed("http://intranet.example/", resolver=resolver)
    assert ok is False and reason.startswith("blocked-address:")
    for url in (
        "http://127.0.0.1:8787/healthz",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:7f00:1]/",
        "http://localhost/",
        "http://user:pw@127.0.0.1/",
        "http://0.0.0.0/",
    ):
        assert net_guard.check_url_allowed(url, resolver=resolver)[0] is False, url


def test_userinfo_does_not_hide_the_real_host() -> None:
    resolver = _resolver({"books.example": ["93.184.216.34"]})
    ok, _ = net_guard.check_url_allowed("http://books.example@127.0.0.1/", resolver=resolver)
    assert ok is False
    ok, _ = net_guard.check_url_allowed("http://127.0.0.1@books.example/", resolver=resolver)
    assert ok is True


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com", "javascript:alert(1)", "//example.com/x", "example.com", ""])
def test_non_http_schemes_and_missing_hosts_rejected(url: str) -> None:
    ok, reason = net_guard.check_url_allowed(url, resolver=_resolver({"example.com": ["93.184.216.34"]}))
    assert ok is False
    assert reason


def test_invalid_port_rejected() -> None:
    ok, reason = net_guard.check_url_allowed("http://example.com:99999/", resolver=_resolver({"example.com": ["93.184.216.34"]}))
    assert (ok, reason) == (False, "invalid-url")


def test_allow_private_switch() -> None:
    assert net_guard.check_url_allowed("http://127.0.0.1:8765/book", allow_private=True) == (True, "")
    assert net_guard.check_url_allowed("http://localhost:8765/book", allow_private=True) == (True, "")
    # The switch only lifts the address filter; scheme and host are still required.
    assert net_guard.check_url_allowed("file:///etc/passwd", allow_private=True)[0] is False
    assert net_guard.check_url_allowed("http:///nohost", allow_private=True)[0] is False


def test_allow_private_from_env() -> None:
    assert net_guard.allow_private_from_env({}) is False
    assert net_guard.allow_private_from_env({net_guard.ALLOW_PRIVATE_ENV: "0"}) is False
    assert net_guard.allow_private_from_env({net_guard.ALLOW_PRIVATE_ENV: ""}) is False
    for value in ("1", "true", "YES", " on "):
        assert net_guard.allow_private_from_env({net_guard.ALLOW_PRIVATE_ENV: value}) is True


def test_allow_private_from_env_reads_process_environment(monkeypatch) -> None:
    monkeypatch.delenv(net_guard.ALLOW_PRIVATE_ENV, raising=False)
    assert net_guard.allow_private_from_env() is False
    monkeypatch.setenv(net_guard.ALLOW_PRIVATE_ENV, "1")
    assert net_guard.allow_private_from_env() is True


def test_blocked_address_error_carries_the_stable_reason_text() -> None:
    error = net_guard.BlockedAddressError("http://127.0.0.1/x", "blocked-address:127.0.0.1")
    assert net_guard.BLOCKED_REASON_TEXT == "blocked: non-public address"
    assert str(error).startswith(net_guard.BLOCKED_REASON_TEXT)
    assert "127.0.0.1" in str(error) and isinstance(error, OSError)


def test_network_policy_defaults_to_allowing_private_and_nests() -> None:
    previous = net_guard.network_allows_private()
    try:
        net_guard.set_network_policy(True)
        assert net_guard.network_allows_private() is True
        with net_guard.network_policy_scope(False):
            assert net_guard.network_allows_private() is False
        net_guard.set_network_policy(False)
        assert net_guard.network_allows_private() is False
        with net_guard.network_policy_scope(True):
            assert net_guard.network_allows_private() is True
    finally:
        net_guard.set_network_policy(previous)
