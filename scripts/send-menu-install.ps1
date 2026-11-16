# Right-click "Send to my phone" / "Send to Calypso" in the Windows 11 main context menu (files, folders, multi-select).
# Run in PowerShell as Administrator (the self-signed certificate must be trusted machine-wide):
#   .\scripts\send-menu-install.ps1
# Builds windows\send-menu, makes + trusts a self-signed code-signing certificate, signs the sparse
# MSIX packages (one per device), registers them with its files here, and adds classic "Send to" shortcuts as a fallback. Undo: send-menu-remove.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Menu = Join-Path $Root "windows\send-menu"
$Build = Join-Path $Menu "build"
$App = Join-Path $Build "app"
$Sdk = "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64"
$Subject = "CN=Argus Send Menu"
$Devices = @("phone", "calypso")   # on the PC: the two other devices; one package each so each is a top-level entry
$Old = @("Argus.SendMenu", "Argus.SendPhone", "Argus.SendCalypso", "Argus.SendPc")

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this in PowerShell as Administrator: trusting the signing certificate needs it." }

# Unregister first: Explorer keeps the DLL loaded while a package is registered, and the build replaces it.
Get-AppxPackage | Where-Object { $Old -contains $_.Name } | Remove-AppxPackage
Write-Host "Building..."
& cmd /c "`"$Menu\build.cmd`""
if ($LASTEXITCODE -ne 0) { throw "build.cmd failed" }

# 1. Certificate: kept in CurrentUser\My (never written to the repo), trusted in LocalMachine\TrustedPeople.
$cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -eq $Subject } | Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -Type Custom -Subject $Subject -KeyUsage DigitalSignature -FriendlyName "Argus send menu" `
        -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddYears(5) `
        -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")
}
$cer = Join-Path $env:TEMP "argus-send-menu.cer"
Export-Certificate -Cert $cert -FilePath $cer | Out-Null
Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\TrustedPeople | Out-Null
Remove-Item $cer -Force

# 2. Sign the packages.
foreach ($d in $Devices) {
    & "$Sdk\signtool.exe" sign /fd SHA256 /sha1 $cert.Thumbprint /s My (Join-Path $Build "ArgusSend-$d.msix")
    if ($LASTEXITCODE -ne 0) { throw "signtool failed" }
}

# 3. Tell the menu where argus.exe is and where the send agent keeps devices.json (send.data_dir in argus.yaml, default
#    ~/.argus-send), then register each package with its files in build\app (the external location).
$argus = Join-Path $Root ".venv\Scripts\argus.exe"
$sendDir = Join-Path $env:LOCALAPPDATA "Argus\send"
New-Item -ItemType Directory -Force $sendDir | Out-Null
if (Test-Path $argus) { [System.IO.File]::WriteAllText((Join-Path $sendDir "argus-exe.txt"), $argus) }
else { Write-Warning "No $argus yet; the menu will look for argus.exe on PATH. Re-run this once the venv has the send command." }
$dataDir = $null
$yaml = Join-Path $Root "argus.yaml"
if (Test-Path $yaml) {
    $inSend = $false
    foreach ($line in Get-Content $yaml) {
        if ($line -match '^send:') { $inSend = $true; continue }
        if ($inSend -and $line -match '^\S') { break }
        if ($inSend -and $line -match '^\s+data_dir:\s*["'']?([^"''#]+?)["'']?\s*(#.*)?$') { $dataDir = $Matches[1]; break }
    }
}
$dataFile = Join-Path $sendDir "data-dir.txt"
if ($dataDir) { [System.IO.File]::WriteAllText($dataFile, [Environment]::ExpandEnvironmentVariables($dataDir)) }
else { Remove-Item $dataFile -Force -ErrorAction SilentlyContinue }

foreach ($d in $Devices) { Add-AppxPackage -Path (Join-Path $Build "ArgusSend-$d.msix") -ExternalLocation $App }

# 4. Classic "Send to" fallback (Show more options > Send to). Explorer passes the selected paths as arguments.
$sendTo = Join-Path $env:APPDATA "Microsoft\Windows\SendTo"
$shell = New-Object -ComObject WScript.Shell
foreach ($d in @(@("my phone", "phone"), @("Calypso", "calypso"))) {
    $lnk = $shell.CreateShortcut((Join-Path $sendTo "Argus - $($d[0]).lnk"))
    $lnk.TargetPath = $(if (Test-Path $argus) { $argus } else { "argus.exe" })
    $lnk.Arguments = "send $($d[1])"
    $lnk.WindowStyle = 7
    $lnk.Description = "Send to $($d[0]) with Argus"
    $lnk.Save()
}

Write-Host "Done. Right-click a file or folder: 'Send to my phone' and 'Send to Calypso' are two entries in the main menu (names follow the saved device list)."
Write-Host "If they do not show yet, restart Explorer (Stop-Process -Name explorer). Remove everything with scripts\send-menu-remove.ps1"
