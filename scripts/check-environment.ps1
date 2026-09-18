param(
    [string]$Python = "python",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$previousPythonPath = $env:PYTHONPATH
$previousBytecode = $env:PYTHONDONTWRITEBYTECODE

try {
    $env:PYTHONPATH = (Join-Path $root "src") + [IO.Path]::PathSeparator + $previousPythonPath
    $env:PYTHONDONTWRITEBYTECODE = "1"

    & $Python -c "import pathlib, sys, SLiCAP, fastapi, pydantic, PySide6, sfg_prototype; from PySide6 import QtWebEngineWidgets; assert sys.version_info[:2] == (3, 12); assert SLiCAP.__version__ == '5.2.1'; print('Python', sys.version.split()[0]); print('Executable', pathlib.Path(sys.executable).resolve()); print('SLiCAP', SLiCAP.__version__); print('PySide6', PySide6.__version__); print('FastAPI', fastapi.__version__); print('Pydantic', pydantic.__version__); print('Bundled SFG', pathlib.Path(sfg_prototype.__file__).resolve())"
    if ($LASTEXITCODE -ne 0) { throw "Python environment check failed." }

    & $Python -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Installed Python dependencies are inconsistent." }

    if (-not $SkipTests) {
        & $Python -m pytest (Join-Path $root "tests") -q -p no:cacheprovider
        if ($LASTEXITCODE -ne 0) { throw "ISACA desktop and algorithm tests failed." }
    } else {
        Write-Host "Full test suite skipped; version, import, and dependency checks passed."
    }
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONDONTWRITEBYTECODE = $previousBytecode
}
