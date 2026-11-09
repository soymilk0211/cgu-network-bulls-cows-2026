# cgu-network-bulls-cows-2026
CGU 網路應用軟體設計：多人幾A幾B連線對戰系統（2026）

Multiplayer Bulls & Cows (幾A幾B) over TCP, protocol **BC/1.0**.
Everything is in one file, `bc.py`, which uses only the Python standard library (Python 3.10+).

## Usage

```
python bc.py server [--host 0.0.0.0] [--port 5040] [--players 2] [--bots 0]
                    [--guess-timeout 60] [--feedback-timeout 60] [--commit-timeout 30]
                    [--reveal-timeout 15] [--max-turns 120] [--max-skips 3]
                    [--games 1] [--log-dir logs]
python bc.py client --host HOST [--port 5040] --name NAME [--team G00]
python bc.py ai     --host HOST [--port 5040] --name NAME [--team G00] [--quiet]
```

`python bc.py <subcommand> --help` lists every option with its default. Timeouts are in seconds and may be
fractional (for example `--guess-timeout 1.5`).

Status: the shared protocol layer and CLI (#1), the human `client` (#2) and the `ai` with the v1 strategy
(#3) are done. The `server` (#4) is still to come.

## How to play (client)

```
python bc.py client --host 127.0.0.1 --port 5040 --name G00-小明 --team G00
```

`--name` and `--team` must be 1–20 characters with no control characters. For the class games, start the
name with your group code, for example `G07-小明`. The client only asks you something when you have to act.
Each question starts with `?` and shows the seconds left:

| When | You type | What the client does |
|---|---|---|
| Game starts | your secret (4 different digits, e.g. `0275`) or just Enter for a random one | sends the **commit** (hash) automatically; the salt stays in memory. If you do not answer in time, a random secret is used |
| Your turn (`TURN` with you as guesser) | your guess, e.g. `0927` | checks the format locally, sends exactly one `GUESS` |
| Someone else guessed | Enter | the answer for your secret is **prefilled** (e.g. `1A2B`); Enter sends it |
| … or another answer, e.g. `2A0B` / `2 0` | a changed answer must be confirmed with `yes` (a wrong answer gets you disqualified at the reveal) |
| Reveal requested | nothing | the secret and salt are sent **automatically and immediately** |

**A missing feedback = FORFEIT.** A missed guess only skips your turn (after `max_skips` skips in a row you
forfeit too). When the time is up the question is withdrawn and late input is not sent.

While you think, the client keeps showing new messages (turn results, chat, lobby) and answers `PING`
automatically. You see every turn's results, eliminations, every reveal verdict and the final ranking. Each
reveal is re-checked locally, and `[!] our own check says …` appears if it disagrees with the server. After
you are out you keep watching as a spectator.

Commands at any time: `/chat TEXT`, `/cands` (possible secrets left per opponent), `/quit`, `/help`.

The client writes its own JSON Lines log (`ts`, `dir`, `peer`, `msg`) to `client_logs/` in the current
folder. That folder is separate from the server's `--log-dir` and ignored by git.

Exit code: 0 after a normal `GAME_OVER`, 1 after a fatal `ERROR` or a lost connection, 2 for a bad
`--name`/`--team`.

## AI player

```
python bc.py ai --host 127.0.0.1 --port 5040 --name G00-AI --team G00 [--quiet]
```

The AI plays without any input. It picks a random secret with `secrets` and sends the commit. Feedback is
always the honest answer, sent at once, and reveals are immediate. It answers `PING` and ignores unknown
messages. `--quiet` prints only errors and the final result. Like the client, it logs to `client_logs/`.

**How it thinks (v1, the spec's baseline, §5.5).** For every opponent still in the game the AI keeps the list
of secrets that are still possible. It starts with all 5040. Every `TURN_RESULT` shrinks these lists, not only
the results of our own guesses. On our turn:

1. **Target:** the opponent with the fewest possible secrets left.
2. **Guess:** the target's possible secret that splits its list into the most different answers (`aAbB`).
   This is Kooi's "most parts" rule. Big lists are estimated on a random sample of 500. The search stops after
   1 s (or a quarter of the guess timeout), and it stops at once if a guess reaches the maximum of 14 parts.

Offline, against one opponent, v1 needed 5.26 guesses on average and at most 7 (300 random secrets). The
slowest decision took 0.17 s.

## Tests

Run these from the repository root. Each command runs all three test files (`tests/test_protocol.py`,
`tests/test_client.py`, `tests/test_ai.py`):

```
python -m pytest -q                      # needs pytest (a development tool only)
python -m unittest discover -s tests -v  # standard library only
```

`tests/` is a plain folder, not a package. That is why `python -m unittest discover -s tests -t .` fails with
"Start directory is not importable", and a bare `python -m unittest` finds no tests. Use the commands above.

## Team workflow

The rules for teammates and their AI coding tools are in [`AGENTS.md`](AGENTS.md). It covers:
- who owns which part of `bc.py`;
- the shared core and its interface contracts;
- one issue = one branch = one PR, with no direct pushes to `main` and no self-merges;
- running all tests before opening a PR;
- stdlib only, following the BC/1.0 spec, and never committing `course/`, logs, secrets or student IDs.

## Code map (`bc.py`)

| Section | What it does | Spec |
|---|---|---|
| 1. Constants and errors | `PROTOCOL_VERSION`, `MAX_LINE_BYTES = 4096`, `LineTooLong`, `BadJson` | §8.2, §13 |
| 2. Framing | `LineBuffer` turns received bytes into whole lines, `decode` / `encode` convert between lines and dicts, `send_msg` sends with `sendall` | §8.2–8.3, §16.3 |
| 3. Validators | `is_valid_number`, `is_int`, `is_valid_feedback`, `is_hex`, `is_valid_text` (name/team/chat), `is_supported_protocol` (`BC/1.x`), `LEGAL_FEEDBACK` | §9, §16.2 |
| 4. Game math | `ALL_NUMBERS`, `score`, `make_commit`, `verify_reveal` (NO_REVEAL / BAD_COMMIT / INCONSISTENT / OK), `filter_candidates` | §2.2, §3.1, §14 |
| 5. Message helpers | `make_pong`, `dispatch`, which ignores unknown message types | §8.3.6, §11.1 |
| 6. Client log | `JsonlLog`: own log in the server's record format, written to `client_logs/` | §5.6 |
| 7. Player session | `PlayerSession`: connection, reader thread, main event loop, public game state (history, candidates per opponent), automatic COMMIT/REVEAL/PONG, local reveal check. Shared by client and AI | §11–12, §16.3 |
| 8. Human client | `HumanClient`: input thread, prompts with deadlines, prefilled feedback plus confirmation | §5.4 |
| 9. AI | `pick_target`, `most_parts_guess`, `choose_guess_v1`, `AIPlayer` | §5.5 |
| 10. CLI | `build_parser` with the exact §5.2 options | §5.2 |

## Course materials

The teacher's spec PDF, reference server and `bc_selftest.py` are private course files. Keep them in a local
`course/` folder, which is listed in `.gitignore`. Never commit them.

## 繁體中文摘要

**這是什麼**：多人幾A幾B 連線對戰（協定 BC/1.0）。所有程式都在根目錄的 `bc.py`，只用 Python 3.10 以上的標準
函式庫，不需安裝任何套件。

**怎麼執行**（`python bc.py <子命令> --help` 可看全部選項與預設值）：

```
python bc.py client --host 伺服器位址 --port 5040 --name G00-小明 --team G00   # 人類玩家
python bc.py ai     --host 伺服器位址 --port 5040 --name G00-AI   --team G00   # AI（可加 --quiet）
python bc.py server ...                                                       # 伺服器：尚未完成（#4）
```

- **人類玩家**：
  - 開局時輸入四位不重複的秘密數字，或直接按 Enter 隨機產生；承諾值（雜湊）會自動送出。
  - 輪到你時才會要求喊數。
  - 別人喊數後，程式會預先算好幾A幾B，按 Enter 就送出；若要改成別的值，必須再輸入 `yes` 確認。
  - 被要求揭示時，程式會自動、立即送出秘密與鹽值。
  - **回報逾時＝棄權**；喊數逾時只會跳過該回合。
  - 隨時可用的指令：`/chat 文字`、`/cands`、`/quit`、`/help`。
- **AI**：全自動。它會替每位對手記住還可能的答案，並用每一回合公開的結果刪減；輪到時鎖定候選最少的對手，
  用「最多劃分」（most parts）挑喊數。
- **日誌**：client 與 ai 的日誌寫在 `client_logs/`。這個資料夾不是伺服器的 `--log-dir`，也不會進 git。

**怎麼測試**（在儲存庫根目錄執行，三個測試檔都會跑到）：

```
python -m pytest -q
python -m unittest discover -s tests -v
```

注意：`-t .` 的寫法與單獨的 `python -m unittest` 都無法使用。

**分工與流程**（詳見 [`AGENTS.md`](AGENTS.md)，請各位與自己的 AI 工具都先讀它）：

- **三個負責區域**（負責人待填）：A 伺服器（#4）、B 人類玩家（#2）、C AI（#3）。共用核心（收發框架、驗證、
  計分、承諾/揭示驗證、`PlayerSession`、日誌、命令列）沒有單一負責人。要改共用核心的介面或行為時：
  1. 先開 issue；
  2. 在同一個 PR 更新所有呼叫處與測試；
  3. 在 PR 裡通知其他負責人。
- **一個 issue＝一個分支（`issue-N-簡短名稱`）＝一個 PR**，PR 內寫 `Closes #N`。依賴的 PR 還沒合併時，
  就從它的分支開分支，並在 PR 說明。
- **不直接推到 `main`，也不合併自己的 PR**，由另一位組員審查並合併。PR 要小，一次只做一件事。
- **開 PR 前**：先跑完上面的測試與三個 `--help`，全部通過才開；PR 內容要寫改了什麼、並貼上測試結果。
- **硬性規定**：
  - 只用標準函式庫（Python 3.10+）。
  - 完全依照 BC/1.0 規範，不自行改動訊息欄位或命令列選項。
  - 絕不提交 `course/`（老師的檔案）、真實對局日誌、密碼金鑰或學號。
  - 穩定優先於強度。
  - 每位組員都要能解釋每一段程式；使用生成式 AI 工具要在報告中聲明。
