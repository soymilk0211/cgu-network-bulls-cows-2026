"""Tests for the AI player (issues #3, #11): inference, v1/v2 strategies, timing, protocol."""

import contextlib
import io
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bc  # noqa: E402
from test_client import PLAYERS, RULES, SALT2, FakeServer, wait_for  # noqa: E402
from test_server import ServerThread  # noqa: E402


def turn_result(turn_id, guesser, guess, results, eliminated=()):
    return {"type": "TURN_RESULT", "turn_id": turn_id, "guesser": guesser, "guess": guess,
            "skipped": False, "results": [{"player": p, "a": a, "b": b} for p, a, b in results],
            "eliminated": list(eliminated), "forfeited": [], "disqualified": []}


# The public results of the complete example game in spec §15 (p1 = G03, p2 = G07, p3 = G12).
SPEC_EXAMPLE = [
    turn_result(1, "p3", "0927", [("p2", 2, 0), ("p1", 1, 2)]),
    turn_result(2, "p2", "0273", [("p3", 1, 0), ("p1", 3, 0)]),
    turn_result(3, "p1", "0524", [("p3", 1, 2), ("p2", 2, 0)]),
    turn_result(4, "p3", "0271", [("p2", 0, 2), ("p1", 3, 0)]),
    turn_result(5, "p2", "0274", [("p3", 1, 1), ("p1", 3, 0)]),
    turn_result(6, "p1", "3527", [("p3", 0, 1), ("p2", 4, 0)], eliminated=["p2"]),
    turn_result(7, "p3", "0275", [("p1", 4, 0)], eliminated=["p1"]),
]


class OfflineAI(bc.AIPlayer):
    """An AIPlayer fed with messages directly, without a network connection."""

    def __init__(self, me, log_dir):
        self.lines = []
        super().__init__("127.0.0.1", 1, "G00-AI", "G00", out=self.lines.append, log_dir=log_dir)
        self.me = me


class InferenceTests(unittest.TestCase):
    def setUp(self):
        self.log_dir = tempfile.mkdtemp(prefix="bc-ai-test-")
        self.addCleanup(shutil.rmtree, self.log_dir, True)

    def observer(self, me):
        ai = OfflineAI(me, self.log_dir)
        ai.handle_message({"type": "COMMITS", "order": ["p3", "p2", "p1"],
                           "commits": {"p1": "0" * 64, "p2": "1" * 64, "p3": "2" * 64}})
        return ai

    def test_spec_example_narrows_every_opponent(self):
        ai = self.observer("p3")  # we watch the §15 game as p3
        self.assertEqual(set(ai.cands), {"p1", "p2"})
        for msg in SPEC_EXAMPLE[:5]:
            ai.handle_message(msg)
        # Every result about p1/p2 was used, including guesses made by others.
        self.assertIn("0275", ai.cands["p1"])  # p1's real secret
        self.assertIn("3527", ai.cands["p2"])  # p2's real secret
        for pid in ("p1", "p2"):
            for cand in ai.cands[pid]:
                for turn_id, guess, a, b in ai.history[pid]:
                    self.assertEqual(bc.score(guess, cand), (a, b))
        self.assertEqual(len(ai.cands["p1"]), 3)  # 0275, 0276, 0278 fit p1's answers
        self.assertEqual(ai.history["p3"], [(2, "0273", 1, 0), (3, "0524", 1, 2), (5, "0274", 1, 1)])
        ai.handle_message(SPEC_EXAMPLE[5])  # p2 is guessed -> no longer a target
        self.assertNotIn("p2", ai.cands)
        self.assertEqual(ai.alive, ["p3", "p1"])

    def test_skipped_turn_changes_nothing(self):
        ai = self.observer("p1")
        before = {p: len(c) for p, c in ai.cands.items()}
        ai.handle_message({"type": "TURN_RESULT", "turn_id": 1, "guesser": "p2", "guess": None,
                           "skipped": True, "results": [], "eliminated": [], "forfeited": [],
                           "disqualified": []})
        self.assertEqual({p: len(c) for p, c in ai.cands.items()}, before)

    def test_forfeited_and_disqualified_opponents_are_dropped(self):
        ai = self.observer("p1")
        ai.handle_message({"type": "TURN_RESULT", "turn_id": 1, "guesser": "p2", "guess": "0123",
                           "skipped": False, "results": [{"player": "p1", "a": 0, "b": 0}],
                           "eliminated": [], "forfeited": ["p2"], "disqualified": ["p3"]})
        self.assertEqual(ai.cands, {})


