# The PC as Argus's GPU worker after Argus moves to the laptop. Run in PowerShell as Administrator:
#   .\scripts\pc-worker.ps1 install https://laptop.ts.net   start the worker (and Ollama) at boot, without logging in
#   .\scripts\pc-worker.ps1 remove                        undo
#   .\scripts\pc-worker.ps1 status
#   .\scripts\pc-worker.ps1 check https://laptop.ts.net     is everything ready for the move? (changes nothing)
# The worker runs under the supervisor (restarts it, and restarts on new code after "git pull").
# Put the laptop's ARGUS_WORKER_TOKEN in this folder's .env first.
param(
    [Parameter(Position = 0)][string]$Command = "status",
    [Parameter(Position = 1)][string]$Url = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Task = "Argus PC worker"
$OllamaTask = "Argus Ollama"
$DesktopTask = "Argus desktop"

switch ($Command) {
    "install" {
        if (-not $Url) { throw "Usage: .\scripts\pc-worker.ps1 install https://<laptop>.<tailnet>.ts.net" }
        if (-not (Test-Path $Py)) { throw "No .venv yet: run .\scripts\dev.ps1 setup first" }
        $User = "$env:USERDOMAIN\$env:USERNAME"
        $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
        $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
        $Boot = New-ScheduledTaskTrigger -AtStartup

        $Cmd = "`$env:ARGUS_URL='$Url'; `$env:ARGUS_OLLAMA_URL='http://127.0.0.1:11434'; Set-Location '$Root'; & '$Py' -m argus.supervisor --no-argusd --no-session"
        $Act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -Command `"$Cmd`""
        Register-ScheduledTask -TaskName $Task -Action $Act -Trigger $Boot -Principal $Principal -Settings $Settings -Force | Out-Null
        Write-Host "[ok] '$Task' starts at boot (before anyone logs in) and connects to $Url"

        $Ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
        if ($Ollama) {
            $OAct = New-ScheduledTaskAction -Execute $Ollama -Argument "serve"
            Register-ScheduledTask -TaskName $OllamaTask -Action $OAct -Trigger $Boot -Principal $Principal -Settings $Settings -Force | Out-Null
            Write-Host "[ok] '$OllamaTask' starts Ollama at boot"
        } else { Write-Host "[--] Ollama not found: install it (winget install Ollama.Ollama) and run install again" }

        # Ari's PC tools (open apps, volume, ...), "Hey Ari" and the island need your logged-in session: a second
        # supervisor at logon runs them (a session-only worker, the listener, the island), talking to the laptop.
        $DCmd = "`$env:ARGUS_URL='$Url'; Set-Location '$Root'; & '$Py' -m argus.supervisor --no-argusd --desk-only"
        $DAct = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -Command `"$DCmd`""
        $Logon = New-ScheduledTaskTrigger -AtLogOn -User $User
        $DPrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $DesktopTask -Action $DAct -Trigger $Logon -Principal $DPrincipal -Settings $Settings -Force | Out-Null
        Write-Host "[ok] '$DesktopTask' starts when you log in (Ari's PC tools, Hey Ari, the island)"

        # start them now too (not only at the next boot / logon): without Ollama running, Ari has no local models
        foreach ($T in @($OllamaTask, $Task, $DesktopTask)) {
            if (Get-ScheduledTask -TaskName $T -ErrorAction SilentlyContinue) { Start-ScheduledTask -TaskName $T }
        }
        Write-Host "Started. It shows up in Helios (on the laptop) within a minute. Logs: $Root\logs\worker.log"
        Write-Host "Also: BIOS Wake-on-LAN on, network card 'Wake on Magic Packet' on, Fast Startup off."
    }
    "check" {
        if (-not $Url) { throw "Usage: .\scripts\pc-worker.ps1 check https://<laptop>.<tailnet>.ts.net" }
        Set-Location $Root
        & $Py -m argus.movecheck $Url
    }
    "remove" {
        foreach ($T in @($Task, $OllamaTask, $DesktopTask)) {
            if (Get-ScheduledTask -TaskName $T -ErrorAction SilentlyContinue) {
                Stop-ScheduledTask -TaskName $T -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $T -Confirm:$false
                Write-Host "[ok] removed '$T'"
            }
        }
    }
    default {
        foreach ($T in @($Task, $OllamaTask, $DesktopTask)) {
            $S = Get-ScheduledTask -TaskName $T -ErrorAction SilentlyContinue
            if ($S) { Write-Host ("  {0,-18} {1}" -f $T, $S.State) } else { Write-Host ("  {0,-18} not installed" -f $T) }
        }
        # "Ready" means installed but not running right now; Ollama must answer for Ari's local models
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null
            Write-Host "  Ollama             answering"
        } catch { Write-Host "  Ollama             NOT answering: Start-ScheduledTask 'Argus Ollama' (or open the Ollama app)" }
    }
}
