@echo off
rem One-click start for Windows: double-click this file. See README.
rem Python 3.10+ with tkinter (python3, then py -3, then python) -> your name ->
rem bc.py server --games 0 (you + 2 AI players, only 127.0.0.1, keeps hosting for the GUI replay button) in the background ->
rem wait until the port accepts -> bc.py gui -> when the window closes, stop the server.
rem Nothing before "chcp 65001" may contain non-ASCII text.
cd /d "%~dp0"
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PORT="
set "TEAM=G00"
set "SERVER_LOG=logs\launcher-server.txt"
title 幾A幾B 一鍵開始

rem ---- 1. Python 3.10+ with tkinter ----
set "PY="
for %%C in ("python3" "py -3" "python") do (
    if not defined PY (
        %%~C -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=%%~C"
    )
)
if not defined PY (
    echo 找不到含 tkinter 的 Python 3.10 以上版本。
    echo 請到 https://www.python.org/downloads/ 下載安裝：勾選「Add python.exe to PATH」，並保留預設的「tcl/tk and IDLE」。
    goto fail
)
for /f "usebackq delims=" %%V in (`%PY% -c "import sys; print(sys.executable, sys.version.split()[0])"`) do echo 使用 %%V

rem Port: the first free one from 5600 (5040 is the default, but Windows system services often use it).
rem The finder is a small script file, because a one-line version needs awkward quoting in cmd.
set "FINDER=%TEMP%\bc_find_port_%RANDOM%.py"
> "%FINDER%" echo import socket
>> "%FINDER%" echo for port in range(5600, 5700):
>> "%FINDER%" echo     probe = socket.socket(); probe.settimeout(1)
>> "%FINDER%" echo     busy = probe.connect_ex(("127.0.0.1", port)) == 0; probe.close()
>> "%FINDER%" echo     if busy: continue
>> "%FINDER%" echo     s = socket.socket()
>> "%FINDER%" echo     try: s.bind(("127.0.0.1", port))
>> "%FINDER%" echo     except OSError: continue
>> "%FINDER%" echo     finally: s.close()
>> "%FINDER%" echo     print(port); break
for /f "usebackq delims=" %%N in (`%PY% "%FINDER%"`) do set "PORT=%%N"
del "%FINDER%" >nul 2>&1
if not defined PORT (
    echo 5600-5699 的連接埠都被占用，找不到可用的。請關掉其他程式，或上一局的伺服器，再試。
    goto fail
)
echo 使用連接埠 %PORT%

rem ---- 2. Name ----
:ask_name
set "NAME=Player"
set /p "NAME=你的名字（1–20 個字，直接按 Enter = Player）："
%PY% -c "import sys; sys.path.insert(0, '.'); import bc; name = sys.argv[1]; sys.exit(0 if bc.is_valid_text(name, 1, 20) and name not in ('BOT-1', 'BOT-2') else 1)" "!NAME!"
if errorlevel 1 (
    echo 名字要 1–20 個字、不能有控制字元，也不能用 BOT-1 或 BOT-2，那是 AI 的名字。
    goto ask_name
)

rem ---- 3. Server in the background, sharing this console; its output goes to the log ----
if not exist logs mkdir logs
rem The helper prints the server's PID and program name, e.g. "1234 python.exe".
set "SERVER_PID="
set "SERVER_EXE="
for /f "usebackq tokens=1*" %%P in (`%PY% -c "import os, subprocess, sys; log = open(sys.argv[2], 'w', encoding='utf-8'); p = subprocess.Popen([sys.executable, 'bc.py', 'server', '--host', '127.0.0.1', '--port', sys.argv[1], '--players', '3', '--bots', '2', '--games', '0'], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT); print(p.pid, os.path.basename(sys.executable))" %PORT% %SERVER_LOG%`) do (
    set "SERVER_PID=%%P"
    set "SERVER_EXE=%%Q"
)
if not defined SERVER_PID (
    echo 無法啟動伺服器。
    goto fail
)
echo 啟動伺服器：你 + 2 個 AI，127.0.0.1:%PORT% …
set /a TRIES=0
:wait_port
call :port_open
if not errorlevel 1 goto port_ready
call :server_running
if errorlevel 1 (
    type "%SERVER_LOG%"
    echo 伺服器沒有啟動成功。上面是它的訊息，也存在 %SERVER_LOG%。
    goto fail
)
set /a TRIES+=1
if %TRIES% geq 15 (
    echo 等了大約 30 秒，連接埠 %PORT% 還是連不上。請看 %SERVER_LOG%。
    call :stop_server
    goto fail
)
ping -n 2 127.0.0.1 >nul
goto wait_port

:port_ready
rem ---- 4. The game window: this line waits until the window is closed ----
echo 打開遊戲視窗：名字 !NAME!，隊伍 %TEAM%。關掉視窗就會停止伺服器。
%PY% bc.py gui --host 127.0.0.1 --port %PORT% --name "!NAME!" --team %TEAM%
set "GUI_EXIT=!errorlevel!"

rem ---- 5. Stop the server ----
call :stop_server
if "%GUI_EXIT%"=="0" (
    echo 遊戲結束。伺服器紀錄在 logs\，你的紀錄在 client_logs\。
    exit /b 0
)
echo 遊戲視窗已關閉，結束代碼 %GUI_EXIT%：中途離開或連線中斷。如果視窗根本沒出現，請看上面的錯誤訊息。
pause
exit /b %GUI_EXIT%

:fail
pause
exit /b 1

rem ---- helpers ----
:server_running
rem errorlevel 0 if our server is still running: same PID and same program name
tasklist /FI "PID eq %SERVER_PID%" /FI "IMAGENAME eq %SERVER_EXE%" 2>nul | find "%SERVER_PID%" >nul
exit /b %errorlevel%

:port_open
rem errorlevel 0 if something accepts connections on 127.0.0.1:PORT
%PY% -c "import socket, sys; s = socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1]))) == 0 else 1)" %PORT%
exit /b %errorlevel%

:stop_server
rem With --games 0 the server keeps hosting until we stop it, and Windows reuses PIDs, so stop it only if that
rem PID is still our server program.  /T also stops the AI players it started.
call :server_running
if not errorlevel 1 taskkill /F /T /PID %SERVER_PID% >nul 2>&1
echo 伺服器已停止。
exit /b 0
