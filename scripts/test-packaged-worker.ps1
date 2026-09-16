param(
    [Parameter(Mandatory = $true)]
    [string]$Executable,
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$exe = (Resolve-Path -LiteralPath $Executable).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $root "build\packaged-worker-smoke"
}
$output = [IO.Path]::GetFullPath($OutputDirectory)
$prefix = $root.TrimEnd('\') + '\'
if (-not $output.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Smoke-test output must stay inside the repository: $output"
}
if (Test-Path -LiteralPath $output) {
    Remove-Item -LiteralPath $output -Recurse -Force
}
New-Item -ItemType Directory -Path $output | Out-Null

$netlist = Get-Content -LiteralPath (Join-Path $root "examples\desktop\rc_lowpass.cir") -Raw
$request = @{
    action = "analysis"
    job_id = "packaged-rc-smoke"
    run_root = $output
    result_path = (Join-Path $output "worker-result.json")
    payload = @{
        request = @{
            netlist_text = $netlist
            modes = @("laplace", "pz", "matrix", "bode", "symbolic")
            frequency_range_hz = @(1.0, 100000.0)
            magnitude_error_db = 2.0
            phase_error_deg = 5.0
            max_steps_per_subrange = 10
        }
    }
}
$requestPath = Join-Path $output "request.json"
$request | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $requestPath -Encoding utf8

$stdout = Join-Path $output "events.ndjson"
$stderr = Join-Path $output "worker-stderr.log"
$process = Start-Process -FilePath $exe -ArgumentList @("--worker", $requestPath) -WorkingDirectory $output -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
if ($process.ExitCode -ne 0) {
    throw "Packaged worker exited with $($process.ExitCode). See $stderr"
}
$result = Get-Content -LiteralPath (Join-Path $output "worker-result.json") -Raw | ConvertFrom-Json
if ($result.status -ne "completed") {
    throw "Packaged worker did not complete: $($result.error)"
}
$pole = [double]$result.result.analyses.pz.poles[0]
if ([math]::Abs($pole + 1000.0) -gt 1e-6) {
    throw "Unexpected RC pole from packaged worker: $pole"
}
$codes = @($result.result.diagnostics | ForEach-Object { $_.code })
if ($codes -contains "graphviz_unavailable") {
    throw "Packaged worker did not find its private Graphviz runtime."
}
Write-Host "Packaged worker smoke test passed; RC pole = $pole rad/s"
