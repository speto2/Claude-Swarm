import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTS = ROOT / "agents"
SKILL_DIR = ROOT / "skills" / "swarm"
HEADER = "| id | title | owner | status | files | check | depends | fixes |"
MODELS = {"haiku", "sonnet", "opus", "fable", "inherit"}


def read(path):
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def split(path):
    """Return (frontmatter lines, body text) of an agent file."""
    text = read(AGENTS / path)
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("no frontmatter in %s" % path)
    front, body = text[4:].split("\n---\n", 1)
    return front.splitlines(), body


class AgentsTest(unittest.TestCase):
    def test_tools_lines(self):
        expect = {
            "swarm-worker.md": "tools: Read, Edit, Write, Bash, Grep, Glob, SendMessage",
            "swarm-worker-web.md": "tools: Read, Edit, Write, Bash, Grep, Glob, SendMessage, WebFetch, WebSearch",
        }
        for name, line in expect.items():
            front, _ = split(name)
            tools = [l for l in front if l.startswith("tools:")]
            self.assertEqual(tools, [line], name)

    def test_same_body(self):
        a, b = split("swarm-worker.md")[1], split("swarm-worker-web.md")[1]
        self.assertTrue(a.strip())
        self.assertEqual(a, b)

    def test_frontmatter_keys_and_name(self):
        for name in ("swarm-worker", "swarm-worker-web"):
            front, _ = split(name + ".md")
            keys = [l.split(":", 1)[0] for l in front]
            self.assertEqual(keys, ["name", "description", "tools", "model"], name)
            self.assertEqual(front[0], "name: " + name)
            fm = dict(l.split(":", 1) for l in front)
            self.assertTrue(fm["description"].strip(), name + " description empty")
            self.assertIn(fm["model"].strip(), MODELS, name)


class SkillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(SKILL_DIR / "SKILL.md")
        if not cls.text.startswith("---\n"):
            raise ValueError("SKILL.md has no frontmatter")
        parts = cls.text.split("\n---\n", 1)
        cls.front = parts[0].splitlines()[1:]
        cls.body = parts[1]

    def test_frontmatter(self):
        self.assertIn("name: swarm", self.front)
        self.assertIn("disable-model-invocation: true", self.front)

    def test_scripts_exist(self):
        # body refers to scripts as "$B/<name>" (B=<base>/scripts) or "scripts/<name>"
        names = set(re.findall(r"(?:\$B|scripts)/([\w.-]+\.(?:py|sh))", self.body))
        self.assertTrue({"swarm-run.py", "swarm-dash.py", "open-window.sh"} <= names, names)
        for n in names:
            self.assertTrue((SKILL_DIR / "scripts" / n).is_file(), n)

    def test_agent_tiers_exist(self):
        # the planner prompt holds the tier list, the body names the tiers it spawns
        tiers = set(re.findall(r"`(swarm-worker[\w-]*)`", self.body + read(SKILL_DIR / "planner.md")))
        self.assertTrue({"swarm-worker", "swarm-worker-web"} <= tiers, tiers)
        for t in tiers:
            self.assertTrue((AGENTS / (t + ".md")).is_file(), t)

    def test_planner_brief(self):
        self.assertIn("<base>/planner.md", self.body)
        text = read(SKILL_DIR / "planner.md")
        for s in ("TASKS", "WORKERS", "HUB", "NOTES", HEADER):
            self.assertIn(s, text)
        self.assertIn(HEADER, self.body)  # section 5's init example uses the same header

    def test_planner_safeguards(self):
        text = read(SKILL_DIR / "planner.md")
        self.assertIn("`WAITING`", text)  # a subagent that ends its turn has handed back
        self.assertIn("Read-only", text)  # planner edits would land before the snapshot, unreviewed
        plan = self.body.split("## 2. Plan", 1)[1].split("## 3.", 1)[0]
        for s in ("`WAITING`", "`continue`", "header row", "its `owner`", "beyond its task's files", "overrides",
                  "git top level", "every WORKERS tier",
                  "as a message"):  # after `continue` the plan comes by SendMessage: one hand-back per agent
            self.assertIn(s, plan)
        preview = self.body.split("## 4. Preview", 1)[1].split("## 5.", 1)[0]
        self.assertIn("`--keep` yourself", preview)  # the planner can't act on run settings

    def test_usage_block_matches_readme(self):
        def usage(text):
            m = re.search(r"^Usage: /swarm.*?\n(?=```)", text, re.M | re.S)
            self.assertIsNotNone(m)
            return m.group(0)
        a, b = usage(self.text), usage(read(ROOT / "README.md"))
        self.assertIn("Examples:", a)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
