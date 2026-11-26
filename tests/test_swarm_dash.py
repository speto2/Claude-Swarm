import contextlib
import curses
import importlib.util
import inspect
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
import unicodedata
import uuid
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "swarm" / "scripts" / "swarm-dash.py"
spec = importlib.util.spec_from_file_location("swarm_dash", SCRIPT)
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)
run_spec = importlib.util.spec_from_file_location("swarm_run", ROOT / "skills" / "swarm" / "scripts" / "swarm-run.py")
sr = importlib.util.module_from_spec(run_spec)
run_spec.loader.exec_module(sr)

FIXTURE = ROOT / "tests" / "fixtures" / "worker-log.jsonl"


@contextlib.contextmanager
def alarm(secs, what):
    """Fail instead of hanging: the exception is not an OSError, so the code under test cannot swallow it."""
    class Blocked(Exception):
        pass
    def fire(*_):
        raise Blocked(f"{what} blocked")
    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(secs)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def asst(*items):
    return {"type": "assistant", "message": {"content": list(items)}}


def user(content):
    return {"type": "user", "message": {"content": content}}


def said(text):
    return json.dumps(asst({"type": "text", "text": text})) + "\n"


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

    def shown(self, name="w1"):
        return sd.worker_status(str(self.run / "workers" / f"{name}.md"))


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
        rec = asst({"type": "tool_use", "name": "Bash", "input": {"command": "x" * 500}})
        lines = sd.format_record(rec)
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 200)
        self.assertTrue(lines[0].endswith("…"))

    def test_strips_terminal_controls(self):
        rec = asst({"type": "text", "text": "hi \x1b]0;pwn\x07\x1b[2J\x9bthere\tok"})
        line = sd.format_record(rec)[0]
        self.assertNotRegex(line, "[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f]")
        self.assertIn("there\tok", line)

    def test_bad_shapes_skipped(self):
        for rec in ({"type": "assistant"},
                    {"type": "assistant", "message": {"content": "str"}}, user([{"type": "tool_result"}])):
            self.assertIsInstance(sd.format_record(rec), list)

    def test_non_dict_items_do_not_discard_the_record(self):
        rec = asst("junk", None, {"type": "text", "text": "kept"}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}})
        self.assertEqual(sd.format_record(rec), ["kept", "-> Bash: ls"])
        rec = user([3, {"type": "tool_result", "content": "ok"}, {"type": "text", "text": "hello"}])
        self.assertEqual(sd.format_record(rec), ["   <- ok", "IN: hello"])
        self.assertEqual(sd.format_record(user(5)), [])

    def test_user_string_agent_message_and_markup(self):
        self.assertEqual(sd.format_record(user('<agent-message from="ab12">\nPING\n</agent-message>')), ["MSG from ab12: PING"])
        self.assertEqual(sd.format_record(user("<system>x</system>")), [])
        self.assertEqual(sd.format_record(user("plain")), ["IN: plain"])

    def test_web_tools_and_send_message_without_recipient(self):
        def use(name, inp):
            return sd.format_record(asst({"type": "tool_use", "name": name, "input": inp}))
        self.assertEqual(use("WebFetch", {"url": "http://x.test", "prompt": "p"}), ["-> WebFetch: http://x.test"])
        self.assertEqual(use("WebSearch", {"query": "cats"}), ["-> WebSearch: cats"])
        self.assertEqual(use("SendMessage", {"message": "hi"}), ["-> SendMessage to ?: hi"])

    def test_attachment_records(self):
        def att(a):
            return sd.format_record({"type": "attachment", "attachment": a})
        self.assertEqual(att({"type": "queued_command", "prompt": "do it\nmore"}), ["IN: do it"])
        self.assertEqual(att({"type": "queued_command", "prompt": '<agent-message from="ab">\nhi\n</agent-message>'}), ["MSG from ab: hi"])
        for other in ({"type": "hook_success", "prompt": "x"}, {"type": "todo_reminder"}, {}, None):
            self.assertEqual(att(other), [], other)
        self.assertEqual(sd.format_record({"type": "attachment"}), [])

    def test_tool_result_with_list_content(self):
        def res(body, **kw):
            return sd.format_record(user([{"type": "tool_result", "content": body, **kw}]))
        self.assertEqual(res([{"type": "text", "text": "\nfirst\nsecond"}, "junk", {"type": "image"}]), ["   <- first"])
        self.assertEqual(res([{"type": "text", "text": "bad"}], is_error=True), ["   !! bad"])
        self.assertEqual(res([]), ["   <- "])
        self.assertEqual(res([{"type": "image"}]), ["   <- "])
        self.assertEqual(res(None), ["   <- "])

    def test_clip_strips_bidi_and_line_separators(self):
        self.assertEqual(sd.clip("a\u202eb\u2066c\u2069d\u2028e\u2029f"), "abcdef")

    def test_message_from_overseer_shows_its_first_line(self):
        rec = user("The coordinator sent a message while you were working:\nFIX t1 (round 1/3)\nmathx.py:3 — bug")
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
        self.append(said("old"))
        w = sd.Worker("w", log=str(self.path))
        w.poll_log()
        self.path.write_text(said("n"))
        w.poll_log()
        self.assertEqual(w.lines, ["n"])

    def test_reader_skips_malformed(self):
        self.append('not json\n{"type":"user"}\n')
        self.assertEqual(sd.LogReader(str(self.path)).read_new(), [{"type": "user"}])

    def test_reader_skips_fifo_and_device(self):  # a roster could point anywhere: a blocking open or endless read would freeze the window
        fifo = self.dir / "fifo.jsonl"
        os.mkfifo(fifo)
        with alarm(3, "LogReader"):
            self.assertEqual(sd.LogReader(str(fifo)).read_new(), [])
            self.assertEqual(sd.LogReader("/dev/zero").read_new(), [])

    def test_reader_follows_symlink_to_log(self):  # Claude Code output files are symlinks to the real log
        self.append('{"type":"user"}\n')
        os.symlink(self.path, self.dir / "x.output")
        self.assertEqual(sd.LogReader(str(self.dir / "x.output")).read_new(), [{"type": "user"}])

    def test_reader_reads_at_most_one_mib_per_call(self):
        self.append(('{"type":"user"}\n' * (3 << 16)))  # 3 MiB
        r = sd.LogReader(str(self.path))
        self.assertGreater(len(r.read_new()), 0)
        self.assertEqual(r.pos, sd.MAX_READ)

    def test_reader_drops_endless_line(self):
        self.append("x" * (5 * sd.MAX_READ // 2) + '\n{"type":"user"}\n')
        r, got = sd.LogReader(str(self.path)), []
        for _ in range(5):
            got += r.read_new()
            self.assertLessEqual(len(r.buf), sd.MAX_READ)
        self.assertEqual(got, [{"type": "user"}])

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
        self.assertFalse(sd.run_over(None, 2 ** 80))  # pid too large for the OS

    def test_status_clipped_to_200(self):
        self.status("w1", "status: " + "x" * 500 + "\n")
        self.assertEqual(self.shown(), "x" * 199 + "…")

    def test_status_reads_only_the_start_of_a_huge_file(self):
        self.status("w1", "x" * (1 << 20))
        self.assertEqual(self.shown(), "x" * 199 + "…")
        self.assertEqual(len(sd.read_small(str(self.run / "workers" / "w1.md"), 8192)[0]), 8192)  # bounded read

    def test_status_strips_controls_and_prefix(self):
        self.status("w1", "status: ok \x1b]0;pwn\x07\n## files changed\n")
        self.assertEqual(self.shown(), "ok ]0;pwn")

    def test_status_missing_or_empty(self):
        self.status("w2", "")
        self.assertEqual(self.shown("w2"), "(no status yet)")
        self.assertEqual(self.shown("none"), "(no status yet)")

    def test_symlink_status_not_read(self):
        (self.run / "workers").mkdir()
        (self.run / "secret.txt").write_text("TOPSECRET\n")
        os.symlink(self.run / "secret.txt", self.run / "workers" / "w1.md")
        self.assertEqual(self.shown(), "(not a regular file)")

    def test_fifo_status_does_not_block(self):
        (self.run / "workers").mkdir()
        os.mkfifo(self.run / "workers" / "w1.md")
        with alarm(3, "worker_status"):
            self.assertEqual(self.shown(), "(not a regular file)")

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

    def test_finished_flag_ignored_when_log_is_newer(self):  # worker resumed after it finished
        log, meta = self.run / "agent-a.jsonl", self.run / "agent-a.meta.json"
        meta.write_text('{"finished": true}')
        log.write_text("{}\n")
        os.utime(meta, (1000, 1000))
        os.utime(log, (2000, 2000))
        self.assertEqual(sd.activity(str(log), 2001), "")
        self.assertEqual(sd.activity(str(log), 2005), " (finished)")  # quiet for 5 s: it really stopped
        os.utime(meta, (2000, 2000))
        self.assertEqual(sd.activity(str(log), 2001), " (finished)")

    def test_symlinked_meta_not_read(self):
        log = self.run / "agent-a.jsonl"
        log.write_text("{}\n")
        (self.run / "secret.json").write_text('{"finished": true}')
        os.symlink(self.run / "secret.json", self.run / "agent-a.meta.json")
        self.assertEqual(sd.activity(str(log), log.stat().st_mtime + 330), " (quiet 5m)")

    def test_activity_ignores_malformed_or_non_dict_meta(self):
        log, meta = self.run / "agent-a.jsonl", self.run / "agent-a.meta.json"
        log.write_text("{}\n")
        os.utime(log, (1000, 1000))
        for raw in (b"{not json", b"", b"\xff\xfe", b"[]", b'["finished"]', b"null", b"5", b'"finished"', b'{"finished": []}'):
            meta.write_bytes(raw)
            os.utime(meta, (1000, 1000))
            self.assertEqual(sd.activity(str(log), 1030), "", raw)
            self.assertEqual(sd.activity(str(log), 1330), " (quiet 5m)", raw)

    def test_activity_quiet_boundary(self):
        log = self.run / "agent-a.jsonl"
        log.write_text("{}\n")
        os.utime(log, (1000, 1000))
        self.assertEqual(sd.activity(str(log), 1000 + sd.STALE_AFTER - 1), "")
        self.assertEqual(sd.activity(str(log), 1000 + sd.STALE_AFTER), " (quiet 2m)")
        self.assertEqual(sd.activity(str(log), 1000 + sd.STALE_AFTER - 0.5), "")
        with mock.patch.object(sd.time, "time", return_value=1000 + sd.STALE_AFTER):  # now=None uses the wall clock
            self.assertEqual(sd.activity(str(log)), " (quiet 2m)")

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

    def test_tables_refuse_symlink_and_fifo(self):
        (self.run / "secret.md").write_text("| id | title |\n|---|---|\n| t1 | x |\n")
        os.symlink(self.run / "secret.md", self.run / "tasks.md")
        os.mkfifo(self.run / "roster.md")
        with alarm(3, "tables"):
            self.assertEqual(sd.read_tasks(str(self.run)), [])
            self.assertEqual(sd.read_roster(str(self.run)), [])

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

    def test_read_roster_skips_empty_names_and_strips_backticks(self):
        self.roster(("", "m", "e", "/tmp/a"), ("  ", "m", "e", "/tmp/b"), ("w1", "m", "e", " `/tmp/c d.log` "))
        self.assertEqual(sd.read_roster(str(self.run)), [{"name": "w1", "model": "m", "effort": "e", "log": "/tmp/c d.log"}])

    def test_parse_table_edges_and_first_table_only(self):
        header, rows = sd.parse_table("| a | b |\n|---|---|\n| 1 | 2 ||\n|| 3 | 4 |\n| 5 | x\\| |\n\ntext\n| c |\n|---|\n| 9 |\n")
        self.assertEqual(header, ["a", "b"])
        self.assertEqual(rows, [["1", "2", ""], ["", "3", "4"], ["5", "x|"]])

    def test_parse_table_same_as_swarm_run(self):
        self.assertEqual(inspect.getsource(sd.parse_table), inspect.getsource(sr.parse_table))

    def test_read_roster_duplicate_names_keeps_every_row(self):  # pins current behaviour
        self.roster(("w1", "haiku", "low", "/tmp/a"), ("w1", "opus", "high", "/tmp/b"))
        got = sd.read_roster(str(self.run))
        self.assertEqual([(r["name"], r["model"], r["log"]) for r in got], [("w1", "haiku", "/tmp/a"), ("w1", "opus", "/tmp/b")])

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

    def test_strip_pads_and_cuts_by_display_width(self):
        cells = lambda t: sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in t)
        self.assertLessEqual(cells(sd.strip_lines(tasks(1, title="日本語" * 20), 20, 1)[0]), 20)
        rows = sd.strip_lines(tasks(40, title="日本語" * 20), 100, 5)
        self.assertTrue(rows)
        for row in rows:
            self.assertLessEqual(cells(row), 100)

    def test_layout_without_tasks_has_no_separator(self):
        d = sd.Dash("x", None)
        d.workers = {"w1": sd.Worker("w1")}
        d._order = ["w1"]
        strip, top, body, _ = sd.layout(d, 30, 80)
        self.assertEqual((strip, top, body), ([], 0, 30))
        d.closing_at = 5
        self.assertEqual(sd.layout(d, 30, 80)[1], 2)

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

    def test_scroll_when_everything_fits(self):  # total <= height: nothing to scroll, always follow
        for total in (0, 1, 5, 10):
            for key in ("up", "down", "pgup", "pgdn", "home", "end", "x"):
                for top in (None, 0, 3):
                    self.assertIsNone(sd.scroll(top, key, total, 10), (total, key, top))
        self.assertEqual(sd.visible(["a", "b"], 10, None), ["a", "b"])
        self.assertEqual(sd.visible(["a", "b"], 10, 0), ["a", "b"])
        self.assertEqual(sd.visible([], 10, None), [])
        self.assertEqual(sd.visible(["a"], 0, None), [])


class DashTest(RunDirTest):
    def write_log(self, n):
        log = self.run / "a.jsonl"
        with open(log, "w") as f:
            for i in range(n):
                f.write(said(f"line {i}"))
        return log

    def test_worker_keeps_whole_log(self):
        w = sd.Worker("w1", "haiku", "low", str(self.write_log(20005)))
        while w.poll_log():  # each poll reads at most 1 MiB
            pass
        self.assertEqual(len(w.lines), 20005)
        self.assertEqual((w.lines[0], w.lines[-1]), ("line 0", "line 20004"))
        self.assertFalse(w.poll_log())

    def test_worker_lines_capped_dropping_oldest(self):
        with mock.patch.object(sd, "MAX_LOG_LINES", 10):
            w = sd.Worker("w1", "haiku", "low", str(self.write_log(25)))
            self.assertTrue(w.poll_log())
        self.assertEqual((w.lines[0], w.lines[-1], len(w.lines), w.dropped), ("line 15", "line 24", 10, 15))

    def test_zoomed_scroll_follows_dropped_lines(self):
        log = self.write_log(25)
        d = sd.Dash(str(self.run), None)
        d.workers = {"w1": sd.Worker("w1", log=str(log)), "w2": sd.Worker("w2")}
        with mock.patch.object(sd, "MAX_LOG_LINES", 10):
            d.zoom, d.top = "w1", 12
            d.poll_logs()
            self.assertEqual(d.top, 0)  # 15 dropped, clamped at 0
            self.assertEqual(len(d.workers["w1"].lines), 10)
            with open(log, "a") as f:
                f.write(said("more"))
            d.top = 6
            d.poll_logs()
            self.assertEqual(d.top, 5)
            d.top = None  # following the newest line: untouched
            with open(log, "a") as f:
                f.write(said("more"))
            d.poll_logs()
            self.assertIsNone(d.top)

    def test_worker_without_log(self):
        self.assertFalse(sd.Worker("w1").poll_log())

    def test_late_roster_adds_worker_and_keeps_selection(self):
        self.status("w2-ui", "status: ui\n")
        d = sd.Dash(str(self.run), None)
        d.refresh_meta()
        self.assertEqual(d.names(), ["w2-ui"])
        self.assertEqual(d.selected, "w2-ui")
        self.roster(("w1-api", "haiku", "low", ""), ("w2-ui", "sonnet", "medium", ""))
        d.refresh_meta()
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
        d.refresh_meta()
        self.assertEqual(d.names(), ["w2-ui", "w1-api", "aa", "zz"])
        self.assertEqual(d.selected, "w2-ui")

    def test_roster_name_cannot_escape_workers_dir(self):
        (self.run / "secret.md").write_text("TOPSECRET\n")
        self.roster(("../secret", "m", "e", ""))
        (self.run / "workers").mkdir()
        d = sd.Dash(str(self.run), None)
        d.refresh_meta()
        self.assertEqual(d.workers["../secret"].status, "(no status yet)")

    def test_poll_logs_reads_fixture_and_keeps_lines_over_refresh(self):
        log = self.run / "agent-a.jsonl"
        shutil.copyfile(FIXTURE, log)
        self.roster(("w1-api", "haiku", "low", log))
        d = sd.Dash(str(self.run), None)
        d.refresh_meta()
        d.poll_logs()
        want = [l for line in FIXTURE.read_text().splitlines() for l in sd.format_record(json.loads(line))]
        self.assertEqual(list(d.workers["w1-api"].lines), want)
        d.refresh_meta()
        d.poll_logs()
        self.assertEqual(list(d.workers["w1-api"].lines), want)

    def test_tick_closes_after_delay(self):
        d = sd.Dash(str(self.run), os.getpid())
        self.assertTrue(d.tick(1000))
        self.assertIsNone(d.closing_at)  # still running
        (self.run / "report.md").write_text("done")
        self.assertTrue(d.tick(100))
        self.assertEqual(d.closing_at, 100 + sd.CLOSE_DELAY)
        self.assertTrue(d.tick(104.9))
        self.assertFalse(d.tick(105))

    def test_refresh_meta_gives_activity_wall_clock_time(self):
        self.roster(("w1", "m", "e", "/tmp/some.log"))
        with mock.patch.object(sd, "activity", return_value="") as act:
            sd.Dash(str(self.run), None).refresh_meta()  # no clock argument: activity() compares wall-clock time with mtimes
        act.assert_called_once_with("/tmp/some.log")

    def test_run_uses_monotonic_clock(self):
        (self.run / "report.md").write_text("done")
        d = sd.Dash(str(self.run), None)
        scr = mock.MagicMock()
        scr.getmaxyx.return_value = (24, 80)
        scr.getch.return_value = ord("q")
        clock = mock.Mock(monotonic=lambda: 1000.0, time=lambda: 5.0)
        with mock.patch.object(sd, "time", clock), mock.patch.object(sd.curses, "curs_set"):
            self.assertEqual(sd.run(scr, d), 0)
        self.assertEqual(d.closing_at, 1000.0 + sd.CLOSE_DELAY)
    def test_poll_log_follows_a_changed_roster_path(self):
        old, new = self.run / "old.jsonl", self.write_log(3)
        old.write_text(said("old 0"))
        w = sd.Worker("w1", log=str(old))
        self.assertTrue(w.poll_log())
        first = w._reader
        self.assertEqual(w.lines, ["old 0"])
        w.log = str(new)
        self.assertTrue(w.poll_log())
        self.assertIsNot(w._reader, first)
        self.assertEqual(w._reader.path, str(new))
        self.assertEqual(w.lines[-3:], ["line 0", "line 1", "line 2"])  # the new log is read from its start
        with open(old, "a") as f:  # the old file is no longer watched
            f.write(said("old 1"))
        self.assertFalse(w.poll_log())
        self.assertNotIn("old 1", w.lines)

    def test_refresh_meta_before_workers_dir_exists(self):
        self.roster(("w1", "haiku", "low", str(self.run / "none.jsonl")))
        d = sd.Dash(str(self.run), None)
        d.refresh_meta()
        self.assertEqual(d.names(), ["w1"])
        self.assertEqual(d.workers["w1"].status, "(no status yet)")
        self.assertEqual(d.selected, "w1")
        d.poll_logs()
        self.assertEqual(d.workers["w1"].lines, [])

    def test_refresh_meta_duplicate_roster_names_make_one_worker(self):  # pins current behaviour: the last row's fields win
        self.roster(("w1", "haiku", "low", "/tmp/a"), ("w2", "m", "e", ""), ("w1", "opus", "high", "/tmp/b"))
        d = sd.Dash(str(self.run), None)
        d.refresh_meta()
        self.assertEqual(d.names(), ["w1", "w2"])
        self.assertEqual((d.workers["w1"].model, d.workers["w1"].effort, d.workers["w1"].log), ("opus", "high", "/tmp/b"))


class FakeScr:
    """Just enough of a curses window: a grid of cells and, like real curses, an error when the bottom-right cell is written."""
    def __init__(self, h, w):
        self.h, self.w = h, w
        self.erase()
        self.refreshed = 0

    def erase(self):
        self.cells = [[" "] * self.w for _ in range(self.h)]  # a wide char's second cell is "", a zero-width mark is appended to the previous cell
        self.attrs = [[0] * self.w for _ in range(self.h)]

    def getmaxyx(self):
        return self.h, self.w

    def refresh(self):
        self.refreshed += 1

    def addstr(self, y, x, text, attr=0):
        if not (0 <= y < self.h and 0 <= x < self.w):
            raise curses.error("addstr() returned ERR")
        for ch in text:
            zero = unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf")
            cw = 0 if zero else 2 if unicodedata.east_asian_width(ch) in "WF" else 1
            if x + cw > self.w:
                raise AssertionError(f"text wrapped past the right edge at row {y}: {text!r}")
            if zero:
                self.cells[y][x - 1] += ch
                continue
            self.cells[y][x] = ch
            self.attrs[y][x] = attr
            if cw == 2:
                self.cells[y][x + 1], self.attrs[y][x + 1] = "", attr
            x += cw
            if y == self.h - 1 and x >= self.w:
                raise curses.error("addstr() returned ERR")  # the character is written, then curses fails to move the cursor

    def row(self, y):
        return "".join(self.cells[y]).rstrip()


def dash_with(names=(), tasks_=(), **fields):
    d = sd.Dash("x", None)
    d.workers = {n: sd.Worker(n) for n in names}
    d._order = list(names)
    d.tasks = list(tasks_)
    for n in names:
        d.workers[n].status = "s-" + n
    d.selected = names[0] if names else None
    for k, v in fields.items():
        setattr(d, k, v)
    return d


class ScreenTest(unittest.TestCase):
    def test_fit_wide_combining_and_zero_width(self):
        self.assertEqual(sd.fit("日本語", 4), "日本")
        self.assertEqual(sd.fit("日本語", 5), "日本")  # a wide char never straddles the edge
        self.assertEqual(sd.fit("日本語", 1), "")
        self.assertEqual(sd.fit("日本", 5, pad=True), "日本 ")
        self.assertEqual(sd.fit("日", 1, pad=True), " ")
        self.assertEqual(sd.fit("Ａb", 2), "Ａ")  # fullwidth latin counts 2
        self.assertEqual(sd.fit("日\u0301本", 2), "日\u0301")  # a mark after a wide char costs nothing
        self.assertEqual(sd.fit("ab\u0301", 2), "ab\u0301")  # ...and is kept even at the edge
        self.assertEqual(sd.fit("a\u200db", 2), "a\u200db")  # zero-width joiner
        self.assertEqual(sd.fit("\u200b\u0301", 3, pad=True), "   ")  # nothing but marks: all dropped, padding keeps the width
        self.assertEqual(sd.fit("", 3, pad=True), "   ")
        self.assertEqual(sd.fit("abc", 0), "")
        self.assertEqual(sd.fit("abc", 0, pad=True), "")
        self.assertEqual(sd.fit("abc", -2, pad=True), "")
        self.assertEqual(sd.fit("\u0301ab", 5), "ab")  # leading combining mark dropped
        self.assertEqual(sd.fit("e\u0301\u200bx", 2), "e\u0301\u200bx")  # Mn and Cf take no cell
        self.assertEqual(sd.fit("日本", 3), "日")

    def test_put_clips_by_cells_and_pads_when_reversed(self):
        scr = FakeScr(3, 10)
        sd.put(scr, 0, 0, "日本語日本語")
        self.assertEqual(scr.row(0), "日本語日本")
        self.assertEqual(scr.cells[0][:3], ["日", "", "本"])
        sd.put(scr, 1, 2, "ab", curses.A_REVERSE)
        self.assertEqual("".join(scr.cells[1]), "  ab      ")
        self.assertEqual(scr.attrs[1], [0, 0] + [curses.A_REVERSE] * 8)  # reverse video is padded to the right edge
        sd.put(scr, 2, 0, "x\u0301yz", maxw=2)
        self.assertEqual(scr.cells[2][:3], ["x\u0301", "y", " "])

    def test_put_ignores_off_screen_and_cleans_text(self):
        scr = FakeScr(3, 10)
        for y, x, maxw in ((-1, 0, None), (3, 0, None), (0, 10, None), (0, -1, None), (0, 0, 0), (0, 0, -3)):
            sd.put(scr, y, x, "zzz", 0, maxw)
        self.assertEqual([scr.row(i) for i in range(3)], ["", "", ""])
        sd.put(scr, 0, 0, "a\tb\nc\x1b[2Jd\u202e")
        self.assertEqual(scr.row(0), "a b c[2Jd")  # tab and newline become spaces; ESC and bidi marks are removed
        sd.put(scr, 1, 8, "abcdef")
        self.assertEqual(scr.row(1), " " * 8 + "ab")

    def test_put_swallows_error_on_bottom_right_cell(self):
        scr = FakeScr(3, 10)
        with self.assertRaises(curses.error):  # the fake really behaves like curses
            scr.addstr(2, 0, "x" * 10)
        sd.put(scr, 2, 0, "y" * 20)
        self.assertEqual(scr.row(2), "y" * 10)
        sd.put(scr, 2, 9, "z")
        sd.put(scr, 2, 0, "w", curses.A_REVERSE)  # padded to the corner too
        self.assertEqual("".join(scr.cells[2]), "w" + " " * 9)
        sd.put(scr, 2, 8, "日")  # a wide char ending exactly in the corner
        self.assertEqual(scr.cells[2][8], "日")

    def test_title(self):
        w = sd.Worker("w1", "haiku", "low")
        self.assertEqual(sd.title(w), "w1 | haiku low")
        self.assertEqual(sd.title(sd.Worker("w1")), "w1")
        self.assertEqual(sd.title(sd.Worker("w1", model="opus")), "w1 | opus")
        self.assertEqual(sd.title(sd.Worker("w1", effort="high")), "w1 | high")

    def test_closing_text(self):
        d = dash_with()
        self.assertEqual(sd.closing_text(d, 100), "")
        d.closing_at = 105
        self.assertEqual(sd.closing_text(d, 100), "run over; closing in 5 s")
        self.assertEqual(sd.closing_text(d, 100.2), "run over; closing in 5 s")  # rounds up
        self.assertEqual(sd.closing_text(d, 104.9), "run over; closing in 1 s")
        self.assertEqual(sd.closing_text(d, 105), "run over; closing in 0 s")
        self.assertEqual(sd.closing_text(d, 200), "run over; closing in 0 s")  # never negative

    def test_layout_empty_and_narrow(self):
        self.assertEqual(sd.layout(dash_with(), 30, 80), ([], 0, 30, None))  # no workers: no grid shape
        d = dash_with(["a", "b", "c", "d", "e"])
        self.assertEqual(sd.layout(d, 10, 40), ([], 0, 10, None))  # boxes would be too small: collapsed list
        self.assertEqual(sd.layout(d, 3, 80)[3], None)
        strip, top, body, shape = sd.layout(dash_with(["a"]), 30, 80)
        self.assertEqual((strip, top, body, shape), ([], 0, 30, (1, 1)))

    def test_layout_with_tasks_and_closing(self):
        d = dash_with(["a", "b"], tasks(3))
        strip, top, body, shape = sd.layout(d, 30, 80)
        self.assertEqual((len(strip), top, body), (3, 4, 26))
        self.assertIsNotNone(shape)
        d.closing_at = 1
        strip, top, body, _ = sd.layout(d, 30, 80)
        self.assertEqual((len(strip), top, body), (3, 5, 25))  # the banner takes the first row
        self.assertEqual(sd.layout(d, 3, 80)[:3], ([], 2, 1))  # a tiny window: the banner pushes the strip out

    def draw(self, d, h, w, now=100):
        scr = FakeScr(h, w)
        sd.draw(scr, d, now)
        self.assertEqual(scr.refreshed, 1)
        return scr

    def test_draw_small_window_only_asks_to_enlarge(self):
        for h, w in ((sd.MIN_H - 1, 80), (24, sd.MIN_W - 1)):
            scr = self.draw(dash_with(["a"], tasks(2)), h, w)
            self.assertEqual(scr.row(0), "enlarge the window"[:w])
            self.assertEqual([scr.row(i) for i in range(1, h)], [""] * (h - 1))

    def test_draw_empty_workers_and_tasks(self):
        scr = self.draw(dash_with(), 12, 50)
        self.assertEqual(scr.row(0), "(no workers yet)")  # no strip, so no separator row either
        self.assertEqual(scr.row(1), "")
        scr = self.draw(dash_with(tasks_=tasks(2)), 12, 50)
        self.assertIn("t1 w1-api", scr.row(0))
        self.assertEqual(scr.row(2), "-" * 50)
        self.assertEqual(scr.row(3), "(no workers yet)")

    def test_draw_grid_boxes(self):
        d = dash_with(["w1", "w2"], tasks(1))
        d.workers["w1"].model, d.workers["w1"].flag = "haiku", " (quiet 3m)"
        d.workers["w1"].lines = [f"line {i}" for i in range(40)]
        scr = self.draw(d, 24, 80)
        self.assertEqual(scr.row(1), "-" * 80)
        self.assertTrue(scr.row(2).startswith("w1 | haiku"))
        self.assertEqual(scr.attrs[2][0], curses.A_REVERSE)  # selected
        self.assertEqual(scr.attrs[2][41], curses.A_BOLD)  # unselected second box
        self.assertIn("s-w1 (quiet 3m)", scr.row(3))
        self.assertIn("line 39", "".join(scr.row(y) for y in range(24)))
        self.assertNotIn("line 0 ", "".join(scr.row(y) for y in range(24)) + " ")

    def test_draw_narrow_collapsed_list_mode(self):
        names = [f"w{i}" for i in range(30)]
        d = dash_with(names, selected="w20")
        d.workers["w3"].flag = " (finished)"
        self.assertIsNone(sd.layout(d, 12, 40)[3])
        scr = self.draw(d, 12, 40)
        shown = [scr.row(y) for y in range(12)]
        self.assertIn("w20: s-w20", shown)
        reversed_rows = [y for y in range(12) if scr.attrs[y][0] == curses.A_REVERSE]
        self.assertEqual([shown[y] for y in reversed_rows], ["w20: s-w20"])
        self.assertNotIn("w0: s-w0", shown)  # window scrolled to keep the selection visible
        d.selected = "w3"
        self.assertIn("w3: s-w3 (finished)", [self.draw(d, 12, 40).row(y) for y in range(12)])

    def test_draw_list_mode_writing_the_bottom_right_cell(self):
        d = dash_with(["a", "b", "c", "d", "e"], selected="e")
        d.workers["e"].status = "x" * 100
        scr = self.draw(d, 10, 40)  # the selected last row is padded to the corner; curses.error must not escape
        self.assertEqual(scr.row(4), "e: " + "x" * 37)

    def test_draw_corner_cell_in_grid_and_zoom(self):
        d = dash_with(["w1"])
        d.workers["w1"].lines = ["y" * 300] * 40
        self.draw(d, 24, 80)
        d.zoom = "w1"
        scr = self.draw(d, 24, 80)
        self.assertEqual(scr.row(23), "y" * 80)

    def test_draw_zoomed_follows_newest_or_scrolls(self):
        d = dash_with(["w1"])
        w = d.workers["w1"]
        w.model, w.flag, w.lines = "haiku", " (quiet 2m)", [f"line {i}" for i in range(50)]
        d.zoom = "w1"
        scr = self.draw(d, 12, 60)
        self.assertEqual(scr.row(0), "w1 | haiku | s-w1 (quiet 2m)")
        self.assertEqual(scr.attrs[0], [curses.A_REVERSE] * 60)
        self.assertEqual([scr.row(y) for y in (1, 11)], ["line 39", "line 49"])  # top None: the newest 11 lines
        d.top = 5
        scr = self.draw(d, 12, 60)
        self.assertEqual([scr.row(y) for y in (1, 11)], ["line 5", "line 15"])
        d.top = 45  # near the end: fewer lines than rows, rest blank
        scr = self.draw(d, 12, 60)
        self.assertEqual([scr.row(y) for y in (1, 5, 6)], ["line 45", "line 49", ""])
        d.top = scroll_to = sd.scroll(None, "up", 50, 11)
        self.assertEqual(self.draw(d, 12, 60).row(1), f"line {scroll_to}")

    def test_draw_zoomed_short_log_and_unknown_zoom(self):
        d = dash_with(["w1"])
        d.workers["w1"].lines = ["only"]
        d.zoom = "w1"
        scr = self.draw(d, 12, 60)
        self.assertEqual((scr.row(1), scr.row(2)), ("only", ""))
        d.zoom = "gone"  # a zoom on a vanished worker falls back to the grid
        self.assertIn("w1", self.draw(d, 12, 60).row(0))

    def test_draw_closing_banner(self):
        d = dash_with(["w1"], tasks(1), closing_at=104)
        scr = self.draw(d, 24, 80, now=100)
        self.assertEqual(scr.row(0), "run over; closing in 4 s")
        self.assertEqual(scr.attrs[0][0], curses.A_BOLD)
        self.assertIn("t1 w1-api", scr.row(1))
        self.assertEqual(scr.row(2), "-" * 80)
        d.zoom = "w1"
        self.assertEqual(self.draw(d, 24, 80, now=103).row(0), "w1 | s-w1  run over; closing in 1 s")
        d = dash_with(["w1"], closing_at=104)  # no tasks: banner alone, then the separator
        scr = self.draw(d, 24, 80, now=100)
        self.assertEqual((scr.row(0), scr.row(1)), ("run over; closing in 4 s", "-" * 80))


class UiTest(RunDirTest):
    def test_main_usage_errors(self):
        for argv in ([], ["a", "b", "c"], ["run", "notapid"], ["run", "0"], ["run", "-1"]):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(sd.main(argv), 2)
            self.assertIn("usage: swarm-dash.py", err.getvalue())

    def test_main_missing_run_folder(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, mock.patch("sys.stdout.isatty", create=True, return_value=True):
            self.assertEqual(sd.main([str(self.dir / "nope")]), 2)
        self.assertIn("not a folder", err.getvalue())

    def test_non_positive_pid_never_reaches_kill(self):
        with mock.patch.object(sd.os, "kill") as kill, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sd.main([str(self.run), "0"]), 2)
            self.assertEqual(sd.main([str(self.run), "-1"]), 2)
            self.assertFalse(sd.run_over(str(self.run), -1))
            self.assertFalse(sd.run_over(str(self.run), 0))
        kill.assert_not_called()

    def test_main_needs_terminal(self):
        r = subprocess.run([sys.executable, str(SCRIPT), str(self.run)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("needs a terminal", r.stderr)

    def test_key_map(self):
        for ch, name in ((curses.KEY_LEFT, "left"), (9, "tab"), (10, "enter"), (27, "esc"), (ord("3"), "3"), (ord("q"), "q"),
                         (curses.KEY_NPAGE, "pgdn"), (curses.KEY_RESIZE, "resize"), (ord("x"), None), (-1, None)):
            self.assertEqual(sd.KEYS.get(ch), name)

    def test_handle_key_zoom_cycle(self):
        log = self.run / "agent-a.jsonl"
        shutil.copyfile(FIXTURE, log)
        self.roster(("w1-api", "haiku", "low", log), ("w2-ui", "haiku", "low", ""))
        d = sd.Dash(str(self.run), None)
        d.refresh_meta()
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
        made = self.tmux("new-session", "-d", "-x", "160", "-y", "45", "-s", "d",
                         shlex.join([sys.executable, str(SCRIPT), str(self.run)]))
        self.assertEqual(made.returncode, 0, made.stderr)

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.sock, *args], capture_output=True, text=True)

    def cap(self):
        return self.tmux("capture-pane", "-p", "-t", "d").stdout

    def wait(self, text, secs=5):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            if text in self.cap():
                return
            time.sleep(0.1)
        self.fail(f"{text!r} not on screen:\n{self.cap()}")

    def gone(self, secs):
        end = time.monotonic() + secs
        while time.monotonic() < end:
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
        self.assertTrue(self.gone(12), self.cap())

    def test_ctrl_c_exits(self):
        self.wait("w1-api")
        self.tmux("send-keys", "-t", "d", "C-c")
        self.assertTrue(self.gone(3), self.cap())


if __name__ == "__main__":
    unittest.main()
