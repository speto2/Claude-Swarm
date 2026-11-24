#!/usr/bin/env python3
"""Live dashboard for a /swarm run: the task strip and every worker in one window.

usage: swarm-dash.py <run-folder> [<owner-pid>]
Exits when <run-folder>/report.md appears or <owner-pid> is gone, so its window closes.
"""
import curses
import errno
import itertools
import json
import math
import os
import re
import stat
import sys
import time
import unicodedata

MAX_LINE = 200
MAX_LOG_LINES = 100_000  # formatted lines kept per worker; the oldest are dropped past this
MAX_READ = 1 << 20  # bytes read from a log per poll; also the longest partial line kept
CLOSE_DELAY = 5  # seconds the last view stays up before the window closes
STALE_AFTER = 120  # seconds without log growth before a running worker is flagged quiet
MIN_BOX_W, MIN_BOX_H = 30, 4  # smaller boxes collapse to one line per worker
MIN_W, MIN_H = 40, 10  # smaller windows only ask to be enlarged
CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")  # worker-written text is untrusted: no escape sequences to the terminal
AGENT_MSG = re.compile(r'<agent-message from="([^"]*)">\s*(.*?)\s*</agent-message>', re.S)
CELL_SPLIT = re.compile(r"(?<!\\)\|")


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
        return f"to {inp.get('to') or '?'}: {first_line(inp.get('message', ''))}"
    for key in ("command", "file_path", "pattern", "url", "query"):
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
        for item in [{"type": "text", "text": content}] if isinstance(content, str) else content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text = str(item.get("text", ""))
                m = AGENT_MSG.search(text)
                if m:
                    out.append(f"MSG from {m[1]}: {first_line(m[2])}")
                elif not text.startswith("<"):
                    out.append(f"IN: {head(text)}")
                continue
            if item.get("type") != "tool_result":
                continue
            body = item.get("content")
            if isinstance(body, list):
                body = "\n".join(x.get("text", "") for x in body if isinstance(x, dict))
            out.append(f"   {'!!' if item.get('is_error') else '<-'} {first_line(body or '')}")
    elif kind == "assistant":
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
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
        self.ident = None  # (st_dev, st_ino) of the file the offset belongs to
        self.tail = b""  # last bytes read, re-read each poll: a mismatch means the file was cut or rewritten
        self.truncated = False  # set by read_new when the file shrank or was rewritten and reading restarted

    def read_new(self):
        self.truncated = False
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)  # roster paths are untrusted: a FIFO would block open() forever
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode):  # FIFO or device: never a worker log
                    return []
                ident = (st.st_dev, st.st_ino)
                if self.ident is not None and (ident != self.ident or st.st_size < self.pos):  # replaced or truncated: start over
                    self.pos, self.buf, self.tail, self.truncated = 0, b"", b"", True
                elif self.tail:
                    f.seek(self.pos - len(self.tail))
                    if f.read(len(self.tail)) != self.tail:  # rewritten in place: start over
                        self.pos, self.buf, self.tail, self.truncated = 0, b"", b"", True
                self.ident = ident
                f.seek(self.pos)
                data = f.read(MAX_READ)
        except OSError:
            return []
        self.pos += len(data)
        self.tail = (self.tail + data)[-256:]
        *lines, self.buf = (self.buf + data).split(b"\n")
        if len(self.buf) > MAX_READ:  # an endless line: drop what we have, its tail fails to parse and is skipped
            self.buf = b""
        out = []
        for line in lines:
            try:
                rec = json.loads(line)
            except (ValueError, RecursionError):  # RecursionError: absurdly nested line
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out


