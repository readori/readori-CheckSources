from server.amd_micro_executor import _decode_queue_body, amd_config


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
