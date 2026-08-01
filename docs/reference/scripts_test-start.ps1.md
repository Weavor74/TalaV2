# File: `test-start.ps1`

## Overview

This is a test fixture or mock file used in the testing suite.

## File Contents

```powershell
$appRoot = "D:\src\TalaV2"
Start-Process -FilePath "powershell.exe" -ArgumentList "-ExecutionPolicy Bypass -NoExit -File D:\src\TalaV2\scripts\start-tala.ps1" -WorkingDirectory $appRoot

```
