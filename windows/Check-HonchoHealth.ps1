<#
.SYNOPSIS
    Run the Honcho health check and turn its findings into notifications.

.DESCRIPTION
    Runs windows\health_check.py with the install's own virtualenv and settings,
    and raises a desktop notification for each state change it reports: a check
    going into alert, a daily reminder while it stays there, and its recovery.
    See health_check.py for the checks themselves.

    Registered by Install-HonchoTasks.ps1 as "Honcho Health Check", every 15
    minutes, launched hidden through start-honcho-hidden.vbs under Windows
    PowerShell 5.1 (the host that can raise a toast). Output is appended to
    logs\health-check.log.

    A toast only reaches someone at the desk. When HEALTH_NOTIFY_HERMES names a
    `hermes send --to` target (environment, or the install's .env), every
    notice is also sent there, including "the check could not run". Same
    notices, same cadence: state changes and a daily reminder, never every run.

.PARAMETER HonchoDir
    Install directory. Defaults to this script's parent.

.PARAMETER Notify
    Raise desktop notifications. Without it, findings are only printed.

.PARAMETER ForceProbe
    Probe every configured model endpoint now instead of waiting for the hourly
    probe.

.OUTPUTS
    Exit 0 when the check ran (whatever it found), 1 when the check itself
    could not run, which is also notified, so a broken monitor is not silent.
#>
[CmdletBinding()]
param(
    [string]$HonchoDir,
    [switch]$Notify,
    [switch]$ForceProbe
)

$ErrorActionPreference = 'Stop'
# Defaulted here, not in param(): Windows PowerShell 5.1 leaves $PSScriptRoot
# empty in the parameter defaults of an advanced script.
if (-not $HonchoDir) { $HonchoDir = Split-Path $PSScriptRoot -Parent }
Import-Module (Join-Path $PSScriptRoot 'HonchoNotify.psm1') -Force

function Get-HealthSetting($name) {
    $value = [Environment]::GetEnvironmentVariable($name)
    $envFile = Join-Path $HonchoDir '.env'
    if (-not $value -and (Test-Path $envFile)) {
        foreach ($line in Get-Content $envFile) {
            $key, $raw = $line.Trim() -split '=', 2
            if ($null -ne $raw -and $key.Trim() -eq $name) { $value = $raw.Trim().Trim('"').Trim("'") }
        }
    }
    return $value
}

$hermesTarget = Get-HealthSetting 'HEALTH_NOTIFY_HERMES'

function Notice($title, $text) {
    if (-not $Notify) { Write-Host "[notice] $title - $text"; return }
    Send-HonchoNotice -Title $title -Text $text
    if ($hermesTarget) { Send-HonchoRemoteNotice -Target $hermesTarget -Title $title -Text $text }
}

Write-Host ("[*] health check at {0:yyyy-MM-dd HH:mm:ss zzz}" -f (Get-Date))
$python = Join-Path $HonchoDir '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Notice 'Honcho health check failed' "No virtualenv at $HonchoDir."
    exit 1
}

$pyArgs = @((Join-Path $HonchoDir 'windows\health_check.py'))
if ($ForceProbe) { $pyArgs += '--force-probe' }

Push-Location $HonchoDir
try {
    $env:PYTHONIOENCODING = 'utf-8'
    # Windows PowerShell 5.1 turns native stderr under 2>&1 into error records,
    # which 'Stop' would make terminating: a Python traceback would then kill
    # this wrapper before it could report the failure.
    $ErrorActionPreference = 'Continue'
    $output = & $python @pyArgs 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
} finally {
    Pop-Location
}

$json = $null
foreach ($line in $output) {
    if ($line.StartsWith('HEALTH_JSON ')) { $json = $line.Substring(12) | ConvertFrom-Json }
    elseif ($line -notmatch '^\[OK\] ') { Write-Host $line }
}
if ($code -ne 0 -or -not $json) {
    Notice 'Honcho health check failed' 'The check itself could not run. See logs\health-check.log.'
    exit 1
}

$notes = @($json.notifications)
$okCount = @($output | Where-Object { $_ -match '^\[OK\] ' }).Count
Write-Host "[*] $okCount ok, $($json.alerts) in alert, probed=$($json.probed), $($notes.Count) notification(s)"

$alerts = @($notes | Where-Object { $_.kind -ne 'recovered' })
$recovered = @($notes | Where-Object { $_.kind -eq 'recovered' })
if ($alerts.Count -eq 1) {
    $a = $alerts[0]
    $prefix = if ($a.kind -eq 'reminder') { 'Still failing' } else { 'Honcho alert' }
    Notice "${prefix}: $($a.id)" $a.text
} elseif ($alerts.Count -gt 1) {
    Notice "Honcho: $($alerts.Count) health alerts" (($alerts | ForEach-Object { $_.id }) -join ', ')
}
if ($recovered.Count -gt 0) {
    Notice 'Honcho recovered' (($recovered | ForEach-Object { $_.id }) -join ', ')
}
exit 0
