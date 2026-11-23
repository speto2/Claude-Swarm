---
name: swarm
description: Run a reviewed swarm of parallel worker subagents on a task. Usage: /swarm <workers> <model> <effort> <task...>
argument-hint: <workers> <model> <effort> <task...>
disable-model-invocation: true
---

# /swarm

You are the overseer. Split the job into tasks with exclusive files, run the workers in parallel, review every task, send fixes back to the worker that wrote it, then run a final review and report.

Arguments: `$ARGUMENTS`

## 1. Validate

- `workers`: integer 1–20
- `model`: haiku | sonnet | opus | fable
- `effort`: low | medium | high | xhigh | max
- `task`: the rest of the line, not empty

If any is missing or invalid, print exactly this and do nothing else:

```
Usage: /swarm <workers> <model> <effort> <task...>
  workers  1-20
  model    haiku | sonnet | opus | fable
  effort   low | medium | high | xhigh | max
Example: /swarm 4 sonnet medium add CSV export to the reports page, with tests
```

If your context does not show that auto mode is active, reply exactly this and stop:
`/swarm needs auto mode: workers run in the background and can't wait for approvals. Switch to auto mode and run it again.`

## 2. Understand and split

- Read what the split needs. The user's usual exploration rules apply.
- Split into tasks. Each task gets:
  - exclusive owned files
  - an acceptance check (test command, build, lint, or a stated manual check), scoped to its own files where possible, so a peer's half-finished edits can't fail it
  - explicit depends-on
- Two tasks need the same file: merge them, or make one depend on the other.
- Pin the exact interfaces between tasks in the task entries: names, signatures, file formats, message shapes. Workers must not guess.
- Pick a tier per task:
  - `swarm-worker` by default
  - `swarm-worker-web` when it needs online docs or lookups
  - `general-purpose` only when it needs MCP tools or a skill. Paste the body of `~/.claude/agents/swarm-worker.md` (below its frontmatter) above its brief.
- One task: run one worker and say so. A tiny job: say that fewer workers would do.

## 3. Assign

- Workers W = min(requested, number of tasks).
- Put chains of dependent tasks on one worker, so no worker waits on another.
- Group same-tier tasks; a worker's tier must cover all of its tasks.
- Starting briefs hold only tasks that can start now. Hold blocked tasks. When one becomes startable, send it with SendMessage to the best-fitting existing worker; that resumes it if it had finished. Never spawn for it.
- Names: `w<N>-<slug>`, for example `w1-api`.

## 4. Run folder and snapshot

- At the project root, create `.swarm/.gitignore` containing `*` if missing, then `<run>` = `.swarm/runs/<YYYYMMDD-HHMMSS>/` with a `workers/` folder. Use absolute paths.
- Write `<run>/tasks.md` (only you write it):
  `| id | title | owner | status | files | check | depends | fixes |`
  Status values: `pending`, `working`, `review`, `fixing`, `done`, `failed`, `stopped`.
- Record a starting snapshot in `<run>/snapshot.txt`, so reviews see only the swarm's changes. In git: `git rev-parse HEAD` plus `git status --porcelain`. Without git: copy the owned files to `<run>/snapshot/`.

## 5. Spawn

Brief per worker. The work rules live in the agent definition; don't repeat them.

```
You are <name>. Run folder: <run>. Your status file: <run>/workers/<name>.md.
Peers: see <run>/roster.md (may appear a few seconds after you start). The overseer is main.
Tasks:
- <id> <title>: <goal>
  Files: <owned files>. Check: <command>. Depends: <ids or none>.
  Interfaces: <pinned names, signatures, formats>
```

Spawn with the Agent tool: `subagent_type` = the tier, `model` and `effort` = the user's values. Spawn one worker first. Spawn the rest together in your next message, so they reuse its cached context.

## 6. Roster and windows

- Write `<run>/roster.md` from the spawn results: `| worker | agent id | model | effort | tasks |`. Agent IDs stay out of chat.
- Open the windows in one Bash call, one line per worker, then the board (`<base>` is this skill's base directory):

```bash
B="<base>/scripts"
bash "$B/open-window.sh" "swarm: w1-api" python3 "$B/swarm-watch.py" "<w1 output_file>" "w1-api"
bash "$B/open-window.sh" "swarm: board" python3 "$B/swarm-board.py" "<run>"
```

- Exit 3 means no window could open; relay the printed `Run this to watch:` commands to the user once.

## 7. Wait and review

- After each task a worker messages `main` with `DONE <task-id>`; after a fix, `FIXED <task-id>`. End your turn while waiting: messages and completion notices wake you.
- Reviews come before any other work, so no worker idles past its 5-minute cache. Review tasks that finish close together in one turn.
- Per task: set status `review`, then:
  - read the worker's status file
  - `git diff --stat` against the snapshot first, then only the parts that matter
  - run the acceptance check and read only failure output (`| tail -n 40`)
- Pass: status `done`. Fail: status `fixing`, add 1 to `fixes`, and send the same worker:

```
FIX <task-id> (round n/3)
<file>:<line> — <problem> — <why it matters> — expected: <result>
```

- After 3 rounds, fix it yourself or mark it `failed`, and say so in the report.
- Never repeat worker output in chat. Never read worker logs.
- A worker asks a question: answer it briefly by SendMessage.
- A worker reports a denied action: tell it to pause that work, and ask the user. Never route the denied action to another worker.

## 8. Final review

When every task is `done`:

- Run the full test suite, build and lint where they exist.
- Read the combined diff against the snapshot for parts that don't fit together, bugs, error handling, code quality and security (input handling, secrets, injection, permissions).
- A problem inside one worker's files: a FIX report to that worker. A problem spanning several workers' files: fix it yourself if it is a few lines; otherwise spawn one fixer worker (same model and effort) that owns those files.
- Repeat until clean, at most 3 rounds.
- If the user's CLAUDE.md asks for a step after code changes, run it once now. Workers skip such steps.

## 9. Report

Write `<run>/report.md` and give the same report in chat, under these headings:

- **Result**
- **Tasks**: per task, worker, status, fix rounds used
- **Review findings and fixes**
- **Checks**: each check, pass or fail, with the exact failure lines
- **Unverified or left**
- **Tokens**: per worker (from the Agent results) and in total

## Stopping

If the user says stop: TaskStop every running worker, mark open tasks `stopped`, and report what finished.

## Keep it cheap

- Messages and fix reports stay short and structured.
- Extra tasks, fixes and final-review fixes go to existing workers by SendMessage. Resume, never respawn.
