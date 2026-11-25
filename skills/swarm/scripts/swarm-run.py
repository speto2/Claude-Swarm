#!/usr/bin/env python3
"""Run-folder helper for /swarm: start a run, show what changed, count what it cost.

usage:
  swarm-run.py init [--keep] < table.md check the task table, create the run folder and snapshot, print the run folder
                                        (last line); first removes dead runs: no report.md, idle 24 h, not kept
  swarm-run.py diff <run> [<task-id>]   changes in one task's files (all tasks' without an id) since the snapshot; with an id it
                                        also records that task's file hashes in <run>/reviewed.json and flags changes in other tasks' files
  swarm-run.py cost <run>               tokens and estimated cost per agent, as a markdown table

Paths in the task table are relative to the project root: the git top level, else the current folder.
"""
import difflib
import errno
import glob
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

MAX_DIFF_LINES = 400  # diff output cap, so a large change can't flood the overseer's context
CELL_SPLIT = re.compile(r"(?<!\\)\|")
CLEAN = "clean"
MAX_READ = 8 * 1024 * 1024  # a regular file over this is hashed, never loaded
LARGE = object()  # read_bytes result for such a file
RUN_NAME = re.compile(r"^\d{8}-\d{6}(-\d+)?$")
SWEEP_AFTER = 24 * 3600  # a run with no report and nothing written for this long is dead
SWARM_COMMAND = "<command-name>/swarm</command-name>"
SWARM_START = re.compile(r"\s*(?:<command-message>[^<]*</command-message>\s*)?" + re.escape(SWARM_COMMAND))  # the command opens the message; quoted text does not count
# List prices per million tokens: (input, output, cache-read multiplier), from Anthropic's pricing table as cached 2026-11-03.
# shortcut: Haiku 5.5 costs more above 100K-token prompts and its read multiplier is the general ~0.1x; update this table when prices change
PRICES = {
    "claude-fable-5-2": (8.0, 40.0, 0.025),
    "claude-opus-5-5": (4.0, 20.0, 0.05),
    "claude-sonnet-5-5": (2.0, 10.0, 0.05),
    "claude-haiku-5-5": (0.10, 0.50, 0.10),
}
WRITE_5M, WRITE_1H = 1.25, 2.0


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


def git(root, *args):
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    except OSError:
        return None


def project_root():
    r = git(os.getcwd(), "rev-parse", "--show-toplevel")
    if r and r.returncode == 0:
        return os.fsdecode(r.stdout).rstrip("\r\n"), True  # not strip(): a folder name may end in a space
    return os.getcwd(), False


