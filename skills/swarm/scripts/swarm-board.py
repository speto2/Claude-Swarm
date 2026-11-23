#!/usr/bin/env python3
"""Live board for a /swarm run: the task table and each worker's status line.

usage: swarm-board.py <run-folder> [<owner-pid>]
Exits when <run-folder>/report.md appears or <owner-pid> is gone, so its window closes.
"""
import os
import re
import stat
import sys
import time

MAX_LINE = 200  # same cap as swarm-watch.py
CLOSE_DELAY = 5  # seconds the last view stays up before the window closes
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # worker-written text is untrusted: no escape sequences to the terminal


def worker_status(path):
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):  # symlink could leak a secret; a FIFO would block open() forever
            return "(not a regular file)"
        with open(path, errors="replace") as f:
            for line in f:
                if line.strip():
                    text = line.strip()
                    text = CTRL.sub("", text[len("status:"):].strip() if text.startswith("status:") else text)
                    return text if len(text) <= MAX_LINE else text[:MAX_LINE - 1] + "…"
    except OSError:
        pass
    return "(no status yet)"


def render(run_dir):
    parts = [f"swarm board: {run_dir}", ""]
    try:
        with open(os.path.join(run_dir, "tasks.md"), errors="replace") as f:
            parts.append(f.read())
    except OSError:
        parts.append("waiting for tasks.md")
    parts += ["", "workers:"]
    wdir = os.path.join(run_dir, "workers")
    try:
        names = sorted(n for n in os.listdir(wdir) if n.endswith(".md"))
    except OSError:
        names = []
    for n in names:
        parts.append(f"{n[:-3]}: {worker_status(os.path.join(wdir, n))}")
    if not names:
        parts.append("(no workers yet)")
    return CTRL.sub("", "\n".join(parts))


def run_over(run_dir, pid):
    """True once the overseer wrote report.md or the process that owns the workers is gone."""
    if os.path.exists(os.path.join(run_dir, "report.md")):
        return True
    if pid:
        try:
            os.kill(pid, 0)  # shortcut: a reused PID keeps the window open; close it by hand then
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
    return False


def main(argv):
    try:
        if len(argv) not in (1, 2):
            raise ValueError
        run_dir, pid = argv[0], int(argv[1]) if len(argv) == 2 else None
    except ValueError:
        print("usage: swarm-board.py <run-folder> [<owner-pid>]", file=sys.stderr)
        return 2
    try:
        while True:
            print("\033[2J\033[H" + render(run_dir), flush=True)
            if run_over(run_dir, pid):
                print(f"\n-- run over; closing in {CLOSE_DELAY} s --", flush=True)
                time.sleep(CLOSE_DELAY)
                return 0
            time.sleep(2)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
