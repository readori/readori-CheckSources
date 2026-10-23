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
    assert "CREATE_NO_WINDOW" in text


def test_validator_reports_external_directory_paths_without_relative_to_crash() -> None:
    text = (ROOT / "validator" / "validate_source_packages.py").read_text(encoding="utf-8")
    assert "def display_path" in text
    assert "loaded_names.append(display_path(path, root))" in text


def test_app_icon_assets_are_present_and_png_is_120_square() -> None:
    png = ROOT / "assets" / "readori-icon.png"
    ico = ROOT / "assets" / "readori-icon.ico"
    favicon = ROOT / "cloudflare" / "public" / "favicon.png"
    assert png.is_file() and ico.is_file() and favicon.is_file()
    data = png.read_bytes()
    assert data[:8] == bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])
    assert data[12:16] == b"IHDR"
    assert int.from_bytes(data[16:20], "big") == 120
    assert int.from_bytes(data[20:24], "big") == 120
    assert favicon.read_bytes() == data
    # ICO header: reserved 0, type 1 (icon), then the six requested sizes (16/24/32/48/64/120).
    ico_bytes = ico.read_bytes()
    assert ico_bytes[:4] == bytes([0, 0, 1, 0])
    assert int.from_bytes(ico_bytes[4:6], "little") >= 6


def test_build_script_embeds_icon_for_both_executables() -> None:
    text = (ROOT / "build_windows.ps1").read_text(encoding="utf-8")
    assert "--icon" in text
    assert "readori-icon.ico" in text
    assert "readori-icon.png" in text
    assert text.count("--add-data") == 2


def test_gui_windows_set_the_readori_icon() -> None:
    gui = (ROOT / "source_validator_gui.py").read_text(encoding="utf-8")
    assert "def apply_window_icon" in gui
    assert "apply_window_icon(self)" in gui
    assert "iconphoto" in gui and "iconbitmap" in gui
    assert "_MEIPASS" in gui
    workbench = (ROOT / "workbench_gui.py").read_text(encoding="utf-8")
    assert workbench.count("apply_window_icon(root)") == 2


def test_console_pages_link_the_favicon() -> None:
    for name in ("index.html", "debug.html"):
        html = (ROOT / "cloudflare" / "public" / name).read_text(encoding="utf-8")
        assert 'rel="icon" type="image/png" href="/favicon.png"' in html
