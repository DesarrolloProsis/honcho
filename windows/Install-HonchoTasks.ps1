<#
.SYNOPSIS
    Register the Honcho API and deriver as logon-triggered Scheduled Tasks,
    plus a weekly check for new upstream releases.

.DESCRIPTION
    Honcho's only clients (agents, editors) are interactive, so the services are
    started at logon rather than at boot. Running at boot would need a machine-
    wide install location and a service principal with stored credentials; see
    WINDOWS.md for that trade-off.

    "Honcho Upstream Check" runs Update-Honcho.ps1 -Check -Notify every Monday
    and raises a desktop notification when a newer release tag exists. It only
    reports; it never updates. Nothing else tells you a release happened.

    All tasks launch through windows\start-honcho-hidden.vbs so no console
    window appears on the desktop.

.PARAMETER HonchoDir
    Install directory. Defaults to this script's parent, so a clone works
    unedited.

.PARAMETER Unregister
    Remove the tasks instead of creating them.

.NOTES
    Settings below are deliberate, each one load-bearing:

      LogonType Interactive   Required. Registering via `schtasks /RU <user> /NP`
                              silently produces an S4U principal instead, and an
                              S4U task never fires -- it sits at LastTaskResult
                              0x41303 ("has not yet run") forever. Using
                              New-ScheduledTaskPrincipal avoids schtasks and the
                              /NP trap entirely.

      ExecutionTimeLimit 0    Required. The default is 3 days, after which
                              Windows kills a long-running service with no
                              explanation.

      MultipleInstances
        IgnoreNew             Stops Task Scheduler launching a second task
                              instance. NOTE: this does NOT protect against an
                              orphaned python.exe from a previous "stopped"
                              task -- that is not a task instance. Always stop
                              with Stop-HonchoService.ps1, which verifies.

      RestartCount 3          Only fires when the process exits non-zero, which
                              is why the deriver was changed to do so on a fatal
                              error instead of exiting 0. NOT set on the upstream
                              check: it exits 2 to mean "update available", and
                              a restart would repeat the notification.

    Health is read from the RESULT code, not the state alone:
      0x41301 = currently running (healthy)
      0x41303 = has not yet run
      0x41306 = terminated
      Ready + 0x1 = it started and died immediately

.EXAMPLE
    .\Install-HonchoTasks.ps1
    .\Install-HonchoTasks.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$HonchoDir,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
# Defaulted here, not in param(): Windows PowerShell 5.1 leaves $PSScriptRoot
# empty in the parameter defaults of an advanced script.
if (-not $HonchoDir) { $HonchoDir = Split-Path $PSScriptRoot -Parent }

$tasks = @(
    @{ Name = 'Honcho API';            Component = 'api';     Service = $true },
    @{ Name = 'Honcho Deriver';        Component = 'deriver'; Service = $true },
    @{ Name = 'Honcho Upstream Check'; Component = 'check';   Service = $false }
)

function Stop-ExistingService {
    <#
        Stop-ScheduledTask alone orphans the running python processes, which
        would keep running after their task is gone. Use the verified stop, and
        refuse to continue if anything survives.
    #>
    $existing = @($tasks | Where-Object {
        $_.Service -and (Get-ScheduledTask -TaskName $_.Name -ErrorAction SilentlyContinue)
    })
    if ($existing.Count -eq 0) { return }
    & (Join-Path $PSScriptRoot 'Stop-HonchoService.ps1') -HonchoDir $HonchoDir
    if ($LASTEXITCODE -ne 0) { throw "services did not stop cleanly; refusing to re-register" }
}

if ($Unregister) {
    Stop-ExistingService
    foreach ($t in $tasks) {
        if (Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
            Write-Host "[OK] removed '$($t.Name)'" -ForegroundColor Yellow
        }
    }
    exit 0
}

$launcher = Join-Path $PSScriptRoot 'start-honcho-hidden.vbs'
if (-not (Test-Path $launcher)) { throw "launcher not found: $launcher" }
if (-not (Test-Path (Join-Path $HonchoDir '.venv\Scripts\python.exe'))) {
    throw "no virtualenv at $HonchoDir. Run 'uv sync' first."
}
New-Item -ItemType Directory -Force -Path (Join-Path $HonchoDir 'logs') | Out-Null

Write-Host "[*] install dir: $HonchoDir" -ForegroundColor Cyan

# Existing tasks are updated in place with -Force rather than unregistered and
# re-created. Changing a definition does not touch a running instance, so the
# services need no stop here: no downtime, and no orphaned deriver (see
# Stop-HonchoService.ps1). The new definition applies from the next start.
foreach ($t in $tasks) {
    $action = New-ScheduledTaskAction `
        -Execute "$env:SystemRoot\System32\wscript.exe" `
        -Argument ('"{0}" {1}' -f $launcher, $t.Component) `
        -WorkingDirectory $HonchoDir

    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited

    if ($t.Service) {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
    } else {
        # StartWhenAvailable: a Monday spent logged off runs at the next logon.
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At '10:00'
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    }

    $description = if ($t.Service) { 'Honcho self-hosted memory service' }
                   else { 'Weekly check for a newer Honcho release tag; reports only' }
    $existed = [bool](Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue)

    try {
        Register-ScheduledTask -TaskName $t.Name -Force `
            -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
            -Description $description -ErrorAction Stop | Out-Null
        Write-Host "[OK] $(if ($existed) { 'updated' } else { 'registered' }) '$($t.Name)'" -ForegroundColor Green
    } catch {
        # A task registered from an elevated shell can be started and stopped by
        # its user but not modified. Keep it rather than fail the whole install.
        if (-not $existed) { throw }
        Write-Host "[!] kept existing '$($t.Name)': $($_.Exception.Message.Trim())" -ForegroundColor Yellow
        Write-Host "    It was probably registered elevated. Re-run this script elevated to update it." -ForegroundColor Yellow
    }
}

Write-Host "`nStarting..." -ForegroundColor Cyan
foreach ($t in $tasks | Where-Object { $_.Service }) {
    # Starting a task that is already running is refused under IgnoreNew and
    # overwrites LastTaskResult with 0x800710E0, hiding the 0x41301 health code.
    if ((Get-ScheduledTask -TaskName $t.Name).State -eq 'Running') {
        Write-Host "    '$($t.Name)' already running"
        continue
    }
    Start-ScheduledTask -TaskName $t.Name
    Start-Sleep -Seconds 3
}
Start-Sleep -Seconds 10

Get-ScheduledTask -TaskName 'Honcho*' | ForEach-Object {
    $info = $_ | Get-ScheduledTaskInfo
    '    {0,-22} state={1,-9} result=0x{2:X}' -f $_.TaskName, $_.State, $info.LastTaskResult
} | Write-Host

Write-Host "`nLogs: $(Join-Path $HonchoDir 'logs')" -ForegroundColor Cyan
Write-Host "Stop with: .\Stop-HonchoService.ps1   (Stop-ScheduledTask alone is not enough)" -ForegroundColor Yellow
