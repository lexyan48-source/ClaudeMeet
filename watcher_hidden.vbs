Dim d: d = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run """" & d & "\run_meet_recorder.bat""", 0, False