def open_regular(path):
    """fd of a regular file, opened without following a symlink or blocking on a FIFO; OSError for anything else."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", os.fspath(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_bytes(path):
    """Contents of a regular file; None if missing or not a regular file; LARGE if over MAX_READ."""
    try:
        fd = open_regular(path)
        with os.fdopen(fd, "rb") as f:
            data = f.read(MAX_READ + 1)
    except OSError:
        return None
    return LARGE if len(data) > MAX_READ else data


def file_hash(path):
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return "link:" + os.readlink(path)
        if not stat.S_ISREG(st.st_mode):
            return "special"  # never open a FIFO or device
        h = hashlib.sha256()
        with os.fdopen(open_regular(path), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def bad_path(root, path, follow=True):
    """Why a task path is not allowed, or None. follow=False leaves a symlink at the last component alone (it is never opened)."""
    if not isinstance(path, str) or not path or "\0" in path:
        return "is not a usable path"
    norm = os.path.normpath(path)
    if os.path.isabs(norm) or norm == ".." or norm.startswith(".." + os.sep):
        return "is outside the project"
    if norm == ".swarm" or norm.startswith(".swarm" + os.sep):
        return "is inside .swarm/"
    base = os.path.realpath(root)
    full = os.path.join(base, norm)
    real = os.path.realpath(full) if follow else os.path.join(os.path.realpath(os.path.dirname(full)), os.path.basename(full))
    rel = os.path.relpath(real, base)
    if rel == ".." or rel.startswith(".." + os.sep):
        return "resolves outside the project"
    if rel == ".swarm" or rel.startswith(".swarm" + os.sep):
        return "resolves into .swarm/"
    return None


def parse_ts(ts):
    """Aware datetime of an ISO-8601 timestamp (naive means UTC), or None."""
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def dirty_files(root):
    """{path: content hash, or None if deleted} for every file git reports as changed or untracked, relative to root; None if git failed."""
    r = git(root, "status", "--porcelain", "-z", "--untracked-files=all")
    if not r or r.returncode != 0:
        return None
    parts, out, i = os.fsdecode(r.stdout).split("\0"), {}, 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            src = parts[i] if i < len(parts) else ""  # with -z the rename or copy source is the next field
            i += 1
            if "R" in xy and src and not (src == ".swarm" or src.startswith(".swarm/")):
                out[src] = file_hash(os.path.join(root, src))  # a renamed-away file is gone (None); a copy's source is untouched
        if path == ".swarm" or path.startswith(".swarm/"):
            continue
        out[path] = file_hash(os.path.join(root, path))
    return out


def read_tasks(text, root):
    """{task id: [owned paths]}, {task id: owner} and a list of problems with the table."""
    header, rows = parse_table(text)
    if "id" not in header or "files" not in header:
        return {}, {}, ["the table needs a header row with `id` and `files` columns"]
    owned, owners, seen, problems = {}, {}, {}, []
    for cells in rows:
        if len(cells) != len(header):
            problems.append(f"row `{' | '.join(cells)[:60]}` has {len(cells)} cells, the header {len(header)}; write a | inside a cell as \\|")
            continue
        row = dict(zip(header, cells))
        tid = row["id"]
        if not tid or tid in owned:
            problems.append(f"task id `{tid}` is empty or used twice")
            continue
        files = []
        for raw in row["files"].split(","):
            name = raw.strip().strip("`")
            if not name or name.lower() in ("none", "-"):
                continue
            path = os.path.normpath(name)
            why = bad_path(root, name)
            if why:
                problems.append(f"{tid}: {name} {why}")
            elif os.path.isdir(os.path.join(root, path)):
                problems.append(f"{tid}: {name} is a folder; list its files")
            elif path in seen:
                problems.append(f"{path} is owned by both {seen[path]} and {tid}")
            else:
                seen[path] = tid
                files.append(path)
        owned[tid], owners[tid] = files, row.get("owner", "")
    return owned, owners, problems


def snapshot_file(src, dst):
    """Copy a regular file; a missing file, symlink, folder or special file is not snapshotted."""
    try:
        if not stat.S_ISREG(os.lstat(src).st_mode):
            return
    except FileNotFoundError:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with os.fdopen(open_regular(src), "rb") as f, open(dst, "wb") as out:
        shutil.copyfileobj(f, out)


def newest_mtime(path):
    """Newest lstat mtime of the folder and everything under it, without following symlinks."""
    newest = os.lstat(path).st_mtime
    for d, dirs, files in os.walk(path):
        for name in dirs + files:
            newest = max(newest, os.lstat(os.path.join(d, name)).st_mtime)
    return newest


def is_kept(run):
    """snapshot.json's keep flag; None when snapshot.json is missing or unreadable."""
    try:
        with open(os.path.join(run, "snapshot.json"), encoding="utf-8") as f:
            return json.load(f).get("keep") is True
    except (OSError, ValueError, AttributeError):
        return None


