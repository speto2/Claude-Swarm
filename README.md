# /swarm

A Claude Code skill that runs a reviewed swarm of parallel worker subagents on one task.

## What it does

`/swarm 4 sonnet medium <task>` makes your Claude Code session the overseer:

1. It splits the task into pieces, each with its own files, so no two workers edit the same file.
2. It runs up to the requested number of workers at the same time, each on the model and effort you chose. If there are fewer pieces than workers, it runs fewer workers.
3. Workers share a run folder (task board, per-worker notes) and can message each other and the overseer.
4. The overseer reviews each piece as it finishes and sends fix reports back to the worker that wrote it.
5. When everything is done, it runs a final review (tests, build, lint, bugs, error handling, code quality, security) and writes a report.
6. One terminal window per worker shows what that worker is doing, plus one window for the task board. The windows read local files and cost no tokens.

## Requirements

- Claude Code 2.1.292 or later
- Auto mode. Workers run in the background, where nobody can approve a prompt, so `/swarm` refuses to start without it.
- Python 3 (standard library only)
- For the windows: Linux with X11 or Wayland and one of xfce4-terminal, x-terminal-emulator, gnome-terminal, konsole, kitty or xterm. Without one, the overseer prints the commands to run instead.

## Install

From the root of this repository:

```bash
mkdir -p ~/.claude/skills ~/.claude/agents
ln -s "$PWD/skills/swarm" ~/.claude/skills/swarm
ln -s "$PWD/agents/swarm-worker.md" ~/.claude/agents/swarm-worker.md
ln -s "$PWD/agents/swarm-worker-web.md" ~/.claude/agents/swarm-worker-web.md
```

Start a new Claude Code session afterwards: agent definitions are loaded when a session starts.

## Choosing a model

The overseer reviews all work, so cheap workers are usually enough: haiku or sonnet at low or medium effort. Your choice always wins.

## Usage

```
Usage: /swarm <workers> <model> <effort> <task...>
  workers  1-20
  model    haiku | sonnet | opus | fable
  effort   low | medium | high | xhigh | max
Example: /swarm 4 sonnet medium add CSV export to the reports page, with tests
```

To stop a running swarm, tell the overseer to stop. It stops every worker and reports what finished.

## Worker types

The overseer picks one per task:

- `swarm-worker`: file and shell tools plus messaging. The default, with a small startup context.
- `swarm-worker-web`: the same plus web fetch and search, for tasks that need online docs.
- `general-purpose`: only for tasks that need MCP tools or a skill. It loads the full context, so it costs more.

## Run folder

Each run writes to `.swarm/runs/<YYYYMMDD-HHMMSS>/` at the project root. `.swarm/.gitignore` contains `*`, so git ignores the whole folder without changes to your own `.gitignore`.

| File | Written by | Holds |
|---|---|---|
| `tasks.md` | overseer | The task board: owner, status, files, check, dependencies, fix rounds |
| `roster.md` | overseer | Worker names, agent IDs, model and effort |
| `snapshot.txt` | overseer | The starting commit and already-modified files, so reviews see only the swarm's changes |
| `workers/<name>.md` | that worker | Status line, files changed, check results, notes for peers |
| `report.md` | overseer | The final report |

Runs are kept as history. Delete `.swarm/` when you no longer need them.

## Uninstall

```bash
rm ~/.claude/skills/swarm ~/.claude/agents/swarm-worker.md ~/.claude/agents/swarm-worker-web.md
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```
