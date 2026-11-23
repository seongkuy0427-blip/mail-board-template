' CHUNIL Smart Mail Portal - start the server in the background (no black window).
' Double-click this file instead of start_portal.bat when you do not want a console window.
' Logs go to data\server.log. To stop the server, run stop_portal.bat (or log off).
'
' First run only: if Python packages are missing, run start_portal.bat once.
Option Explicit

Dim fso, sh, here, py, port, cmd, ok
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
port = "8080"

If Not fso.FileExists(here & "\server.py") Then
  MsgBox "server.py was not found in this folder:" & vbCrLf & here, vbCritical, "CHUNIL Smart Mail Portal"
  WScript.Quit 1
End If

If PortBusy(port) Then
  MsgBox "The portal is already running." & vbCrLf & vbCrLf & _
         "Local:  http://127.0.0.1:" & port & vbCrLf & _
         "Log:    data\server.log", vbInformation, "CHUNIL Smart Mail Portal"
  WScript.Quit 0
End If

If Not fso.FolderExists(here & "\data") Then fso.CreateFolder(here & "\data")

' Prefer pythonw.exe (no console window); fall back to python.exe (still hidden by window mode 0).
py = FindExe("pythonw.exe")
If py = "" Then py = FindExe("python.exe")
If py = "" Then
  MsgBox "Python was not found." & vbCrLf & vbCrLf & _
         "Install Python 3.10 or later and check 'Add python.exe to PATH'," & vbCrLf & _
         "then run start_portal.bat once.", vbCritical, "CHUNIL Smart Mail Portal"
  WScript.Quit 1
End If

cmd = """" & py & """ """ & here & "\server.py"" --port " & port & _
      " --log-file """ & here & "\data\server.log"""
sh.Run cmd, 0, False

' Wait up to ~12 seconds for the port to answer, then tell the user where it is.
ok = False
Dim i
For i = 1 To 24
  WScript.Sleep 500
  If PortBusy(port) Then
    ok = True
    Exit For
  End If
Next

If ok Then
  sh.Popup "Portal is running in the background." & vbCrLf & vbCrLf & _
           "Local:  http://127.0.0.1:" & port & vbCrLf & _
           "Team address and log: data\server.log", 6, "CHUNIL Smart Mail Portal", 64
Else
  MsgBox "The server did not start within 12 seconds." & vbCrLf & vbCrLf & _
         "Check data\server.log for the reason (port in use, missing packages...)." & vbCrLf & _
         "You can also run start_portal.bat once to see the message in a window.", _
         vbExclamation, "CHUNIL Smart Mail Portal"
End If
WScript.Quit 0


' ---------------------------------------------------------------------------
' True if something is already listening on the port.
Function PortBusy(p)
  Dim ex, line
  PortBusy = False
  On Error Resume Next
  Set ex = sh.Exec("cmd /c netstat -ano -p tcp")
  Do While Not ex.StdOut.AtEndOfStream
    line = ex.StdOut.ReadLine()
    If InStr(line, ":" & p & " ") > 0 And InStr(line, "LISTENING") > 0 Then
      PortBusy = True
      Exit Do
    End If
  Loop
  On Error GoTo 0
End Function

' Full path of an executable found on PATH ("" when not found).
Function FindExe(name)
  Dim ex, line, tmp
  FindExe = ""
  On Error Resume Next
  Set ex = sh.Exec("cmd /c where " & name)
  Do While Not ex.StdOut.AtEndOfStream
    line = Trim(ex.StdOut.ReadLine())
    If line <> "" Then
      If fso.FileExists(line) Then FindExe = line
      Exit Do
    End If
  Loop
  On Error GoTo 0
End Function
