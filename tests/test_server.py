"""Tests for the server (issue #4).

Most tests start a real GameServer on 127.0.0.1 with --port 0 in a background
thread and talk to it with RawClient, a small blocking test client.  The test
decides every message, so we can check exact error codes, the order of
messages and the timing rules.
"""

import asyncio
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bc  # noqa: E402

SLOW = ["--guess-timeout", "5", "--feedback-timeout", "5", "--commit-timeout", "5", "--reveal-timeout", "5"]


def wait_for(cond, timeout=5.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class ServerThread:
    """A GameServer running in a background thread (its own asyncio loop)."""

    def __init__(self, *options):
        self.log_dir = tempfile.mkdtemp(prefix="bc-server-test-")
        args = bc.build_parser().parse_args(["server", "--host", "127.0.0.1", "--port", "0",
                                             "--log-dir", self.log_dir, *options])
        self.lines = []
        self.server = bc.GameServer(args, out=self.lines.append)
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve()), daemon=True)
        self.thread.start()
        wait_for(lambda: self.server.port is not None, what="server start")
        self.port = self.server.port

    def stop(self):
        self.server.request_stop()
        self.thread.join(10)
        shutil.rmtree(self.log_dir, ignore_errors=True)

    def log_records(self):
        files = glob.glob(os.path.join(self.log_dir, "*"))
        assert len(files) == 1, files
        with open(files[0], encoding="utf-8") as f:
            return [json.loads(line) for line in f]


