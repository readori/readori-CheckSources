from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_core_and_entrypoints_are_present() -> None:
    assert (ROOT / "validator" / "validate_source_packages.py").is_file()
    assert (ROOT / "source_validator_cli.py").is_file()
    assert (ROOT / "source_validator_gui.py").is_file()


def test_gui_has_safe_child_process_controls() -> None:
    text = (ROOT / "source_validator_gui.py").read_text(encoding="utf-8")
    assert "subprocess.Popen" in text
    assert "process.terminate" in text
    assert "--source-timeout" in text

