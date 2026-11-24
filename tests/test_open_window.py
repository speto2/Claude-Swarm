import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "skills" / "swarm" / "scripts" / "open-window.sh"
USAGE = "usage: open-window.sh <title> <command> [args...]"


class OpenWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()) / "with space "
        self.bin = self.tmp / "bin"
        self.bin.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp.parent)

    def run_script(self, args, env):
        return subprocess.run(["bash", str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_launches_first_terminal_with_args_intact(self):
        log = self.tmp / "stub.log"
        stub = self.bin / "xfce4-terminal"
        stub.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$STUB_LOG"\n')
        stub.chmod(0o755)
        target = f"{self.tmp}/x y.py"
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "DISPLAY": ":99", "STUB_LOG": str(log)}
        start = time.monotonic()
        r = self.run_script(["w1 log", "python3", target, "arg"], env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - start, 2)
        deadline = time.monotonic() + 3
        while not log.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.1)  # let the stub finish writing
        self.assertEqual(log.read_text().splitlines(),
                         ["--maximize", "--title", "w1 log", "-x", "python3", target, "arg"])

    def test_no_display_prints_command(self):
        env = {"PATH": "/usr/bin:/bin"}
        r = self.run_script(["t", "python3", f"{self.tmp}/x y.py"], env)
        self.assertEqual(r.returncode, 3)
        self.assertTrue(r.stdout.startswith("Run this to watch: "), r.stdout)
        self.assertIn("x\\ y.py", r.stdout)

    def test_no_terminal_prints_command(self):
        for tool in ("bash", "setsid"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        env = {"PATH": str(self.bin), "DISPLAY": ":99"}
        r = subprocess.run([str(self.bin / "bash"), str(SCRIPT), "t", "python3", "x y.py"],
                           env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertTrue(r.stdout.startswith("Run this to watch: "), r.stdout)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def run_stubbed(self, args):
        for tool in ("bash", "setsid", "sleep"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        return subprocess.run([str(self.bin / "bash"), str(SCRIPT), *args],
                              env={"PATH": str(self.bin), "DISPLAY": ":99"},
                              capture_output=True, text=True, timeout=10)

    def test_failing_terminal_falls_through_to_next(self):
        log = self.tmp / "xterm.log"
        self.stub("xfce4-terminal", "exit 1\n")
        self.stub("xterm", f'printf \'%s\\n\' "$@" > "{log}"\n')
        r = self.run_stubbed(["w1 log", "python3", "x y.py", "arg"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text().splitlines(),
                         ["-maximized", "-T", "w1 log", "-e", "python3", "x y.py", "arg"])

    def test_x_terminal_emulator_args_follow_its_target(self):
        alt = self.tmp / "alt"
        alt.mkdir()
        cases = {"xterm": ["-maximized", "-T", "w1 log", "-e"], "lxterminal": ["-T", "w1 log", "-e"]}
        for target, flags in cases.items():
            with self.subTest(target):
                for tool in ("x-terminal-emulator", "readlink", "bash", "setsid", "sleep"):
                    (self.bin / tool).unlink(missing_ok=True)
                log = self.tmp / f"{target}.log"
                (alt / target).write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{log}"\n')
                (alt / target).chmod(0o755)
                (self.bin / "x-terminal-emulator").symlink_to(alt / target)
                (self.bin / "readlink").symlink_to(shutil.which("readlink"))
                r = self.run_stubbed(["w1 log", "python3", "x y.py", "arg"])
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(log.read_text().splitlines(), [*flags, "python3", "x y.py", "arg"])

    def test_maximize_flags(self):
        cases = {
            "gnome-terminal": ["--maximize", "--title", "w1 log", "--"],
            "kitty": ["--start-as=maximized", "--title", "w1 log"],
        }
        for name, flags in cases.items():
            with self.subTest(name):
                log = self.tmp / f"{name}.log"
                self.stub(name, f'printf \'%s\\n\' "$@" > "{log}"\n')
                r = self.run_stubbed(["w1 log", "python3", "x y.py", "arg"])
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(log.read_text().splitlines(), [*flags, "python3", "x y.py", "arg"])
                (self.bin / name).unlink()
                for tool in ("bash", "setsid", "sleep"):
                    (self.bin / tool).unlink()

    def test_all_terminals_failing_prints_command(self):
        self.stub("xfce4-terminal", "exit 1\n")
        self.stub("xterm", "exit 1\n")
        r = self.run_stubbed(["t", "python3", "x y.py"])
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertTrue(r.stdout.startswith("Run this to watch: "), r.stdout)

    def test_usage_error(self):
        r = self.run_script(["only-title"], {"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 2)
        self.assertIn(USAGE, r.stderr)


if __name__ == "__main__":
    unittest.main()
