import contextlib
import sys
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("swarm_run", ROOT / "skills" / "swarm" / "scripts" / "swarm-run.py")
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)

TABLE = """| id | title | owner | status | files | check | depends | fixes |
|---|---|---|---|---|---|---|---|
| t1 | api | w1-api | pending | src/api.py, `src/new.py` | python3 -m pytest -q \\| tail -n 5 | none | 0 |
| t2 | docs | w2-docs | pending | docs/a b.md | manual read | none | 0 |
| h1 | hub lines | main | pending | hub.py | - | t1 | 0 |
"""


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


def run_main(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sr.main(argv)
    return code, out.getvalue(), err.getvalue()


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base)
        self.root = self.base / "proj with space "
        (self.root / "src").mkdir(parents=True)
        (self.root / "docs").mkdir()
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        env = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.base)})
        env.start()
        self.addCleanup(env.stop)


class GitRunTest(TempDirTest):
    def setUp(self):
        super().setUp()
        (self.root / "src" / "api.py").write_text("def a():\n    return 1\n")
        (self.root / "docs" / "a b.md").write_text("old\n")
        (self.root / "dirty.txt").write_text("v1\n")
        (self.root / "other.py").write_text("x = 1\n")
        (self.root / "hub.py").write_text("ROUTES = []\n")
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "init")
        (self.root / "docs" / "a b.md").write_text("dirty before\n")  # owned, already modified at start
        (self.root / "dirty.txt").write_text("v2\n")  # not owned, already modified at start
        os.chdir(self.root / "src")  # paths stay relative to the git top level
        code, out, err = run_main(["init"], TABLE)
        self.assertEqual(code, 0, err)
        self.run_dir = out.strip()

    def test_init_layout(self):
        run = pathlib.Path(self.run_dir)
        self.assertEqual(run.parent.parent, self.root / ".swarm")
        self.assertEqual((self.root / ".swarm" / ".gitignore").read_text(), "*\n")
        self.assertEqual((run / "tasks.md").read_text(), TABLE)
        self.assertTrue((run / "workers").is_dir())
        self.assertEqual((run / "snapshot" / "src" / "api.py").read_text(), "def a():\n    return 1\n")
        self.assertEqual((run / "snapshot" / "docs" / "a b.md").read_text(), "dirty before\n")
        self.assertFalse((run / "snapshot" / "src" / "new.py").exists())
        snap = json.loads((run / "snapshot.json").read_text())
        self.assertEqual(snap["owned"], {"t1": ["src/api.py", "src/new.py"], "t2": ["docs/a b.md"], "h1": ["hub.py"]})
        self.assertEqual(snap["owners"], {"t1": "w1-api", "t2": "w2-docs", "h1": "main"})
        self.assertEqual(sorted(snap["dirty"]), ["dirty.txt", "docs/a b.md"])

    def test_second_init_same_second_gets_own_folder(self):
        with mock.patch.object(sr.time, "strftime", return_value=pathlib.Path(self.run_dir).name):
            code, out, _ = run_main(["init"], TABLE)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), self.run_dir + "-2")

    def test_diff_shows_every_kind_of_change(self):
        (self.root / "src" / "api.py").write_text("def a():\n    return 2\n")
        (self.root / "src" / "new.py").write_text("NEW = 1\n")
        (self.root / "docs" / "a b.md").write_text("dirty after\n")
        (self.root / "other.py").write_text("x = 2\n")
        (self.root / "dirty.txt").write_text("v3\n")
        (self.root / "hub.py").write_text("ROUTES = ['csv']\n")  # a worker touched main's hub file
        code, out, _ = run_main(["diff", self.run_dir, "t1"])
        self.assertEqual(code, 0)
        self.assertIn("== src/api.py: modified (+1 -1)", out)
        self.assertIn("+    return 2", out)
        self.assertIn("== src/new.py: new (+1 -0)", out)
        self.assertNotIn("== docs/a b.md", out)  # t2's file: reported only as a peer change, not diffed
        self.assertIn("CHANGED IN OTHER TASKS' FILES: docs/a b.md (t2 pending)", out)
        self.assertIn("CHANGED IN main's FILES: hub.py", out)
        self.assertIn("CHANGED OUTSIDE EVERY TASK: dirty.txt, other.py", out)
        code, out, _ = run_main(["diff", self.run_dir])
        self.assertIn("== docs/a b.md: modified (+1 -1)", out)
        self.assertIn("-dirty before", out)
        self.assertIn("== hub.py: modified (+1 -1)", out)
        self.assertNotIn("CHANGED IN main's FILES", out)  # the final review diffs everything, hub edits included

    def test_diff_when_nothing_changed(self):
        code, out, _ = run_main(["diff", self.run_dir])
        self.assertEqual(code, 0)
        self.assertIn("unchanged: src/api.py, src/new.py, docs/a b.md, hub.py", out)
        self.assertNotIn("CHANGED IN main's FILES", run_main(["diff", self.run_dir, "t1"])[1])
        self.assertIn("nothing changed outside the tasks' files", out)

    def test_diff_binary_and_cap(self):
        (self.root / "src" / "new.py").write_bytes(b"\x00\x01")
        (self.root / "src" / "api.py").write_text("".join(f"line {i}\n" for i in range(50)))
        with mock.patch.object(sr, "MAX_DIFF_LINES", 10):
            _, out, _ = run_main(["diff", self.run_dir, "t1"])
        self.assertIn("== src/new.py: new, binary", out)
        self.assertIn("[not shown: over the 10-line cap]", out)
        self.assertIn("NOT SHOWN, read these directly: src/api.py", out)
        self.assertNotIn("line 49", out)

    def test_diff_unknown_task(self):
        code, _, err = run_main(["diff", self.run_dir, "t9"])
        self.assertEqual(code, 1)
        self.assertIn("unknown task t9", err)

    def test_init_rejects_bad_tables(self):
        cases = {
            "| id | files |\n|---|---|\n| t1 | a.py |\n| t2 | ./a.py |\n": "a.py is owned by both t1 and t2",
            "| id | files |\n| t1 | ../x.py |\n": "outside the project",
            "| id | files |\n| t1 | /etc/passwd |\n": "outside the project",
            "| id | files |\n| t1 | .swarm/x |\n": "inside .swarm/",
            "| id | files |\n| t1 | src |\n": "is a folder",
            "| id | files | check |\n| t1 | a.py | pytest | tail |\n": "write a | inside a cell as \\|",
            "| id | files |\n| t1 | a.py |\n| t1 | b.py |\n": "used twice",
            "| task | paths |\n| t1 | a.py |\n": "`id` and `files` columns",
        }
        before = sorted(os.listdir(self.root / ".swarm" / "runs"))
        for table, problem in cases.items():
            code, _, err = run_main(["init"], table)
            self.assertEqual(code, 1, table)
            self.assertIn(problem, err, table)
        self.assertEqual(sorted(os.listdir(self.root / ".swarm" / "runs")), before)


