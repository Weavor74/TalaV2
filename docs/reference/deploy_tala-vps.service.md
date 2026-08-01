# File: `tala-vps.service`

## Overview

A systemd service unit file used to persist the Tala node server on Linux VPS instances.

## File Contents

```service
[Unit]
Description=Tala permanent VPS peer
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=tala
Group=tala
WorkingDirectory=/opt/tala
EnvironmentFile=/opt/tala/.env
ExecStart=/opt/tala/scripts/start-tala-vps.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target

```
