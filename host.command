#!/bin/bash
# 幾A幾B 開房給別人連（macOS）：在 Finder 雙擊這個檔案。
#
# 1. 找一個有 tkinter 的 Python 3.10+（先試 python3，再試 python3.13 … python3.10、python）。
# 2. 問總玩家數（2–6，Enter = 3）、AI 數量（0 到 總數-1，Enter = 0）、你的名字（Enter = Player）。
# 3. 確認連接埠 5040 沒被占用，在背景啟動伺服器（--host 0.0.0.0 --games 0），讓同一個區域網路的人連得進來。
# 4. 顯示這台電腦的區域網路 IP，給朋友在 join.command / join.bat 輸入。
# 5. 打開你自己的遊戲視窗（bc.py gui --host 127.0.0.1）。
# 6. 關掉遊戲視窗後，自動停止伺服器（和它的 AI）。
#
# 詳見 README 的「連到別人／開房給別人」。這個檔案只呼叫 bc.py，不改它。

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

# ---- 2. Players, bots, name ---------------------------------------------------
while :; do
    printf '總玩家數（包含你和 AI，2–6，直接按 Enter = 3）：'
    IFS= read -r PLAYERS
    [ -z "$PLAYERS" ] && PLAYERS=3
    if "$PY" -c 'import re, sys
sys.exit(0 if re.fullmatch(r"[0-9]{1,3}", sys.argv[1]) and 2 <= int(sys.argv[1]) <= 6 else 1)' "$PLAYERS"; then
        break
    fi
    echo "總玩家數要是 2–6 的數字（伺服器最多 6 位玩家）。"
done
PLAYERS=$((10#$PLAYERS))

MAX_BOTS=$((PLAYERS - 1))
while :; do
    printf 'AI 數量（0–%s，直接按 Enter = 0；AI 也算在總玩家數裡）：' "$MAX_BOTS"
    IFS= read -r BOTS
    [ -z "$BOTS" ] && BOTS=0
    if "$PY" -c 'import re, sys
sys.exit(0 if re.fullmatch(r"[0-9]{1,3}", sys.argv[1]) and 0 <= int(sys.argv[1]) <= int(sys.argv[2]) else 1)' "$BOTS" "$MAX_BOTS"; then
        break
    fi
    echo "AI 數量要是 0–$MAX_BOTS 的數字（至少要留一位給真人）。"
done
BOTS=$((10#$BOTS))

while :; do
    printf '你的名字（1–20 個字，直接按 Enter = Player）：'
    IFS= read -r NAME
    [ -z "$NAME" ] && NAME="Player"
    if "$PY" -c 'import re, sys
sys.path.insert(0, ".")
import bc
name = sys.argv[1]
sys.exit(0 if bc.is_valid_text(name, 1, 20) and not re.fullmatch(r"BOT-[0-9]+", name) else 1)' "$NAME"; then
        break
    fi
    echo "名字要 1–20 個字、不能有控制字元，也不能用 BOT-1、BOT-2… 這種名字（那是 AI 的名字）。"
done

# ---- 3. Port 5040 must be free -----------------------------------------------
if port_open || ! "$PY" -c 'import socket, sys
s = socket.socket()
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()' "$PORT"; then
    finish 1 "連接埠 $PORT 已經被占用，無法開房。
通常是上一局的伺服器（或別的程式）還在用它：請關掉那個視窗再試。
如果是系統程式占用，可以改用手動指令開在別的連接埠，見 README 的「連到別人／開房給別人」。"
fi

# ---- 4. Server in the background (listens on every network interface) --------
mkdir -p logs
SERVER_LOG="logs/host-server.txt"
"$PY" bc.py server --host 0.0.0.0 --port "$PORT" --players "$PLAYERS" --bots "$BOTS" --games 0 >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

stop_server() {  # only if that PID is still our server (with --games 0 it keeps hosting until we stop it)
    if ps -p "$SERVER_PID" -o command= 2>/dev/null | grep -q "bc.py server"; then
        pkill -TERM -P "$SERVER_PID" 2>/dev/null  # the AI players started by the server
        kill -TERM "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
        echo "伺服器已停止。"
    fi
}
trap stop_server EXIT
trap 'exit 130' INT TERM HUP  # Ctrl+C or closing Terminal: the EXIT trap stops the server

echo "啟動伺服器（共 $PLAYERS 位玩家：你 + 其他真人 $((PLAYERS - BOTS - 1)) 位 + AI $BOTS 個，連接埠 $PORT）…"
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

# ---- 5. Tell the friends where to connect --------------------------------------
LAN_IPS=$("$PY" -c 'import socket
ips = set()
try:
    ips.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
except OSError:
    pass
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("10.255.255.255", 1))
    ips.add(s.getsockname()[0])
    s.close()
except OSError:
    pass
for ip in sorted(ips):
    if not ip.startswith(("127.", "169.254.", "0.")):
        print(ip)')
echo
echo "================================================================"
if [ -n "$LAN_IPS" ]; then
    echo "房間開好了！請同一個 Wi-Fi／區域網路的朋友，雙擊 join.command（Windows：join.bat），輸入："
    echo "$LAN_IPS" | while IFS= read -r ip; do echo "    主機 IP：$ip    連接埠：$PORT"; done
    echo "（有多個 IP 時，先試和朋友同一個網段的，例如都是 192.168.1.x）"
else
    echo "房間開好了，但找不到這台電腦的區域網路 IP。請先連上 Wi-Fi／網路，再到「系統設定 → 網路」查看 IP。"
fi
echo "還需要 $((PLAYERS - BOTS - 1)) 位真人加入，人到齊後遊戲就會自動開始。"
echo "第一次可能會跳出防火牆詢問「是否允許 Python 接受連入連線」：請按「允許」，朋友才連得進來。"
echo "================================================================"
echo

# ---- 6. Your own game window (blocks until it is closed) ---------------------
echo "打開遊戲視窗：名字 $NAME，隊伍 $TEAM。關掉視窗就會停止伺服器（朋友也會斷線）。"
"$PY" bc.py gui --host 127.0.0.1 --port "$PORT" --name "$NAME" --team "$TEAM"
GUI_EXIT=$?

# ---- 7. Stop the server -------------------------------------------------------
stop_server
trap - EXIT
if [ "$GUI_EXIT" -eq 0 ]; then
    finish 0 "遊戲結束。伺服器紀錄在 logs/，你的紀錄在 client_logs/。"
fi
finish "$GUI_EXIT" "遊戲視窗已關閉（結束代碼 $GUI_EXIT：中途離開或連線中斷）。如果視窗根本沒出現，請看上面的錯誤訊息。"
