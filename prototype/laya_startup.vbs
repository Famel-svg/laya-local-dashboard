Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.codex\laya-runtime"
pythonw = root & "\.venv\Scripts\pythonw.exe"
server = root & "\laya_mcp_dashboard.py"
app = "LayaLocal.exe"
If fso.FileExists(pythonw) And fso.FileExists(server) And fso.FileExists(app) Then
  Set env = shell.Environment("PROCESS")
  env("LAYA_TRANSPORT") = "streamable-http"
  env("LAYA_HOST") = "127.0.0.1"
  env("LAYA_PORT") = "8767"
  env("LAYA_DASHBOARD_PORT") = "8766"
  env("LAYA_OPEN_DASHBOARD") = "0"
  env("LAYA_PRELOAD") = "0"
  shell.CurrentDirectory = root
  shell.Run Chr(34) & app & Chr(34) & " --background", 0, False
End If