class SymlinkAndSnapshotTest(TempDirTest):
    def test_symlink_out_of_root_rejected(self):
        (self.base / "secret.txt").write_text("s\n")
        os.symlink(self.base, self.root / "out")
        os.chdir(self.root)
        code, _, err = run_main(["init"], "| id | files |\n| t1 | out/secret.txt |\n")
        self.assertEqual(code, 1)
        self.assertIn("outside the project", err)

    def test_malformed_snapshot_is_clean_error(self):
        run = self.base / "run"
        run.mkdir()
        (run / "snapshot.json").write_text("{}")
        code, _, err = run_main(["diff", str(run)])
        self.assertEqual(code, 1)
        self.assertIn("no valid snapshot.json", err)


class NoGitRunTest(TempDirTest):
    def test_init_and_diff_without_git(self):
        (self.root / "src" / "api.py").write_text("v1\n")
        os.chdir(self.root)
        code, out, _ = run_main(["init"], "| id | files |\n| t1 | src/api.py, src/new.py |\n")
        self.assertEqual(code, 0)
        run = out.strip()
        (self.root / "src" / "api.py").write_text("v2\n")
        code, out, _ = run_main(["diff", run])
        self.assertIn("== src/api.py: modified (+1 -1)", out)
        self.assertIn("no git: changes outside the tasks' files are not checked", out)


