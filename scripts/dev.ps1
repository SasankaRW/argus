# Argus dev helper for Windows (PowerShell).
#   .\scripts\dev.ps1 up      start everything: Ollama, argusd, a worker, then open Helios (or double-click scripts\up.cmd)
#   .\scripts\dev.ps1 down    stop argusd and the worker started by "up" ("down all" also stops Ollama)
#   .\scripts\dev.ps1 status  what is running
#   .\scripts\dev.ps1 logs    every log in one terminal ("logs worker" or "logs argus,worker" for some); also in Helios
#   .\scripts\dev.ps1 setup   create .venv, install Argus and dev tools, copy example config
#   .\scripts\dev.ps1 test    run tests
#   .\scripts\dev.ps1 lint    run ruff
#   .\scripts\dev.ps1 check   validate argus.yaml and the database
#   .\scripts\dev.ps1 run     start argusd
#   .\scripts\dev.ps1 worker  run a worker in this terminal (for debugging; "up" runs one in the background)
#   .\scripts\dev.ps1 demo    submit a demo job (needs argusd and a worker running)
#   .\scripts\dev.ps1 events  watch live events in the terminal
#   .\scripts\dev.ps1 helios  rebuild Helios after changing helios/src (needs Node.js; the built copy is in Git)
#   .\scripts\dev.ps1 models  check Ollama, the tier models and Claude ("models claude" also makes one real Claude call)
#   .\scripts\dev.ps1 classify  send a model job; T1's answer is rejected on purpose so you see an escalation
#   .\scripts\dev.ps1 notify    send a test notification to the Argus phone app
#   .\scripts\dev.ps1 approval  send a pretend bill that waits for your approval (phone or Helios)
#   .\scripts\dev.ps1 approve   approve the newest waiting approval ("approve no" rejects it)
#   .\scripts\dev.ps1 mcp       let Claude Code use Argus (adds Argus as an MCP server to the claude CLI)
#
# Version control (see CONTRIBUTING.md):
#   .\scripts\dev.ps1 github            one-time: log in to GitHub, create the private repo, push everything
#   .\scripts\dev.ps1 apply pkg.tgz     a change package: new branch, files, changelog, Helios, tests, one commit
#   .\scripts\dev.ps1 branch feat/name  start work on a new branch from an up-to-date main
#   .\scripts\dev.ps1 pr                push the branch and open a pull request (title + description from the commit)
#   .\scripts\dev.ps1 merge             wait for CI, then squash-merge the pull request and return to main
#   .\scripts\dev.ps1 ship a.tgz b.tgz  apply, pr and merge each package in turn (stops at the first problem)
#   .\scripts\dev.ps1 rerun             re-run CI's failed jobs once (a flaky test unrelated to the change)
#   .\scripts\dev.ps1 sync              bring main up to date with GitHub
#   .\scripts\dev.ps1 release minor     release: tests, version bump, changelog, tag, push, GitHub release
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(Position = 1)][string]$Arg = "",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$More = @()
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"

$Stamp = Join-Path $Root ".venv\argus-deps.hash"
# Everything the PC runs: plugins' helpers, Ari's voice (Piper), hearing and "Hey Ari" (Whisper), the Ari popup (Qt)
$Extras = "dev,plugins,voice,listen,popup"
$UpFile = Join-Path $Root ".venv\argus-up.json"
$ArgusUrl = "http://127.0.0.1:8600"
$LogDir = Join-Path $Root "logs"
$OllamaUrl = if ($env:ARGUS_OLLAMA_URL) { $env:ARGUS_OLLAMA_URL } else { "http://127.0.0.1:11434" }

function Need-Venv {
    if (-not (Test-Path $Py)) { throw "No .venv yet. Run: .\scripts\dev.ps1 setup" }
    # New version with new dependencies? Update .venv automatically.
    $Hash = (Get-FileHash (Join-Path $Root "pyproject.toml")).Hash + ":" + $Extras
    if (-not (Test-Path $Stamp) -or (Get-Content $Stamp) -ne $Hash) {
        Write-Host "Dependencies changed; updating .venv ..."
        & $Py -m pip install -q -e ".[$Extras]"
        if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
        Set-Content $Stamp $Hash
    }
}

function GitOk {
    # Run git and stop on failure (PowerShell does not stop on a failing program by itself).
    & git @args
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed" }
}

