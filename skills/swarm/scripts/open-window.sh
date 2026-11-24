#!/usr/bin/env bash
# Open a detached terminal window running a command; print the command if no window is possible.
# usage: open-window.sh <title> <command> [args...]
# exit: 0 window launched, 2 usage error, 3 no window possible (command printed)

if [ "$#" -lt 2 ]; then
  echo "usage: open-window.sh <title> <command> [args...]" >&2
  exit 2
fi
title=$1
shift

fallback() {
  printf 'Run this to watch: '
  printf '%q ' "$@"
  printf '\n'
  exit 3
}

[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || fallback "$@"

for term in xfce4-terminal x-terminal-emulator gnome-terminal konsole kitty xterm; do
  command -v "$term" >/dev/null 2>&1 || continue
  case $term in
    xfce4-terminal)      args=(--maximize --title "$title" -x) ;;
    x-terminal-emulator) args=(-T "$title" -e) ;;
    xterm)               args=(-maximized -T "$title" -e) ;;
    gnome-terminal)      args=(--maximize --title "$title" --) ;;
    konsole)             args=(-p "tabtitle=$title" -e) ;;  # no maximize flag, only --fullscreen
    kitty)               args=(--start-as=maximized --title "$title") ;;
  esac
  setsid -w "$term" "${args[@]}" "$@" >/dev/null 2>&1 </dev/null &
  pid=$!
  for _ in 1 2 3 4 5 6 7 8 9 10; do  # still alive after ~0.5 s: the window is up
    sleep 0.05
    kill -0 "$pid" 2>/dev/null || break
  done
  kill -0 "$pid" 2>/dev/null && exit 0
  wait "$pid" && exit 0  # exited fast: success only if status 0, else try the next terminal
done

fallback "$@"
