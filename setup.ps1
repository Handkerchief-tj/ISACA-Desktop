param(
    [string]$Python = "python",
    [switch]$SkipTests,
    [switch]$WithDeploy
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path

Push-Location $root
try {
    & $Python -c "import sys; assert sys.version_info[:2] == (3, 12), 'ISACA requires Python 3.12'"
    if ($LASTEXITCODE -ne 0) { throw "Activate a Python 3.12 environment first." }

    $extras = if ($WithDeploy) { ".[test,deploy]" } else { ".[test]" }
    Write-Host "Installing ISACA from source with extras: $extras"
    & $Python -m pip install -e $extras
    if ($LASTEXITCODE -ne 0) { throw "ISACA dependency installation failed." }

    Write-Host "On the first SLiCAP import, answer 'n' to the NGspice prompt unless it is installed."
    $checkScript = Join-Path $root "scripts\check-environment.ps1"
    & $checkScript -Python $Python -SkipTests:$SkipTests
    if ($LASTEXITCODE -ne 0) { throw "ISACA verification failed." }
} finally {
    Pop-Location
}

Write-Host "ISACA Desktop is ready. Start it with .\start-desktop.ps1"
