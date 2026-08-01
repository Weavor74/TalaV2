# File: `upload_vps.ps1`

## Overview

Deployment script that uses gcloud to push zip bundles and configuration updates to a live web server.

## File Contents

```powershell
gcloud compute scp D:\src\TalaV2\blockchain\public\tala-windows.zip tala-vps:tala-windows.zip --zone=us-central1-a
gcloud compute scp D:\src\TalaV2\blockchain\public\tala-macos.zip tala-vps:tala-macos.zip --zone=us-central1-a
gcloud compute scp D:\src\TalaV2\blockchain\public\tala-linux.zip tala-vps:tala-linux.zip --zone=us-central1-a
gcloud compute scp D:\src\TalaV2\blockchain\public\download.html tala-vps:download.html --zone=us-central1-a
gcloud compute scp D:\src\TalaV2\blockchain\public\version.json tala-vps:version.json --zone=us-central1-a
gcloud compute scp D:\src\TalaV2\blockchain\src\app.js tala-vps:app.js --zone=us-central1-a

gcloud compute ssh tala-vps --zone=us-central1-a --command="sudo mv ~/tala-windows.zip /opt/tala/blockchain/public/; sudo mv ~/tala-macos.zip /opt/tala/blockchain/public/; sudo mv ~/tala-linux.zip /opt/tala/blockchain/public/; sudo mv ~/download.html /opt/tala/blockchain/public/; sudo mv ~/version.json /opt/tala/blockchain/public/; sudo mv ~/app.js /opt/tala/blockchain/src/app.js; sudo pm2 restart all || sudo systemctl restart tala-vps.service || echo 'Restart your node'"

```
