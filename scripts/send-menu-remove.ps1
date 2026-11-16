# Undo send-menu-install.ps1: unregister the package, drop the certificate, the SendTo shortcuts and the build output.
# Run in PowerShell as Administrator (the certificate lives in a machine store).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Subject = "CN=Argus Send Menu"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this in PowerShell as Administrator: removing the trusted certificate needs it." }

Get-AppxPackage -Name "Argus.SendMenu" | Remove-AppxPackage
foreach ($store in "Cert:\LocalMachine\TrustedPeople", "Cert:\CurrentUser\My") {
    Get-ChildItem $store | Where-Object { $_.Subject -eq $Subject } | Remove-Item
}
$sendTo = Join-Path $env:APPDATA "Microsoft\Windows\SendTo"
Remove-Item (Join-Path $sendTo "Argus - my phone.lnk"), (Join-Path $sendTo "Argus - Calypso.lnk") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:LOCALAPPDATA "Argus\send\argus-exe.txt") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Root "windows\send-menu\build") -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Removed. Restart Explorer if the entries are still showing."
