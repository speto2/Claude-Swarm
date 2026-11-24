#!/usr/bin/env python3
"""Run-folder helper for /swarm: start a run, show what changed, count what it cost.

usage:
  swarm-run.py init < table.md          check the task table, create the run folder and snapshot, print the run folder
  swarm-run.py diff <run> [<task-id>]   changes in one task's files (every task's without an id) since the snapshot
  swarm-run.py cost <run>               tokens and estimated cost per agent, as a markdown table

Paths in the task table are relative to the project root: the git top level, else the current folder.
"""
import difflib
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

MAX_DIFF_LINES = 400  # diff output cap, so a large change can't flood the overseer's context
CELL_SPLIT = re.compile(r"(?<!\\)\|")
CLEAN = "clean"
SWARM_COMMAND = "<command-name>/swarm</command-name>"
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
    """Header names (lower case) and rows (lists of cells) of a markdown table. A `\\|` inside a cell is not a separator."""
    header, rows = None, []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip().replace("\\|", "|") for c in CELL_SPLIT.split(line.strip("|"))]
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
        return r.stdout.decode().rstrip("\n"), True  # not strip(): a folder name may end in a space
    return os.getcwd(), False


def file_hash(path):
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return "link:" + os.readlink(path)
        if not stat.S_ISREG(st.st_mode):
            return "special"  # never open a FIFO or device
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def dirty_files(root):
    """{path: content hash, or None if deleted} for every file git reports as changed or untracked, relative to root."""
    r = git(root, "status", "--porcelain", "-z", "--untracked-files=all")
    if not r or r.returncode != 0:
        return {}
    parts, out, i = r.stdout.decode(errors="surrogateescape").split("\0"), {}, 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # with -z the rename or copy source is the next field
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
            real = os.path.realpath(os.path.join(root, path))
            if (os.path.isabs(path) or path == ".." or path.startswith(".." + os.sep)
                    or os.path.commonpath([real, os.path.realpath(root)]) != os.path.realpath(root)):  # also catches a symlink out of the root
                problems.append(f"{tid}: {name} is outside the project")
            elif path == ".swarm" or path.startswith(".swarm" + os.sep):
                problems.append(f"{tid}: {name} is inside .swarm/")
            elif os.path.isdir(os.path.join(root, path)):
                problems.append(f"{tid}: {name} is a folder; list its files")
            elif path in seen:
                problems.append(f"{path} is owned by both {seen[path]} and {tid}")
            else:
                seen[path] = tid
                files.append(path)
        owned[tid], owners[tid] = files, row.get("owner", "")
    return owned, owners, problems


