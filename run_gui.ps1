param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Show-StartupError([string] $message) {
    try {
        Add-Type -AssemblyName PresentationFramework -ErrorAction Stop
        [System.Windows.MessageBox]::Show($message, "Readori Source Validator") | Out-Null
    } catch {
        Write-Error $message
    }
}

function Resolve-BootstrapPython {
    $candidates = @(
        (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
        (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
        (Join-Path $root "python\python.exe")
    )
    return $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}

$bootstrapPython = Resolve-BootstrapPython
$venv = Join-Path $root ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not $bootstrapPython -and -not (Test-Path -LiteralPath $venvPython)) {
    Show-StartupError "Python 3.12+ was not found. Install Python first, then run this launcher again."
    exit 1
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    try {
        & $bootstrapPython -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed with exit code $LASTEXITCODE" }
    } catch {
        Show-StartupError "Could not create the local Python environment.`n$($_.Exception.Message)"
        exit 1
    }
}

$depsReady = $false
try {
    & $venvPython -c "import requests, bs4, jsonpath_ng" 2>$null
    $depsReady = ($LASTEXITCODE -eq 0)
} catch {
    $depsReady = $false
}

if (-not $depsReady) {
    $logDir = Join-Path $root "output"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $installLog = Join-Path $logDir "dependency_install.log"
    try {
        $pipOutput = & $venvPython -m pip install -r (Join-Path $root "requirements-validate-sources.txt") 2>&1
        $pipOutput | Out-File -FilePath $installLog -Encoding utf8
        if ($LASTEXITCODE -ne 0) { throw "pip exited with code $LASTEXITCODE" }
    } catch {
        Show-StartupError "Required validator dependencies could not be installed.`nSee: $installLog`n$($_.Exception.Message)"
        exit 1
    }
}

try {
    & $venvPython (Join-Path $root "source_validator_gui.py") @Arguments
    exit $LASTEXITCODE
} catch {
    Show-StartupError "Could not start the Readori validator GUI.`n$($_.Exception.Message)"
    exit 1
}

