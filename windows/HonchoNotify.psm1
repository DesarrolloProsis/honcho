<#
    Desktop notifications for the Honcho scheduled tasks.

    Uses the WinRT toast API, which Windows PowerShell 5.1 can load and
    PowerShell 7 cannot; under 7 the notice is written to the output only.
    Never throws: a notification that cannot be shown must not fail the
    check that wanted to raise it. Send-HonchoRemoteNotice forwards the same
    notice to a Hermes messaging target, for when nobody is at the desk.
#>

function Send-HonchoNotice {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][string]$Text
    )
    try {
        $null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
        $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
            [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $lines = $xml.GetElementsByTagName('text')
        $null = $lines.Item(0).AppendChild($xml.CreateTextNode($Title))
        $null = $lines.Item(1).AppendChild($xml.CreateTextNode($Text))
        # powershell.exe's own AppUserModelID, so no app registration is needed.
        $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
            [Windows.UI.Notifications.ToastNotification]::new($xml))
        Write-Host "[notify] $Title - $Text"
    } catch {
        Write-Host "[!] could not raise a notification ($($_.Exception.Message)): $Title - $Text"
    }
}

function Send-HonchoRemoteNotice {
    <#
        The same notice through `hermes send --to <Target>`, which reuses the
        Hermes gateway's platform credentials with no LLM involved. The body
        goes through a temp file so no alert text can break the command line's
        quoting. Waits at most 60 seconds, and never throws: a messaging outage
        must not fail the check, and the toast has already been raised.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Target,
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][string]$Text
    )
    $body = $null
    try {
        $hermes = (Get-Command hermes -ErrorAction SilentlyContinue).Source
        if (-not $hermes) { $hermes = Join-Path $env:LOCALAPPDATA 'hermes\bin\hermes.exe' }
        if (-not (Test-Path $hermes)) { throw "hermes not found" }
        $body = [IO.Path]::GetTempFileName()
        [IO.File]::WriteAllText($body, $Text, (New-Object Text.UTF8Encoding $false))
        $argLine = "send --to `"$Target`" --quiet --subject `"$($Title -replace '"', "'")`" --file `"$body`""
        $p = Start-Process -FilePath $hermes -ArgumentList $argLine -WindowStyle Hidden -PassThru
        if (-not $p.WaitForExit(60000)) {
            $p.Kill()
            throw "hermes send timed out after 60 s"
        }
        if ($p.ExitCode -ne 0) { throw "hermes send exited $($p.ExitCode)" }
        Write-Host "[notify:$Target] $Title - $Text"
    } catch {
        Write-Host "[!] could not send to ${Target} ($($_.Exception.Message)): $Title - $Text"
    } finally {
        if ($body) { Remove-Item $body -ErrorAction SilentlyContinue }
    }
}

Export-ModuleMember -Function Send-HonchoNotice, Send-HonchoRemoteNotice
