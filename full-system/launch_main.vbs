Dim shell
Set shell = CreateObject("WScript.Shell")
Dim cmd
cmd = "powershell.exe -ExecutionPolicy Bypass -NoProfile -File ""C:\Users\Administrator\Desktop\algo-trading\full-system\run_main.ps1"""
shell.Run "cmd.exe /k " & cmd, 1, False
Set shell = Nothing