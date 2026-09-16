param(
    [string]$Version = "0.1.0",
    [string]$BootstrapPython = "python",
    [switch]$SkipTests,
    [switch]$ReuseCompiledStandalone,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$previousLocation = Get-Location

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $Program $($Arguments -join ' ')"
    }
}

function Assert-WithinRepository([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = $root.TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Release path is outside the repository: $full"
    }
    return $full
}

function Remove-SafeDirectory([string]$Path) {
    $full = Assert-WithinRepository $Path
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force
    }
}

function Clear-SensitiveBuildEnvironment {
    Get-ChildItem Env: | Where-Object {
        $_.Name -match '(?i)(api[_-]?key|token|secret|password|credential)'
    } | ForEach-Object {
        Remove-Item -LiteralPath ("Env:" + $_.Name) -ErrorAction SilentlyContinue
    }
}

try {
    Set-Location $root
    $buildEnvironment = Assert-WithinRepository (Join-Path $root ".venv-build")
    $buildPython = Join-Path $buildEnvironment "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $buildPython)) {
        Invoke-Checked $BootstrapPython @("-m", "venv", $buildEnvironment)
    }

    $versionCheck = & $buildPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0 -or $versionCheck.Trim() -ne "3.12") {
        throw "The release environment must use Python 3.12; found $versionCheck"
    }

    Invoke-Checked $buildPython @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
    Invoke-Checked $buildPython @(
        "-m", "pip", "install",
        "--constraint", (Join-Path $root "packaging\constraints-release.txt"),
        ".[test,deploy]"
    )
    Invoke-Checked $buildPython @(
        "-c",
        "import SLiCAP, PySide6, sys; assert sys.version_info[:2] == (3, 12); assert SLiCAP.__version__ == '5.2.1'; assert PySide6.__version__ == '6.11.2'; print('release runtime verified')"
    )

    if (-not $SkipTests) {
        Invoke-Checked $buildPython @("-m", "pytest", "tests", "-q", "-p", "no:cacheprovider")
    }

    Clear-SensitiveBuildEnvironment
    $standaloneRoot = Assert-WithinRepository (Join-Path $root "build\standalone")
    if (-not $ReuseCompiledStandalone) {
        Remove-SafeDirectory $standaloneRoot
        New-Item -ItemType Directory -Path $standaloneRoot -Force | Out-Null
        $temporarySpec = Assert-WithinRepository (Join-Path $root "build\pysidedeploy.spec")
        $spec = Get-Content -LiteralPath (Join-Path $root "pysidedeploy.spec") -Raw
        $spec = $spec -replace '(?m)^project_dir =.*$', "project_dir = $root"
        $spec = $spec -replace '(?m)^input_file =.*$', "input_file = $(Join-Path $root 'release_entry.py')"
        $spec = $spec -replace '(?m)^exec_directory =.*$', "exec_directory = $standaloneRoot"
        $spec = $spec -replace '(?m)^python_path =.*$', "python_path = $buildPython"
        [IO.File]::WriteAllText($temporarySpec, $spec, [Text.UTF8Encoding]::new($false))

        $deploy = Join-Path $buildEnvironment "Scripts\pyside6-deploy.exe"
        if (-not (Test-Path -LiteralPath $deploy)) {
            throw "pyside6-deploy is missing from the release environment."
        }
        Invoke-Checked $deploy @(
            "-c", $temporarySpec,
            "--force",
            "--keep-deployment-files",
            "--extra-ignore-dirs", "docs,examples,tests,runs,build,dist,.venv-build"
        )
    }

    $executable = @(
        Get-ChildItem -LiteralPath $standaloneRoot -Recurse -Filter ISACA.exe -ErrorAction SilentlyContinue
        Get-ChildItem -LiteralPath (Join-Path $root "deployment") -Recurse -Filter ISACA.exe -ErrorAction SilentlyContinue
    ) | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $executable) {
        throw "No compiled ISACA.exe was found. Run the standalone compilation first."
    }
    $applicationDirectory = $executable.Directory.FullName

    # Nuitka does not discover DLLs linked by Conda's standard-library .pyd files.
    $pythonBase = (& $buildPython -c "import sys; print(sys.base_prefix)").Trim()
    $condaRuntime = Join-Path $pythonBase "Library\bin"
    foreach ($runtimeName in @(
        "ffi.dll",
        "libbz2.dll",
        "libcrypto-3-x64.dll",
        "libexpat.dll",
        "liblzma.dll",
        "libssl-3-x64.dll",
        "sqlite3.dll"
    )) {
        $runtimeSource = Join-Path $condaRuntime $runtimeName
        if (-not (Test-Path -LiteralPath $runtimeSource)) {
            throw "Required Conda runtime DLL is missing: $runtimeSource"
        }
        Copy-Item -LiteralPath $runtimeSource -Destination (Join-Path $applicationDirectory $runtimeName) -Force
    }

    $pytexitVersionSource = Join-Path $buildEnvironment "Lib\site-packages\pytexit\__version__.txt"
    $pytexitDirectory = Join-Path $applicationDirectory "pytexit"
    if (-not (Test-Path -LiteralPath $pytexitVersionSource)) {
        throw "Required pytexit version resource is missing: $pytexitVersionSource"
    }
    New-Item -ItemType Directory -Path $pytexitDirectory -Force | Out-Null
    Copy-Item -LiteralPath $pytexitVersionSource -Destination (Join-Path $pytexitDirectory "__version__.txt") -Force

    & (Join-Path $PSScriptRoot "fetch-graphviz.ps1") -Destination (Join-Path $applicationDirectory "graphviz")
    if ($LASTEXITCODE -ne 0) {
        throw "Private Graphviz preparation failed."
    }
    $licenses = Join-Path $applicationDirectory "licenses"
    Invoke-Checked $buildPython @(
        (Join-Path $PSScriptRoot "collect-release-licenses.py"),
        $licenses,
        "--notices", (Join-Path $root "packaging\THIRD_PARTY_NOTICES.md"),
        "--katex-license", (Join-Path $root "src\isaca_desktop\resources\katex\LICENSE")
    )
    New-Item -ItemType Directory -Path (Join-Path $applicationDirectory "docs") -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination (Join-Path $applicationDirectory "docs\README.md") -Force
    Copy-Item -LiteralPath (Join-Path $root "docs\desktop\usage.md") -Destination (Join-Path $applicationDirectory "docs\USER_GUIDE.md") -Force
    Set-Content -LiteralPath (Join-Path $applicationDirectory "VERSION.txt") -Value $Version -Encoding utf8

    & (Join-Path $PSScriptRoot "test-packaged-worker.ps1") -Executable $executable.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged worker smoke test failed."
    }

    $dist = Assert-WithinRepository (Join-Path $root "dist")
    New-Item -ItemType Directory -Path $dist -Force | Out-Null
    $installer = $null
    if (-not $SkipInstaller) {
        $isccCandidates = @(
            "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe"
        )
        $iscc = $isccCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
        if (-not $iscc) {
            throw "Inno Setup ISCC.exe was not found. Install Inno Setup 7 and rerun the build."
        }
        Invoke-Checked $iscc @(
            "/DAppVersion=$Version",
            "/DSourceDir=$applicationDirectory",
            "/DOutputDir=$dist",
            (Join-Path $root "packaging\isaca-desktop.iss")
        )
        $installer = Get-ChildItem -LiteralPath $dist -Filter "ISACA-Desktop-$Version-win64-setup.exe" |
            Select-Object -First 1
        if (-not $installer) {
            throw "Inno Setup completed without producing the expected installer."
        }
    }

    $standaloneSize = (Get-ChildItem -LiteralPath $applicationDirectory -Recurse -File |
        Measure-Object Length -Sum).Sum
    $manifest = [ordered]@{
        version = $Version
        built_at_utc = [DateTime]::UtcNow.ToString("o")
        source_commit = (git rev-parse HEAD).Trim()
        python = (& $buildPython --version).Trim()
        standalone_directory = $applicationDirectory
        standalone_bytes = $standaloneSize
        executable_sha256 = (Get-FileHash -LiteralPath $executable.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        installer = if ($installer) { $installer.FullName } else { $null }
        installer_bytes = if ($installer) { $installer.Length } else { $null }
        installer_sha256 = if ($installer) { (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null }
        tests = if ($SkipTests) { "skipped" } else { "passed" }
        packaged_worker_smoke = "passed"
        vision_models_included = $false
    }
    $manifestPath = Join-Path $dist "release-manifest-$Version.json"
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding utf8
    Write-Host "Release build complete."
    Write-Host "Standalone: $applicationDirectory"
    if ($installer) { Write-Host "Installer:  $($installer.FullName)" }
    Write-Host "Manifest:   $manifestPath"
} finally {
    Set-Location $previousLocation
}
