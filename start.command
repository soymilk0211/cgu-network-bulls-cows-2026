#!/bin/bash
# 幾A幾B 一鍵開始（macOS）：在 Finder 雙擊這個檔案。
#
# 1. 找一個有 tkinter 的 Python 3.10+（先試 python3，再試 python3.13 … python3.10、python）。
# 2. 問你的名字（直接按 Enter = Player），隊伍用 G00。
# 3. 在背景啟動伺服器：你 + 2 個 AI，共 3 位玩家，只在本機 127.0.0.1:5040。
# 4. 等連接埠 5040 可以連線後，打開遊戲視窗（bc.py gui）。
# 5. 關掉遊戲視窗後，自動停止伺服器（和它的 AI）。
#
# 詳見 README 的「一鍵開始」。這個檔案只呼叫 bc.py，不改它。

PORT=5040
TEAM=G00
cd "$(dirname "$0")" || exit 1

finish() {  # print a message and exit with the given code; on errors wait for Enter first
    echo "$2"
    if [ "$1" -ne 0 ] && [ -t 0 ]; then
        printf '按 Enter 關閉…'
        read -r _
    fi
    exit "$1"
}

# ---- 1. Python 3.10+ with tkinter ------------------------------------------
PY=""
for cand in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$cand" >/dev/null 2>&1 &&
        "$cand" -c 'import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        PY="$cand"
        break
    fi
done
if [ -z "$PY" ]; then
    finish 1 "找不到含 tkinter 的 Python 3.10 以上版本。
請到 https://www.python.org/downloads/ 下載安裝（官方安裝檔已內含 tkinter），
或用 Homebrew：brew install python python-tk"
fi
echo "使用 $("$PY" -c 'import sys; print(sys.executable, sys.version.split()[0])')"

port_open() {  # true if something accepts connections on 127.0.0.1:$PORT
    "$PY" -c 'import socket, sys
s = socket.socket()
s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$PORT"
}

if port_open; then
    finish 1 "連接埠 $PORT 已經有程式在使用（可能是上一局的伺服器還沒關）。
請先關掉它，或等一下再試。"
fi

# ---- 2. Name ----------------------------------------------------------------
while :; do
    printf '你的名字（1–20 個字，直接按 Enter = Player）：'
    IFS= read -r NAME
    [ -z "$NAME" ] && NAME="Player"
    if "$PY" -c 'import sys
sys.path.insert(0, ".")
import bc
name = sys.argv[1]
sys.exit(0 if bc.is_valid_text(name, 1, 20) and name not in ("BOT-1", "BOT-2") else 1)' "$NAME"; then
        break
    fi
    echo "名字要 1–20 個字、不能有控制字元，也不能用 BOT-1 或 BOT-2（AI 的名字）。"
done

# ---- 3. Server in the background --------------------------------------------
mkdir -p logs
SERVER_LOG="logs/launcher-server.txt"
"$PY" bc.py server --host 127.0.0.1 --port "$PORT" --players 3 --bots 2 >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

stop_server() {  # only if that PID is still our server (it ends by itself after one game)
    if ps -p "$SERVER_PID" -o command= 2>/dev/null | grep -q "bc.py server"; then
        pkill -TERM -P "$SERVER_PID" 2>/dev/null  # the AI players started by the server
        kill -TERM "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
        echo "伺服器已停止。"
    fi
}
trap stop_server EXIT
trap 'exit 130' INT TERM HUP  # Ctrl+C or closing Terminal: the EXIT trap stops the server

echo "啟動伺服器（你 + 2 個 AI，127.0.0.1:$PORT）…"
tries=0
until port_open; do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        cat "$SERVER_LOG"
        finish 1 "伺服器沒有啟動成功（上面是它的訊息，也存在 $SERVER_LOG）。"
    fi
    tries=$((tries + 1))
    if [ "$tries" -ge 50 ]; then
        finish 1 "等了 15 秒，連接埠 $PORT 還是連不上。請看 $SERVER_LOG。"
    fi
    sleep 0.3
done

# ---- 4. The game window (blocks until it is closed) -------------------------
echo "打開遊戲視窗：名字 $NAME，隊伍 $TEAM。關掉視窗就會停止伺服器。"
"$PY" bc.py gui --host 127.0.0.1 --port "$PORT" --name "$NAME" --team "$TEAM"
GUI_EXIT=$?

# ---- 5. Stop the server -------------------------------------------------------
stop_server
trap - EXIT
if [ "$GUI_EXIT" -eq 0 ]; then
    finish 0 "遊戲結束。伺服器紀錄在 logs/，你的紀錄在 client_logs/。"
fi
finish "$GUI_EXIT" "遊戲視窗已關閉（結束代碼 $GUI_EXIT：中途離開或連線中斷）。如果視窗根本沒出現，請看上面的錯誤訊息。"
