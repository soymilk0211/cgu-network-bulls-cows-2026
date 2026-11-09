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
    6. Command line interface               (§5.2)
"""

import argparse
import hashlib
import itertools
import json
import re
import secrets
import sys

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

    Rules (§8.2):
      * a message ends with LF (b"\\n"); one CR right before the LF is dropped;
      * empty lines are ignored;
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
            if line:
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
    Returns (verdict, bad_turns):
      ("BAD_COMMIT", [])      secret/salt malformed or hash != commit
      ("INCONSISTENT", [ids]) hash ok, but feedback for these turns was wrong
      ("OK", [])              everything matches
    NO_REVEAL (player never revealed) is decided by the caller on timeout.
    """
    if not (is_valid_number(secret) and is_salt(salt)):
        return "BAD_COMMIT", []
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
# 6. Command line interface                                      §5.2
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


def run_server(args):
    print("server: not implemented yet (issue #4)")
    return 1


def run_client(args):
    print("client: not implemented yet (issue #2)")
    return 1


def run_ai(args):
    print("ai: not implemented yet (issue #3)")
    return 1


def main(argv=None):
    force_utf8_console()
    args = build_parser().parse_args(argv)
    commands = {"server": run_server, "client": run_client, "ai": run_ai}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
