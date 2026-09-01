import time
from pathlib import Path

from server import source_validator_server as server


def _source(url: str, name: str = "Example") -> dict:
    return {
        "bookSourceName": name,
        "bookSourceUrl": url,
        "searchUrl": "/search?q={{key}}",
        "ruleSearch": {"bookList": "//book", "name": "//title"},
    }


def _full_detail() -> dict:
    return {
        "stages": {"search": True, "detail": True, "toc": True, "content": True},
        "detailBookName": "Example book",
        "tocUniqueChapterCount": 2,
        "contentPreviewChars": 80,
        "sampleBookUrl": "https://example.com/book/1",
    }


def test_canonical_key_groups_url_variants() -> None:
    first = _source("HTTPS://Example.com:443/path/?b=2&a=1")
    second = _source("https://example.com/path?a=1&b=2")

    assert server.canonical_source_key(first) == server.canonical_source_key(second)
    groups = server.prepare_source_groups([first, second])
    assert len(groups) == 1
    assert groups[0]["duplicate_count"] == 1
    assert len(groups[0]["variants"]) == 2


def test_job_store_closes_sqlite_connections(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = server.JobStore(database)
    store.create_job("job", server.prepare_source_groups([_source("https://example.com")]), {}, "test.json")
    assert store.job("job")["source_count"] == 1

    del store
    database.unlink()


def test_service_runs_staged_pipeline_and_persists_result(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_quick(url: str, candidates: list[dict]) -> tuple[str, dict, dict]:
        calls.append("quick")
        source = dict(candidates[0])
        return url, {"source": source, "bookUrls": ["https://example.com/book/1"]}, {"pipelineStage": "quick-scan", "stages": {"search": True}}

    def fake_full(url: str, candidates: list[dict], quick_seed: dict | None = None) -> tuple[str, dict, dict]:
        calls.append("full")
        record = dict(candidates[0])
        record["customTag"] = "✅ 书籍+详情+目录+正文通过"
        return url, record, _full_detail()

    monkeypatch.setattr(server.core, "quick_scan_group", fake_quick)
    monkeypatch.setattr(server.core, "validate_group", fake_full)

    settings = server.ServerSettings(
        db_path=tmp_path / "jobs.sqlite3",
        max_workers=2,
        max_jobs=1,
        default_domain_concurrency=2,
    )
    service = server.ValidationService(settings)
    try:
        groups = server.prepare_source_groups(
            [_source("https://example.com", "A"), _source("https://example.com/", "A"), _source("https://other.example", "B")]
        )
        job_id = "staged-job"
        service.store.create_job(job_id, groups, {"rounds": 1, "min_pass_rounds": 1, "profile": "device"}, "test.json")
        assert service.submit(job_id)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = service.store.job(job_id)
            if job and job["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.02)

        job = service.store.job(job_id)
        assert job is not None
        assert job["status"] == "completed"
        assert job["source_count"] == 2
        assert job["duplicate_count"] == 1
        assert job["passed_count"] == 2
        assert len(service.result(job_id)) == 2
        assert calls.count("quick") == 2
        assert calls.count("full") == 2
        events = service.store.events(job_id)
        assert any(event["stage"] == "completed" for event in events)
    finally:
        service.close()


def test_device_gate_rejects_incomplete_chain() -> None:
    ok, reason = server.detail_is_device_compatible(_source("https://example.com"), {"stages": {}}, {})
    assert not ok
    assert "incomplete" in reason


def test_device_gate_handles_malformed_metrics() -> None:
    detail = _full_detail()
    detail["tocUniqueChapterCount"] = "not-a-number"
    ok, reason = server.detail_is_device_compatible(_source("https://example.com"), detail, {})
    assert not ok
    assert "empty chapter" in reason


def test_amd_micro_profile_clamps_runtime_limits(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("READORI_VALIDATOR_EXECUTOR_PROFILE", "amd-micro")
    monkeypatch.setenv("READORI_VALIDATOR_MAX_WORKERS", "16")
    monkeypatch.setenv("READORI_VALIDATOR_MAX_JOBS", "4")
    monkeypatch.setenv("READORI_VALIDATOR_DOMAIN_CONCURRENCY", "8")
    settings = server.ServerSettings.from_env()
    assert settings.amd_micro is True
    assert settings.max_workers == 1
    assert settings.max_jobs == 1
    assert settings.default_domain_concurrency == 1

    settings = server.ServerSettings(db_path=tmp_path / "amd.sqlite3", max_workers=16, max_jobs=4, amd_micro=True)
    service = server.ValidationService(settings)
    try:
        config = service._load_config({"config_json": '{"workers":16,"domain_concurrency":8,"rounds":5,"min_pass_rounds":5}'})
        assert config["workers"] == 1
        assert config["domain_concurrency"] == 1
        assert config["rounds"] == 2
    finally:
        service.close()


def test_result_export_is_not_truncated_at_one_thousand_sources(tmp_path: Path) -> None:
    settings = server.ServerSettings(db_path=tmp_path / "jobs.sqlite3", max_jobs=1)
    store = server.JobStore(settings.db_path)
    service = server.ValidationService(settings, store)
    try:
        groups = server.prepare_source_groups(
            [_source(f"https://example-{index}.com", f"Example {index}") for index in range(1005)]
        )
        store.create_job("large-job", groups, {"rounds": 1, "min_pass_rounds": 1}, "large.json")
        for row in store.all_sources("large-job"):
            store.update_source(
                "large-job",
                row["source_key"],
                quick_status="passed",
                full_status="passed",
                stability_pass_count=1,
                stability_last_round=1,
                final_status="passed",
                result_json=row["source_json"],
            )
        assert len(service.result("large-job")) == 1005
    finally:
        service.close()
