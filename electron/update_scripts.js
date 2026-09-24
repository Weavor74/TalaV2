/**
 * The updater scripts app.update writes and detaches (extracted from main.js
 * under the size ratchet). Both do the same dance: download, verify sha256,
 * extract to STAGING (a bad archive leaves the install untouched), back up
 * every file the update will overwrite, apply, and restore the backup if the
 * apply fails. They run after the app quits, so they must carry their own
 * logging: %TEMP%\tala-update.log on Windows, /tmp/tala-update.log on Unix.
 *
 * The URL travels as TALA_UPDATE_URL / a quoted argument, never interpolated
 * into script code — tests/electron/updater_contract.test.js pins that URL
 * metacharacters arrive as data.
 */
'use strict';

function buildWindowsUpdateScript({ zipPath, appRoot, startScript, stagingPath, backupPath, expectedHash }) {
  const hashCheckBlock = expectedHash ? `
$actualHash = (Get-FileHash -Path "${zipPath}" -Algorithm SHA256).Hash
if ($actualHash.ToLower() -ne "${expectedHash.toLowerCase()}") {
    Write-Host "Downloaded update failed integrity check (hash mismatch). Aborting." -ForegroundColor Red
    Start-Sleep -Seconds 5
    exit
}
Write-Host "Integrity check passed." -ForegroundColor Green
` : '';
  return `
$updateUrl = $env:TALA_UPDATE_URL
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# Unix logs to /tmp/tala-update.log; without this, a failed Windows update
# closes the app and leaves no trace of why.
try { Start-Transcript -Path "$env:TEMP\\tala-update.log" -Force | Out-Null } catch {}
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Tala Cognitive Engine Room Updater" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Waiting for Tala to close..."
Start-Sleep -Seconds 3

Write-Host "Downloading update from $updateUrl..." -ForegroundColor Yellow
try {
    Invoke-WebRequest -Uri $updateUrl -OutFile "${zipPath}" -UseBasicParsing -ErrorAction Stop
} catch {
    Write-Host "Invoke-WebRequest failed, retrying with WebClient..." -ForegroundColor Yellow
    (New-Object System.Net.WebClient).DownloadFile($updateUrl, "${zipPath}")
}

if (-not (Test-Path "${zipPath}") -or (Get-Item "${zipPath}").Length -lt 1000) {
    Write-Host "Download failed or file corrupted." -ForegroundColor Red
    Start-Sleep -Seconds 5
    exit
}
${hashCheckBlock}
$staging = "${stagingPath}"
$backup = "${backupPath}"
if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
if (Test-Path $backup) { Remove-Item -Recurse -Force $backup }
Write-Host "Extracting update to staging..." -ForegroundColor Yellow
try {
    Expand-Archive -Path "${zipPath}" -DestinationPath $staging -Force
} catch {
    Write-Host "Extraction failed; the install was not touched: $_" -ForegroundColor Red
    Start-Sleep -Seconds 5
    exit 1
}
Write-Host "Backing up the files this update overwrites..."
Get-ChildItem -Path $staging -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($staging.Length + 1)
    $existing = Join-Path "${appRoot}" $rel
    if (Test-Path $existing) {
        $dest = Join-Path $backup $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Copy-Item $existing $dest -Force
    }
}
Write-Host "Applying update to ${appRoot}..." -ForegroundColor Yellow
robocopy $staging "${appRoot}" /E /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Host "Apply failed (robocopy exit $LASTEXITCODE); restoring backup..." -ForegroundColor Red
    robocopy $backup "${appRoot}" /E /NFL /NDL /NJH /NJS | Out-Null
    Write-Host "Backup restored from $backup" -ForegroundColor Yellow
    Start-Sleep -Seconds 5
    exit 1
}
Remove-Item -Recurse -Force $staging
Write-Host "Update successful! (backup kept at $backup)" -ForegroundColor Green

Write-Host "Restarting Tala Engine..." -ForegroundColor Cyan
Start-Sleep -Seconds 2
Start-Process -FilePath "powershell.exe" -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Normal -NoExit -File \`"${startScript}\`"" -WorkingDirectory "${appRoot}"
exit
`;
}

function buildUnixUpdateScript({ zipPath, appRoot, startScript, stagingPath, backupPath, logPath, updaterPython, shellCmd }) {
  return `#!/bin/sh
exec > "${logPath}" 2>&1
sleep 3
STAGING="${stagingPath}"
BACKUP="${backupPath}"
FILELIST="${stagingPath}.files"
rm -rf "$STAGING" "$BACKUP" "$FILELIST"
mkdir -p "$STAGING" "$BACKUP"
# Extracting update to staging first: a bad archive leaves the install untouched.
if command -v unzip >/dev/null 2>&1; then
  unzip -o "${zipPath}" -d "$STAGING" || { echo "Extraction failed; the install was not touched."; exit 1; }
elif command -v ${updaterPython || 'python3'} >/dev/null 2>&1; then
  ${updaterPython || 'python3'} -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "${zipPath}" "$STAGING" || { echo "Extraction failed; the install was not touched."; exit 1; }
else
  echo "No extractor available (unzip or python3); update NOT applied."
  exit 1
fi
# Back up every file this update overwrites, then apply; restore on failure.
(cd "$STAGING" && find . -type f | sed 's|^\\./||') > "$FILELIST"
while IFS= read -r f; do
  if [ -f "${appRoot}/$f" ]; then
    mkdir -p "$BACKUP/$(dirname "$f")"
    cp -p "${appRoot}/$f" "$BACKUP/$f"
  fi
done < "$FILELIST"
if ! cp -Rp "$STAGING/." "${appRoot}/"; then
  echo "Apply failed; restoring backup from $BACKUP..."
  cp -Rp "$BACKUP/." "${appRoot}/"
  exit 1
fi
rm -rf "$STAGING" "$FILELIST"
rm -f "${zipPath}"
echo "Update applied; backup kept at $BACKUP"
chmod +x "${startScript}"
cd "${appRoot}"
${shellCmd} "${startScript}" &
`;
}

module.exports = { buildWindowsUpdateScript, buildUnixUpdateScript };
