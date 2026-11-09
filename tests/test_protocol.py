"""Tests for the shared protocol layer in bc.py (issue #1).

Run with:  python -m pytest -q      (or: python -m unittest discover -s tests -v)
"""

import json
import socket
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bc  # noqa: E402


def line_of_exact_size(total_bytes):
    """A valid JSON message line whose length INCLUDING the LF is total_bytes."""
    head, tail = b'{"type":"CHAT","text":"', b'"}\n'
    filler = total_bytes - len(head) - len(tail)
    return head + b"x" * filler + tail


class LineBufferTests(unittest.TestCase):
    def test_one_message_split_across_three_feeds(self):
        buf = bc.LineBuffer()
        self.assertEqual(buf.feed(b'{"type":'), [])
        self.assertEqual(buf.feed(b'"PI'), [])
        self.assertEqual(buf.feed(b'NG"}\n'), [b'{"type":"PING"}'])
        self.assertEqual(buf.pending, 0)

    def test_three_messages_in_one_feed(self):
        buf = bc.LineBuffer()
        lines = buf.feed(b'{"type":"A"}\n{"type":"B"}\n{"type":"C"}\n{"ty')
        self.assertEqual(lines, [b'{"type":"A"}', b'{"type":"B"}', b'{"type":"C"}'])
        self.assertEqual(buf.feed(b'pe":"D"}\n'), [b'{"type":"D"}'])

    def test_crlf_is_stripped(self):
        buf = bc.LineBuffer()
        self.assertEqual(buf.feed(b'{"type":"PING"}\r\n'), [b'{"type":"PING"}'])

    def test_only_one_cr_is_stripped(self):
        buf = bc.LineBuffer()
        self.assertEqual(buf.feed(b"abc\r\r\n"), [b"abc\r"])

    def test_empty_lines_are_ignored(self):
        buf = bc.LineBuffer()
        self.assertEqual(buf.feed(b'\n\r\n{"type":"PING"}\n\n  \t\r\n'), [b'{"type":"PING"}'])

    def test_exactly_4096_bytes_including_lf_is_accepted(self):
        line = line_of_exact_size(4096)
        self.assertEqual(len(line), 4096)
        lines = bc.LineBuffer().feed(line)
        self.assertEqual(lines, [line[:-1]])
        self.assertEqual(bc.decode(lines[0])["type"], "CHAT")

    def test_exactly_4096_bytes_in_small_pieces_is_accepted(self):
        line = line_of_exact_size(4096)
        buf = bc.LineBuffer()
        out = []
        for i in range(0, len(line), 100):
            out += buf.feed(line[i:i + 100])
        self.assertEqual(out, [line[:-1]])

    def test_4097_bytes_is_rejected(self):
        line = line_of_exact_size(4097)
        with self.assertRaises(bc.LineTooLong):
            bc.LineBuffer().feed(line)

    def test_long_line_without_lf_is_rejected_without_growing(self):
        buf = bc.LineBuffer()
        with self.assertRaises(bc.LineTooLong):
            for _ in range(100):  # 100 * 1000 bytes, never an LF
                buf.feed(b"y" * 1000)
                self.assertLess(buf.pending, bc.MAX_LINE_BYTES)
        self.assertEqual(buf.pending, 0)

    def test_4095_bytes_without_lf_is_still_waiting(self):
        buf = bc.LineBuffer()
        self.assertEqual(buf.feed(b"z" * 4095), [])
        self.assertEqual(buf.pending, 4095)

    def test_lines_before_a_long_line_are_kept(self):
        buf = bc.LineBuffer()
        with self.assertRaises(bc.LineTooLong) as ctx:
            buf.feed(b'{"type":"A"}\n' + b"q" * 5000 + b"\n")
        self.assertEqual(ctx.exception.complete, [b'{"type":"A"}'])

    def test_multibyte_utf8_split_mid_character(self):
        msg = {"type": "HELLO", "name": "玩家一號"}
        data = bc.encode(msg)
        cut = data.index("家".encode("utf-8")) + 1  # inside the 3-byte character
        buf = bc.LineBuffer()
        self.assertEqual(buf.feed(data[:cut]), [])
        lines = buf.feed(data[cut:])
        self.assertEqual(bc.decode(lines[0]), msg)


