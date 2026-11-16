# One sparse package per device, so each is its own top-level entry in the Windows 11 right-click menu (Windows puts
# several verbs of one package under a submenu). All share send_menu.dll in the external location (build\app).
param([Parameter(Mandatory)][string]$Build, [Parameter(Mandatory)][string]$Sdk, [string[]]$Devices = @("phone", "calypso"))
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Table = @{
    phone   = @{ Id = "Phone";   Name = "my phone"; Clsid = "18078fca-6f80-4237-bb30-b9e3004e444e" }
    calypso = @{ Id = "Calypso"; Name = "Calypso";  Clsid = "aa8ffa12-3cef-4798-80ed-b1eae89eb6d8" }
    pc      = @{ Id = "Pc";      Name = "PC";       Clsid = "9056c0b5-5657-4668-8f04-1a823db93665" }
}
foreach ($dev in $Devices) {
    $t = $Table[$dev]
    $pkg = Join-Path $Build "pkg-$dev"
    New-Item -ItemType Directory -Force "$pkg\Assets" | Out-Null
    Copy-Item (Join-Path $Build "app\Assets\logo.png") "$pkg\Assets\logo.png"
    $xml = (Get-Content (Join-Path $Here "AppxManifest.xml.in") -Raw).Replace("@ID@", $t.Id).Replace("@NAME@", $t.Name).Replace("@CLSID@", $t.Clsid)
    [System.IO.File]::WriteAllText("$pkg\AppxManifest.xml", $xml, (New-Object System.Text.UTF8Encoding $false))
    & "$Sdk\makeappx.exe" pack /o /d $pkg /nv /p (Join-Path $Build "ArgusSend-$dev.msix") | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "makeappx failed for $dev" }
}
