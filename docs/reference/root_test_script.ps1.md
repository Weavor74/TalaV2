# File: `test_script.ps1`

## Overview

This is a test fixture or mock file used in the testing suite.

## File Contents

```powershell
Start-Process -FilePath "powershell.exe" -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Normal -NoExit -File "${startScript}"" -WorkingDirectory "${appRoot}"

```
