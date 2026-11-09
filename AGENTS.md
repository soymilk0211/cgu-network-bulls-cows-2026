# AGENTS.md: rules for contributors and AI coding tools

This file is for all three teammates and for every AI coding tool they use. Read it before changing anything.
It is the only rules file, so there is no tool-specific copy. Answer in the language the teammate uses; this
file is in English.

- **Source of truth:** the BC/1.0 spec PDF from the course. It is kept locally in `course/`, never in git. If
  an issue or this file conflicts with the spec, follow the spec and say so in the PR.
- **Priority:** stability over strength. A game with an `ERROR`, forfeit or disqualification costs far more
  than a weaker guess.

## 1. Layout

| Path | What |
|---|---|
| `bc.py` | the whole program (single entry point, §5.1), in numbered sections 1–10 (see the README code map) |
| `tests/test_protocol.py` | tests for the shared core (sections 1–5, CLI) |
| `tests/test_client.py` | tests for the human client, plus the shared test helpers `FakeServer`, `wait_for`, `PLAYERS`, `RULES`, `SALT2` |
| `tests/test_ai.py` | tests for the AI. It imports the helpers from `tests/test_client.py` |
| `README.md`, `requirements.txt`, `.gitignore`, `AGENTS.md` | docs and config. `requirements.txt` holds comments only (no runtime dependencies) |

## 2. Ownership

| Area | Owner | Code in `bc.py` | Tests | Issue |
|---|---|---|---|---|
| A: server mode | TBD | `run_server` (still a stub) and the future server section | `tests/test_server.py` (to come) | #4 |
| B: human client | TBD | section 8: `Prompt`, `parse_feedback`, `HELP_TEXT`, `HumanClient`; `run_client` | `tests/test_client.py` | #2 |
| C: AI | TBD | section 9: `AI_TIME_BUDGET`, `AI_SAMPLE_SIZE`, `pick_target`, `count_parts`, `most_parts_guess`, `choose_guess_v1`, `AIPlayer`; `run_ai` | `tests/test_ai.py` | #3 |

- **Inside your own area** you can change things freely, as long as the shared contracts below still hold.
  The area owner reviews every PR that touches their area.
- **Cross-area links** to keep in mind:
  - The server's `--bots K` will start `python bc.py ai --host H --port P --name BOT-k ...` as subprocesses
    (§5.3), so the `ai` command line and its exit behaviour are an interface that A depends on.
  - B and C both build on `PlayerSession` (section 7).
  - `tests/test_ai.py` imports helpers from `tests/test_client.py`.

## 3. Shared core (no single owner)

Shared means:
- `bc.py` sections 1–7: constants, errors, framing, validators, game math, message helpers, client log,
  `PlayerSession`, `read_rules`, `answer_deadline`;
- section 10 (CLI): `build_parser`, `force_utf8_console`, `check_identity`, `main`;
- the shared test helpers listed in §1.

To **change the signature or behaviour** of anything shared:
1. Open an issue first that describes the change and why.
2. Update all callers and their tests in the same PR.
3. Mention the other area owners in the PR and wait for their review.

Bug fixes that keep the contract, and new helpers, are normal PRs. They still need all tests green.

## 4. Interface contracts (shared core)

These match the code on branch `issue-3-ai`. If you change one, update this table in the same PR.

**Constants**

| Name | Contract |
|---|---|
| `PROTOCOL_VERSION` | `"BC/1.0"`, sent in `HELLO` |
| `DEFAULT_PORT` | `5040` |
| `MAX_LINE_BYTES` | `4096`: maximum bytes of one message **including** the final LF |
| `LEGAL_FEEDBACK` | frozenset of the 14 legal `(a, b)` pairs (`a+b <= 4`, no `(3, 1)`) |
| `ALL_NUMBERS` | tuple of the 5040 legal numbers as `str` (`"0123"` …) |
| `CLIENT_LOG_DIR` | `"client_logs"`: the client/AI log folder, never the server's `--log-dir` |
| `DEFAULT_RULES` | fallback rules: guess 60, feedback 60, commit 30, reveal 15, max_turns 120, max_skips 3 |

**Framing (section 2)**

| Signature | Contract |
|---|---|
| `LineBuffer(max_bytes=4096)` | collects received bytes. `.pending` = bytes waiting for an LF |
| `LineBuffer.feed(data) -> list[bytes]` | returns the complete lines, without the LF and without one CR before it. Empty and whitespace-only lines are dropped. Raises `LineTooLong` when a line (LF included) is longer than `max_bytes`, or when `max_bytes` bytes are buffered with no LF; the buffer is then cleared |
| `LineTooLong(complete=())` | `.complete` = the lines that arrived before the long one. Handle them first. The server maps this to fatal `LINE_TOO_LONG` |
| `BadJson` | raised by `decode`. The server maps it to non-fatal `BAD_JSON` |
| `decode(line) -> dict` | `line` is `bytes` (or `str`). Strict UTF-8 plus `json.loads` only. Raises `BadJson` for: invalid UTF-8, a BOM, invalid JSON, `NaN`/`±Infinity`, not an object, a missing or non-string `"type"`, absurd nesting. Unknown fields are kept |
| `encode(msg) -> bytes` | compact JSON (`ensure_ascii=False`, `allow_nan=False`) + `b"\n"`. Raises `ValueError` if the result is over 4096 bytes or contains NaN |
| `send_msg(sock, msg) -> bytes` | `encode` + `sock.sendall`. Returns the bytes sent; socket errors (`OSError`) propagate |

