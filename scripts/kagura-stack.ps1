<#
.SYNOPSIS
  Start Tala's US-origin local model stack on Kagura (RTX 3050 6 GB, Windows).

.DESCRIPTION
  Sets the D:-only environment, checks the pieces, pulls the Ollama models and
  fetches the gpt-oss GGUF if they are missing, then starts Tala. Tala itself
  starts llama-server (main model) and talks to Ollama (fallback + vision) --
  see docs/inference-profiles.md.

  .\scripts\kagura-stack.ps1 -Check      # report what is present, change nothing
  .\scripts\kagura-stack.ps1             # pull/fetch anything missing, then start Tala
  .\scripts\kagura-stack.ps1 -NoStart    # prepare only
#>
param(
    [switch]$Check,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

# Everything on D:. Nothing below writes to C:.
$env:TALA_PROFILE     = "kagura"
$env:TALA_TOOLS_DIR   = if ($env:TALA_TOOLS_DIR)  { $env:TALA_TOOLS_DIR }  else { "D:\tools" }
$env:TALA_MODELS_DIR  = if ($env:TALA_MODELS_DIR) { $env:TALA_MODELS_DIR } else { "D:\models" }
$env:OLLAMA_MODELS    = if ($env:OLLAMA_MODELS)   { $env:OLLAMA_MODELS }   else { "D:\ollama\models" }
# Keep an HF cache the user already has on a non-C: drive; otherwise use the profile's.
if (-not $env:HF_HOME -or $env:HF_HOME -match '^[Cc]:') { $env:HF_HOME = "$($env:TALA_MODELS_DIR)\hf-cache" }

$LlamaServer = Join-Path $env:TALA_TOOLS_DIR "llama-cuda-copy\llama-server.exe"
$Gguf        = Join-Path $env:TALA_MODELS_DIR "gguf\gpt-oss-20b-heretic.MXFP4_MOE.gguf"
$OllamaModels = @(
    "gemma3:4b",
    "hf.co/mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated-GGUF:Q4_K_M"
)

function Test-Ollama {
    try { Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 2 | Out-Null; $true } catch { $false }
}

Write-Host "Profile        : kagura"
Write-Host "Tools dir      : $env:TALA_TOOLS_DIR"
Write-Host "Models dir     : $env:TALA_MODELS_DIR"
Write-Host "OLLAMA_MODELS  : $env:OLLAMA_MODELS"
Write-Host "HF_HOME        : $env:HF_HOME"

foreach ($p in @($env:TALA_TOOLS_DIR, $env:TALA_MODELS_DIR, $env:OLLAMA_MODELS, $env:HF_HOME)) {
    if ($p -match '^[Cc]:') { throw "Refusing to use a C: path: $p" }
}

$haveServer = Test-Path $LlamaServer
$haveGguf   = Test-Path $Gguf
Write-Host ("llama-server   : {0}  ({1})" -f $LlamaServer, $(if ($haveServer) { "found" } else { "MISSING" }))
Write-Host ("gpt-oss GGUF   : {0}  ({1})" -f $Gguf, $(if ($haveGguf) { "found" } else { "missing" }))

$ollamaUp = Test-Ollama
Write-Host ("Ollama         : {0}" -f $(if ($ollamaUp) { "running" } else { "not running" }))
$installed = @()
if ($ollamaUp) {
    $installed = (Invoke-RestMethod "http://localhost:11434/api/tags").models | ForEach-Object { $_.name }
    foreach ($m in $OllamaModels) {
        $has = $installed -contains $m
        Write-Host ("  {0,-70} {1}" -f $m, $(if ($has) { "present" } else { "missing" }))
    }
}

if ($Check) { return }

if (-not $haveServer) {
    throw "llama-server not found at $LlamaServer. Put the CUDA llama.cpp build there (or set TALA_TOOLS_DIR)."
}

if (-not $ollamaUp) {
    Write-Host "Starting Ollama (OLLAMA_MODELS=$env:OLLAMA_MODELS)..."
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    for ($i = 0; $i -lt 20 -and -not (Test-Ollama); $i++) { Start-Sleep -Seconds 1 }
    if (-not (Test-Ollama)) { throw "Ollama did not start." }
    $installed = (Invoke-RestMethod "http://localhost:11434/api/tags").models | ForEach-Object { $_.name }
}

foreach ($m in $OllamaModels) {
    if ($installed -notcontains $m) {
        Write-Host "Pulling $m ..."
        & ollama pull $m
        if ($LASTEXITCODE -ne 0) { throw "ollama pull $m failed" }
    }
}

if (-not $haveGguf) {
    Write-Host "Downloading the gpt-oss-20b Heretic GGUF (12.1 GB) to $Gguf ..."
    New-Item -ItemType Directory -Force -Path (Split-Path $Gguf) | Out-Null
    & python -m pip install --quiet "huggingface_hub[cli]"
    & python -m huggingface_hub.commands.huggingface_cli download mradermacher/gpt-oss-20b-heretic-GGUF `
        gpt-oss-20b-heretic.MXFP4_MOE.gguf --local-dir (Split-Path $Gguf)
    if (-not (Test-Path $Gguf)) { throw "GGUF download did not produce $Gguf" }
}

if ($NoStart) { Write-Host "Stack prepared."; return }

Write-Host "Starting Tala (llama-server starts on first use)..."
& "$PSScriptRoot\start-tala.ps1"
exit $LASTEXITCODE