def rec(kind, ts, content=None, mid=None, model=None, usage=None):
    r = {"type": kind, "timestamp": ts}
    if kind == "user":
        r["message"] = {"role": "user", "content": content}
    else:
        r["message"] = {"id": mid, "model": model, "usage": usage, "content": content or []}
    return r


def usage(inp, write, read, out, w1h=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": write, "cache_read_input_tokens": read, "output_tokens": out,
            "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": write - w1h}}


def write_log(path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs) + "{not json\n")


class CostTest(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base)
        cmd = "<command-message>swarm</command-message>\n<command-name>/swarm</command-name>\n<command-args>{}</command-args>"
        sess = self.base / "projects" / "p" / "sess"
        write_log(self.base / "projects" / "p" / "sess.jsonl", [
            rec("user", "2026-11-23T06:00:00.000Z", cmd.format("an earlier run")),
            rec("assistant", "2026-11-23T06:00:01.000Z", mid="m0", model="claude-opus-5-5", usage=usage(1, 1, 1, 9999)),
            rec("user", "2026-11-23T07:00:00.000Z", cmd.format("this run")),
            rec("user", "2026-11-23T07:00:01.000Z", [{"type": "tool_result", "content": "grep hit: " + sr.SWARM_COMMAND}]),
            rec("assistant", "2026-11-23T07:00:02.000Z", mid="m1", model="claude-opus-5-5", usage=usage(10, 1000, 100000, 500, w1h=1000)),
            rec("assistant", "2026-11-23T07:00:02.500Z", mid="m1", model="claude-opus-5-5", usage=usage(10, 1000, 100000, 500, w1h=1000)),
            rec("assistant", "2026-11-23T07:00:03.000Z", mid="m2", model="<synthetic>", usage=usage(0, 0, 0, 0)),
        ])
        write_log(sess / "subagents" / "agent-aaa.jsonl", [
            rec("user", "2026-11-23T07:01:00.000Z", "brief"),
            rec("assistant", "2026-11-23T07:01:05.000Z", mid="w1", model="claude-sonnet-5-5", usage=usage(2, 2000, 10000, 100)),
        ])
        write_log(sess / "subagents" / "agent-bbb.jsonl", [
            rec("user", "2026-11-23T07:00:30.000Z", "explore"),
            rec("assistant", "2026-11-23T07:00:31.000Z", mid="h1", model="claude-future-9", usage=usage(5, 0, 0, 5)),
        ])
        (sess / "subagents" / "agent-bbb.meta.json").write_text('{"agentType": "Explore"}')
        write_log(sess / "subagents" / "agent-ccc.jsonl", [
            rec("assistant", "2026-11-23T06:30:00.000Z", mid="old", model="claude-haiku-5-5", usage=usage(1, 1, 1, 1)),
        ])
        out_file = self.base / "tasks" / "aaa.output"  # Claude Code's output file is a symlink to the agent log
        out_file.parent.mkdir()
        out_file.symlink_to(sess / "subagents" / "agent-aaa.jsonl")
        self.run_dir = self.base / "run"
        self.run_dir.mkdir()
        (self.run_dir / "roster.md").write_text(
            "| worker | agent id | model | effort | tasks | output file |\n|---|---|---|---|---|---|\n"
            f"| w1-api | aaa | sonnet | medium | t1 | {out_file} |\n")

    def test_tally_dedupes_and_prices(self):
        t = sr.tally(sr.records(self.base / "projects" / "p" / "sess.jsonl"))
        self.assertEqual(t["requests"], 2)  # m0 and m1 once each; the synthetic zero-usage record is skipped
        t = sr.tally(sr.records(self.base / "projects" / "p" / "sess" / "subagents" / "agent-aaa.jsonl"))
        self.assertAlmostEqual(t["usd"], (2 + 2000 * 1.25 + 10000 * 0.05) * 2 / 1e6 + 100 * 10 / 1e6)

    def test_cost_table(self):
        code, out, err = run_main(["cost", str(self.run_dir)])
        self.assertEqual(code, 0, err)
        self.assertIn("| overseer | claude-opus-5-5 | 1 | 10 | 1,000 | 100,000 | 500 | $0.04 |", out)
        self.assertIn("| w1-api | claude-sonnet-5-5 | 1 | 2 | 2,000 | 10,000 | 100 | $0.01 |", out)
        self.assertIn("| other: Explore | claude-future-9 | 1 | 5 | 0 | 0 | 5 | $0.00+? |", out)
        self.assertNotIn("haiku", out)  # agent ccc started before this run
        self.assertLess(out.index("| w1-api"), out.index("| other: Explore"))
        self.assertIn("Overseer share of the estimated cost: 84%.", out)

    def test_cost_without_worker_log(self):
        (self.run_dir / "roster.md").write_text("| worker | agent id | output file |\n| w1 | aaa | /nonexistent |\n")
        code, _, err = run_main(["cost", str(self.run_dir)])
        self.assertEqual(code, 1)
        self.assertIn("no readable worker log", err)

    def test_usage_errors(self):
        for argv in ([], ["init", "x"], ["diff"], ["cost"], ["bogus"]):
            self.assertEqual(run_main(argv)[0], 2, argv)



