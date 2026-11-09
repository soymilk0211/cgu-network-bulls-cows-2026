#!/usr/bin/env python3
"""bc.py - multiplayer Bulls & Cows (幾A幾B) over TCP, protocol BC/1.0.

One file, three subcommands (spec §5.1-5.2):

    python bc.py server [...]   game server / referee     (issue #4)
    python bc.py client [...]   human player, console     (issue #2)
    python bc.py ai     [...]   automatic AI player       (issue #3)

Only the Python standard library is used (Python 3.10+).

Layout of this file (search for the "=====" banners):
    1. Constants and errors
    2. Framing: one JSON object per line   (§8.2-8.3)
    3. Field validators                     (§9)
    4. Game math: score, commit, reveal     (§2.2, §3.1, §14)
    5. Message helpers: PING/PONG, dispatch (§8.3.6, §11.1)
    6. Client-side JSON Lines log           (§5.6)
    7. PlayerSession: connection + game state shared by client and AI (§11-12)
    8. HumanClient: console player          (§5.4)
    9. AIPlayer: automatic player, v1 most-parts / v2 entropy (§5.5)
   10. GameServer: the referee, asyncio     (§5.3, §10-§14)
   11. Command line interface               (§5.2)
"""

import argparse
import asyncio
import hashlib
import itertools
import json
import math
import os
import queue
import random
import re
import secrets
import socket
import sys
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

# =====================================================================
# 1. Constants and errors
# =====================================================================

PROTOCOL_VERSION = "BC/1.0"
DEFAULT_PORT = 5040
MAX_LINE_BYTES = 4096  # one message, counted in bytes, INCLUDING the final LF


class ProtocolError(Exception):
    """Base class for 'the peer sent us something invalid'."""


class LineTooLong(ProtocolError):
    """A message is longer than MAX_LINE_BYTES (server: fatal LINE_TOO_LONG, §13).

    `complete` holds the normal lines that arrived *before* the long one,
    so the caller can still handle them in order before giving up.
    """

    def __init__(self, complete=()):
        super().__init__(f"message longer than {MAX_LINE_BYTES} bytes")
        self.complete = list(complete)


class BadJson(ProtocolError):
    """A line is not a valid BC/1.0 JSON message (server: BAD_JSON, §13)."""


# =====================================================================
# 2. Framing: one JSON object per line (JSON Lines)        §8.2-8.3
# =====================================================================
#
# TCP is a byte stream, not a message stream: one recv() may return half a
# message, or three messages at once.  LineBuffer collects the bytes and
# hands back only complete lines.


