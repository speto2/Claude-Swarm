import os
import pathlib
import shlex
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

    TOOLS = ("bash", "setsid", "sleep", "true", "readlink", "python3")

    def fresh_bin(self):
        """Empty bin dir holding only the tools the script itself needs."""
        shutil.rmtree(self.bin)
        self.bin.mkdir()
        for tool in self.TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def logger(self, name, append=False):
        log = self.tmp / f"{name}.log"
        self.stub(name, f'printf \'%s\\n\' "$@" {">>" if append else ">"} "{log}"\n')
        return log

    def run_stubbed(self, args):
        return subprocess.run([str(self.bin / "bash"), str(SCRIPT), *args],
                              env={"PATH": str(self.bin), "DISPLAY": ":99"},
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
                         ["--maximize", "--title=w1 log", "-x", "python3", target, "arg"])

    def test_no_display_prints_command(self):
        env = {"PATH": "/usr/bin:/bin"}
        r = self.run_script(["t", "python3", f"{self.tmp}/x y.py"], env)
        self.assertEqual(r.returncode, 3)
        self.assertTrue(r.stdout.startswith("Run this to watch: "), r.stdout)
        self.assertIn("x\\ y.py", r.stdout)

    def test_fallback_has_no_trailing_whitespace(self):
        r = self.run_script(["t", "python3", "a b"], {"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 3)
        self.assertEqual(r.stdout, "Run this to watch: python3 a\\ b\n")

    def test_no_terminal_prints_command(self):
        self.fresh_bin()
        r = self.run_stubbed(["t", "python3", "x y.py"])
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(r.stdout, "Run this to watch: python3 x\\ y.py\n")

    def test_missing_command_exits_2_without_opening_a_terminal(self):
        self.fresh_bin()
        log = self.logger("xfce4-terminal", append=True)
        self.logger("xterm", append=True)
        r = self.run_stubbed(["t", "no-such-cmd-xyz", "arg"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("no-such-cmd-xyz", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertFalse(log.exists())
        self.assertFalse((self.tmp / "xterm.log").exists())

    def test_failing_terminal_falls_through_to_next(self):
        self.fresh_bin()
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
        cases = {"xterm": ["-maximized", "-T", "w1 log", "-e"], "lxterminal": ["-T", "w1 log", "-e"],
                 "alacritty": ["--title=w1 log", "-o", 'window.startup_mode="Maximized"', "-e"]}
        for target, flags in cases.items():
            with self.subTest(target):
                self.fresh_bin()
                log = self.tmp / f"{target}.log"
                (alt / target).write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{log}"\n')
                (alt / target).chmod(0o755)
                (self.bin / "x-terminal-emulator").symlink_to(alt / target)
                r = self.run_stubbed(["w1 log", "python3", "x y.py", "arg"])
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(log.read_text().splitlines(), [*flags, "python3", "x y.py", "arg"])

    def test_wrapper_targets_run_the_real_terminal_with_its_own_flags(self):
        alt = self.tmp / "alt"
        alt.mkdir()
        cases = {"gnome-terminal": ["--maximize", "--title=w1 log", "--"],
                 "xfce4-terminal": ["--maximize", "--title=w1 log", "-x"]}
        for real, flags in cases.items():
            with self.subTest(real):
                self.fresh_bin()
                wlog = self.tmp / "wrapper.log"
                wlog.unlink(missing_ok=True)
                (alt / f"{real}.wrapper").write_text(f'#!/bin/sh\necho called > "{wlog}"\n')
                (alt / f"{real}.wrapper").chmod(0o755)
                (self.bin / "x-terminal-emulator").symlink_to(alt / f"{real}.wrapper")
                log = self.logger(real, append=True)
                log.unlink(missing_ok=True)
                self.assertEqual(self.run_stubbed(["w1 log", "python3", "x y.py"]).returncode, 0)
                self.assertFalse(wlog.exists())
                self.assertEqual(log.read_text().splitlines(), [*flags, "python3", "x y.py"])

    def test_wrapper_without_real_terminal_is_skipped(self):
        self.fresh_bin()
        alt = self.tmp / "alt"
        alt.mkdir()
        (alt / "gnome-terminal.wrapper").write_text("#!/bin/sh\nexit 0\n")
        (alt / "gnome-terminal.wrapper").chmod(0o755)
        (self.bin / "x-terminal-emulator").symlink_to(alt / "gnome-terminal.wrapper")
        self.assertEqual(self.run_stubbed(["t", "python3", "x"]).returncode, 3)

    def test_wrapper_real_terminal_is_not_retried_after_failing(self):
        self.fresh_bin()
        alt = self.tmp / "alt"
        alt.mkdir()
        (alt / "xfce4-terminal.wrapper").write_text("#!/bin/sh\nexit 0\n")
        (alt / "xfce4-terminal.wrapper").chmod(0o755)
        (self.bin / "x-terminal-emulator").symlink_to(alt / "xfce4-terminal.wrapper")
        log = self.tmp / "count.log"
        self.stub("xfce4-terminal", f'echo run >> "{log}"\nexit 1\n')
        self.assertEqual(self.run_stubbed(["t", "python3", "x"]).returncode, 3)
        self.assertEqual(log.read_text().splitlines(), ["run"])

    CASES = {
        "xfce4-terminal": ["--maximize", "--title=w1 log", "-x"],
        "xterm": ["-maximized", "-T", "w1 log", "-e"],
        "gnome-terminal": ["--maximize", "--title=w1 log", "--"],
        "ptyxis": ["--maximize", "--title=w1 log", "--"],
        "kgx": ["--title=w1 log", "--"],
        "konsole": ["-p", "tabtitle=w1 log", "-e"],
        "mate-terminal": ["--maximize", "--title=w1 log", "-x"],
        "kitty": ["--start-as=maximized", "--title=w1 log", "--"],
        "alacritty": ["--title=w1 log", "-o", 'window.startup_mode="Maximized"', "-e"],
        "foot": ["--maximized", "--title=w1 log", "--"],
        "wezterm": ["start", "--"],
        "ghostty": ["--maximize=true", "--title=w1 log", "-e"],
    }

    def test_terminal_flags(self):
        for name, flags in self.CASES.items():
            with self.subTest(name):
                self.fresh_bin()
                log = self.logger(name)
                r = self.run_stubbed(["w1 log", "python3", "x y.py", "arg"])
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(log.read_text().splitlines(), [*flags, "python3", "x y.py", "arg"])

    def test_tilix_gets_one_shell_quoted_command_string(self):
        self.fresh_bin()
        log = self.logger("tilix")
        r = self.run_stubbed(["w1 log", "python3", "x y.py", "it's", "$HOME"])
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = log.read_text().splitlines()
        self.assertEqual(lines[:3], ["--maximize", "--title=w1 log", "-e"])
        self.assertEqual(shlex.split(lines[3]), ["python3", "x y.py", "it's", "$HOME"])
        self.assertEqual(len(lines), 4)

    def test_terminal_order(self):
        order = ["xfce4-terminal", "ptyxis", "gnome-terminal", "kgx", "konsole", "mate-terminal", "tilix",
                 "kitty", "alacritty", "foot", "wezterm", "ghostty", "xterm"]
        self.fresh_bin()
        calls = self.tmp / "order.log"
        for name in order:
            self.stub(name, f'echo {name} >> "{calls}"\nexit 1\n')
        self.assertEqual(self.run_stubbed(["t", "python3", "x"]).returncode, 3)
        self.assertEqual(calls.read_text().split(), order)

    def test_title_cannot_inject_flags_or_properties(self):
        self.fresh_bin()
        log = self.logger("konsole")
        r = self.run_stubbed(["a;b=c\td\x01-e", "python3", "x"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text().splitlines()[:2], ["-p", "tabtitle=ab=cd-e"])
        for name in ("xfce4-terminal", "gnome-terminal", "kitty", "ptyxis", "kgx", "ghostty", "mate-terminal"):
            with self.subTest(name):
                self.fresh_bin()
                log = self.logger(name)
                self.assertEqual(self.run_stubbed(["--version", "python3", "x"]).returncode, 0)
                self.assertIn("--title=--version", log.read_text().splitlines())
                self.assertNotIn("--version", log.read_text().splitlines())

    def test_setsid_without_dash_w_still_launches(self):
        self.fresh_bin()
        (self.bin / "setsid").unlink()
        self.stub("setsid", '[ "$1" = -w ] && { echo "invalid option -- w" >&2; exit 1; }\nexec "$@"\n')
        log = self.logger("xfce4-terminal")
        r = self.run_stubbed(["w1 log", "python3", "x y.py"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text().splitlines(), ["--maximize", "--title=w1 log", "-x", "python3", "x y.py"])

    def test_no_setsid_falls_back_to_nohup(self):
        self.fresh_bin()
        (self.bin / "setsid").unlink()
        (self.bin / "nohup").symlink_to(shutil.which("nohup"))
        log = self.logger("xfce4-terminal")
        r = self.run_stubbed(["w1 log", "python3", "x y.py"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text().splitlines(), ["--maximize", "--title=w1 log", "-x", "python3", "x y.py"])

    def test_all_terminals_failing_prints_command(self):
        self.fresh_bin()
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
