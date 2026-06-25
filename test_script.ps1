Start-Process -FilePath "powershell.exe" -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Normal -NoExit -File "${startScript}"" -WorkingDirectory "${appRoot}"