def sweep_dead_runs(runs_dir, now):
    """Delete runs that died without a report: no report.md, not kept, nothing written for SWEEP_AFTER."""
    try:
        names = sorted(os.listdir(runs_dir))
    except FileNotFoundError:
        return
    except OSError as e:
        print(f"warning: could not sweep {runs_dir}: {e}", file=sys.stderr)
        return
    for name in names:
        run = os.path.join(runs_dir, name)
        if not RUN_NAME.match(name) or os.path.islink(run) or not os.path.isdir(run):
            continue
        if os.path.exists(os.path.join(run, "report.md")) or is_kept(run):
            continue
        try:
            if now - newest_mtime(run) > SWEEP_AFTER:
                shutil.rmtree(run)
                print(f"swept dead run {name}")
        except OSError as e:
            print(f"warning: could not sweep {name}: {e}", file=sys.stderr)


def cmd_init(text, keep=False):
    root, is_git = project_root()
    owned, owners, problems = read_tasks(text, root)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    swarm = os.path.join(root, ".swarm")
    sweep_dead_runs(os.path.join(swarm, "runs"), time.time())
    os.makedirs(swarm, exist_ok=True)
    ignore = os.path.join(swarm, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as f:
            f.write("*\n")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run, n = os.path.join(swarm, "runs", stamp), 2
    while True:
        try:
            os.makedirs(run)
            break
        except FileExistsError:
            run, n = os.path.join(swarm, "runs", f"{stamp}-{n}"), n + 1
    try:
        os.makedirs(os.path.join(run, "workers"))
        with open(os.path.join(run, "tasks.md"), "w", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        for files in owned.values():
            for path in files:
                snapshot_file(os.path.join(root, path), os.path.join(run, "snapshot", path))
        dirty = dirty_files(root) if is_git else {}
        if dirty is None:
            print("warning: git status failed; changes outside the tasks' files will not be checked", file=sys.stderr)
        with open(os.path.join(run, "snapshot.json"), "w") as f:
            json.dump({"root": root, "git": is_git, "owned": owned, "owners": owners, "dirty": dirty, "keep": keep,
                       "started_at": datetime.now(timezone.utc).isoformat()}, f, indent=1)
    except OSError as e:
        shutil.rmtree(run, ignore_errors=True)  # no half-made run folder without a snapshot.json
        print(f"init failed: {e}", file=sys.stderr)
        return 1
    print(run)
    return 0


def file_diff(old_path, new_path, name):
    """(header line, diff body lines); header None when unchanged; the body is empty unless it is a text diff."""
    try:
        mode = os.lstat(new_path).st_mode
    except OSError:
        mode = None
    if mode is not None and not stat.S_ISREG(mode):
        what = "directory" if stat.S_ISDIR(mode) else f"symlink -> {os.readlink(new_path)}" if stat.S_ISLNK(mode) else "special file"
        return f"== {name}: {what}", []
    old, new = read_bytes(old_path), read_bytes(new_path)
    large = old is LARGE or new is LARGE
    if (file_hash(old_path) == file_hash(new_path)) if large else old == new:
        return None, []
    kind = "new" if old is None else "deleted" if new is None else "modified"
    if large:
        return f"== {name}: {kind}, too large to diff", []
    if b"\0" in (old or b"") or b"\0" in (new or b""):
        return f"== {name}: {kind}, binary", []
    a, b = ((x or b"").decode(errors="replace").splitlines() for x in (old, new))
    body = list(difflib.unified_diff(a, b, lineterm=""))[2:]  # the == header names the file
    plus = sum(line.startswith("+") for line in body)
    note = ""
    if kind == "modified" and not body:  # the bytes differ but no line does
        nl = lambda x: x.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        note = ("; line endings changed" if nl(old) == nl(new)
                else "; trailing newline changed" if nl(old).rstrip(b"\n") == nl(new).rstrip(b"\n")
                else "; only non-UTF-8 bytes changed")
    return f"== {name}: {kind} (+{plus} -{sum(line.startswith('-') for line in body)}{note})", body


def read_text(path):
    data = read_bytes(path)
    return data.decode(errors="replace") if isinstance(data, bytes) else ""


def load_json(path):
    data = read_bytes(path)
    try:
        return json.loads(data) if isinstance(data, bytes) else None
    except ValueError:
        return None


def cmd_diff(run, task=None):
    snap = load_json(os.path.join(run, "snapshot.json"))
    if not isinstance(snap, dict):
        print(f"no valid snapshot.json in {run}", file=sys.stderr)
        return 1
    root, owned = snap.get("root"), snap.get("owned")
    if not isinstance(root, str) or not isinstance(owned, dict) or not all(isinstance(ps, list) for ps in owned.values()):
        print(f"no valid snapshot.json in {run}: malformed", file=sys.stderr)
        return 1
    why = lambda p: "contains .." if isinstance(p, str) and ".." in p.split(os.sep) else bad_path(root, p, follow=False)
    bad = [f"{p!r}: {why(p)}" for ps in owned.values() for p in ps if why(p)]
    if bad:
        print("snapshot.json lists paths that are not allowed:\n" + "\n".join(bad), file=sys.stderr)
        return 1
    if task is not None and task not in owned:
        print(f"unknown task {task}; tasks: {', '.join(owned)}", file=sys.stderr)
        return 1
    owners = snap["owners"] if isinstance(snap.get("owners"), dict) else {}
    files = owned[task] if task is not None else [p for ps in owned.values() for p in ps]
    budget, unchanged = MAX_DIFF_LINES, []
    for path in files:
        header, body = file_diff(os.path.join(run, "snapshot", path), os.path.join(root, path), path)
        if header is None:
            unchanged.append(path)
        elif len(body) > budget:
            print(f"{header} NOT SHOWN (over the {MAX_DIFF_LINES}-line cap), read it directly")
        else:
            print(header)
            if body:
                print("\n".join(body))
            budget -= len(body)
    if unchanged:
        print("unchanged: " + ", ".join(unchanged))

    moved = lambda p: file_hash(os.path.join(run, "snapshot", p)) != file_hash(os.path.join(root, p))
    if task is not None:
        # main edits its hub files only in the final review, so any change before then came from a worker
        hub = [p for t, ps in owned.items() if t != task and owners.get(t) == "main" for p in ps]
        touched = [p for p in hub if moved(p)]
        if touched:
            print("CHANGED IN main's FILES: " + ", ".join(touched))
        reviewed_path = os.path.join(run, "reviewed.json")
        reviewed = load_json(reviewed_path)
        reviewed = reviewed if isinstance(reviewed, dict) else {}
        header, rows = parse_table(read_text(os.path.join(run, "tasks.md")))
        status = {}
        if "id" in header and "status" in header:
            status = {r[header.index("id")]: r[header.index("status")].strip("` ").lower() for r in rows if len(r) == len(header)}
        others = []
        for t, ps in owned.items():
            if t == task or owners.get(t) == "main":
                continue
            entry = reviewed.get(t) if isinstance(reviewed.get(t), dict) else {}
            for p in ps:
                if status.get(t) == "pending" and moved(p) or status.get(t) == "done" and p in entry and file_hash(os.path.join(root, p)) != entry[p]:
                    others.append(f"{p} ({t} {status[t]})")
        if others:
            print("CHANGED IN OTHER TASKS' FILES: " + ", ".join(others))
        reviewed[task] = {p: file_hash(os.path.join(root, p)) for p in owned[task]}
        try:
            with open(reviewed_path, "w") as f:
                json.dump(reviewed, f, indent=1)
        except OSError as e:
            print(f"could not record reviewed.json: {e}", file=sys.stderr)
    if not snap.get("git"):
        print("no git: changes outside the tasks' files are not checked")
        return 0
    now, before = dirty_files(root), snap.get("dirty")
    if now is None or before is None:
        print("changes outside the tasks' files not checked: git status failed" + (" when the run started" if now is not None else ""))
        return 0
    mine = {p for ps in owned.values() for p in ps}
    outside = sorted(p for p in set(now) | set(before) if p not in mine and now.get(p, CLEAN) != before.get(p, CLEAN))
    print("CHANGED OUTSIDE EVERY TASK: " + ", ".join(outside) if outside else "nothing changed outside the tasks' files")
    return 0


def records(path):
    try:
        with os.fdopen(open_regular(path), errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def message(rec):
    msg = rec.get("message")
    return msg if isinstance(msg, dict) else {}


def is_swarm_command(rec):
    content = message(rec).get("content")
    if isinstance(content, list):  # only the user's own text blocks, not a tool result that quotes the command
        content = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    else:
        content = [content]
    return rec.get("type") == "user" and any(isinstance(c, str) and SWARM_START.match(c) for c in content)


def lookup_price(model):
    """PRICES entry for a model id, or None: exact key, then the id without provider prefix, [..], @.., -v1:0 and -YYYYMMDD, then longest prefix."""
    if model in PRICES:
        return PRICES[model]
    m = model.lower().rsplit("/", 1)[-1]
    m = re.sub(r"^(?:[a-z]+\.)?anthropic\.", "", m)
    m = re.sub(r"\[.*\]$", "", m).split("@")[0]
    m = re.sub(r"-v\d+(?::\d+)?$", "", m)
    m = re.sub(r"-\d{8}$", "", m)
    key = m if m in PRICES else max((k for k in PRICES if m.startswith(k)), key=len, default=None)
    return PRICES.get(key)


def count(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


FIELDS = (("input", "input_tokens"), ("write", "cache_creation_input_tokens"), ("read", "cache_read_input_tokens"), ("output", "output_tokens"))


def tally(recs):
    """Token totals and estimated dollars over the assistant requests in a log; each request counted once."""
    t = {"requests": 0, "input": 0, "write": 0, "read": 0, "output": 0, "usd": 0.0, "models": set(), "unpriced": False}
    final = {}  # message id -> the record with the most output tokens; a request is logged once per content block with growing output
    for n, rec in enumerate(recs):
        msg = message(rec)
        usage = msg.get("usage")
        if rec.get("type") != "assistant" or not isinstance(usage, dict):
            continue
        mid = msg.get("id") if isinstance(msg.get("id"), str) else ("anon", n)
        if mid not in final or count(usage.get("output_tokens")) >= count(final[mid][1].get("output_tokens")):
            final[mid] = (msg, usage)
    for msg, usage in final.values():
        n = {k: count(usage.get(f)) for k, f in FIELDS}
        if not any(n.values()):
            continue
        cc = usage.get("cache_creation")
        w1h = min(count(cc.get("ephemeral_1h_input_tokens")) if isinstance(cc, dict) else 0, n["write"])
        model = msg.get("model") if isinstance(msg.get("model"), str) and msg.get("model") else "?"
        t["requests"] += 1
        for k, v in n.items():
            t[k] += v
        t["models"].add(model)
        price = lookup_price(model)
        if price:
            pin, pout, rmul = price
            t["usd"] += (n["input"] + (n["write"] - w1h) * WRITE_5M + w1h * WRITE_1H + n["read"] * rmul) * pin / 1e6 + n["output"] * pout / 1e6
        else:
            t["unpriced"] = True
    return t


def agent_label(log, aid):
    try:
        with os.fdopen(open_regular(os.path.splitext(log)[0] + ".meta.json")) as f:
            meta = json.load(f)
        return f"other: {meta.get('agentType') or aid}"
    except (OSError, ValueError, AttributeError):
        return f"other: {aid}"


def cell(text):
    """A value safe inside a markdown table cell: no control characters or newlines, `|` escaped."""
    return "".join(c for c in str(text) if c.isprintable()).replace("|", "\\|")


def cmd_cost(run):
    header, rows = parse_table(read_text(os.path.join(run, "roster.md")))
    names, rostered, candidates = {}, {}, []
    for cells in rows:
        row = {k: v.strip().strip("`").strip() for k, v in zip(header, cells)}
        key = row.get("agent id") or row.get("worker")
        if not key:
            continue
        names[key] = row.get("worker") or key
        log = os.path.realpath(row.get("output file") or "")
        if row.get("output file") and os.path.isfile(log):
            rostered[key] = log
            candidates.append(os.path.dirname(os.path.dirname(log)))  # <session>/subagents/agent-<id>.jsonl
    if not rostered:
        print("cost: roster.md names no readable worker log in its `output file` column", file=sys.stderr)
        return 1
    session = next((s for s in candidates if os.path.isfile(s + ".jsonl")), candidates[0])
    main_log = list(records(session + ".jsonl"))
    starts = [(i, parse_ts(rec.get("timestamp"))) for i, rec in enumerate(main_log) if is_swarm_command(rec)]
    snap = load_json(os.path.join(run, "snapshot.json"))
    started = parse_ts(snap.get("started_at")) if isinstance(snap, dict) else None
    k = len(starts) - 1  # the run being costed is the last /swarm start at or before its snapshot; else the latest
    if started:
        k = next((j for j in range(len(starts) - 1, -1, -1) if starts[j][1] and starts[j][1] <= started), k)
    nxt = starts[k + 1] if starts and k + 1 < len(starts) else None  # the next /swarm start ends this run's share
    start_dt, end_dt = (starts[k][1] if starts else None), (nxt[1] if nxt else None)
    workers, others = {key: (names[key], tally(records(log))) for key, log in rostered.items()}, []
    for log in glob.glob(os.path.join(session, "subagents", "agent-*.jsonl")):
        if os.path.realpath(log) in rostered.values():
            continue
        aid = os.path.basename(log)[len("agent-"):-len(".jsonl")]
        recs = list(records(log))
        if aid in names:  # rostered, but its output file column pointed elsewhere
            workers[aid] = (names[aid], tally(recs))
            continue
        first = next((d for d in (parse_ts(r.get("timestamp")) for r in recs) if d), None)
        if start_dt and first and first >= start_dt and (not end_dt or first < end_dt):  # helpers this run started, such as an explorer or a fixer missing from the roster
            others.append((first, agent_label(log, aid), tally(recs)))
    end = nxt[0] if nxt else None
    agents = [("overseer", tally(main_log[starts[k][0]:end]))] if starts else []
    agents += [workers[a] for a in names if a in workers] + [(label, t) for _, label, t in sorted(others, key=lambda o: o[0])]
    lines = ["| agent | model | requests | input | cache write | cache read | output | est. cost |", "|---|---|---|---|---|---|---|---|"]
    total = {"requests": 0, "input": 0, "write": 0, "read": 0, "output": 0, "usd": 0.0}

    def row(label, models, t, unpriced):
        return (f"| {label} | {models} | {t['requests']:,} | {t['input']:,} | {t['write']:,} | {t['read']:,} | {t['output']:,} | "
                f"${t['usd']:.2f}{'+?' if unpriced else ''} |")

    for label, t in agents:
        lines.append(row(cell(label), cell(", ".join(sorted(t["models"]))) or "-", t, t["unpriced"]))
        for key in total:
            total[key] += t[key]
    lines.append(row("total", "", total, any(t["unpriced"] for _, t in agents)))
    print("\n".join(lines))
    if starts and total["usd"]:
        print(f"\nOverseer share: {agents[0][1]['usd'] / total['usd']:.0%}.")
    elif not starts:
        print("\nOverseer not counted: no /swarm command found in its session log.")
    print("Estimates at list prices of 2026-11-03; `+?` = tokens of a model without a price. The step that writes the report is not counted.")
    return 0


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")  # a non-UTF-8 file name must not crash the report
    if argv in (["init"], ["init", "--keep"]):
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        return cmd_init(sys.stdin.read(), keep=len(argv) == 2)
    if argv[:1] == ["diff"] and len(argv) in (2, 3):
        return cmd_diff(*argv[1:])
    if argv[:1] == ["cost"] and len(argv) == 2:
        return cmd_cost(argv[1])
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
