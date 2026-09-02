from server.amd_micro_executor import _decode_queue_body, amd_config, build_executor_from_env


def test_queue_body_decodes_json_and_base64() -> None:
    assert _decode_queue_body('{"jobId":"one"}') == {"jobId": "one"}
    assert _decode_queue_body("eyJqb2JJZCI6Im9uZSJ9") == {"jobId": "one"}
    assert _decode_queue_body("not-a-message") is None


def test_amd_config_is_always_single_concurrency() -> None:
    config = amd_config({"workers": 16, "domain_concurrency": 8, "rounds": 5, "min_pass_rounds": 5})
    assert config["workers"] == 1
    assert config["domain_concurrency"] == 1
    assert config["rounds"] == 2
    assert config["min_pass_rounds"] == 2


def test_d1_lease_executor_does_not_require_queue_credentials(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("READORI_AMD_EXECUTOR_BASE_URL", "https://validator.example")
    monkeypatch.setenv("READORI_AMD_EXECUTOR_TOKEN", "executor-secret")
    monkeypatch.delenv("READORI_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("READORI_CF_QUEUE_ID", raising=False)
    monkeypatch.delenv("READORI_CF_QUEUE_API_TOKEN", raising=False)

    executor = build_executor_from_env(tmp_path, poll_seconds=1, retain_completed=True)

    assert executor.control.base_url == "https://validator.example"
    assert not hasattr(executor, "queue")
