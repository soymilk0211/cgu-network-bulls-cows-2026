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

Status: all three modes work. That covers the shared protocol layer and CLI (#1), the human `client` (#2), the
`ai` with the v1 strategy (#3) and the `server` (#4).

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

## Hosting (server)

```
python bc.py server --port 5040 --players 4 --bots 1          # 4 players, 1 of them our AI
python bc.py server --players 2 --bots 2 --log-dir logs       # hosts one AI-only game and exits
```

- **Lobby:** the server listens on `--host` (default `0.0.0.0`, all IPv4 addresses) and waits for `--players`
  players (2–6). If the server's console is a terminal, typing `start` begins the game early once at least 2
  players are in. `--bots K` starts K of our own `ai` players (`BOT-1` … `BOT-K`) as subprocesses. They
  connect to `127.0.0.1` when `--host` is `0.0.0.0`, and the server collects them after every game.
- **Game:** the server runs the whole BC/1.0 state machine LOBBY → COMMIT → PLAY → END → OVER (§10–§14). That
  covers every time limit, the public consistency check after every feedback, reveals and verdicts, and the
  ranking. All error codes are sent in the spec's check order. Feedback is never shown before every answer
  of the turn is in.
- **After a game:** the server closes all connections of that game. `--games N` hosts N games one after the
  other (`0` = until stopped), then the process exits by itself.
- **Logs:** one JSON Lines file per game in `--log-dir`, named `<date>-<time>-<game_id>.jsonl`. Every record
  is `{"ts", "dir", "peer", "msg"}`, and `peer` is the `player_id`, or `IP:port` before `HELLO`. Unparsable
  input is logged as `{"raw": …}`. Only server logs go into this folder; the bots log to `client_logs/`.
- **Robustness:** asyncio runs everything in one thread, with one reading task and one writing task per
  connection. Each connection has a bounded send queue, so a client that stops reading is dropped instead of
  slowing everyone down. An error in one connection only closes that connection.
- **Bad options:** a bad `--players`, `--bots`, timeout, `--max-turns`, `--max-skips` or `--games` value
  makes the server exit with code 2.

Check a server with the teacher's self-test tool (keep it in the untracked `course/` folder):

```
python course/bc_selftest.py --server "python bc.py server" --json selftest.json
```

## Checkpoint 1: our client and AI against the reference server

`tools/checkpoint1.py` (standard library only) plays N games (default 3) with our `bc.py client` and our
`bc.py ai` against a BC/1.0 server, collects all logs into one dated folder and prints a summary. The server
is the teacher's reference `bc.py`, kept in the untracked `course/` folder. The tool never contains or
imports it; you either start it yourself or give its command.

```
# Mode 1: the server already runs. It needs --players 2, and --games 0 (or at least as many games).
python3 course/bc.py server --port 5040 --players 2 --games 0 --log-dir logs/reference
python3 tools/checkpoint1.py --host 127.0.0.1 --port 5040 --team GXX --server-log-dir logs/reference

# Mode 2: the tool starts the server and adds --host --port --players 2 --games N --log-dir itself.
python3 tools/checkpoint1.py --team GXX --games 3 --server-cmd "python3 course/bc.py server"
```

| Option | Meaning |
|---|---|
| `--host`, `--port` | the server (default `127.0.0.1:5040`) |
| `--team` | **required**: our group code (`GXX` above is a placeholder). Nothing in the repository hardcodes it |
| `--games` | number of games (default 3) |
| `--name-prefix` | names are `PREFIX` + `human` / `ai` (default `TEAM-`, e.g. `GXX-human`, `GXX-ai`) |
| `--out` | output folder (default `checkpoint1_out/`); every run gets a `<date>-<time>/` subfolder |
| `--server-cmd` | mode 2: the command that starts the server |
| `--server-log-dir` | mode 1: the running server's `--log-dir`; the logs of our games are copied |
| `--game-timeout`, `--python`, `--seed` | give up a game after N seconds (300); Python for `bc.py`; seed for the guesses |

The client is the real console client, driven through its stdin and stdout like a quick player: Enter for a
random secret (the commit is automatic), a random legal guess on our turn, Enter to accept the prefilled
feedback. The reveal is automatic in the client. Between games the tool waits 1 s and retries a refused
join, because a server that just ended a game refuses `HELLO` with `GAME_IN_PROGRESS` for a moment.

Each run folder holds `client_logs/` (our client and AI logs), `server_logs/`, `console/` (what every
process printed), `summary.txt` and `summary.json`. The summary lists every game with its `game_id`, end
reason, turns, our two players' ranks, and any `ERROR`, `TIMEOUT`, `FORFEIT`, `DQ`, failed reveal or skipped
turn. The exit code is 0 only if all games finished without such problems.

**Never commit** `checkpoint1_out/`, `logs/`, `client_logs/` (real game logs, all in `.gitignore`) or any
teacher file from `course/`. Submit the logs the way the course asks, not through git.

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
| 10. Server | `GameServer` (lobby, state machine, timeouts, reveals, ranking, bots, logs), `Connection` (bounded send queue), `ServerPlayer`, `hello_error`, `compute_ranking` | §5.3, §10–§14, §16.1 |
| 11. CLI | `build_parser` with the exact §5.2 options; `server_arg_problems` | §5.2 |

## Course materials

The teacher's spec PDF, reference server and `bc_selftest.py` are private course files. Keep them in a local
`course/` folder, which is listed in `.gitignore`. Never commit them.
