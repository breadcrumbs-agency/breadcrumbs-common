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
    def run_viz(self, which, run_rc):
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
            rc = viz.run("home", err=err)
        return rc, calls, browser, err.getvalue()

    def test_cmux_gets_a_browser_split(self):
        rc, calls, browser, _ = self.run_viz("/x/cmux", 0)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [["/x/cmux", "browser", "open", URL]])
        browser.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