class StrategyTests(unittest.TestCase):
    def test_target_is_the_opponent_with_fewest_candidates(self):
        cands = {"p1": ["0123", "4567", "8901"], "p2": ["1234", "5678"], "p3": list(bc.ALL_NUMBERS)}
        self.assertEqual(bc.pick_target(cands), "p2")
        self.assertEqual(bc.pick_target({"p1": [], "p2": ["0123", "4567"]}), "p2")  # empty skipped
        self.assertIsNone(bc.pick_target({}))
        self.assertIsNone(bc.pick_target({"p1": []}))

    def test_guess_targets_the_smallest_set(self):
        small = bc.filter_candidates(bc.ALL_NUMBERS, "0123", 3, 0)
        cands = {"p1": list(bc.ALL_NUMBERS), "p2": small}
        self.assertIn(bc.choose_guess_v1(cands), small)

    def test_most_parts_is_optimal_on_a_small_set(self):
        cands = bc.filter_candidates(bc.ALL_NUMBERS, "0123", 1, 1)  # 720 candidates
        cands = bc.filter_candidates(cands, "4567", 1, 0)  # a few dozen left
        self.assertLessEqual(len(cands), bc.AI_SAMPLE_SIZE)  # exact, no sampling
        best = max(bc.count_parts(g, cands) for g in cands)
        guess = bc.most_parts_guess(cands, budget=5.0, rng=random.Random(7))
        self.assertIn(guess, cands)
        self.assertEqual(bc.count_parts(guess, cands), best)

    def test_guess_is_always_legal(self):
        rng = random.Random(3)
        for size in (1, 2, 3, 10, 200, 2000, 5040):
            cands = {"p2": rng.sample(bc.ALL_NUMBERS, size)}
            guess = bc.choose_guess_v1(cands, budget=0.2, rng=rng)
            self.assertTrue(bc.is_valid_number(guess))
            self.assertIn(guess, cands["p2"])
        self.assertTrue(bc.is_valid_number(bc.choose_guess_v1({})))  # nobody left: random

    def test_worst_case_decisions_are_fast(self):
        # Second move with a full set, and the biggest sets after one answer (0A1B: 1440, 0A2B: 1260).
        cases = [list(bc.ALL_NUMBERS),
                 bc.filter_candidates(bc.ALL_NUMBERS, "0123", 0, 1),
                 bc.filter_candidates(bc.ALL_NUMBERS, "0123", 0, 2)]
        for cands in cases:
            start = time.monotonic()
            guess = bc.choose_guess_v1({"p2": cands, "p3": list(bc.ALL_NUMBERS)})
            self.assertLess(time.monotonic() - start, 1.5, f"{len(cands)} candidates")
            self.assertIn(guess, cands)

    def test_v1_solves_single_opponent_games(self):
        rng = random.Random(2026)
        counts = []
        for _ in range(25):
            secret = rng.choice(bc.ALL_NUMBERS)
            cands = list(bc.ALL_NUMBERS)
            for n in range(1, 11):
                guess = bc.choose_guess_v1({"p2": cands}, budget=0.3, rng=rng)
                a, b = bc.score(guess, secret)
                if (a, b) == (4, 0):
                    counts.append(n)
                    break
                cands = bc.filter_candidates(cands, guess, a, b)
                self.assertIn(secret, cands)
        self.assertEqual(len(counts), 25)  # every game solved within 10 guesses
        self.assertLessEqual(max(counts), 8)
        self.assertLess(sum(counts) / len(counts), 6.0)


def weighted_score(guess, cands, target):
    """v2's score of a guess, computed exactly (for lists no bigger than AI_V2_SAMPLE)."""
    value = bc.answer_entropy(guess, cands[target])
    return value + bc.AI_V2_WEIGHT * sum(bc.answer_entropy(guess, c)
                                         for pid, c in cands.items() if pid != target and c)


