' Launch a Honcho component with no visible console window.
'
' Scheduled Tasks run under an interactive principal, so a console application
' flashes or keeps a window open on the user's desktop. wscript.exe has no
' console, so launching through this script hides it.
'
' Usage (from a Scheduled Task action):
'     wscript.exe "<install>\windows\start-honcho-hidden.vbs" api
'     wscript.exe "<install>\windows\start-honcho-hidden.vbs" deriver
'     wscript.exe "<install>\windows\start-honcho-hidden.vbs" check
'
' "check" runs windows\Update-Honcho.ps1 -Check -Notify under Windows
' PowerShell 5.1, which is always present and is the host that can raise a
' desktop notification. Its exit code (2 = newer release tag) is passed through.
'
' Paths are derived from this script's own location, so the install directory
' can be anywhere and nothing here needs editing. Do NOT hardcode a profile
' path: a non-ASCII character in a username does not survive Task Scheduler's
' argument encoding, and the task then fails instantly with LastTaskResult 0x1
' and no log output at all.
'
' ENCODING: save this file as ANSI/ASCII, never UTF-8. wscript parses .vbs as
' ANSI, so a UTF-8 file containing any non-ASCII byte is silently corrupted --
' paths break, the script exits 0, and nothing is ever launched. Keeping this
' file pure ASCII sidesteps the problem entirely.
'
' The venv interpreter is invoked directly rather than through `uv run`. That
' keeps the process chain short (cmd -> python instead of cmd -> uv -> python),
' which matters because Task Scheduler only terminates its direct child --
' see windows\Stop-HonchoService.ps1.

Option Explicit

Dim shell, fso, component, scriptDir, honchoDir, pythonExe, target, logFile, command, exitCode, q
Dim psExe
q = Chr(34)
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

If WScript.Arguments.Count <> 1 Then WScript.Quit 2
component = LCase(WScript.Arguments(0))

' windows\start-honcho-hidden.vbs -> the install root is two levels up.
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
honchoDir = fso.GetParentFolderName(scriptDir)

pythonExe = fso.BuildPath(honchoDir, ".venv\Scripts\python.exe")

If component = "api" Then
    target = q & fso.BuildPath(honchoDir, "windows\run_api.py") & q
ElseIf component = "deriver" Then
    target = "-m src.deriver"
ElseIf component = "check" Then
    psExe = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
    target = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File " & _
             q & fso.BuildPath(scriptDir, "Update-Honcho.ps1") & q & " -Check -Notify"
Else
    WScript.Quit 2
End If

If component = "check" Then
    logFile = fso.BuildPath(honchoDir, "logs\upstream-check.log")
Else
    logFile = fso.BuildPath(honchoDir, "logs\" & component & ".log")
    If Not fso.FileExists(pythonExe) Then WScript.Quit 3
End If

If Not fso.FolderExists(fso.BuildPath(honchoDir, "logs")) Then
    fso.CreateFolder fso.BuildPath(honchoDir, "logs")
End If

shell.CurrentDirectory = honchoDir

' Must go through cmd.exe: WshShell.Run uses CreateProcess semantics and does
' NOT interpret ">>" or "2>&1", so without this the redirection operators are
' passed to python as extra argv entries and all output is silently discarded.
' The doubled quote after /c is the standard cmd form: cmd /c ""exe" args".
' -u keeps stdout unbuffered so the log stays current and survives a kill.
' 2>&1 is essential, not optional: logging.basicConfig writes to stderr, so
' redirecting stdout alone still discards every log line.
If component = "check" Then
    command = "cmd.exe /c " & q & q & psExe & q & " " & target & _
              " >> " & q & logFile & q & " 2>&1" & q
Else
    command = "cmd.exe /c " & q & q & pythonExe & q & " -u " & target & _
              " >> " & q & logFile & q & " 2>&1" & q
End If

exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