class ParseTableTest(unittest.TestCase):
    def test_edge_cells_and_escaped_pipes(self):
        h, rows = sr.parse_table("| a | b | c |\n|---|---|---|\n| x | y ||\n|| x | y |\n| p \\| q | r\\| |  |\n")
        self.assertEqual(h, ["a", "b", "c"])
        self.assertEqual(rows, [["x", "y", ""], ["", "x", "y"], ["p | q", "r|", ""]])

    def test_only_first_table(self):
        h, rows = sr.parse_table("intro\n| a | b |\n|--|--|\n| 1 | 2 |\n\ntext\n| c | d |\n| 3 | 4 |\n")
        self.assertEqual((h, rows), (["a", "b"], [["1", "2"]]))


class SafetyTest(GitRunTest):
    def paths(self):
        return pathlib.Path(self.run_dir)

    def test_init_rejects_symlink_escaping_root(self):
        (self.base / "secret.txt").write_text("secret\n")
        (self.root / "src" / "esc.py").symlink_to(self.base / "secret.txt")
        before = sorted(os.listdir(self.root / ".swarm" / "runs"))
        code, _, err = run_main(["init"], "| id | files |\n| t1 | src/esc.py |\n")
        self.assertEqual(code, 1)
        self.assertIn("src/esc.py resolves outside the project", err)
        self.assertEqual(sorted(os.listdir(self.root / ".swarm" / "runs")), before)

    def test_init_snapshot_failure_removes_run_folder(self):
        before = sorted(os.listdir(self.root / ".swarm" / "runs"))
        with mock.patch.object(sr, "snapshot_file", side_effect=PermissionError(13, "denied")):
            code, out, err = run_main(["init"], TABLE)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("denied", err)
        self.assertEqual(sorted(os.listdir(self.root / ".swarm" / "runs")), before)

    def test_fifo_at_owned_path_does_not_hang(self):
        os.remove(self.root / "src" / "api.py")
        os.mkfifo(self.root / "src" / "api.py")
        self.assertIsNone(sr.read_bytes(self.root / "src" / "api.py"))
        self.assertEqual(sr.file_hash(self.root / "src" / "api.py"), "special")
        code, out, _ = run_main(["diff", self.run_dir, "t1"])
        self.assertEqual(code, 0)
        self.assertIn("== src/api.py: special file", out)

    def test_symlink_at_owned_path_is_not_followed(self):
        (self.base / "secret.txt").write_text("TOPSECRET\n")
        os.remove(self.root / "src" / "api.py")
        (self.root / "src" / "api.py").symlink_to(self.base / "secret.txt")
        self.assertIsNone(sr.read_bytes(self.root / "src" / "api.py"))
        code, out, _ = run_main(["diff", self.run_dir, "t1"])
        self.assertEqual(code, 0)
        self.assertIn("== src/api.py: symlink -> ", out)
        self.assertNotIn("TOPSECRET", out)

    def test_large_file_compared_by_hash(self):
        big, snap = self.root / "src" / "api.py", self.paths() / "snapshot" / "src" / "api.py"
        with mock.patch.object(sr, "MAX_READ", 16):
            self.assertIs(sr.read_bytes(big), sr.LARGE)
            self.assertEqual(sr.file_diff(snap, big, "src/api.py"), (None, []))  # same bytes as the snapshot
            big.write_text("def a():\n    return 'a much longer line than sixteen bytes'\n")
            self.assertEqual(sr.file_diff(snap, big, "src/api.py"), ("== src/api.py: modified, too large to diff", []))

    def test_tampered_snapshot_paths_rejected(self):
        snap_path = self.paths() / "snapshot.json"
        good = snap_path.read_text()
        for bad, why in (("../secret", "contains .."), ("/etc/passwd", "outside"), (".swarm/runs/x", ".swarm"), ("a/../../b", "contains ..")):
            snap = json.loads(good)
            snap["owned"]["t1"].append(bad)
            snap_path.write_text(json.dumps(snap))
            code, out, err = run_main(["diff", self.run_dir, "t1"])
            self.assertEqual((code, out), (1, ""), bad)
            self.assertIn(bad, err)
            self.assertIn(why, err)
        (self.root / "link").symlink_to(self.base)  # a folder component that leaves the project
        snap = json.loads(good)
        snap["owned"]["t1"].append("link/x")
        snap_path.write_text(json.dumps(snap))
        self.assertEqual(run_main(["diff", self.run_dir, "t1"])[0], 1)

    def test_peer_check_pending_and_done(self):
        (self.root / "docs" / "a b.md").write_text("by t1\n")
        out = run_main(["diff", self.run_dir, "t1"])[1]
        self.assertIn("CHANGED IN OTHER TASKS' FILES: docs/a b.md (t2 pending)", out)
        reviewed = json.loads((self.paths() / "reviewed.json").read_text())
        self.assertEqual(sorted(reviewed["t1"]), ["src/api.py", "src/new.py"])
        run_main(["diff", self.run_dir, "t2"])  # t2 reviewed with its file as it is now
        tasks = (self.paths() / "tasks.md").read_text().replace("| t2 | docs | w2-docs | pending", "| t2 | docs | w2-docs | done")
        (self.paths() / "tasks.md").write_text(tasks)
        self.assertNotIn("OTHER TASKS", run_main(["diff", self.run_dir, "t1"])[1])  # unchanged since t2's review
        (self.root / "docs" / "a b.md").write_text("by t1 again\n")
        self.assertIn("(t2 done)", run_main(["diff", self.run_dir, "t1"])[1])
        reviewed = json.loads((self.paths() / "reviewed.json").read_text())
        self.assertIn("t1", reviewed)  # merged, not replaced
        self.assertIn("t2", reviewed)

    def test_peer_check_skips_done_task_without_review(self):
        tasks = (self.paths() / "tasks.md").read_text().replace("| t2 | docs | w2-docs | pending", "| t2 | docs | w2-docs | done")
        (self.paths() / "tasks.md").write_text(tasks)
        (self.root / "docs" / "a b.md").write_text("changed\n")
        self.assertNotIn("OTHER TASKS", run_main(["diff", self.run_dir, "t1"])[1])

    def test_hub_check_excludes_the_task_itself(self):
        (self.root / "hub.py").write_text("ROUTES = [1]\n")
        self.assertNotIn("CHANGED IN main's FILES", run_main(["diff", self.run_dir, "h1"])[1])

    def test_git_status_failure_is_reported(self):
        with mock.patch.object(sr, "git", return_value=subprocess.CompletedProcess([], 128, b"", b"fatal")):
            self.assertIsNone(sr.dirty_files(self.root))
            code, out, _ = run_main(["diff", self.run_dir])
        self.assertEqual(code, 0)
        self.assertIn("changes outside the tasks' files not checked: git status failed", out)
        self.assertNotIn("nothing changed", out)

    def test_init_with_failing_git_status_warns_and_stores_null(self):
        real = sr.git
        fake = lambda root, *a: subprocess.CompletedProcess([], 128, b"", b"") if a[0] == "status" else real(root, *a)
        with mock.patch.object(sr, "git", fake):
            code, out, err = run_main(["init"], TABLE)
        self.assertEqual(code, 0)
        self.assertIn("git status failed", err)
        run = pathlib.Path(out.strip())
        self.assertIsNone(json.loads((run / "snapshot.json").read_text())["dirty"])
        code, out, _ = run_main(["diff", str(run)])
        self.assertIn("changes outside the tasks' files not checked: git status failed", out)
        self.assertNotIn("nothing changed", out)

    def test_init_stores_started_at(self):
        snap = json.loads((self.paths() / "snapshot.json").read_text())
        self.assertIsNotNone(sr.parse_ts(snap["started_at"]))

    def test_non_utf8_path_does_not_crash_diff(self):
        name = b"bad\xff.txt"
        try:
            with open(os.path.join(os.fsencode(self.root), name), "w") as f:
                f.write("x")
        except OSError:
            self.skipTest("file system rejects non-UTF-8 names")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")  # strict stdout, as on a UTF-8 terminal
        r = subprocess.run([sys.executable, str(ROOT / "skills" / "swarm" / "scripts" / "swarm-run.py"), "diff", self.run_dir],
                           cwd=self.root, env=env, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"bad\\udcff.txt", r.stdout)

    def test_file_diff_explains_invisible_changes(self):
        f = self.root / "src" / "api.py"
        f.write_bytes(b"def a():\r\n    return 1\r\n")
        self.assertIn("(+0 -0; line endings changed)", run_main(["diff", self.run_dir, "t1"])[1])
        f.write_bytes(b"def a():\n    return 1")
        self.assertIn("(+0 -0; trailing newline changed)", run_main(["diff", self.run_dir, "t1"])[1])
        f.write_bytes(b"def a():\n    return \xfe\n")
        (self.paths() / "snapshot" / "src" / "api.py").write_bytes(b"def a():\n    return \xff\n")
        self.assertIn("only non-UTF-8 bytes changed", run_main(["diff", self.run_dir, "t1"])[1])

    def test_file_replaced_by_directory(self):
        os.remove(self.root / "src" / "api.py")
        (self.root / "src" / "api.py").mkdir()
        self.assertIn("== src/api.py: directory", run_main(["diff", self.run_dir, "t1"])[1])


