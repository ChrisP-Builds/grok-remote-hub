' Silent launcher for watch-hub.ps1 (no console flash).
' Used by Task Scheduler task GrokRemoteHubWatch.
' WindowStyle 0 = completely hidden (more reliable than powershell -WindowStyle Hidden).
Option Explicit
Dim sh, fso, root, ps1, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = root & "\watch-hub.ps1"
If Not fso.FileExists(ps1) Then
  WScript.Quit 1
End If
cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & ps1 & """ -Quiet"
Set sh = CreateObject("WScript.Shell")
' 0 = hide window, False = do not wait
sh.Run cmd, 0, False
