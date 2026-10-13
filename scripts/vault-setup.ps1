# The notes vault on this PC: Syncthing (installed once, starts at logon) + ~/Vault + the Syncthing folder.
#   .\scripts\vault-setup.ps1                     install/start Syncthing, make ~/Vault, share it; prints this PC's ID
#   .\scripts\vault-setup.ps1 add <ID> <name> <tailscale-ip>   another device of yours (the laptop, the phone)
#   .\scripts\vault-setup.ps1 status              who is connected, is the vault in step
# Devices only meet at their Tailscale addresses (discovery and relays are off). See plugins/vault/README.md.
param(
    [Parameter(Position = 0)][string]$Command = "setup",
    [Parameter(Position = 1)][string]$Id = "",
    [Parameter(Position = 2)][string]$Name = "",
    [Parameter(Position = 3)][string]$Address = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
$Helper = Join-Path $PSScriptRoot "vault_sync.py"

function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }

function Find-Syncthing {
    $c = Get-Command syncthing -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $hits = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter syncthing.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($hits) { return $hits.FullName }
    return $null
}

function Wait-Gui {
    for ($i = 0; $i -lt 30; $i++) {
        try { Invoke-WebRequest "http://127.0.0.1:8384/rest/noauth/health" -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true }
        catch { Start-Sleep -Seconds 1 }
    }
    return $false
}

switch ($Command) {
    "setup" {
        $exe = Find-Syncthing
        if (-not $exe) {
            Step "Installing Syncthing (winget)"
            winget install --id Syncthing.Syncthing -e --accept-source-agreements --accept-package-agreements
            $exe = Find-Syncthing
            if (-not $exe) { throw "Syncthing installed, but syncthing.exe wasn't found: open a new PowerShell and run this again" }
        }
        Write-Host "OK: $exe"
        $v2 = (& $exe --version) -match "v2\."
        $stArgs = if ($v2) { "serve --no-browser" } else { "-no-browser -no-console" }
        Step "Starting Syncthing at logon (hidden)"
        $action = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-WindowStyle Hidden -NoProfile -Command `"& '$exe' $stArgs`""
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName "Syncthing (notes vault)" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        if (-not (Wait-Gui)) {
            Start-ScheduledTask -TaskName "Syncthing (notes vault)"
            if (-not (Wait-Gui)) { throw "Syncthing didn't start (try: & '$exe' $stArgs)" }
        }
        Step "Vault folder and Syncthing settings"
        & $Py $Helper setup --vault "$HOME\Vault"
        Write-Host ""
        Write-Host "Next: on the laptop run deploy/linux/vault-setup.sh, then add each device here:" -ForegroundColor Yellow
        Write-Host "  .\scripts\vault-setup.ps1 add <its ID> laptop <its 100.x.y.z>"
        Write-Host "Then open $HOME\Vault in Obsidian ('Open folder as vault')."
    }
    "add" {
        if (-not $Id -or -not $Name -or -not $Address) { throw "usage: vault-setup.ps1 add <device ID> <name> <tailscale ip>" }
        & $Py $Helper add --id $Id --name $Name --address $Address
    }
    "status" { & $Py $Helper status }
    default { throw "unknown command $Command (setup, add, status)" }
}
