<#
.SYNOPSIS
  Start Tala's US-origin local model stack on Kagura (RTX 3050 6 GB, Windows).

.DESCRIPTION
  Sets the D:-only environment, checks the pieces, pulls the Ollama models and
  fetches the gpt-oss GGUF if they are missing, then starts Tala. Tala itself
  starts llama-server (main model) and talks to Ollama (fallback on the GPU, vision on the
  CPU so an image turn never stops the main model) -- see docs/inference-profiles.md.
  scripts/bench_kagura.py re-measures the main model's settings on this card.

  .\scripts\kagura-stack.ps1 -Check      # report what is present, change nothing
  .\scripts\kagura-stack.ps1             # pull/fetch anything missing, then start Tala
  .\scripts\kagura-stack.ps1 -NoStart    # prepare only
  .\scripts\kagura-stack.ps1 -FetchPersona   # also fetch the 3B the persona LoRA is trained on and served with (~2 GB GGUF)

  The weekly persona LoRA (docs/lora-pipeline.md) needs three more things, all on D:: a CUDA build of torch plus bitsandbytes in
  the venv, the base weights in the Hugging Face cache, and a llama.cpp checkout for the GGUF converter. This script only reports them.
#>
param(
    [switch]$Check,
    [switch]$NoStart,
    [switch]$FetchPersona
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
    "hf.co/mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated-GGUF:Q4_K_M",
    # Topic detection for messages with no keyword (CPU only, optional): docs/astro-topic-stance.md
    "nomic-embed-text:latest"
)
if (-not $env:UV_CACHE_DIR -or $env:UV_CACHE_DIR -match '^[Cc]:') { $env:UV_CACHE_DIR = "D:\caches\uv" }
$VenvPython = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"

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

# Local voice (docs/local-voice.md): Kokoro, faster-whisper and the wake-word models live under $TALA_MODELS_DIR\voice, put
# there once by scripts\setup_voice.py. At run time nothing is fetched and nothing leaves the machine.
$VoiceDir  = Join-Path $env:TALA_MODELS_DIR "voice"
$haveVoice = (Test-Path (Join-Path $VoiceDir "kokoro\kokoro-v1_0.pth")) -and (Test-Path (Join-Path $VoiceDir "whisper\base.en\model.bin"))
Write-Host ("voice models   : {0}  ({1})" -f $VoiceDir, $(if ($haveVoice) { "found" } else { "missing: run  .venv\Scripts\python scripts\setup_voice.py" }))

# Persona adapter (docs/lora-pipeline.md): the Llama 3.2 3B the weekly LoRA rides on. Optional; nothing is fetched at run time.
$PersonaGguf = Join-Path $env:TALA_MODELS_DIR "gguf\Llama-3.2-3B-Instruct-Q4_K_M.gguf"
$havePersona = Test-Path $PersonaGguf
Write-Host ("persona GGUF   : {0}  ({1})" -f $PersonaGguf, $(if ($havePersona) { "found" } else { "missing: pass -FetchPersona, or see docs/lora-pipeline.md" }))
if (Test-Path $VenvPython) {
    $torch = & $VenvPython -c "import torch; print(torch.__version__, torch.cuda.is_available())" 2>$null
    $bnb   = & $VenvPython -c "import bitsandbytes" 2>$null; $haveBnb = ($LASTEXITCODE -eq 0)
    Write-Host ("persona train  : torch {0}; bitsandbytes {1}" -f $(if ($torch) { $torch } else { "missing" }), $(if ($haveBnb) { "found" } else { "missing" }))
}

function Test-Swisseph {
    if (-not (Test-Path $VenvPython)) { return $false }
    & $VenvPython -c "import swisseph" 2>$null
    return ($LASTEXITCODE -eq 0)
}
$haveSwe = Test-Swisseph
Write-Host ("pyswisseph     : {0}" -f $(if ($haveSwe) { "present" } else { "MISSING (mood + topic stance need it)" }))

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

if (-not $haveSwe) {
    Write-Host "Installing pyswisseph (UV_CACHE_DIR=$env:UV_CACHE_DIR) ..."
    & uv pip install --python $VenvPython "pyswisseph>=2.10.3.2"
    if ($LASTEXITCODE -ne 0) { throw "installing pyswisseph failed" }
}

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

if ($FetchPersona -and -not $havePersona) {
    Write-Host "Downloading the Llama 3.2 3B Instruct Q4_K_M GGUF (~2 GB) to $PersonaGguf ..."
    New-Item -ItemType Directory -Force -Path (Split-Path $PersonaGguf) | Out-Null
    & python -m pip install --quiet "huggingface_hub[cli]"
    & python -m huggingface_hub.commands.huggingface_cli download bartowski/Llama-3.2-3B-Instruct-GGUF `
        Llama-3.2-3B-Instruct-Q4_K_M.gguf --local-dir (Split-Path $PersonaGguf)
    if (-not (Test-Path $PersonaGguf)) { throw "GGUF download did not produce $PersonaGguf" }
}

if ($NoStart) { Write-Host "Stack prepared."; return }

Write-Host "Starting Tala (llama-server starts on first use)..."
& "$PSScriptRoot\start-tala.ps1"
exit $LASTEXITCODE
