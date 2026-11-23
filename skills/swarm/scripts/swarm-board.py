#!/usr/bin/env python3
"""Live board for a /swarm run: the task table and each worker's status line.

usage: swarm-board.py <run-folder>
"""
import os
import re
import sys
import time

CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # worker-written text is untrusted: no escape sequences to the terminal


def worker_status(path):
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if line.strip():
                    text = line.strip()
                    return text[len("status:"):].strip() if text.startswith("status:") else text
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


def main(argv):
    if len(argv) != 1:
        print("usage: swarm-board.py <run-folder>", file=sys.stderr)
        return 2
    try:
        while True:
            print("\033[2J\033[H" + render(argv[0]), flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
