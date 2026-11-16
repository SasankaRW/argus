# Argus Send agent on this PC: starts at logon, and TCP 8610 is open on the Private network profile only.
# Run in PowerShell as Administrator (the firewall rule needs it):
#   .\scripts\send-agent-install.ps1 install     scheduled task at logon + firewall rule, starts the agent now
#   .\scripts\send-agent-install.ps1 status
#   .\scripts\send-agent-install.ps1 remove      task and rule gone (received files and keys stay)
# Set the send: block in argus.yaml first (device_id, name, tailscale name); see argus.example.yaml.
param(
    [Parameter(Position = 0)][string]$Command = "status",
    [int]$Port = 8610
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Task = "Argus Send agent"
$Rule = "Argus Send (TCP $Port, Private)"

switch ($Command) {
    "install" {
        if (-not (Test-Path $Py)) { throw "No virtual environment at $Py. Run the Argus setup first." }
        $Action = New-ScheduledTaskAction -Execute $Py -Argument "-m argus.send send-agent" -WorkingDirectory $Root
        $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
        Register-ScheduledTask -TaskName $Task -Action $Action -Trigger $Trigger -Settings $Settings -Force `
            -Description "Argus Send: send and receive files between my devices" | Out-Null
        if (-not (Get-NetFirewallRule -DisplayName $Rule -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $Rule -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
                -Profile Private -Program $Py | Out-Null
        }
        Start-ScheduledTask -TaskName $Task
        Write-Host "Installed: '$Task' runs at logon; port $Port is open on Private networks only."
    }
    "remove" {
        Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
        Remove-NetFirewallRule -DisplayName $Rule -ErrorAction SilentlyContinue
        Write-Host "Removed the task and the firewall rule."
    }
    default {
        $t = Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
        Write-Host ("Task: " + $(if ($t) { $t.State } else { "not installed" }))
        $r = Get-NetFirewallRule -DisplayName $Rule -ErrorAction SilentlyContinue
        Write-Host ("Firewall rule: " + $(if ($r) { "$($r.Enabled), profile $($r.Profile)" } else { "none" }))
    }
}
