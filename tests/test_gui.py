"""Tests for the optional GUI (`bc.py gui`).

Everything here runs without a display and without tkinter: the command line,
the text helpers, and GuiSession (the HumanClient behind the window) against
the scripted FakeServer.  Only WindowTests needs tkinter and a display; it is
skipped where either is missing.
"""

import contextlib
import io
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bc  # noqa: E402
from test_client import PLAYERS, RULES, SALT2, FakeServer, wait_for  # noqa: E402

SNAPSHOT = {"me": "p1", "players": {p["id"]: p for p in PLAYERS}, "order": ["p2", "p1"], "alive": ["p2", "p1"],
            "secret": "0275", "cands": {"p2": 5040}, "out_of_game": False, "game_id": "g1", "game_over": False}


def prompt(kind, turn_id=None, **data):
    return bc.Prompt(kind, "question", time.monotonic() + 30, turn_id, data)


class CommandLineTests(unittest.TestCase):
    def test_gui_options_match_the_client(self):
        a = bc.build_parser().parse_args(["gui", "--host", "127.0.0.1", "--name", "玩家"])
        self.assertEqual((a.command, a.host, a.port, a.name, a.team), ("gui", "127.0.0.1", 5040, "玩家", "G00"))
        a = bc.build_parser().parse_args(["gui", "--host", "h", "--port", "6000", "--name", "n", "--team", "T"])
        self.assertEqual((a.port, a.team), (6000, "T"))
        for argv in (["gui", "--name", "x"], ["gui", "--host", "x"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                bc.build_parser().parse_args(argv)

    def test_help_works_without_a_display(self):
        for sub, words in (((), ["server", "client", "ai", "gui"]),
                           (("gui",), ["--host", "--port", "--name", "--team"])):
            out = subprocess.run([sys.executable, str(ROOT / "bc.py"), *sub, "--help"], capture_output=True,
                                 text=True, encoding="utf-8", timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            for word in words:
                self.assertIn(word, out.stdout)

    def test_missing_tkinter_is_a_clear_error(self):
        args = bc.build_parser().parse_args(["gui", "--host", "127.0.0.1", "--name", "n"])
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"tkinter": None}), contextlib.redirect_stdout(out):
            self.assertEqual(bc.run_gui(args), 1)
        self.assertIn("tkinter", out.getvalue())
        self.assertIn("bc.py client", out.getvalue())

    def test_bad_name_exits_with_2(self):
        args = bc.build_parser().parse_args(["gui", "--host", "127.0.0.1", "--name", "x" * 21])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bc.run_gui(args), 2)


