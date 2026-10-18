"""R146 WP-C: static checks of the desktop workbench (no window is opened, nothing is clicked).

Same style as test_staged_pipeline / test_standalone_layout: read the sources and assert the structure that keeps the
window safe -- an rpc child process, reader threads that only enqueue, UI updates through ``after``.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import workbench_gui
from validator import rule_tester

ROOT = Path(__file__).resolve().parents[1]
GUI = (ROOT / "source_validator_gui.py").read_text(encoding="utf-8")
WORKBENCH = (ROOT / "workbench_gui.py").read_text(encoding="utf-8")
CLI_ENTRY = (ROOT / "source_validator_cli.py").read_text(encoding="utf-8")


def test_main_gui_has_the_workbench_button_and_opens_a_single_window() -> None:
    assert "书源工作台" in GUI
    assert "def _open_workbench" in GUI and "WorkbenchWindow(" in GUI
    assert "child_command=child_command" in GUI  # the same CLI launcher as the batch runs
    assert ".lift()" in GUI  # an already open workbench is raised, not duplicated
    # the workbench is imported lazily: the batch window must keep working without it
    top_level_imports = [line for line in GUI.splitlines() if line.startswith(("import ", "from "))]
    assert not any("workbench" in line for line in top_level_imports)


def test_workbench_talks_to_an_rpc_child_process() -> None:
    assert "child_command" in WORKBENCH
    assert re.search(r'list\(self\.child_command\(\)\)\s*\+\s*\["rpc"\]', WORKBENCH)
    assert "subprocess.Popen" in WORKBENCH and "CREATE_NO_WINDOW" in WORKBENCH
    assert 'encoding="utf-8"' in WORKBENCH
    assert "stdin=subprocess.PIPE" in WORKBENCH and "stdout=subprocess.PIPE" in WORKBENCH


def test_workbench_never_imports_the_validator() -> None:
    tree = ast.parse(WORKBENCH)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name.split(".")[0] == "validator" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in {"validator", "validate_source_packages", "debug_engine"}
    assert not re.search(r"^\s*(from|import)\s+validator", WORKBENCH, re.M)
    assert "from source_validator_gui import child_command" in WORKBENCH  # only the standalone start-up path


def test_reader_threads_only_enqueue_and_the_ui_drains_with_after() -> None:
    assert "threading.Thread(target=self._read_stdout" in WORKBENCH
    assert "threading.Thread(target=self._read_stderr" in WORKBENCH
    assert "queue.Queue" in WORKBENCH and "self.messages.put(" in WORKBENCH
    assert "self.after(DRAIN_MS, self._drain)" in WORKBENCH
    tree = ast.parse(WORKBENCH)
    readers = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in {"_read_stdout", "_read_stderr"}]
    assert {n.name for n in readers} == {"_read_stdout", "_read_stderr"}
    for node in readers:
        body = ast.get_source_segment(WORKBENCH, node) or ""
        assert "tk." not in body and "self.status_var" not in body and "messagebox" not in body
        assert "self.messages.put(" in body


def test_stop_cancels_then_kills_and_restarts() -> None:
    assert 'self.client.send("cancel"' in WORKBENCH
    assert "STOP_GRACE_MS = 3000" in WORKBENCH
    assert "_stop_watchdog" in WORKBENCH and "self.client.kill()" in WORKBENCH
    assert "def _resync" in WORKBENCH and '"restore"' in WORKBENCH  # edits survive a restart
    assert "self.client.kill()" in WORKBENCH.split("def _close", 1)[1]  # closing the window ends the child


def test_layout_has_the_three_columns_and_all_tabs() -> None:
    assert "ttk.PanedWindow" in WORKBENCH and 'self.minsize(1100, 700)' in WORKBENCH
    assert list(workbench_gui.FIELD_SPEC) == ["基本", "搜索", "发现", "详情", "目录", "正文"]
    for text in ("撤销", "重做", "还原", "应用 JSON", "格式化", "导入…", "导出已修改…", "▶ 调试", "⏭ 单步", "■ 停止",
                 "校验此源", "修复前后对比", "校验关键字", "++目录", "--正文"):
        assert f'"{text}"' in WORKBENCH, text
    for tab in ("日志", "原始响应", "规则测试", "建议", "差异", "对比", "帮助"):
        assert f'text="{tab}"' in WORKBENCH, tab
    assert '"JSON"' in WORKBENCH
    assert "1.1.1（当前公开版）" in WORKBENCH  # the default version is the public build


def test_field_spec_matches_the_rule_tester() -> None:
    paths = [spec[0] for specs in workbench_gui.FIELD_SPEC.values() for spec in specs]
    assert len(paths) == len(set(paths))
    for path, label, kind, hint, testable in (s for specs in workbench_gui.FIELD_SPEC.values() for s in specs):
        assert kind in {"line", "text", "bool", "int"} and label
        assert (path in rule_tester.FIELD_KIND) == testable, path
    assert set(workbench_gui.RULE_TEST_FIELDS) <= set(rule_tester.FIELD_KIND)
    # every field the editor can test has a "▶" hint row in the spec (Legado editor hint texts)
    assert "ruleSearch.bookList" in workbench_gui.RULE_TEST_FIELDS and "ruleContent.replaceRegex" in workbench_gui.RULE_TEST_FIELDS
    assert "@CSS:" in workbench_gui.HELP_TEXT and "调试 key" in workbench_gui.HELP_TEXT


def test_entry_point_and_build_scripts_know_the_workbench() -> None:
    assert '"validate-one"' in CLI_ENTRY and "def dispatch" in CLI_ENTRY
    verify = (ROOT / "verify_standalone.ps1").read_text(encoding="utf-8")
    assert "workbench_gui.py" in verify and r"validator\workbench_cli.py" in verify
    build = (ROOT / "build_windows.ps1").read_text(encoding="utf-8")
    assert "validator.workbench_cli" in build and "workbench_gui" in build


def test_new_files_use_lf_line_endings() -> None:
    # Checkouts may convert to CRLF (core.autocrlf on Windows); only mixed endings are a defect.
    for path in (ROOT / "workbench_gui.py", ROOT / "validator" / "workbench_cli.py", ROOT / "source_validator_cli.py",
                 Path(__file__)):
        raw = path.read_bytes()
        assert raw.count(b"\r\n") in (0, raw.count(b"\n")), path.name


def test_self_test_builds_the_control_tree_without_a_window() -> None:
    proc = subprocess.run([sys.executable, str(ROOT / "workbench_gui.py"), "--self-test"], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "WORKBENCH_SELF_TEST_OK" in proc.stdout or "WORKBENCH_SELF_TEST_SKIPPED" in proc.stdout