class DecodeEncodeTests(unittest.TestCase):
    def test_rejects_non_object(self):
        with self.assertRaises(bc.BadJson):
            bc.decode(b"[1,2]")

    def test_rejects_non_string_type(self):
        with self.assertRaises(bc.BadJson):
            bc.decode(b'{"type":1}')

    def test_rejects_missing_type(self):
        with self.assertRaises(bc.BadJson):
            bc.decode(b'{"a":1}')

    def test_rejects_nan_and_infinity(self):
        for bad in (b'{"type":"X","v":NaN}', b'{"type":"X","v":Infinity}',
                    b'{"type":"X","v":-Infinity}', b"NaN"):
            with self.subTest(bad=bad), self.assertRaises(bc.BadJson):
                bc.decode(bad)

    def test_rejects_garbage_bom_and_bad_utf8(self):
        for bad in (b"hello", b'{"type":"X"', b'\xef\xbb\xbf{"type":"X"}', b'{"type":"\xff"}'):
            with self.subTest(bad=bad), self.assertRaises(bc.BadJson):
                bc.decode(bad)

    def test_rejects_absurd_nesting_without_crashing(self):
        with self.assertRaises(bc.BadJson):
            bc.decode(b"[" * 3000 + b"]" * 3000)

    def test_accepts_unknown_extra_fields(self):
        msg = bc.decode(b'{"type":"PING","nonce":"n1","future_field":{"x":[1,2]}}')
        self.assertEqual(msg["type"], "PING")
        self.assertEqual(msg["future_field"], {"x": [1, 2]})

    def test_encode_is_compact_utf8_with_lf(self):
        data = bc.encode({"type": "CHAT", "text": "你好"})
        self.assertEqual(data, '{"type":"CHAT","text":"你好"}\n'.encode("utf-8"))

    def test_encode_decode_round_trip(self):
        msg = {"type": "FEEDBACK", "turn_id": 3, "a": 1, "b": 2}
        self.assertEqual(bc.decode(bc.encode(msg)[:-1]), msg)

    def test_encode_rejects_nan_and_oversize(self):
        with self.assertRaises(ValueError):
            bc.encode({"type": "X", "v": float("nan")})
        with self.assertRaises(ValueError):
            bc.encode({"type": "CHAT", "text": "x" * 5000})

    def test_send_msg_sends_whole_line(self):
        a, b = socket.socketpair()
        try:
            bc.send_msg(a, {"type": "PING", "nonce": "abc"})
            a.close()
            received = b""
            while chunk := b.recv(1024):
                received += chunk
        finally:
            b.close()
        self.assertEqual(received, b'{"type":"PING","nonce":"abc"}\n')


class ValidatorTests(unittest.TestCase):
    def test_valid_numbers(self):
        for good in ("0275", "1234", "9876", "0123"):
            self.assertTrue(bc.is_valid_number(good), good)

    def test_invalid_numbers(self):
        for bad in ("0０75", "０２７５", "1123", "123", "12345", "12a4", " 123", "123\n",
                    1234, None, ["1", "2", "3", "4"]):
            self.assertFalse(bc.is_valid_number(bad), repr(bad))

    def test_is_int_rejects_bool_and_float(self):
        self.assertTrue(bc.is_int(0))
        self.assertTrue(bc.is_int(7))
        for bad in (True, False, 1.0, "1", None):
            self.assertFalse(bc.is_int(bad), repr(bad))

    def test_feedback(self):
        self.assertTrue(bc.is_valid_feedback(1, 2))
        self.assertTrue(bc.is_valid_feedback(4, 0))
        self.assertTrue(bc.is_valid_feedback(0, 0))
        self.assertFalse(bc.is_valid_feedback(3, 1))  # impossible pair
        self.assertFalse(bc.is_valid_feedback(2, 3))  # a + b > 4
        self.assertFalse(bc.is_valid_feedback(True, 0))  # bool is not an int here
        self.assertFalse(bc.is_valid_feedback(1.0, 2))
        self.assertFalse(bc.is_valid_feedback(-1, 2))

    def test_exactly_14_legal_feedback_pairs(self):
        self.assertEqual(len(bc.LEGAL_FEEDBACK), 14)
        seen = {bc.score("0123", s) for s in bc.ALL_NUMBERS}  # every pair really occurs
        self.assertEqual(seen, set(bc.LEGAL_FEEDBACK))

    def test_hex(self):
        self.assertTrue(bc.is_salt("2eb6594c74b9b9d71e2d9b5bd14300df"))
        self.assertFalse(bc.is_salt("2EB6594C74B9B9D71E2D9B5BD14300DF"))  # uppercase
        self.assertFalse(bc.is_salt("2eb6594c74b9b9d71e2d9b5bd14300d"))  # 31 chars
        self.assertFalse(bc.is_salt("2eb6594c74b9b9d71e2d9b5bd14300dg"))  # not hex
        self.assertTrue(bc.is_commit("ab" * 32))
        self.assertFalse(bc.is_commit("ab" * 16))
        self.assertFalse(bc.is_commit(None))


    def test_text_fields(self):  # name/team 1-20, CHAT 1-200, no control characters (§9)
        self.assertTrue(bc.is_valid_text("G07-小明", 1, 20))
        self.assertTrue(bc.is_valid_text("x" * 20, 1, 20))
        self.assertFalse(bc.is_valid_text("x" * 21, 1, 20))
        self.assertFalse(bc.is_valid_text("", 1, 20))
        self.assertFalse(bc.is_valid_text("a\tb", 1, 20))  # Cc
        self.assertFalse(bc.is_valid_text("a\u200bb", 1, 20))  # Cf (zero-width space)
        self.assertFalse(bc.is_valid_text(None, 1, 20))
        self.assertTrue(bc.is_valid_text("大家好", 1, 200))

    def test_protocol_version(self):  # §9, §16.2
        for good in ("BC/1.0", "BC/1.1", "BC/1.12"):
            self.assertTrue(bc.is_supported_protocol(good), good)
        for bad in ("BC/2.0", "BC/1", "bc/1.0", "BC/1.0 ", "BC/1.x", None, 1.0):
            self.assertFalse(bc.is_supported_protocol(bad), repr(bad))


