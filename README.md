# /swarm

A Claude Code skill that runs a reviewed swarm of parallel worker subagents on one task.

## What it does

`/swarm <task>` makes your Claude Code session the overseer:

1. It splits the task into pieces, each with its own files, so no two workers edit the same file. Files that several pieces only add lines to, such as a route table, stay with the overseer, which adds the lines at the end.
2. It shows you the plan (pieces, files, checks, workers) and waits for your go before starting anything.
3. It runs the workers at the same time: one per piece by default, on sonnet at medium effort, or the number, model and effort you choose (see Usage).
4. Workers share a run folder (task board, per-worker notes) and can message each other and the overseer.
5. The overseer reviews each piece as it finishes and sends fix reports back to the worker that wrote it.
6. When everything is done, it runs a final review (tests, build, lint, bugs, error handling, code quality, security) and writes a report, with the tokens and estimated cost of the overseer, every worker and any helper agent.
7. With `w` (see Usage), one terminal window shows the task strip and a box per worker (status, quiet or finished flag, newest lines). Arrow keys, Tab or 1-9 pick a worker, Enter opens its log (newest 100,000 lines) full-screen, Up/Down/PgUp/PgDn/Home/End scroll, End follows new lines again, Esc or q goes back to the grid. In the grid, q closes the dashboard without stopping the run. It opens maximized where the terminal supports it, reads local files and costs no tokens. It closes itself 5 s after the run ends (the report is written, also after a stop) or, when the overseer can identify it, when the Claude Code session running the workers exits. It shows what workers print, so a secret a worker prints can appear in the window.

## Requirements

- Claude Code 2.1.292 or later
- Auto mode. Workers run in the background, where nobody can approve a prompt, so `/swarm` refuses to start without it.
- Linux (macOS not supported yet); Python 3, standard library only
- Git is optional. Without it, the review cannot catch a worker editing files outside its tasks.
- For the dashboard: Linux with X11 or Wayland and one of xfce4-terminal, x-terminal-emulator, ptyxis, gnome-terminal, kgx, konsole, mate-terminal, tilix, kitty, alacritty, foot, wezterm, ghostty, or xterm. Without one, the overseer prints the command to run instead.

## Install

### With Claude Code

Paste this prompt into Claude Code, from any folder. It clones the repository to `~/Claude-Swarm`, links the skill and the two worker agents, and runs the tests. It never overwrites an existing file or folder.

```text
Install the /swarm skill from https://github.com/speto2/Claude-Swarm for me. Do these steps in order and stop at the first problem:
1. If ~/Claude-Swarm already exists, stop and ask me what to do. Otherwise run: git clone https://github.com/speto2/Claude-Swarm.git ~/Claude-Swarm
2. Check that python3 is installed and that `claude --version` is 2.1.292 or later. On Linux, also say whether one of these terminals is installed for the optional live dashboard: xfce4-terminal, x-terminal-emulator, ptyxis, gnome-terminal, kgx, konsole, mate-terminal, tilix, kitty, alacritty, foot, wezterm, ghostty, xterm. Report anything missing.
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
for p in skills/swarm agents/swarm-worker.md agents/swarm-worker-web.md; do
  if [ -e ~/.claude/"$p" ] || [ -L ~/.claude/"$p" ]; then echo "exists, skipped: ~/.claude/$p"; else ln -s "$PWD/$p" ~/.claude/"$p"; fi
done
```

The loop skips and reports any link path that already exists, a dangling link included, so it never nests a link inside an existing folder or overwrites anything; running it again changes nothing. To reinstall, run the uninstall line first (see Uninstall). The links point into the repository, so keep it where you installed it from.

Start a new Claude Code session afterwards: agent definitions are loaded when a session starts.

## Choosing a model

The overseer reviews all work, so cheap workers are usually enough: haiku or sonnet at low or medium effort.

The overseer is usually the biggest cost: every step it takes re-reads your whole session. Start `/swarm` in a fresh session (or after `/clear`), with a task line that stands on its own, to keep that context small.

## Usage

```
Usage: /swarm [--keep] [<workers>[w] <model> <effort>] <task...>
  --keep   keep the run folder (.swarm/runs/...) after the report; by default it is deleted
  workers  1-20; add w (as in 4w) to open a live dashboard window
  model    haiku | sonnet | opus | fable
  effort   low | medium | high | xhigh | max
Without them: one worker per task, sonnet, medium. Start with w for the dashboard.
A task that starts with a number needs the full form.
Examples: /swarm add CSV export to the reports page, with tests
          /swarm 4w sonnet medium add CSV export to the reports page, with tests
          /swarm --keep w add CSV export to the reports page, with tests
```

- `/swarm <task>`: the defaults, no dashboard.
- `/swarm w <task>`: the defaults, with the dashboard.
- `/swarm 4 opus high <task>`: at most 4 workers on opus at high effort; `4w` adds the dashboard.
- `/swarm --keep <task>`: keep the run folder afterwards. `--keep` goes first, before any other option.

The overseer then shows its plan. Reply go to start, describe changes to adjust it, or say stop.

To stop a running swarm, tell the overseer to stop. It stops every worker and reports what finished, with the cost so far.

## Worker types

The overseer picks one per task:

- `swarm-worker`: file and shell tools plus messaging. The default, with a small startup context.
- `swarm-worker-web`: the same plus web fetch and search, for tasks that need online docs.
- `general-purpose`: only for tasks that need MCP tools or a skill. It loads the full context, so it costs more.

Worker types are guidance on cost and scope, not a security boundary. In auto mode every worker can run shell commands without approval. The file-ownership and no-commit rules are instructions, not enforced limits. A worker that reads hostile text, such as a file or a fetched page, could be steered. Run `/swarm` only in repositories whose content you trust.

## Run folder

Each run writes to `.swarm/runs/<YYYYMMDD-HHMMSS>/` at the project root. `.swarm/.gitignore` contains `*`, so git ignores the whole folder without changes to your own `.gitignore`.

| File | Written by | Holds |
|---|---|---|
| `tasks.md` | overseer | The task board: owner, status, files, check, dependencies, fix rounds |
| `roster.md` | overseer | Worker names, agent IDs, model, effort and log paths |
| `snapshot/`, `snapshot.json` | `swarm-run.py init` | Copies of the owned files and the starting git state, so reviews see only the swarm's changes |
| `workers/<name>.md` | that worker | Status line, files changed, check results, notes for peers |
| `reviewed.json` | `swarm-run.py diff` | Each task's file hashes at its last review, so a later diff can flag a worker editing a finished task's files |
| `report.md` | overseer | The final report, with the cost table from `swarm-run.py cost` |

The cost table reads Claude Code's own session logs and prices them at Anthropic's list prices as of 2026-11-03 (in `swarm-run.py`). It is an estimate: it doesn't know your plan or discounts, and prices change.

After the report, the run folder is deleted; the report stays in the chat. Start with `--keep` to keep it. The next `/swarm` in the same project also deletes runs that ended without a report (a crash or a closed session) once nothing in them has changed for 24 hours. Kept runs stay until you delete them.

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
