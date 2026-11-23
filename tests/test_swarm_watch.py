import importlib.util
import json
import os
import pathlib
import shutil
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("swarm_watch", ROOT / "skills" / "swarm" / "scripts" / "swarm-watch.py")
sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)

FIXTURE = ROOT / "tests" / "fixtures" / "worker-log.jsonl"


class FormatTest(unittest.TestCase):
    def test_format_records(self):
        recs = [json.loads(l) for l in FIXTURE.read_text().splitlines()]
        got = [sw.format_record(r) for r in recs]
        self.assertEqual(got, [
            ["IN: You are w1-math."],
            [],
            [],
            [],
            ["-> Bash: python3 -m unittest discover -s demo"],
            ["   <- OK"],
            ["   !! Exit code 1"],
            ["MSG from ab12: PING from w2"],
            ["Tests pass."],
            ["-> SendMessage to main: DONE t1"],
            [],
            [],
        ])

    def test_truncates_long_lines(self):
        rec = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 500}}]}}
        lines = sw.format_record(rec)
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 200)
        self.assertTrue(lines[0].endswith("…"))

    def test_strips_terminal_controls(self):
        rec = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi \x1b]0;pwn\x07\x1b[2J\x9bthere\tok"}]}}
        line = sw.format_record(rec)[0]
        self.assertNotRegex(line, "[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f]")
        self.assertIn("there\tok", line)

    def test_bad_shapes_skipped(self):
        for rec in ({"type": "assistant"},
                    {"type": "assistant", "message": {"content": "str"}},
                    {"type": "user", "message": {"content": [{"type": "tool_result"}]}}):
            self.assertIsInstance(sw.format_record(rec), list)

    def test_message_from_overseer_shows_its_first_line(self):
        rec = {"type": "user", "message": {"content":
               "The coordinator sent a message while you were working:\nFIX t1 (round 1/3)\nmathx.py:3 — bug"}}
        self.assertEqual(sw.format_record(rec),
                         ["IN: The coordinator sent a message while you were working: FIX t1 (round 1/3)"])


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp()) / "with space "
        self.dir.mkdir()
        self.addCleanup(shutil.rmtree, self.dir.parent)
        self.path = self.dir / "a.jsonl"

    def append(self, text):
        with open(self.path, "a") as f:
            f.write(text)

    def test_reader_waits_for_missing_file(self):
        r = sw.LogReader(str(self.path))
        self.assertEqual(r.read_new(), [])
        self.append('{"type": "user"}\n')
        self.assertEqual(r.read_new(), [{"type": "user"}])

    def test_reader_holds_partial_line(self):
        r = sw.LogReader(str(self.path))
        self.append('{"type": "assistant"')
        self.assertEqual(r.read_new(), [])
        self.append(', "x": 1}\n')
        self.assertEqual(r.read_new(), [{"type": "assistant", "x": 1}])

    def test_reader_skips_malformed(self):
        self.append('not json\n{"type":"user"}\n')
        self.assertEqual(sw.LogReader(str(self.path)).read_new(), [{"type": "user"}])

    def test_read_meta_through_symlink(self):
        (self.dir / "agent-x.jsonl").write_text("")
        (self.dir / "agent-x.meta.json").write_text('{"model":"haiku","effort":"low","finished":true}')
        os.symlink(self.dir / "agent-x.jsonl", self.dir / "x.output")
        self.assertEqual(sw.read_meta(str(self.dir / "x.output"))["model"], "haiku")
        (self.dir / "agent-x.meta.json").unlink()
        self.assertEqual(sw.read_meta(str(self.dir / "x.output")), {})


if __name__ == "__main__":
    unittest.main()