class GameMathTests(unittest.TestCase):
    def test_commit_test_vector(self):  # §3.1
        self.assertEqual(
            bc.commit_hash("0275", "2eb6594c74b9b9d71e2d9b5bd14300df"),
            "6b05c8fe4113aa24833fcbd089d33f0fe48ff68eb0a7d7fff87319a394338721",
        )

    def test_make_commit(self):
        salt, commit = bc.make_commit("0275")
        self.assertTrue(bc.is_salt(salt))
        self.assertTrue(bc.is_commit(commit))
        self.assertEqual(commit, bc.commit_hash("0275", salt))
        self.assertNotEqual(bc.make_commit("0275")[0], salt)  # fresh salt each time

    def test_score_example(self):  # §2.2
        self.assertEqual(bc.score("0927", "0275"), (1, 2))
        self.assertEqual(bc.score("0275", "0275"), (4, 0))
        self.assertEqual(bc.score("1346", "0275"), (0, 0))
        self.assertEqual(bc.score("5720", "0275"), (0, 4))

    def test_all_numbers(self):
        self.assertEqual(len(bc.ALL_NUMBERS), 5040)
        self.assertEqual(len(set(bc.ALL_NUMBERS)), 5040)
        self.assertTrue(all(bc.is_valid_number(n) for n in bc.ALL_NUMBERS))
        self.assertIn("0123", bc.ALL_NUMBERS)

    def test_filter_candidates(self):
        cands = bc.filter_candidates(bc.ALL_NUMBERS, "0927", 1, 2)
        self.assertIn("0275", cands)
        self.assertTrue(all(bc.score("0927", c) == (1, 2) for c in cands))
        self.assertEqual(bc.filter_candidates(cands, "0275", 4, 0), ["0275"])

    def test_verify_reveal_ok(self):
        salt = "2eb6594c74b9b9d71e2d9b5bd14300df"
        commit = bc.commit_hash("0275", salt)
        history = [(1, "0927", 1, 2), (4, "1346", 0, 0)]
        self.assertEqual(bc.verify_reveal(commit, "0275", salt, history), ("OK", []))

    def test_verify_reveal_bad_commit(self):
        salt = "2eb6594c74b9b9d71e2d9b5bd14300df"
        commit = bc.commit_hash("0275", salt)
        self.assertEqual(bc.verify_reveal(commit, "0276", salt, []), ("BAD_COMMIT", []))
        self.assertEqual(bc.verify_reveal(commit, "0275", "0" * 32, []), ("BAD_COMMIT", []))
        # BAD_COMMIT is checked before the history: a lie does not change the verdict
        self.assertEqual(bc.verify_reveal(commit, "0276", salt, [(1, "0276", 0, 0)]), ("BAD_COMMIT", []))

    def test_verify_reveal_no_reveal(self):  # §14.2 step 1: no well-formed REVEAL
        salt = "2eb6594c74b9b9d71e2d9b5bd14300df"
        commit = bc.commit_hash("0275", salt)
        self.assertEqual(bc.verify_reveal(commit, None, None, []), ("NO_REVEAL", []))
        self.assertEqual(bc.verify_reveal(commit, "0275", "nonsense", []), ("NO_REVEAL", []))
        self.assertEqual(bc.verify_reveal(commit, "０２７５", salt, []), ("NO_REVEAL", []))
        self.assertEqual(bc.verify_reveal(None, None, None, [(1, "0123", 0, 0)]), ("NO_REVEAL", []))

    def test_verify_reveal_inconsistent(self):
        salt = "2eb6594c74b9b9d71e2d9b5bd14300df"
        commit = bc.commit_hash("0275", salt)
        history = [(1, "0927", 1, 2), (3, "1346", 0, 1), (5, "5720", 0, 4), (7, "0285", 2, 0)]
        self.assertEqual(bc.verify_reveal(commit, "0275", salt, history), ("INCONSISTENT", [3, 7]))


