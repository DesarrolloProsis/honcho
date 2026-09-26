<#
    Desktop notifications for the Honcho scheduled tasks.

    Uses the WinRT toast API, which Windows PowerShell 5.1 can load and
    PowerShell 7 cannot; under 7 the notice is written to the output only.
    Never throws: a notification that cannot be shown must not fail the
    check that wanted to raise it.
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

Export-ModuleMember -Function Send-HonchoNotice
