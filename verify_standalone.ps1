param([string] $Python = "")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
    $Python = (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
    if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1) }
}
if (-not $Python) { throw "Python 3.12+ was not found. Pass -Python path." }

& $Python -m py_compile `
    (Join-Path $root "source_validator_gui.py") `
    (Join-Path $root "source_validator_cli.py") `
    (Join-Path $root "validator\validate_source_packages.py") `
    (Join-Path $root "static-tools\capture.py")
& $Python -c "import sys; sys.path.insert(0, r'$root'); import source_validator_gui; print('GUI_IMPORT_OK')"
Write-Host "Standalone static checks passed. Install requirements-validate-sources.txt before network validation."