class PriceTest(unittest.TestCase):
    def test_lookup_price(self):
        sonnet = sr.PRICES["claude-sonnet-5-5"]
        for model in ("claude-sonnet-5-5", "claude-sonnet-5-5-20261101", "anthropic/claude-sonnet-5-5", "anthropic.claude-sonnet-5-5-20261101-v1:0",
                      "us.anthropic.claude-sonnet-5-5-v1:0", "claude-sonnet-5-5[1m]", "claude-sonnet-5-5@20261101", "claude-sonnet-5-5-preview"):
            self.assertEqual(sr.lookup_price(model), sonnet, model)
        self.assertEqual(sr.lookup_price("claude-haiku-5-5-20261101"), sr.PRICES["claude-haiku-5-5"])
        for model in ("claude-future-9", "?", "<synthetic>"):
            self.assertIsNone(sr.lookup_price(model), model)

    def test_tally_keeps_final_streaming_output(self):
        a = rec("assistant", "t", mid="m", model="claude-opus-5-5", usage=usage(10, 0, 0, 4))
        b = rec("assistant", "t", mid="m", model="claude-opus-5-5", usage=usage(10, 0, 0, 500))
        for order in ([a, b], [b, a]):
            t = sr.tally(order)
            self.assertEqual((t["requests"], t["output"]), (1, 500))
        t = sr.tally([rec("assistant", "t", mid=None, model="claude-opus-5-5", usage=usage(1, 0, 0, 1))] * 2)
        self.assertEqual(t["requests"], 2)  # no id: each record counts

    def test_tally_survives_malformed_usage(self):
        bad = rec("assistant", "t", mid="m", model="claude-opus-5-5",
                  usage={"input_tokens": "x", "output_tokens": [1], "cache_creation_input_tokens": 5, "cache_creation": 7, "cache_read_input_tokens": None})
        t = sr.tally([bad, {"type": "assistant", "message": "text"}, {"type": "assistant", "message": {"usage": []}},
                      rec("assistant", "t", mid=["unhashable"], model=3, usage=usage(1, 0, 0, 1))])
        self.assertEqual((t["requests"], t["write"], t["input"]), (2, 5, 1))
        self.assertTrue(t["unpriced"])  # model 3 -> "?"

    def test_swarm_command_must_open_the_message(self):
        cmd = "<command-message>swarm</command-message>\n" + sr.SWARM_COMMAND
        self.assertTrue(sr.is_swarm_command(rec("user", "t", cmd)))
        self.assertTrue(sr.is_swarm_command(rec("user", "t", [{"type": "text", "text": "  " + sr.SWARM_COMMAND}])))
        self.assertFalse(sr.is_swarm_command(rec("user", "t", "I wrote " + sr.SWARM_COMMAND)))
        self.assertFalse(sr.is_swarm_command(rec("user", "t", [{"type": "tool_result", "content": sr.SWARM_COMMAND}])))

    def test_parse_ts(self):
        z, frac, naive = sr.parse_ts("2026-11-23T07:00:00Z"), sr.parse_ts("2026-11-23T07:00:00.5+00:00"), sr.parse_ts("2026-11-23T07:00:00")
        self.assertLess(z, frac)
        self.assertEqual(z, naive)
        self.assertEqual(sr.parse_ts("2026-11-23T09:00:00+02:00"), z)
        self.assertIsNone(sr.parse_ts("garbage"))
        self.assertIsNone(sr.parse_ts(None))