class LineBuffer:
    """Collects received bytes and splits them into complete lines.

        buf = LineBuffer()
        for line in buf.feed(sock.recv(4096)):
            msg = decode(line)

    Rules (§8.3):
      * a message ends with LF (b"\\n"); one CR right before the LF is dropped;
      * empty lines (also lines of only spaces/tabs) are ignored;
      * a message may be at most MAX_LINE_BYTES bytes including the LF.
        If it is longer we raise LineTooLong.  We never keep more than
        MAX_LINE_BYTES bytes while waiting for an LF, so a peer that never
        sends LF cannot make us use unbounded memory.
    """

    def __init__(self, max_bytes=MAX_LINE_BYTES):
        self.max_bytes = max_bytes
        self._buf = bytearray()

    @property
    def pending(self):
        """Number of bytes received but not yet part of a complete line."""
        return len(self._buf)

    def feed(self, data):
        """Add received bytes; return the list of complete lines (as bytes)."""
        self._buf += data
        lines = []
        while True:
            end = self._buf.find(b"\n")
            if end < 0:
                break
            if end + 1 > self.max_bytes:  # +1 counts the LF itself
                self._buf.clear()
                raise LineTooLong(lines)
            line = bytes(self._buf[:end])
            del self._buf[: end + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if line.strip():  # skip empty / whitespace-only lines
                lines.append(line)
        # No LF yet.  If we already hold max_bytes, the LF would make the
        # message at least max_bytes + 1 long, so it can never be valid.
        if len(self._buf) >= self.max_bytes:
            self._buf.clear()
            raise LineTooLong(lines)
        return lines


def _reject_constant(name):
    # json.loads calls this for NaN, Infinity and -Infinity.  They are not
    # real JSON, so we treat the whole line as unparsable (§8.3.3).
    raise ValueError(f"{name} is not allowed in BC/1.0")


def decode(line):
    """Turn one received line (bytes, without LF) into a message dict.

    Raises BadJson if the line is not UTF-8, not JSON, contains
    NaN/Infinity, is not a JSON object, or has no string "type".
    Unknown extra fields are kept; receivers simply ignore them (§8.3.6).
    Only json.loads is used - never eval() or pickle (§16.1).
    """
    try:
        text = line.decode("utf-8") if isinstance(line, (bytes, bytearray)) else line
        # A UTF-8 BOM stays as U+FEFF here and json.loads rejects it.
        msg = json.loads(text, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BadJson(f"unparsable message: {exc}") from None
    if not isinstance(msg, dict):
        raise BadJson("a message must be a JSON object")
    if not isinstance(msg.get("type"), str):
        raise BadJson('a message needs a string field "type"')
    return msg


def encode(msg):
    """Turn a message dict into bytes ready to send (compact JSON + LF)."""
    text = json.dumps(msg, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    data = text.encode("utf-8") + b"\n"
    if len(data) > MAX_LINE_BYTES:
        raise ValueError(f"message is {len(data)} bytes, limit is {MAX_LINE_BYTES}")
    return data


def send_msg(sock, msg):
    """Send one message on a socket and return the bytes sent.

    sendall() keeps sending until every byte is out; a bare send() may send
    only part of the data (§16.3.5).
    """
    data = encode(msg)
    sock.sendall(data)
    return data


# =====================================================================
# 3. Field validators                                       §9, §16.3.3-4
# =====================================================================

_NUMBER_RE = re.compile(r"[0-9]{4}")  # [0-9] = ASCII digits only, unlike \d
_HEX_RE = re.compile(r"[0-9a-f]+")  # lowercase hex only
_PROTOCOL_RE = re.compile(r"BC/1\.[0-9]+")  # any BC/1.x is compatible (§9, §16.2)

# The 14 possible answers (a, b): a + b <= 4, and 3A1B is impossible because
# if 3 digits are in the right place the 4th cannot be a "B" (§2.2).
LEGAL_FEEDBACK = frozenset(
    (a, b) for a in range(5) for b in range(5) if a + b <= 4 and (a, b) != (3, 1)
)


def is_int(x):
    """True for a real int.  bool is a subclass of int in Python, so exclude it."""
    return isinstance(x, int) and not isinstance(x, bool)


def is_valid_number(s):
    """A secret/guess: a str of exactly 4 ASCII digits, all different.

    Leading zero is fine ("0275").  We do not use str.isdigit(), because it
    also accepts fullwidth digits like "０２７５".
    """
    return isinstance(s, str) and _NUMBER_RE.fullmatch(s) is not None and len(set(s)) == 4


def is_valid_feedback(a, b):
    """Feedback a, b: ints (not bool) forming one of the 14 legal pairs."""
    return is_int(a) and is_int(b) and (a, b) in LEGAL_FEEDBACK


def is_hex(s, length):
    """A str of exactly `length` lowercase hex characters."""
    return isinstance(s, str) and len(s) == length and _HEX_RE.fullmatch(s) is not None


def is_commit(s):
    """A commit is a SHA-256 hex digest: 64 lowercase hex characters (§3.1)."""
    return is_hex(s, 64)


def is_salt(s):
    """A salt is 16 random bytes as 32 lowercase hex characters (§3.1)."""
    return is_hex(s, 32)


def is_valid_text(s, min_len, max_len):
    """A str of min_len..max_len characters with no control characters.

    Used for name and team (1-20) and CHAT text (1-200).  "Control
    character" means any Unicode category starting with C: Cc, Cf, Cs,
    Co, Cn (§9).  Length is counted in characters, not bytes.
    """
    return (isinstance(s, str) and min_len <= len(s) <= max_len
            and not any(unicodedata.category(ch).startswith("C") for ch in s))


def is_supported_protocol(s):
    """True for any "BC/1.x" version string; major version 1 is what we speak."""
    return isinstance(s, str) and _PROTOCOL_RE.fullmatch(s) is not None


# =====================================================================
# 4. Game math                                          §2.2, §3.1, §14
# =====================================================================

# All 5040 legal numbers: 4 different digits out of 10, order matters.
ALL_NUMBERS = tuple("".join(p) for p in itertools.permutations("0123456789", 4))


def score(guess, secret):
    """How `guess` scores against `secret`: returns (a, b).

    a = right digit in the right place, b = right digit in the wrong place.
    Example (§2.2): score("0927", "0275") == (1, 2).
    """
    a = sum(1 for g, s in zip(guess, secret) if g == s)
    common = len(set(guess) & set(secret))
    return a, common - a


def commit_hash(secret, salt):
    """commit = SHA-256 of "secret:salt", as lowercase hex (§3.1)."""
    return hashlib.sha256(f"{secret}:{salt}".encode("ascii")).hexdigest()


def make_commit(secret):
    """Lock in a secret.  Returns (salt, commit).

    The salt comes from the `secrets` module (cryptographically strong),
    never from `random` (§16.3.7).  Keep the salt private until REVEAL.
    """
    salt = secrets.token_hex(16)
    return salt, commit_hash(secret, salt)


def verify_reveal(commit, secret, salt, history):
    """Check a revealed secret against its commit and past feedback (§14.2).

    history: list of (turn_id, guess, a, b) - the feedback this player gave.
    Pass secret=None, salt=None if no valid REVEAL arrived in time.
    Returns (verdict, bad_turns), checked in the order of §14.2:
      ("NO_REVEAL", [])       no well-formed secret + salt
      ("BAD_COMMIT", [])      SHA256(secret:salt) != commit
      ("INCONSISTENT", [ids]) hash ok, but feedback for these turns was wrong
      ("OK", [])              everything matches
    """
    if not (is_valid_number(secret) and is_salt(salt)):
        return "NO_REVEAL", []
    if commit_hash(secret, salt) != commit:
        return "BAD_COMMIT", []
    bad_turns = [turn_id for turn_id, guess, a, b in history if score(guess, secret) != (a, b)]
    if bad_turns:
        return "INCONSISTENT", bad_turns
    return "OK", []


def filter_candidates(cands, guess, a, b):
    """Keep only the possible secrets that would have answered (a, b) to guess."""
    return [c for c in cands if score(guess, c) == (a, b)]


# =====================================================================
# 5. Message helpers                                    §8.3.6, §11.1
# =====================================================================


def make_pong(ping):
    """Reply to a PING: a PONG that echoes the nonce if there is one (§11.1)."""
    pong = {"type": "PONG"}
    if "nonce" in ping:
        pong["nonce"] = ping["nonce"]
    return pong


def dispatch(handlers, msg):
    """Call handlers[msg["type"]](msg).  Returns False if the type is unknown.

    Clients and AIs must ignore message types they do not know (§8.3.6),
    so an unknown type is simply skipped.
    """
    handler = handlers.get(msg.get("type"))
    if handler is None:
        return False
    handler(msg)
    return True


# =====================================================================
# 6. Client-side JSON Lines log                                  §5.6
# =====================================================================
#
# Our client and AI keep their own log in the same record format as the
# server log.  It goes to CLIENT_LOG_DIR, never into the server's --log-dir.

CLIENT_LOG_DIR = "client_logs"


class JsonlLog:
    """One {"ts", "dir", "peer", "msg"} JSON record per line, flushed at once.

    Both the reader thread and the main thread write to it, so a lock keeps
    the lines from mixing.  A failing disk must never stop the game, so
    write errors are ignored.
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        try:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8")
        except OSError as exc:
            print(f"[warning] cannot write the log file {path}: {exc}")
            self._file = None

    def write(self, direction, peer, msg):
        if self._file is None:
            return
        record = {"ts": round(time.time(), 3), "dir": direction, "peer": peer, "msg": msg}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                self._file.write(line)
                self._file.flush()
            except (OSError, ValueError):
                pass

    def close(self):
        with self._lock:
            if self._file is not None:
                self._file.close()


def client_log_path(kind, name, log_dir=CLIENT_LOG_DIR):
    """client_logs/20261110-143000-human-G00-小明-12345.jsonl (unique per run)."""
    safe_name = re.sub(r"[^\w-]+", "_", name)[:20] or "player"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(log_dir, f"{stamp}-{kind}-{safe_name}-{os.getpid()}.jsonl")


def printable(value, limit=200):
    """Text from the network, made safe to print: no control characters.

    A bad server could put terminal escape codes into a name or a chat line.
    """
    text = str(value)[:limit]
    return "".join("?" if unicodedata.category(ch).startswith("C") else ch for ch in text)


# =====================================================================
# 7. Player session: shared by the human client and the AI     §11-12
# =====================================================================

SOFTWARE = "cgu-bc/0.2"
DEFAULT_RULES = {"guess_timeout": 60, "feedback_timeout": 60, "commit_timeout": 30,
                 "reveal_timeout": 15, "max_turns": 120, "max_skips": 3}


def read_rules(rules):
    """Take the rules from GAME_START, falling back to defaults for bad values."""
    result = dict(DEFAULT_RULES)
    if isinstance(rules, dict):
        for key in result:
            value = rules.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                result[key] = value
    return result


def answer_deadline(timeout, margin=1.0):
    """When to stop waiting for our own answer, measured from now.

    The server's timer starts when it sends the message, and the network
    adds delay, so we aim to answer `margin` seconds early (§12).  For very
    short timeouts we keep at least half of the time.
    """
    return time.monotonic() + max(timeout - margin, timeout / 2)


class PlayerSession:
    """One connection to a BC/1.0 server as a player.

    It does the networking and keeps the public game state.  The human
    client (HumanClient) and the AI (AIPlayer) are subclasses.  They differ
    only in three hooks: begin_commit, begin_guess, begin_feedback.

    Threads:
      * reader thread: recv() -> LineBuffer -> decode -> self.events queue
      * input thread (human only): stdin lines -> self.events queue
      * main thread, run(): handles one event at a time, updates the state
        and does ALL the sending.  PING is answered here at once, because
        the main thread never waits for the keyboard.
    """

    kind = "human"
    TICK = 0.2  # seconds; how often run() checks deadlines when nothing happens

    def __init__(self, host, port, name, team, out=print, log_dir=CLIENT_LOG_DIR):
        self.host, self.port, self.name, self.team = host, port, name, team
        self.peer = f"{host}:{port}"
        self.out = out
        self.log = JsonlLog(client_log_path(self.kind, name, log_dir))
        self.events = queue.Queue()
        self.sock = None
        self.done = False  # True -> run() stops
        self.game_over = False  # True once GAME_OVER arrived (normal end)
        self.handlers = {
            "WELCOME": self.on_welcome, "LOBBY": self.on_lobby,
            "GAME_START": self.on_game_start, "COMMITS": self.on_commits,
            "TURN": self.on_turn, "GUESS_MADE": self.on_guess_made,
            "TURN_RESULT": self.on_turn_result, "REVEAL_REQUEST": self.on_reveal_request,
            "REVEALED": self.on_revealed, "GAME_OVER": self.on_game_over,
            "ERROR": self.on_error, "PING": self.on_ping, "CHAT": self.on_chat,
        }  # any other type (PONG, future types) is ignored (§8.3.6)
        self.me = None  # our player_id, from WELCOME
        self.players = {}  # player_id -> {"id", "name", "team", "kind"}
        self.reset_game()

    def reset_game(self):
        self.game_id = None
        self.rules = dict(DEFAULT_RULES)
        self.order = []  # turn order (from COMMITS: only real participants)
        self.alive = []  # players still in the game
        self.commits = {}  # player_id -> commit
        self.history = {}  # player_id -> [(turn_id, guess, a, b)], public feedback
        self.cands = {}  # opponent player_id -> list of still-possible secrets
        self.secret = None
        self.salt = None  # stays in memory only; sent only in REVEAL
        self.out_of_game = False  # eliminated / forfeited / DQ -> spectator

    # ----- output -------------------------------------------------------

    def say(self, text):
        self.out(text)

    def label(self, pid):
        """'p2(阿明)' for display; our own id gets ', you'."""
        info = self.players.get(pid)
        name = printable(info.get("name"), 20) if isinstance(info, dict) else "?"
        you = ", you" if pid == self.me else ""
        return f"{printable(pid, 20)}({name}{you})"

    def labels(self, pids):
        return ", ".join(self.label(p) for p in pids) or "-"

    # ----- network ------------------------------------------------------

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self.sock.settimeout(None)  # from now on the reader thread blocks in recv()
        threading.Thread(target=self._reader, name="net-reader", daemon=True).start()
        self.send({"type": "HELLO", "protocol": PROTOCOL_VERSION, "name": self.name,
                   "team": self.team, "kind": self.kind, "software": SOFTWARE})

    def send(self, msg):
        """Send one message (main thread only).  Returns False if the link is gone."""
        if self.sock is None:
            return False
        try:
            send_msg(self.sock, msg)
        except OSError as exc:
            self.say(f"[network] sending failed: {exc}")
            self.done = True
            return False
        self.log.write("send", self.peer, msg)
        return True

    def _reader(self):
        """Reader thread: turn the byte stream into events for the main thread."""
        buf = LineBuffer()
        reason = "the server closed the connection"
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                try:
                    lines, too_long = buf.feed(data), False
                except LineTooLong as exc:
                    lines, too_long = exc.complete, True
                for line in lines:
                    try:
                        msg = decode(line)
                    except BadJson:
                        raw = line.decode("utf-8", "replace")
                        self.log.write("recv", self.peer, {"raw": raw})
                        self.events.put(("bad", raw))
                        continue
                    self.log.write("recv", self.peer, msg)
                    self.events.put(("msg", msg))
                if too_long:
                    self.log.write("recv", self.peer, {"raw": "(line longer than 4096 bytes)"})
                    self.events.put(("bad", "(line longer than 4096 bytes)"))
        except OSError as exc:
            reason = f"connection error: {exc}"
        self.events.put(("closed", reason))

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.log.close()

    # ----- main loop ----------------------------------------------------

    def run(self):
        """Play one game.  Returns 0 after a normal GAME_OVER, else 1."""
        try:
            self.connect()
        except OSError as exc:
            self.say(f"Cannot connect to {self.peer}: {exc}")
            self.close()
            return 1
        self.say(f"Connected to {self.peer} as {self.name!r} (team {self.team}). "
                 f"Log file: {self.log.path}")
        self.start_input()
        try:
            while not self.done:
                try:
                    kind, data = self.events.get(timeout=self.TICK)
                except queue.Empty:
                    kind, data = "tick", None
                self.handle_event(kind, data)
                self.check_deadlines()
        except KeyboardInterrupt:
            self.say("\nInterrupted - leaving the game.")
            self.send({"type": "BYE"})
        finally:
            self.close()
        return 0 if self.game_over else 1

    def handle_event(self, kind, data):
        if kind == "msg":
            self.handle_message(data)
        elif kind == "bad":
            self.say(f"[warning] ignored an unparsable line from the server: {printable(data, 80)}")
        elif kind == "closed":
            if not self.game_over:
                self.say(f"[network] {data} before the game ended. Exiting.")
            self.done = True
        elif kind == "input":
            self.handle_input(data)

    def handle_message(self, msg):
        """Run the handler for msg["type"]; unknown types are ignored (§8.3.6).

        A message from a buggy server with missing or wrongly typed fields
        is reported and skipped instead of crashing the client.
        """
        try:
            dispatch(self.handlers, msg)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            self.say(f"[warning] ignored a malformed {printable(msg.get('type'), 30)} "
                     f"message ({type(exc).__name__}: {printable(exc, 80)})")

    # ----- hooks for subclasses -----------------------------------------

    def start_input(self):
        """Start reading the keyboard (human only)."""

    def handle_input(self, text):
        """A line typed by the user (human only)."""

    def check_deadlines(self):
        """Called after every event and every TICK seconds (human only)."""

    def withdraw_prompt(self, reason=None):
        """Cancel a question we are asking the user (human only)."""

    def begin_commit(self):
        raise NotImplementedError

    def begin_guess(self, turn_id):
        raise NotImplementedError

    def begin_feedback(self, turn_id, guesser, guess):
        raise NotImplementedError

    # ----- actions shared by human and AI -------------------------------

    def send_commit(self, secret):
        """Lock in our secret: send only the hash; keep secret and salt (§3.1)."""
        self.secret = secret
        self.salt, commit = make_commit(secret)
        self.send({"type": "COMMIT", "commit": commit})

    def send_guess(self, turn_id, guess):
        self.send({"type": "GUESS", "turn_id": turn_id, "guess": guess})

    def send_feedback(self, turn_id, a, b):
        self.send({"type": "FEEDBACK", "turn_id": turn_id, "a": a, "b": b})

    # ----- message handlers ---------------------------------------------

    def on_welcome(self, msg):
        self.me = msg["player_id"]
        proto = msg.get("protocol")
        if not is_supported_protocol(proto):
            if isinstance(proto, str) and proto.startswith("BC/") and not proto.startswith("BC/1."):
                self.say(f"The server speaks {printable(proto, 20)}, we speak BC/1.x. Leaving.")
                self.send({"type": "BYE"})
                self.done = True
                return
            self.say(f"[warning] unexpected protocol string {printable(proto, 20)!r}; continuing.")
        self.say(f"Joined the lobby. Our player id is {self.me}.")

    def on_lobby(self, msg):
        players = msg["players"]
        self.players = {p["id"]: p for p in players}
        self.say(f"[lobby] {len(players)}/{msg.get('max_players', '?')} players: "
                 f"{self.labels(p['id'] for p in players)}")

    def on_game_start(self, msg):
        self.reset_game()
        self.game_id = msg.get("game_id")
        self.players.update({p["id"]: p for p in msg["players"]})
        self.order = list(msg["order"])
        self.rules = read_rules(msg.get("rules"))
        r = self.rules
        self.say(f"\n=== Game {printable(self.game_id, 64)} starts. Turn order: "
                 f"{' -> '.join(self.label(p) for p in self.order)}")
        self.say(f"    time limits: guess {r['guess_timeout']}s, feedback {r['feedback_timeout']}s, "
                 f"commit {r['commit_timeout']}s; max {r['max_turns']} turns")
        self.begin_commit()

    def on_commits(self, msg):
        self.withdraw_prompt()
        self.order = list(msg["order"])
        self.commits = dict(msg["commits"])
        self.alive = list(self.order)
        self.history = {pid: [] for pid in self.order}
        self.cands = {pid: list(ALL_NUMBERS) for pid in self.order if pid != self.me}
        self.say(f"All commits are in. Players: {self.labels(self.order)}")
        if self.me not in self.order:
            self.out_of_game = True
            self.say("We are not in this game (no commit in time).")

    def on_turn(self, msg):
        turn_id, guesser = msg["turn_id"], msg["guesser"]
        self.alive = list(msg["alive"])
        self.withdraw_prompt()
        self.say(f"\n--- Turn {turn_id}: {self.label(guesser)} guesses. Alive: {self.labels(self.alive)}")
        if guesser == self.me and not self.out_of_game:
            self.begin_guess(turn_id)

    def on_guess_made(self, msg):
        turn_id, guesser, guess = msg["turn_id"], msg["guesser"], msg["guess"]
        responders = msg["responders"]
        self.say(f"{self.label(guesser)} guessed {printable(guess, 8)}.")
        if self.me in responders and not self.out_of_game and self.secret is not None:
            self.begin_feedback(turn_id, guesser, guess)

    def on_turn_result(self, msg):
        turn_id, guesser, guess = msg["turn_id"], msg["guesser"], msg.get("guess")
        self.withdraw_prompt(f"turn {turn_id} is over")
        if msg.get("skipped") or guess is None:
            self.say(f"Turn {turn_id}: {self.label(guesser)} did not guess in time - skipped.")
        else:
            parts = []
            for r in msg["results"]:
                pid, a, b = r["player"], r["a"], r["b"]
                if not is_valid_feedback(a, b):
                    continue
                self.history.setdefault(pid, []).append((turn_id, guess, a, b))
                if pid in self.cands:
                    self.cands[pid] = filter_candidates(self.cands[pid], guess, a, b)
                parts.append(f"{self.label(pid)} {a}A{b}B")
            self.say(f"Turn {turn_id}: {self.label(guesser)} guessed {printable(guess, 8)} -> "
                     + ("; ".join(parts) or "no answers"))
        for key, text in (("eliminated", "was guessed (4A0B) and is out"),
                          ("forfeited", "forfeited"), ("disqualified", "was disqualified")):
            for pid in msg.get(key, []):
                self.say(f"  x {self.label(pid)} {text}")
                if pid in self.alive:
                    self.alive.remove(pid)
                self.cands.pop(pid, None)
                if pid == self.me:
                    self.out_of_game = True
                    self.say("  You are out of the game - now watching as a spectator.")
        if self.cands:
            self.say("  candidates left: " + ", ".join(
                f"{self.label(p)} {len(c)}" for p, c in self.cands.items()))

    def on_reveal_request(self, msg):
        if self.secret is None:
            self.say("[warning] REVEAL_REQUEST, but we never committed a secret.")
            return
        self.send({"type": "REVEAL", "secret": self.secret, "salt": self.salt})  # at once (§12.5)
        self.say(f"Reveal requested ({printable(msg.get('reason'), 20)}): "
                 f"sent our secret {self.secret} and salt automatically.")

    def on_revealed(self, msg):
        pid, verdict = msg["player"], msg.get("verdict")
        secret, salt = msg.get("secret"), msg.get("salt")
        ours, bad = verify_reveal(self.commits.get(pid), secret, salt, self.history.get(pid, []))
        line = f"  reveal {self.label(pid)}: secret {printable(secret, 8)}, verdict {printable(verdict, 20)}"
        if msg.get("bad_turns"):
            line += f", bad turns {printable(msg.get('bad_turns'), 60)}"
        if ours != verdict:
            line += f"   [!] our own check says {ours}" + (f" {bad}" if bad else "")
        self.say(line)

    def on_game_over(self, msg):
        self.withdraw_prompt()
        self.say(f"\n===== GAME OVER ({printable(msg.get('reason'), 20)}, "
                 f"{printable(msg.get('turns'), 6)} turns) =====")
        for row in msg["ranking"]:
            rank = row.get("rank")
            rank = "-" if rank is None else rank
            out = row.get("out_turn")
            when = "" if out is None else f" (out in turn {out})"
            mark = "   <- you" if row.get("player") == self.me else ""
            self.say(f"  {rank:>2}  {self.label(row.get('player'))}  "
                     f"{printable(row.get('status'), 10)}{when}{mark}")
        self.game_over = True
        self.done = True

    def on_error(self, msg):
        # Never answer an ERROR (§13); just show it.
        fatal = msg.get("fatal") is True
        self.say(f"[server error] {printable(msg.get('code'), 30)}: {printable(msg.get('message'))}"
                 + (" (fatal)" if fatal else ""))
        if fatal:
            self.say("The server ends our connection after a fatal error. Exiting.")
            self.done = True

    def on_ping(self, msg):
        self.send(make_pong(msg))

    def on_chat(self, msg):
        self.say(f"[chat] {self.label(msg.get('from'))}: {printable(msg.get('text'))}")


# =====================================================================
# 8. Human client (console)                                      §5.4
# =====================================================================


@dataclass
class Prompt:
    """A question we are waiting for the user to answer."""

    kind: str  # "secret", "guess", "feedback" or "confirm"
    question: str
    deadline: float  # time.monotonic() value; after it the prompt is withdrawn
    turn_id: int | None = None
    data: dict = field(default_factory=dict)
    warned: bool = False  # the "10 seconds left" reminder was printed (or not needed)


_FEEDBACK_RE = re.compile(r"([0-4])\s*[aA]\s*([0-4])\s*[bB]|([0-4])\s*[\s,]\s*([0-4])")


def parse_feedback(text):
    """'1A2B', '1a2b', '1 2' or '1,2' -> (1, 2); None if not a legal answer."""
    m = _FEEDBACK_RE.fullmatch(text.strip())
    if not m:
        return None
    a, b = (m.group(1), m.group(2)) if m.group(1) is not None else (m.group(3), m.group(4))
    pair = (int(a), int(b))
    return pair if pair in LEGAL_FEEDBACK else None


HELP_TEXT = """Commands (any time):
  /chat TEXT   send a chat message to everyone
  /cands       show how many secrets are still possible for each opponent
  /quit        leave the game (counts as a disconnect / forfeit)
  /help        show this help"""


class HumanClient(PlayerSession):
    """Console player.  COMMIT and REVEAL are automatic; guesses and feedback
    are typed by the user, who is asked only when it is their turn (§5.4)."""

    kind = "human"

    def __init__(self, *args, use_stdin=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_stdin = use_stdin
        self.prompt = None  # the Prompt we are waiting on, or None

    # ----- keyboard -----------------------------------------------------

    def start_input(self):
        self.say("Type /help for commands.")
        if self.use_stdin:
            threading.Thread(target=self._input_reader, name="stdin-reader", daemon=True).start()

    def _input_reader(self):
        """Input thread: blocking readline() here, never in the main thread (§16.3.2)."""
        while True:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                return
            if not line:  # stdin closed
                return
            self.events.put(("input", line))

    def ask(self, kind, question, deadline, turn_id=None, **data):
        self.prompt = Prompt(kind, question, deadline, turn_id, data)
        # A "10 seconds left" reminder only makes sense for longer time limits.
        self.prompt.warned = deadline - time.monotonic() < 20
        self.show_prompt()

    def show_prompt(self):
        p = self.prompt
        left = max(0, p.deadline - time.monotonic())
        self.say(f"? {p.question}  [{left:.0f}s left]")

    def withdraw_prompt(self, reason=None):
        if self.prompt is not None:
            if reason:
                self.say(f"(prompt withdrawn: {reason})")
            self.prompt = None

    def check_deadlines(self):
        p = self.prompt
        if p is None:
            return
        left = p.deadline - time.monotonic()
        if left <= 0:
            self.prompt = None
            self.on_prompt_expired(p)
        elif left <= 10 and not p.warned:
            p.warned = True
            self.say(f"(!) {left:.0f} seconds left to answer: {p.question}")

    def on_prompt_expired(self, p):
        if p.kind == "secret":
            secret = secrets.choice(ALL_NUMBERS)
            self.say(f"(time is almost up - using the random secret {secret})")
            self.send_commit(secret)
        elif p.kind == "guess":
            self.say("(time is up - prompt withdrawn; the server will skip your turn)")
        else:
            self.say("(time is up - prompt withdrawn; a missing feedback means FORFEIT)")

    def handle_input(self, text):
        text = text.strip()
        if text.startswith("/"):
            self.command(text)
            return
        p = self.prompt
        if p is None:
            if text:
                self.say("(nothing to answer right now - type /help for commands)")
            return
        if p.kind == "secret":
            self.answer_secret(text)
        elif p.kind == "guess":
            self.answer_guess(p, text)
        elif p.kind == "feedback":
            self.answer_feedback(p, text)
        elif p.kind == "confirm":
            self.answer_confirm(p, text)

    def command(self, text):
        cmd, _, rest = text.partition(" ")
        rest = rest.strip()
        if cmd in ("/chat", "/say"):
            if is_valid_text(rest, 1, 200):
                self.send({"type": "CHAT", "text": rest})
            else:
                self.say("(chat text must be 1-200 characters without control characters)")
        elif cmd == "/cands":
            if self.cands:
                self.say("candidates left: " + ", ".join(
                    f"{self.label(p)} {len(c)}" for p, c in self.cands.items()))
            else:
                self.say("(no candidate information yet)")
        elif cmd == "/quit":
            self.say("Leaving the game.")
            self.send({"type": "BYE"})
            self.done = True
        else:
            self.say(HELP_TEXT)

    # ----- COMMIT: automatic, the user only picks the number (§5.4.2) -----

    def begin_commit(self):
        deadline = answer_deadline(self.rules["commit_timeout"], margin=2.0)
        self.ask("secret", "Choose your secret: 4 different digits (e.g. 0275), "
                           "or just press Enter for a random one", deadline)

    def answer_secret(self, text):
        if text == "":
            text = secrets.choice(ALL_NUMBERS)
        elif not is_valid_number(text):
            self.say("(need exactly 4 different digits 0-9, e.g. 0275)")
            self.show_prompt()
            return
        self.prompt = None
        self.send_commit(text)
        self.say(f"Your secret is {text}. Only its hash (commit) was sent; "
                 f"the salt stays in memory until a reveal.")

    # ----- GUESS: only when TURN.guesser is us (§5.4.3) ---------------------

    def begin_guess(self, turn_id):
        if self.cands:
            self.say("  candidates left: " + ", ".join(
                f"{self.label(p)} {len(c)}" for p, c in self.cands.items()))
        deadline = answer_deadline(self.rules["guess_timeout"])
        self.ask("guess", f"Turn {turn_id} - YOUR TURN. Type your guess (4 different digits)",
                 deadline, turn_id)

    def answer_guess(self, p, text):
        if not is_valid_number(text):
            self.say("(need exactly 4 different digits 0-9, e.g. 0275)")
            self.show_prompt()
            return
        self.prompt = None
        self.send_guess(p.turn_id, text)

    # ----- FEEDBACK: prefilled, a changed value needs confirmation (§5.4.4) ---

    def begin_feedback(self, turn_id, guesser, guess, deadline=None):
        a, b = score(guess, self.secret)
        if deadline is None:
            deadline = answer_deadline(self.rules["feedback_timeout"])
        self.ask("feedback",
                 f"{self.label(guesser)} guessed {guess}; your secret is {self.secret} -> {a}A{b}B. "
                 f"Press Enter to send {a}A{b}B (or type another answer like 1A2B). "
                 f"No answer in time = FORFEIT!",
                 deadline, turn_id, guesser=guesser, guess=guess, a=a, b=b)

    def answer_feedback(self, p, text):
        if text == "":
            pair = (p.data["a"], p.data["b"])
        else:
            pair = parse_feedback(text)
            if pair is None:
                self.say("(type an answer like 1A2B or '1 2'; a+b <= 4 and 3A1B is impossible)")
                self.show_prompt()
                return
        if pair == (p.data["a"], p.data["b"]):
            self.prompt = None
            self.send_feedback(p.turn_id, *pair)
            self.say(f"Sent {pair[0]}A{pair[1]}B.")
            return
        # The user changed the computed answer: ask once more (§5.4.4).
        data = dict(p.data, chosen=pair)
        self.ask("confirm",
                 f"Your secret {self.secret} gives {p.data['a']}A{p.data['b']}B, but you typed "
                 f"{pair[0]}A{pair[1]}B. A wrong answer gets you DISQUALIFIED at the reveal. "
                 f"Type yes to send {pair[0]}A{pair[1]}B anyway, anything else to go back",
                 p.deadline, p.turn_id, **data)

    def answer_confirm(self, p, text):
        if text.lower() in ("y", "yes"):
            a, b = p.data["chosen"]
            self.prompt = None
            self.send_feedback(p.turn_id, a, b)
            self.say(f"Sent {a}A{b}B.")
        else:
            self.begin_feedback(p.turn_id, p.data["guesser"], p.data["guess"], deadline=p.deadline)


# =====================================================================
# 9. AI player                                                   §5.5
# =====================================================================
#
# The AI keeps, for every alive opponent, the list of secrets that are still
# possible ("candidates").  PlayerSession.on_turn_result already shrinks these
# lists with EVERY public result, not only our own guesses (§16.3.8).
#
# Two strategies, chosen with `ai --strategy` (v2 is described further below).
#
# v1 strategy (the spec's baseline, Kooi 2005 "most parts"):
#   1. target = the alive opponent with the fewest candidates;
#   2. guess  = the target candidate that splits the target's candidates into
#               the most different (a, b) answers.
# Big candidate lists are estimated on a random sample, and the search stops
# at a time budget, so a decision never takes long.

AI_TIME_BUDGET = 1.0  # seconds per guess at most (spec: "within 2 s", §5.5)
AI_SAMPLE_SIZE = 500  # secrets used to estimate the parts of a big candidate list
# The strategy uses `random` only to shuffle and sample - nothing secret.
# Our own secret and salt always come from `secrets` (§16.3.7).


def pick_target(cands):
    """The opponent with the fewest candidates (ties: smallest player id).

    cands: {player_id: [possible secrets]}.  Empty lists are skipped (that
    player lied and will be disqualified).  Returns None if nobody is left.
    """
    options = [pid for pid, c in cands.items() if c]
    if not options:
        return None
    return min(options, key=lambda pid: (len(cands[pid]), pid))


def count_parts(guess, secrets_list):
    """How many different (a, b) answers `guess` gets from these secrets."""
    return len({score(guess, s) for s in secrets_list})


def most_parts_guess(cands, budget=AI_TIME_BUDGET, rng=random):
    """Kooi's most-parts choice among `cands` (§5.5), within `budget` seconds.

    Candidates are tried in random order; when time runs out we return the
    best one found so far.  14 parts is the maximum, so we stop early there.
    """
    if len(cands) <= 2:  # nothing to split: just try to hit it now
        return cands[0]
    if len(cands) == len(ALL_NUMBERS):  # no information yet: all guesses are equal
        return rng.choice(cands)
    deadline = time.monotonic() + budget
    sample = cands if len(cands) <= AI_SAMPLE_SIZE else rng.sample(cands, AI_SAMPLE_SIZE)
    best_parts = min(len(LEGAL_FEEDBACK), len(sample))  # the best that is possible
    order = list(cands)
    rng.shuffle(order)
    best, best_count = order[0], 0
    for guess in order:
        parts = count_parts(guess, sample)
        if parts > best_count:
            best, best_count = guess, parts
            if parts == best_parts:
                break
        if time.monotonic() > deadline:
            break
    return best


def choose_guess_v1(cands, budget=AI_TIME_BUDGET, rng=random):
    """v1: most-parts guess for the opponent with the fewest candidates."""
    target = pick_target(cands)
    if target is None:
        return rng.choice(ALL_NUMBERS)
    return most_parts_guess(cands[target], budget, rng)


# v2 strategy (issue #11): weighted multi-opponent entropy.
#   1. target = the opponent with the fewest candidates, as in v1.  If only one
#      candidate is left, we know its secret: guess it at once.
#   2. Every guess in a pool gets the score
#          H(target) + AI_V2_WEIGHT * (sum of H(o) for the other alive opponents o)
#      H(x) is the entropy, in bits, of the answers opponent x could give.  All
#      alive opponents answer every guess, so one guess teaches us about
#      everybody; v1 only looks at the target.
#   3. The pool: the target's candidates first (only they can hit the target, and
#      on equal scores the first guess wins), then some candidates of each other
#      opponent, then some random numbers (a number nobody can have may still
#      split the lists better).
# Big lists are estimated on random samples.  If the scoring does not finish in
# its share of the time budget, v1 decides with the time that is left.

AI_V2_WEIGHT = 0.5  # weight of each non-target opponent's entropy
AI_V2_SAMPLE = 200  # secrets per opponent used to estimate an entropy
AI_V2_POOL = 150  # target candidates scored as guesses
AI_V2_EXTRA = 75  # candidates of each other opponent, and random numbers, added to the pool
AI_V2_SHARE = 0.6  # v2 may use this part of the budget; on a timeout v1 gets the rest
AI_STRATEGIES = ("v1", "v2")
DEFAULT_STRATEGY = "v2"  # chosen with tools/ai_experiment.py (see the README)


def answer_entropy(guess, secrets_list):
    """Entropy in bits of the (a, b) answers `guess` gets from these secrets.

    0 means every secret gives the same answer, so we learn nothing.  The more
    evenly the secrets spread over different answers, the higher the entropy
    and the fewer secrets stay possible on average after the answer.
    `secrets_list` must not be empty.
    """
    n = len(secrets_list)
    counts = Counter([score(guess, s) for s in secrets_list])
    return math.log2(n) - sum(c * math.log2(c) for c in counts.values()) / n


def limited(items, limit, rng=random):
    """A list of `items`, or a random sample of `limit` of them if there are more."""
    return list(items) if len(items) <= limit else rng.sample(list(items), limit)


def entropy_guess(cands, target, deadline, rng=random):
    """The v2 choice: the pool guess with the best weighted entropy.

    Returns None if time.monotonic() passes `deadline` before every guess in
    the pool is scored.
    """
    others = [c for pid, c in cands.items() if pid != target and c]  # skip empty lists
    target_sample = limited(cands[target], AI_V2_SAMPLE, rng)
    other_samples = [limited(c, AI_V2_SAMPLE, rng) for c in others]
    pool = limited(cands[target], AI_V2_POOL, rng)
    for c in others:
        pool += limited(c, AI_V2_EXTRA, rng)
    pool += rng.sample(ALL_NUMBERS, AI_V2_EXTRA)
    best, best_score = None, -1.0
    for guess in dict.fromkeys(pool):  # drops repeated guesses, keeps the order
        if time.monotonic() > deadline:
            return None
        value = answer_entropy(guess, target_sample)
        value += AI_V2_WEIGHT * sum(answer_entropy(guess, s) for s in other_samples)
        if value > best_score:
            best, best_score = guess, value
    return best


def choose_guess_v2(cands, budget=AI_TIME_BUDGET, rng=random, stats=None):
    """v2: weighted multi-opponent entropy, with v1 as the fallback on a timeout.

    If `stats` is a dict, stats["fallback"] is set to True when v1 decided.
    """
    start = time.monotonic()
    target = pick_target(cands)
    if target is None:
        return rng.choice(ALL_NUMBERS)
    if len(cands[target]) == 1:
        return cands[target][0]  # we know the target's secret
    if len(cands[target]) == len(ALL_NUMBERS):
        return rng.choice(ALL_NUMBERS)  # no information yet: all guesses are equal
    guess = entropy_guess(cands, target, start + budget * AI_V2_SHARE, rng)
    if guess is None:  # out of time: v1 with what is left of the budget
        if stats is not None:
            stats["fallback"] = True
        return choose_guess_v1(cands, max(0.0, budget - (time.monotonic() - start)), rng)
    return guess


class AIPlayer(PlayerSession):
    """Fully automatic player: random secret, instant feedback and reveal, v1 or v2 guesses."""

    kind = "ai"
    # With --quiet only lines starting like this are printed.
    QUIET_KEEP = ("[server error]", "[network]", "[warning]", "Cannot connect", "The server",
                  "\n=====")

    def __init__(self, *args, quiet=False, strategy=DEFAULT_STRATEGY, **kwargs):
        if strategy not in AI_STRATEGIES:
            raise ValueError(f"unknown AI strategy {strategy!r}")
        self.quiet = quiet
        self.strategy = strategy
        super().__init__(*args, **kwargs)

    def say(self, text):
        if not self.quiet or text.startswith(self.QUIET_KEEP):
            self.out(f"[{printable(self.name, 20)}] {text.lstrip()}")

    def begin_commit(self):
        self.send_commit(secrets.choice(ALL_NUMBERS))  # unpredictable secret

    def begin_feedback(self, turn_id, guesser, guess):
        # Always the honest answer, sent at once (§5.5).
        self.send_feedback(turn_id, *score(guess, self.secret))

    def begin_guess(self, turn_id):
        start = time.monotonic()
        # Stay far below the guess timeout, even with very short test timeouts.
        budget = min(AI_TIME_BUDGET, self.rules["guess_timeout"] * 0.25)
        stats = {}
        try:
            if self.strategy == "v2":
                guess = choose_guess_v2(self.cands, budget, stats=stats)
            else:
                guess = choose_guess_v1(self.cands, budget)
        except Exception as exc:  # noqa: BLE001 - a bug must never cost us the turn
            self.say(f"[warning] strategy failed ({type(exc).__name__}: {exc}); guessing at random")
            guess = None
        if not is_valid_number(guess):
            guess = secrets.choice(ALL_NUMBERS)
        self.send_guess(turn_id, guess)
        how = "v2 ran out of time, v1 decided" if stats.get("fallback") else self.strategy
        self.say(f"AI guess {guess} ({how}, took {time.monotonic() - start:.2f}s; "
                 f"target sizes: {sorted(len(c) for c in self.cands.values())})")


# =====================================================================
# 10. Server (referee)                                  §5.3, §10-§14
# =====================================================================
#
# Concurrency: asyncio, everything in ONE thread (no locks needed).
#   * handle_connection()  one task per TCP connection: read bytes ->
#                          LineBuffer -> decode -> handle_message()
#   * Connection._write_loop()  one task per connection: sends queued
#                          messages.  The queue is bounded, so a client that
#                          stops reading is dropped instead of slowing others
#                          down (§16.1.2).
#   * run_game()           the state machine LOBBY -> COMMIT -> PLAY -> END
#                          -> OVER (§10).  It waits with wait_until(); the
#                          message handlers only check a message, store it
#                          and call wake().
# All time limits are measured here, on the server (§16.1.4).

HELLO_TIMEOUT = 10.0  # seconds from accept() to a valid HELLO (§12.1)
MAX_ERRORS = 20  # the 21st error on one connection also ends it (§13)
SEND_QUEUE_LIMIT = 500  # queued messages for one client before we drop it (§16.1.2)
BOT_EXIT_TIMEOUT = 5.0  # seconds a bot may take to exit after a game before it is killed
FLUSH_TIMEOUT = 2.0  # seconds to let the last messages (GAME_OVER) go out


def seconds_value(x):
    """60.0 -> 60 and 1.5 -> 1.5, so GAME_START.rules looks like the spec's examples."""
    return int(x) if float(x).is_integer() else float(x)


def hello_error(msg, phase, joined, max_players):
    """Check a HELLO in the order of §12.1.3.  Returns (code, text) or None if OK.

    joined: the players already in the lobby (objects with a .name).
    Every error found here is fatal.
    """
    if not all(isinstance(msg.get(k), str) for k in ("protocol", "name", "team", "kind")):
        return "BAD_FIELD", "HELLO needs string fields protocol, name, team and kind"
    if not is_supported_protocol(msg["protocol"]):
        return "VERSION_UNSUPPORTED", f"this server speaks {PROTOCOL_VERSION}"
    if not (is_valid_text(msg["name"], 1, 20) and is_valid_text(msg["team"], 1, 20)
            and msg["kind"] in ("human", "ai")):
        return "INVALID_VALUE", ("name and team must be 1-20 characters without control "
                                 "characters; kind must be human or ai")
    if phase != "LOBBY":
        return "GAME_IN_PROGRESS", "a game is running; try again later"
    if len(joined) >= max_players:
        return "ROOM_FULL", "the lobby is full"
    if any(p.name == msg["name"] for p in joined):  # exact match, no normalisation (§9)
        return "NAME_TAKEN", "this name is already used in this game"
    return None


def compute_ranking(entries):
    """Final ranking (§14.3).  entries: [(player_id, status, out_turn)] in turn order.

    SURVIVOR first, then GUESSED (later out = better), then FORFEIT (later out =
    better).  Equal keys share a rank (1, 2, 2, 4).  DQ players come last with
    rank None.  Python's sort is stable, so ties stay in turn order.
    """
    group = {"SURVIVOR": 0, "GUESSED": 1, "FORFEIT": 2}

    def key(entry):
        _, status, out_turn = entry
        return (group[status], 0 if status == "SURVIVOR" else -out_turn)

    ranked = [e for e in entries if e[1] != "DQ"]
    keys = [key(e) for e in ranked]
    rows = []
    for entry in sorted(ranked, key=key):
        rank = 1 + sum(1 for k in keys if k < key(entry))
        rows.append({"player": entry[0], "status": entry[1], "out_turn": entry[2], "rank": rank})
    for pid, status, out_turn in entries:
        if status == "DQ":
            rows.append({"player": pid, "status": "DQ", "out_turn": out_turn, "rank": None})
    return rows


class Connection:
    """The server side of one TCP connection."""

    def __init__(self, server, reader, writer):
        self.server = server
        self.reader, self.writer = reader, writer
        address = writer.get_extra_info("peername")
        self.address = f"{address[0]}:{address[1]}" if address else "?"
        self.player = None  # a ServerPlayer once HELLO was accepted
        self.errors = 0
        self.closed = False
        self.queue = asyncio.Queue(maxsize=SEND_QUEUE_LIMIT)
        self.writer_task = asyncio.create_task(self._write_loop())

    @property
    def peer(self):
        """For the log: player_id after HELLO, "IP:port" before (§5.6)."""
        return self.player.pid if self.player else self.address

    def send(self, msg):
        """Queue one message.  Never blocks: a full queue drops the client."""
        if self.closed:
            return
        self.server.log("send", self.peer, msg)
        try:
            self.queue.put_nowait(encode(msg))
        except asyncio.QueueFull:
            self.server.say(f"{self.peer} does not read its messages - dropping the connection")
            self.abort()

    def error(self, code, text, fatal=False):
        """Send one ERROR (§13).  Fatal -> close.  The 21st error -> TOO_MANY_ERRORS."""
        self.errors += 1
        self.send({"type": "ERROR", "code": code, "message": text, "fatal": fatal})
        if fatal:
            self.close()
        elif self.errors > MAX_ERRORS:
            self.send({"type": "ERROR", "code": "TOO_MANY_ERRORS",
                       "message": f"more than {MAX_ERRORS} errors", "fatal": True})
            self.close()

    def close(self):
        """Close after the queued messages are sent."""
        if self.closed:
            return
        self.closed = True
        try:
            self.queue.put_nowait(None)  # None = "stop after everything before it"
        except asyncio.QueueFull:
            self.abort()
        self.server.wake()

    def abort(self):
        """Close at once, dropping unsent data (used for clients that do not read)."""
        self.closed = True
        self.writer.transport.abort()
        self.writer_task.cancel()
        self.server.wake()

    async def _write_loop(self):
        try:
            while True:
                data = await self.queue.get()
                if data is None:
                    break
                self.writer.write(data)
                await self.writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.closed = True
            try:
                self.writer.close()
            except (ConnectionError, OSError, RuntimeError):
                pass
            self.server.wake()


class ServerPlayer:
    """What the server knows about one player in the current game."""

    def __init__(self, pid, name, team, kind, conn):
        self.pid, self.name, self.team, self.kind, self.conn = pid, name, team, kind, conn
        self.status = "LOBBY"  # LOBBY -> ALIVE -> GUESSED / FORFEIT / DQ, or REMOVED
        self.commit = None
        self.history = []  # public feedback: [(turn_id, guess, a, b)]
        self.cands = list(ALL_NUMBERS)  # secrets that fit all of its feedback (§14.1)
        self.skips = 0  # consecutive skipped turns
        self.out_turn = None
        self.reveal = None  # (secret, salt) once a well-formed REVEAL arrived

    @property
    def connected(self):
        return not self.conn.closed

    def public(self):
        return {"id": self.pid, "name": self.name, "team": self.team, "kind": self.kind}


class GameServer:
    """BC/1.0 referee: hosts --games games one after the other (§5.3, §12)."""

    def __init__(self, args, out=print):
        self.args = args
        self.out = out
        self.port = None  # the real listening port (useful with --port 0)
        self.changed = None  # asyncio.Event, created in serve()
        self.log_file = None
        self.bot_procs = []
        self.games_played = 0
        self.new_game_state()

    def new_game_state(self):
        self.phase = "LOBBY"
        self.game_id = None
        self.players = {}  # player_id -> ServerPlayer, in join order
        self.next_id = 1  # player ids restart at p1 every game (§9)
        self.order = []  # turn order of the real participants (COMMITS.order)
        self.start_requested = False  # console "start"
        self.turn_id = 0
        self.last_guesser = None
        self.guesser = None
        self.guess = None
        self.guess_open = False
        self.responders = []
        self.feedback = {}  # player_id -> (a, b), kept secret until all are in (§3)
        self.feedback_open = False
        self.reveal_waiting = set()

    # ----- small helpers -------------------------------------------------

    def say(self, text):
        self.out(f"[server] {text}")

    def log(self, direction, peer, msg):
        if self.log_file is not None:
            self.log_file.write(direction, peer, msg)

    def wake(self):
        """Something changed: let wait_until() check its condition again."""
        if self.changed is not None:
            self.changed.set()

    async def wait_until(self, condition, timeout):
        """Wait until condition() is true or `timeout` seconds pass (None = forever)."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while not condition():
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return False
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                pass
        return True

    def in_game(self):
        """Players who get broadcasts: everyone except those removed in COMMIT (§11.3)."""
        return [p for p in self.players.values() if p.status != "REMOVED"]

    def broadcast(self, msg):
        for p in self.in_game():
            p.conn.send(msg)

    def broadcast_lobby(self):
        self.broadcast({"type": "LOBBY", "players": [p.public() for p in self.players.values()],
                        "min_players": 2, "max_players": self.args.players})

    def alive_ids(self):
        return [pid for pid in self.order if self.players[pid].status == "ALIVE"]

    def in_turn_order(self, pids):
        return sorted(pids, key=self.order.index)

    def rules(self):
        a = self.args
        return {"guess_timeout": seconds_value(a.guess_timeout),
                "feedback_timeout": seconds_value(a.feedback_timeout),
                "commit_timeout": seconds_value(a.commit_timeout),
                "reveal_timeout": seconds_value(a.reveal_timeout),
                "max_turns": a.max_turns, "max_skips": a.max_skips}

    # ----- connections ---------------------------------------------------

    async def handle_connection(self, reader, writer):
        """One task per connection: read, split into lines, handle each message."""
        conn = Connection(self, reader, writer)
        loop = asyncio.get_running_loop()
        hello_deadline = loop.time() + HELLO_TIMEOUT
        buf = LineBuffer()
        try:
            while not conn.closed:
                timeout = None if conn.player else hello_deadline - loop.time()
                try:
                    if timeout is not None and timeout <= 0:
                        raise asyncio.TimeoutError
                    data = await asyncio.wait_for(reader.read(65536), timeout)
                except asyncio.TimeoutError:
                    conn.error("TIMEOUT", f"no valid HELLO within {HELLO_TIMEOUT:g} seconds", fatal=True)
                    break
                if not data:
                    break  # the client closed the connection
                try:
                    lines, too_long = buf.feed(data), False
                except LineTooLong as exc:
                    lines, too_long = exc.complete, True
                for line in lines:
                    if conn.closed:
                        break
                    self.handle_line(conn, line)
                if too_long and not conn.closed:
                    self.log("recv", conn.peer, {"raw": f"(line longer than {MAX_LINE_BYTES} bytes)"})
                    conn.error("LINE_TOO_LONG", f"a message may be at most {MAX_LINE_BYTES} bytes",
                               fatal=True)
        except (ConnectionError, OSError):
            pass
        except Exception as exc:  # noqa: BLE001 - one bad connection must never stop the server
            self.say(f"internal error on {conn.peer}: {type(exc).__name__}: {exc}")
        finally:
            conn.close()
            self.on_disconnect(conn)

    def handle_line(self, conn, line):
        try:
            msg = decode(line)
        except BadJson:
            self.log("recv", conn.peer, {"raw": line.decode("utf-8", "replace")})
            conn.error("BAD_JSON", "not a JSON object with a string field type")
            return
        self.log("recv", conn.peer, msg)
        mtype = msg["type"]
        if conn.player is None:  # before HELLO (§12.1.2)
            if mtype == "HELLO":
                self.on_hello(conn, msg)
            else:
                conn.error("UNEXPECTED", "the first message must be HELLO", fatal=True)
            return
        if self.phase == "OVER":
            return  # messages after GAME_OVER are ignored (§12.6)
        handlers = {
            "HELLO": lambda c, m: c.error("UNEXPECTED", "you already sent HELLO"),
            "COMMIT": self.on_commit, "GUESS": self.on_guess, "FEEDBACK": self.on_feedback,
            "REVEAL": self.on_reveal, "CHAT": self.on_chat,
            "PING": lambda c, m: c.send(make_pong(m)), "PONG": lambda c, m: None,
            "BYE": lambda c, m: c.close(),  # BYE = disconnect (§12.6)
        }
        handler = handlers.get(mtype)
        if handler is None:
            conn.error("UNKNOWN_TYPE", f"unknown message type {printable(mtype, 30)}")
        else:
            handler(conn, msg)

    def on_disconnect(self, conn):
        p = conn.player
        if p is not None and self.phase == "LOBBY" and self.players.get(p.pid) is p:
            del self.players[p.pid]  # leaving the lobby = gone (§12.1.5)
            self.say(f"{p.pid} {p.name} left the lobby")
            self.broadcast_lobby()
        self.wake()  # in a game, the next decision point handles it (§12.6)

    # ----- message handlers (check order of §13) --------------------------

    def on_hello(self, conn, msg):
        problem = hello_error(msg, self.phase, list(self.players.values()), self.args.players)
        if problem:
            conn.error(*problem, fatal=True)
            return
        pid = f"p{self.next_id}"
        self.next_id += 1
        player = ServerPlayer(pid, msg["name"], msg["team"], msg["kind"], conn)
        self.players[pid] = player
        conn.player = player
        conn.send({"type": "WELCOME", "protocol": PROTOCOL_VERSION, "player_id": pid, "server": SOFTWARE})
        self.say(f"{pid} {printable(player.name, 20)} ({printable(player.team, 20)}, {player.kind}) "
                 f"joined: {len(self.players)}/{self.args.players}")
        self.broadcast_lobby()
        self.wake()

    def on_commit(self, conn, msg):
        p, commit = conn.player, msg.get("commit")
        if not isinstance(commit, str):
            conn.error("BAD_FIELD", "COMMIT needs a string field commit")
        elif self.phase != "COMMIT" or p.status != "ALIVE":
            conn.error("UNEXPECTED", "COMMIT is not accepted now")
        elif p.commit is not None:
            conn.error("DUPLICATE", "you already sent COMMIT")
        elif not is_commit(commit):
            conn.error("INVALID_VALUE", "commit must be 64 lowercase hex characters")
        else:
            p.commit = commit
            self.wake()

    def on_guess(self, conn, msg):
        p, turn_id, guess = conn.player, msg.get("turn_id"), msg.get("guess")
        if not is_int(turn_id) or not isinstance(guess, str):
            conn.error("BAD_FIELD", "GUESS needs an integer turn_id and a string guess")
        elif self.phase != "PLAY":
            conn.error("UNEXPECTED", "not in the play phase")
        elif turn_id != self.turn_id:
            conn.error("STALE_TURN", f"the current turn is {self.turn_id}")
        elif p.pid != self.guesser:
            conn.error("NOT_YOUR_TURN", f"the guesser of this turn is {self.guesser}")
        elif not self.guess_open:
            conn.error("UNEXPECTED", "this turn's guess was already made or timed out")
        elif not is_valid_number(guess):
            conn.error("INVALID_VALUE", "guess must be 4 different digits")
        else:
            self.guess = guess
            self.guess_open = False
            self.wake()

    def on_feedback(self, conn, msg):
        p = conn.player
        turn_id, a, b = msg.get("turn_id"), msg.get("a"), msg.get("b")
        if not (is_int(turn_id) and is_int(a) and is_int(b)):
            conn.error("BAD_FIELD", "FEEDBACK needs integer turn_id, a and b")
        elif self.phase != "PLAY":
            conn.error("UNEXPECTED", "not in the play phase")
        elif turn_id != self.turn_id:
            conn.error("STALE_TURN", f"the current turn is {self.turn_id}")
        elif not self.feedback_open or p.pid not in self.responders:
            conn.error("UNEXPECTED", "no feedback is expected from you now")
        elif p.pid in self.feedback:
            conn.error("DUPLICATE", "you already answered this turn")
        elif not is_valid_feedback(a, b):
            conn.error("INVALID_VALUE", "a+b must be at most 4 and 3A1B is impossible")
        else:
            self.feedback[p.pid] = (a, b)
            self.wake()

    def on_reveal(self, conn, msg):
        p, secret, salt = conn.player, msg.get("secret"), msg.get("salt")
        if not isinstance(secret, str) or not isinstance(salt, str):
            conn.error("BAD_FIELD", "REVEAL needs string fields secret and salt")
        elif p.reveal is not None:
            conn.error("DUPLICATE", "you already revealed")
        elif p.pid not in self.reveal_waiting:
            conn.error("UNEXPECTED", "no reveal is requested from you")
        elif not (is_valid_number(secret) and is_salt(salt)):
            conn.error("INVALID_VALUE", "secret must be 4 different digits, salt 32 lowercase hex")
        else:
            p.reveal = (secret, salt)  # checked later; a wrong one is a verdict, not an ERROR
            self.reveal_waiting.discard(p.pid)
            self.wake()

    def on_chat(self, conn, msg):
        text = msg.get("text")
        if not isinstance(text, str):
            conn.error("BAD_FIELD", "CHAT needs a string field text")
        elif not is_valid_text(text, 1, 200):
            conn.error("INVALID_VALUE", "text must be 1-200 characters without control characters")
        else:
            self.broadcast({"type": "CHAT", "from": conn.player.pid, "text": text})

    # ----- one game: LOBBY -> COMMIT -> PLAY -> END -> OVER (§10) ----------

    async def run_game(self):
        self.new_game_state()
        self.game_id = secrets.token_hex(6)  # 12 hex characters (§9)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.log_file = JsonlLog(os.path.join(self.args.log_dir, f"{stamp}-{self.game_id}.jsonl"))
        self.say(f"lobby open (game {self.game_id}); waiting for {self.args.players} players")
        await self.start_bots()

        # LOBBY (§12.1)
        await self.wait_until(lambda: len(self.players) >= self.args.players
                              or (self.start_requested and len(self.players) >= 2), None)

        # COMMIT (§12.2)
        self.phase = "COMMIT"
        order = list(self.players)
        random.shuffle(order)  # the turn order needs no secrecy, so `random` is fine
        for p in self.players.values():
            p.status = "ALIVE"
        self.broadcast({"type": "GAME_START", "game_id": self.game_id,
                        "players": [self.players[pid].public() for pid in order],
                        "order": order, "rules": self.rules()})
        self.say(f"game starts; turn order {order}")
        everyone = list(self.players.values())
        await self.wait_until(lambda: all(p.commit is not None or not p.connected for p in everyone),
                              self.args.commit_timeout)
        for p in everyone:
            if p.commit is None or not p.connected:
                if p.connected:
                    p.conn.error("TIMEOUT", "no COMMIT in time; you are removed from this game", fatal=True)
                p.status = "REMOVED"
        self.order = [pid for pid in order if self.players[pid].status == "ALIVE"]
        if len(self.order) < 2:
            self.finish("ABORTED", 0, [])
            return
        self.broadcast({"type": "COMMITS", "order": self.order,
                        "commits": {pid: self.players[pid].commit for pid in self.order}})

        # PLAY (§12.3)
        self.phase = "PLAY"
        reason = None
        while reason is None:
            await self.play_turn()
            if len(self.alive_ids()) <= 1:
                reason = "LAST_PLAYER"
            elif self.turn_id >= self.args.max_turns:
                reason = "MAX_TURNS"

        # END (§12.4)
        self.phase = "END"
        survivors = self.alive_ids()
        if survivors:
            await self.collect_reveals(survivors, "GAME_END")
        entries = []
        for pid in self.order:
            p = self.players[pid]
            entries.append((pid, "SURVIVOR" if p.status == "ALIVE" else p.status, p.out_turn))
        self.finish(reason, self.turn_id, compute_ranking(entries))

    def next_guesser(self):
        """The first alive player after the previous guesser; turn 1: order[0] (§12.3.1)."""
        start = 0 if self.last_guesser is None else self.order.index(self.last_guesser) + 1
        for i in range(len(self.order)):
            pid = self.order[(start + i) % len(self.order)]
            if self.players[pid].status == "ALIVE":
                return pid
        return None

    async def play_turn(self):
        a, P = self.args, self.players
        self.turn_id += 1
        turn_id = self.turn_id
        guesser = self.next_guesser()
        self.last_guesser = guesser
        G = P[guesser]
        alive = self.alive_ids()
        self.guesser, self.guess, self.guess_open = guesser, None, True
        self.broadcast({"type": "TURN", "turn_id": turn_id, "guesser": guesser, "alive": alive})

        # Steps 2-3: wait for the guess; stop early if the guesser is gone.
        await self.wait_until(lambda: self.guess is not None or not G.connected, a.guess_timeout)
        self.guess_open = False
        guess = self.guess
        results, eliminated, forfeited, disqualified = [], [], [], []
        if guess is None:
            if G.connected:
                G.skips += 1
                if G.skips >= a.max_skips:
                    forfeited.append(guesser)
        else:
            # Steps 4-6: collect ALL feedback before showing any of it (§3).
            G.skips = 0
            self.responders = [pid for pid in alive if pid != guesser]
            self.feedback = {}
            self.feedback_open = True
            self.broadcast({"type": "GUESS_MADE", "turn_id": turn_id, "guesser": guesser,
                            "guess": guess, "responders": self.responders})
            await self.wait_until(lambda: all(pid in self.feedback or not P[pid].connected
                                              for pid in self.responders), a.feedback_timeout)
            self.feedback_open = False
            for pid in self.responders:  # in turn order
                if pid not in self.feedback:
                    forfeited.append(pid)
                    continue
                fa, fb = self.feedback[pid]
                results.append({"player": pid, "a": fa, "b": fb})
                q = P[pid]
                q.history.append((turn_id, guess, fa, fb))
                q.cands = filter_candidates(q.cands, guess, fa, fb)  # public check (§14.1)
                if not q.cands:
                    disqualified.append(pid)
                elif (fa, fb) == (4, 0):
                    eliminated.append(pid)
        # Step 7: alive but disconnected (the guesser too) -> FORFEIT.
        for pid in alive:
            if not P[pid].connected and pid not in forfeited + eliminated + disqualified:
                forfeited.append(pid)
        forfeited = self.in_turn_order(forfeited)
        # Step 8.
        for status, pids in (("GUESSED", eliminated), ("FORFEIT", forfeited), ("DQ", disqualified)):
            for pid in pids:
                P[pid].status = status
                P[pid].out_turn = turn_id
        self.broadcast({"type": "TURN_RESULT", "turn_id": turn_id, "guesser": guesser, "guess": guess,
                        "skipped": guess is None, "results": results, "eliminated": eliminated,
                        "forfeited": forfeited, "disqualified": disqualified})
        # Step 9.
        if eliminated:
            await self.collect_reveals(eliminated, "ELIMINATED")

    async def collect_reveals(self, pids, reason):
        """REVEAL_REQUEST to pids, wait, then verify and broadcast REVEALED in turn order."""
        P = self.players
        self.reveal_waiting = set(pids)
        for pid in pids:
            if P[pid].connected:
                P[pid].conn.send({"type": "REVEAL_REQUEST", "reason": reason})
        await self.wait_until(lambda: all(P[pid].reveal is not None or not P[pid].connected
                                          for pid in pids), self.args.reveal_timeout)
        self.reveal_waiting = set()
        for pid in pids:
            q = P[pid]
            secret, salt = q.reveal if q.reveal else (None, None)
            verdict, bad_turns = verify_reveal(q.commit, secret, salt, q.history)
            self.broadcast({"type": "REVEALED", "player": pid, "secret": secret, "salt": salt,
                            "verdict": verdict, "bad_turns": bad_turns})
            if verdict != "OK":
                q.status = "DQ"
                if q.out_turn is None:
                    q.out_turn = self.turn_id  # failed at the final reveal: the last turn (§11.3)

    def finish(self, reason, turns, ranking):
        """Broadcast GAME_OVER and close every connection of this game (§12.4)."""
        self.phase = "OVER"
        self.broadcast({"type": "GAME_OVER", "game_id": self.game_id, "reason": reason,
                        "turns": turns, "ranking": ranking})
        self.say(f"game over ({reason}, {turns} turns): "
                 + ", ".join(f"{r['rank']}:{r['player']}({r['status']})" for r in ranking))
        for p in self.players.values():
            p.conn.close()

    # ----- bots, console, main loop ----------------------------------------

    async def start_bots(self):
        """--bots K: start K of our own `ai` players as subprocesses (§5.3.4)."""
        host = "127.0.0.1" if self.args.host == "0.0.0.0" else self.args.host
        for i in range(1, self.args.bots + 1):
            cmd = [sys.executable, os.path.abspath(__file__), "ai", "--host", host,
                   "--port", str(self.port), "--name", f"BOT-{i}", "--team", "BOT", "--quiet"]
            self.bot_procs.append(await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL))

    async def reap_bots(self):
        """Wait for the bots to exit after a game; kill the ones that do not."""
        for proc in self.bot_procs:
            try:
                await asyncio.wait_for(proc.wait(), BOT_EXIT_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        self.bot_procs = []

    def start_console(self):
        """Read console commands only if stdin is a terminal (§5.3.8)."""
        if sys.stdin is None or not sys.stdin.isatty():
            return
        loop = asyncio.get_running_loop()

        def read_commands():
            for line in sys.stdin:
                if line.strip().lower() == "start":
                    loop.call_soon_threadsafe(self.request_start)

        threading.Thread(target=read_commands, name="console", daemon=True).start()
        self.say("type 'start' to begin early with at least 2 players")

    def request_start(self):
        if self.phase == "LOBBY" and len(self.players) >= 2:
            self.start_requested = True
            self.wake()
        else:
            self.say("'start' needs at least 2 players in the lobby")

    def request_stop(self):
        """Stop serve() from another thread (used by the tests)."""
        loop, task = getattr(self, "_loop", None), getattr(self, "_task", None)
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # the server already finished and its loop is closed

    async def end_game(self):
        """After a game: let GAME_OVER go out, collect the bots, close the log."""
        tasks = [p.conn.writer_task for p in self.players.values()]
        if tasks:
            await asyncio.wait(tasks, timeout=FLUSH_TIMEOUT)
        await self.reap_bots()
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None

    async def serve(self):
        self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
        self.changed = asyncio.Event()
        listener = await asyncio.start_server(self.handle_connection, self.args.host, self.args.port,
                                              reuse_address=True)  # SO_REUSEADDR (§5.3.7)
        self.port = listener.sockets[0].getsockname()[1]
        self.say(f"{PROTOCOL_VERSION} server listening on {self.args.host}:{self.port}")
        self.start_console()
        try:
            async with listener:
                while self.args.games == 0 or self.games_played < self.args.games:
                    try:
                        await self.run_game()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - report a bug, keep hosting
                        self.say(f"internal error, game abandoned: {type(exc).__name__}: {exc}")
                        for p in self.players.values():
                            p.conn.close()
                    self.games_played += 1
                    await self.end_game()
        except asyncio.CancelledError:
            self.say("stopping")
        finally:
            for p in self.players.values():
                p.conn.close()
            for proc in self.bot_procs:
                if proc.returncode is None:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), BOT_EXIT_TIMEOUT)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
            if self.log_file is not None:
                self.log_file.close()
                self.log_file = None
        return 0


# =====================================================================
# 11. Command line interface                                     §5.2
# =====================================================================


def build_parser():
    """The exact options and defaults from §5.2."""
    parser = argparse.ArgumentParser(
        prog="bc.py",
        description="Multiplayer Bulls & Cows over TCP (protocol BC/1.0).",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{server,client,ai}")

    s = sub.add_parser("server", help="run the game server (referee)",
                       description="Run the BC/1.0 game server.")
    s.add_argument("--host", default="0.0.0.0", help="address to listen on (default: %(default)s)")
    s.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port (default: %(default)s)")
    s.add_argument("--players", type=int, default=2,
                   help="start the game when this many players joined (default: %(default)s)")
    s.add_argument("--bots", type=int, default=0,
                   help="start this many of our own AI players as bots (default: %(default)s)")
    s.add_argument("--guess-timeout", type=float, default=60,
                   help="seconds to send a GUESS (default: %(default)s)")
    s.add_argument("--feedback-timeout", type=float, default=60,
                   help="seconds to send FEEDBACK (default: %(default)s)")
    s.add_argument("--commit-timeout", type=float, default=30,
                   help="seconds to send COMMIT (default: %(default)s)")
    s.add_argument("--reveal-timeout", type=float, default=15,
                   help="seconds to send REVEAL (default: %(default)s)")
    s.add_argument("--max-turns", type=int, default=120,
                   help="end the game after this many turns (default: %(default)s)")
    s.add_argument("--max-skips", type=int, default=3,
                   help="consecutive skipped turns before FORFEIT (default: %(default)s)")
    s.add_argument("--games", type=int, default=1,
                   help="number of games to host, 0 = unlimited (default: %(default)s)")
    s.add_argument("--log-dir", default="logs", help="directory for server logs (default: %(default)s)")

    for name, kind in (("client", "a human player (console)"), ("ai", "an automatic AI player")):
        p = sub.add_parser(name, help=f"connect as {kind}",
                           description=f"Connect to a BC/1.0 server as {kind}.")
        p.add_argument("--host", required=True, help="server address")
        p.add_argument("--port", type=int, default=DEFAULT_PORT, help="server port (default: %(default)s)")
        p.add_argument("--name", required=True, help="player name shown to others")
        p.add_argument("--team", default="G00", help="team / group id (default: %(default)s)")
        if name == "ai":
            p.add_argument("--quiet", action="store_true", help="print less output")
            p.add_argument("--strategy", choices=AI_STRATEGIES, default=DEFAULT_STRATEGY,
                           help="how to choose guesses: v1 = most parts (the spec's baseline), "
                                "v2 = weighted multi-opponent entropy (default: %(default)s)")
    return parser


def force_utf8_console():
    """Use UTF-8 for the console even where the default is e.g. cp950 (§16.3.6)."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass  # e.g. a stream that was replaced or closed; keep going


def server_arg_problems(args):
    """Option values the server cannot work with (§5.2 table)."""
    problems = []
    if not 2 <= args.players <= 6:
        problems.append("--players must be between 2 and 6")
    if not 0 <= args.bots <= args.players:
        problems.append("--bots must be between 0 and --players")
    for option in ("guess_timeout", "feedback_timeout", "commit_timeout", "reveal_timeout"):
        if not getattr(args, option) > 0:
            problems.append(f"--{option.replace('_', '-')} must be a positive number of seconds")
    if args.max_turns < 1 or args.max_skips < 1:
        problems.append("--max-turns and --max-skips must be at least 1")
    if args.games < 0:
        problems.append("--games must be 0 (unlimited) or more")
    return problems


def run_server(args):
    problems = server_arg_problems(args)
    for problem in problems:
        print(f"error: {problem}")
    if problems:
        return 2
    try:
        return asyncio.run(GameServer(args).serve())
    except KeyboardInterrupt:
        print("[server] stopped")
        return 0
    except OSError as exc:  # for example: the port is already in use
        print(f"[server] cannot start: {exc}")
        return 1


def check_identity(args):
    """name and team must be 1-20 characters without control characters (§9)."""
    for option, value in (("--name", args.name), ("--team", args.team)):
        if not is_valid_text(value, 1, 20):
            print(f"error: {option} must be 1-20 characters without control characters")
            return False
    return True


def run_client(args):
    if not check_identity(args):
        return 2
    return HumanClient(args.host, args.port, args.name, args.team).run()


def run_ai(args):
    if not check_identity(args):
        return 2
    return AIPlayer(args.host, args.port, args.name, args.team, quiet=args.quiet,
                    strategy=args.strategy).run()


def main(argv=None):
    force_utf8_console()
    args = build_parser().parse_args(argv)
    commands = {"server": run_server, "client": run_client, "ai": run_ai}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