**Validators (section 3)**: all return `bool` and never raise.

| Signature | True when |
|---|---|
| `is_int(x)` | `x` is an `int` and not a `bool` |
| `is_valid_number(s)` | `s` is a `str` matching `[0-9]{4}` (ASCII only, no fullwidth digits) with 4 different digits |
| `is_valid_feedback(a, b)` | both `is_int` and `(a, b) in LEGAL_FEEDBACK` |
| `is_hex(s, length)` | `s` is a `str` of exactly `length` lowercase hex characters |
| `is_commit(s)` / `is_salt(s)` | `is_hex(s, 64)` / `is_hex(s, 32)` |
| `is_valid_text(s, min_len, max_len)` | `s` is a `str` of `min_len..max_len` characters with no Unicode category C (name/team 1–20, CHAT 1–200) |
| `is_supported_protocol(s)` | `s` is a `str` that fully matches `BC/1\.[0-9]+` |

**Game math (section 4)**

| Signature | Contract |
|---|---|
| `score(guess, secret) -> (a, b)` | A and B counts (§2.2). No validation: pass legal numbers |
| `commit_hash(secret, salt) -> str` | `sha256(f"{secret}:{salt}".encode("ascii")).hexdigest()`. Non-ASCII input raises `UnicodeEncodeError`, so validate first |
| `make_commit(secret) -> (salt, commit)` | new `secrets.token_hex(16)` salt on every call; `commit = commit_hash(secret, salt)` |
| `verify_reveal(commit, secret, salt, history) -> (verdict, bad_turns)` | `history` = list of `(turn_id, guess, a, b)`. Checks in §14.2 order: `"NO_REVEAL"` if secret/salt are missing or malformed, `"BAD_COMMIT"` if the hash differs, `"INCONSISTENT"` with the wrong `turn_id`s in history order, else `"OK"`. `bad_turns` is `[]` unless the verdict is `INCONSISTENT` |
| `filter_candidates(cands, guess, a, b) -> list` | new list of the `c` in `cands` with `score(guess, c) == (a, b)`, in the same order |

**Message helpers (section 5)**

| Signature | Contract |
|---|---|
| `make_pong(ping) -> dict` | `{"type": "PONG"}`, plus `"nonce"` copied from `ping` if it is present |
| `dispatch(handlers, msg) -> bool` | calls `handlers[msg["type"]](msg)` and returns `True`. Returns `False` (message ignored) for an unknown type. Handler exceptions propagate |

**Client log and display (section 6)**

| Signature | Contract |
|---|---|
| `JsonlLog(path)` | creates the folder and opens `path` for appending (UTF-8). If that fails it prints a warning and becomes a no-op. `.path` is the file path |
| `JsonlLog.write(direction, peer, msg)` | appends one `{"ts", "dir", "peer", "msg"}` line and flushes it. Thread-safe; ignores I/O errors |
| `JsonlLog.close()` | closes the file |
| `client_log_path(kind, name, log_dir="client_logs") -> str` | `<log_dir>/<YYYYmmdd-HHMMSS>-<kind>-<safe name>-<pid>.jsonl` |
| `printable(value, limit=200) -> str` | `str(value)[:limit]` with Unicode category C characters replaced by `?`. Use it before printing anything that came from the network |

**Player session (section 7)**: the base class of `HumanClient` and `AIPlayer`.

