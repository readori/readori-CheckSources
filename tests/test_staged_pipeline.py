from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_staged_pipeline_has_bounded_stages() -> None:
    text = (ROOT / "validator" / "validate_source_packages.py").read_text(encoding="utf-8")
    assert "def quick_scan_single_source" in text
    assert "def run_parallel_stage" in text
    assert "def run_staged_pipeline" in text
    assert '"quickScanTimeoutSeconds"' in text
    assert '"stabilityRetest"' in text


def test_gui_passes_quick_timeout_and_reports_stage() -> None:
    text = (ROOT / "source_validator_gui.py").read_text(encoding="utf-8")
    assert "quick_timeout_var" in text
    assert '"--quick-timeout"' in text
    assert "STAGE_RE" in text


def test_gui_keeps_file_and_directory_inputs_separate() -> None:
    text = (ROOT / "source_validator_gui.py").read_text(encoding="utf-8")
    assert "file_input_var" in text
    assert "directory_input_var" in text
    assert "self.directory_input_var.set(\"\")" in text
    assert "self.file_input_var.set(\"\")" in text
