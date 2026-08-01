# File: `test_err.ps1`

## Overview

This is a test fixture or mock file used in the testing suite.

## File Contents

```powershell
$s = "foo"; $Arg = "-File ${s}"" ; Write-Host $Arg; Start-Process powershell -ArgumentList $Arg -Wait

```
