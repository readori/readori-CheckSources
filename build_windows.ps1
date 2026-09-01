param(
    [string] $Python = "",
    [string] $OutputDir = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
    $commands = @(
        (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
        (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
    )
    $Python = $commands | Where-Object { $_ } | Select-Object -First 1
}
if (-not $Python) {
    throw "Python 3.12+ was not found. Install it or pass -Python path."
}

$venv = Join-Path $root ".venv"
if (-not (Test-Path -LiteralPath (Join-Path $venv "Scripts\python.exe"))) {
    & $Python -m venv $venv
}
$venvPython = Join-Path $venv "Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $root "requirements-validate-sources.txt") pyinstaller

if (-not $OutputDir) {
    $OutputDir = Join-Path $root "dist"
}
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$buildRoot = Join-Path $root "build"
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null

$common = @(
    "--clean", "--noconfirm", "--onefile",
    "--distpath", $OutputDir,
    "--workpath", (Join-Path $buildRoot "pyinstaller-work"),
    "--specpath", (Join-Path $buildRoot "spec")
)
& $venvPython -m PyInstaller @common "--name" "ReadoriSourceValidatorCLI" "--console" (Join-Path $root "source_validator_cli.py")
& $venvPython -m PyInstaller @common "--name" "ReadoriSourceValidator" "--windowed" (Join-Path $root "source_validator_gui.py")

$readmeSource = [System.IO.Path]::GetFullPath((Join-Path $root "README.md"))
$readmeDestination = [System.IO.Path]::GetFullPath((Join-Path $OutputDir "README.md"))
if (-not [System.String]::Equals($readmeSource, $readmeDestination, [System.StringComparison]::OrdinalIgnoreCase)) {
    Copy-Item $readmeSource $readmeDestination -Force
}
Write-Host "Build completed:"
Write-Host (Join-Path $OutputDir "ReadoriSourceValidator.exe")
Write-Host (Join-Path $OutputDir "ReadoriSourceValidatorCLI.exe")