class CostRunsTest(CostTest):
    def snapshot(self, started_at):
        (self.run_dir / "snapshot.json").write_text(json.dumps({"started_at": started_at}))

    def test_cost_of_the_first_run_in_a_session(self):
        self.snapshot("2026-11-23T06:00:30+00:00")
        code, out, err = run_main(["cost", str(self.run_dir)])
        self.assertEqual(code, 0, err)
        self.assertIn("| overseer | claude-opus-5-5 | 1 | 1 | 1 | 1 | 9,999 |", out)  # not the second run's m1
        self.assertIn("| other: ccc | claude-haiku-5-5 |", out)  # started inside the first run's window
        self.assertNotIn("Explore", out)  # started in the second

    def test_cost_second_run_by_started_at_and_fallback(self):
        for started in ("2026-11-23T07:00:10Z", None, "garbage", "2026-11-23T05:00:00Z"):
            self.snapshot(started)
            out = run_main(["cost", str(self.run_dir)])[1]
            self.assertIn("| overseer | claude-opus-5-5 | 1 | 10 | 1,000 | 100,000 | 500 |", out, started)
            self.assertIn("other: Explore", out)

    def test_total_marks_unpriced(self):
        out = run_main(["cost", str(self.run_dir)])[1]
        self.assertRegex(out, r"\| total \| .* \| \$\d+\.\d\d\+\? \|")

    def test_roster_with_backticks_and_log_outside_subagents(self):
        sess = self.base / "projects" / "p" / "sess"
        elsewhere = self.base / "elsewhere" / "zzz.jsonl"
        write_log(elsewhere, [rec("assistant", "2026-11-23T07:02:00.000Z", mid="z1", model="claude-haiku-5-5", usage=usage(7, 0, 0, 3))])
        (self.run_dir / "roster.md").write_text(
            "| worker | agent id | output file |\n|---|---|---|\n"
            f"| `w9-out` | `zzz` | `{elsewhere}` |\n"
            f"| `w1-api` | `aaa` | `{sess / 'subagents' / 'agent-aaa.jsonl'}` |\n")
        code, out, err = run_main(["cost", str(self.run_dir)])
        self.assertEqual(code, 0, err)
        self.assertIn("| w9-out | claude-haiku-5-5 | 1 | 7 | 0 | 0 | 3 |", out)
        self.assertIn("| w1-api | claude-sonnet-5-5 | 1 | 2 |", out)
        self.assertIn("| overseer |", out)  # session found through the log in subagents/
        self.assertEqual(out.count("| w1-api"), 1)  # not counted again by the agent-*.jsonl scan

    def test_label_cannot_break_the_table(self):
        meta = self.base / "projects" / "p" / "sess" / "subagents" / "agent-bbb.meta.json"
        meta.write_text(json.dumps({"agentType": "Ex|plore\nx| y\x1b[31m"}))
        out = run_main(["cost", str(self.run_dir)])[1]
        self.assertIn("| other: Ex\\|plorex\\| y[31m | claude-future-9 |", out)
        for line in out.splitlines():
            if line.startswith("| other"):
                self.assertEqual(len(sr.CELL_SPLIT.split(line.strip().strip("|"))), 8)


if __name__ == "__main__":
    unittest.main()
