#!/usr/bin/env python3
"""Live, readable view of one /swarm worker's log.

usage: swarm-watch.py <output-file> <name> [<run-folder> <owner-pid>]
Exits when <run-folder>/report.md appears or <owner-pid> is gone, so its window closes.
"""
import json
import os
import re
import sys
import time

MAX_LINE = 200
CLOSE_DELAY = 5  # seconds the last lines stay up before the window closes
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # log text is untrusted: no escape sequences to the terminal
AGENT_MSG = re.compile(r'<agent-message from="([^"]*)">\s*(.*?)\s*</agent-message>', re.S)


def clip(line):
    line = CTRL.sub("", line)
    return line if len(line) <= MAX_LINE else line[:MAX_LINE - 1] + "…"


def first_line(text):
    return next((l.strip() for l in str(text).splitlines() if l.strip()), "")


def head(text):
    """First non-empty line, plus the next one when the first only introduces it."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) > 1 and lines[0].endswith(":"):
        return f"{lines[0]} {lines[1]}"
    return lines[0] if lines else ""


def summarize(name, inp):
    if not isinstance(inp, dict):
        return ""
    if name == "SendMessage":
        return f"to {inp.get('to')}: {first_line(inp.get('message', ''))}"
    for key in ("command", "file_path", "pattern"):
        if key in inp:
            return first_line(inp[key])
    return json.dumps(inp, separators=(",", ":"))


def _lines(rec):
    kind = rec.get("type")
    if kind == "attachment":
        att = rec.get("attachment") or {}
        if att.get("type") != "queued_command":
            return []
        prompt = str(att.get("prompt", ""))
        m = AGENT_MSG.search(prompt)
        return [f"MSG from {m[1]}: {first_line(m[2])}"] if m else [f"IN: {head(prompt)}"]
    content = (rec.get("message") or {}).get("content")
    out = []
    if kind == "user":
        if isinstance(content, str):
            return [] if content.startswith("<") else [f"IN: {head(content)}"]
        for item in content or []:
            if item.get("type") != "tool_result":
                continue
            body = item.get("content")
            if isinstance(body, list):
                body = "\n".join(x.get("text", "") for x in body if isinstance(x, dict))
            out.append(f"   {'!!' if item.get('is_error') else '<-'} {first_line(body or '')}")
    elif kind == "assistant":
        for item in content or []:
            if item.get("type") == "tool_use":
                name = item.get("name")
                sep = " " if name == "SendMessage" else ": "
                out.append(f"-> {name}{sep}{summarize(name, item.get('input'))}")
            elif item.get("type") == "text":
                out += [l.strip() for l in item.get("text", "").splitlines() if l.strip()]
    return out


def format_record(rec):
    """Readable lines for one log record; [] for records not worth showing."""
    try:
        return [clip(l) for l in _lines(rec)]
    except (AttributeError, TypeError, KeyError):
        return []  # log format drift: skip the record rather than crash the window


class LogReader:
    """Reads whole new JSON lines from a file that may not exist yet or may be mid-write."""

    def __init__(self, path):
        self.path = path
        self.pos = 0
        self.buf = b""

    def read_new(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        self.pos += len(data)
        *lines, self.buf = (self.buf + data).split(b"\n")
        out = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out


def read_meta(log_path):
    meta = os.path.splitext(os.path.realpath(log_path))[0] + ".meta.json"
    try:
        with open(meta) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def header(name, meta):
    fields = " ".join(f"{k}={meta[k]}" for k in ("model", "effort") if meta.get(k))
    return clip(f"== {name}{' | ' + fields if fields else ''} ==")


def run_over(run_dir, pid):
    """True once the overseer wrote report.md or the process that owns the workers is gone."""
    if run_dir and os.path.exists(os.path.join(run_dir, "report.md")):
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
        if len(argv) not in (2, 4):
            raise ValueError
        path, name = argv[:2]
        run_dir, pid = (argv[2], int(argv[3])) if len(argv) == 4 else (None, None)
    except ValueError:
        print("usage: swarm-watch.py <output-file> <name> [<run-folder> <owner-pid>]", file=sys.stderr)
        return 2
    print(header(name, read_meta(path)), flush=True)
    reader = LogReader(path)
    announced = False
    try:
        while True:
            recs = reader.read_new()
            for rec in recs:
                for line in format_record(rec):
                    print(line, flush=True)
            if recs:
                announced = False  # a resumed worker keeps writing the same file
            elif not announced and read_meta(path).get("finished"):
                print(f"-- {name} finished; window closes when the run ends --", flush=True)
                announced = True
            if run_over(run_dir, pid):
                print(f"-- run over; closing in {CLOSE_DELAY} s --", flush=True)
                time.sleep(CLOSE_DELAY)
                return 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
