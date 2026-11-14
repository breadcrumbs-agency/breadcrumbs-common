"""`viz` (breadcrumbs/viz.py): where the explorer opens. The explorer and the terminal are ports of breadcrumbs-spacegraph;
these tests give them as fakes, so no window opens and no server starts."""
import argparse
import io
import os
import sys
import unittest
from unittest import mock

from breadcrumbs import cli, viz

URL = "http://127.0.0.1:1/#k=x"


def explorer(home, port=viz.PORT, harness=None):
    return URL


class Run(unittest.TestCase):
    def run_viz(self, which, run_rc, env=None):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=run_rc, stderr=b"denied")

        err = io.StringIO()
        with mock.patch.object(viz, "_explorer", return_value=explorer), \
                mock.patch.object(viz, "_term", return_value=lambda url: None), \
                mock.patch.object(viz.shutil, "which", return_value=which), \
                mock.patch.object(viz.subprocess, "run", fake_run), \
                mock.patch.object(viz.webbrowser, "open") as browser:
            rc = viz.run("home", err=err, env={} if env is None else env)
        return rc, calls, browser, err.getvalue()

    def test_cmux_gets_a_browser_split(self):
        rc, calls, browser, _ = self.run_viz("/x/cmux", 0)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [["/x/cmux", "browser", "open", URL]])
        browser.assert_not_called()

    def test_cmux_in_a_cmux_surface_gets_the_split(self):
        rc, calls, browser, said = self.run_viz("/x/cmux", 0, env={"CMUX_SURFACE_ID": "s"})
        self.assertEqual((rc, calls), (0, [["/x/cmux", "browser", "open", URL]]))
        self.assertIn("cmux split", said)

    def test_another_terminal_does_not_ask_the_cmux_app(self):
        """cmux is installed, but this is Ghostty (or tmux, or WezTerm): the cmux app is not the user's window."""
        for env in ({"TERM_PROGRAM": "ghostty"}, {"TMUX": "t"}, {"WEZTERM_PANE": "1"}, {"TERM_PROGRAM": "iTerm.app"}):
            rc, calls, browser, _ = self.run_viz("/x/cmux", 0, env=env)
            self.assertEqual((rc, calls), (0, []), env)
            browser.assert_called_once_with(URL)

    def test_no_cmux_falls_back_to_the_browser(self):
        rc, calls, browser, _ = self.run_viz(None, 0)
        self.assertEqual((rc, calls), (0, []))
        browser.assert_called_once_with(URL)

    def test_a_refused_cmux_says_so_and_falls_back(self):
        rc, _, browser, err = self.run_viz("/x/cmux", 1)
        self.assertEqual(rc, 0)
        self.assertIn("cmux could not open it: denied", err)
        browser.assert_called_once()

    def test_a_terminal_that_takes_the_page_draws_it(self):
        drawn = []
        with mock.patch.object(viz, "_explorer", return_value=explorer), \
                mock.patch.object(viz, "_term", return_value=lambda url: drawn.append(url) or 0), \
                mock.patch.object(viz.shutil, "which", return_value=None), \
                mock.patch.object(viz.webbrowser, "open") as browser, \
                mock.patch.object(sys, "stdout", mock.Mock(isatty=lambda: True)):
            rc = viz.run("home", err=io.StringIO())
        self.assertEqual((rc, drawn), (0, [URL]))
        browser.assert_not_called()

    def test_no_explorer_is_an_error_the_command_says(self):
        err = io.StringIO()
        with mock.patch.object(viz.ports, "get", return_value=None), mock.patch("sys.stderr", err):
            rc = viz.command(argparse.Namespace(port=viz.PORT), "/h/alpha/breadcrumbs")
        self.assertEqual(rc, 1)
        self.assertIn("breadcrumbs viz: the explorer is not installed", err.getvalue())

    def test_a_memory_with_no_sessions_says_so_and_opens_nothing(self):
        err = io.StringIO()
        with mock.patch.object(viz, "_explorer", return_value=mock.Mock(side_effect=LookupError("holds no sessions yet"))), \
                mock.patch.object(viz.webbrowser, "open") as browser, mock.patch("sys.stderr", err):
            rc = viz.command(argparse.Namespace(port=viz.PORT), "/h/empty/breadcrumbs")
        self.assertEqual(rc, 1)
        self.assertIn("breadcrumbs viz: holds no sessions yet", err.getvalue())
        browser.assert_not_called()

    def test_a_blocked_profile_opens_nothing(self):
        err = io.StringIO()
        blocked = lambda name: (lambda folder: "profile `alpha` opted out") if name == "blocked" else None
        with mock.patch.object(viz.ports, "get", side_effect=blocked), \
                mock.patch.object(viz, "_explorer") as explorer_of, mock.patch("sys.stderr", err):
            rc = viz.command(argparse.Namespace(port=viz.PORT), "/h/alpha/breadcrumbs")
        self.assertEqual(rc, 1)
        self.assertIn("opted out", err.getvalue())
        explorer_of.assert_not_called()

    def test_viz_is_a_command_of_common(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertIn("viz", out.getvalue())


class Autostart(unittest.TestCase):
    """viz.autostart: a split in cmux or tmux, the OS in a bare console. No real window opens here."""

    def start(self, env, which=lambda n: "/x/" + n, rc=0):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=rc, stderr=b"denied")

        term_argv = lambda home, harness=None: ["breadcrumbs", "--home", home, "term"]
        with mock.patch.object(viz, "_explorer", return_value=explorer), \
                mock.patch.object(viz, "_term_argv", return_value=term_argv), \
                mock.patch.object(viz.shutil, "which", which), \
                mock.patch.object(viz.subprocess, "run", fake_run), \
                mock.patch.object(viz, "can_launch", return_value=True), \
                mock.patch.object(viz.webbrowser, "open", return_value=True) as browser:
            where = viz.autostart("/h/breadcrumbs", env=env, err=io.StringIO())
        return where, calls, browser

    def test_cmux_gets_a_browser_split(self):
        where, calls, browser = self.start({"CMUX_SURFACE_ID": "s"})
        self.assertEqual((where, calls), ("cmux", [["cmux", "browser", "open", URL]]))
        browser.assert_not_called()

    def test_tmux_gets_a_pane_that_runs_term(self):
        where, calls, browser = self.start({"TMUX": "t", "TMUX_PANE": "%3"})
        self.assertEqual(where, "tmux")
        self.assertEqual(calls[0][:6], ["tmux", "split-window", "-h", "-d", "-t", "%3"])
        self.assertTrue(calls[0][-1].endswith("--home /h/breadcrumbs term"))
        browser.assert_not_called()

    def test_a_bare_console_asks_the_os(self):
        where, calls, browser = self.start({})
        self.assertEqual((where, calls), ("os", []))
        browser.assert_called_once_with(URL)

    def test_a_refused_split_falls_back_to_the_os(self):
        where, _, browser = self.start({"CMUX_SURFACE_ID": "s"}, rc=1)
        self.assertEqual(where, "os")
        browser.assert_called_once()

    def test_the_switch_turns_it_off(self):
        where, calls, browser = self.start({"CMUX_SURFACE_ID": "s", "BREADCRUMBS_VIZ": "0"})
        self.assertEqual((where, calls), (None, []))
        browser.assert_not_called()

    def test_linux_without_a_display_does_not_launch(self):
        self.assertFalse(viz.can_launch({}, platform="linux"))
        self.assertTrue(viz.can_launch({"DISPLAY": ":0"}, platform="linux"))


