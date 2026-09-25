# build_llama_engine.ps1 — build Tala's embedded llama.cpp engine binaries.
#
# Release engineering, not app code. This does NOT run as part of the app,
# first_run.py, or CI for the main app -- it produces the artifacts that
# get uploaded to talacore.app (alongside version.json) as
# llama_binaries.json points to, which python/inference/llama_server_manager.py
# downloads on demand. See that module's docstring for why the binary is
# distributed this way rather than bundled into the app's own update zip.
#
# Builds three variants from a pinned commit of a custom llama.cpp fork
# (github.com/thecodacus/llama.cpp @ parallel-decision) that adds a
# /v1/decision endpoint for single-pass structured decisions ("Jev mode"):
#
#   cpu    - always works, the ultimate fallback
#   cuda   - NVIDIA, multi-generation (not pinned to one compute capability
#            or driver version)
#   vulkan - AMD/Intel/NVIDIA via Vulkan; needs the Vulkan SDK (not just the
#            driver-provided runtime) to build -- winget install
#            KhronosGroup.VulkanSDK if `cmake -DGGML_VULKAN=ON` fails to find it
#
# Usage:
#   pwsh scripts/build_llama_engine.ps1                    # build all three
#   pwsh scripts/build_llama_engine.ps1 -Variants cpu,cuda  # a subset
#
# Requires: git, cmake, a C++ toolchain (MSVC on Windows), and for -cuda the
# CUDA toolkit (nvcc on PATH).

param(
    [string[]]$Variants = @("cpu", "cuda", "vulkan"),
    [string]$Repo = "https://github.com/thecodacus/llama.cpp.git",
    [string]$Branch = "parallel-decision",
    # Bump together with python/inference/llama_server_manager.py's
    # BINARY_VERSION and llama_binaries.json when the fork moves.
    [string]$Commit = "ad129b08d9f134cd298d1f8a85efc52b1b66e18e",
    [string]$WorkDir = "$(Split-Path -Parent $PSScriptRoot)\..\llama-build",
    [string]$OutDir = "$(Split-Path -Parent $PSScriptRoot)\..\llama-build\dist"
)

$ErrorActionPreference = "Stop"

$SrcDir = Join-Path $WorkDir "src"

if (-not (Test-Path $SrcDir)) {
    Write-Host "Cloning $Repo @ $Branch ..."
    git clone --branch $Branch --single-branch $Repo $SrcDir
}

Push-Location $SrcDir
try {
    git fetch origin $Branch
    git checkout $Commit
    $actualCommit = git rev-parse HEAD
    if ($actualCommit -ne $Commit) {
        throw "Checked out $actualCommit, expected pinned commit $Commit"
    }
    Write-Host "Pinned to commit $actualCommit"

    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

    function Build-Variant($name, $extraArgs) {
        $buildDir = "build-$name"
        Write-Host "`n=== Configuring $name ==="
        $cmakeArgs = @(
            "-B", $buildDir,
            "-DGGML_NATIVE=OFF",
            "-DLLAMA_CURL=OFF",
            "-DCMAKE_BUILD_TYPE=Release"
        ) + $extraArgs
        cmake @cmakeArgs

        Write-Host "=== Building $name (this is the slow part) ==="
        cmake --build $buildDir --config Release --target llama-server -j

        $binDir = Join-Path $buildDir "bin\Release"
        $exe = Join-Path $binDir "llama-server.exe"
        if (-not (Test-Path $exe)) {
            throw "$name build did not produce llama-server.exe"
        }

        $zipPath = Join-Path $OutDir "llama-server-windows-$name.zip"
        Write-Host "=== Packaging $name -> $zipPath ==="
        if (Test-Path $zipPath) { Remove-Item $zipPath }
        Compress-Archive -Path (Join-Path $binDir "*.dll"), $exe -DestinationPath $zipPath

        $hash = (Get-FileHash -Algorithm SHA256 $zipPath).Hash.ToLower()
        Write-Host "$name sha256: $hash"
        return @{ variant = $name; file = (Split-Path -Leaf $zipPath); sha256 = $hash }
    }

    $results = @()
    if ($Variants -contains "cpu") {
        $results += Build-Variant "cpu" @()
    }
    if ($Variants -contains "cuda") {
        $results += Build-Variant "cuda" @(
            "-DGGML_CUDA=ON",
            "-DCMAKE_CUDA_ARCHITECTURES=61;70;75;80;86;89;90"
        )
    }
    if ($Variants -contains "vulkan") {
        $results += Build-Variant "vulkan" @("-DGGML_VULKAN=ON")
    }

    Write-Host "`n=== Done. Manifest entries (paste into llama_binaries.json's windows key, ==="
    Write-Host "=== after uploading each zip to talacore.app and filling in its url) ==="
    foreach ($r in $results) {
        Write-Host ('  "{0}": {{ "url": "https://talacore.app/engines/{1}", "sha256": "{2}" }}' -f $r.variant, $r.file, $r.sha256)
    }
    Write-Host "`nbinary_version for llama_binaries.json and llama_server_manager.BINARY_VERSION: $actualCommit"
} finally {
    Pop-Location
}