def read_small(path, limit):
    """(first `limit` bytes, mtime) of a regular file opened without following a symlink or blocking; None for anything else.
    Raises OSError if it cannot be opened (a roster path is untrusted: a symlink could leak a secret, a FIFO would block open())."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            return None
        raise
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        return (f.read(limit), st.st_mtime) if stat.S_ISREG(st.st_mode) else None


def worker_status(path):
    try:
        got = read_small(path, 8192)
        if got is None:
            return "(not a regular file)"
        for line in got[0].decode(errors="replace").splitlines():
            if line.strip():
                text = line.strip()
                text = CTRL.sub("", text[len("status:"):].strip() if text.startswith("status:") else text)
                return text if len(text) <= MAX_LINE else text[:MAX_LINE - 1] + "…"
    except OSError:
        pass
    return "(no status yet)"


def activity(log, now=None):
    """' (finished)' or ' (quiet Nm)' for a worker that stopped writing its log; '' while it is active or unknown.
    `now` is wall-clock time: it is compared with file mtimes."""
    if not log:
        return ""
    try:
        real = os.path.realpath(log)
        log_mtime = os.stat(real).st_mtime
    except OSError:
        return ""
    idle = (time.time() if now is None else now) - log_mtime
    try:
        got = read_small(os.path.splitext(real)[0] + ".meta.json", 65536)
        # a worker resumed after it finished writes its log again: the older "finished" no longer holds
        if got and json.loads(got[0]).get("finished") and (got[1] >= log_mtime or idle >= 5):
            return " (finished)"
    except (OSError, ValueError, AttributeError):
        pass
    return f" (quiet {int(idle // 60)}m)" if idle >= STALE_AFTER else ""


def run_over(run_dir, pid):
    """True once the overseer wrote report.md or the process that owns the workers is gone."""
    if run_dir and os.path.exists(os.path.join(run_dir, "report.md")):
        return True
    if pid and pid > 0:
        try:
            os.kill(pid, 0)  # shortcut: a reused PID keeps the window open; close it by hand then
        except ProcessLookupError:
            return True
        except (PermissionError, OverflowError):  # OverflowError: pid too large for the OS
            pass
    return False


def parse_table(text):
    """Header names (lower case) and rows (lists of cells) of the first markdown table in text. A `\\|` inside a cell is not a separator."""
    header, rows = None, []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if header is not None:
                break  # the first table has ended; a later table is not merged into it
            continue
        line = line[1:]  # at most one edge pipe on each side, so empty edge cells survive
        if line.endswith("|") and not line.endswith("\\|"):
            line = line[:-1]
        cells = [c.strip().replace("\\|", "|") for c in CELL_SPLIT.split(line)]
        if header is None:
            header = [c.lower() for c in cells]
        elif not all(set(c) <= set("-: ") for c in cells):
            rows.append(cells)
    return header or [], rows


def _table(run_dir, name, columns):
    """(rows as dicts of `columns` (missing column -> ""), header names); ([], []) if the file is missing."""
    try:
        with open(os.path.join(run_dir, name), errors="replace") as f:
            header, rows = parse_table(f.read())
    except OSError:
        return [], []
    return [{k: (r[header.index(c)] if c in header and header.index(c) < len(r) else "") for k, c in columns.items()}
            for r in rows], header


def read_tasks(run_dir):
    return _table(run_dir, "tasks.md", {k: k for k in ("id", "title", "owner", "status", "fixes")})[0]


def read_roster(run_dir):
    """Workers in roster.md file order; empty until the overseer writes it."""
    rows, header = _table(run_dir, "roster.md", {"name": "worker", "model": "model", "effort": "effort", "log": "output file"})
    if not {"worker", "model", "effort", "output file"} <= set(header):
        return []
    rows = [dict(r, **{k: CTRL.sub("", r[k]).strip() for k in ("name", "model", "effort")}, log=r["log"].strip("` \t")) for r in rows]
    return [r for r in rows if r["name"]]


class Worker:
    def __init__(self, name, model="", effort="", log=""):
        self.name, self.model, self.effort, self.log = name, model, effort, log
        self.lines = []  # formatted log, at most MAX_LOG_LINES
        self.dropped = 0  # lines the last poll_log dropped off the front
        self.status = "(no status yet)"
        self.flag = ""
        self._reader = None

    def poll_log(self):
        """Append newly written, formatted log lines; True if there were any."""
        self.dropped = 0
        if not self.log:
            return False
        if self._reader is None or self._reader.path != self.log:
            self._reader = LogReader(self.log)
        records = self._reader.read_new()
        if self._reader.truncated:  # the log restarted: every stored line is gone
            self.dropped = len(self.lines)
            self.lines.clear()
        new = [line for rec in records for line in format_record(rec)]
        self.lines.extend(new)
        cut = max(0, len(self.lines) - MAX_LOG_LINES)
        del self.lines[:cut]
        self.dropped += cut
        return bool(new)


class Dash:
    def __init__(self, run_dir, pid):
        self.run_dir, self.pid = run_dir, pid
        self.tasks, self.workers = [], {}
        self.selected = self.zoom = self.top = self.closing_at = None  # top: zoom scroll, None = follow the newest line
        self._order = []

    def names(self):
        """Roster order, then workers known only from a status file, by name."""
        extra = sorted(n for n in self.workers if n not in self._order)
        return [n for n in self._order if n in self.workers] + extra

    def refresh_meta(self, now=None):  # now is unused: activity() compares wall-clock time with file mtimes
        self.tasks = read_tasks(self.run_dir)
        roster = read_roster(self.run_dir)
        self._order = list(dict.fromkeys(r["name"] for r in roster))
        for r in roster:  # new workers only: log offsets and stored lines survive refreshes
            w = self.workers.setdefault(r["name"], Worker(r["name"]))
            w.model, w.effort, w.log = r["model"], r["effort"], r["log"]
        wdir = os.path.join(self.run_dir, "workers")
        try:
            files = {n[:-3] for n in os.listdir(wdir) if n.endswith(".md")}
        except OSError:
            files = set()
        for n in files:
            self.workers.setdefault(n, Worker(n))
        for n, w in self.workers.items():  # only names listed in workers/ are opened: a roster name must not point elsewhere
            w.status = worker_status(os.path.join(wdir, n + ".md")) if n in files else "(no status yet)"
            w.flag = activity(w.log)
        if self.selected is None and self.workers:
            self.selected = self.names()[0]

    def poll_logs(self):
        for w in self.workers.values():
            w.poll_log()
            if w.dropped and w.name == self.zoom and self.top is not None:  # keep the zoomed view on the same lines
                self.top = max(0, self.top - w.dropped)

    def tick(self, now):
        """False once the run is over and the closing delay has passed."""
        if self.closing_at is None and run_over(self.run_dir, self.pid):
            self.closing_at = now + CLOSE_DELAY
        return self.closing_at is None or now < self.closing_at


def grid_shape(n, height, width):
    """(cols, rows) for n boxes, or None when no shape gives boxes of at least MIN_BOX_W x MIN_BOX_H."""
    best, best_score = None, 0
    for cols in range(1, n + 1):
        box_w, box_h = width // cols, height // math.ceil(n / cols)
        if box_w >= MIN_BOX_W and box_h >= MIN_BOX_H and min(box_w, 3 * box_h) > best_score:  # strict: ties keep fewer columns
            best, best_score = (cols, math.ceil(n / cols)), min(box_w, 3 * box_h)
    return best


def strip_lines(tasks, width, max_rows):
    """Task strip rows: one column if it fits, else columns, else a last `… N more` row."""
    if max_rows < 1 or width < 1:
        return []
    items = [CTRL.sub("", f"{t['id']} {t['owner']} {t['status']} f{t['fixes']}  {t['title']}") for t in tasks]
    n = len(items)
    if n <= max_rows:
        return [fit(i, width) for i in items]
    cols = max(1, min(math.ceil(n / max_rows), width // MIN_BOX_W))
    shown = n if cols * max_rows >= n else cols * (max_rows - 1)
    rows = math.ceil(shown / cols) if shown else 0
    cw = width // cols - 1  # one blank column between strip columns
    out = [" ".join(fit(items[c * rows + r], cw, pad=True) for c in range(cols) if c * rows + r < shown).rstrip()[:width]
           for r in range(rows)]
    return out + ([f"… {n - shown} more"[:width]] if shown < n else [])


def move(names, selected, key, cols):
    """The selected name after `key`, in a grid of `cols` columns (no wrap except Tab)."""
    if not names:
        return None
    if selected not in names:
        return names[0]
    i, n, cols = names.index(selected), len(names), max(1, cols)
    if key == "tab":
        i = (i + 1) % n
    elif key in list("123456789"):
        i = int(key) - 1 if int(key) <= n else i
    elif key == "left" and i % cols:
        i -= 1
    elif key == "right" and i % cols < cols - 1 and i + 1 < n:
        i += 1
    elif key == "up" and i >= cols:
        i -= cols
    elif key == "down" and i + cols < n:
        i += cols
    return names[i]


def list_window(n, index, height):
    """(start, end) of the slice of an n-line list that fits `height` lines and contains `index`."""
    height = max(0, height)
    start = max(0, min(index - height // 2, n - height))
    return start, min(n, start + height)


def scroll(top, key, total, height):
    """New zoom scroll position; None follows the newest line."""
    bottom = max(0, total - height)
    cur = bottom if top is None else top
    new = {"up": cur - 1 if top is not None else max(0, total - height - 1), "down": cur + 1, "pgup": cur - height,
           "pgdn": cur + height, "home": 0, "end": bottom}.get(key, cur)
    new = max(0, new)
    return None if key == "end" or new >= total - height else new


def visible(lines, height, top):
    """The last `height` lines (top None) or the `height` lines from index `top`."""
    if height < 1:
        return []
    if top is None:
        return list(itertools.islice(reversed(lines), height))[::-1]
    return list(itertools.islice(lines, top, top + height))


KEYS = {curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right", curses.KEY_UP: "up", curses.KEY_DOWN: "down", 9: "tab",
        10: "enter", 13: "enter", curses.KEY_ENTER: "enter", 27: "esc", ord("q"): "q", curses.KEY_PPAGE: "pgup",
        curses.KEY_NPAGE: "pgdn", curses.KEY_HOME: "home", curses.KEY_END: "end", curses.KEY_RESIZE: "resize"}
KEYS.update({ord(c): c for c in "123456789"})


def key_name(ch):
    return KEYS.get(ch)


def fit(text, width, pad=False):
    """`text` cut to `width` terminal cells (wide characters count 2), optionally padded with spaces."""
    out, used = [], 0
    for ch in text:
        zero = unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf")
        if zero and used == 0:
            continue  # a mark with nothing before it would attach to the neighbouring cell
        cw = 0 if zero else 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + cw > width:
            break
        out.append(ch)
        used += cw
    return "".join(out) + " " * (width - used if pad else 0)


def put(scr, y, x, text, attr=0, maxw=None):
    """Write clipped text; never raises (the bottom-right cell makes addstr raise)."""
    h, w = scr.getmaxyx()
    width = min(w - x, maxw if maxw is not None else w)
    if not (0 <= y < h and 0 <= x < w and width > 0):
        return
    text = CTRL.sub("", text).replace("\t", " ").replace("\n", " ")
    try:
        scr.addstr(y, x, fit(text, width, pad=bool(attr & curses.A_REVERSE)), attr)
    except curses.error:
        pass


def closing_text(dash, now):
    return f"run over; closing in {max(0, math.ceil(dash.closing_at - now))} s" if dash.closing_at is not None else ""


def title(w):
    return w.name + (" | " + " ".join(p for p in (w.model, w.effort) if p) if w.model or w.effort else "")


def layout(dash, height, width):
    """(strip rows, first body row, body height, grid shape or None) for the grid view."""
    closing = dash.closing_at is not None
    strip = strip_lines(dash.tasks, width, max(1, height // 3) - closing)
    top = len(strip) + closing + 1 if strip or closing else 0  # no separator line under an empty strip
    # width + 1: each box is drawn one cell narrower than its slot, so the last box needs no blank column after it
    return strip, top, height - top, grid_shape(len(dash.names()), height - top, width + 1)


def draw(stdscr, dash, now):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < MIN_H or w < MIN_W:
        put(stdscr, 0, 0, "enlarge the window")
    elif dash.zoom in dash.workers:
        wk = dash.workers[dash.zoom]
        head = " | ".join(p for p in (title(wk), wk.status + wk.flag) if p) + ("  " + closing_text(dash, now)).rstrip()
        put(stdscr, 0, 0, head, curses.A_REVERSE)
        for i, line in enumerate(visible(wk.lines, h - 1, dash.top)):
            put(stdscr, 1 + i, 0, line)
    else:
        strip, top, body, shape = layout(dash, h, w)
        closing = [closing_text(dash, now)] if dash.closing_at is not None else []
        for i, line in enumerate(closing + strip):
            put(stdscr, i, 0, line, curses.A_BOLD if closing and i == 0 else 0)
        if top:
            put(stdscr, top - 1, 0, "-" * w)
        names = dash.names()
        if not names:
            put(stdscr, top, 0, "(no workers yet)")
        elif shape is None:
            start, end = list_window(len(names), names.index(dash.selected) if dash.selected in names else 0, body)
            for i, n in enumerate(names[start:end]):
                wk = dash.workers[n]
                put(stdscr, top + i, 0, f"{n}: {wk.status}{wk.flag}", curses.A_REVERSE if n == dash.selected else 0)
        else:
            cols, rows = shape
            bw, bh = (w + 1) // cols, body // rows
            for i, n in enumerate(names):
                wk, y, x = dash.workers[n], top + i // cols * bh, i % cols * bw
                put(stdscr, y, x, title(wk), curses.A_REVERSE if n == dash.selected else curses.A_BOLD, bw - 1)
                put(stdscr, y + 1, x, wk.status + wk.flag, 0, bw - 1)
                for j, line in enumerate(visible(wk.lines, bh - 2, None)):
                    put(stdscr, y + 2 + j, x, line, 0, bw - 1)
    stdscr.refresh()


def handle_key(dash, key, cols, height):
    """Apply one key; False means quit."""
    if dash.zoom is not None:
        if key in ("esc", "q"):
            dash.zoom = dash.top = None
        elif key in ("up", "down", "pgup", "pgdn", "home", "end") and dash.zoom in dash.workers:
            dash.top = scroll(dash.top, key, len(dash.workers[dash.zoom].lines), height - 1)
    elif key == "q":
        return False
    elif key == "enter":
        if dash.selected in dash.workers:
            dash.zoom, dash.top = dash.selected, None
    elif key != "esc":
        dash.selected = move(dash.names(), dash.selected, key, cols)
    return True


def run(stdscr, dash):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.timeout(200)
    last_meta = last_log = float("-inf")
    while True:
        now = time.monotonic()  # intervals and the close countdown must not follow clock jumps
        if now - last_meta >= 2:
            dash.refresh_meta(now)
            last_meta = now
        if now - last_log >= 0.5:
            dash.poll_logs()
            last_log = now
        if not dash.tick(now):
            return 0
        draw(stdscr, dash, now)
        key = key_name(stdscr.getch())
        if key:
            h, w = stdscr.getmaxyx()
            shape = layout(dash, h, w)[3]
            if not handle_key(dash, key, shape[0] if shape else 1, h):
                return 0


def main(argv):
    try:
        if len(argv) not in (1, 2):
            raise ValueError
        run_dir, pid = argv[0], int(argv[1]) if len(argv) == 2 else None
        if pid is not None and pid <= 0:  # os.kill(0 or negative) would signal whole process groups
            raise ValueError
    except ValueError:
        print("usage: swarm-dash.py <run-folder> [<owner-pid>]", file=sys.stderr)
        return 2
    if not sys.stdout.isatty():
        print("swarm-dash.py needs a terminal", file=sys.stderr)
        return 2
    if not os.path.isdir(run_dir):
        print(f"swarm-dash.py: {run_dir}: not a folder", file=sys.stderr)
        return 2
    os.environ.setdefault("ESCDELAY", "25")  # before curses starts, so Esc does not lag a second
    try:
        return curses.wrapper(run, Dash(run_dir, pid))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
