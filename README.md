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

Status: the shared protocol layer and the command-line interface are done (issue #1). The `client` (#2),
`ai` (#3) and `server` (#4) modes are still to come.

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
| 3. Validators | `is_valid_number`, `is_int`, `is_valid_feedback`, `is_hex`, `LEGAL_FEEDBACK` | §9 |
| 4. Game math | `ALL_NUMBERS`, `score`, `make_commit`, `verify_reveal`, `filter_candidates` | §2.2, §3.1, §14 |
| 5. Message helpers | `make_pong`, `dispatch`, which ignores unknown message types | §8.3.6, §11.1 |
| 6. CLI | `build_parser` with the exact §5.2 options | §5.2 |

## Course materials

The teacher's spec PDF, reference server and `bc_selftest.py` are private course files. Keep them in a local
`course/` folder, which is listed in `.gitignore`. Never commit them.