class HelperTests(unittest.TestCase):
    def test_pong_echoes_nonce(self):
        self.assertEqual(bc.make_pong({"type": "PING", "nonce": "n-42"}), {"type": "PONG", "nonce": "n-42"})

    def test_pong_without_nonce(self):
        self.assertEqual(bc.make_pong({"type": "PING"}), {"type": "PONG"})

    def test_dispatch_calls_handler_and_ignores_unknown(self):
        seen = []
        handlers = {"PING": seen.append}
        self.assertTrue(bc.dispatch(handlers, {"type": "PING"}))
        self.assertFalse(bc.dispatch(handlers, {"type": "SOMETHING_NEW", "x": 1}))
        self.assertEqual(seen, [{"type": "PING"}])


class CliTests(unittest.TestCase):
    def parse(self, *argv):
        return bc.build_parser().parse_args(list(argv))

    def test_server_defaults(self):
        a = self.parse("server")
        self.assertEqual((a.host, a.port, a.players, a.bots), ("0.0.0.0", 5040, 2, 0))
        self.assertEqual((a.guess_timeout, a.feedback_timeout, a.commit_timeout, a.reveal_timeout),
                         (60, 60, 30, 15))
        self.assertEqual((a.max_turns, a.max_skips, a.games, a.log_dir), (120, 3, 1, "logs"))

    def test_server_timeouts_accept_floats(self):
        a = self.parse("server", "--guess-timeout", "1.5", "--feedback-timeout", "2.5",
                       "--commit-timeout", "0.5", "--reveal-timeout", "1.25")
        self.assertEqual((a.guess_timeout, a.feedback_timeout, a.commit_timeout, a.reveal_timeout),
                         (1.5, 2.5, 0.5, 1.25))

    def test_client_and_ai(self):
        c = self.parse("client", "--host", "127.0.0.1", "--name", "玩家")
        self.assertEqual((c.host, c.port, c.name, c.team), ("127.0.0.1", 5040, "玩家", "G00"))
        a = self.parse("ai", "--host", "h", "--port", "6000", "--name", "bot", "--team", "G07", "--quiet")
        self.assertEqual((a.host, a.port, a.name, a.team, a.quiet), ("h", 6000, "bot", "G07", True))
        self.assertFalse(self.parse("ai", "--host", "h", "--name", "n").quiet)

    def test_client_requires_host_and_name(self):
        with self.assertRaises(SystemExit):
            self.parse("client", "--name", "x")
        with self.assertRaises(SystemExit):
            self.parse("ai", "--host", "x")

    def test_help_runs(self):
        expected = {
            (): ["server", "client", "ai"],
            ("server",): ["--host", "--port", "--players", "--bots", "--guess-timeout",
                          "--feedback-timeout", "--commit-timeout", "--reveal-timeout",
                          "--max-turns", "--max-skips", "--games", "--log-dir"],
            ("client",): ["--host", "--port", "--name", "--team"],
            ("ai",): ["--host", "--port", "--name", "--team", "--quiet"],
        }
        for sub, options in expected.items():
            with self.subTest(sub=sub):
                out = subprocess.run([sys.executable, str(ROOT / "bc.py"), *sub, "--help"],
                                     capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(out.returncode, 0, out.stderr)
                for opt in options:
                    self.assertIn(opt, out.stdout)


if __name__ == "__main__":
    unittest.main()
