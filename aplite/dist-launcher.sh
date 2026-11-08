#!/bin/sh
# `aplite` at the top of the Linux tar.gz, and the AppImage's AppRun (a link to it); aplite/build_dist.sh copies it.
# Runs aplite with the bundled Python, for people and tools alike: `aplite <component|patch|url> [args]`.
# Started without a terminal and without arguments (double-clicked in a file manager), it first reopens itself in a
# terminal emulator: $TERMINAL, xdg-terminal-exec, x-terminal-emulator, then a list of known ones. With arguments
# (a tool running `aplite Generate ...` with its output captured) it runs in place. APLITE_NO_TERMINAL=1 never reopens.
HERE=$(dirname "$(readlink -f "$0")")
MARK=--aplite-opened-terminal  # first argument of the relaunch: an environment variable wouldn't survive
                               # terminals that start commands from a server process (gnome-terminal, kgx, ptyxis)
opened_terminal=
if [ "${1:-}" = "$MARK" ]; then
    opened_terminal=1
    shift
fi

if [ -z "$opened_terminal" ] && [ -z "${APLITE_NO_TERMINAL:-}" ] && [ $# -eq 0 ] && ! { [ -t 0 ] && [ -t 1 ]; }; then
    # the command to run in the terminal: this AppImage (the runtime sets $APPIMAGE) or this script, same args
    set -- "${APPIMAGE:-$HERE/aplite}" "$MARK" "$@"
    has() { command -v "$1" >/dev/null 2>&1; }
    if [ -n "${TERMINAL:-}" ] && has "${TERMINAL%% *}"; then
        exec $TERMINAL -e "$@"  # unquoted: $TERMINAL may carry its own options
    fi
    has xdg-terminal-exec && exec xdg-terminal-exec "$@"
    has x-terminal-emulator && exec x-terminal-emulator -e "$@"
    has ptyxis && exec ptyxis -- "$@"
    has kgx && exec kgx -- "$@"
    has gnome-terminal && exec gnome-terminal -- "$@"
    has konsole && exec konsole -e "$@"
    has xfce4-terminal && exec xfce4-terminal -x "$@"
    has mate-terminal && exec mate-terminal -x "$@"
    has terminator && exec terminator -x "$@"
    has alacritty && exec alacritty -e "$@"
    has kitty && exec kitty "$@"
    has foot && exec foot "$@"
    has wezterm && exec wezterm start -- "$@"
    has xterm && exec xterm -e "$@"
    message="Archipelago Lite runs in a terminal, but no terminal emulator was found. Set \$TERMINAL, or start it from a terminal."
    echo "$message" >&2
    if has zenity; then zenity --error --text="$message"
    elif has kdialog; then kdialog --error "$message"
    elif has notify-send; then notify-send "Archipelago Lite" "$message"
    elif has xmessage; then xmessage "$message"
    fi
    exit 1
fi

export SKIP_REQUIREMENTS_UPDATE=1
# tar.gz: user data in this folder, as upstream's tar.gz keeps it (aplite/datadir.py); the AppImage uses the home folder
[ -z "${APPIMAGE:-}" ] && export APLITE_DATA_DIR="$HERE"
export PYTHONPATH="$HERE/app${PYTHONPATH:+:$PYTHONPATH}"
if [ "${1:-}" = "--python" ]; then  # the AppImage's background manager and spares (aplite/background.py)
    shift
    exec "$HERE/python/bin/python3" "$@"
fi
if [ -n "$opened_terminal" ]; then
    # in a terminal we opened: keep an error on screen before the window closes
    "$HERE/python/bin/python3" -m aplite "$@"
    status=$?
    if [ "$status" -ne 0 ]; then
        printf '\nArchipelago Lite exited with an error (status %s). Press Enter to close.' "$status"
        read -r _
    fi
    exit "$status"
fi
exec "$HERE/python/bin/python3" -m aplite "$@"