class V2StrategyTests(unittest.TestCase):
    def test_answer_entropy(self):
        self.assertEqual(bc.answer_entropy("0123", ["0123"]), 0.0)
        self.assertAlmostEqual(bc.answer_entropy("0123", ["0123", "4567"]), 1.0)
        # answers 0A0B, 0A0B, 3A0B, 3A0B: two equal groups = 1 bit
        self.assertAlmostEqual(bc.answer_entropy("0123", ["4567", "5678", "0124", "0125"]), 1.0)
        # answers 4A0B, 2A2B, 0A0B, 0A4B: four groups of one = 2 bits
        self.assertAlmostEqual(bc.answer_entropy("0123", ["0123", "0132", "4567", "1032"]), 2.0)

    def test_known_secret_is_guessed_at_once(self):
        cands = {"p2": list(bc.ALL_NUMBERS), "p3": ["5079"], "p4": ["0123", "4567"]}
        self.assertEqual(bc.choose_guess_v2(cands), "5079")

    def test_guess_is_always_legal(self):
        rng = random.Random(4)
        for sizes in ((1,), (2,), (3, 5040), (10, 0, 40), (200, 900), (2000, 3000, 4000), (5040, 5040)):
            cands = {f"p{i + 2}": rng.sample(bc.ALL_NUMBERS, n) for i, n in enumerate(sizes)}
            guess = bc.choose_guess_v2(cands, budget=0.5, rng=rng)
            self.assertTrue(bc.is_valid_number(guess), sizes)
        self.assertTrue(bc.is_valid_number(bc.choose_guess_v2({})))  # nobody left: random
        self.assertTrue(bc.is_valid_number(bc.choose_guess_v2({"p2": []})))  # only a liar left

    def test_choice_uses_every_opponents_candidates(self):
        # Small lists, so v2 scores exactly: its guess must be at least as good as every
        # candidate of the target and of the other opponent (all of them are in the pool).
        target = bc.filter_candidates(bc.ALL_NUMBERS, "0123", 1, 1)
        target = bc.filter_candidates(bc.filter_candidates(target, "4567", 1, 0), "8901", 0, 2)  # 44
        other = bc.filter_candidates(bc.filter_candidates(bc.ALL_NUMBERS, "0123", 0, 2), "4567", 1, 1)
        other = other[:bc.AI_V2_EXTRA]  # 75 of 168, so all of them are in the pool
        self.assertLess(len(target), len(other))
        self.assertLessEqual(len(target), bc.AI_V2_POOL)
        cands = {"p2": target, "p3": other}
        guess = bc.choose_guess_v2(cands, budget=5.0, rng=random.Random(1))
        best = max(weighted_score(g, cands, "p2") for g in target + other)
        self.assertGreaterEqual(weighted_score(guess, cands, "p2"), best - 1e-9)

    def test_worst_case_decisions_are_fast(self):
        # After one public answer: up to 5 opponents with the biggest lists (1440, 1260, ...).
        answers = ((0, 1), (0, 2), (1, 1), (1, 0))
        big = [bc.filter_candidates(bc.ALL_NUMBERS, "0123", a, b) for a, b in answers]
        for opponents in (1, 3, 5):
            cands = {f"p{i + 2}": big[i % len(big)] for i in range(opponents)}
            start = time.monotonic()
            guess = bc.choose_guess_v2(cands)
            self.assertLess(time.monotonic() - start, 1.5, f"{opponents} opponents")
            self.assertTrue(bc.is_valid_number(guess))
        start = time.monotonic()
        bc.choose_guess_v2({f"p{i}": list(bc.ALL_NUMBERS) for i in range(2, 7)})  # cold: all full
        self.assertLess(time.monotonic() - start, 0.1)

    def test_timeout_falls_back_to_v1(self):
        cands = {"p2": bc.filter_candidates(bc.ALL_NUMBERS, "0123", 0, 1), "p3": list(bc.ALL_NUMBERS)}
        stats = {}
        start = time.monotonic()
        guess = bc.choose_guess_v2(cands, budget=0.0, stats=stats)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(stats.get("fallback"))
        self.assertIn(guess, cands["p2"])  # v1 always guesses a target candidate
        stats = {}
        bc.choose_guess_v2(cands, budget=5.0, stats=stats)
        self.assertNotIn("fallback", stats)

    def test_v2_solves_single_opponent_games(self):
        rng = random.Random(2027)
        counts = []
        for _ in range(25):
            secret = rng.choice(bc.ALL_NUMBERS)
            cands = list(bc.ALL_NUMBERS)
            for n in range(1, 11):
                guess = bc.choose_guess_v2({"p2": cands}, budget=0.5, rng=rng)
                a, b = bc.score(guess, secret)
                if (a, b) == (4, 0):
                    counts.append(n)
                    break
                cands = bc.filter_candidates(cands, guess, a, b)
                self.assertIn(secret, cands)
        self.assertEqual(len(counts), 25)
        self.assertLessEqual(max(counts), 8)
        self.assertLess(sum(counts) / len(counts), 6.0)

    def test_strategy_names(self):
        self.assertEqual(bc.AI_STRATEGIES, ("v1", "v2"))
        self.assertIn(bc.DEFAULT_STRATEGY, bc.AI_STRATEGIES)
        with self.assertRaises(ValueError):
            bc.AIPlayer("127.0.0.1", 1, "x", "T", strategy="v3")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bc.build_parser().parse_args(["ai", "--host", "h", "--name", "n", "--strategy", "v3"])