class HelperTests(unittest.TestCase):
    def test_small_helpers(self):
        self.assertEqual(bc.feedback_text(1, 2), "1A2B")
        self.assertEqual(bc.gui_name(SNAPSHOT, "p1"), "p1 G00-測試（你）")
        self.assertEqual(bc.gui_name(SNAPSHOT, "p2"), "p2 對手")
        self.assertEqual(bc.gui_name(SNAPSHOT, "p9"), "p9 ?")
        self.assertEqual(bc.gui_word(bc.GUI_STATUS, "DQ"), "取消資格（DQ）")
        self.assertEqual(bc.gui_word(bc.GUI_STATUS, "NEW"), "NEW")  # unknown codes are shown as they are

    def test_answers_are_checked_like_the_console(self):
        check = bc.check_answer_text
        for kind, text in (("secret", ""), ("secret", "0275"), ("guess", " 0927 "), ("feedback", ""),
                           ("feedback", "1a2b"), ("feedback", "1 2"), ("confirm", "anything")):
            self.assertEqual(check(kind, text), "", (kind, text))
        for kind, text in (("secret", "1123"), ("guess", ""), ("guess", "０２７５"), ("guess", "12345"),
                           ("feedback", "3A1B"), ("feedback", "2A3B"), ("feedback", "0927")):
            self.assertNotEqual(check(kind, text), "", (kind, text))

    def test_prompt_views(self):
        self.assertIsNone(bc.prompt_view(None, SNAPSHOT))
        secret = bc.prompt_view(prompt("secret"), SNAPSHOT)
        self.assertEqual((secret["entry"], secret["buttons"]), ("", [("送出秘密", None), ("隨機", "")]))
        guess = bc.prompt_view(prompt("guess", 4), SNAPSHOT)
        self.assertIn("第 4 回合", guess["title"])
        self.assertEqual(guess["buttons"], [("送出猜測", None)])
        feedback = bc.prompt_view(prompt("feedback", 5, guesser="p2", guess="0273", a=3, b=0), SNAPSHOT)
        self.assertEqual(feedback["entry"], "3A0B")  # prefilled with the honest answer
        self.assertIn("p2 對手", feedback["title"])
        self.assertIn("0275", feedback["hint"])  # our secret
        self.assertIn("棄權", feedback["hint"])  # a missing feedback = FORFEIT
        confirm = bc.prompt_view(prompt("confirm", 5, guesser="p2", guess="0273", a=3, b=0, chosen=(1, 0)),
                                 SNAPSHOT)
        self.assertIsNone(confirm["entry"])
        self.assertEqual(confirm["buttons"], [("確定送出 1A0B", "yes"), ("返回修改", "no")])
        self.assertIn("3A0B", confirm["hint"])
        self.assertIsNone(bc.prompt_view(prompt("other"), SNAPSHOT))

    def test_history_rows(self):
        msg = {"type": "TURN_RESULT", "turn_id": 3, "guesser": "p2", "guess": "0275", "skipped": False,
               "results": [{"player": "p1", "a": 4, "b": 0}, {"player": "p3", "a": 9, "b": 9}],
               "eliminated": ["p1"], "forfeited": ["p3"], "disqualified": []}
        self.assertEqual(bc.turn_result_row(msg, SNAPSHOT),
                         ("3", "p2 對手", "0275", "p1 G00-測試（你） 4A0B", "p1 G00-測試（你） 被猜中、p3 ? 棄權"))
        skipped = {"type": "TURN_RESULT", "turn_id": 4, "guesser": "p1", "guess": None, "skipped": True,
                   "results": [], "eliminated": [], "forfeited": [], "disqualified": []}
        self.assertIn("跳過", bc.turn_result_row(skipped, SNAPSHOT)[3])
        reveal = {"type": "REVEALED", "player": "p2", "secret": "3527", "salt": SALT2,
                  "verdict": "INCONSISTENT", "bad_turns": [2, 6]}
        self.assertEqual(bc.reveal_row(reveal, SNAPSHOT),
                         ("揭示", "p2 對手", "3527", "判定 回報不一致（INCONSISTENT），錯誤回合 [2, 6]", ""))
        over = {"type": "GAME_OVER", "ranking": [
            {"player": "p1", "status": "SURVIVOR", "out_turn": None, "rank": 1},
            {"player": "p2", "status": "DQ", "out_turn": 7, "rank": None}]}
        self.assertEqual(bc.ranking_rows(over, SNAPSHOT),
                         [("第 1 名", "p1 G00-測試（你）", "", "存活（SURVIVOR）", ""),
                          ("第 - 名", "p2 對手", "", "取消資格（DQ）", "第 7 回合出局")])


