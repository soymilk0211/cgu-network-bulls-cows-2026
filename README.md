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

```
python -m pytest -q                      # if pytest is installed
python -m unittest discover -s tests -v  # standard library only
```

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
