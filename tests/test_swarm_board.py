import importlib.util
import pathlib
import shutil
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