def cmd_init(text):
    root, is_git = project_root()
    owned, owners, problems = read_tasks(text, root)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    swarm = os.path.join(root, ".swarm")
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
    os.makedirs(os.path.join(run, "workers"))
    with open(os.path.join(run, "tasks.md"), "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    for files in owned.values():
        for path in files:
            src = os.path.join(root, path)
            if os.path.isfile(src):
                dst = os.path.join(run, "snapshot", path)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
    with open(os.path.join(run, "snapshot.json"), "w") as f:
        json.dump({"root": root, "git": is_git, "owned": owned, "owners": owners,
                   "dirty": dirty_files(root) if is_git else {}}, f, indent=1)
    print(run)
    return 0


def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def file_diff(old_path, new_path, name):
    """(header line, diff body lines); the body is empty when unchanged or binary."""
    old, new = read_bytes(old_path), read_bytes(new_path)
    if old == new:
        return None, []
    kind = "new" if old is None else "deleted" if new is None else "modified"
    if b"\0" in (old or b"") or b"\0" in (new or b""):
        return f"== {name}: {kind}, binary", []
    a = (old or b"").decode(errors="replace").splitlines()
    b = (new or b"").decode(errors="replace").splitlines()
    body = list(difflib.unified_diff(a, b, lineterm=""))[2:]  # the == header names the file
    plus = sum(1 for line in body if line.startswith("+"))
    minus = sum(1 for line in body if line.startswith("-"))
    return f"== {name}: {kind} (+{plus} -{minus})", body


def cmd_diff(run, task=None):
    try:
        with open(os.path.join(run, "snapshot.json")) as f:
            snap = json.load(f)
        root, owned = snap["root"], snap["owned"]
    except (OSError, ValueError, KeyError, TypeError):
        print(f"no valid snapshot.json in {run}", file=sys.stderr)
        return 1
    if task is not None and task not in owned:
        print(f"unknown task {task}; tasks: {', '.join(owned)}", file=sys.stderr)
        return 1
    files = owned[task] if task is not None else [p for ps in owned.values() for p in ps]
    budget, unchanged, skipped = MAX_DIFF_LINES, [], []
    for path in files:
        header, body = file_diff(os.path.join(run, "snapshot", path), os.path.join(root, path), path)
        if header is None:
            unchanged.append(path)
        elif len(body) > budget:
            print(f"{header} [not shown: over the {MAX_DIFF_LINES}-line cap]")
            skipped.append(path)
        else:
            print(header)
            if body:
                print("\n".join(body))
            budget -= len(body)
    if unchanged:
        print("unchanged: " + ", ".join(unchanged))
    if skipped:
        print("NOT SHOWN, read these directly: " + ", ".join(skipped))
    if task is not None:  # main edits its hub files only in the final review, so any change before then came from a worker
        hub = [p for t, ps in owned.items() if snap.get("owners", {}).get(t) == "main" for p in ps]
        touched = [p for p in hub if read_bytes(os.path.join(run, "snapshot", p)) != read_bytes(os.path.join(root, p))]
        if touched:
            print("CHANGED IN main's FILES: " + ", ".join(touched))
    if not snap["git"]:
        print("no git: changes outside the tasks' files are not checked")
        return 0
    now, before = dirty_files(root), snap["dirty"]
    mine = {p for ps in owned.values() for p in ps}
    outside = sorted(p for p in set(now) | set(before) if p not in mine and now.get(p, CLEAN) != before.get(p, CLEAN))
    print("CHANGED OUTSIDE EVERY TASK: " + ", ".join(outside) if outside else "nothing changed outside the tasks' files")
    return 0


def records(path):
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def is_swarm_command(rec):
    content = (rec.get("message") or {}).get("content")
    return rec.get("type") == "user" and isinstance(content, str) and SWARM_COMMAND in content


def tally(recs):
    """Token totals and estimated dollars over the assistant requests in a log; each request counted once."""
    t = {"requests": 0, "input": 0, "write": 0, "read": 0, "output": 0, "usd": 0.0, "models": set(), "unpriced": False}
    seen = set()
    for rec in recs:
        msg = rec.get("message") or {}
        usage = msg.get("usage")
        if rec.get("type") != "assistant" or not isinstance(usage, dict):
            continue
        if msg.get("id") is not None:
            if msg["id"] in seen:
                continue  # one request is logged once per content block, with the same usage
            seen.add(msg["id"])
        inp, out = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
        write, read = usage.get("cache_creation_input_tokens") or 0, usage.get("cache_read_input_tokens") or 0
        if not (inp or out or write or read):
            continue
        w1h = min((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0, write)
        model = msg.get("model") or "?"
        t["requests"] += 1
        t["input"], t["write"], t["read"], t["output"] = t["input"] + inp, t["write"] + write, t["read"] + read, t["output"] + out
        t["models"].add(model)
        if model in PRICES:
            pin, pout, rmul = PRICES[model]
            t["usd"] += (inp + (write - w1h) * WRITE_5M + w1h * WRITE_1H + read * rmul) * pin / 1e6 + out * pout / 1e6
        else:
            t["unpriced"] = True
    return t


def agent_label(log, aid):
    try:
        with open(os.path.splitext(log)[0] + ".meta.json") as f:
            meta = json.load(f)
        return f"other: {meta.get('agentType') or aid}"
    except (OSError, ValueError, AttributeError):
        return f"other: {aid}"


def cmd_cost(run):
    try:
        with open(os.path.join(run, "roster.md"), errors="replace") as f:
            header, rows = parse_table(f.read())
    except OSError:
        header, rows = [], []
    names, session = {}, None
    for cells in rows:
        row = dict(zip(header, cells))
        if row.get("agent id"):
            names[row["agent id"]] = row.get("worker") or row["agent id"]
        log = os.path.realpath(row.get("output file") or "")
        if session is None and row.get("output file") and os.path.isfile(log):
            session = os.path.dirname(os.path.dirname(log))  # <session>/subagents/agent-<id>.jsonl
    if session is None:
        print("cost: roster.md names no readable worker log in its `output file` column", file=sys.stderr)
        return 1
    main_log = list(records(session + ".jsonl"))
    starts = [i for i, rec in enumerate(main_log) if is_swarm_command(rec)]
    start_ts = main_log[starts[-1]].get("timestamp", "") if starts else ""
    workers, others = {}, []
    for log in glob.glob(os.path.join(session, "subagents", "agent-*.jsonl")):
        aid = os.path.basename(log)[len("agent-"):-len(".jsonl")]
        recs = list(records(log))
        if aid in names:
            workers[aid] = (names[aid], tally(recs))
            continue
        first = next((r["timestamp"] for r in recs if r.get("timestamp")), "")
        if start_ts and first >= start_ts:  # helpers this run started, such as an explorer or a fixer missing from the roster
            others.append((first, agent_label(log, aid), tally(recs)))
    agents = [("overseer", tally(main_log[starts[-1]:]))] if starts else []
    agents += [workers[a] for a in names if a in workers] + [(label, t) for _, label, t in sorted(others)]
    lines = ["| agent | model | requests | input | cache write | cache read | output | est. cost |", "|---|---|---|---|---|---|---|---|"]
    total = {"requests": 0, "input": 0, "write": 0, "read": 0, "output": 0, "usd": 0.0}
    for label, t in agents:
        cost = f"${t['usd']:.2f}" + ("+?" if t["unpriced"] else "")
        lines.append(f"| {label} | {', '.join(sorted(t['models'])) or '-'} | {t['requests']} | {t['input']:,} | {t['write']:,} | {t['read']:,} | {t['output']:,} | {cost} |")
        for k in total:
            total[k] += t[k]
    lines.append(f"| total | | {total['requests']} | {total['input']:,} | {total['write']:,} | {total['read']:,} | {total['output']:,} | ${total['usd']:.2f} |")
    print("\n".join(lines))
    if starts and total["usd"]:
        print(f"\nOverseer share of the estimated cost: {agents[0][1]['usd'] / total['usd']:.0%}.")
    elif not starts:
        print("\nOverseer not counted: no /swarm command found in its session log.")
    print("Estimates at Anthropic list prices as of 2026-11-03; `+?` marks tokens of a model without a price. The step that writes the report is not counted.")
    return 0


def main(argv):
    if argv[:1] == ["init"] and len(argv) == 1:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        return cmd_init(sys.stdin.read())
    if argv[:1] == ["diff"] and len(argv) in (2, 3):
        return cmd_diff(*argv[1:])
    if argv[:1] == ["cost"] and len(argv) == 2:
        return cmd_cost(argv[1])
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
