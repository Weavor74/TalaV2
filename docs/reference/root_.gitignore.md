# File: `.gitignore`

## Overview

This file contains generic configuration or data.

## File Contents

```
# Dependencies
node_modules/
.venv/

# Python caches and test state
__pycache__/
*.py[cod]
.pytest_cache/

# Local environment
.env

# Tala generated/runtime state
.tala_snapshots/
data/*.db
data/*.sqlite
data/*.sqlite3
blockchain/data/*.json
nodes/*.html
nodes/*.html.bak
nodes/_archive_legacy/
nodes/.wizard_state.json
nodes/.workflow_state.json

# Keep placeholder files
!data/.gitkeep
!blockchain/data/.gitkeep
!nodes/.gitkeep

# Logs and temporary files
*.log
*.tmp
*.bak

# Build and scratch directories
dist/
scratch/

```
