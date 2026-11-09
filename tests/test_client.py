"""Tests for the human client (issue #2), using a scripted fake server.

The fake server is a real TCP listener on 127.0.0.1; the test decides every
byte it sends, so we can split messages, glue several together, and mix in
PING / CHAT / unknown messages.  The client runs in a thread; "typing" is
done by putting ("input", text) events into client.events once the client
shows the expected prompt.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bc  # noqa: E402

SALT2 = "8cccf65853321415f26451c5a5d016f4"  # p2's salt in our scripted games
RULES = {"guess_timeout": 30, "feedback_timeout": 30, "commit_timeout": 30,
         "reveal_timeout": 15, "max_turns": 120, "max_skips": 3}
PLAYERS = [{"id": "p1", "name": "G00-測試", "team": "G00", "kind": "human"},
           {"id": "p2", "name": "對手", "team": "G07", "kind": "ai"}]


def wait_for(cond, timeout=5.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class FakeServer:
    """A scripted BC/1.0 server for exactly one client connection."""

    def __init__(self):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.conn = None
        self.buf = bc.LineBuffer()
        self.pending = []  # decoded messages received but not consumed yet
        self.received = []  # every message the client sent, in order

    def accept(self, timeout=5.0):
        self.listener.settimeout(timeout)
        self.conn, _ = self.listener.accept()

    def send(self, *msgs):
        """Send dicts and/or raw bytes in ONE sendall (several messages in one packet)."""
        self.conn.sendall(b"".join(m if isinstance(m, bytes) else bc.encode(m) for m in msgs))

    def send_split(self, msg, pieces=3):
        """Send one message cut into `pieces` packets."""
        data = bc.encode(msg)
        step = len(data) // pieces + 1
        for i in range(0, len(data), step):
            self.conn.sendall(data[i:i + step])
            time.sleep(0.05)

    def recv(self, timeout=5.0):
        """Next message from the client, or None on timeout / close."""
        end = time.monotonic() + timeout
        while not self.pending:
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.conn.settimeout(left)
            try:
                data = self.conn.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                return None
            if not data:
                return None
            for line in self.buf.feed(data):
                msg = bc.decode(line)
                self.received.append(msg)
                self.pending.append(msg)
        return self.pending.pop(0)

    def expect(self, mtype, timeout=5.0):
        """Next message of type mtype (other types in between are skipped)."""
        end = time.monotonic() + timeout
        while True:
            msg = self.recv(max(0.0, end - time.monotonic()))
            if msg is None:
                raise AssertionError(f"client did not send {mtype}")
            if msg["type"] == mtype:
                return msg

    def assert_silent(self, seconds):
        msg = self.recv(seconds)
        if msg is not None:
            raise AssertionError(f"client unexpectedly sent {msg}")

    def close(self):
        for s in (self.conn, self.listener):
            if s is not None:
                s.close()


class ClientTestCase(unittest.TestCase):
    client_class = bc.HumanClient

    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        self.log_dir = tempfile.mkdtemp(prefix="bc-client-test-")
        self.addCleanup(shutil.rmtree, self.log_dir, True)
        self.out = []
        self.client = self.make_client()
        self.exit_code = []
        self.thread = threading.Thread(target=lambda: self.exit_code.append(self.client.run()), daemon=True)
        self.thread.start()
        self.server.accept()

    def make_client(self):
        return self.client_class("127.0.0.1", self.server.port, "G00-測試", "G00", out=self.out.append,
                                 log_dir=self.log_dir, use_stdin=False)

    def output(self):
        return "\n".join(self.out)

    def type_answer(self, kind, text):
        """Wait until the client asks a `kind` question, then type `text`."""
        wait_for(lambda: self.client.prompt is not None and self.client.prompt.kind == kind,
                 what=f"a {kind} prompt")
        self.client.events.put(("input", text))

    def join_lobby(self):
        hello = self.server.expect("HELLO")
        self.server.send({"type": "WELCOME", "protocol": "BC/1.0", "player_id": "p1"},
                         {"type": "LOBBY", "players": PLAYERS, "min_players": 2, "max_players": 2})
        return hello

    def start_game(self, rules=RULES, secret="0275"):
        """GAME_START -> the user picks `secret` -> COMMIT -> COMMITS.  Returns our commit."""
        self.server.send({"type": "GAME_START", "game_id": "g1", "players": PLAYERS,
                          "order": ["p1", "p2"], "rules": rules})
        self.type_answer("secret", secret)
        commit = self.server.expect("COMMIT")["commit"]
        self.server.send({"type": "COMMITS", "order": ["p1", "p2"],
                          "commits": {"p1": commit, "p2": bc.commit_hash("3527", SALT2)}})
        return commit

    def finish(self, timeout=5.0):
        self.thread.join(timeout)
        self.assertFalse(self.thread.is_alive(), "client did not exit")
        return self.exit_code[0]


class FullGameTest(ClientTestCase):
    def test_full_game_with_noise_between_messages(self):
        hello = self.server.expect("HELLO")
        self.assertEqual((hello["protocol"], hello["name"], hello["team"], hello["kind"]),
                         ("BC/1.0", "G00-測試", "G00", "human"))

        # WELCOME cut into 3 packets; LOBBY and PING glued into one packet.
        self.server.send_split({"type": "WELCOME", "protocol": "BC/1.0", "player_id": "p1", "x_new": 1})
        self.server.send({"type": "LOBBY", "players": PLAYERS, "min_players": 2, "max_players": 2},
                         {"type": "PING", "nonce": "n-1"})
        self.assertEqual(self.server.expect("PONG"), {"type": "PONG", "nonce": "n-1"})

        # Garbage, an unknown type, CHAT and a non-fatal ERROR must all be survived.
        self.server.send(b"this is not json\n",
                         {"type": "FUTURE_THING", "whatever": [1, 2]},
                         {"type": "CHAT", "from": "p2", "text": "大家好"},
                         {"type": "ERROR", "code": "UNKNOWN_TYPE", "message": "test", "fatal": False})

        # COMMIT is computed and sent automatically after the user picks the secret.
        self.server.send({"type": "GAME_START", "game_id": "g1", "players": PLAYERS,
                          "order": ["p1", "p2"], "rules": RULES, "x_extra": True})
        self.type_answer("secret", "0275")
        commit = self.server.expect("COMMIT")["commit"]
        self.assertTrue(bc.is_commit(commit))
        self.server.send({"type": "COMMITS", "order": ["p1", "p2"],
                          "commits": {"p1": commit, "p2": bc.commit_hash("3527", SALT2)}})

        # Turn 1: the opponent guesses; Enter sends the prefilled score.
        self.server.send({"type": "TURN", "turn_id": 1, "guesser": "p2", "alive": ["p1", "p2"]},
                         {"type": "PING", "nonce": "n-2"},
                         {"type": "GUESS_MADE", "turn_id": 1, "guesser": "p2", "guess": "0927",
                          "responders": ["p1"]})
        self.assertEqual(self.server.expect("PONG")["nonce"], "n-2")
        self.type_answer("feedback", "")
        self.assertEqual(self.server.expect("FEEDBACK"),
                         {"type": "FEEDBACK", "turn_id": 1, "a": 1, "b": 2})  # score("0927", "0275")
        self.server.send({"type": "TURN_RESULT", "turn_id": 1, "guesser": "p2", "guess": "0927",
                          "skipped": False, "results": [{"player": "p1", "a": 1, "b": 2}],
                          "eliminated": [], "forfeited": [], "disqualified": []})

        # Turn 2: our guess; an invalid one is refused locally, not sent.
        self.server.send({"type": "TURN", "turn_id": 2, "guesser": "p1", "alive": ["p1", "p2"]})
        self.type_answer("guess", "1123")
        self.type_answer("guess", "3527")
        self.assertEqual(self.server.expect("GUESS"), {"type": "GUESS", "turn_id": 2, "guess": "3527"})
        self.server.send({"type": "GUESS_MADE", "turn_id": 2, "guesser": "p1", "guess": "3527",
                          "responders": ["p2"]},
                         {"type": "CHAT", "from": "p2", "text": "nice"},
                         {"type": "TURN_RESULT", "turn_id": 2, "guesser": "p1", "guess": "3527",
                          "skipped": False, "results": [{"player": "p2", "a": 4, "b": 0}],
                          "eliminated": ["p2"], "forfeited": [], "disqualified": []},
                         {"type": "REVEALED", "player": "p2", "secret": "3527", "salt": SALT2,
                          "verdict": "OK", "bad_turns": []})

        # End of game: REVEAL is sent automatically and immediately.
        self.server.send({"type": "REVEAL_REQUEST", "reason": "GAME_END"})
        start = time.monotonic()
        reveal = self.server.expect("REVEAL")
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(reveal["secret"], "0275")
        self.assertEqual(bc.commit_hash(reveal["secret"], reveal["salt"]), commit)
        self.server.send({"type": "REVEALED", "player": "p1", "secret": "0275", "salt": reveal["salt"],
                          "verdict": "OK", "bad_turns": []},
                         {"type": "GAME_OVER", "game_id": "g1", "reason": "LAST_PLAYER", "turns": 2,
                          "ranking": [{"player": "p1", "status": "SURVIVOR", "out_turn": None, "rank": 1},
                                      {"player": "p2", "status": "GUESSED", "out_turn": 2, "rank": 2}]})
        self.assertEqual(self.finish(), 0)

        text = self.output()
        for expected in ("大家好", "UNKNOWN_TYPE", "unparsable", "verdict OK", "GAME OVER"):
            self.assertIn(expected, text)
        self.assertNotIn("our own check says", text)  # our reveal checks agree with the server
        self.assertNotIn("ERROR", [m["type"] for m in self.server.received])  # never answer an ERROR
        sent_types = [m["type"] for m in self.server.received]
        self.assertEqual(sent_types.count("GUESS"), 1)
        self.assertEqual(sent_types.count("FEEDBACK"), 1)

        # Client log: own folder, one JSON record per line with ts/dir/peer/msg.
        files = os.listdir(self.log_dir)
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.log_dir, files[0]), encoding="utf-8") as f:
            records = [json.loads(line) for line in f]
        self.assertTrue(all(set(r) == {"ts", "dir", "peer", "msg"} for r in records))
        self.assertEqual(records[0]["dir"], "send")
        self.assertEqual(records[0]["msg"]["type"], "HELLO")
        self.assertIn({"raw": "this is not json"}, [r["msg"] for r in records])


class FeedbackTests(ClientTestCase):
    def guess_made(self, guess="0927"):
        self.server.send({"type": "TURN", "turn_id": 1, "guesser": "p2", "alive": ["p1", "p2"]},
                         {"type": "GUESS_MADE", "turn_id": 1, "guesser": "p2", "guess": guess,
                          "responders": ["p1"]})

    def test_changed_feedback_needs_a_second_confirmation(self):
        self.join_lobby()
        self.start_game()
        self.guess_made()
        self.type_answer("feedback", "2A0B")  # differs from the computed 1A2B
        self.type_answer("confirm", "no")  # -> back to the feedback question
        self.type_answer("feedback", "2 0")
        self.server.assert_silent(0.3)  # nothing sent without confirmation
        self.type_answer("confirm", "yes")
        self.assertEqual(self.server.expect("FEEDBACK"), {"type": "FEEDBACK", "turn_id": 1, "a": 2, "b": 0})

    def test_typing_the_computed_value_needs_no_confirmation(self):
        self.join_lobby()
        self.start_game()
        self.guess_made()
        self.type_answer("feedback", "1a2b")
        self.assertEqual(self.server.expect("FEEDBACK"), {"type": "FEEDBACK", "turn_id": 1, "a": 1, "b": 2})

    def test_impossible_feedback_is_refused_locally(self):
        self.join_lobby()
        self.start_game()
        self.guess_made()
        self.type_answer("feedback", "3A1B")
        self.server.assert_silent(0.3)
        self.assertEqual(self.client.prompt.kind, "feedback")

    def test_eliminated_player_reveals_and_becomes_spectator(self):
        self.join_lobby()
        commit = self.start_game(secret="0275")
        self.guess_made("0275")
        self.type_answer("feedback", "")
        self.assertEqual(self.server.expect("FEEDBACK")["a"], 4)
        self.server.send({"type": "TURN_RESULT", "turn_id": 1, "guesser": "p2", "guess": "0275",
                          "skipped": False, "results": [{"player": "p1", "a": 4, "b": 0}],
                          "eliminated": ["p1"], "forfeited": [], "disqualified": []},
                         {"type": "REVEAL_REQUEST", "reason": "ELIMINATED"})
        reveal = self.server.expect("REVEAL")
        self.assertEqual(bc.commit_hash(reveal["secret"], reveal["salt"]), commit)
        wait_for(lambda: self.client.out_of_game, what="spectator mode")
        self.assertIn("spectator", self.output())


class TimeoutTests(ClientTestCase):
    def test_guess_prompt_is_withdrawn_after_the_deadline(self):
        self.join_lobby()
        self.start_game(rules=dict(RULES, guess_timeout=1.2))  # local deadline = 0.6 s
        self.server.send({"type": "TURN", "turn_id": 1, "guesser": "p1", "alive": ["p1", "p2"]})
        wait_for(lambda: self.client.prompt is not None and self.client.prompt.kind == "guess")
        wait_for(lambda: self.client.prompt is None, timeout=3, what="prompt withdrawal")
        self.assertIn("time is up", self.output())
        self.client.events.put(("input", "0123"))  # too late: must not be sent
        self.server.assert_silent(0.5)

    def test_prompt_is_withdrawn_when_the_turn_ends(self):
        self.join_lobby()
        self.start_game()
        self.server.send({"type": "TURN", "turn_id": 1, "guesser": "p1", "alive": ["p1", "p2"]})
        wait_for(lambda: self.client.prompt is not None and self.client.prompt.kind == "guess")
        self.server.send({"type": "TURN_RESULT", "turn_id": 1, "guesser": "p1", "guess": None,
                          "skipped": True, "results": [], "eliminated": [], "forfeited": [],
                          "disqualified": []})
        wait_for(lambda: self.client.prompt is None, what="prompt withdrawal")
        self.assertIn("prompt withdrawn", self.output())

    def test_commit_is_sent_automatically_if_the_user_does_not_answer(self):
        self.join_lobby()
        self.server.send({"type": "GAME_START", "game_id": "g1", "players": PLAYERS,
                          "order": ["p1", "p2"], "rules": dict(RULES, commit_timeout=1)})
        commit = self.server.expect("COMMIT", timeout=3)["commit"]
        self.assertTrue(bc.is_commit(commit))
        self.assertEqual(bc.commit_hash(self.client.secret, self.client.salt), commit)


class EndingTests(ClientTestCase):
    def test_fatal_error_ends_the_client(self):
        self.join_lobby()
        self.server.send({"type": "ERROR", "code": "TIMEOUT", "message": "bye", "fatal": True})
        self.assertEqual(self.finish(), 1)
        self.assertIn("fatal", self.output())

    def test_disconnect_ends_the_client(self):
        self.join_lobby()
        self.server.conn.close()
        self.assertEqual(self.finish(), 1)
        self.assertIn("closed the connection", self.output())

    def test_other_major_version_is_refused(self):
        self.server.expect("HELLO")
        self.server.send({"type": "WELCOME", "protocol": "BC/2.0", "player_id": "p1"})
        self.server.expect("BYE")
        self.assertEqual(self.finish(), 1)

    def test_wrong_server_verdict_is_flagged(self):
        self.join_lobby()
        self.start_game()
        # The server claims OK, but secret 3528 does not match p2's commit (3527).
        self.server.send({"type": "REVEALED", "player": "p2", "secret": "3528", "salt": SALT2,
                          "verdict": "OK", "bad_turns": []})
        wait_for(lambda: "our own check says BAD_COMMIT" in self.output(), what="mismatch warning")

    def test_malformed_server_message_does_not_crash(self):
        self.join_lobby()
        self.server.send({"type": "TURN", "turn_id": 1},  # missing guesser / alive
                         {"type": "PING", "nonce": "still-alive"})
        self.assertEqual(self.server.expect("PONG")["nonce"], "still-alive")
        self.assertIn("malformed TURN", self.output())


class HelperTests(unittest.TestCase):
    def test_parse_feedback(self):
        self.assertEqual(bc.parse_feedback("1A2B"), (1, 2))
        self.assertEqual(bc.parse_feedback(" 1a 2b "), (1, 2))
        self.assertEqual(bc.parse_feedback("1 2"), (1, 2))
        self.assertEqual(bc.parse_feedback("4,0"), (4, 0))
        for bad in ("3A1B", "2A3B", "12", "", "1A", "x", "５A０B"):
            self.assertIsNone(bc.parse_feedback(bad), bad)

    def test_read_rules_falls_back_to_defaults(self):
        rules = bc.read_rules({"guess_timeout": 1.5, "feedback_timeout": True, "commit_timeout": -1})
        self.assertEqual(rules["guess_timeout"], 1.5)
        self.assertEqual(rules["feedback_timeout"], 60)
        self.assertEqual(rules["commit_timeout"], 30)
        self.assertEqual(bc.read_rules(None), bc.DEFAULT_RULES)

    def test_answer_deadline_keeps_a_margin(self):
        now = time.monotonic()
        self.assertAlmostEqual(bc.answer_deadline(60) - now, 59, delta=0.1)
        self.assertAlmostEqual(bc.answer_deadline(1.0) - now, 0.5, delta=0.1)

    def test_printable_removes_control_characters(self):
        self.assertEqual(bc.printable("a\x1b[2Jb\nc"), "a?[2Jb?c")

    def test_client_rejects_bad_name_before_connecting(self):
        args = bc.build_parser().parse_args(["client", "--host", "127.0.0.1", "--name", "x" * 21])
        self.assertEqual(bc.run_client(args), 2)


if __name__ == "__main__":
    unittest.main()
