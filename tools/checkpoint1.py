#!/usr/bin/env python3
"""Checkpoint 1 helper (issue #10): play N games with our client and our AI
against a BC/1.0 server, collect the logs, and print a summary.

Only the Python standard library is used.  The teacher's reference server is
never shipped or imported: the operator runs it (or names its command) from
the untracked course/ folder.

Two ways to run it:

  1. The server already runs (default).  Start it yourself, for example
         python3 course/bc.py server --port 5040 --players 2 --games 0 --log-dir logs/reference
     and then
         python3 tools/checkpoint1.py --port 5040 --team GXX --server-log-dir logs/reference

  2. The tool starts the server: give --server-cmd.  The tool adds
     --host, --port, --players 2, --games N and --log-dir <run>/server_logs:
         python3 tools/checkpoint1.py --team GXX --server-cmd "python3 course/bc.py server"

For each game the tool starts `bc.py client` and `bc.py ai` as subprocesses.
The client is driven through its stdin/stdout like a person would use it:
Enter for a random secret, a random legal guess on our turn, Enter to accept
the prefilled feedback.  The reveal is automatic in the client itself.

Everything goes into <--out>/<date>-<time>/ (default checkpoint1_out/, which
git ignores):
    client_logs/   our client and AI JSON Lines logs (written there directly)
    server_logs/   the server's logs (started server, or copied from --server-log-dir)
    console/       what each process printed
    summary.txt, summary.json
Never commit these folders: they are real game logs.
"""

import argparse
import json
import os
import queue
import random
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BC_PY = os.path.join(ROOT, "bc.py")
sys.path.insert(0, ROOT)

import bc  # noqa: E402  (our own bc.py: validators and ALL_NUMBERS)

JOIN_TIMEOUT = 10.0  # seconds for the client to get WELCOME
JOIN_RETRIES = 5  # a server between two games refuses HELLO with GAME_IN_PROGRESS
PAUSE = 1.0  # seconds between games, so the server can reopen its lobby
SERVER_START_TIMEOUT = 15.0


def build_parser():
    p = argparse.ArgumentParser(
        prog="checkpoint1.py",
        description="Play N games with our `bc.py client` (auto-driven) and `bc.py ai` against a "
                    "BC/1.0 server (e.g. the course reference server) and collect the logs.",
        epilog="Mode 1 (default): the server already listens at --host:--port; start it with "
               "--players 2 and --games 0 (or at least --games). Mode 2: --server-cmd starts it; "
               "the tool appends --host --port --players 2 --games N --log-dir. "
               "Never commit the output folder or any teacher file.")
    p.add_argument("--host", default="127.0.0.1", help="server address (default: %(default)s)")
    p.add_argument("--port", type=int, default=bc.DEFAULT_PORT, help="server port (default: %(default)s)")
    p.add_argument("--team", required=True,
                   help="our group code, e.g. GXX (required; nothing is hardcoded)")
    p.add_argument("--games", type=int, default=3, help="number of games to play (default: %(default)s)")
    p.add_argument("--name-prefix", default=None,
                   help="player names are PREFIX + 'human' / 'ai' (default: TEAM + '-')")
    p.add_argument("--out", default="checkpoint1_out",
                   help="output folder; each run gets a dated subfolder (default: %(default)s)")
    p.add_argument("--server-cmd", default=None,
                   help='start the server with this command, e.g. "python3 course/bc.py server"')
    p.add_argument("--server-log-dir", default=None,
                   help="mode 1: the running server's --log-dir; new files are copied to the output")
    p.add_argument("--game-timeout", type=float, default=300.0,
                   help="seconds before a game is given up (default: %(default)s)")
    p.add_argument("--python", default=sys.executable,
                   help="Python used to run bc.py (default: this Python)")
    p.add_argument("--seed", type=int, default=None, help="seed for the client's random guesses")
    return p


# ---------------------------------------------------------------------------
# Driving the human client
# ---------------------------------------------------------------------------


