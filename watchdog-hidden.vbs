' Long-lived silent watchdog (pythonw, no console).
' Used by logon task GrokRemoteHubWatch.
Option Explicit
Dim sh, fso, root, pythonw, venvPyw, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
venvPyw = root & "\.venv\Scripts\pythonw.exe"
If fso.FileExists(venvPyw) Then
  pythonw = venvPyw
Else
  ' PATH fallback when venv pythonw is missing
  pythonw = "pythonw.exe"
End If
cmd = """" & pythonw & """ -m hub.watchdog"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root
' 0 = hide window, False = do not wait
On Error Resume Next
sh.Run cmd, 0, False
If Err.Number <> 0 Then
  WScript.Quit 1
End If