class StrategiesAgainstOurServerTest(unittest.TestCase):
    """`bc.py ai --strategy v1` and `--strategy v2` play a whole game against our server."""

    def test_v1_and_v2_finish_a_game(self):
        server = ServerThread("--players", "2", "--guess-timeout", "5", "--feedback-timeout", "5")
        self.addCleanup(server.stop)
        work = tempfile.mkdtemp(prefix="bc-ai-proc-")
        self.addCleanup(shutil.rmtree, work, True)
        procs = {}
        for strategy in ("v1", "v2"):
            procs[strategy] = subprocess.Popen(
                [sys.executable, str(ROOT / "bc.py"), "ai", "--host", "127.0.0.1", "--port", str(server.port),
                 "--name", f"AI-{strategy}", "--team", "T", "--strategy", strategy],
                cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        outputs = {s: p.communicate(timeout=120)[0] for s, p in procs.items()}
        for strategy, proc in procs.items():
            self.assertEqual(proc.returncode, 0, outputs[strategy])
            self.assertIn(f"({strategy}, took", outputs[strategy])  # the option reached the player
            self.assertIn("GAME OVER", outputs[strategy])
        wait_for(lambda: not server.thread.is_alive(), timeout=10, what="server exit after --games 1")
        over = [r["msg"] for r in server.log_records() if r["msg"]["type"] == "GAME_OVER"]
        self.assertEqual(over[0]["reason"], "LAST_PLAYER")


class AIProtocolTest(unittest.TestCase):
    """The AI against a scripted fake server: everything must be automatic."""

    strategy = "v2"

    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        self.log_dir = tempfile.mkdtemp(prefix="bc-ai-test-")
        self.addCleanup(shutil.rmtree, self.log_dir, True)
        self.out = []
        self.ai = bc.AIPlayer("127.0.0.1", self.server.port, "G00-AI", "G00", out=self.out.append,
                              log_dir=self.log_dir, quiet=True, strategy=self.strategy)
        self.exit_code = []
        self.thread = threading.Thread(target=lambda: self.exit_code.append(self.ai.run()), daemon=True)
        self.thread.start()
        self.server.accept()

    def test_full_game_is_automatic(self):
        hello = self.server.expect("HELLO")
        self.assertEqual(hello["kind"], "ai")
        self.server.send({"type": "WELCOME", "protocol": "BC/1.0", "player_id": "p1"},
                         {"type": "LOBBY", "players": PLAYERS, "min_players": 2, "max_players": 2},
                         {"type": "PING", "nonce": "n-1"})
        self.assertEqual(self.server.expect("PONG")["nonce"], "n-1")
        self.server.send({"type": "GAME_START", "game_id": "g1", "players": PLAYERS, "order": ["p2", "p1"],
                          "rules": dict(RULES, guess_timeout=1.5, x_new=1), "x_extra": 1},
                         {"type": "SOMETHING_NEW"}, {"type": "CHAT", "from": "p2", "text": "hi"},
                         {"type": "ERROR", "code": "UNKNOWN_TYPE", "message": "x", "fatal": False})
        commit = self.server.expect("COMMIT")["commit"]
        self.assertTrue(bc.is_commit(commit))
        self.server.send({"type": "COMMITS", "order": ["p2", "p1"],
                          "commits": {"p2": bc.commit_hash("3527", SALT2), "p1": commit}})

        # Opponent's turn: the AI answers honestly at once.
        self.server.send({"type": "TURN", "turn_id": 1, "guesser": "p2", "alive": ["p2", "p1"]},
                         {"type": "GUESS_MADE", "turn_id": 1, "guesser": "p2", "guess": "0927",
                          "responders": ["p1"]})
        fb = self.server.expect("FEEDBACK", timeout=1.0)
        self.assertEqual(fb["turn_id"], 1)
        self.server.send(turn_result(1, "p2", "0927", [("p1", fb["a"], fb["b"])]))

        # Our turn: one legal guess, well inside the 1.5 s guess timeout.
        self.server.send({"type": "TURN", "turn_id": 2, "guesser": "p1", "alive": ["p2", "p1"]})
        start = time.monotonic()
        guess = self.server.expect("GUESS", timeout=1.5)
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(guess["turn_id"], 2)
        self.assertTrue(bc.is_valid_number(guess["guess"]))
        self.server.send({"type": "GUESS_MADE", "turn_id": 2, "guesser": "p1", "guess": guess["guess"],
                          "responders": ["p2"]},
                         turn_result(2, "p1", guess["guess"], [("p2", *bc.score(guess["guess"], "3527"))]))

        # End: automatic REVEAL that matches the commit, and our feedback was honest.
        self.server.send({"type": "REVEAL_REQUEST", "reason": "GAME_END"})
        reveal = self.server.expect("REVEAL", timeout=1.0)
        self.assertEqual(bc.commit_hash(reveal["secret"], reveal["salt"]), commit)
        self.assertEqual((fb["a"], fb["b"]), bc.score("0927", reveal["secret"]))
        self.server.send({"type": "GAME_OVER", "game_id": "g1", "reason": "MAX_TURNS", "turns": 2,
                          "ranking": [{"player": "p2", "status": "SURVIVOR", "out_turn": None, "rank": 1},
                                      {"player": "p1", "status": "SURVIVOR", "out_turn": None, "rank": 1}]})
        self.thread.join(5)
        self.assertEqual(self.exit_code, [0])
        types = [m["type"] for m in self.server.received]
        self.assertEqual(types, ["HELLO", "PONG", "COMMIT", "FEEDBACK", "GUESS", "REVEAL"])
        # --quiet: only server errors and the final summary line are printed.
        self.assertEqual(len(self.out), 2)
        self.assertIn("[server error] UNKNOWN_TYPE", self.out[0])
        self.assertIn("GAME OVER", self.out[1])

    def test_ai_reveals_when_eliminated(self):
        self.server.expect("HELLO")
        self.server.send({"type": "WELCOME", "protocol": "BC/1.0", "player_id": "p1"},
                         {"type": "GAME_START", "game_id": "g1", "players": PLAYERS, "order": ["p2", "p1"],
                          "rules": RULES})
        commit = self.server.expect("COMMIT")["commit"]
        self.server.send({"type": "COMMITS", "order": ["p2", "p1"],
                          "commits": {"p2": bc.commit_hash("3527", SALT2), "p1": commit}})
        wait_for(lambda: self.ai.secret is not None)
        secret = self.ai.secret
        self.server.send({"type": "TURN", "turn_id": 1, "guesser": "p2", "alive": ["p2", "p1"]},
                         {"type": "GUESS_MADE", "turn_id": 1, "guesser": "p2", "guess": secret,
                          "responders": ["p1"]})
        fb = self.server.expect("FEEDBACK")
        self.assertEqual((fb["a"], fb["b"]), (4, 0))
        self.server.send(turn_result(1, "p2", secret, [("p1", 4, 0)], eliminated=["p1"]),
                         {"type": "REVEAL_REQUEST", "reason": "ELIMINATED"})
        reveal = self.server.expect("REVEAL")
        self.assertEqual(reveal["secret"], secret)
        self.assertEqual(bc.commit_hash(secret, reveal["salt"]), commit)
        self.assertTrue(self.ai.out_of_game)


class AIProtocolTestV1(AIProtocolTest):
    """The same protocol tests with --strategy v1."""

    strategy = "v1"


if __name__ == "__main__":
    unittest.main()
