import contextlib
import curses
import importlib.util
import io
import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "swarm" / "scripts" / "swarm-dash.py"
spec = importlib.util.spec_from_file_location("swarm_dash", SCRIPT)
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)

FIXTURE = ROOT / "tests" / "fixtures" / "worker-log.jsonl"
ROSTER_HEAD = "| worker | agent id | model | effort | tasks | output file |\n|---|---|---|---|---|---|\n"


class RunDirTest(unittest.TestCase):
    def setUp(self):
        self.run = pathlib.Path(tempfile.mkdtemp()) / "with space "
        self.run.mkdir()
        self.dir = self.run
        self.addCleanup(shutil.rmtree, self.run.parent)

    def roster(self, *rows):  # rows: (name, model, effort, log)
        (self.run / "roster.md").write_text(ROSTER_HEAD + "".join(
            f"| {n} | id | {m} | {e} | t1 | {l} |\n" for n, m, e, l in rows))

    def status(self, name, text):
        (self.run / "workers").mkdir(exist_ok=True)
        (self.run / "workers" / f"{name}.md").write_text(text)


class FormatTest(unittest.TestCase):
    def test_format_records(self):
        recs = [json.loads(l) for l in FIXTURE.read_text().splitlines()]
        got = [sd.format_record(r) for r in recs]
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
        lines = sd.format_record(rec)
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 200)
        self.assertTrue(lines[0].endswith("…"))

    def test_strips_terminal_controls(self):
        rec = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi \x1b]0;pwn\x07\x1b[2J\x9bthere\tok"}]}}
        line = sd.format_record(rec)[0]
        self.assertNotRegex(line, "[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f]")
        self.assertIn("there\tok", line)

    def test_bad_shapes_skipped(self):
        for rec in ({"type": "assistant"},
                    {"type": "assistant", "message": {"content": "str"}},
                    {"type": "user", "message": {"content": [{"type": "tool_result"}]}}):
            self.assertIsInstance(sd.format_record(rec), list)

    def test_message_from_overseer_shows_its_first_line(self):
        rec = {"type": "user", "message": {"content":
               "The coordinator sent a message while you were working:\nFIX t1 (round 1/3)\nmathx.py:3 — bug"}}
        self.assertEqual(sd.format_record(rec),
                         ["IN: The coordinator sent a message while you were working: FIX t1 (round 1/3)"])


class ReaderTest(RunDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.dir / "a.jsonl"

    def append(self, text):
        with open(self.path, "a") as f:
            f.write(text)

    def test_reader_waits_for_missing_file(self):
        r = sd.LogReader(str(self.path))
        self.assertEqual(r.read_new(), [])
        self.append('{"type": "user"}\n')
        self.assertEqual(r.read_new(), [{"type": "user"}])

    def test_reader_holds_partial_line(self):
        r = sd.LogReader(str(self.path))
        self.append('{"type": "assistant"')
        self.assertEqual(r.read_new(), [])
        self.append(', "x": 1}\n')
        self.assertEqual(r.read_new(), [{"type": "assistant", "x": 1}])

    def test_reader_survives_absurd_nesting(self):
        self.append("[" * 100000 + '\n{"type":"user"}\n')
        self.assertEqual(sd.LogReader(str(self.path)).read_new(), [{"type": "user"}])

    def test_reader_restarts_after_truncation(self):
        self.append('{"type":"user","n":1}\n{"type":"user","n":2}\n')
        r = sd.LogReader(str(self.path))
        self.assertEqual(len(r.read_new()), 2)
        self.path.write_text('{"type":"user","n":3}\n')
        self.assertEqual(r.read_new(), [{"type": "user", "n": 3}])
        self.assertTrue(r.truncated)
        self.assertEqual(r.read_new(), [])
        self.assertFalse(r.truncated)

    def test_reader_restarts_after_replacement_by_larger_file(self):
        self.append('{"type":"user","n":1}\n')
        r = sd.LogReader(str(self.path))
        self.assertEqual(len(r.read_new()), 1)
        new = self.path.with_name("new.jsonl")
        new.write_text('{"type":"user","n":2}\n{"type":"user","n":3}\n')
        os.replace(new, self.path)
        self.assertEqual([x["n"] for x in r.read_new()], [2, 3])
        self.assertTrue(r.truncated)

    def test_reader_restarts_after_in_place_rewrite_past_pos(self):
        self.append('{"type":"user","n":1}\n')
        r = sd.LogReader(str(self.path))
        self.assertEqual(len(r.read_new()), 1)
        with open(self.path, "w") as f:  # same inode, truncated then regrown past the old position
            f.write('{"type":"user","n":2}\n{"type":"user","n":3}\n')
        self.assertEqual([x["n"] for x in r.read_new()], [2, 3])
        self.assertTrue(r.truncated)

    def test_worker_drops_lines_after_truncation(self):
        self.append('{"type":"assistant","message":{"content":[{"type":"text","text":"old"}]}}\n')
        w = sd.Worker("w", log=str(self.path))
        w.poll_log()
        self.path.write_text('{"type":"assistant","message":{"content":[{"type":"text","text":"n"}]}}\n')
        w.poll_log()
        self.assertEqual(w.lines, ["n"])

    def test_reader_skips_malformed(self):
        self.append('not json\n{"type":"user"}\n')
        self.assertEqual(sd.LogReader(str(self.path)).read_new(), [{"type": "user"}])

    def test_reader_skips_fifo_and_device(self):  # a roster could point anywhere: a blocking open or endless read would freeze the window
        fifo = self.dir / "fifo.jsonl"
        os.mkfifo(fifo)
        class Blocked(Exception):  # not an OSError, so read_new cannot swallow it
            pass
        def timeout(*_):
            raise Blocked("LogReader blocked")
        old = signal.signal(signal.SIGALRM, timeout)
        signal.alarm(3)
        try:
            self.assertEqual(sd.LogReader(str(fifo)).read_new(), [])
            self.assertEqual(sd.LogReader("/dev/zero").read_new(), [])
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def test_reader_follows_symlink_to_log(self):  # Claude Code output files are symlinks to the real log
        self.append('{"type":"user"}\n')
        os.symlink(self.path, self.dir / "x.output")
        self.assertEqual(sd.LogReader(str(self.dir / "x.output")).read_new(), [{"type": "user"}])

    def test_run_over(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.assertFalse(sd.run_over(None, None))
        self.assertFalse(sd.run_over(str(self.dir), None))
        self.assertFalse(sd.run_over(str(self.dir), os.getpid()))
        self.assertTrue(sd.run_over(str(self.dir), dead.pid))
        (self.dir / "report.md").write_text("done")
        self.assertTrue(sd.run_over(str(self.dir), os.getpid()))
        self.assertTrue(sd.run_over(str(self.dir), None))

    def test_status_clipped_to_200(self):
        self.status("w1", "status: " + "x" * 500 + "\n")
        st = sd.worker_status(str(self.run / "workers" / "w1.md"))
        self.assertEqual(st, "x" * 199 + "…")
        self.assertEqual(len(st), 200)

    def test_status_strips_controls_and_prefix(self):
        self.status("w1", "status: ok \x1b]0;pwn\x07\n## files changed\n")
        self.assertEqual(sd.worker_status(str(self.run / "workers" / "w1.md")), "ok ]0;pwn")

    def test_status_missing_or_empty(self):
        self.status("w2", "")
        self.assertEqual(sd.worker_status(str(self.run / "workers" / "w2.md")), "(no status yet)")
        self.assertEqual(sd.worker_status(str(self.run / "workers" / "none.md")), "(no status yet)")

    def test_symlink_status_not_read(self):
        (self.run / "workers").mkdir()
        (self.run / "secret.txt").write_text("TOPSECRET\n")
        os.symlink(self.run / "secret.txt", self.run / "workers" / "w1.md")
        st = sd.worker_status(str(self.run / "workers" / "w1.md"))
        self.assertEqual(st, "(not a regular file)")
        self.assertNotIn("TOPSECRET", st)

    def test_fifo_status_does_not_block(self):
        (self.run / "workers").mkdir()
        os.mkfifo(self.run / "workers" / "w1.md")
        self.assertEqual(sd.worker_status(str(self.run / "workers" / "w1.md")), "(not a regular file)")

    def test_activity_flags(self):
        log = self.run / "agent-a.jsonl"
        log.write_text("{}\n")
        now = log.stat().st_mtime
        self.assertEqual(sd.activity(str(log), now + 30), "")
        self.assertEqual(sd.activity(str(log), now + 330), " (quiet 5m)")
        (self.run / "agent-a.meta.json").write_text('{"finished": true}')
        self.assertEqual(sd.activity(str(log), now + 330), " (finished)")
        self.assertEqual(sd.activity("", now), "")
        self.assertEqual(sd.activity(str(self.run / "missing.jsonl"), now), "")

    def test_activity_flags_quiet_worker_from_roster(self):
        log = self.run / "agent-a.jsonl"
        log.write_text("{}\n")
        os.utime(log, (0, 0))
        self.roster(("w1-math", "haiku", "low", log))
        w = sd.read_roster(str(self.run))[0]
        self.assertRegex(sd.activity(w["log"]), r"^ \(quiet \d+m\)$")

    def test_read_tasks(self):  # tasks.md as swarm-run.py init writes it, with an escaped pipe
        (self.run / "tasks.md").write_text("| id | title | owner | status | files | check | depends | fixes |\n"
            "|---|---|---|---|---|---|---|---|\n| t1 | a \\| b | w1-api | working | x.py | true | none | 1 |\n")
        self.assertEqual(sd.read_tasks(str(self.run)),
                         [{"id": "t1", "title": "a | b", "owner": "w1-api", "status": "working", "fixes": "1"}])

    def test_read_tasks_missing_file_and_columns(self):
        self.assertEqual(sd.read_tasks(str(self.run)), [])
        (self.run / "tasks.md").write_text("| id | title |\n|---|---|\n| t1 | a |\n")
        self.assertEqual(sd.read_tasks(str(self.run)),
                         [{"id": "t1", "title": "a", "owner": "", "status": "", "fixes": ""}])

    def test_read_roster_order_and_fields(self):  # second row before first alphabetically
        self.roster(("w2-ui", "sonnet", "medium", "/tmp/x.log"), ("w1-api", "haiku", "low", "/tmp/y.log"))
        got = sd.read_roster(str(self.run))
        self.assertEqual([r["name"] for r in got], ["w2-ui", "w1-api"])
        self.assertEqual(got[0], {"name": "w2-ui", "model": "sonnet", "effort": "medium", "log": "/tmp/x.log"})

    def test_read_roster_missing_or_bad_header(self):
        self.assertEqual(sd.read_roster(str(self.run)), [])
        (self.run / "roster.md").write_text("| worker | model |\n|---|---|\n| w1 | haiku |\n")
        self.assertEqual(sd.read_roster(str(self.run)), [])


def tasks(n, owner="w1-api", title="do a thing"):
    return [{"id": f"t{i}", "title": title, "owner": owner, "status": "working", "fixes": "0"} for i in range(1, n + 1)]


class LayoutTest(unittest.TestCase):
    def test_grid_shape(self):
        self.assertEqual(sd.grid_shape(1, 40, 160), (1, 1))
        self.assertEqual(sd.grid_shape(4, 40, 160), (2, 2))
        self.assertEqual(sd.grid_shape(20, 48, 240), (5, 4))
        self.assertIsNone(sd.grid_shape(20, 20, 80))
        self.assertIsNone(sd.grid_shape(2, 3, 200))  # boxes under 4 lines
        self.assertIsNone(sd.grid_shape(0, 40, 160))

    def test_strip_one_column(self):
        got = sd.strip_lines(tasks(3), 80, 5)
        self.assertEqual(len(got), 3)
        for line in got:
            self.assertTrue(line.startswith("t") and " w1-api working f0  do a thing" in line)
        self.assertEqual(got[0], "t1 w1-api working f0  do a thing")

    def test_strip_columns(self):
        got = sd.strip_lines(tasks(10), 120, 4)
        self.assertLessEqual(len(got), 4)
        self.assertTrue(all(len(l) <= 120 for l in got))
        for i in range(1, 11):
            self.assertRegex("\n".join(got), rf"\bt{i} w1-api")

    def test_strip_overflow(self):
        got = sd.strip_lines(tasks(30), 60, 3)
        self.assertEqual(len(got), 3)
        shown = sum(1 for i in range(1, 31) if f"t{i} w1-api" in "\n".join(got[:-1]))
        self.assertEqual(got[-1], f"… {30 - shown} more")
        self.assertTrue(0 < shown < 30)
        self.assertTrue(all(len(l) <= 60 for l in got))

    def test_strip_edges(self):
        self.assertEqual(sd.strip_lines([], 80, 3), [])
        self.assertEqual(sd.strip_lines(tasks(3), 80, 0), [])
        self.assertEqual(sd.strip_lines(tasks(5), 80, 1), ["… 5 more"])
        self.assertTrue(all(len(l) <= 10 for l in sd.strip_lines(tasks(5), 10, 2)))

    def test_strip_strips_controls(self):
        t = tasks(1)
        t[0]["title"] = "a\x1b[31mb"
        t[0]["owner"] = "w\x9b1"
        out = "\n".join(sd.strip_lines(t, 80, 3))
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x9b", out)
        self.assertIn("a[31mb", out)

    def test_move(self):
        names = ["a", "b", "c", "d", "e"]
        self.assertEqual(sd.move(names, "a", "right", 2), "b")
        self.assertEqual(sd.move(names, "b", "right", 2), "b")  # row end: no wrap
        self.assertEqual(sd.move(names, "a", "left", 2), "a")
        self.assertEqual(sd.move(names, "a", "down", 2), "c")
        self.assertEqual(sd.move(names, "c", "up", 2), "a")
        self.assertEqual(sd.move(names, "a", "up", 2), "a")
        self.assertEqual(sd.move(names, "e", "down", 2), "e")
        self.assertEqual(sd.move(names, "e", "right", 2), "e")
        self.assertEqual(sd.move(names, "e", "tab", 2), "a")
        self.assertEqual(sd.move(names, "a", "4", 2), "d")
        self.assertEqual(sd.move(names, "a", "9", 2), "a")
        self.assertEqual(sd.move(names, None, "tab", 2), "a")
        self.assertIsNone(sd.move([], None, "tab", 2))

    def test_list_window_keeps_selected_visible(self):
        self.assertEqual(sd.list_window(20, 0, 7), (0, 7))
        self.assertEqual(sd.list_window(20, 19, 7), (13, 20))
        self.assertEqual(sd.list_window(3, 2, 7), (0, 3))
        for i in range(20):
            s, e = sd.list_window(20, i, 7)
            self.assertTrue(s <= i < e and e - s == 7)

    def test_scroll_and_visible(self):
        lines = [str(i) for i in range(100)]
        self.assertEqual(sd.visible(lines, 10, None), lines[-10:])
        top = sd.scroll(None, "up", 100, 10)
        self.assertEqual(top, 89)
        self.assertEqual(sd.scroll(top, "home", 100, 10), 0)
        self.assertEqual(sd.scroll(50, "pgdn", 100, 10), 60)
        self.assertEqual(sd.scroll(50, "pgup", 100, 10), 40)
        self.assertEqual(sd.scroll(3, "pgup", 100, 10), 0)
        self.assertIsNone(sd.scroll(85, "pgdn", 100, 10))
        self.assertIsNone(sd.scroll(0, "end", 100, 10))
        self.assertIsNone(sd.scroll(89, "down", 100, 10))
        self.assertEqual(sd.visible(lines, 10, 0), lines[:10])


class DashTest(RunDirTest):
    def test_worker_keeps_whole_log(self):
        log = self.run / "a.jsonl"
        with open(log, "w") as f:
            for i in range(20005):
                f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"line {i}"}]}}) + "\n")
        w = sd.Worker("w1", "haiku", "low", str(log))
        self.assertTrue(w.poll_log())
        self.assertEqual(len(w.lines), 20005)
        self.assertEqual((w.lines[0], w.lines[-1]), ("line 0", "line 20004"))
        self.assertFalse(w.poll_log())

    def test_worker_without_log(self):
        self.assertFalse(sd.Worker("w1").poll_log())

    def test_late_roster_adds_worker_and_keeps_selection(self):
        self.status("w2-ui", "status: ui\n")
        d = sd.Dash(str(self.run), None)
        d.refresh_meta(0)
        self.assertEqual(d.names(), ["w2-ui"])
        self.assertEqual(d.selected, "w2-ui")
        self.roster(("w1-api", "haiku", "low", ""), ("w2-ui", "sonnet", "medium", ""))
        d.refresh_meta(1)
        self.assertEqual(d.names(), ["w1-api", "w2-ui"])
        self.assertEqual(d.selected, "w2-ui")
        self.assertEqual(d.workers["w2-ui"].model, "sonnet")
        self.assertEqual(d.workers["w2-ui"].status, "ui")
        self.assertEqual(d.workers["w1-api"].status, "(no status yet)")

    def test_names_roster_order_then_status_only_sorted(self):
        self.roster(("w2-ui", "m", "e", ""), ("w1-api", "m", "e", ""))
        self.status("zz", "x")
        self.status("aa", "x")
        d = sd.Dash(str(self.run), None)
        d.refresh_meta(0)
        self.assertEqual(d.names(), ["w2-ui", "w1-api", "aa", "zz"])
        self.assertEqual(d.selected, "w2-ui")

    def test_roster_name_cannot_escape_workers_dir(self):
        (self.run / "secret.md").write_text("TOPSECRET\n")
        self.roster(("../secret", "m", "e", ""))
        (self.run / "workers").mkdir()
        d = sd.Dash(str(self.run), None)
        d.refresh_meta(0)
        self.assertEqual(d.workers["../secret"].status, "(no status yet)")

    def test_poll_logs_reads_fixture_and_keeps_lines_over_refresh(self):
        log = self.run / "agent-a.jsonl"
        shutil.copyfile(FIXTURE, log)
        self.roster(("w1-api", "haiku", "low", log))
        d = sd.Dash(str(self.run), None)
        d.refresh_meta(0)
        d.poll_logs()
        want = [l for line in FIXTURE.read_text().splitlines() for l in sd.format_record(json.loads(line))]
        self.assertEqual(list(d.workers["w1-api"].lines), want)
        d.refresh_meta(1)
        d.poll_logs()
        self.assertEqual(list(d.workers["w1-api"].lines), want)

    def test_tick_closes_after_delay(self):
        (self.run / "report.md").write_text("done")
        d = sd.Dash(str(self.run), None)
        self.assertTrue(d.tick(100))
        self.assertEqual(d.closing_at, 100 + sd.CLOSE_DELAY)
        self.assertTrue(d.tick(104.9))
        self.assertFalse(d.tick(105))

    def test_tick_runs_while_not_over(self):
        d = sd.Dash(str(self.run), os.getpid())
        self.assertTrue(d.tick(1000))
        self.assertIsNone(d.closing_at)


