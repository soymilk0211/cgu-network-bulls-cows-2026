@echo off
rem Host a game for others on Windows: double-click this file. See README.
rem Python 3.10+ with tkinter -> players, AI bots, your name -> port 5040 must be free ->
rem bc.py server --host 0.0.0.0 --games 0 in the background -> show this PC's LAN IPv4 address(es) ->
rem bc.py gui --host 127.0.0.1 -> when the window closes, stop the server.
rem
rem Every line that runs is ASCII-only.  cmd.exe misreads UTF-8 batch lines after chcp 65001 (lines get cut in the
rem middle), so the Traditional Chinese texts live at the very end of this file, after the last "exit", as lines
rem like "::NAME|text".  :say prints them with findstr and never lets cmd parse them as commands.
rem Texts may use !VAR! (filled in at print time) but never % & | < > ^ or a double quote.
cd /d "%~dp0"
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PORT=5040"
set "TEAM=G00"
set "SERVER_LOG=logs\host-server.txt"
title Bulls and Cows - host a game

rem ---- 1. Python 3.10+ with tkinter ----
set "PY="
for %%C in ("python3" "py -3" "python") do (
    if not defined PY (
        %%~C -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=%%~C"
    )
)
if not defined PY (
    call :say PY_MISSING
    goto fail
)
for /f "usebackq delims=" %%V in (`%PY% -c "import sys; print(sys.executable, sys.version.split()[0])"`) do set "PYINFO=%%V"
call :say PY_USING

rem ---- 2. Players, bots, name ----
:ask_players
set "PLAYERS=3"
call :say_n ASK_PLAYERS
set /p "PLAYERS="
%PY% -c "import re, sys; sys.exit(0 if re.fullmatch('[0-9]{1,3}', sys.argv[1]) and 2 <= int(sys.argv[1]) <= 6 else 1)" "!PLAYERS!"
if errorlevel 1 (
    call :say BAD_PLAYERS
    goto ask_players
)
rem Valid values are 2-6, so the last digit is the value (this also drops leading zeros, which set /a reads as octal).
set /a PLAYERS=1!PLAYERS! - 10 * (1!PLAYERS! / 10)
set /a MAX_BOTS=PLAYERS-1

:ask_bots
set "BOTS=0"
call :say_n ASK_BOTS
set /p "BOTS="
%PY% -c "import re, sys; sys.exit(0 if re.fullmatch('[0-9]{1,3}', sys.argv[1]) and 0 <= int(sys.argv[1]) <= int(sys.argv[2]) else 1)" "!BOTS!" !MAX_BOTS!
if errorlevel 1 (
    call :say BAD_BOTS
    goto ask_bots
)
set /a BOTS=1!BOTS! - 10 * (1!BOTS! / 10)
set /a HUMANS_LEFT=PLAYERS-BOTS-1

:ask_name
set "NAME=Player"
call :say_n ASK_NAME
set /p "NAME="
%PY% -c "import re, sys; sys.path.insert(0, '.'); import bc; name = sys.argv[1]; sys.exit(0 if bc.is_valid_text(name, 1, 20) and not re.fullmatch('BOT-[0-9]+', name) else 1)" "!NAME!"
if errorlevel 1 (
    call :say BAD_NAME
    goto ask_name
)

rem ---- 3. Port 5040 must be free ----
call :port_open
if not errorlevel 1 goto port_busy
%PY% -c "import socket, sys; s = socket.socket(); s.bind(('0.0.0.0', int(sys.argv[1])))" !PORT! >nul 2>&1
if errorlevel 1 goto port_busy
goto port_free
:port_busy
call :say PORT_BUSY
goto fail
:port_free

rem ---- 4. Server in the background; its output goes to the log ----
if not exist logs mkdir logs
rem The helper prints the server's PID and program name, e.g. "1234 python.exe".
set "SERVER_PID="
set "SERVER_EXE="
for /f "usebackq tokens=1*" %%P in (`%PY% -c "import os, subprocess, sys; log = open(sys.argv[4], 'w', encoding='utf-8'); p = subprocess.Popen([sys.executable, 'bc.py', 'server', '--host', '0.0.0.0', '--port', sys.argv[1], '--players', sys.argv[2], '--bots', sys.argv[3], '--games', '0'], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT); print(p.pid, os.path.basename(sys.executable))" !PORT! !PLAYERS! !BOTS! %SERVER_LOG%`) do (
    set "SERVER_PID=%%P"
    set "SERVER_EXE=%%Q"
)
if not defined SERVER_PID (
    call :say NO_SERVER
    goto fail
)
call :say STARTING
set /a TRIES=0
:wait_port
call :port_open
if not errorlevel 1 goto port_ready
call :server_running
if errorlevel 1 (
    type "%SERVER_LOG%"
    call :say SERVER_FAILED
    goto fail
)
set /a TRIES+=1
if !TRIES! geq 15 (
    call :say PORT_TIMEOUT
    call :stop_server
    goto fail
)
ping -n 2 127.0.0.1 >nul
goto wait_port

