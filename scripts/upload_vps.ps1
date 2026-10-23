# Publish the release built by scripts/build_bundles.py to talacore.app.
#
# Order is the whole point: bundles and site first, verified from the public
# URL, and version.json LAST — so a new version is only ever advertised once
# its downloads actually resolve. (The old script moved everything in one
# unguarded sweep; a partial upload or a dead app.js still went live, which is
# how the site spent weeks advertising 0.1.83 with all three zips 404ing.)
#
# TWO webroots, on purpose. The live Caddyfile on the VM is NOT the repo's:
# it proxies only /chain/* to the node app and serves everything else
# statically from /var/www/talacore. So the public site — version.json, the
# zips, download.html — is /var/www/talacore, and /opt/tala/blockchain/public
# is what the node app itself reads. Publishing to only one of them is how
# the release path silently broke: uploads landed in /opt/tala where no
# public request ever looked.
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$pub = Join-Path $repo 'blockchain\public'
$zone = 'us-central1-a'
$vm = 'tala-vps'

$bundles = 'tala-windows.zip', 'tala-macos.zip', 'tala-linux.zip'

# --- Stage everything to the VM's home dir. Any failure aborts before
# --- anything is moved into the webroot.
foreach ($f in ($bundles + @('download.html', 'version.json'))) {
    gcloud compute scp (Join-Path $pub $f) "${vm}:$f" --zone=$zone
    if ($LASTEXITCODE -ne 0) { throw "scp of $f failed; nothing has gone live." }
}
# The chain node is app.js PLUS its sibling modules (blockchain/src/*.js). Shipping
# app.js alone leaves a node that dies on its first require(), so every .js file in
# the directory goes, and each is syntax-checked here first (a dead node must not go live).
$nodeFiles = Get-ChildItem (Join-Path $repo 'blockchain\src') -Filter *.js
if (-not ($nodeFiles | Where-Object Name -eq 'app.js')) { throw 'blockchain/src/app.js is missing; nothing has gone live.' }
if (Get-Command node -ErrorAction SilentlyContinue) {
    foreach ($f in $nodeFiles) {
        node --check $f.FullName
        if ($LASTEXITCODE -ne 0) { throw "$($f.Name) does not parse; nothing has gone live." }
    }
}
foreach ($f in $nodeFiles) {
    gcloud compute scp $f.FullName "${vm}:$($f.Name)" --zone=$zone
    if ($LASTEXITCODE -ne 0) { throw "scp of $($f.Name) failed; nothing has gone live." }
}
$nodeHome = ($nodeFiles | ForEach-Object { "~/$($_.Name)" }) -join ' '

# --- Go live: bundles, site page, server code. NOT version.json yet.
gcloud compute ssh $vm --zone=$zone --command="sudo cp ~/tala-windows.zip ~/tala-macos.zip ~/tala-linux.zip ~/download.html /var/www/talacore/ && sudo mv ~/tala-windows.zip ~/tala-macos.zip ~/tala-linux.zip ~/download.html /opt/tala/blockchain/public/ && sudo mv $nodeHome /opt/tala/blockchain/src/ && sudo systemctl restart tala-vps.service"
if ($LASTEXITCODE -ne 0) { throw 'remote install of bundles/node files failed; version.json was NOT updated.' }

# --- Verify each bundle from the public URL, byte-for-byte, before
# --- advertising it. A mismatch here means a client would download something
# --- other than what bump_version.py hashed into version.json.
foreach ($f in $bundles) {
    $tmp = Join-Path $env:TEMP "verify-$f"
    Invoke-WebRequest -Uri "https://talacore.app/$f" -OutFile $tmp -UseBasicParsing
    $remote = (Get-FileHash $tmp -Algorithm SHA256).Hash
    $local = (Get-FileHash (Join-Path $pub $f) -Algorithm SHA256).Hash
    Remove-Item $tmp -Force
    if ($remote -ne $local) { throw "$f served from talacore.app does not match the local build; version.json was NOT updated." }
    Write-Host "verified $f ($remote)" -ForegroundColor Green
}

# --- Only now advertise the release, in both webroots.
gcloud compute ssh $vm --zone=$zone --command="sudo cp ~/version.json /var/www/talacore/version.json && sudo mv ~/version.json /opt/tala/blockchain/public/"
if ($LASTEXITCODE -ne 0) { throw 'version.json publish failed — bundles are live but the release is not yet advertised. Re-run this step.' }

$live = (Invoke-WebRequest -Uri 'https://talacore.app/version.json' -UseBasicParsing).Content | ConvertFrom-Json
Write-Host "talacore.app now advertises $($live.version)" -ForegroundColor Cyan
