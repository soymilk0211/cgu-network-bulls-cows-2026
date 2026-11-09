"""Tests for tools/checkpoint1.py (issue #10): prompt driver, log summary, a real run.

The real run uses OUR server (`bc.py server`) through --server-cmd, so it needs
no course files.  Running the tool against the reference server is a manual
step (see the README).
"""

import json
import os
import random
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import bc  # noqa: E402
import checkpoint1  # noqa: E402

TOOL = str(ROOT / "tools" / "checkpoint1.py")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PromptDriverTests(unittest.TestCase):
    """The driver must understand the questions the real HumanClient prints."""

    def setUp(self):
        self.log_dir = tempfile.mkdtemp(prefix="bc-cp1-test-")
        self.addCleanup(shutil.rmtree, self.log_dir, True)
        self.lines = []
        self.client = bc.HumanClient("127.0.0.1", 1, "T-human", "T", out=self.lines.append,
                                     log_dir=self.log_dir, use_stdin=False)
        self.addCleanup(self.client.close)
        self.client.secret = "0275"
        self.rng = random.Random(1)

    def last_prompt(self):
        prompts = [line for line in self.lines if line.startswith("? ")]
        self.assertTrue(prompts, self.lines)
        return prompts[-1]

    def test_secret_prompt_gets_enter(self):
        self.client.begin_commit()
        self.assertEqual(checkpoint1.answer_for_prompt(self.last_prompt(), self.rng), "")

    def test_guess_prompt_gets_a_legal_guess(self):
        self.client.begin_guess(3)
        for _ in range(50):
            self.assertTrue(bc.is_valid_number(checkpoint1.answer_for_prompt(self.last_prompt(), self.rng)))

    def test_feedback_prompt_accepts_the_prefilled_answer(self):
        self.client.begin_feedback(4, "p2", "0927")
        self.assertIn("1A2B", self.last_prompt())
        self.assertEqual(checkpoint1.answer_for_prompt(self.last_prompt(), self.rng), "")

    def test_confirm_prompt_is_never_answered_yes(self):
        self.client.begin_feedback(4, "p2", "0927")
        self.client.answer_feedback(self.client.prompt, "2A0B")
        self.assertEqual(self.client.prompt.kind, "confirm")
        self.assertEqual(checkpoint1.answer_for_prompt(self.last_prompt(), self.rng), "")

    def test_other_lines_are_not_answered(self):
        for line in ("Joined the lobby. Our player id is p1.", "(!) 9 seconds left to answer: x",
                     "[chat] p2(x): ? hello", ""):
            self.assertIsNone(checkpoint1.answer_for_prompt(line, self.rng))


class LogSummaryTests(unittest.TestCase):
    def write_log(self, records):
        folder = tempfile.mkdtemp(prefix="bc-cp1-test-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "x.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for direction, msg in records:
                f.write(json.dumps({"ts": 0, "dir": direction, "peer": "s", "msg": msg}) + "\n")
            f.write("not json\n")  # a broken line is skipped
        return path

    def test_normal_game(self):
        path = self.write_log([
            ("recv", {"type": "WELCOME", "player_id": "p2"}),
            ("recv", {"type": "GAME_START", "game_id": "abc123"}),
            ("send", {"type": "GUESS", "turn_id": 1, "guess": "0123"}),
            ("recv", {"type": "REVEALED", "player": "p2", "verdict": "OK"}),
            ("recv", {"type": "GAME_OVER", "reason": "LAST_PLAYER", "turns": 5, "ranking": [
                {"player": "p2", "rank": 1, "status": "SURVIVOR"},
                {"player": "p1", "rank": 2, "status": "GUESSED"}]}),
        ])
        info = checkpoint1.summarize_log(path)
        self.assertEqual((info["player"], info["game_id"], info["rank"], info["status"], info["guesses"]),
                         ("p2", "abc123", 1, "SURVIVOR", 1))
        self.assertEqual(info["over"], {"reason": "LAST_PLAYER", "turns": 5})
        self.assertEqual(checkpoint1.problems_of(info, 0), [])

    def test_problems_are_reported(self):
        path = self.write_log([
            ("recv", {"type": "WELCOME", "player_id": "p1"}),
            ("recv", {"type": "ERROR", "code": "TIMEOUT", "message": "x", "fatal": False}),
            ("recv", {"type": "TURN_RESULT", "guesser": "p1", "skipped": True}),
            ("recv", {"type": "REVEALED", "player": "p1", "verdict": "INCONSISTENT"}),
        ])
        problems = checkpoint1.problems_of(checkpoint1.summarize_log(path), 1)
        self.assertEqual(problems, ["ERROR TIMEOUT", "no GAME_OVER", "reveal INCONSISTENT",
                                    "1 skipped turn(s)", "exit code 1"])


class CommandLineTests(unittest.TestCase):
    def run_tool(self, *argv, cwd=None, timeout=60):
        return subprocess.run([sys.executable, TOOL, *argv], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout)

    def test_help_documents_the_options(self):
        out = self.run_tool("--help")
        self.assertEqual(out.returncode, 0, out.stderr)
        for option in ("--host", "--port", "--team", "--games", "--name-prefix", "--out",
                       "--server-cmd", "--server-log-dir"):
            self.assertIn(option, out.stdout)

    def test_team_is_required_and_checked(self):
        self.assertEqual(self.run_tool("--games", "1").returncode, 2)  # no default group code
        out = self.run_tool("--team", "x" * 21)
        self.assertEqual(out.returncode, 2)
        self.assertIn("--team", out.stdout)

    def test_two_games_against_our_server(self):
        work = tempfile.mkdtemp(prefix="bc-cp1-run-")
        self.addCleanup(shutil.rmtree, work, True)
        parts = [sys.executable, str(ROOT / "bc.py"), "server"]
        server_cmd = subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)
        out = self.run_tool("--team", "T99", "--games", "2", "--port", str(free_port()), "--out", "out",
                            "--server-cmd", server_cmd, "--seed", "5", cwd=work, timeout=180)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("2/2 games finished", out.stdout)
        [run_dir] = [os.path.join(work, "out", d) for d in os.listdir(os.path.join(work, "out"))]
        with open(os.path.join(run_dir, "summary.json"), encoding="utf-8") as f:
            summary = json.load(f)
        self.assertEqual([g["finished"] for g in summary["games"]], [True, True])
        self.assertEqual(len(summary["server_logs"]), 2)
        for game in summary["games"]:
            self.assertEqual(game["problems"], [])
            self.assertEqual({p["name"] for p in game["players"].values()}, {"T99-human", "T99-ai"})
            human = game["players"]["human"]
            self.assertGreater(human["guesses"], 0)  # the driver really typed guesses
            self.assertTrue(os.path.isfile(human["log"]))
        self.assertEqual(len(os.listdir(os.path.join(run_dir, "client_logs"))), 4)
        self.assertTrue(os.path.isfile(os.path.join(run_dir, "summary.txt")))


if __name__ == "__main__":
    unittest.main()