:port_ready
rem ---- 5. Tell the friends where to connect ----
rem The lister is a small script file, because a one-line version needs awkward quoting in cmd.
set "LISTER=%TEMP%\bc_lan_ips_%RANDOM%.py"
> "%LISTER%" echo import socket
>> "%LISTER%" echo ips = set()
>> "%LISTER%" echo try:
>> "%LISTER%" echo     ips.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
>> "%LISTER%" echo except OSError:
>> "%LISTER%" echo     pass
>> "%LISTER%" echo try:
>> "%LISTER%" echo     s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
>> "%LISTER%" echo     s.connect(('10.255.255.255', 1))
>> "%LISTER%" echo     ips.add(s.getsockname()[0])
>> "%LISTER%" echo     s.close()
>> "%LISTER%" echo except OSError:
>> "%LISTER%" echo     pass
>> "%LISTER%" echo for ip in sorted(ips):
>> "%LISTER%" echo     if not ip.startswith(('127.', '169.254.', '0.')): print(ip)
echo.
echo ================================================================
set "HAVE_IP="
for /f "usebackq delims=" %%I in (`%PY% "%LISTER%"`) do (
    if not defined HAVE_IP call :say ROOM_READY
    set "HAVE_IP=1"
    set "LAN_IP=%%I"
    call :say IP_LINE
)
del "%LISTER%" >nul 2>&1
if defined HAVE_IP (call :say IP_HINT) else (call :say NO_IP)
call :say WAIT_HUMANS
call :say FIREWALL
echo ================================================================
echo.

rem ---- 6. Your own game window: this line waits until the window is closed ----
call :say OPENING
%PY% bc.py gui --host 127.0.0.1 --port !PORT! --name "!NAME!" --team !TEAM!
set "GUI_EXIT=!errorlevel!"

rem ---- 7. Stop the server ----
call :stop_server
if "!GUI_EXIT!"=="0" (
    call :say DONE
    exit /b 0
)
call :say CLOSED
pause
exit /b !GUI_EXIT!

:fail
pause
exit /b 1

rem ---- helpers ----
:say
rem print the lines "::NAME|text" for NAME=%1, with a newline after each
for /f "usebackq tokens=1* delims=|" %%A in (`findstr /b /c:"::%1|" "%~f0"`) do echo(%%B
exit /b 0

:say_n
rem same, but no newline (a prompt)
for /f "usebackq tokens=1* delims=|" %%A in (`findstr /b /c:"::%1|" "%~f0"`) do <nul set /p "=%%B"
exit /b 0

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
call :say STOPPED
exit /b 0

rem ---- texts (never run: the script exits above) ----
exit /b 0
::PY_MISSING|找不到含 tkinter 的 Python 3.10 以上版本。
::PY_MISSING|請到 https://www.python.org/downloads/ 下載安裝：勾選「Add python.exe to PATH」，並保留預設的「tcl/tk and IDLE」。
::PY_USING|使用 !PYINFO!
::ASK_PLAYERS|總玩家數（包含你和 AI，2-6，直接按 Enter = 3）：
::BAD_PLAYERS|總玩家數要是 2-6 的數字，伺服器最多 6 位玩家。
::ASK_BOTS|AI 數量（0-!MAX_BOTS!，直接按 Enter = 0，AI 也算在總玩家數裡）：
::BAD_BOTS|AI 數量要是 0-!MAX_BOTS! 的數字，至少要留一位給真人。
::ASK_NAME|你的名字（1–20 個字，直接按 Enter = Player）：
::BAD_NAME|名字要 1–20 個字、不能有控制字元，也不能用 BOT-1、BOT-2 這種名字，那是 AI 的名字。
::PORT_BUSY|連接埠 !PORT! 已經被占用，無法開房。
::PORT_BUSY|通常是上一局的伺服器，或別的程式還在用它：請關掉那個視窗再試。
::PORT_BUSY|如果是系統程式占用，可以改用手動指令開在別的連接埠，見 README 的「連到別人／開房給別人」。
::NO_SERVER|無法啟動伺服器。
::STARTING|啟動伺服器：共 !PLAYERS! 位玩家，你 + 其他真人 !HUMANS_LEFT! 位 + AI !BOTS! 個，連接埠 !PORT! …
::SERVER_FAILED|伺服器沒有啟動成功。上面是它的訊息，也存在 !SERVER_LOG!。
::PORT_TIMEOUT|等了大約 30 秒，連接埠 !PORT! 還是連不上。請看 !SERVER_LOG!。
::ROOM_READY|房間開好了！請同一個 Wi-Fi 或區域網路的朋友，雙擊 join.bat（Mac 用 join.command），輸入：
::IP_LINE|    主機 IP：!LAN_IP!    連接埠：!PORT!
::IP_HINT|有多個 IP 時，先試和朋友同一個網段的，例如都是 192.168.1.x。
::NO_IP|房間開好了，但找不到這台電腦的區域網路 IP。請先連上 Wi-Fi 或網路，再在命令提示字元執行 ipconfig 查看 IPv4 位址。
::WAIT_HUMANS|還需要 !HUMANS_LEFT! 位真人加入，人到齊後遊戲就會自動開始。
::FIREWALL|第一次會跳出「Windows 防火牆」詢問是否允許 Python 連入：請勾選「私人網路」再按「允許存取」，朋友才連得進來。
::OPENING|打開遊戲視窗：名字 !NAME!，隊伍 !TEAM!。關掉視窗就會停止伺服器，朋友也會斷線。
::DONE|遊戲結束。伺服器紀錄在 logs\，你的紀錄在 client_logs\。
::CLOSED|遊戲視窗已關閉，結束代碼 !GUI_EXIT!：中途離開或連線中斷。如果視窗根本沒出現，請看上面的錯誤訊息。
::STOPPED|伺服器已停止。
