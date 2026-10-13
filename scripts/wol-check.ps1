# Can Argus wake this PC? Looks at the Windows side of Wake-on-LAN and, with -Fix (run as administrator), turns on
# what's off. The BIOS side can't be checked from Windows: it is printed as a reminder.
#   .\scripts\dev.ps1 wol-check        look
#   .\scripts\dev.ps1 wol-check fix    look and fix (asks for administrator)
param([switch]$Fix)
$ErrorActionPreference = "Continue"

function Say($ok, $text) {
    $mark = if ($ok -eq $true) { "ok" } elseif ($ok -eq $false) { "!!" } else { "--" }
    $colour = switch ($mark) { "ok" { "Green" } "!!" { "Red" } default { "Yellow" } }
    Write-Host ("  [{0}] {1}" -f $mark, $text) -ForegroundColor $colour
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($Fix -and -not $admin) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$PSCommandPath`" -Fix"
    exit 0
}

Write-Host "Wake-on-LAN on this PC" -ForegroundColor Cyan
$wired = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.MediaType -eq "802.3" })
if ($wired.Count -eq 0) {
    Say $false "no wired network card found: Wi-Fi can't be woken, the PC needs a cable"
    exit 1
}
foreach ($a in $wired) {
    $mac = ($a.MacAddress -replace "-", ":").ToUpper()
    Write-Host ""
    Write-Host ("{0}  ({1})  {2}" -f $a.Name, $a.InterfaceDescription, $a.Status)
    Say ($a.Status -eq "Up") ("cable connected: " + $a.Status)
    Say $null ("power.pc_mac for the laptop's argus.yaml:  pc_mac: " + $mac)

    $pm = Get-NetAdapterPowerManagement -Name $a.Name -ErrorAction SilentlyContinue
    if ($pm) {
        $magic = "$($pm.WakeOnMagicPacket)" -eq "Enabled"
        Say $magic ("Wake on Magic Packet (power management): " + $pm.WakeOnMagicPacket)
        if ($Fix -and -not $magic) {
            Set-NetAdapterPowerManagement -Name $a.Name -WakeOnMagicPacket Enabled -ErrorAction SilentlyContinue
            Say $null "  -> turned on"
        }
    }
    $props = Get-NetAdapterAdvancedProperty -Name $a.Name -ErrorAction SilentlyContinue
    foreach ($kw in @("*WakeOnMagicPacket", "S5WakeOnLan", "WakeOnLink", "*ModernStandbyWoLMagicPacket")) {
        $p = $props | Where-Object { $_.RegistryKeyword -eq $kw }
        if (-not $p) { continue }
        $on = "$($p.RegistryValue)" -eq "1"
        Say $on ("{0} ({1}): {2}" -f $p.DisplayName, $kw, $p.DisplayValue)
        if ($Fix -and -not $on -and $kw -ne "WakeOnLink") {
            Set-NetAdapterAdvancedProperty -Name $a.Name -RegistryKeyword $kw -RegistryValue 1 -ErrorAction SilentlyContinue
            Say $null "  -> turned on"
        }
    }
    $armed = (powercfg /devicequery wake_armed) -contains $a.InterfaceDescription
    Say $armed "allowed to wake the computer (Device Manager > Power Management)"
    if ($Fix -and -not $armed) {
        powercfg /deviceenablewake "$($a.InterfaceDescription)" | Out-Null
        Say $null "  -> allowed"
    }
}

Write-Host ""
$key = "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power"
$fast = (Get-ItemProperty -Path $key -Name HiberbootEnabled -ErrorAction SilentlyContinue).HiberbootEnabled
Say ($fast -ne 1) ("Windows Fast Startup " + $(if ($fast -eq 1) { "is ON (a shut-down PC often won't wake)" } else { "is off" }))
if ($Fix -and $fast -eq 1) {
    Set-ItemProperty -Path $key -Name HiberbootEnabled -Value 0
    Say $null "  -> turned off"
}

Write-Host ""
Say $null "BIOS (can't be checked from Windows): Power On By PCI-E / Wake on LAN = Enabled, ErP = Disabled"
if (-not $Fix) { Write-Host "Fix what's red:  .\scripts\dev.ps1 wol-check fix" -ForegroundColor Cyan }
