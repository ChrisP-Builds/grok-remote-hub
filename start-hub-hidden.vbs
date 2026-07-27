' Silent launcher for start-hub.ps1 (no console flash) for logon task.
Option Explicit
Dim sh, fso, root, ps1, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = root & "\start-hub.ps1"
If Not fso.FileExists(ps1) Then
  WScript.Quit 1
End If
cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & ps1 & """"
Set sh = CreateObject("WScript.Shell")
sh.Run cmd, 0, False
