# Publish the llama.cpp engine binaries built by scripts/build_llama_engine.ps1
# to talacore.app, alongside version.json.
#
# Same stage-then-verify discipline as upload_vps.ps1: everything lands in the
# VM home dir first, only a successful scp of every file lets anything move
# into the webroot, and each zip is re-downloaded from the public URL and
# hash-checked against the local build before this script calls it done. A
# binary that fails that check is exactly the kind of silent corruption
# llama_server_manager.py's own sha256 check exists to catch client-side --
# catching it here first means a bad upload never reaches a user.
#
# Same two-webroot split as upload_vps.ps1 (see that script for why): the
# public one, /var/www/talacore, is what Caddy actually serves and is the only
# one that matters for llama_binaries.json's URLs to resolve. Also mirrored to
# /opt/tala/blockchain/public for consistency with every other published
# artifact, though nothing there currently reads it.
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$distDir = Join-Path (Split-Path -Parent $repo) 'llama-build\dist'
$manifest = Join-Path $repo 'blockchain\public\llama_binaries.json'
$zone = 'us-central1-a'
$vm = 'tala-vps'

$zips = 'llama-server-windows-cpu.zip', 'llama-server-windows-vulkan.zip', 'llama-server-windows-cuda.zip'

foreach ($f in $zips) {
    if (-not (Test-Path (Join-Path $distDir $f))) { throw "$f not found in $distDir -- run build_llama_engine.ps1 first." }
}

# --- Stage everything to the VM's home dir. Any failure aborts before
# --- anything is moved into the webroot.
foreach ($f in $zips) {
    gcloud compute scp (Join-Path $distDir $f) "${vm}:$f" --zone=$zone
    if ($LASTEXITCODE -ne 0) { throw "scp of $f failed; nothing has gone live." }
}
gcloud compute scp $manifest "${vm}:llama_binaries.json" --zone=$zone
if ($LASTEXITCODE -ne 0) { throw "scp of llama_binaries.json failed; nothing has gone live." }

# --- Go live: create the engines/ subdirectory in the public webroot if it
# --- doesn't exist yet, move the zips there, mirror everything to the node
# --- app's directory too, and publish the manifest last (mirrors
# --- upload_vps.ps1's version.json-last ordering -- the manifest is the
# --- thing that makes these binaries discoverable at all).
$zipList = ($zips -join ' ')
gcloud compute ssh $vm --zone=$zone --command="sudo mkdir -p /var/www/talacore/engines /opt/tala/blockchain/public/engines && sudo cp $zipList /var/www/talacore/engines/ && sudo mv $zipList /opt/tala/blockchain/public/engines/"
if ($LASTEXITCODE -ne 0) { throw 'remote install of engine zips failed; llama_binaries.json was NOT updated.' }

# --- Verify each zip from the public URL, byte-for-byte, before publishing
# --- the manifest that points at it.
foreach ($f in $zips) {
    $tmp = Join-Path $env:TEMP "verify-$f"
    Invoke-WebRequest -Uri "https://talacore.app/engines/$f" -OutFile $tmp -UseBasicParsing
    $remote = (Get-FileHash $tmp -Algorithm SHA256).Hash
    $local = (Get-FileHash (Join-Path $distDir $f) -Algorithm SHA256).Hash
    Remove-Item $tmp -Force
    if ($remote -ne $local) { throw "$f served from talacore.app does not match the local build; llama_binaries.json was NOT updated." }
    Write-Host "verified $f ($remote)" -ForegroundColor Green
}

# --- Only now publish the manifest, in both webroots.
gcloud compute ssh $vm --zone=$zone --command="sudo cp ~/llama_binaries.json /var/www/talacore/llama_binaries.json && sudo mv ~/llama_binaries.json /opt/tala/blockchain/public/llama_binaries.json"
if ($LASTEXITCODE -ne 0) { throw 'llama_binaries.json publish failed -- binaries are live but not yet discoverable. Re-run this step.' }

$live = (Invoke-WebRequest -Uri 'https://talacore.app/llama_binaries.json' -UseBasicParsing).Content | ConvertFrom-Json
Write-Host "talacore.app now serves llama.cpp engine binary_version $($live.binary_version)" -ForegroundColor Cyan
