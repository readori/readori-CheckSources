"""R146 WP-C: workbench sub-commands (debug / test-rule / fix / validate-one / export / rpc).

Runs against the local fixture site; no external network.  Console commands run in-process (``workbench_cli.main``),
the dispatcher / help / rpc tests use a real child process.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import debug_fixture_site as fixture_site
import source_validator_cli
from validator import workbench_cli as wb

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "source_validator_cli.py"


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
def files(site, tmp_path):
    """``all.json`` (every fixture source, in fixture order) and one file per source name."""
    names = list(site.sources)
    all_path = tmp_path / "all.json"
    all_path.write_text(json.dumps([site.sources[n] for n in names], ensure_ascii=False), encoding="utf-8")
    out = {"all": all_path, "names": names}
    for name in ("good", "lazy_cover", "toc_broken", "search_broken"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(site.sources[name], ensure_ascii=False), encoding="utf-8")
        out[name] = path
    return SimpleNamespace(**out)


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    code = wb.main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# dispatch: batch mode untouched
# ---------------------------------------------------------------------------

def test_dispatch_without_subcommand_runs_the_batch_main(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(source_validator_cli, "main", lambda: calls.append("batch") or 7)
    monkeypatch.setattr(wb, "main", lambda argv=None: calls.append("workbench") or 9)
    for argv in ([], ["--input", "a.json"], ["--help"], ["--app-version", "1.1.2"], ["debugger"], ["input.json"]):
        assert source_validator_cli.dispatch(argv) == 7
    assert calls == ["batch"] * 6


@pytest.mark.parametrize("command", ["debug", "test-rule", "fix", "validate-one", "export", "rpc"])
def test_dispatch_routes_workbench_commands(monkeypatch, command: str) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(source_validator_cli, "main", lambda: pytest.fail("batch main must not run"))
    monkeypatch.setattr(wb, "main", lambda argv=None: seen.append(list(argv)) or 11)
    assert source_validator_cli.dispatch([command, "x.json", "--json"]) == 11
    assert seen == [[command, "x.json", "--json"]]
    assert command in source_validator_cli.WORKBENCH_COMMANDS and command in wb.SUBCOMMANDS


def test_batch_help_keeps_its_flags_and_hides_the_workbench() -> None:
    proc = subprocess.run([sys.executable, str(CLI), "--help"], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0
    for flag in ("--input", "--report-path", "--validated-output", "--workers", "--quick-timeout", "--rounds",
                 "--min-pass-rounds", "--source-timeout", "--app-version", "--block-private-network", "--pipeline"):
        assert flag in proc.stdout, flag
    assert "validate-one" not in proc.stdout


@pytest.mark.parametrize("command", ["debug", "test-rule", "fix", "validate-one", "export"])
def test_subcommand_help_runs(command: str) -> None:
    proc = subprocess.run([sys.executable, str(CLI), command, "--help"], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0
    assert "--source" in proc.stdout and "--app-version" in proc.stdout


# ---------------------------------------------------------------------------
# source selection
# ---------------------------------------------------------------------------

def _rows() -> list[dict]:
    return [
        {"bookSourceName": "笔趣阁", "bookSourceUrl": "https://a.test"},
        {"bookSourceName": "笔趣阁二站", "bookSourceUrl": "https://b.test/"},
        {"bookSourceName": "起点", "bookSourceUrl": "https://c.test#tag"},
    ]


def test_select_source_precedence_and_ambiguity() -> None:
    rows = _rows()
    assert wb.select_source(rows, "https://a.test")[0] == 0           # exact URL
    assert wb.select_source(rows, "https://b.test")[0] == 1           # normalized (trailing slash)
    assert wb.select_source(rows, "https://c.test")[0] == 2           # effective base URL (fragment removed)
    assert wb.select_source(rows, "笔趣阁")[0] == 0                   # exact name beats substring
    assert wb.select_source(rows, "起")[0] == 2                       # unique substring
    assert wb.select_source(rows, index=1)[0] == 1
    assert wb.select_source(rows[:1])[0] == 0                         # a single source needs no selector
    with pytest.raises(wb.CliError) as ambiguous:
        wb.select_source(rows, "笔趣")                                # two substring hits
    assert "笔趣阁二站" in str(ambiguous.value) and ambiguous.value.code == 2
    with pytest.raises(wb.CliError):
        wb.select_source(rows)                                        # several sources, no selector
    with pytest.raises(wb.CliError):
        wb.select_source(rows, "nope")
    with pytest.raises(wb.CliError):
        wb.select_source(rows, index=9)
    with pytest.raises(wb.CliError):
        wb.select_source(rows, "笔趣阁", index=0)


# ---------------------------------------------------------------------------
# debug
# ---------------------------------------------------------------------------

def test_debug_search_text_output_and_exit_0(files, capsys, tmp_path) -> None:
    raw_dir = tmp_path / "raw"
    code, out, err = run(["debug", files.good, "--key", "我的", "--save-raw", raw_dir], capsys)
    assert code == 0, err
    assert out.startswith("书源：夹具站")
    assert "⇒开始搜索关键字:我的" in out and "︽搜索页解析完成" in out and "✓ 正文调试完成" in out
    assert (raw_dir / "index.json").is_file() and (raw_dir / "variables.json").is_file()
    assert any(p.name.endswith("-search.html") for p in raw_dir.iterdir())


def test_debug_pick_book_and_chapter(files, capsys) -> None:
    code, out, _err = run(["debug", files.good, "--key", "我的", "--pick-book", "1", "--pick-chapter", "2"], capsys)
    assert code == 0
    assert "/book/2" in out


def test_debug_jsonl_every_line_is_an_event(files, capsys) -> None:
    code, out, _err = run(["debug", files.good, "--key", "++/book/1/toc", "--jsonl"], capsys)
    assert code == 0
    events = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert events and all("kind" in e and "seq" in e for e in events)
    assert events[-1]["kind"] == "verdict" and events[-1]["state"] == 1000
    assert any(e["kind"] == "fetch" and e["raw_id"] for e in events)


def test_debug_json_summary_goes_to_stdout_log_to_stderr(files, capsys) -> None:
    code, out, err = run(["debug", files.good, "--key", "我的", "--json"], capsys)
    assert code == 0
    summary = json.loads(out)
    assert summary["status"] == "pass" and summary["exitCode"] == 0 and summary["key"]["kind"] == "search"
    assert "⇒开始搜索关键字" in err


def test_debug_auto_key_runs_the_validation_chain(files, capsys) -> None:
    code, out, _err = run(["debug", files.good], capsys)
    assert code == 0
    assert "✓ 校验结论：可用 · App 1.1.1" in out


def test_debug_failing_source_exits_1(files, capsys) -> None:
    code, out, _err = run(["debug", files.search_broken, "--key", "我的"], capsys)
    assert code == 1
    assert "列表大小:0" in out and "未获取到书籍" in out


def test_debug_timeout_exits_4(site, tmp_path, capsys) -> None:
    path = tmp_path / "slow.json"
    path.write_text(json.dumps(site.sources["slow"], ensure_ascii=False), encoding="utf-8")
    code, out, _err = run(["debug", path, "--key", "我的", "--timeout", "1"], capsys)
    assert code == 4
    assert "校验超时" in out


@pytest.mark.parametrize("argv, needle", [
    (["debug", "missing-file.json"], "找不到输入"),
    (["debug", "{all}"], "--source"),                              # several sources, none selected
    (["debug", "{all}", "--source", "no-such-source"], "没有匹配"),
    (["debug", "{good}", "--key", "热门::"], "链接不能为空"),
    (["debug", "{good}", "--source", "x", "--index", "0"], "不能同时使用"),
    (["test-rule", "{good}", "--field", "ruleNope.x", "--body-file", "x"], "不支持的字段"),
    (["test-rule", "{good}", "--field", "ruleSearch.name"], "需要响应内容"),
    (["test-rule", "{good}", "--field", "ruleSearch.name", "--raw-id", "1", "--body-file", "x"], "--raw"),
])
def test_usage_errors_exit_2(files, capsys, argv, needle) -> None:
    argv = [str(a).replace("{all}", str(files.all)).replace("{good}", str(files.good)) for a in argv]
    code, _out, err = run(argv, capsys)
    assert code == 2
    assert needle in err


def test_argparse_usage_error_returns_2(capsys) -> None:
    code, _out, err = run(["debug"], capsys)
    assert code == 2 and "usage" in err.lower()
    assert wb.main([]) == 2


def test_validate_one_needs_interaction_exits_3(site, tmp_path, capsys) -> None:
    src = copy.deepcopy(site.sources["good"])
    src["loginCheckJs"] = "java.startBrowserAwait('https://x.test/verify','验证');result"
    path = tmp_path / "interactive.json"
    path.write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")
    code, out, _err = run(["validate-one", path, "--app-version", "1.1.2"], capsys)
    assert code == 3
    assert "需要登录/验证" in out
    code111, _out, _err = run(["validate-one", path, "--app-version", "1.1.1"], capsys)
    assert code111 == 1  # the public build rejects such a source instead of asking for interaction


# ---------------------------------------------------------------------------
# validate-one
# ---------------------------------------------------------------------------

def test_validate_one_pass_and_fail(files, capsys) -> None:
    code, out, _err = run(["validate-one", files.good], capsys)
    assert code == 0 and out.startswith("✓ 可用 · App 1.1.1") and "搜索✓ 详情✓ 目录✓ 正文✓" in out
    code, out, _err = run(["validate-one", files.toc_broken], capsys)
    assert code == 1 and "toc failed" in out and "目录✗" in out


def test_validate_one_json_trace_and_both_versions(files, capsys) -> None:
    code, out, err = run(["validate-one", files.good, "--json", "--trace", "--app-version", "both"], capsys)
    assert code == 0
    summary = json.loads(out)
    assert set(summary["results"]) == {"1.1.1", "1.1.2"} and summary["compatibility"] == "两版均可用"
    assert "校验结论" in err  # --trace log goes to stderr next to --json
    code, out, _err = run(["validate-one", files.good, "--jsonl", "--trace"], capsys)
    lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert code == 0 and lines[-1]["kind"] == "verdict"


# ---------------------------------------------------------------------------
# test-rule
# ---------------------------------------------------------------------------

def test_test_rule_on_saved_raw_and_body_file(files, capsys, tmp_path) -> None:
    raw_dir = tmp_path / "raw"
    assert run(["debug", files.good, "--key", "我的", "--save-raw", raw_dir], capsys)[0] == 0
    code, out, _err = run(["test-rule", files.good, "--field", "ruleSearch.name", "--raw", raw_dir], capsys)
    assert code == 0 and "我的老婆是女帝" in out and "列表大小：2" in out
    code, out, err = run(["test-rule", files.good, "--field", "ruleSearch.bookList", "--raw", raw_dir,
                          "--rule", "class.no-such-list@tag.li"], capsys)
    assert code == 1 and "列表大小：0" in out and "没有匹配" in err
    body = next(p for p in raw_dir.iterdir() if p.name.endswith("-search.html"))
    code, out, _err = run(["test-rule", files.good, "--field", "ruleSearch.author", "--body-file", body,
                           "--base-url", "https://x.test", "--item", "1", "--json"], capsys)
    data = json.loads(out)
    assert code == 0 and data["ok"] and data["exitCode"] == 0 and data["kind"] == "string"
    code, out, _err = run(["test-rule", files.good, "--field", "ruleSearch.bookUrl", "--body-file", body,
                           "--base-url", "https://x.test", "--jsonl"], capsys)
    assert code == 0 and json.loads(out.splitlines()[-1])["value"] == "https://x.test/book/1"


# ---------------------------------------------------------------------------
# fix / export
# ---------------------------------------------------------------------------

def test_fix_dry_run_writes_nothing(files, capsys, tmp_path) -> None:
    out_path, report_path = tmp_path / "fixed.json", tmp_path / "fix-report.json"
    code, out, err = run(["fix", files.lazy_cover, "--apply", "F5", "--out", out_path, "--report", report_path,
                          "--dry-run"], capsys)
    assert code == 0, err
    assert "F5" in out and "已应用" in out and "--dry-run：未写文件" in out
    assert not out_path.exists() and not report_path.exists()
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(("fixed", "fix-report"))) == []


def test_fix_apply_safe_revalidate_writes_fixed_source_and_report(files, capsys, tmp_path) -> None:
    out_path, report_path = tmp_path / "fixed.json", tmp_path / "fix-report.json"
    code, out, err = run(["fix", files.lazy_cover, "--apply-safe", "--revalidate", "--out", out_path,
                          "--report", report_path], capsys)
    assert code == 0, err
    assert "修复前：可用 → 修复后：可用" in out and "已写" in out
    fixed = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(fixed) == 1 and fixed[0]["ruleSearch"]["coverUrl"] == "tag.img@data-original"
    assert fixed[0]["bookSourceComment"].startswith("// Fixed: ") and "America/Los_Angeles" in fixed[0]["bookSourceComment"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema"] == "readori.fix-report/1"
    item = report["sources"][0]
    assert item["changes"][0]["path"] == "ruleSearch.coverUrl" and item["changes"][0]["origin"] == "suggestion:F5"
    assert item["before"]["verdict"] == "pass" and item["after"]["verdict"] == "pass"
    assert item["comment"].startswith("// Fixed: ")


def test_fix_refuses_a_suggestion_whose_preview_failed(files, capsys, tmp_path) -> None:
    out_path = tmp_path / "fixed.json"
    code, out, _err = run(["fix", files.good, "--apply", "F7,F9", "--out", out_path], capsys)
    assert not out_path.exists()
    assert "F7 未应用：预览未通过" in out and "F9 未应用" in out
    assert code == 0  # the source itself passes; nothing to fix


def test_fix_json_summary_lists_suggestions(files, capsys) -> None:
    code, out, _err = run(["fix", files.lazy_cover, "--dry-run", "--json"], capsys)
    data = json.loads(out)
    assert code == 0 and data["dryRun"] is True and data["written"] == {}
    assert {s["code"] for s in data["suggestions"]} >= {"F5"}
    assert all("preview" in s for s in data["suggestions"])


def test_fix_domain_migration_changes_source_url_unless_kept(site, files, capsys, tmp_path) -> None:
    moved = files.names.index("moved")
    out_path = tmp_path / "moved.json"
    code, out, err = run(["fix", files.all, "--index", moved, "--apply", "F1", "--out", out_path,
                          "--report", tmp_path / "r.json"], capsys)
    assert code == 0, err
    fixed = json.loads(out_path.read_text(encoding="utf-8"))[0]
    assert fixed["bookSourceUrl"] == site.alt
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["sources"][0]
    assert report["originalBookSourceUrl"] == site.base and report["bookSourceUrl"] == site.alt

    kept = tmp_path / "kept.json"
    run(["fix", files.all, "--index", moved, "--apply", "F1", "--keep-source-url", "--out", kept,
         "--report", tmp_path / "r2.json"], capsys)
    # with the primary key kept there is nothing left to patch for this source, so no file is written
    assert not kept.exists()


def test_export_from_report_applies_changes_and_filters(files, capsys, tmp_path) -> None:
    fixed_path, report_path = tmp_path / "fixed.json", tmp_path / "fix-report.json"
    assert run(["fix", files.lazy_cover, "--apply", "F5", "--out", fixed_path, "--report", report_path], capsys)[0] == 0
    exported = tmp_path / "export.json"
    code, out, err = run(["export", files.lazy_cover, "--from-report", report_path, "--only-changed", "--out", exported],
                         capsys)
    assert code == 0, err
    rows = json.loads(exported.read_text(encoding="utf-8"))
    assert len(rows) == 1 and rows[0]["ruleSearch"]["coverUrl"] == "tag.img@data-original"
    assert rows[0]["bookSourceComment"].startswith("// Fixed: ")
    assert "__readoriValidation" not in rows[0]
    # a file whose source has no change in the report: --only-changed has nothing to write
    other = tmp_path / "other.json"
    other.write_text(json.dumps({**json.loads(files.good.read_text("utf-8")), "bookSourceUrl": "https://other.test"}),
                     encoding="utf-8")
    nothing = tmp_path / "nothing.json"
    code, out, err = run(["export", other, "--from-report", report_path, "--only-changed", "--out", nothing], capsys)
    assert code == 1 and not nothing.exists() and "找不到" in err
    # several sources share the URL (the fixture family): the report item is matched by name
    both = tmp_path / "both.json"
    code, _out, err = run(["export", files.all, "--from-report", report_path, "--only-changed", "--out", both], capsys)
    assert code == 0, err
    rows = json.loads(both.read_text(encoding="utf-8"))
    assert [r["bookSourceName"] for r in rows] == ["夹具站·lazy_cover"]
    # plain export keeps every source untouched
    code, _out, _err = run(["export", files.all, "--out", tmp_path / "plain.json"], capsys)
    assert code == 0 and len(json.loads((tmp_path / "plain.json").read_text(encoding="utf-8"))) == len(files.names)
    bad = tmp_path / "bad-report.json"
    bad.write_text("{}", encoding="utf-8")
    assert run(["export", files.good, "--from-report", bad], capsys)[0] == 2


# ---------------------------------------------------------------------------
# step prompt
# ---------------------------------------------------------------------------

def test_prompt_pick_resumes_or_cancels(monkeypatch) -> None:
    from validator import debug_engine

    event = {"kind": "pause", "data": {"what": "book", "choices": [{"index": 0}, {"index": 1}, {"index": 2}], "total": 3}}
    answers = iter(["9", "x", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    controller = debug_engine.DebugController()
    wb._prompt_pick(event, controller)
    assert controller.wait_choice(0.5) == 2
    monkeypatch.setattr("builtins.input", lambda _prompt="": "q")
    wb._prompt_pick(event, controller)
    assert controller.cancelled


# ---------------------------------------------------------------------------
# rpc
# ---------------------------------------------------------------------------

class _Rpc:
    """A child ``source_validator_cli.py rpc`` process with a reader thread."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen([sys.executable, "-u", str(CLI), "rpc"], cwd=ROOT, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     bufsize=1)
        self.frames: list[dict] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        self._next = 1

    def _read(self) -> None:
        for line in self.proc.stdout:
            with self._lock:
                self.frames.append(json.loads(line))

    def send(self, method: str, **params) -> int:
        rid, self._next = self._next, self._next + 1
        self.proc.stdin.write(json.dumps({"id": rid, "method": method, "params": params}, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return rid

    def wait(self, predicate, timeout: float = 30.0):
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                for frame in self.frames:
                    if predicate(frame):
                        return frame
            time.sleep(0.02)
        raise AssertionError(f"timeout; frames so far: {self.frames[-5:]}")

    def reply(self, rid: int, timeout: float = 30.0):
        return self.wait(lambda f: f.get("id") == rid and ("result" in f or "error" in f), timeout)

    def done(self, rid: int, timeout: float = 60.0):
        return self.wait(lambda f: f.get("id") == rid and "done" in f, timeout)["done"]

    def events(self, rid: int) -> list[dict]:
        with self._lock:
            return [f["event"] for f in self.frames if f.get("id") == rid and "event" in f]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            self.proc.kill()


@pytest.fixture()
def rpc():
    client = _Rpc()
    try:
        client.wait(lambda f: f.get("ready"), 30)
        yield client
    finally:
        client.close()


def test_rpc_load_edit_debug_cancel_and_done(files, rpc) -> None:
    loaded = rpc.reply(rpc.send("load", path=str(files.all)))["result"]
    assert loaded["count"] == len(files.names) and loaded["sources"][0]["sourceKey"] == "s0"
    assert loaded["sources"][0]["name"] == "夹具站"

    rid = rpc.send("edit", sourceKey="s0", op="set", path="ruleSearch.name", value="tag.h3@html")
    state = rpc.reply(rid)["result"]
    assert state["changed"] and state["canUndo"] and state["view"]["ruleSearch"]["name"] == "tag.h3@html"
    assert state["diff"] == [{"path": "ruleSearch.name", "old": "tag.h3@text", "new": "tag.h3@html"}]
    assert rpc.reply(rpc.send("edit", sourceKey="s0", op="undo"))["result"]["changed"] is False

    # a running debug is stopped by cancel and ends with a done frame (stdout stays pure JSON)
    rid = rpc.send("debug", sourceKey="s0", key="我的", step=True)
    assert rpc.reply(rid)["result"]["started"] is True
    rpc.wait(lambda f: f.get("id") == rid and f.get("event", {}).get("kind") == "pause")
    assert rpc.reply(rpc.send("debug", sourceKey="s0", key="我的"))["error"] == "正在运行"
    rpc.send("cancel")
    done = rpc.done(rid)
    assert done["status"] == "cancelled" and done["ok"] is False

    # a normal run: events stream with text, then done carries the summary and raw list
    rid = rpc.send("debug", sourceKey="s0", key="我的")
    done = rpc.done(rid)
    assert done["status"] == "pass" and done["ok"] and done["rawCount"] >= 3 and done["raw"][0]["raw_id"] == 1
    events = rpc.events(rid)
    assert events and any(e.get("text") for e in events) and events[-1]["kind"] == "verdict"

    raw = rpc.reply(rpc.send("raw_get", sourceKey="s0", rawId=1, maxBytes=64))["result"]
    assert raw["status"] == 200 and raw["body"]
    suggestions = rpc.reply(rpc.send("suggest", sourceKey="s0"))["result"]["suggestions"]
    assert {"id", "code", "title", "patch", "apply_allowed"} <= set(suggestions[0])
    result = rpc.reply(rpc.send("test_rule", sourceKey="s0", field="ruleSearch.bookList"))["result"]
    assert result["ok"] and result["items_count"] == 2
    unknown = rpc.reply(rpc.send("no_such_method"))
    assert "未知方法" in unknown["error"]


def test_rpc_validate_compare_and_export(files, rpc, tmp_path) -> None:
    rpc.reply(rpc.send("load", path=str(files.lazy_cover)))
    rid = rpc.send("validate", sourceKey="s0", quickTimeout=8, sourceTimeout=30)
    done = rpc.done(rid)
    assert done["ok"] and done["result"]["verdict"] == "pass" and "detail" not in done["result"]
    assert any(e["kind"] == "verdict" for e in rpc.events(rid))

    rpc.reply(rpc.send("edit", sourceKey="s0", op="set", path="ruleSearch.coverUrl", value="tag.img@data-original",
                       reason="封面懒加载"))
    done = rpc.done(rpc.send("compare", sourceKey="s0", sourceTimeout=30))
    assert done["before"]["verdict"] == "pass" and done["after"]["verdict"] == "pass"
    assert [row["label"] for row in done["rows"]] == ["快速扫描", "搜索", "详情", "目录", "正文", "结论"]

    target, report = tmp_path / "out.json", tmp_path / "rep.json"
    exported = rpc.reply(rpc.send("export", onlyChanged=True, path=str(target), reportPath=str(report)))["result"]
    assert exported["count"] == 1 and exported["written"]["path"] == str(target)
    row = json.loads(target.read_text(encoding="utf-8"))[0]
    assert row["ruleSearch"]["coverUrl"] == "tag.img@data-original" and row["bookSourceComment"].startswith("// Fixed: ")
    assert json.loads(report.read_text(encoding="utf-8"))["sources"][0]["after"]["verdict"] == "pass"


def test_rpc_server_edit_replace_restore_and_errors(files) -> None:
    frames: list[dict] = []
    server = wb.RpcServer(frames.append)
    server.handle({"id": 1, "method": "load", "params": {"path": str(files.good)}})
    assert frames[-1]["result"]["count"] == 1
    server.handle({"id": 2, "method": "edit", "params": {"sourceKey": "s0", "op": "replace",
                                                         "json": json.dumps({**json.loads(files.good.read_text("utf-8")),
                                                                              "bookSourceGroup": "组"})}})
    state = frames[-1]["result"]
    assert state["working"]["bookSourceGroup"] == "组" and state["canUndo"]
    server.handle({"id": 3, "method": "edit", "params": {"sourceKey": "s0", "op": "restore",
                                                         "working": state["working"]}})
    assert frames[-1]["result"]["working"]["bookSourceGroup"] == "组"
    server.handle({"id": 4, "method": "edit", "params": {"sourceKey": "s0", "op": "replace", "json": "[1]"}})
    assert "书源对象" in frames[-1]["error"]
    server.handle({"id": 5, "method": "edit", "params": {"sourceKey": "s9", "op": "get"}})
    assert "没有这条书源" in frames[-1]["error"]
    server.handle({"id": 6, "method": "debug", "params": {"sourceKey": "s0", "key": "热门::"}})
    assert "链接不能为空" in frames[-1]["error"]
    server.handle({"id": 7, "method": "suggest", "params": {"sourceKey": "s0"}})
    deadline = time.time() + 5
    while time.time() < deadline and "还没有调试记录" not in str(frames[-1].get("error", "")):
        time.sleep(0.02)
    assert "还没有调试记录" in frames[-1]["error"]
    server.handle("not an object")
    assert frames[-1]["error"]
