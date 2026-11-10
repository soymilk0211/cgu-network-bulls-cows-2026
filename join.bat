@echo off
rem Join someone else's game on Windows: double-click this file. See README.
rem Python 3.10+ with tkinter -> host IP, port, name, team -> check the host is reachable -> bc.py gui.
rem
rem Every line that runs is ASCII-only.  cmd.exe misreads UTF-8 batch lines after chcp 65001 (lines get cut in the
rem middle), so the Traditional Chinese texts live at the very end of this file, after the last "exit", as lines
rem like "::NAME|text".  :say prints them with findstr and never lets cmd parse them as commands.
rem Texts may use !VAR! (filled in at print time) but never % & | < > ^ or a double quote.
cd /d "%~dp0"
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
title Bulls and Cows - join a game

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

rem ---- 2. Host, port, name, team ----
:ask_host
set "HOST="
call :say_n ASK_HOST
set /p "HOST="
%PY% -c "import re, sys; sys.exit(0 if re.fullmatch('[A-Za-z0-9._:-]{1,253}', sys.argv[1]) else 1)" "!HOST!"
if errorlevel 1 (
    call :say BAD_HOST
    goto ask_host
)

:ask_port
set "PORT=5040"
call :say_n ASK_PORT
set /p "PORT="
%PY% -c "import re, sys; sys.exit(0 if re.fullmatch('[0-9]{1,5}', sys.argv[1]) and 1 <= int(sys.argv[1]) <= 65535 else 1)" "!PORT!"
if errorlevel 1 (
    call :say BAD_PORT
    goto ask_port
)

:ask_name
set "NAME=Player"
call :say_n ASK_NAME
set /p "NAME="
%PY% -c "import sys; sys.path.insert(0, '.'); import bc; sys.exit(0 if bc.is_valid_text(sys.argv[1], 1, 20) else 1)" "!NAME!"
if errorlevel 1 (
    call :say BAD_NAME
    goto ask_name
)

:ask_team
set "TEAM=G00"
call :say_n ASK_TEAM
set /p "TEAM="
%PY% -c "import sys; sys.path.insert(0, '.'); import bc; sys.exit(0 if bc.is_valid_text(sys.argv[1], 1, 20) else 1)" "!TEAM!"
if errorlevel 1 (
    call :say BAD_TEAM
    goto ask_team
)

rem ---- 3. Can we reach the host? ----
call :say CHECKING
%PY% -c "import socket, sys; s = socket.socket(); s.settimeout(3); sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)" "!HOST!" !PORT! 2>nul
if errorlevel 1 (
    call :say UNREACHABLE
    goto fail
)

rem ---- 4. The game window: this line waits until the window is closed ----
call :say OPENING
%PY% bc.py gui --host "!HOST!" --port !PORT! --name "!NAME!" --team "!TEAM!"
set "GUI_EXIT=!errorlevel!"
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

rem ---- texts (never run: the script exits above) ----
exit /b 0
::PY_MISSING|找不到含 tkinter 的 Python 3.10 以上版本。
::PY_MISSING|請到 https://www.python.org/downloads/ 下載安裝：勾選「Add python.exe to PATH」，並保留預設的「tcl/tk and IDLE」。
::PY_USING|使用 !PYINFO!
::ASK_HOST|主機的 IP（向開房的人要，例如 192.168.1.23）：
::BAD_HOST|請輸入主機的 IP，或網址：只能有英數字、點、冒號、減號，不能是空的。
::ASK_PORT|連接埠（直接按 Enter = 5040）：
::BAD_PORT|連接埠要是 1-65535 的數字。
::ASK_NAME|你的名字（1–20 個字，直接按 Enter = Player）：
::BAD_NAME|名字要 1–20 個字，不能有控制字元。
::ASK_TEAM|隊伍（1–20 個字，直接按 Enter = G00）：
::BAD_TEAM|隊伍要 1–20 個字，不能有控制字元。
::CHECKING|檢查連得上 !HOST!:!PORT! …
::UNREACHABLE|連不上 !HOST!:!PORT!。請檢查：
::UNREACHABLE|  1. IP 和連接埠有沒有打錯，請開房的人再確認一次；
::UNREACHABLE|  2. 開房的人已經執行 host.command 或 host.bat，而且視窗還開著；
::UNREACHABLE|  3. 兩台電腦在同一個 Wi-Fi 或區域網路，不是不同網路，也不是訪客網路；
::UNREACHABLE|  4. 開房的人的防火牆有允許連入，Windows 防火牆跳出詢問時要按「允許存取」。
::OPENING|打開遊戲視窗：!HOST!:!PORT!，名字 !NAME!，隊伍 !TEAM!。
::DONE|遊戲結束。你的紀錄在 client_logs\。
::CLOSED|遊戲視窗已關閉，結束代碼 !GUI_EXIT!：中途離開或連線中斷。如果視窗根本沒出現，請看上面的錯誤訊息。