| Signature | Contract |
|---|---|
| `read_rules(rules) -> dict` | `DEFAULT_RULES`, overridden by each value from `GAME_START.rules` that is a positive `int`/`float` (not `bool`) |
| `answer_deadline(timeout, margin=1.0) -> float` | `time.monotonic() + max(timeout - margin, timeout / 2)`: when to stop waiting for our own answer |
| `PlayerSession(host, port, name, team, out=print, log_dir="client_logs")` | one connection = one game. The class attribute `kind` (`"human"`/`"ai"`) goes into `HELLO` and the log file name |
| `.run() -> int` | connects, sends `HELLO`, then handles events until done. Returns `0` after `GAME_OVER`, else `1` (connect failure, fatal `ERROR`, disconnect, other major version, `/quit`, Ctrl+C) |
| `.events` | `queue.Queue` of `(kind, data)`: the reader thread puts `"msg"` (dict), `"bad"` (str) and `"closed"` (reason). The human input thread adds `"input"` (str) |
| `.send(msg) -> bool` | **main thread only.** Sends and logs the message. On a socket error it returns `False` and sets `done` |
| `.send_commit(secret)` | sets `secret` and `salt` (via `make_commit`) and sends `COMMIT` |
| `.send_guess(turn_id, guess)` / `.send_feedback(turn_id, a, b)` | send `GUESS` / `FEEDBACK` with no validation; the caller validates |
| `.say(text)` | all output goes through here (subclasses may filter or prefix it) |
| automatic in the base class | `PONG` for `PING`; `REVEAL` at once for `REVEAL_REQUEST`; `BYE` and exit if `WELCOME` has another major version; a local `verify_reveal` of every `REVEALED`; unknown types ignored; a malformed message (`KeyError`/`TypeError`/`ValueError`/`AttributeError`/`IndexError` in a handler) is reported and skipped; never answers an `ERROR` |
| hook `begin_commit()` | **must** be implemented. Called on `GAME_START`; must lead to exactly one `send_commit` |
| hook `begin_guess(turn_id)` | **must** be implemented. Called only when `TURN.guesser == me` and we are not out; at most one `send_guess` |
| hook `begin_feedback(turn_id, guesser, guess)` | **must** be implemented. Called only when we are in `GUESS_MADE.responders`, not out, and committed; at most one `send_feedback` |
| hooks `start_input()`, `handle_input(text)`, `check_deadlines()`, `withdraw_prompt(reason=None)` | optional; no-ops in the base class. `check_deadlines` runs after every event and at least every `TICK` = 0.2 s |
| public state | `me`, `players`, `game_id`, `rules`, `order`, `alive`, `commits`, `history` (`{pid: [(turn_id, guess, a, b)]}`), `cands` (`{alive opponent pid: [possible secrets]}`, filtered by **every** `TURN_RESULT`), `secret`, `salt`, `out_of_game`, `game_over`, `done`. Only the `on_*` handlers change it; `reset_game()` runs on `GAME_START` |

**CLI (section 10)**

| Signature | Contract |
|---|---|
| `build_parser() -> argparse.ArgumentParser` | the exact §5.2 subcommands, option names and defaults; all timeouts are `type=float`. New options may be added (§5.2 allows it); existing names and defaults must not change |
| `force_utf8_console()` | reconfigures stdin/stdout/stderr to UTF-8 (`errors="replace"`) |
| `check_identity(args) -> bool` | `--name` and `--team` pass `is_valid_text(..., 1, 20)`; otherwise prints an error and returns `False` |
| `main(argv=None) -> int` | parses the arguments and calls `run_server` / `run_client` / `run_ai(args) -> int` |
| exit codes | `0` OK, `1` error / disconnect / not implemented, `2` bad `--name`/`--team` |

## 5. Workflow

- **One issue = one branch = one PR.** Name branches `issue-N-short-name` and put `Closes #N` in the PR
  (`Part of #N` if the PR covers only part of the issue).
- **Branch from an up-to-date `main`.** If the PR you depend on is not merged yet, branch from its branch, set
  that branch as the PR base, and say so at the top of the PR body.
- **Never push directly to `main`, never force-push `main`, and never merge your own PR.** Another teammate
  reviews and merges.
- **Keep PRs small.** One topic per PR, with no drive-by changes in other areas.
- **Before opening a PR,** run from the repo root. Everything must pass, and a PR with failing tests is never
  opened as done or merged:
  ```
  python -m pytest -q
  python -m unittest discover -s tests -v
  python bc.py server --help && python bc.py client --help && python bc.py ai --help
  ```
- **The PR body lists** what changed and why, the spec sections involved, the pasted test results, and notes
  on any manual runs (for example against the reference server).
- **Check `git status` before `git add`.** Add files by name; never `git add -A` or `git add .` blindly.
- **Do not decide alone** on choices marked "group to confirm" (for example asyncio vs threads for the
  server, or the AI v2 weights). Ask the group.

## 6. Hard rules

1. **Standard library only, Python 3.10+.** Grading runs Python 3.10 on Ubuntu with only
   `pip install -r requirements.txt` (§6.4.1). Do not use 3.11+ features. A third-party package needs a group
   decision and a pinned version in `requirements.txt`.
2. **Must work on Windows, macOS and Linux.** Always pass `encoding="utf-8"` for files, sockets and logs.
3. **Follow the BC/1.0 spec exactly.** No protocol extensions. Do not add, rename or remove message types,
   fields, error codes, or CLI option names/defaults on your own.
4. **Keep the single root `bc.py` as the entry point**; tests go in `tests/`. No extra packages or modules.
5. **Never commit:**
   - `course/`: the spec PDF, the reference server and `bc_selftest.py` are private teacher files. Do not copy
     or quote them at length either.
   - real game logs (`logs/`, `client_logs/`, `*.log`);
   - secrets or tokens;
   - student IDs, real names or the group number. Owners stay `TBD` here until the group fills them in.
6. **Handle network data safely:**
   - parse it with `json.loads` only (never `eval`/`exec`/`pickle`);
   - make secrets and salts with `secrets`, never `random`;
   - send with `sendall`;
   - validate every field (a `bool` is not an `int`);
   - never crash on bad input.
7. **Every teammate must be able to explain every line** in the solo Q&A (§6.4, §7.2), so keep the code
   simple and commented. Any use of a generative AI tool must be declared in the report (§7.2).
