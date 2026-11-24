#!/usr/bin/env bash
# Open a detached terminal window running a command; print the command if no window is possible.
# usage: open-window.sh <title> <command> [args...]
# exit: 0 window launched, 2 usage error or <command> not found, 3 no window possible (command printed)

if [ "$#" -lt 2 ]; then
  echo "usage: open-window.sh <title> <command> [args...]" >&2
  exit 2
fi
title=$1
shift

# Fail before any terminal opens: a missing command would only flash a dead window.
if ! command -v "$1" >/dev/null 2>&1; then
  echo "open-window.sh: command not found: $1" >&2
  exit 2
fi

fallback() {
  printf 'Run this to watch:'
  printf ' %q' "$@"
  printf '\n'
  exit 3
}

[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || fallback "$@" # Close when command in $@ exits 

# -w (wait, so $! tracks the terminal) is util-linux only; probe once, degrade to plain setsid or nohup.
if setsid -w true >/dev/null 2>&1; then detach=(setsid -w)
elif command -v setsid >/dev/null 2>&1; then detach=(setsid)
else detach=(nohup); fi

tried=" "
for term in xfce4-terminal x-terminal-emulator ptyxis gnome-terminal kgx konsole mate-terminal tilix kitty alacritty foot wezterm ghostty xterm; do
  command -v "$term" >/dev/null 2>&1 || continue
  name=$term
  if [ "$term" = x-terminal-emulator ]; then  # Debian alternatives link: use its target's flags
    name=$(readlink -f "$(command -v "$term")")
    name=${name##*/}
    # Debian's *.wrapper scripts only translate xterm options (and drop --maximize): run the real terminal.
    if [ "$name" != "${name%.wrapper}" ]; then
      name=${name%.wrapper}
      term=$name
      command -v "$term" >/dev/null 2>&1 || continue
    fi
  fi
  case $tried in *" $name "*) continue ;; esac
  tried+="$name "
  run=("$@")
  case $name in
    xfce4-terminal)      args=(--maximize --title="$title" -x) ;;
    xterm)               args=(-maximized -T "$title" -e) ;;
    gnome-terminal)      args=(--maximize --title="$title" --) ;;
    ptyxis)              args=(--maximize --title="$title" --) ;;
    kgx)                 args=(--title="$title" --) ;;  # no maximize flag
    konsole)             args=(-p "tabtitle=${title//[;[:cntrl:]]/}" -e) ;;  # ';' would add profile properties; no maximize flag, only --fullscreen
    mate-terminal)       args=(--maximize --title="$title" -x) ;;
    tilix)               args=(--maximize --title="$title" -e)  # -e takes one string that tilix shell-parses: single-quote each word
                         q=; for a in "$@"; do q+=" '${a//\'/\'\\\'\'}'"; done
                         run=("${q# }") ;;
    kitty)               args=(--start-as=maximized --title="$title" --) ;;
    alacritty)           args=(--title="$title" -o 'window.startup_mode="Maximized"' -e) ;;
    foot)                args=(--maximized --title="$title" --) ;;
    wezterm)             args=(start --) ;;  # `start` has no title or maximize option
    ghostty)             args=(--maximize=true --title="$title" -e) ;;
    *)                   args=(-T "$title" -e) ;;  # x-terminal-emulator pointing at another terminal
  esac
  "${detach[@]}" "$term" "${args[@]}" "${run[@]}" >/dev/null 2>&1 </dev/null &
  pid=$!
  for _ in 1 2 3 4 5 6 7 8 9 10; do  # still alive after ~0.5 s: the window is up
    sleep 0.05
    kill -0 "$pid" 2>/dev/null || break
  done
  kill -0 "$pid" 2>/dev/null && exit 0
  wait "$pid" && exit 0  # exited fast: success only if status 0, else try the next terminal
done

fallback "$@"
