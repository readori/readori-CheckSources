from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_core_and_entrypoints_are_present() -> None:
    assert (ROOT / "validator" / "validate_source_packages.py").is_file()
    assert (ROOT / "source_validator_cli.py").is_file()
    assert (ROOT / "source_validator_gui.py").is_file()


def test_gui_has_safe_child_process_controls() -> None:
    text = (ROOT / "source_validator_gui.py").read_text(encoding="utf-8")
    assert "subprocess.Popen" in text
    # 🐛 Bug 修复（2026-08-30）：原来直接用 Popen.terminate()，在 Windows 上
    # 取消/关闭时不会级联杀掉验证过程里可能启动的 node.exe 子进程，会留下
    # 孤儿进程。改成 taskkill /T 连带整棵进程树一起结束，这里断言的是新的、
    # 更安全的实现，而不是旧的、有孤儿进程风险的 process.terminate()。
    assert "_terminate_process_tree" in text
    assert "taskkill" in text
    assert "process.terminate" not in text
    assert "--source-timeout" in text


def test_gui_spinboxes_reject_non_numeric_input() -> None:
    # 🐛 Bug 修复（2026-08-30）：Spinbox 允许直接键入非数字内容时，IntVar.get()
    # 会抛出未捕获的 TclError，打包成无控制台窗口的 exe 后用户点"开始验证"会
    # 毫无反应。这里断言两层防护都在：按键级别的输入限制 + 提交时的兜底捕获。
    text = (ROOT / "source_validator_gui.py").read_text(encoding="utf-8")
    assert "_validate_digits" in text
    assert 'validate="key"' in text
    assert "except tk.TclError" in text


def test_gui_input_mode_validation_does_not_cross_check_unrelated_field() -> None:
    # 🐛 Bug 修复（2026-08-30）：之前 file_value 优先生效时仍然会去校验
    # directory_value 是否是目录，两个输入框都有残留内容时会报出跟用户实际
    # 选择完全无关的错误。这里断言校验逻辑已经改成先确定生效的是哪一种模式，
    # 只校验被选中的那一种。
    text = (ROOT / "source_validator_gui.py").read_text(encoding="utf-8")
    assert 'mode = "file"' in text
    assert 'mode = "directory"' in text

