' ====================================================
'  音乐搜索下载工具 - VBS 启动器
'  双击本文件启动程序，不会弹出黑色控制台窗口
'  需与 music_downloader.py 放在同一目录
' ====================================================
Option Explicit

Dim fso, shell, baseDir, pyPath, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' 以本启动器所在目录定位程序文件
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
pyPath = baseDir & "\music_downloader.py"

If Not fso.FileExists(pyPath) Then
    MsgBox "找不到程序文件：" & pyPath & vbCrLf & vbCrLf & "请将本启动器放在 music_downloader.py 同目录。", vbCritical, "音乐下载工具"
    WScript.Quit
End If

' 依次尝试：pythonw.exe -> python.exe -> py.exe
' 均以 3 参数形式启动（无窗口、不等待）
On Error Resume Next

cmd = "pythonw.exe " & Chr(34) & pyPath & Chr(34)
shell.Run cmd, 0, False
If Err.Number = 0 Then WScript.Quit

Err.Clear
cmd = "python.exe " & Chr(34) & pyPath & Chr(34)
shell.Run cmd, 0, False
If Err.Number = 0 Then WScript.Quit

Err.Clear
cmd = "py.exe " & Chr(34) & pyPath & Chr(34)
shell.Run cmd, 0, False
If Err.Number <> 0 Then
    MsgBox "启动失败：" & Err.Description & vbCrLf & vbCrLf & "请确认已安装 Python（python.org 安装包），并勾选 Add Python to PATH。", vbCritical, "音乐下载工具"
End If

On Error Goto 0
