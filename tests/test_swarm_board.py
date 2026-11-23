import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("swarm_board", ROOT / "skills" / "swarm" / "scripts" / "swarm-board.py")
sb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sb)


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.run = pathlib.Path(tempfile.mkdtemp()) / "with space "
        self.run.mkdir()
        self.addCleanup(shutil.rmtree, self.run.parent)

    def test_render_full(self):
        tasks = "| id | title | owner | status |\n| t1 | add | w1-math | working |\n"
        (self.run / "tasks.md").write_text(tasks)
        (self.run / "workers").mkdir()
        (self.run / "workers" / "w1-math.md").write_text("status: working on t1\n## files changed\n")
        (self.run / "workers" / "w2-str.md").write_text("")
        out = sb.render(str(self.run))
        self.assertIn(tasks, out)
        self.assertIn("w1-math: working on t1", out)
        self.assertIn("w2-str: (no status yet)", out)

    def test_render_waiting(self):
        self.assertIn("waiting for tasks.md", sb.render(str(self.run)))

    def test_render_no_workers(self):
        self.assertIn("(no workers yet)", sb.render(str(self.run)))


    def test_render_strips_terminal_controls(self):
        (self.run / "tasks.md").write_text("| t1 | \x1b[2J |\n")
        (self.run / "workers").mkdir()
        (self.run / "workers" / "w1.md").write_text("status: ok \x1b]0;pwn\x07\n")
        out = sb.render(str(self.run))
        self.assertNotRegex(out, "[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f]")
        self.assertIn("w1: ok", out)
    def test_status_clipped_to_200(self):
        (self.run / "workers").mkdir()
        (self.run / "workers" / "w1.md").write_text("status: " + "x" * 500 + "\n")
        st = sb.worker_status(str(self.run / "workers" / "w1.md"))
        self.assertEqual(st, "x" * 199 + "…")
        self.assertEqual(len(st), 200)

    def test_symlink_status_not_read(self):
        (self.run / "workers").mkdir()
        (self.run / "secret.txt").write_text("TOPSECRET\n")
        os.symlink(self.run / "secret.txt", self.run / "workers" / "w1.md")
        out = sb.render(str(self.run))
        self.assertIn("w1: (not a regular file)", out)
        self.assertNotIn("TOPSECRET", out)

    def test_fifo_status_does_not_block(self):
        (self.run / "workers").mkdir()
        os.mkfifo(self.run / "workers" / "w1.md")
        self.assertIn("w1: (not a regular file)", sb.render(str(self.run)))


    def test_activity_flags(self):
        log = self.run / "agent-a.jsonl"
        log.write_text("{}\n")
        now = log.stat().st_mtime
        self.assertEqual(sb.activity(str(log), now + 30), "")
        self.assertEqual(sb.activity(str(log), now + 330), " (quiet 5m)")
        (self.run / "agent-a.meta.json").write_text('{"finished": true}')
        self.assertEqual(sb.activity(str(log), now + 330), " (finished)")
        self.assertEqual(sb.activity("", now), "")
        self.assertEqual(sb.activity(str(self.run / "missing.jsonl"), now), "")

    def test_render_flags_quiet_worker_from_roster(self):
        log = self.run / "agent-a.jsonl"
        log.write_text("{}\n")
        os.utime(log, (0, 0))
        (self.run / "roster.md").write_text(
            "| worker | agent id | model | effort | tasks | output file |\n|---|---|---|---|---|---|\n"
            f"| w1-math | a | haiku | low | t1 | {log} |\n")
        (self.run / "workers").mkdir()
        (self.run / "workers" / "w1-math.md").write_text("status: testing\n")
        (self.run / "workers" / "w2-str.md").write_text("status: editing\n")
        out = sb.render(str(self.run))
        self.assertRegex(out, r"w1-math: testing \(quiet \d+m\)")
        self.assertIn("w2-str: editing\n", out + "\n")

    def test_run_over(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.assertFalse(sb.run_over(str(self.run), None))
        self.assertFalse(sb.run_over(str(self.run), os.getpid()))
        self.assertTrue(sb.run_over(str(self.run), dead.pid))
        (self.run / "report.md").write_text("done")
        self.assertTrue(sb.run_over(str(self.run), None))

    def test_main_exits_when_run_over(self):
        (self.run / "report.md").write_text("done")
        out = io.StringIO()
        with mock.patch.object(sb, "CLOSE_DELAY", 0), contextlib.redirect_stdout(out):
            self.assertEqual(sb.main([str(self.run), str(os.getpid())]), 0)
        self.assertIn("-- run over; closing in 0 s --", out.getvalue())

    def test_main_usage_errors(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sb.main([str(self.run), "notapid"]), 2)
            self.assertEqual(sb.main([]), 2)

if __name__ == "__main__":
    unittest.main()
