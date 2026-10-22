# Ari's Workstation as its own Windows session (a "child session", Windows 10/11 Pro): Ari works in a window of its
# own and never moves your windows or pulls you over to another desktop. Run in PowerShell as Administrator:
#   .\scripts\workstation-setup.ps1 install https://laptop.ts.net   turn child sessions on, start Ari's worker in there
#   .\scripts\workstation-setup.ps1 status
#   .\scripts\workstation-setup.ps1 remove                         back to the virtual desktop
# Then set ari.workstation: child in argus.yaml and restart (.\scripts\pc-worker.ps1 restart).
param(
    [Parameter(Position = 0)][string]$Command = "status",
    [Parameter(Position = 1)][string]$Url = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Startup = [Environment]::GetFolderPath("Startup")
$Link = Join-Path $Startup "Argus workstation.lnk"

switch ($Command) {
    "install" {
        if (-not $Url) { throw "Usage: .\scripts\workstation-setup.ps1 install https://<laptop>.<tailnet>.ts.net" }
        $Os = (Get-CimInstance Win32_OperatingSystem).Caption
        if ($Os -notmatch "Pro|Enterprise|Education") { throw "Child sessions need Windows Pro, Enterprise or Education (this is: $Os)" }
        Set-Location $Root
        & $Py -m pip install -q "comtypes>=1.4"
        & $Py -m argus.child_session enable
        if ($LASTEXITCODE -ne 0) { throw "Couldn't turn child sessions on (run PowerShell as Administrator)" }
        # Startup-folder entries start in the child session too: there it runs the Workstation's worker; in your own
        # session it sees it isn't the child session and stops at once.
        $Cmd = "`$env:ARGUS_URL='$Url'; Set-Location '$Root'; & '$Py' -m argus.supervisor --no-argusd --only-in-child"
        $Sh = New-Object -ComObject WScript.Shell
        $L = $Sh.CreateShortcut($Link)
        $L.TargetPath = "powershell.exe"
        $L.Arguments = "-NoProfile -WindowStyle Hidden -Command `"$Cmd`""
        $L.WorkingDirectory = $Root
        $L.WindowStyle = 7
        $L.Save()
        Write-Host "[ok] child sessions are on; Ari's worker starts inside it ($Link)"
        Write-Host "Now: in argus.yaml set  ari: {workstation: child}  then  .\scripts\pc-worker.ps1 restart"
        Write-Host "A window 'Ari's Workstation' opens with Ari's own session; Helios shows the worker workstation-<pc>."
    }
    "status" {
        Set-Location $Root
        & $Py -m argus.child_session status
        Write-Host ("Startup entry: " + $(if (Test-Path $Link) { "yes ($Link)" } else { "no" }))
    }
    "remove" {
        if (Test-Path $Link) { Remove-Item $Link -Force }
        Write-Host "[ok] removed the Startup entry. Set ari.workstation: desktop in argus.yaml and restart."
        Write-Host "(Child sessions stay allowed in Windows; nothing uses them now.)"
    }
    default { throw "install, status or remove" }
}
