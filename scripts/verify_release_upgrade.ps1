# Verify a published release by performing a real OTA upgrade in a scratch
# install: extract the PREVIOUS release, plant live user state, then do
# exactly what the shipped updater does -- fetch the manifest from
# talacore.app, download the zip, verify sha256, Expand-Archive over the
# install root -- and assert the version arrived and no user state was touched.
#
# Run AFTER scripts/upload_vps.ps1, with the previous release's windows zip:
#   scripts/verify_release_upgrade.ps1 -BaselineZip path\to\tala-windows-<old>.zip
#
# First ran 2026-09-23 (0.1.83 -> 0.1.84); before that, no complete real
# upgrade had ever been observed -- tests covered the mechanism only.
param(
    [Parameter(Mandatory = $true)][string]$BaselineZip,
    # Default: what this checkout is about to advertise.
    [string]$ExpectedVersion
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $ExpectedVersion) {
    $ExpectedVersion = ((Get-Content (Join-Path $repo 'blockchain\public\version.json') -Raw) | ConvertFrom-Json).version
}

$root = Join-Path $env:TEMP "tala-upgrade-verify-$([guid]::NewGuid().ToString('n').Substring(0,8))"
New-Item -ItemType Directory -Path $root | Out-Null
try {
    # --- The "existing install".
    Expand-Archive -Path $BaselineZip -DestinationPath $root
    $baseVersion = (Get-Content (Join-Path $root 'package.json') | ConvertFrom-Json).version
    if ($baseVersion -eq $ExpectedVersion) { throw "baseline is already $ExpectedVersion; pass the PREVIOUS release's zip" }

    # --- Live user state that MUST survive. Settings nodes shipped in bundles
    # --- up to 0.1.83, so an old install may or may not have one; cover both.
    $settingsNode = Join-Path $root 'nodes\settings-inference.html'
    if (-not (Test-Path $settingsNode)) { Set-Content -Path $settingsNode -Value '<html></html>' }
    Add-Content -Path $settingsNode -Value '<!-- SENTINEL: user changed this setting -->'
    Set-Content -Path (Join-Path $root 'emotion_state.json') -Value '{"sentinel":"user affect state"}'
    Set-Content -Path (Join-Path $root 'nodes\persona-tala.html') -Value '<html><!-- SENTINEL: private persona --></html>'

    # --- The update, exactly as the updater script performs it.
    $manifest = (Invoke-WebRequest -Uri 'https://talacore.app/version.json' -UseBasicParsing).Content | ConvertFrom-Json
    if ($manifest.version -ne $ExpectedVersion) { throw "server advertises $($manifest.version), expected $ExpectedVersion -- publish first" }
    $zipPath = Join-Path $root 'tala-update.zip'
    Invoke-WebRequest -Uri $manifest.downloads.windows -OutFile $zipPath -UseBasicParsing
    $actualHash = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash
    if ($actualHash.ToLower() -ne $manifest.sha256.windows.ToLower()) { throw 'hash mismatch: downloaded zip does not match published sha256' }
    Expand-Archive -Path $zipPath -DestinationPath $root -Force
    Remove-Item $zipPath -Force

    # --- Assertions.
    $fail = @()
    $v2 = (Get-Content (Join-Path $root 'package.json') | ConvertFrom-Json).version
    if ($v2 -ne $ExpectedVersion) { $fail += "version after update is $v2, expected $ExpectedVersion" }
    if ((Get-Content $settingsNode -Raw) -notmatch 'SENTINEL: user changed this setting') { $fail += 'user settings node was overwritten' }
    if ((Get-Content (Join-Path $root 'emotion_state.json') -Raw) -notmatch 'user affect state') { $fail += 'emotion_state.json was overwritten' }
    if ((Get-Content (Join-Path $root 'nodes\persona-tala.html') -Raw) -notmatch 'SENTINEL: private persona') { $fail += 'persona node was touched' }

    # The updated install must refuse a same-version "update" (downgrade gate).
    $rootFwd = $root.Replace('\', '/')
    $gate = node -e "const a=require('$rootFwd/electron/harness_admin.js'); console.log(a.isNewerVersion('$ExpectedVersion','$ExpectedVersion') ? 'WOULD-APPLY' : 'REFUSES')"
    if ($gate -ne 'REFUSES') { $fail += "downgrade gate on the updated install answered '$gate' for same-version" }

    if ($fail.Count) { $fail | ForEach-Object { Write-Host "FAIL: $_" -ForegroundColor Red }; exit 1 }
    Write-Host "UPGRADE VERIFIED: $baseVersion -> $ExpectedVersion from talacore.app, hash-verified, user state intact, downgrade gate live." -ForegroundColor Green
}
finally {
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
}