function Quiet {
    # Run a program ignoring its error output; returns its exit code. (Windows PowerShell 5.1 would
    # otherwise stop the script when a program writes to stderr while errors are set to Stop.)
    param([scriptblock]$Block)
    $Old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Block 2>&1 | Out-Null; return $LASTEXITCODE } finally { $ErrorActionPreference = $Old }
}

function Ensure-Hooks { git config core.hooksPath .githooks | Out-Null }

function Need-Gh {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Host "Installing the GitHub CLI (gh) ..."
        winget install --id GitHub.cli -e --accept-source-agreements --accept-package-agreements
        throw "GitHub CLI installed. Close and reopen PowerShell, then run the command again."
    }
    if ((Quiet { gh auth status }) -ne 0) {
        Write-Host "Log in to GitHub (a browser window opens) ..."
        gh auth login --hostname github.com --git-protocol https --web
        if ($LASTEXITCODE -ne 0) { throw "GitHub login failed" }
    }
    gh auth setup-git | Out-Null
}

function Submit-Job([string]$Body, [int]$Seconds) {
    $Headers = @{}
    if (Test-Path ".env") {
        $Line = Get-Content ".env" | Where-Object { $_ -match "^ARGUS_WORKER_TOKEN=(.+)$" } | Select-Object -First 1
        if ($Line -match "^ARGUS_WORKER_TOKEN=(.+)$") { $Headers["Authorization"] = "Bearer $($Matches[1].Trim())" }
    }
    $Job = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8600/jobs" -Headers $Headers -ContentType "application/json" -Body $Body
    Write-Host "Submitted job $($Job.id). Waiting for a worker ..."
    for ($i = 0; $i -lt $Seconds * 2; $i++) {
        Start-Sleep -Milliseconds 500
        $J = Invoke-RestMethod -Uri "http://127.0.0.1:8600/jobs/$($Job.id)" -Headers $Headers
        if ($J.state -in @("succeeded", "dead", "retry")) { break }
    }
    Write-Host "State: $($J.state)"
    if ($J.result) { $J.result | ConvertTo-Json -Compress }
    if ($J.error) { Write-Host "Error: $($J.error)" }
}

