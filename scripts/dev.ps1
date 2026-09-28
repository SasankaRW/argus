# Argus dev helper for Windows (PowerShell).
#   .\scripts\dev.ps1 setup   create .venv, install Argus and dev tools, copy example config
#   .\scripts\dev.ps1 test    run tests
#   .\scripts\dev.ps1 lint    run ruff
#   .\scripts\dev.ps1 check   validate argus.yaml and the database
#   .\scripts\dev.ps1 run     start argusd
#   .\scripts\dev.ps1 worker  start a worker (demo plugin) in another window
#   .\scripts\dev.ps1 demo    submit a demo job (needs argusd and a worker running)
#   .\scripts\dev.ps1 events  watch live events in the terminal
#   .\scripts\dev.ps1 helios  rebuild Helios after changing helios/src (needs Node.js; the built copy is in Git)
param([Parameter(Position = 0)][string]$Command = "help")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"

$Stamp = Join-Path $Root ".venv\argus-deps.hash"

function Need-Venv {
    if (-not (Test-Path $Py)) { throw "No .venv yet. Run: .\scripts\dev.ps1 setup" }
    # New version with new dependencies? Update .venv automatically.
    $Hash = (Get-FileHash (Join-Path $Root "pyproject.toml")).Hash
    if (-not (Test-Path $Stamp) -or (Get-Content $Stamp) -ne $Hash) {
        Write-Host "Dependencies changed; updating .venv ..."
        & $Py -m pip install -q -e ".[dev]"
        if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
        Set-Content $Stamp $Hash
    }
}

switch ($Command) {
    "setup" {
        if (-not (Test-Path $Py)) {
            Write-Host "Creating .venv ..."
            py -3 -m venv .venv
        }
        & $Py -m pip install --upgrade pip | Out-Null
        & $Py -m pip install -e ".[dev]"
        Set-Content $Stamp (Get-FileHash (Join-Path $Root "pyproject.toml")).Hash
        if (-not (Test-Path "argus.yaml")) { Copy-Item "argus.example.yaml" "argus.yaml"; Write-Host "Created argus.yaml" }
        if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Created .env (edit the password)" }
        Write-Host "Setup done. Next: .\scripts\dev.ps1 test"
    }
    "test"  { Need-Venv; & $Py -m pytest -q }
    "lint"  { Need-Venv; & $Py -m ruff check core tests }
    "check" { Need-Venv; & $Py -m argus --check }
    "run"   { Need-Venv; & $Py -m argus }
    "worker" { Need-Venv; & $Py -m argus.worker.cli }
    "events" { Need-Venv; & $Py -m argus.tail }
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
    "demo"  {
        Need-Venv
        $Headers = @{}
        if (Test-Path ".env") {
            $Line = Get-Content ".env" | Where-Object { $_ -match "^ARGUS_WORKER_TOKEN=(.+)$" } | Select-Object -First 1
            if ($Line -match "^ARGUS_WORKER_TOKEN=(.+)$") { $Headers["Authorization"] = "Bearer $($Matches[1].Trim())" }
        }
        $Body = '{"plugin":"demo","workflow":"echo","input":{"text":"hello from argus"}}'
        $Job = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8600/jobs" -Headers $Headers -ContentType "application/json" -Body $Body
        Write-Host "Submitted job $($Job.id). Waiting for a worker ..."
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Milliseconds 500
            $J = Invoke-RestMethod -Uri "http://127.0.0.1:8600/jobs/$($Job.id)" -Headers $Headers
            if ($J.state -in @("succeeded", "dead")) { break }
        }
        Write-Host "State: $($J.state)"; $J.result | ConvertTo-Json -Compress
    }
    default {
        Get-Content $PSCommandPath | Select-Object -Skip 1 -First 10 | ForEach-Object { $_.TrimStart("#") }
    }
}
