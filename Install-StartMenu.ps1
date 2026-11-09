$sm = [Environment]::GetFolderPath('StartMenu')
$lnkPath = Join-Path $sm 'Programs\GoAnywhere.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnkPath)
$s.TargetPath = 'C:\Users\antho\ianygo-build\dist\GoAnywhere\GoAnywhere.exe'
$s.WorkingDirectory = 'C:\Users\antho\ianygo-build\dist\GoAnywhere'
$s.IconLocation = 'C:\Users\antho\ianygo-build\dist\GoAnywhere\GoAnywhere.exe,0'
$s.Description = 'GoAnywhere - free iOS GPS spoofer'
$s.Save()
Write-Output ("shortcut: " + (Test-Path $lnkPath))
