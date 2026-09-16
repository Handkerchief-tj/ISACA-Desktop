param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [string]$CacheDirectory = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$version = "16.1.0"
$archiveName = "windows_10_cmake_Release_Graphviz-$version-win64.zip"
$url = "https://gitlab.com/api/v4/projects/4207231/packages/generic/graphviz-releases/$version/$archiveName"
$expectedSha256 = "733e49626c492242eb8dca30ea627b6ead20710e207998c7933b4909d92d6abc"

function Assert-WithinRepository([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = $root.TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Release path is outside the repository: $full"
    }
    return $full
}

if (-not $CacheDirectory) {
    $CacheDirectory = Join-Path $root "build\cache"
}
$cache = Assert-WithinRepository $CacheDirectory
$destinationPath = Assert-WithinRepository $Destination
New-Item -ItemType Directory -Path $cache -Force | Out-Null
$archive = Join-Path $cache $archiveName

$download = $true
if (Test-Path -LiteralPath $archive) {
    $download = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedSha256
}
if ($download) {
    $temporaryArchive = "$archive.download"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $temporaryArchive
    $actual = (Get-FileHash -LiteralPath $temporaryArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expectedSha256) {
        Remove-Item -LiteralPath $temporaryArchive -Force
        throw "Graphviz SHA-256 mismatch: expected $expectedSha256, got $actual"
    }
    Move-Item -LiteralPath $temporaryArchive -Destination $archive -Force
}

$extract = Join-Path $cache "graphviz-$version-extracted"
if (Test-Path -LiteralPath $extract) {
    Remove-Item -LiteralPath $extract -Recurse -Force
}
New-Item -ItemType Directory -Path $extract | Out-Null
Expand-Archive -LiteralPath $archive -DestinationPath $extract -Force
$dot = Get-ChildItem -LiteralPath $extract -Recurse -Filter dot.exe | Select-Object -First 1
if (-not $dot) {
    throw "The verified Graphviz archive does not contain dot.exe."
}
$graphvizRoot = Split-Path (Split-Path $dot.FullName -Parent) -Parent
if (Test-Path -LiteralPath $destinationPath) {
    Remove-Item -LiteralPath $destinationPath -Recurse -Force
}
New-Item -ItemType Directory -Path $destinationPath | Out-Null
Copy-Item -Path (Join-Path $graphvizRoot "*") -Destination $destinationPath -Recurse -Force

$privateDot = Join-Path $destinationPath "bin\dot.exe"
if (-not (Test-Path -LiteralPath $privateDot)) {
    throw "Private Graphviz layout is invalid: $privateDot"
}
& $privateDot -V
if ($LASTEXITCODE -ne 0) {
    throw "Private Graphviz dot.exe did not start successfully."
}
Write-Host "Private Graphviz $version prepared at $destinationPath"
