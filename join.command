#!/bin/bash
# 幾A幾B 連到別人的房間（macOS）：在 Finder 雙擊這個檔案。
#
# 1. 找一個有 tkinter 的 Python 3.10+（先試 python3，再試 python3.13 … python3.10、python）。
# 2. 問主機的 IP（必填）、連接埠（Enter = 5040）、你的名字、隊伍（Enter = G00）。
# 3. 先檢查連得上主機；連不上會告訴你可能的原因。
# 4. 打開遊戲視窗（bc.py gui）。
#
# 要自己開房給別人連，請用 host.command。詳見 README 的「連到別人／開房給別人」。
# 這個檔案只呼叫 bc.py，不改它。

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

# ---- 2. Host, port, name, team ----------------------------------------------
while :; do
    printf '主機的 IP（向開房的人要，例如 192.168.1.23）：'
    IFS= read -r HOST
    if "$PY" -c 'import re, sys
sys.exit(0 if re.fullmatch(r"[A-Za-z0-9._:-]{1,253}", sys.argv[1]) else 1)' "$HOST"; then
        break
    fi
    echo "請輸入主機的 IP（或網址），只能有英數字、點、冒號、減號，不能是空的。"
done

while :; do
    printf '連接埠（直接按 Enter = 5040）：'
    IFS= read -r PORT
    [ -z "$PORT" ] && PORT=5040
    if "$PY" -c 'import re, sys
sys.exit(0 if re.fullmatch(r"[0-9]{1,5}", sys.argv[1]) and 1 <= int(sys.argv[1]) <= 65535 else 1)' "$PORT"; then
        break
    fi
    echo "連接埠要是 1–65535 的數字。"
done

while :; do
    printf '你的名字（1–20 個字，直接按 Enter = Player）：'
    IFS= read -r NAME
    [ -z "$NAME" ] && NAME="Player"
    if "$PY" -c 'import sys
sys.path.insert(0, ".")
import bc
sys.exit(0 if bc.is_valid_text(sys.argv[1], 1, 20) else 1)' "$NAME"; then
        break
    fi
    echo "名字要 1–20 個字，不能有控制字元。"
done

while :; do
    printf '隊伍（1–20 個字，直接按 Enter = G00）：'
    IFS= read -r TEAM
    [ -z "$TEAM" ] && TEAM="G00"
    if "$PY" -c 'import sys
sys.path.insert(0, ".")
import bc
sys.exit(0 if bc.is_valid_text(sys.argv[1], 1, 20) else 1)' "$TEAM"; then
        break
    fi
    echo "隊伍要 1–20 個字，不能有控制字元。"
done

# ---- 3. Can we reach the host? ------------------------------------------------
echo "檢查連得上 $HOST:$PORT …"
if ! "$PY" -c 'import socket, sys
s = socket.socket()
s.settimeout(3)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)' "$HOST" "$PORT" 2>/dev/null; then
    finish 1 "連不上 $HOST:$PORT。請檢查：
  1. IP 和連接埠有沒有打錯（請開房的人再確認一次）；
  2. 開房的人已經執行 host.command / host.bat，而且視窗還開著；
  3. 兩台電腦在同一個 Wi-Fi／區域網路（不是不同網路，也不是訪客網路）；
  4. 開房的人的防火牆有允許連入（Windows 防火牆跳出詢問時要按「允許存取」）。"
fi

# ---- 4. The game window (blocks until it is closed) -------------------------
echo "打開遊戲視窗：$HOST:$PORT，名字 $NAME，隊伍 $TEAM。"
"$PY" bc.py gui --host "$HOST" --port "$PORT" --name "$NAME" --team "$TEAM"
GUI_EXIT=$?
if [ "$GUI_EXIT" -eq 0 ]; then
    finish 0 "遊戲結束。你的紀錄在 client_logs/。"
fi
finish "$GUI_EXIT" "遊戲視窗已關閉（結束代碼 $GUI_EXIT：中途離開或連線中斷）。如果視窗根本沒出現，請看上面的錯誤訊息。"
