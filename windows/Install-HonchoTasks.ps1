<#
.SYNOPSIS
    Register the Honcho API and deriver as logon-triggered Scheduled Tasks.

.DESCRIPTION
    Honcho's only clients (agents, editors) are interactive, so the services are
    started at logon rather than at boot. Running at boot would need a machine-
    wide install location and a service principal with stored credentials; see
    WINDOWS.md for that trade-off.

    Both tasks launch through windows\start-honcho-hidden.vbs so no console
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
                              error instead of exiting 0.

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
    [string]$HonchoDir = (Split-Path $PSScriptRoot -Parent),
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

$tasks = @(
    @{ Name = 'Honcho API';     Component = 'api' },
    @{ Name = 'Honcho Deriver'; Component = 'deriver' }
)

if ($Unregister) {
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

foreach ($t in $tasks) {
    $action = New-ScheduledTaskAction `
        -Execute "$env:SystemRoot\System32\wscript.exe" `
        -Argument ('"{0}" {1}' -f $launcher, $t.Component) `
        -WorkingDirectory $HonchoDir

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    if (Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
    }

    Register-ScheduledTask -TaskName $t.Name `
        -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
        -Description 'Honcho self-hosted memory service' | Out-Null

    Write-Host "[OK] registered '$($t.Name)'" -ForegroundColor Green
}

Write-Host "`nStarting..." -ForegroundColor Cyan
foreach ($t in $tasks) { Start-ScheduledTask -TaskName $t.Name; Start-Sleep -Seconds 3 }
Start-Sleep -Seconds 10

Get-ScheduledTask -TaskName 'Honcho*' | ForEach-Object {
    $info = $_ | Get-ScheduledTaskInfo
    '    {0,-16} state={1,-9} result=0x{2:X}' -f $_.TaskName, $_.State, $info.LastTaskResult
} | Write-Host

Write-Host "`nLogs: $(Join-Path $HonchoDir 'logs')" -ForegroundColor Cyan
Write-Host "Stop with: .\Stop-HonchoService.ps1   (Stop-ScheduledTask alone is not enough)" -ForegroundColor Yellow
