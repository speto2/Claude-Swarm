# /swarm

A Claude Code skill that runs a reviewed swarm of parallel worker subagents on one task.

## What it does

`/swarm 4 sonnet medium <task>` makes your Claude Code session the overseer:

1. It splits the task into pieces, each with its own files, so no two workers edit the same file.
2. It runs up to the requested number of workers at the same time, each on the model and effort you chose. If there are fewer pieces than workers, it runs fewer workers.
3. Workers share a run folder (task board, per-worker notes) and can message each other and the overseer.
4. The overseer reviews each piece as it finishes and sends fix reports back to the worker that wrote it.
5. When everything is done, it runs a final review (tests, build, lint, bugs, error handling, code quality, security) and writes a report.
6. With `y` (see Usage), one terminal window per worker shows what that worker is doing, plus one window for the task board. The windows read local files and cost no tokens. They close themselves when the run ends (the report is written, also after a stop) or when the Claude Code session running the workers exits. They show what workers print, including the first line of tool output, so a secret a worker prints can appear in a window.

## Requirements

- Claude Code 2.1.292 or later
- Auto mode. Workers run in the background, where nobody can approve a prompt, so `/swarm` refuses to start without it.
- Python 3 (standard library only)
- For the windows: Linux with X11 or Wayland and one of xfce4-terminal, x-terminal-emulator, gnome-terminal, konsole, kitty or xterm. Without one, the overseer prints the commands to run instead.

## Install

### With Claude Code

Paste this prompt into Claude Code, from any folder. It clones the repository to `~/Claude-Swarm`, links the skill and the two worker agents, and runs the tests. It never overwrites an existing file or folder.

```text
Install the /swarm skill from https://github.com/speto2/Claude-Swarm for me. Do these steps in order and stop at the first problem:
1. If ~/Claude-Swarm already exists, stop and ask me what to do. Otherwise run: git clone https://github.com/speto2/Claude-Swarm.git ~/Claude-Swarm
2. Check that python3 is installed and that `claude --version` is 2.1.292 or later. On Linux, also say whether one of these terminals is installed for the optional live windows: xfce4-terminal, x-terminal-emulator, gnome-terminal, konsole, kitty, xterm. Report anything missing.
3. Create ~/.claude/skills and ~/.claude/agents if they are missing. Then create these three symbolic links with `ln -s`, using absolute paths. Check each link path first: if it already exists, do not touch it, stop and tell me.
   - ~/.claude/skills/swarm -> ~/Claude-Swarm/skills/swarm
   - ~/.claude/agents/swarm-worker.md -> ~/Claude-Swarm/agents/swarm-worker.md
   - ~/.claude/agents/swarm-worker-web.md -> ~/Claude-Swarm/agents/swarm-worker-web.md
4. In ~/Claude-Swarm, run: python3 -m unittest discover -s tests
5. Report what you did and the test result. Then tell me to start a new Claude Code session and switch to auto mode before I run /swarm.
```

The links point into `~/Claude-Swarm`, so keep that folder where it is.

### By hand

From the root of this repository:

```bash
mkdir -p ~/.claude/skills ~/.claude/agents
ln -sT "$PWD/skills/swarm" ~/.claude/skills/swarm
ln -sT "$PWD/agents/swarm-worker.md" ~/.claude/agents/swarm-worker.md
ln -sT "$PWD/agents/swarm-worker-web.md" ~/.claude/agents/swarm-worker-web.md
```

`ln -sT` refuses to create a link when the path already exists, so it never nests inside an existing directory or overwrites anything. To reinstall, run the uninstall line first. The links point into this repository, so keep it where you installed it from.

Start a new Claude Code session afterwards: agent definitions are loaded when a session starts.

## Choosing a model

The overseer reviews all work, so cheap workers are usually enough: haiku or sonnet at low or medium effort. Your choice always wins.

## Usage

```
Usage: /swarm <workers> <model> <effort> [windows] <task...>
  workers  1-20
  model    haiku | sonnet | opus | fable
  effort   low | medium | high | xhigh | max
  windows  y | yes | n | no (optional; default no). y opens a terminal window per worker plus a task board
Example: /swarm 4 sonnet medium y add CSV export to the reports page, with tests
```

The windows word is read only right after the effort. A task that itself starts with y, yes, n or no needs the windows word in front of it, for example `/swarm 2 haiku low n no longer log passwords in auth.py`.

To stop a running swarm, tell the overseer to stop. It stops every worker and reports what finished.

## Worker types

The overseer picks one per task:

- `swarm-worker`: file and shell tools plus messaging. The default, with a small startup context.
- `swarm-worker-web`: the same plus web fetch and search, for tasks that need online docs.
- `general-purpose`: only for tasks that need MCP tools or a skill. It loads the full context, so it costs more.

Worker types are guidance on cost and scope, not a security boundary. In auto mode every worker can run shell commands without approval. The file-ownership and no-commit rules are instructions to the workers, not limits that are enforced. A worker that reads hostile text, such as a file or a fetched page, could be steered. Run `/swarm` only in repositories whose content you trust.

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

If you installed with the Claude Code prompt, you can then delete `~/Claude-Swarm`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