class Terminals(unittest.TestCase):
    """viz.terminal_of: the terminal, by the environment it leaves. One case for each name."""

    CASES = (
        ("cmux", {"CMUX_SURFACE_ID": "s", "TERM_PROGRAM": "ghostty"}),  # a Ghostty window: cmux wins
        ("cmux", {"CMUX_WORKSPACE_ID": "w"}),
        ("tmux", {"TMUX": "/tmp/tmux-501/default,1,0", "TERM_PROGRAM": "tmux", "KITTY_WINDOW_ID": "3"}),  # a multiplexer wins
        ("zellij", {"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "main"}),
        ("screen", {"STY": "123.pts-0.host", "TERM": "screen"}),
        ("wezterm", {"WEZTERM_PANE": "4", "TERM_PROGRAM": "WezTerm"}),
        ("wezterm", {"TERM_PROGRAM": "WezTerm"}),
        ("kitty", {"KITTY_WINDOW_ID": "1", "TERM": "xterm-kitty"}),
        ("kitty", {"TERM": "xterm-kitty"}),
        ("ghostty", {"TERM_PROGRAM": "ghostty", "TERM": "xterm-ghostty"}),
        ("ghostty", {"GHOSTTY_RESOURCES_DIR": "/Applications/Ghostty.app/Contents/Resources/ghostty"}),
        ("iterm2", {"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": "w0t0p0:ABC"}),
        ("apple-terminal", {"TERM_PROGRAM": "Apple_Terminal"}),
        ("vscode", {"TERM_PROGRAM": "vscode"}),
        ("warp", {"TERM_PROGRAM": "WarpTerminal"}),
        ("alacritty", {"ALACRITTY_WINDOW_ID": "9", "TERM": "alacritty"}),
        ("windows-terminal", {"WT_SESSION": "guid"}),
        ("konsole", {"KONSOLE_VERSION": "230804"}),
        ("hyper", {"TERM_PROGRAM": "Hyper"}),
        ("", {}),
        ("", {"TERM": "xterm-256color"}),
    )

    def test_each_terminal_by_name(self):
        for name, env in self.CASES:
            self.assertEqual(viz.terminal_of(env), name, env)

    def test_only_four_can_split(self):
        for name, env in self.CASES:
            self.assertEqual(viz.split_kind(env), name if name in ("cmux", "tmux", "wezterm", "kitty") else None, env)

    def test_the_default_is_the_process_environment(self):
        with mock.patch.dict(os.environ, {"WEZTERM_PANE": "7"}, clear=True):
            self.assertEqual(viz.terminal_of(), "wezterm")


class SplitArgv(unittest.TestCase):
    CMD = ["breadcrumbs", "--home", "/h/breadcrumbs", "term"]

    def test_tmux_splits_the_pane_that_asks(self):
        argv = viz.split_argv("tmux", {"TMUX_PANE": "%3"}, self.CMD)
        self.assertEqual(argv[:6], ["tmux", "split-window", "-h", "-d", "-t", "%3"])
        self.assertEqual(argv[6], "breadcrumbs --home /h/breadcrumbs term")

    def test_tmux_with_no_pane_id_splits_the_current_one(self):
        self.assertEqual(viz.split_argv("tmux", {}, self.CMD)[:4], ["tmux", "split-window", "-h", "-d"])
        self.assertNotIn("-t", viz.split_argv("tmux", {}, self.CMD))

    def test_wezterm_splits_to_the_right_and_runs_the_command_after_two_dashes(self):
        argv = viz.split_argv("wezterm", {"WEZTERM_PANE": "4"}, self.CMD)
        self.assertEqual(argv, ["wezterm", "cli", "split-pane", "--right", "--pane-id", "4", "--"] + self.CMD)

    def test_kitty_launches_a_window_without_taking_the_focus(self):
        argv = viz.split_argv("kitty", {"KITTY_LISTEN_ON": "unix:/tmp/kitty"}, self.CMD)
        self.assertEqual(argv[:4], ["kitten", "@", "--to", "unix:/tmp/kitty"])
        self.assertIn("--keep-focus", argv)
        self.assertEqual(argv[-4:], self.CMD)
        self.assertNotIn("--to", viz.split_argv("kitty", {}, self.CMD))

    def test_cmux_has_no_command_line_here(self):
        with self.assertRaises(ValueError):
            viz.split_argv("cmux", {}, self.CMD)


class AutostartTerminals(Autostart):
    """The same fakes as Autostart, for the terminals that came after cmux and tmux."""

    def test_wezterm_gets_a_pane_that_runs_term(self):
        where, calls, browser = self.start({"WEZTERM_PANE": "4"})
        self.assertEqual(where, "wezterm")
        self.assertEqual(calls, [["wezterm", "cli", "split-pane", "--right", "--pane-id", "4", "--",
                                  "breadcrumbs", "--home", "/h/breadcrumbs", "term"]])
        browser.assert_not_called()

    def test_kitty_gets_a_window_that_runs_term(self):
        where, calls, browser = self.start({"KITTY_WINDOW_ID": "1"})
        self.assertEqual(where, "kitty")
        self.assertEqual(calls[0][:2], ["kitten", "@"])
        browser.assert_not_called()

    def test_a_split_that_is_not_installed_asks_the_os(self):
        where, calls, browser = self.start({"WEZTERM_PANE": "4"}, which=lambda n: None)
        self.assertEqual((where, calls), ("os", []))
        browser.assert_called_once_with(URL)

    def test_a_refused_pane_says_which_terminal_and_falls_back(self):
        err = io.StringIO()
        with mock.patch.object(viz, "_explorer", return_value=explorer), \
                mock.patch.object(viz, "_term_argv", return_value=lambda home, harness=None: ["x"]), \
                mock.patch.object(viz.shutil, "which", lambda n: "/x/" + n), \
                mock.patch.object(viz.subprocess, "run", return_value=mock.Mock(returncode=1, stderr=b"remote control is off")), \
                mock.patch.object(viz, "can_launch", return_value=True), \
                mock.patch.object(viz.webbrowser, "open", return_value=True) as browser:
            where = viz.autostart("/h/breadcrumbs", env={"KITTY_WINDOW_ID": "1"}, err=err)
        self.assertEqual(where, "os")
        self.assertIn("kitty could not split: remote control is off", err.getvalue())
        browser.assert_called_once_with(URL)

    def test_terminals_with_no_split_open_the_page(self):
        for env in ({"TERM_PROGRAM": "ghostty"}, {"TERM_PROGRAM": "iTerm.app"}, {"ZELLIJ": "0"}, {"TERM_PROGRAM": "vscode"},
                    {"TERM_PROGRAM": "Apple_Terminal"}, {"STY": "1.x"}):
            where, calls, browser = self.start(env)
            self.assertEqual((where, calls), ("os", []), env)
            browser.assert_called_once_with(URL)


class Where(unittest.TestCase):
    """`viz --where`: what was found and what would happen. It opens nothing."""

    def lines(self, env, have=("cmux", "tmux", "wezterm", "kitten"), platform="darwin"):
        return dict(viz.where(env, which=lambda n: "/x/" + n if n in have else None, platform=platform))

    def test_cmux(self):
        got = self.lines({"CMUX_SURFACE_ID": "s"})
        self.assertEqual((got["terminal"], got["multiplexer"], got["split"], got["kitty graphics"]), ("cmux", "cmux", "cmux", "yes"))
        self.assertEqual(got["autostart"], "a browser split of cmux")

    def test_tmux_and_wezterm_and_kitty_name_their_pane(self):
        for env, name in (({"TMUX": "t"}, "tmux"), ({"WEZTERM_PANE": "1"}, "wezterm"), ({"KITTY_WINDOW_ID": "1"}, "kitty")):
            got = self.lines(env)
            self.assertEqual(got["split"], name, env)
            self.assertIn("a %s pane that runs `term`" % name, got["autostart"])

    def test_a_split_without_its_program_says_so(self):
        got = self.lines({"TMUX": "t"}, have=())
        self.assertIn("`tmux` is not on the PATH", got["split"])
        self.assertIn("OS browser", got["autostart"])

    def test_ghostty_alone_has_graphics_and_no_split(self):
        got = self.lines({"TERM_PROGRAM": "ghostty"})
        self.assertEqual((got["terminal"], got["split"], got["kitty graphics"]), ("ghostty", "none", "yes"))
        self.assertEqual(got["autostart"], "the page in the OS browser")

    def test_iterm_has_no_kitty_graphics(self):
        self.assertEqual(self.lines({"TERM_PROGRAM": "iTerm.app"})["kitty graphics"], "no")

    def test_unknown_terminal_and_no_display(self):
        got = self.lines({}, platform="linux")
        self.assertTrue(got["terminal"].startswith("unknown"))
        self.assertTrue(got["autostart"].startswith("nothing: no split"))

    def test_the_switch(self):
        self.assertIn("BREADCRUMBS_VIZ is off", self.lines({"CMUX_SURFACE_ID": "s", "BREADCRUMBS_VIZ": "off"})["autostart"])

    def test_the_command_prints_it_and_opens_nothing(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "ghostty"}, clear=True), mock.patch("sys.stdout", out), \
                mock.patch.object(viz, "_explorer") as explorer_of, mock.patch.object(viz.webbrowser, "open") as browser:
            rc = viz.command(argparse.Namespace(port=viz.PORT, where=True), "/h/breadcrumbs")
        self.assertEqual(rc, 0)
        self.assertIn("terminal        ghostty", out.getvalue())
        explorer_of.assert_not_called()
        browser.assert_not_called()


@unittest.skipUnless(__import__("shutil").which("tmux"), "tmux is not installed")
class RealTmux(unittest.TestCase):
    """A real split, on a private tmux server (its own socket, killed at the end): autostart opens a second pane that
    runs the `term` command line, to the right of the pane that asked."""

    SOCKET = "bctest-%d" % os.getpid()

    def tmux(self, *argv):
        import subprocess
        return subprocess.run(["tmux", "-L", self.SOCKET] + list(argv), capture_output=True, text=True, timeout=20)

    def setUp(self):
        r = self.tmux("new-session", "-d", "-x", "200", "-y", "50", "-s", "t", "sleep 60")
        if r.returncode != 0:
            self.skipTest("tmux cannot start a server here: %s" % r.stderr.strip())
        self.addCleanup(self.tmux, "kill-server")

    def test_autostart_splits_the_pane_and_the_new_pane_runs_term(self):
        info = self.tmux("display-message", "-p", "-t", "t", "#{socket_path},#{pid},0|#{pane_id}").stdout.strip()
        socket_path_pid, pane = info.split("|")
        env = {"TMUX": socket_path_pid, "TMUX_PANE": pane, "PATH": os.environ.get("PATH", "")}
        term_argv = lambda home, harness=None: ["sleep", "61"]
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(viz, "_term_argv", return_value=term_argv), \
                mock.patch.object(viz, "_explorer", side_effect=AssertionError("a split needs no server of ours")):
            where = viz.autostart("/h/breadcrumbs", env=env, err=io.StringIO())
        self.assertEqual(where, "tmux")
        panes = self.tmux("list-panes", "-t", "t", "-F", "#{pane_id} #{pane_width} #{pane_current_command}").stdout.split("\n")
        panes = [p.split() for p in panes if p]
        self.assertEqual(len(panes), 2, panes)
        self.assertEqual(sorted(p[2] for p in panes), ["sleep", "sleep"])
        self.assertEqual(panes[0][0], pane)  # the chat's pane is the first, and keeps the focus (-d)
        self.assertEqual(self.tmux("display-message", "-p", "-t", "t", "#{pane_id}").stdout.strip(), pane)
        self.assertLess(int(panes[0][1]) + int(panes[1][1]), 200)  # side by side, with the divider between


if __name__ == "__main__":
    unittest.main()
