<#
.SYNOPSIS
    Rebase the Windows patch series onto a newer upstream release tag.

.DESCRIPTION
    Fetches upstream, reports what is new, and (unless -Check) stops the
    services, rebases onto the latest release TAG, re-syncs dependencies, runs
    migrations, restarts and verifies.

    Targets a tag, never upstream/main: main is a rolling integration branch,
    so rebasing onto it puts the patch series on top of code upstream has not
    released.

.PARAMETER HonchoDir
    Install directory. Defaults to this script's parent.

.PARAMETER Check
    Report only. Makes no changes and touches no services.

.PARAMETER Tag
    Rebase onto this tag instead of the newest one.

.NOTES
    Fail-closed by design. In PowerShell, $ErrorActionPreference = 'Stop' does
    NOT make a non-zero exit from a native command (git, uv, alembic) a
    terminating error, so every one is checked explicitly via $LASTEXITCODE.
    A half-finished update that reported success would be worse than a loud
    failure, because the services are stopped by then.

.EXAMPLE
    .\Update-Honcho.ps1 -Check
    .\Update-Honcho.ps1
#>
[CmdletBinding()]
param(
    [string]$HonchoDir = (Split-Path $PSScriptRoot -Parent),
    [switch]$Check,
    [string]$Tag
)

$ErrorActionPreference = 'Stop'
Set-Location $HonchoDir

function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m"  -ForegroundColor Yellow }
function Die($m)  { Write-Host "[X] $m"  -ForegroundColor Red; exit 1 }

function Invoke-Checked {
    <# Run a native command and abort unless it exits 0. #>
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) { Die "$What failed (exit $LASTEXITCODE)" }
}

# --- 0. remotes, identified by URL and not by name ---------------------------
# Remote NAMES are not reliable: a clone of the fork and the fork's own live
# checkout have used `origin` for opposite repositories. Resolve by URL so the
# script cannot rebase onto the wrong thing.
$remotes = @{}
foreach ($line in (git remote -v | Where-Object { $_ -match '\(fetch\)$' })) {
    $parts = $line -split '\s+'
    $remotes[$parts[0]] = $parts[1]
}
$upstreamRemote = ($remotes.GetEnumerator() |
    Where-Object { $_.Value -match 'plastic-labs/honcho' } |
    Select-Object -First 1).Key

