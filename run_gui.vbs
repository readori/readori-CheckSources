Option Explicit

Dim shell, fso, root, script, command, i, arg
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
script = root & "\run_gui.ps1"
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & script & """"
For i = 0 To WScript.Arguments.Count - 1
    arg = WScript.Arguments(i)
    command = command & " """ & Replace(arg, """"", """"""") & """"
Next
shell.Run command, 0, False
