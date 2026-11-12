"""`viz`: open the explorer where the user is. One word, no options to know.

The explorer (the page and its read-only server) is a viewer of breadcrumbs-spacegraph. This module only finds where the
page opens, and it asks the layers above common for the parts that draw, through two ports:

    explorer(home, port, harness)  the URL of the explorer of this memory; its server is started, or reused
    term(url)                      the page drawn in this terminal (Kitty, Ghostty); None when the terminal is no such one
    term_argv(home, harness)      the command line of `term` for this memory (a tmux pane runs it)

In cmux the page opens as a browser split beside the terminal. In Kitty or Ghostty it draws in the terminal (term).
Anywhere else it opens the default browser. The server is shared: a second `viz` reuses it.

autostart: a Hermes chat that starts in a terminal gets the explorer once (cmux or tmux: a split; a bare console: the OS
opens it). No harness starts it by itself now; `viz` and `/viz` open it. BREADCRUMBS_VIZ=0 turns the autostart off.
"""
import os
import shlex
import shutil
import socket
import subprocess
import sys
import webbrowser

from . import ports

PORT = 8765


def _explorer():
    explorer = ports.get("explorer")
    if explorer is None:
        raise LookupError("the explorer is not installed (its viewers are breadcrumbs-spacegraph)")
    return explorer


def _term():
    return ports.get("term")


def _term_argv():
    return ports.get("term_argv")


def listening(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def split_kind(env=os.environ):
    """The terminal multiplexer that can split a window for us: "cmux", "tmux", or None (a bare console, or a
    terminal with no split command, such as Ghostty or Kitty alone)."""
    if env.get("CMUX_SURFACE_ID") or env.get("CMUX_WORKSPACE_ID"):
        return "cmux"
    if env.get("TMUX"):
        return "tmux"
    return None


def can_launch(env=os.environ, platform=sys.platform):
    """True when the OS can open a page: macOS and Windows always; Linux only with a display (else Python's
    webbrowser would start a text browser on the chat's own terminal)."""
    return platform in ("darwin", "win32") or bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def autostart(home, env=os.environ, err=None, harness=None):
    """Hermes starts in a terminal: in cmux or tmux, split a window for the explorer (a cmux browser split, or a
    tmux pane that runs `term`); in a bare console, ask the OS to open it. Returns "cmux", "tmux", "os" or None.
    harness: the program that starts it (the icon beside the brand)."""
    err = err or sys.stderr
    if env.get("BREADCRUMBS_VIZ", "").lower() in ("0", "off", "no", "false"):
        return None
    kind = split_kind(env)
    argv_of = _term_argv()
    if kind == "tmux" and argv_of and shutil.which("tmux"):
        argv = ["tmux", "split-window", "-h", "-d"] + (["-t", env["TMUX_PANE"]] if env.get("TMUX_PANE") else [])
        r = subprocess.run(argv + [shlex.join(argv_of(home, harness))], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode == 0:
            return "tmux"
        print("breadcrumbs viz: tmux could not split: %s" % r.stderr.decode("utf-8", "replace").strip(), file=err)
    url = _explorer()(home, PORT, harness)
    if kind == "cmux" and shutil.which("cmux"):
        r = subprocess.run(["cmux", "browser", "open", url], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode == 0:
            return "cmux"
        print("breadcrumbs viz: cmux could not open it: %s" % r.stderr.decode("utf-8", "replace").strip(), file=err)
    if can_launch(env) and webbrowser.open(url):
        return "os"
    return None


def run(home, port=PORT, err=None, terminal=True, harness=None):
    """terminal=False: the caller is not the user's shell (the /viz command of a chat), so the terminal is not a choice.
    harness: the harness that asks (the explorer may name it)."""
    err = err or sys.stderr
    url = _explorer()(home, port, harness)
    cmux = shutil.which("cmux")
    if cmux:
        r = subprocess.run([cmux, "browser", "open", url], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode == 0:
            print("breadcrumbs viz: opened in a cmux split", file=err)
            return 0
        print("breadcrumbs viz: cmux could not open it: %s" % r.stderr.decode("utf-8", "replace").strip(), file=err)
    term = _term()
    if terminal and sys.stdout.isatty() and term:
        rc = term(url)
        if rc is not None:
            return rc
    webbrowser.open(url)
    print("breadcrumbs viz: opened in the browser: " + url, file=err)
    return 0


def command(args, home):
    """`python -m breadcrumbs viz`: a profile that opted out of visualization (the port "blocked") gets one line."""
    blocked = ports.get("blocked")
    why = blocked(os.path.dirname(os.path.abspath(home))) if blocked else ""
    if why:
        print("breadcrumbs: %s" % why, file=sys.stderr)
        return 1
    try:
        return run(home, port=args.port)
    except LookupError as e:
        print("breadcrumbs viz: %s" % e, file=sys.stderr)
        return 1