class RawClient:
    """A blocking test client that speaks raw BC/1.0."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = bc.LineBuffer(max_bytes=1 << 20)
        self.pending = []
        self.inbox = []  # every message received, in order
        self.eof = False

    def send(self, *msgs):
        self.sock.sendall(b"".join(m if isinstance(m, bytes) else bc.encode(m) for m in msgs))

    def recv(self, timeout=5.0):
        """The next message, or None on timeout or when the server closed the connection."""
        end = time.monotonic() + timeout
        while not self.pending:
            left = end - time.monotonic()
            if left <= 0 or self.eof:
                return None
            self.sock.settimeout(left)
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                data = b""
            if not data:
                self.eof = True
                return None
            for line in self.buf.feed(data):
                msg = bc.decode(line)
                self.inbox.append(msg)
                self.pending.append(msg)
        return self.pending.pop(0)

    def expect(self, mtype, timeout=5.0, turn_id=None):
        """The next message of type mtype (and of turn turn_id if given); others are skipped."""
        end = time.monotonic() + timeout
        while True:
            msg = self.recv(max(0.0, end - time.monotonic()))
            if msg is None:
                raise AssertionError(f"no {mtype} received" + (f" for turn {turn_id}" if turn_id else ""))
            if msg["type"] == mtype and (turn_id is None or msg.get("turn_id") == turn_id):
                return msg

    def expect_error(self, code, fatal=False):
        msg = self.expect("ERROR")
        assert (msg["code"], msg["fatal"]) == (code, fatal), msg
        return msg

    def closed_within(self, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.recv(0.1) is None and self.eof:
                return True
        return False

    def hello(self, name, kind="human"):
        self.send({"type": "HELLO", "protocol": "BC/1.0", "name": name, "team": "T", "kind": kind})
        return self.expect("WELCOME")

    def close(self):
        self.sock.close()


class ServerTestCase(unittest.TestCase):
    def start_server(self, *options):
        server = ServerThread(*options)
        self.addCleanup(server.stop)
        return server

    def connect(self, server):
        client = RawClient(server.port)
        self.addCleanup(client.close)
        return client

    def start_game(self, players=2, options=SLOW, secrets_=None):
        """Join `players` raw clients, commit, and return (server, {pid: client}, {pid: (secret, salt)}, order)."""
        server = self.start_server("--players", str(players), *options)
        clients = {}
        for i in range(players):
            c = self.connect(server)
            clients[c.hello(f"P{i + 1}")["player_id"]] = c
        order = None
        keys = {}
        for n, (pid, c) in enumerate(clients.items()):
            start = c.expect("GAME_START")
            order = start["order"]
            secret = (secrets_ or ["0275", "3527", "1468"])[n]
            salt, commit = bc.make_commit(secret)
            keys[pid] = (secret, salt, commit)
            c.send({"type": "COMMIT", "commit": commit})
        for c in clients.values():
            c.expect("COMMITS")
        return server, clients, keys, order


class UnitTests(unittest.TestCase):
    class Joined:
        def __init__(self, name):
            self.name = name

    def test_hello_checks_in_spec_order(self):  # §12.1.3
        ok = {"type": "HELLO", "protocol": "BC/1.0", "name": "x", "team": "T", "kind": "human"}
        lobby = [self.Joined("taken")]
        check = bc.hello_error
        self.assertIsNone(check(ok, "LOBBY", lobby, 3))
        self.assertIsNone(check(dict(ok, protocol="BC/1.7"), "LOBBY", lobby, 3))  # any BC/1.x
        self.assertEqual(check({k: v for k, v in ok.items() if k != "kind"}, "LOBBY", lobby, 3)[0], "BAD_FIELD")
        self.assertEqual(check(dict(ok, name=5, protocol="BC/2.0"), "LOBBY", lobby, 3)[0], "BAD_FIELD")
        self.assertEqual(check(dict(ok, protocol="BC/2.0", name=""), "LOBBY", lobby, 3)[0],
                         "VERSION_UNSUPPORTED")
        self.assertEqual(check(dict(ok, name=""), "PLAY", lobby, 3)[0], "INVALID_VALUE")
        self.assertEqual(check(dict(ok, name="a\tb"), "LOBBY", lobby, 3)[0], "INVALID_VALUE")
        self.assertEqual(check(dict(ok, name="x" * 21), "LOBBY", lobby, 3)[0], "INVALID_VALUE")
        self.assertEqual(check(dict(ok, kind="robot"), "LOBBY", lobby, 3)[0], "INVALID_VALUE")
        self.assertEqual(check(dict(ok, name="taken"), "COMMIT", lobby, 1)[0], "GAME_IN_PROGRESS")
        self.assertEqual(check(dict(ok, name="taken"), "LOBBY", lobby, 1)[0], "ROOM_FULL")
        self.assertEqual(check(dict(ok, name="taken"), "LOBBY", lobby, 3)[0], "NAME_TAKEN")

    def test_ranking_spec_example(self):  # §2.4: A survives, B and C guessed in turn 9, D forfeits in 12
        rows = bc.compute_ranking([("A", "SURVIVOR", None), ("B", "GUESSED", 9),
                                   ("C", "GUESSED", 9), ("D", "FORFEIT", 12)])
        self.assertEqual([(r["player"], r["rank"]) for r in rows], [("A", 1), ("B", 2), ("C", 2), ("D", 4)])

    def test_ranking_dq_last_and_ties_in_turn_order(self):
        rows = bc.compute_ranking([("p3", "DQ", 4), ("p1", "SURVIVOR", None), ("p2", "GUESSED", 3),
                                   ("p4", "SURVIVOR", None), ("p5", "GUESSED", 6), ("p6", "FORFEIT", 2)])
        self.assertEqual([(r["player"], r["status"], r["out_turn"], r["rank"]) for r in rows],
                         [("p1", "SURVIVOR", None, 1), ("p4", "SURVIVOR", None, 1), ("p5", "GUESSED", 6, 3),
                          ("p2", "GUESSED", 3, 4), ("p6", "FORFEIT", 2, 5), ("p3", "DQ", 4, None)])

    def test_server_option_checks(self):
        parse = bc.build_parser().parse_args
        self.assertEqual(bc.server_arg_problems(parse(["server"])), [])
        self.assertTrue(bc.server_arg_problems(parse(["server", "--players", "7"])))
        self.assertTrue(bc.server_arg_problems(parse(["server", "--bots", "3"])))
        self.assertTrue(bc.server_arg_problems(parse(["server", "--guess-timeout", "0"])))
        self.assertTrue(bc.server_arg_problems(parse(["server", "--games", "-1"])))
        self.assertEqual(bc.seconds_value(60.0), 60)
        self.assertEqual(bc.seconds_value(1.5), 1.5)


class LobbyTests(ServerTestCase):
    def test_errors_before_hello(self):
        server = self.start_server("--players", "3")
        c = self.connect(server)
        c.send(b"this is not json\n")
        c.expect_error("BAD_JSON", fatal=False)
        c.send({"type": "PING"})
        c.expect_error("UNEXPECTED", fatal=True)
        self.assertTrue(c.closed_within(5))

        for bad, code in (({"protocol": "BC/2.0", "name": "x", "team": "T", "kind": "human"}, "VERSION_UNSUPPORTED"),
                          ({"protocol": "BC/1.0", "name": "x", "team": "T"}, "BAD_FIELD"),
                          ({"protocol": "BC/1.0", "name": "", "team": "T", "kind": "human"}, "INVALID_VALUE")):
            c = self.connect(server)
            c.send(dict(bad, type="HELLO"))
            c.expect_error(code, fatal=True)
            self.assertTrue(c.closed_within(5))

        c = self.connect(server)
        c.send(b"{" + b"x" * 5000 + b"}\n")
        c.expect_error("LINE_TOO_LONG", fatal=True)

    def test_hello_timeout(self):
        old = bc.HELLO_TIMEOUT
        bc.HELLO_TIMEOUT = 0.5
        self.addCleanup(setattr, bc, "HELLO_TIMEOUT", old)
        server = self.start_server()
        c = self.connect(server)
        c.expect_error("TIMEOUT", fatal=True)
        self.assertTrue(c.closed_within(5))

    def test_lobby_and_messages_after_hello(self):
        server = self.start_server("--players", "3")
        a = self.connect(server)
        welcome = a.hello("阿明")
        self.assertEqual((welcome["protocol"], welcome["player_id"]), ("BC/1.0", "p1"))
        lobby = a.expect("LOBBY")
        self.assertEqual((lobby["min_players"], lobby["max_players"]), (2, 3))
        self.assertEqual(lobby["players"], [{"id": "p1", "name": "阿明", "team": "T", "kind": "human"}])

        dup = self.connect(server)
        dup.send({"type": "HELLO", "protocol": "BC/1.0", "name": "阿明", "team": "T", "kind": "ai"})
        dup.expect_error("NAME_TAKEN", fatal=True)

        a.send({"type": "HELLO", "protocol": "BC/1.0", "name": "again", "team": "T", "kind": "human"})
        a.expect_error("UNEXPECTED", fatal=False)  # repeated HELLO keeps the connection
        a.send({"type": "PING", "nonce": "n-42"}, {"type": "WHATEVER", "x": 1},
               {"type": "CHAT", "text": "大家好"}, {"type": "CHAT", "text": ""}, {"type": "CHAT", "text": 7},
               {"type": "COMMIT", "commit": "a" * 64})
        self.assertEqual(a.expect("PONG"), {"type": "PONG", "nonce": "n-42"})
        a.expect_error("UNKNOWN_TYPE")
        self.assertEqual(a.expect("CHAT"), {"type": "CHAT", "from": "p1", "text": "大家好"})
        a.expect_error("INVALID_VALUE")
        a.expect_error("BAD_FIELD")
        a.expect_error("UNEXPECTED")  # COMMIT in the lobby

        b = self.connect(server)
        b.hello("B")
        self.assertEqual(len(a.expect("LOBBY")["players"]), 2)
        b.close()  # leaving the lobby removes the player
        self.assertEqual([p["id"] for p in a.expect("LOBBY")["players"]], ["p1"])

    def test_too_many_errors(self):
        server = self.start_server()
        c = self.connect(server)
        c.hello("x")
        c.send(*[{"type": "NOPE"} for _ in range(21)])
        for _ in range(21):
            c.expect_error("UNKNOWN_TYPE")
        c.expect_error("TOO_MANY_ERRORS", fatal=True)
        self.assertTrue(c.closed_within(5))


class GameTests(ServerTestCase):
    def test_commit_errors_and_commits_broadcast(self):
        server = self.start_server("--players", "2", *SLOW)
        a, b = self.connect(server), self.connect(server)
        pa, pb = a.hello("A")["player_id"], b.hello("B")["player_id"]
        start = a.expect("GAME_START")
        self.assertEqual(set(start["rules"]), {"guess_timeout", "feedback_timeout", "commit_timeout",
                                               "reveal_timeout", "max_turns", "max_skips"})
        self.assertEqual(start["rules"]["guess_timeout"], 5)
        self.assertEqual(sorted(start["order"]), [pa, pb])
        self.assertEqual([p["id"] for p in start["players"]], start["order"])
        late = self.connect(server)
        late.send({"type": "HELLO", "protocol": "BC/1.0", "name": "late", "team": "T", "kind": "human"})
        late.expect_error("GAME_IN_PROGRESS", fatal=True)

        _, commit = bc.make_commit("0275")
        a.send({"type": "GUESS", "turn_id": 1, "guess": "0123"})
        a.expect_error("UNEXPECTED")
        a.send({"type": "COMMIT", "commit": 5}, {"type": "COMMIT", "commit": "XYZ"},
               {"type": "COMMIT", "commit": commit}, {"type": "COMMIT", "commit": commit})
        a.expect_error("BAD_FIELD")
        a.expect_error("INVALID_VALUE")
        a.expect_error("DUPLICATE")
        _, commit_b = bc.make_commit("3527")
        b.send({"type": "COMMIT", "commit": commit_b})
        commits = a.expect("COMMITS")
        self.assertEqual(commits["commits"], {pa: commit, pb: commit_b})
        self.assertEqual(commits["order"], start["order"])

    def test_turn_errors_and_feedback_is_withheld(self):
        server, clients, keys, order = self.start_game(players=3)
        g, r1, r2 = order  # guess 9806 below is nobody's secret, so nobody is eliminated
        turn = clients[g].expect("TURN")
        self.assertEqual((turn["turn_id"], turn["guesser"], turn["alive"]), (1, g, order))
        clients[r1].send({"type": "GUESS", "turn_id": 1, "guess": "4567"})
        clients[r1].expect_error("NOT_YOUR_TURN")
        clients[g].send({"type": "GUESS", "turn_id": True, "guess": "0123"},
                        {"type": "GUESS", "turn_id": 6, "guess": "0123"},
                        {"type": "GUESS", "turn_id": 1, "guess": "1123"},
                        {"type": "GUESS", "turn_id": 1, "guess": "9806"},
                        {"type": "GUESS", "turn_id": 1, "guess": "4567"})
        clients[g].expect_error("BAD_FIELD")
        clients[g].expect_error("STALE_TURN")
        clients[g].expect_error("INVALID_VALUE")
        clients[g].expect_error("UNEXPECTED")  # the turn already has a guess
        made = clients[r1].expect("GUESS_MADE")
        self.assertEqual((made["guesser"], made["guess"], made["responders"]), (g, "9806", [r1, r2]))

        a1, b1 = bc.score("9806", keys[r1][0])
        clients[r1].send({"type": "FEEDBACK", "turn_id": 1, "a": 3, "b": 1},
                         {"type": "FEEDBACK", "turn_id": 1, "a": a1, "b": b1},
                         {"type": "FEEDBACK", "turn_id": 1, "a": a1, "b": b1})
        clients[r1].expect_error("INVALID_VALUE")
        clients[r1].expect_error("DUPLICATE")
        clients[g].send({"type": "FEEDBACK", "turn_id": 1, "a": 0, "b": 0},
                        {"type": "REVEAL", "secret": keys[g][0], "salt": keys[g][1]})
        clients[g].expect_error("UNEXPECTED")  # the guesser is not a responder
        clients[g].expect_error("UNEXPECTED")  # no reveal requested
        # r1 answered, r2 not yet: nobody may see any result yet (§3, §16.3.10).
        time.sleep(0.3)
        for pid in order:
            while clients[pid].recv(0.05) is not None:
                pass
            self.assertNotIn("TURN_RESULT", [m["type"] for m in clients[pid].inbox])
        a2, b2 = bc.score("9806", keys[r2][0])
        clients[r2].send({"type": "FEEDBACK", "turn_id": 1, "a": a2, "b": b2})
        for pid in order:
            result = clients[pid].expect("TURN_RESULT")
            self.assertEqual(result["results"], [{"player": r1, "a": a1, "b": b1},
                                                 {"player": r2, "a": a2, "b": b2}])
            self.assertIs(result["skipped"], False)
        self.assertEqual(clients[g].expect("TURN", turn_id=2)["guesser"], r1)  # rotation

    def full_game(self, reveal=None):
        """2 players; order[0] guesses the other's secret in turn 3.

        `reveal` = (secret, salt) the eliminated player reveals (default: the true ones).
        Returns (server, all messages order[0] received, (x, y), GAME_OVER).
        """
        server, clients, keys, order = self.start_game(players=2)
        x, y = order
        cx, cy = clients[x], clients[y]
        secret_y = keys[y][0]
        plan = {1: (x, "0123"), 2: (y, "4567"), 3: (x, secret_y)}
        for turn_id, (guesser, guess) in plan.items():
            other = y if guesser == x else x
            clients[guesser].expect("TURN", turn_id=turn_id)
            clients[guesser].send({"type": "GUESS", "turn_id": turn_id, "guess": guess})
            clients[other].expect("GUESS_MADE", turn_id=turn_id)
            a, b = bc.score(guess, keys[other][0])
            clients[other].send({"type": "FEEDBACK", "turn_id": turn_id, "a": a, "b": b})
            cx.expect("TURN_RESULT", turn_id=turn_id)
        cy.expect("REVEAL_REQUEST")
        secret, salt = reveal or (secret_y, keys[y][1])
        cy.send({"type": "REVEAL", "secret": secret, "salt": salt})
        cx.expect("REVEAL_REQUEST")
        cx.send({"type": "REVEAL", "secret": keys[x][0], "salt": keys[x][1]})
        over = cx.expect("GAME_OVER")
        self.assertTrue(cy.closed_within(5))
        server.thread.join(5)
        self.assertFalse(server.thread.is_alive(), "server must exit after --games 1")
        return server, cx.inbox, (x, y), over

    def test_full_game_ranking_and_log(self):
        server, inbox, (x, y), over = self.full_game()
        revealed = [m for m in inbox if m["type"] == "REVEALED"]
        self.assertEqual([(m["player"], m["verdict"]) for m in revealed], [(y, "OK"), (x, "OK")])
        self.assertEqual(over["reason"], "LAST_PLAYER")
        self.assertEqual(over["turns"], 3)
        self.assertEqual(over["ranking"], [{"player": x, "status": "SURVIVOR", "out_turn": None, "rank": 1},
                                           {"player": y, "status": "GUESSED", "out_turn": 3, "rank": 2}])
        result3 = [m for m in inbox if m["type"] == "TURN_RESULT"][-1]
        self.assertEqual(result3["eliminated"], [y])
        records = server.log_records()  # exactly one log file for this game
        self.assertTrue(all(set(r) == {"ts", "dir", "peer", "msg"} for r in records))
        self.assertTrue(records[0]["peer"].startswith("127.0.0.1:"))  # before HELLO: IP:port
        self.assertEqual(records[0]["msg"]["type"], "HELLO")
        self.assertIn(over["game_id"], os.listdir(server.log_dir)[0])
        self.assertEqual(sum(1 for r in records if r["msg"]["type"] == "GAME_OVER"), 2)  # one per player

    def test_inconsistent_answer_leads_to_dq(self):
        server, clients, keys, order = self.start_game(players=2, secrets_=["0275", "3527"])
        x, y = order
        # y answers 0A0B to 4567 in turn 1 and 1A0B to the same guess in turn 3: no secret fits both.
        for turn_id, guess, answer in ((1, "4567", (0, 0)), (2, "8901", None), (3, "4567", (1, 0))):
            guesser = x if turn_id != 2 else y
            other = y if guesser == x else x
            clients[guesser].expect("TURN", turn_id=turn_id)
            clients[guesser].send({"type": "GUESS", "turn_id": turn_id, "guess": guess})
            clients[other].expect("GUESS_MADE", turn_id=turn_id)
            a, b = answer or bc.score(guess, keys[other][0])
            clients[other].send({"type": "FEEDBACK", "turn_id": turn_id, "a": a, "b": b})
        result = clients[x].expect("TURN_RESULT", turn_id=3)
        self.assertEqual(result["disqualified"], [y])
        clients[x].expect("REVEAL_REQUEST")  # x is the last player: final reveal
        clients[x].send({"type": "REVEAL", "secret": keys[x][0], "salt": keys[x][1]})
        over = clients[x].expect("GAME_OVER")
        self.assertEqual(over["ranking"][-1], {"player": y, "status": "DQ", "out_turn": 3, "rank": None})

    def test_bad_reveal_leads_to_dq(self):
        _, inbox, (x, y), over = self.full_game(reveal=("0123", "0" * 32))
        revealed = [m for m in inbox if m["type"] == "REVEALED" and m["player"] == y][0]
        self.assertEqual(revealed["verdict"], "BAD_COMMIT")
        self.assertEqual(over["ranking"][-1]["status"], "DQ")
        self.assertIsNone(over["ranking"][-1]["rank"])

    def test_skips_lead_to_forfeit(self):
        server, clients, keys, order = self.start_game(
            players=2, options=["--guess-timeout", "0.3", "--max-skips", "2", "--feedback-timeout", "5"])
        x, y = order  # x never guesses; y guesses a number that is not x's secret
        clients[y].send({"type": "PING"})
        results = []
        while True:
            msg = clients[y].recv(5)
            self.assertIsNotNone(msg)
            if msg["type"] == "TURN" and msg["guesser"] == y:
                clients[y].send({"type": "GUESS", "turn_id": msg["turn_id"], "guess": "9876"})
            elif msg["type"] == "GUESS_MADE":
                a, b = bc.score("9876", keys[x][0])
                clients[x].send({"type": "FEEDBACK", "turn_id": msg["turn_id"], "a": a, "b": b})
            elif msg["type"] == "TURN_RESULT":
                results.append(msg)
                if x in msg["forfeited"]:
                    break
        skips = [r for r in results if r["skipped"]]
        self.assertEqual(len(skips), 2)
        self.assertIs(skips[-1], results[-1])  # forfeited exactly at the 2nd skip
        self.assertIsNone(skips[0]["guess"])
        self.assertEqual(clients[y].expect("REVEAL_REQUEST")["reason"], "GAME_END")
        clients[y].send({"type": "REVEAL", "secret": keys[y][0], "salt": keys[y][1]})
        ranking = clients[y].expect("GAME_OVER")["ranking"]
        self.assertEqual([(r["player"], r["status"]) for r in ranking], [(y, "SURVIVOR"), (x, "FORFEIT")])

    def test_feedback_timeout_and_disconnect_forfeit(self):
        server, clients, keys, order = self.start_game(
            players=3, options=["--feedback-timeout", "0.5", "--guess-timeout", "5"])
        g, r1, r2 = order
        clients[r2].close()  # disconnected: the next decision point makes it FORFEIT
        clients[g].expect("TURN")
        clients[g].send({"type": "GUESS", "turn_id": 1, "guess": "0123"})
        clients[g].expect("GUESS_MADE")  # r1 stays silent -> feedback timeout
        result = clients[g].expect("TURN_RESULT")
        self.assertEqual(result["forfeited"], [r1, r2])
        self.assertIs(result["skipped"], False)
        self.assertEqual(result["results"], [])

    def test_commit_timeout_aborts(self):
        server = self.start_server("--players", "2", "--commit-timeout", "0.5")
        a, b = self.connect(server), self.connect(server)
        a.hello("A")
        b.hello("B")
        a.expect("GAME_START")
        _, commit = bc.make_commit("0275")
        a.send({"type": "COMMIT", "commit": commit})  # b never commits
        b.expect_error("TIMEOUT", fatal=True)
        self.assertTrue(b.closed_within(5))
        over = a.expect("GAME_OVER")
        self.assertEqual((over["reason"], over["turns"], over["ranking"]), ("ABORTED", 0, []))
        self.assertNotIn("GAME_OVER", [m["type"] for m in b.inbox])  # removed players get nothing


class RobustnessTests(ServerTestCase):
    def test_client_that_does_not_read_is_dropped_without_blocking_others(self):
        server = self.start_server("--players", "3")
        flood = self.connect(server)
        flood.hello("flood")
        big = "n" * 3000
        ping = bc.encode({"type": "PING", "nonce": big})
        flood.sock.settimeout(10)
        try:
            for _ in range(3000):  # ~9 MB of PONGs that this client never reads
                flood.sock.sendall(ping)
        except OSError:
            pass  # the server may already have dropped us
        other = self.connect(server)
        other.hello("other")
        other.send({"type": "PING", "nonce": "quick"})
        self.assertEqual(other.expect("PONG", timeout=3)["nonce"], "quick")
        wait_for(lambda: any("does not read" in line for line in server.lines), what="slow client dropped")


class ProcessTests(unittest.TestCase):
    """The real command line, as the graders' pre-check runs it (§6.4.1)."""

    def run_server(self, *options):
        work = tempfile.mkdtemp(prefix="bc-server-proc-")
        self.addCleanup(shutil.rmtree, work, True)
        out = subprocess.run([sys.executable, str(ROOT / "bc.py"), "server", "--port", "0",
                              "--log-dir", "logs", *options], cwd=work, capture_output=True,
                             text=True, encoding="utf-8", timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        files = sorted(glob.glob(os.path.join(work, "logs", "*")))
        overs = []
        for path in files:
            with open(path, encoding="utf-8") as f:
                records = [json.loads(line) for line in f]
            overs.append([r["msg"] for r in records if r["msg"]["type"] == "GAME_OVER"][0])
            welcomes = [r["msg"]["player_id"] for r in records if r["msg"]["type"] == "WELCOME"]
            self.assertEqual(sorted(welcomes), [f"p{i + 1}" for i in range(len(welcomes))])
        return files, overs

    def test_bots_finish_a_game_alone(self):
        files, overs = self.run_server("--bots", "2", "--players", "2")
        self.assertEqual(len(files), 1)
        self.assertEqual(overs[0]["reason"], "LAST_PLAYER")
        self.assertEqual(sum(1 for r in overs[0]["ranking"] if r["rank"] == 1), 1)

    def test_several_games_one_log_each(self):
        files, overs = self.run_server("--bots", "3", "--players", "3", "--games", "2")
        self.assertEqual(len(files), 2)
        self.assertEqual(len({o["game_id"] for o in overs}), 2)

    def test_bad_options_exit_with_2(self):
        out = subprocess.run([sys.executable, str(ROOT / "bc.py"), "server", "--players", "9"],
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(out.returncode, 2)
        self.assertIn("--players", out.stdout)


if __name__ == "__main__":
    unittest.main()