def answer_for_prompt(line, rng):
    """The text to type for one line printed by `bc.py client`, or None.

    The client prints every question on its own line starting with "? "
    (HumanClient.show_prompt).  We answer like a quick, honest player.
    """
    if not line.startswith("? "):
        return None
    if "YOUR TURN" in line:
        return rng.choice(bc.ALL_NUMBERS)  # any legal guess
    # Secret: Enter = random secret.  Feedback: Enter = the prefilled (honest) answer.
    # Confirm (only after a changed answer, which we never type): Enter = go back.
    return ""


class Proc:
    """A subprocess whose output is copied to a transcript file by a thread."""

    def __init__(self, cmd, cwd, transcript, stdin=False):
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.lines = queue.Queue()
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, encoding="utf-8",
                                     errors="replace", stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT,
                                     stdin=subprocess.PIPE if stdin else subprocess.DEVNULL)
        self.file = open(transcript, "a", encoding="utf-8")
        self.copier = threading.Thread(target=self._copy, daemon=True)
        self.copier.start()

    def _copy(self):
        for line in self.proc.stdout:
            self.file.write(line)
            self.file.flush()
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)  # end of output

    def type(self, text):
        try:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass  # the client already exited

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.copier.join(5)  # let the last output reach the transcript
        self.file.close()


def drive_client(client, rng, joined):
    """Thread: answer the client's questions until its output ends."""
    while True:
        line = client.lines.get()
        if line is None:
            return
        if line.startswith("Joined the lobby"):
            joined.set()
        answer = answer_for_prompt(line, rng)
        if answer is not None:
            client.type(answer)


# ---------------------------------------------------------------------------
# Reading our JSON Lines logs
# ---------------------------------------------------------------------------


def read_jsonl(path):
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except ValueError:
                pass
    return records


def summarize_log(path):
    """What one client/AI log says about its game (see the keys below)."""
    info = {"log": path, "player": None, "game_id": None, "over": None, "rank": None,
            "status": None, "errors": [], "skipped": 0, "guesses": 0, "reveal": None}
    for rec in read_jsonl(path):
        msg = rec.get("msg")
        if not isinstance(msg, dict):
            continue
        mtype = msg.get("type")
        if rec.get("dir") == "send":
            if mtype == "GUESS":
                info["guesses"] += 1
            continue
        if mtype == "WELCOME":
            info["player"] = msg.get("player_id")
        elif mtype == "GAME_START":
            info["game_id"] = msg.get("game_id")
        elif mtype == "ERROR":
            info["errors"].append(str(msg.get("code")))
        elif mtype == "TURN_RESULT":
            if msg.get("skipped") and msg.get("guesser") == info["player"]:
                info["skipped"] += 1
        elif mtype == "REVEALED" and msg.get("player") == info["player"]:
            info["reveal"] = msg.get("verdict")
        elif mtype == "GAME_OVER":
            info["over"] = {"reason": msg.get("reason"), "turns": msg.get("turns")}
            for row in msg.get("ranking") or []:
                if isinstance(row, dict) and row.get("player") == info["player"]:
                    info["rank"], info["status"] = row.get("rank"), row.get("status")
    return info


def problems_of(info, exit_code):
    """Short labels for everything that went wrong for one of our players."""
    found = [f"ERROR {code}" for code in info["errors"]]
    if info["over"] is None:
        found.append("no GAME_OVER")
    if info["status"] in ("FORFEIT", "DQ"):
        found.append(info["status"])
    if info["reveal"] not in (None, "OK"):
        found.append(f"reveal {info['reveal']}")
    if info["skipped"]:
        found.append(f"{info['skipped']} skipped turn(s)")
    if exit_code != 0:
        found.append(f"exit code {exit_code}")
    return found


# ---------------------------------------------------------------------------
# Running the games
# ---------------------------------------------------------------------------


