param([string] $Python = "")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
    $Python = (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
    if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1) }
}
if (-not $Python) { throw "Python 3.12+ was not found. Install it or pass -Python path." }
$venv = Join-Path $root ".venv"
if (-not (Test-Path -LiteralPath (Join-Path $venv "Scripts\python.exe"))) { & $Python -m venv $venv }
$venvPython = Join-Path $venv "Scripts\python.exe"
& $venvPython -m pip install -r (Join-Path $root "requirements-validate-sources.txt")
Write-Host "Dependencies installed. You can now run run_gui.cmd."