class GuiSessionTest(unittest.TestCase):
    """The session behind the window, against the scripted fake server (no tkinter)."""

    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        self.log_dir = tempfile.mkdtemp(prefix="bc-gui-test-")
        self.addCleanup(shutil.rmtree, self.log_dir, True)
        self.session = bc.GuiSession("127.0.0.1", self.server.port, "G00-測試", "G00", log_dir=self.log_dir)
        self.seen = []  # every ui event, in order
        self.session.start()
        self.server.accept()

    def next_ui(self, kind, timeout=5.0):
        """The next ui event of this kind (earlier events of other kinds are kept in self.seen)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                event = self.session.ui_events.get(timeout=0.05)
            except queue.Empty:
                continue
            self.seen.append(event)
            if event[0] == kind:
                return event
        raise AssertionError(f"no {kind} ui event")

    def next_prompt(self, kind):
        while True:
            event = self.next_ui("prompt")
            if event[1] is not None:
                seq, p, snapshot = event[1]
                self.assertEqual(p.kind, kind)
                return seq, p, snapshot

    def answer(self, seq, text):
        self.session.events.put(("answer", (seq, text)))

    def start_game(self):
        self.assertEqual(self.server.expect("HELLO")["kind"], "human")
        self.server.send({"type": "WELCOME", "protocol": "BC/1.0", "player_id": "p1"},
                         {"type": "LOBBY", "players": PLAYERS, "min_players": 2, "max_players": 2},
                         {"type": "GAME_START", "game_id": "g1", "players": PLAYERS, "order": ["p2", "p1"],
                          "rules": RULES})

    def test_full_game_through_the_window_queues(self):
        self.start_game()
        seq, p, _ = self.next_prompt("secret")
        lobby = [e for e in self.seen if e[0] == "msg" and e[1]["type"] == "LOBBY"]
        self.assertEqual(set(lobby[0][2]["players"]), {"p1", "p2"})  # the window gets a snapshot
        self.answer(seq, "0275")
        commit = self.server.expect("COMMIT")["commit"]
        self.assertEqual(commit, bc.commit_hash("0275", self.session.salt))
        self.assertIsNone(self.next_ui("prompt")[1])  # answered: the panel is cleared
        self.answer(seq, "1234")  # a late answer to a closed question is never sent
        self.server.send({"type": "COMMITS", "order": ["p2", "p1"],
                          "commits": {"p2": bc.commit_hash("3527", SALT2), "p1": commit}},
                         {"type": "TURN", "turn_id": 1, "guesser": "p1", "alive": ["p2", "p1"]})
        seq, p, _ = self.next_prompt("guess")
        self.answer(seq, "0927")
        self.assertEqual(self.server.expect("GUESS"), {"type": "GUESS", "turn_id": 1, "guess": "0927"})

        # Feedback: prefilled; a changed answer asks for confirmation; "no" goes back.
        self.server.send({"type": "GUESS_MADE", "turn_id": 1, "guesser": "p1", "guess": "0927",
                          "responders": ["p2"]},
                         {"type": "TURN_RESULT", "turn_id": 1, "guesser": "p1", "guess": "0927", "skipped": False,
                          "results": [{"player": "p2", "a": 1, "b": 1}], "eliminated": [], "forfeited": [],
                          "disqualified": []},
                         {"type": "TURN", "turn_id": 2, "guesser": "p2", "alive": ["p2", "p1"]},
                         {"type": "GUESS_MADE", "turn_id": 2, "guesser": "p2", "guess": "0273", "responders": ["p1"]})
        seq, p, snapshot = self.next_prompt("feedback")
        self.assertEqual((p.data["a"], p.data["b"]), (3, 0))
        self.assertEqual(bc.prompt_view(p, snapshot)["entry"], "3A0B")
        self.answer(seq, "1A0B")
        seq, p, _ = self.next_prompt("confirm")
        self.assertEqual(p.data["chosen"], (1, 0))
        self.server.assert_silent(0.3)  # nothing is sent before the confirmation
        self.answer(seq, "no")
        seq, p, _ = self.next_prompt("feedback")
        self.answer(seq, "")  # Enter / the button with the prefilled answer
        self.assertEqual(self.server.expect("FEEDBACK"), {"type": "FEEDBACK", "turn_id": 2, "a": 3, "b": 0})

        self.session.events.put(("input", "/chat 你好"))
        self.assertEqual(self.server.expect("CHAT")["text"], "你好")
        self.server.send({"type": "REVEAL_REQUEST", "reason": "GAME_END"})
        reveal = self.server.expect("REVEAL")  # automatic, no question in the window
        self.assertEqual((reveal["secret"], reveal["salt"]), ("0275", self.session.salt))
        self.server.send({"type": "GAME_OVER", "game_id": "g1", "reason": "MAX_TURNS", "turns": 2,
                          "ranking": [{"player": "p2", "status": "SURVIVOR", "out_turn": None, "rank": 1},
                                      {"player": "p1", "status": "SURVIVOR", "out_turn": None, "rank": 1}]})
        self.assertEqual(self.next_ui("done")[1], 0)
        types = [m["type"] for m in self.server.received]
        self.assertEqual(types, ["HELLO", "COMMIT", "GUESS", "FEEDBACK", "CHAT", "REVEAL"])
        texts = [e[1] for e in self.seen if e[0] == "text"]
        self.assertTrue(any("too late" in t for t in texts))
        msgs = [e[1]["type"] for e in self.seen if e[0] == "msg"]
        self.assertIn("TURN_RESULT", msgs)
        self.assertEqual(msgs[-1], "GAME_OVER")

    def test_quit_sends_bye(self):
        self.start_game()
        self.next_prompt("secret")
        self.session.events.put(("input", "/quit"))
        self.server.expect("BYE")
        self.assertEqual(self.next_ui("done")[1], 1)


class PlayAgainHelperTests(unittest.TestCase):
    def test_fresh_state_is_clean_and_not_shared(self):
        a, b = bc.fresh_gui_state(), bc.fresh_gui_state()
        self.assertEqual(a, {"out_status": {}, "result": None, "closing": None, "last_problem": None,
                             "rejoining": False, "prompt": None})
        a["out_status"]["p2"] = "DQ"
        self.assertEqual(b["out_status"], {})  # every reset gets its own dict

    def test_rejoin_error_is_chinese_and_mentions_games_0(self):
        text = bc.rejoin_error_text("127.0.0.1:5040", "[server error] GAME_IN_PROGRESS: x (fatal)")
        for word in ("127.0.0.1:5040", "再玩一局", "--games 0", "GAME_IN_PROGRESS"):
            self.assertIn(word, text)
        self.assertNotIn("伺服器回應", bc.rejoin_error_text("h:1", None))


class PlayAgainSessionTest(unittest.TestCase):
    """next_game(): a second whole game with a NEW connection (headless, scripted server)."""

    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        self.log_dir = tempfile.mkdtemp(prefix="bc-gui-test-")
        self.addCleanup(shutil.rmtree, self.log_dir, True)

    def wait_done(self, session):
        self.texts = []
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                event = session.ui_events.get(timeout=0.05)
            except queue.Empty:
                continue
            if event[0] == "text":
                self.texts.append(event[1])
            if event[0] == "done":
                return event[1]
        raise AssertionError("session did not end")

    def play_one(self, session):
        """Join, then end the game at once with GAME_OVER (the server then closes)."""
        session.start()
        self.server.accept()
        self.assertEqual(self.server.expect("HELLO")["name"], "G00-測試")
        self.server.send({"type": "WELCOME", "protocol": "BC/1.0", "player_id": "p1"},
                         {"type": "GAME_OVER", "game_id": "g", "reason": "ABORTED", "turns": 0, "ranking": []})
        self.assertEqual(self.wait_done(session), 0)
        self.server.conn.close()

    def test_next_game_is_a_fresh_connection_with_the_same_identity(self):
        first = bc.GuiSession("127.0.0.1", self.server.port, "G00-測試", "G00", log_dir=self.log_dir)
        self.play_one(first)
        self.assertTrue(first.game_over)
        second = first.next_game()
        self.assertIsNot(second, first)
        self.assertEqual((second.host, second.port, second.name, second.team, second.log_dir),
                         (first.host, first.port, first.name, first.team, first.log_dir))
        self.assertFalse(second.game_over)  # no state carried over
        self.assertEqual((second.me, second.history, second.cands, second.secret), (None, {}, {}, None))
        self.play_one(second)  # HELLO again, WELCOME again, GAME_OVER again
        self.assertTrue(second.game_over)

    def test_unreachable_server_ends_with_exit_1_and_can_be_retried(self):
        port = self.server.port
        self.server.close()  # nobody listens any more
        session = bc.GuiSession("127.0.0.1", port, "G00-測試", "G00", log_dir=self.log_dir)
        session.start()
        self.assertEqual(self.wait_done(session), 1)
        self.assertTrue(any(t.startswith("Cannot connect") for t in self.texts), self.texts)
        retry = session.next_game()  # a retry is just another new session
        retry.start()
        self.assertEqual(self.wait_done(retry), 1)


def make_root():
    """A Tk root window, or None if tkinter or a display is missing."""
    try:
        import tkinter
        return tkinter.Tk()
    except Exception:  # noqa: BLE001 - ImportError, or TclError without a display
        return None


class WindowTests(unittest.TestCase):
    """The real window, fed with events by hand.  Skipped without tkinter or a display."""

    def setUp(self):
        self.root = make_root()
        if self.root is None:
            self.skipTest("tkinter or a display is not available")
        self.addCleanup(self.root.destroy)
        import tkinter
        from tkinter import font, messagebox, ttk
        self.log_dir = tempfile.mkdtemp(prefix="bc-gui-test-")
        self.addCleanup(shutil.rmtree, self.log_dir, True)
        self.session = bc.GuiSession("127.0.0.1", 1, "G00-測試", "G00", log_dir=self.log_dir)
        self.addCleanup(self.session.close)
        self.app = bc.GuiApp(self.root, self.session, tkinter, ttk, font, messagebox)

    def feed(self, *events):
        for event in events:
            self.session.ui_events.put(event)
        self.app.poll()

    def test_panel_history_and_end(self):
        app = self.app
        self.assertEqual(app.action_title.cget("text"), "目前不需要你動作")
        self.feed(("msg", {"type": "WELCOME", "player_id": "p1", "protocol": "BC/1.0"}, SNAPSHOT),
                  ("text", "Joined the lobby."),
                  ("prompt", (7, prompt("feedback", 2, guesser="p2", guess="0273", a=3, b=0), SNAPSHOT)))
        self.assertIn("已連線", app.conn_var.get())
        self.assertEqual(app.entry_var.get(), "3A0B")
        self.assertEqual([b.cget("text") for b in app.buttons], ["送出回報"])
        app.entry_var.set("9A9B")
        app.answer(None)  # refused locally
        self.assertNotEqual(app.error_var.get(), "")
        self.assertTrue(self.session.events.empty())
        app.entry_var.set("3A0B")
        app.answer(None)
        self.assertEqual(self.session.events.get_nowait(), ("answer", (7, "3A0B")))
        self.assertTrue(app.buttons[0].instate(["disabled"]))  # until the next question
        self.feed(("prompt", None),
                  ("msg", {"type": "TURN_RESULT", "turn_id": 2, "guesser": "p2", "guess": "0273", "skipped": False,
                           "results": [{"player": "p1", "a": 3, "b": 0}], "eliminated": [], "forfeited": [],
                           "disqualified": []}, SNAPSHOT),
                  ("msg", {"type": "GAME_OVER", "reason": "LAST_PLAYER", "turns": 2, "ranking": [
                      {"player": "p1", "status": "SURVIVOR", "out_turn": None, "rank": 1}]}, SNAPSHOT),
                  ("done", 0))
        self.assertEqual(app.buttons, [])
        self.assertEqual(len(app.history_view.get_children()), 2)  # one turn + one ranking row
        self.assertIn("你的名次：1", app.status_var.get())
        self.assertEqual(app.result, 0)
        self.assertEqual(app.quit_button.cget("text"), "關閉視窗")
        self.assertEqual(len(app.players_view.get_children()), 2)


    def test_play_again_resets_the_window_and_uses_a_new_session(self):
        app, old = self.app, self.session
        self.feed(("msg", {"type": "WELCOME", "player_id": "p1", "protocol": "BC/1.0"}, SNAPSHOT),
                  ("msg", {"type": "TURN_RESULT", "turn_id": 1, "guesser": "p2", "guess": "0273", "skipped": False,
                           "results": [{"player": "p1", "a": 3, "b": 0}], "eliminated": ["p2"], "forfeited": [],
                           "disqualified": []}, SNAPSHOT),
                  ("done", 0))
        self.assertTrue(app.again_button.winfo_ismapped() or app.again_button.winfo_manager())
        self.assertEqual(app.out_status, {"p2": "GUESSED"})
        started = []
        with mock.patch.object(bc.GuiSession, "start", lambda self: started.append(self)):
            app.play_again()
        self.assertIsNot(app.session, old)
        self.assertEqual(started, [app.session])
        self.assertEqual((app.out_status, app.result, app.rejoining), ({}, None, True))
        self.assertEqual(app.history_view.get_children(), ())
        self.assertEqual(app.players_view.get_children(), ())
        self.assertIn("尚未選擇", app.secret_var.get())
        self.assertIn("重新加入", app.status_var.get())
        self.assertFalse(app.again_button.winfo_manager())  # hidden while connecting
        self.addCleanup(app.session.close)
        app.play_again()  # ignored while connected / connecting
        self.assertEqual(started, [app.session])

    def test_failed_rejoin_keeps_the_window_and_allows_retry(self):
        app = self.app
        self.feed(("done", 0))
        shown = []
        app.messagebox = mock.Mock(showerror=lambda *a, **k: shown.append(a))
        with mock.patch.object(bc.GuiSession, "start", lambda self: None):
            app.play_again()
        self.addCleanup(app.session.close)
        for event in (("text", "Cannot connect to 127.0.0.1:5040: refused"), ("done", 1)):
            app.session.ui_events.put(event)  # from the NEW session
        app.poll()
        self.assertEqual(len(shown), 1)
        self.assertIn("再玩一局", shown[0][1])
        self.assertFalse(app.rejoining)
        self.assertTrue(app.again_button.winfo_manager())  # retry is possible
        self.assertEqual(app.result, 1)


if __name__ == "__main__":
    unittest.main()