if (-not $upstreamRemote) {
    Warn "no remote points at plastic-labs/honcho; adding 'upstream'"
    if (-not $Check) {
        Invoke-Checked 'git remote add' { git remote add upstream https://github.com/plastic-labs/honcho.git }
        $upstreamRemote = 'upstream'
    } else {
        Die "cannot check for updates without an upstream remote"
    }
}
Info "upstream remote: $upstreamRemote -> $($remotes[$upstreamRemote])"

# --- 1. clean tree required --------------------------------------------------
$dirty = git status --porcelain --untracked-files=no
if ($dirty) {
    Write-Host $dirty
    Die "tracked files have uncommitted changes. Commit or stash them first."
}

Invoke-Checked 'git fetch' { git fetch $upstreamRemote --tags --quiet }

# --- 2. what's new -----------------------------------------------------------
$base = git describe --tags --abbrev=0 (git merge-base HEAD "$upstreamRemote/main") 2>$null
if (-not $Tag) {
    $Tag = git tag -l 'v*' --sort=-v:refname | Select-Object -First 1
}
Info "current base : $base"
Info "target tag   : $Tag"

if ($base -eq $Tag) { Ok "already on the newest release tag."; exit 0 }

Info "commits between:"
git log --oneline "$base..$Tag" | Select-Object -First 15 | Write-Host

# --- 3. things that make an update more than a rebase ------------------------
$migrations = git diff --name-only "$base..$Tag" -- migrations
if ($migrations) {
    Warn "this release changes migrations:"
    $migrations | ForEach-Object { Write-Host "      $_" }
    Warn "BACK UP THE DATABASE before continuing. A rollback restores code, not schema."
}

$pyBefore = git show "${base}:.python-version" 2>$null
$pyAfter  = git show "${Tag}:.python-version" 2>$null
if ($pyBefore -ne $pyAfter) {
    Warn "Python floor moves $pyBefore -> $pyAfter. This is a runtime migration:"
    Warn "  uv python install $pyAfter; remove .venv; uv sync"
}

# --- 4. has upstream fixed any blocker natively? -----------------------------
Info "checking whether upstream now handles Windows itself..."
$checks = @(
    @{ Label = 'bare `import fcntl`';         File = 'src/telemetry/reasoning_traces.py'; Pattern = '^\s*import fcntl\s*$' },
    @{ Label = 'unguarded add_signal_handler'; File = 'src/deriver/queue_manager.py';      Pattern = 'add_signal_handler' },
    @{ Label = 'unconditional uvloop import';  File = 'src/deriver/__main__.py';           Pattern = '^\s*import uvloop\s*$' }
)
foreach ($c in $checks) {
    $content = git show "$($Tag):$($c.File)" 2>$null
    if ($content -match $c.Pattern) {
        Write-Host "      - $($c.Label): still present (our patch is still needed)"
    } else {
        Ok "      - $($c.Label): GONE upstream - our patch may be droppable"
    }
}

if ($Check) { Info "check-only mode, stopping here."; exit 0 }

# --- 5. safety net -----------------------------------------------------------
$backupTag = "pre-update-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss')
Invoke-Checked 'git tag' { git tag $backupTag }
Ok "tagged current state as '$backupTag' (roll back: git reset --hard $backupTag)"

# --- 6. stop services, and VERIFY they stopped -------------------------------
# Stop-ScheduledTask alone leaves the python grandchildren running; restarting
# afterwards would put two derivers on the same queue.
& (Join-Path $PSScriptRoot 'Stop-HonchoService.ps1') -HonchoDir $HonchoDir
if ($LASTEXITCODE -ne 0) { Die "services did not stop cleanly; refusing to continue" }

# --- 7. rebase ---------------------------------------------------------------
Info "rebasing onto $Tag..."
git rebase $Tag
if ($LASTEXITCODE -ne 0) {
    Warn "REBASE CONFLICT - upstream changed lines this series patches."
    Write-Host "    Resolve, then: git add <file>; git rebase --continue"
    Write-Host "    Or abandon:    git rebase --abort"
    Write-Host "    Services are STOPPED. Restart them once resolved."
    exit 1
}
Ok "rebase clean"

Invoke-Checked 'uv sync'  { uv sync --all-extras --dev }
Invoke-Checked 'migrations' { uv run alembic upgrade head }
Ok "dependencies and schema up to date"

# --- 8. verify before restarting --------------------------------------------
Info "verifying the Windows patches still hold..."
Invoke-Checked 'trace lock harness' { uv run python trace_lock_concurrency.py }
Ok "trace lock verified under contention"

# --- 9. restart and check ----------------------------------------------------
foreach ($t in 'Honcho API', 'Honcho Deriver') {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName $t
        Start-Sleep -Seconds 3
    }
}
Start-Sleep -Seconds 15

try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/v3/workspaces/list' -Method POST `
        -Body '{}' -ContentType 'application/json' -TimeoutSec 25 -UseBasicParsing
    if ($r.StatusCode -eq 200) { Ok "API healthy (HTTP 200)" }
} catch {
    Die "API did not come back up. Check logs\api.log. Roll back: git reset --hard $backupTag"
}

Get-ScheduledTask -TaskName 'Honcho*' | ForEach-Object {
    $i = $_ | Get-ScheduledTaskInfo
    '    {0,-16} state={1,-9} result=0x{2:X}' -f $_.TaskName, $_.State, $i.LastTaskResult
} | Write-Host

Write-Host ""
Ok "update complete. Rollback tag if needed: $backupTag"
Warn "The rollback tag restores CODE ONLY. Migrations are not reversed."
