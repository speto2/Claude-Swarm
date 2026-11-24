import pathlib
import unittest

AGENTS = pathlib.Path(__file__).resolve().parent.parent / "agents"


def split(path):
    """Return (frontmatter lines, body text) of an agent file."""
    text = (AGENTS / path).read_text()
    assert text.startswith("---\n"), path
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


if __name__ == "__main__":
    unittest.main()