def wait_for_server(host, port, timeout):
    """True once something accepts TCP connections at host:port."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def new_logs(folder, before):
    if not os.path.isdir(folder):
        return []
    return sorted(os.path.join(folder, f) for f in os.listdir(folder) if f not in before)


class Runner:
    def __init__(self, args, run_dir, out=print):
        self.args, self.run_dir, self.out = args, run_dir, out
        self.rng = random.Random(args.seed)
        prefix = args.team + "-" if args.name_prefix is None else args.name_prefix
        self.names = {"human": prefix + "human", "ai": prefix + "ai"}
        self.client_logs = os.path.join(run_dir, bc.CLIENT_LOG_DIR)
        self.console = os.path.join(run_dir, "console")
        os.makedirs(self.console, exist_ok=True)

    def bc_cmd(self, mode, name):
        a = self.args
        return [a.python, "-u", BC_PY, mode, "--host", a.host, "--port", str(a.port),
                "--name", name, "--team", a.team]

    def play_one(self, number):
        """Play game `number`.  Returns the result dict for the summary."""
        transcript = os.path.join(self.console, f"game-{number:02d}-{{}}.txt")
        for attempt in range(1, JOIN_RETRIES + 1):
            before = set(os.listdir(self.client_logs)) if os.path.isdir(self.client_logs) else set()
            client = Proc(self.bc_cmd("client", self.names["human"]), self.run_dir,
                          transcript.format("human"), stdin=True)
            joined = threading.Event()
            threading.Thread(target=drive_client, args=(client, self.rng, joined), daemon=True).start()
            end = time.monotonic() + JOIN_TIMEOUT
            while not joined.is_set() and client.proc.poll() is None and time.monotonic() < end:
                time.sleep(0.05)
            if joined.is_set():
                break
            client.stop()
            self.out(f"  game {number}: the client could not join (attempt {attempt}); retrying")
            time.sleep(PAUSE * 2)
        else:
            return {"game": number, "finished": False, "problems": ["could not join the server"],
                    "players": {}}

        ai = Proc(self.bc_cmd("ai", self.names["ai"]), self.run_dir, transcript.format("ai"))
        end = time.monotonic() + self.args.game_timeout
        codes = {}
        for kind, proc in (("human", client), ("ai", ai)):
            try:
                codes[kind] = proc.proc.wait(max(0.1, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                codes[kind] = None
            proc.stop()

        result = {"game": number, "players": {}, "problems": []}
        for path in new_logs(self.client_logs, before):
            # Log names are <date>-<time>-<kind>-<name>-<pid>.jsonl (bc.client_log_path).
            kind = os.path.basename(path).split("-")[2]
            if kind not in self.names:
                continue
            info = summarize_log(path)
            code = codes.get(kind)
            info["exit_code"] = code
            info["name"] = self.names[kind]
            if code is None:
                result["problems"].append(f"{kind}: no exit within --game-timeout")
            result["problems"] += [f"{kind}: {p}" for p in problems_of(info, code if code is not None else 0)]
            result["players"][kind] = info
        overs = [p["over"] for p in result["players"].values() if p["over"]]
        result["finished"] = len(result["players"]) == 2 and len(overs) == 2
        result["game_id"] = next((p["game_id"] for p in result["players"].values() if p["game_id"]), None)
        result["over"] = overs[0] if overs else None
        return result

    def run(self):
        results = []
        for number in range(1, self.args.games + 1):
            if number > 1:
                time.sleep(PAUSE)
            self.out(f"game {number}/{self.args.games} ...")
            results.append(self.play_one(number))
        return results


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def player_cell(info):
    if not info:
        return "-"
    rank = "-" if info.get("rank") is None else info["rank"]
    return f"{info.get('player') or '?'} rank {rank} {info.get('status') or '?'}"


def format_summary(args, run_dir, results, server_logs):
    finished = sum(1 for r in results if r["finished"])
    lines = [f"Checkpoint 1 summary: {finished}/{len(results)} games finished "
             f"(server {args.host}:{args.port}, team {args.team})", ""]
    lines.append(f"{'game':<5} {'game_id':<14} {'reason':<12} {'turns':>5}  "
                 f"{'human':<24} {'ai':<24} problems")
    for r in results:
        over = r.get("over") or {}
        game_id, reason = str(r.get("game_id") or "-"), str(over.get("reason") or "-")
        turns = str(over.get("turns", "-"))
        human, ai = player_cell(r["players"].get("human")), player_cell(r["players"].get("ai"))
        lines.append(f"{r['game']:<5} {game_id:<14} {reason:<12} {turns:>5}  {human:<24} {ai:<24} "
                     f"{'; '.join(r['problems']) or '-'}")
    problems = sum(len(r["problems"]) for r in results)
    lines += ["", f"ERROR / TIMEOUT / FORFEIT / DQ / skipped turns for our players: {problems}", "",
              f"Output folder: {run_dir}"]
    for r in results:
        for kind in ("human", "ai"):
            if kind in r["players"]:
                path = os.path.relpath(r["players"][kind]["log"], run_dir)
                lines.append(f"  game {r['game']} {kind:<5} {path}")
    for path in server_logs:
        lines.append(f"  server log   {os.path.relpath(path, run_dir)}")
    if not server_logs:
        lines.append("  (no server logs collected; use --server-cmd or --server-log-dir)")
    return "\n".join(lines)


def copy_server_logs(src, dest, since, game_ids):
    """Mode 1: copy the server's log files of our games.

    Only files written during this run that mention one of our game ids are
    copied, so other games on the same server stay out of our output.
    """
    copied = []
    if not src or not os.path.isdir(src):
        return copied
    os.makedirs(dest, exist_ok=True)
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if not os.path.isfile(path) or os.path.getmtime(path) < since:
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if any(gid in text for gid in game_ids):
            shutil.copy2(path, os.path.join(dest, name))
            copied.append(os.path.join(dest, name))
    return copied


def main(argv=None):
    args = build_parser().parse_args(argv)
    prefix = args.team + "-" if args.name_prefix is None else args.name_prefix
    for label, value in (("--team", args.team), ("human name", prefix + "human"), ("ai name", prefix + "ai")):
        if not bc.is_valid_text(value, 1, 20):
            print(f"error: {label} {value!r} must be 1-20 characters without control characters")
            return 2
    if args.games < 1:
        print("error: --games must be at least 1")
        return 2

    run_dir = os.path.abspath(os.path.join(args.out, time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(run_dir, exist_ok=True)
    server_log_dir = os.path.join(run_dir, "server_logs")
    started = time.time() - 1
    server = None
    if args.server_cmd:
        os.makedirs(os.path.join(run_dir, "console"), exist_ok=True)
        extra = ["--host", args.host, "--port", str(args.port), "--players", "2",
                 "--games", str(args.games), "--log-dir", server_log_dir]
        if os.name == "nt":  # Windows starts a process from one command line string
            cmd = args.server_cmd + " " + subprocess.list2cmdline(extra)
            print("starting the server: " + cmd)
        else:
            cmd = shlex.split(args.server_cmd) + extra
            print("starting the server: " + shlex.join(cmd))
        server = Proc(cmd, run_dir, os.path.join(run_dir, "console", "server.txt"))
    if not wait_for_server(args.host, args.port, SERVER_START_TIMEOUT if server else 3.0):
        print(f"error: no server is listening at {args.host}:{args.port}")
        if server:
            server.stop()
        return 1

    results = Runner(args, run_dir).run()

    if server:
        try:
            server.proc.wait(15)  # it exits by itself after --games games
        except subprocess.TimeoutExpired:
            pass
        server.stop()
        server_logs = new_logs(server_log_dir, set())
    else:
        game_ids = [r["game_id"] for r in results if r.get("game_id")]
        server_logs = copy_server_logs(args.server_log_dir, server_log_dir, started, game_ids)

    text = format_summary(args, run_dir, results, server_logs)
    print("\n" + text)
    with open(os.path.join(run_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"host": args.host, "port": args.port, "team": args.team, "games": results,
                   "server_logs": server_logs}, f, ensure_ascii=False, indent=2)
    ok = all(r["finished"] and not r["problems"] for r in results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
