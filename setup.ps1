param(
    [string]$Python = "python",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path

Push-Location $root
try {
    & $Python -c "import sys; assert sys.version_info[:2] == (3, 12), 'ISACA requires Python 3.12'"
    if ($LASTEXITCODE -ne 0) { throw "Activate a Python 3.12 environment first." }

    & $Python -m pip install -e ".[test]"
    if ($LASTEXITCODE -ne 0) { throw "ISACA dependency installation failed." }

    if (-not $SkipTests) {
        & (Join-Path $root "scripts\check-environment.ps1") -Python $Python
        if ($LASTEXITCODE -ne 0) { throw "ISACA verification failed." }
    }
} finally {
    Pop-Location
}

Write-Host "ISACA Desktop is ready. Start it with .\start-desktop.ps1"