class UiTest(RunDirTest):
    def test_main_usage_errors(self):
        for argv in ([], ["a", "b", "c"], ["run", "notapid"]):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(sd.main(argv), 2)
            self.assertIn("usage: swarm-dash.py", err.getvalue())

    def test_main_missing_run_folder(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, unittest.mock.patch("sys.stdout.isatty", create=True, return_value=True):
            self.assertEqual(sd.main([str(self.dir / "nope")]), 2)
        self.assertIn("not a folder", err.getvalue())

    def test_run_over_ignores_huge_pid(self):
        self.assertFalse(sd.run_over(None, 2 ** 80))

    def test_main_needs_terminal(self):
        r = subprocess.run([sys.executable, str(SCRIPT), str(self.run)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("needs a terminal", r.stderr)

    def test_key_name(self):
        self.assertEqual(sd.key_name(curses.KEY_LEFT), "left")
        self.assertEqual(sd.key_name(9), "tab")
        self.assertEqual(sd.key_name(10), "enter")
        self.assertEqual(sd.key_name(27), "esc")
        self.assertEqual(sd.key_name(ord("3")), "3")
        self.assertEqual(sd.key_name(ord("q")), "q")
        self.assertEqual(sd.key_name(curses.KEY_NPAGE), "pgdn")
        self.assertEqual(sd.key_name(curses.KEY_RESIZE), "resize")
        self.assertIsNone(sd.key_name(ord("x")))
        self.assertIsNone(sd.key_name(-1))

    def test_handle_key_zoom_cycle(self):
        log = self.run / "agent-a.jsonl"
        shutil.copyfile(FIXTURE, log)
        self.roster(("w1-api", "haiku", "low", log), ("w2-ui", "haiku", "low", ""))
        d = sd.Dash(str(self.run), None)
        d.refresh_meta(0)
        d.poll_logs()
        self.assertTrue(sd.handle_key(d, "right", 2, 4))
        self.assertEqual(d.selected, "w2-ui")
        self.assertTrue(sd.handle_key(d, "left", 2, 4))
        self.assertTrue(sd.handle_key(d, "enter", 2, 4))
        self.assertEqual((d.zoom, d.top), ("w1-api", None))
        self.assertTrue(sd.handle_key(d, "up", 2, 4))  # 7 lines, 3 visible
        self.assertIsNotNone(d.top)
        self.assertTrue(sd.handle_key(d, "end", 2, 4))
        self.assertIsNone(d.top)
        self.assertTrue(sd.handle_key(d, "esc", 2, 4))
        self.assertIsNone(d.zoom)
        sd.handle_key(d, "enter", 2, 4)
        self.assertTrue(sd.handle_key(d, "q", 2, 4))  # q leaves zoom first
        self.assertIsNone(d.zoom)
        self.assertFalse(sd.handle_key(d, "q", 2, 4))


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TmuxTest(RunDirTest):
    def setUp(self):
        super().setUp()
        (self.run / "tasks.md").write_text("| id | title | owner | status | fixes |\n|---|---|---|---|---|\n"
                                           "| t1 | build api | w1-api | working | 0 |\n| t2 | build ui | w2-ui | todo | 0 |\n")
        logs = []
        for n in ("a", "b"):
            logs.append(self.run / f"agent-{n}.jsonl")
            shutil.copyfile(FIXTURE, logs[-1])
        self.roster(("w1-api", "haiku", "low", logs[0]), ("w2-ui", "sonnet", "medium", logs[1]))
        self.status("w1-api", "status: writing api\n")
        self.sock = f"swarmdash-{uuid.uuid4().hex[:8]}"
        self.addCleanup(subprocess.run, ["tmux", "-L", self.sock, "kill-server"], capture_output=True)
        self.tmux("new-session", "-d", "-x", "160", "-y", "45", "-s", "d",
                  shlex.join([sys.executable, str(SCRIPT), str(self.run)]))

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.sock, *args], capture_output=True, text=True)

    def cap(self):
        return self.tmux("capture-pane", "-p", "-t", "d").stdout

    def wait(self, text, secs=5):
        end = time.time() + secs
        while time.time() < end:
            if text in self.cap():
                return
            time.sleep(0.1)
        self.fail(f"{text!r} not on screen:\n{self.cap()}")

    def gone(self, secs):
        end = time.time() + secs
        while time.time() < end:
            if self.tmux("has-session", "-t", "d").returncode != 0:
                return True
            time.sleep(0.1)
        return False

    def test_dashboard_end_to_end(self):
        self.wait("writing api")
        screen = self.cap()
        self.assertIn("t1 w1-api", screen)
        self.assertIn("w1-api | haiku low", screen)
        self.assertIn("-> Bash: python3 -m unittest discover -s demo", screen)
        self.tmux("send-keys", "-t", "d", "Enter")
        self.wait("w1-api | haiku low | writing api")
        self.tmux("send-keys", "-t", "d", "Escape")
        self.wait("t1 w1-api")
        self.tmux("resize-window", "-t", "d", "-x", "30", "-y", "8")
        self.wait("enlarge the window")
        self.tmux("resize-window", "-t", "d", "-x", "160", "-y", "45")
        self.wait("w1-api | haiku low")
        (self.run / "report.md").write_text("done")
        self.wait("closing in")
        self.assertTrue(self.gone(8), self.cap())

    def test_ctrl_c_exits(self):
        self.wait("w1-api")
        self.tmux("send-keys", "-t", "d", "C-c")
        self.assertTrue(self.gone(3), self.cap())


if __name__ == "__main__":
    unittest.main()
