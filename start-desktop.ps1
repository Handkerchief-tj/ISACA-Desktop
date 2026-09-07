param(
    [string]$Python = "python",
    [string]$Project = "",
    [string]$File = ""
)

$ErrorActionPreference = "Stop"
$previousPythonPath = $env:PYTHONPATH
$previousEncoding = $env:PYTHONIOENCODING
try {
    $env:PYTHONPATH = (Join-Path $PSScriptRoot "src") + [IO.Path]::PathSeparator + $previousPythonPath
    $env:PYTHONIOENCODING = "utf-8"
    & $Python -c "import sys; from importlib.metadata import version; assert sys.version_info[:2] == (3, 12), 'Activate slicap5_env (Python 3.12)'; assert version('SLiCAP') == '5.2.1', 'SLiCAP==5.2.1 required'; import PySide6.QtWebEngineWidgets; import sfg_prototype"
    if ($LASTEXITCODE -ne 0) { throw "Desktop environment check failed. Activate slicap5_env first." }
    $arguments = @("-m", "isaca_desktop")
    if ($Project) { $arguments += @("--project", $Project) }
    if ($File) { $arguments += @("--file", $File) }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw "ISACA desktop exited with code $LASTEXITCODE." }
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONIOENCODING = $previousEncoding
}
