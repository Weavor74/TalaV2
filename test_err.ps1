$s = "foo"; $Arg = "-File ${s}"" ; Write-Host $Arg; Start-Process powershell -ArgumentList $Arg -Wait