function Is-Up([string]$Url) {
    try { Invoke-RestMethod -Uri $Url -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

function Wait-Up([string]$Url, [int]$Seconds, [string]$What) {
    for ($i = 0; $i -lt $Seconds * 2; $i++) {
        if (Is-Up $Url) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "$What did not start within $Seconds s."
}

function Start-Hidden([string]$Name, [string]$Exe, [string[]]$ArgList, [string]$OutFile, [string]$ErrFile) {
    # No console window: output goes to files in logs\, shown in Helios (Logs) and by ".\scripts\dev.ps1 logs".
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Start-Process -FilePath $Exe -ArgumentList $ArgList -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir $OutFile) -RedirectStandardError (Join-Path $LogDir $ErrFile)
}

function Show-Tail([string]$File, [int]$Lines = 15) {
    $F = Join-Path $LogDir $File
    if ((Test-Path $F) -and (Get-Item $F).Length -gt 0) {
        Write-Host "  --- last lines of logs\$File ---" -ForegroundColor DarkGray
        Get-Content $F -Tail $Lines | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    }
}

function Line([string]$Mark, [string]$Name, [string]$State, [string]$Detail) {
    $Colour = switch ($Mark) { "ok" { "Green" } "--" { "Yellow" } default { "Red" } }
    Write-Host ("  [" + $Mark + "] ") -ForegroundColor $Colour -NoNewline
    Write-Host ($Name.PadRight(9)) -NoNewline
    Write-Host ($State.PadRight(10)) -ForegroundColor Gray -NoNewline
    Write-Host $Detail -ForegroundColor DarkGray
}

function Rule { Write-Host ("  " + ("-" * 58)) -ForegroundColor DarkGray }

# What "up" started: pid plus start time, because Windows reuses pids quickly. A record only counts when both
# still match, so "down" never kills an unrelated program and "up" never trusts a stale entry.
function Proc-Record($P) { @{ pid = $P.Id; started = $P.StartTime.ToUniversalTime().ToString("o") } }

function Load-Up {
    $R = @{}
    if (Test-Path $UpFile) {
        try { (Get-Content $UpFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $R[$_.Name] = $_.Value } } catch { }
    }
    $R
}

function Save-Up($R) {
    if ($R.Count) { $R | ConvertTo-Json | Set-Content $UpFile } elseif (Test-Path $UpFile) { Remove-Item $UpFile }
}

function Live-Proc($Rec) {
    if (-not $Rec -or -not ($Rec.PSObject.Properties.Name -contains "pid")) { return $null }
    $P = Get-Process -Id $Rec.pid -ErrorAction SilentlyContinue
    if ($P -and $P.StartTime.ToUniversalTime().ToString("o") -eq $Rec.started) { return $P }
    return $null
}

function Token {
    if (-not (Test-Path ".env")) { return "" }
    $Line = Get-Content ".env" | Where-Object { $_ -match "^ARGUS_WORKER_TOKEN=(.+)$" } | Select-Object -First 1
    if ($Line -match "^ARGUS_WORKER_TOKEN=(.+)$") { return $Matches[1].Trim() }
    return ""
}

function Current-Branch { (git rev-parse --abbrev-ref HEAD).Trim() }

switch ($Command) {
    "up" {
        Need-Venv
        $Rec = Load-Up
        $Ver = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()
        Write-Host ""
        Write-Host "  Argus " -NoNewline -ForegroundColor Yellow
        Write-Host "v$Ver" -ForegroundColor DarkGray
        Rule

        if (Is-Up "$OllamaUrl/api/version") { Line "ok" "Ollama" "running" $OllamaUrl }
        elseif (Get-Command ollama -ErrorAction SilentlyContinue) {
            $O = Start-Hidden "ollama" (Get-Command ollama).Source @("serve") "ollama.out" "ollama.log"
            $Rec.ollama = Proc-Record $O
            Save-Up $Rec
            try { Wait-Up "$OllamaUrl/api/version" 30 "Ollama" } catch { Line "!!" "Ollama" "failed" "see below"; Show-Tail "ollama.log"; throw }
            Line "ok" "Ollama" "started" $OllamaUrl
        }
        else { Line "--" "Ollama" "missing" "model jobs will wait (winget install Ollama.Ollama)" }

        # argusd and the worker run under the supervisor: it restarts them if they stop, and on new code
        # (a merged pull request) once no job is running.
        if (Live-Proc $Rec.supervisor) { Line "ok" "argusd" "running" "$ArgusUrl (supervised)" }
        else {
            foreach ($Old in @("worker", "argusd")) {  # from an older "up" that started them one by one
                $P = Live-Proc $Rec.$Old
                if ($P) { Quiet { taskkill /PID $P.Id /T /F } | Out-Null }
                $Rec.Remove($Old)
            }
            if (Is-Up "$ArgusUrl/health") { throw "Something else is already running on $ArgusUrl. Stop it first." }
            $S = Start-Hidden "supervisor" $Py @("-m", "argus.supervisor") "supervisor.out" "supervisor-crash.log"
            $Rec.supervisor = Proc-Record $S
            Save-Up $Rec
            try { Wait-Up "$ArgusUrl/health" 45 "argusd" }
            catch { Line "!!" "argusd" "failed" "see below"; Show-Tail "supervisor-crash.log"; Show-Tail "argusd-crash.log"; Show-Tail "argus.log"; throw }
            Line "ok" "argusd" "started" "$ArgusUrl (supervised)"
            Start-Sleep -Seconds 4
            $Wk = $false
            try { $St = Invoke-RestMethod -Uri "$ArgusUrl/status" -TimeoutSec 3; $Wk = @($St.workers | Where-Object { $_.state -eq "online" }).Count -gt 0 } catch { }
            if ($Wk) { Line "ok" "worker" "started" "desktop, gpu" } else { Line "--" "worker" "starting" "see Helios > Logs if it stays offline" }
        }
        Rule
        $T = Token
        Start-Process ($(if ($T) { "$ArgusUrl/?token=$T" } else { $ArgusUrl }))
        Write-Host "  Helios   " -NoNewline; Write-Host "$ArgusUrl  (opened in your browser)" -ForegroundColor Cyan
        Write-Host "  Logs     " -NoNewline; Write-Host "Helios > Logs, or .\scripts\dev.ps1 logs" -ForegroundColor DarkGray
        Write-Host "  Stop     " -NoNewline; Write-Host ".\scripts\dev.ps1 down" -ForegroundColor DarkGray
        Write-Host ""
    }
    "status" {
        $Rec = Load-Up
        Write-Host ""
        Line $(if (Is-Up "$OllamaUrl/api/version") { "ok" } else { "--" }) "Ollama" $(if (Is-Up "$OllamaUrl/api/version") { "running" } else { "stopped" }) $OllamaUrl
        Line $(if (Is-Up "$ArgusUrl/health") { "ok" } else { "--" }) "argusd" $(if (Is-Up "$ArgusUrl/health") { "running" } else { "stopped" }) $ArgusUrl
        $SP = Live-Proc $Rec.supervisor
        Line $(if ($SP) { "ok" } else { "--" }) "supervisor" $(if ($SP) { "running" } else { "stopped" }) $(if ($SP) { "restarts argusd and the worker on new code" } else { "" })
        Write-Host ""
    }
    "logs" {
        # Every log in one terminal, coloured; e.g. ".\scripts\dev.ps1 logs worker" for one of them.
        Need-Venv
        $Names = @(); if ($Arg) { $Names = $Arg -split "," }
        & $Py -m argus.logview @Names
    }
    "down" {
        $Names = @("supervisor", "worker", "argusd")
        if ($Arg -eq "all") { $Names += "ollama" }
        $Left = Load-Up
        if ($Left.Count -eq 0) { Write-Host "Nothing started by 'up' is recorded." }
        foreach ($N in $Names) {
            if (-not $Left.ContainsKey($N)) { continue }
            $P = Live-Proc $Left[$N]
            # /T also stops the python process running inside the window
            if ($P -and (Quiet { taskkill /PID $P.Id /T /F }) -eq 0) { Line "ok" $N "stopped" "" }
            else { Line "--" $N "was not running" "" }
            $Left.Remove($N)
        }
        # stopped on purpose: not a power cut (argusd leaves this file only when it did not stop by itself)
        if (Test-Path "data\argus.running") { Remove-Item "data\argus.running" -ErrorAction SilentlyContinue }
        Save-Up $Left
        if ($Arg -ne "all" -and (Is-Up "$OllamaUrl/api/version")) { Write-Host "Ollama keeps running (use 'down all' to stop it too)." }
    }
    "setup" {
        if (-not (Test-Path $Py)) {
            Write-Host "Creating .venv ..."
            py -3 -m venv .venv
        }
        & $Py -m pip install --upgrade pip | Out-Null
        & $Py -m pip install -e ".[$Extras]"
        Set-Content $Stamp (Get-FileHash (Join-Path $Root "pyproject.toml")).Hash
        if (-not (Test-Path "argus.yaml")) { Copy-Item "argus.example.yaml" "argus.yaml"; Write-Host "Created argus.yaml" }
        if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Created .env (edit the password)" }
        Ensure-Hooks
        Write-Host "Setup done. Next: .\scripts\dev.ps1 test"
    }
    "test"  { Need-Venv; & $Py -m pytest -q }
    "lint"  { Need-Venv; & $Py -m ruff check core tests scripts }
    "check" { Need-Venv; & $Py -m argus --check }
    "run"   { Need-Venv; & $Py -m argus }
    "worker" { Need-Venv; & $Py -m argus.worker.cli --cap desktop --cap gpu }
    "events" { Need-Venv; & $Py -m argus.tail }
    "models" {
        Need-Venv
        if ($Arg -eq "claude") { & $Py -m argus.models.doctor --claude } else { & $Py -m argus.models.doctor }
    }
    "helios" {
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "Needs Node.js (https://nodejs.org). Not needed just to use Helios." }
        Push-Location (Join-Path $Root "helios")
        try {
            npm ci
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
            npm run build
            if ($LASTEXITCODE -ne 0) { throw "build failed" }
        } finally { Pop-Location }
        Write-Host "Helios rebuilt. Restart argusd and open http://127.0.0.1:8600"
    }
    "demo"  { Need-Venv; Submit-Job '{"plugin":"demo","workflow":"echo","input":{"text":"hello from argus"}}' 20 }
    "classify" {
        Need-Venv
        $File = if ($Arg) { $Arg } else { "lecture_07.pdf" }
        Submit-Job ('{"plugin":"demo","workflow":"classify","input":{"filename":"' + $File + '","reject_tier":"T1"}}') 600
    }
    { $_ -in "notify", "ntfy" } {
        $H = @{}; $T = Token; if ($T) { $H["Authorization"] = "Bearer $T" }
        try { Invoke-RestMethod -Method Post -Uri "$ArgusUrl/outbox/test" -Headers $H | Out-Null }
        catch { throw "Could not send: $($_.ErrorDetails.Message) (is argusd running? is the phone app signed in?)" }
        Write-Host "Test message queued. The Argus phone app shows it within a second or two (or when it next connects)."
    }
    "mcp" {
        $T = Token
        if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { throw "claude CLI not found (npm install -g @anthropic-ai/claude-code)" }
        $Hdr = @(); if ($T) { $Hdr = @("--header", "Authorization: Bearer $T") }
        Quiet { claude mcp remove --scope user argus } | Out-Null
        & claude mcp add --transport http --scope user argus "$ArgusUrl/mcp" @Hdr
        if ($LASTEXITCODE -ne 0) { throw "claude mcp add failed" }
        Write-Host "Done. In Claude Code, ask: 'what is Argus doing?' (tools: argus_status, list_jobs, get_job, read_log, run_button, ...)"
    }
    "approval" {
        $H = @{}; $T = Token; if ($T) { $H["Authorization"] = "Bearer $T" }
        $Body = '{"plugin":"demo","workflow":"approval","input":{"vendor":"CEB","amount":"4250","due":"2026-10-15"}}'
        $Job = Invoke-RestMethod -Method Post -Uri "$ArgusUrl/jobs" -Headers $H -ContentType "application/json" -Body $Body
        Write-Host "Submitted job $($Job.id)."
        $Said = ""
        for ($i = 0; $i -lt 600; $i++) {
            Start-Sleep -Milliseconds 500
            $J = Invoke-RestMethod -Uri "$ArgusUrl/jobs/$($Job.id)" -Headers $H
            if ($J.state -ne $Said) {
                if ($J.state -eq "waiting") { Write-Host "Waiting for you: tap Approve on your phone, or open the Approvals box in Helios (or run: .\scripts\dev.ps1 approve)" }
                else { Write-Host "State: $($J.state)" }
                $Said = $J.state
            }
            if ($J.state -in @("succeeded", "dead", "cancelled")) { break }
        }
        if ($J.result) { $J.result | ConvertTo-Json -Compress }
    }
    "approve" {
        $H = @{}; $T = Token; if ($T) { $H["Authorization"] = "Bearer $T" }
        $P = @(Invoke-RestMethod -Uri "$ArgusUrl/approvals?state=pending&limit=1" -Headers $H)
        if ($P.Count -eq 0 -or -not $P[0]) { Write-Host "Nothing is waiting for approval."; break }
        $Answer = if ($Arg -in @("no", "reject")) { "reject" } else { "approve" }
        $R = Invoke-RestMethod -Method Post -Uri "$ArgusUrl/approvals/$($P[0].id)/decide" -Headers $H -ContentType "application/json" -Body (@{ answer = $Answer; by = "dev.ps1" } | ConvertTo-Json)
        Write-Host "$($P[0].plugin): $($P[0].title) -> $($R.state)"
    }
    "github" {
        Need-Gh
        Ensure-Hooks
        if ((Current-Branch) -ne "main") { throw "Switch to main first: git switch main" }
        $HasOrigin = (git remote) -contains "origin"
        if (-not $HasOrigin) {
            $Name = if ($Arg) { $Arg } else { "argus" }
            Write-Host "Creating private GitHub repo '$Name' ..."
            gh repo create $Name --private --source . --remote origin --description "Argus: local-first LLM orchestrator with the Helios dashboard"
            if ($LASTEXITCODE -ne 0) { throw "Could not create the repo (does '$Name' already exist? Run: .\scripts\dev.ps1 github other-name)" }
        }
        $env:ARGUS_ALLOW_MAIN = "1"
        try { GitOk push -u origin main; GitOk push origin --tags } finally { Remove-Item Env:ARGUS_ALLOW_MAIN }
        $Repo = (gh repo view --json nameWithOwner -q .nameWithOwner).Trim()
        gh repo edit $Repo --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false --delete-branch-on-merge | Out-Null
        # Branch rules on GitHub. GitHub Free does not enforce them on private repos; the local hook still does.
        $Main = @{ ref_name = @{ include = @("~DEFAULT_BRANCH"); exclude = @() } }
        $Hard = @{ name = "main: never deleted or force-pushed"; target = "branch"; enforcement = "active"; conditions = $Main
                   rules = @(@{ type = "deletion" }, @{ type = "non_fast_forward" }) } | ConvertTo-Json -Depth 10
        $Flow = @{ name = "main: pull requests with green CI"; target = "branch"; enforcement = "active"; conditions = $Main
                   bypass_actors = @(@{ actor_id = 5; actor_type = "RepositoryRole"; bypass_mode = "always" })
                   rules = @(
                       @{ type = "pull_request"; parameters = @{ required_approving_review_count = 0; dismiss_stale_reviews_on_push = $false
                          require_code_owner_review = $false; require_last_push_approval = $false; required_review_thread_resolution = $false } },
                       @{ type = "required_status_checks"; parameters = @{ strict_required_status_checks_policy = $false
                          required_status_checks = @(@{ context = "ci-ok" }) } }) } | ConvertTo-Json -Depth 10
        $Ok = $true
        foreach ($Rules in @($Hard, $Flow)) {
            if ((Quiet { $Rules | gh api -X POST "repos/$Repo/rulesets" --input - }) -ne 0) { $Ok = $false }
        }
        if ($Ok) { Write-Host "GitHub now protects main too." }
        else { Write-Host "GitHub did not accept branch rules (private repos need GitHub Pro for that). The local hook protects main on this PC." }
        Write-Host "Connected: https://github.com/$Repo"
    }
    "branch" {
        if (-not $Arg) { throw "Name the branch, e.g. .\scripts\dev.ps1 branch feat/ari-direct-paths" }
        if ($Arg -cnotmatch '^(feat|fix|perf|refactor|docs|test|chore|ci|build)/[a-z0-9][a-z0-9.-]{2,48}$') {
            throw "Name it type/short-topic in lower case, e.g. feat/ari-direct-paths (see CONTRIBUTING.md)"
        }
        Ensure-Hooks
        if (git status --porcelain) { throw "You have uncommitted changes. Commit or stash them first." }
        if (Get-Command gh -ErrorAction SilentlyContinue) {
            $Used = gh pr list --head $Arg --state all --json number 2>$null | ConvertFrom-Json
            if ($Used) { throw "Pull request #$($Used[0].number) already used $Arg. Pick a new name." }
        }
        GitOk switch main
        if ((git remote) -contains "origin") { GitOk pull --ff-only --quiet }
        GitOk switch -c $Arg
        Write-Host "On $Arg. When ready: .\scripts\dev.ps1 pr"
    }
    "apply" {
        if (-not $Arg) { throw "Name the package, e.g. .\scripts\dev.ps1 apply $HOME\Downloads\argus-change.tgz" }
        Need-Venv
        Ensure-Hooks
        & $Py scripts/change.py apply $Arg
        if ($LASTEXITCODE -ne 0) { throw "Not applied (see above)" }
    }
    "pr" {
        Need-Gh
        Need-Venv
        Ensure-Hooks
        $B = Current-Branch
        if ($B -eq "main") { throw "You are on main. Start a branch: .\scripts\dev.ps1 branch feat/name" }
        if (git status --porcelain) { throw "You have uncommitted changes. Commit them first." }
        $Title = @(git log --reverse --format=%s main..HEAD)[0]
        if (-not $Title) { throw "This branch has no commits yet." }
        & $Py scripts/change.py check-title $Title
        if ($LASTEXITCODE -ne 0) { throw "Fix the first commit's title (git commit --amend while it is the only one)." }
        GitOk push -u origin $B
        if ((Quiet { gh pr view $B --json url -q .url }) -ne 0) {
            # The description: what apply kept, else the commit bodies.
            $Kept = Join-Path $Root (".git\argus-change\" + ($B -replace "/", "__") + ".md")
            $BodyFile = Join-Path $env:TEMP "argus-pr-body.md"
            if (Test-Path $Kept) { Copy-Item $Kept $BodyFile -Force }
            else { [IO.File]::WriteAllText($BodyFile, ((git log --reverse --format="%b" main..HEAD) -join "`n")) }
            gh pr create --base main --head $B --title $Title --body-file $BodyFile
            if ($LASTEXITCODE -ne 0) { throw "Could not open the pull request" }
        }
        Write-Host "Pull request: $(gh pr view $B --json url -q .url)"
        Write-Host "When CI is green: .\scripts\dev.ps1 merge"
    }
    "merge" {
        Need-Gh
        $B = Current-Branch
        if ($B -eq "main") { throw "Run this on the pull request's branch." }
        if (git status --porcelain) { throw "You have uncommitted changes. Commit them, run .\scripts\dev.ps1 pr, then merge." }
        # Merge exactly what CI tested: local commits made after "pr" would otherwise be lost with the branch.
        Quiet { git fetch origin $B } | Out-Null
        if ((git rev-parse HEAD).Trim() -ne (git rev-parse "origin/$B").Trim()) {
            throw "This branch has commits GitHub has not seen. Run .\scripts\dev.ps1 pr first, then merge."
        }
        Write-Host "Waiting for CI on $B ..."
        # Right after "pr" GitHub may not have registered the checks yet; wait for them to appear.
        for ($i = 0; $i -lt 12; $i++) {
            $Old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
            $J = gh pr checks $B --json name 2>$null
            $ErrorActionPreference = $Old
            if ($J -and @($J | ConvertFrom-Json).Count -gt 0) { break }
            Start-Sleep -Seconds 5
        }
        gh pr checks $B --watch --fail-fast --interval 10
        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host "CI is not green. Open the failed job's log and read the first error:" -ForegroundColor Yellow
            Write-Host "  - caused by this change: fix it, commit, .\scripts\dev.ps1 pr, then merge again"
            Write-Host "  - a test this change doesn't touch, failing on one runner only: .\scripts\dev.ps1 rerun (once)"
            Write-Host "  - the same test fails twice: it is real, fix it"
            throw "Not merged"
        }
        # One commit on main: the PR title (#number) and its description.
        $Info = gh pr view $B --json number,title,body | ConvertFrom-Json
        $BodyFile = Join-Path $env:TEMP "argus-merge-body.md"
        [IO.File]::WriteAllText($BodyFile, [string]$Info.body)
        gh pr merge $B --squash --delete-branch --subject "$($Info.title) (#$($Info.number))" --body-file $BodyFile
        if ($LASTEXITCODE -ne 0) { throw "Merge failed" }
        GitOk switch main
        GitOk pull --ff-only --quiet
        Write-Host "Merged into main. (Releases happen at milestones: .\scripts\dev.ps1 release minor)"
    }
    "ship" {
        # The whole flow for change packages, one after another: apply -> pr -> merge (CI must be green).
        $Pkgs = @($Arg) + @($More) | Where-Object { $_ }
        if (-not $Pkgs) { throw "Name the package(s), e.g. .\scripts\dev.ps1 ship G:\Projects\argus-ari-p1.tgz G:\Projects\argus-ari-p2.tgz" }
        foreach ($P in $Pkgs) { if (-not (Test-Path $P)) { throw "No such package: $P" } }
        $N = 0
        foreach ($P in $Pkgs) {
            $N++
            Write-Host ""
            Write-Host "== $N/$($Pkgs.Count): $P" -ForegroundColor Cyan
            & $PSCommandPath apply $P
            & $PSCommandPath pr
            & $PSCommandPath merge
        }
        Write-Host ""
        Write-Host "Shipped $($Pkgs.Count) package(s). The PC's worker restarts on the new code by itself, and the laptop" -ForegroundColor Green
        Write-Host "updates itself within ~5 minutes (once auto-update.sh is set up there)."
        Write-Host "For Ari's listener and island now: .\scripts\pc-worker.ps1 restart"
    }
    "rerun" {
        Need-Gh
        $B = Current-Branch
        $Runs = gh run list --branch $B --limit 1 --json databaseId | ConvertFrom-Json
        if (-not $Runs) { throw "No CI run found for $B" }
        gh run rerun $Runs[0].databaseId --failed
        if ($LASTEXITCODE -ne 0) { throw "Could not re-run the failed jobs" }
        Write-Host "Re-running the failed jobs. Then: .\scripts\dev.ps1 merge"
    }
    "sync" {
        Ensure-Hooks
        GitOk switch main
        GitOk pull --ff-only
        GitOk fetch --tags --prune --quiet
        Write-Host "main is up to date."
    }
    "release" {
        if ($Arg -notin @("patch", "minor", "major")) { throw "Usage: .\scripts\dev.ps1 release patch|minor|major" }
        Need-Venv
        Ensure-Hooks
        & $Py scripts/release.py $Arg
        if ($LASTEXITCODE -ne 0) { throw "Release stopped" }
    }
    default {
        Get-Content $PSCommandPath | Select-Object -Skip 1 -First 29 | ForEach-Object { $_.TrimStart("#") }
    }
}
