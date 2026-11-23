import contextlib
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
        self.assertNotIn("a b.md", out)
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


if __name__ == "__main__":
    unittest.main()
