$appRoot = "D:\src\TalaV2"
Start-Process -FilePath "powershell.exe" -ArgumentList "-ExecutionPolicy Bypass -NoExit -File D:\src\TalaV2\scripts\start-tala.ps1" -WorkingDirectory $appRoot
