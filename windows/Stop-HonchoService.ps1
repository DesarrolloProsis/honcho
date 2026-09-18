<#
.SYNOPSIS
    Stop a Honcho scheduled task and every process it left behind.

.DESCRIPTION
    `Stop-ScheduledTask` is not sufficient on its own. Measured on Windows 11:
    it terminates only the task's direct child (cmd.exe or wscript.exe) and
    reports the task as `Ready` with LastTaskResult 0x41306, while the
    uv.exe -> python.exe grandchildren keep running. The service appears stopped
    and is not.

    That matters for updates: starting the task again after an apparent stop
    leaves TWO derivers polling the same queue.

    This script:
      1. attempts a graceful shutdown (CTRL_BREAK_EVENT, which the deriver
         handles as SIGBREAK),
      2. stops the scheduled task,
      3. force-kills anything still alive,
      4. VERIFIES nothing remains, and fails loudly if it does.

    MEASURED CAVEAT on step 1: GenerateConsoleCtrlEvent requires the target PID
    to be a process GROUP LEADER. A service-launched deriver is not -- the chain
    is wscript -> cmd -> python, and python inherits cmd's group. So for the
    normal service case the CTRL_BREAK is a no-op and step 3 does the work.
    It succeeds only when the process was started directly from a console, or
    spawned with CREATE_NEW_PROCESS_GROUP.

    Force-killing a deriver is survivable: queue items it had claimed are
    reclaimed after their lease expires. The property that matters here is that
    the stop is COMPLETE and VERIFIED, not that it is graceful. Do not read a
    successful run as "it shut down cleanly".

.PARAMETER TaskName
    Scheduled task to stop. Defaults to both Honcho tasks.

.PARAMETER HonchoDir
    Install directory whose processes should be stopped. Defaults to the parent
    of this script, so a clone works without editing anything.

.PARAMETER GracePeriodSeconds
    How long to wait for a graceful exit before force-killing.

.EXAMPLE
    .\Stop-HonchoService.ps1
    .\Stop-HonchoService.ps1 -TaskName 'Honcho Deriver' -GracePeriodSeconds 30
#>
[CmdletBinding()]
param(
    [string[]]$TaskName = @('Honcho API', 'Honcho Deriver'),
    [string]$HonchoDir = (Split-Path $PSScriptRoot -Parent),
    [int]$GracePeriodSeconds = 15
)

$ErrorActionPreference = 'Stop'

function Get-HonchoProcess {
    <#
        Match on the install directory, never on a bare command-line substring.
        A pattern like "*src.deriver*" also matches the PowerShell process
        running the query, and killing that takes down the caller's own shell.
        Restricting by Name AND by install path avoids both that and the risk of
        killing an unrelated Python process.
    #>
    param([string]$Dir)

    $needle = [regex]::Escape($Dir)
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^(python|uv|pythonw)\.exe$' -and
            $_.CommandLine -and
            $_.CommandLine -match $needle
        }
}

function Send-CtrlBreak {
    <# Ask a process group to shut down gracefully. Best effort by design. #>
    param([int]$ProcessId)

    $signature = @'
[DllImport("kernel32.dll", SetLastError=true)]
public static extern bool GenerateConsoleCtrlEvent(uint dwCtrlEvent, uint dwProcessGroupId);
'@
    try {
        $k32 = Add-Type -MemberDefinition $signature -Name 'HonchoCtrl' -Namespace 'Win32' -PassThru -ErrorAction Stop
        # 1 = CTRL_BREAK_EVENT. CTRL_C_EVENT cannot be targeted at another group.
        return $k32::GenerateConsoleCtrlEvent(1, $ProcessId)
    } catch {
        return $false
    }
}

Write-Host "[*] install dir: $HonchoDir" -ForegroundColor Cyan

$before = @(Get-HonchoProcess -Dir $HonchoDir)
Write-Host "[*] $($before.Count) Honcho process(es) running before stop"

# --- 1. graceful first -------------------------------------------------------
foreach ($p in $before) {
    if (Send-CtrlBreak -ProcessId $p.ProcessId) {
        Write-Host "    sent CTRL_BREAK to PID $($p.ProcessId)"
    }
}
if ($before.Count -gt 0) { Start-Sleep -Seconds $GracePeriodSeconds }

# --- 2. stop the task itself -------------------------------------------------
foreach ($t in $TaskName) {
    $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "[!] task not found: $t" -ForegroundColor Yellow
        continue
    }
    if ($task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $t
        Write-Host "    stopped task '$t'"
    } else {
        Write-Host "    task '$t' was already $($task.State)"
    }
}
Start-Sleep -Seconds 3

# --- 3. force-kill survivors -------------------------------------------------
$survivors = @(Get-HonchoProcess -Dir $HonchoDir)
foreach ($p in $survivors) {
    Write-Host "    force-killing orphaned $($p.Name) PID $($p.ProcessId)" -ForegroundColor Yellow
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($survivors.Count -gt 0) { Start-Sleep -Seconds 3 }

# --- 4. verify, and fail loudly ---------------------------------------------
$remaining = @(Get-HonchoProcess -Dir $HonchoDir)
if ($remaining.Count -gt 0) {
    Write-Host "[X] STILL RUNNING after stop: $($remaining.ProcessId -join ', ')" -ForegroundColor Red
    Write-Host "    Do NOT start the service again until these are gone -- a second" -ForegroundColor Red
    Write-Host "    deriver would poll the same queue concurrently." -ForegroundColor Red
    exit 1
}

Write-Host "[OK] all Honcho processes stopped and verified gone." -ForegroundColor Green
exit 0
