"""sync.py and statedb.py: each session lands in the memory of the project where it ran, and each memory gets a state.db.
The harness is a fake one with invented text; the projects are folders of a temporary home."""
import contextlib
import io
import json
import os
import tempfile
import unittest
import unittest.mock

from breadcrumbs import cli, core, ports, scope, statedb, sync, usage
from breadcrumbs.history import Refs

KEY = "fake-sync"


def rows(text, ts):
    return [{"role": "user", "content": text, "ts": ts}, {"role": "assistant", "content": "noted: " + text, "ts": ts + 5}]


def request(sess, n, ts, model="m-big"):
    return usage.record(KEY, sess, "%s-r%d" % (sess, n), ts, model, "prov", 3, 100 * n, 50, 20, 0)


class SyncTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.base = os.path.join(self.home, ".fake", "breadcrumbs")
        os.makedirs(self.base)
        self.apple = self.folder("work", "apple")  # a git repository
        os.makedirs(os.path.join(self.apple, ".git"))
        self.pear = self.folder("work", "pear")  # a folder with a marker
        open(os.path.join(self.pear, ".breadcrumbs"), "w").close()
        self.notes = self.folder("notes")  # a plain folder: the folder itself
        self.off = self.folder("work", "off")
        with open(os.path.join(self.off, ".breadcrumbs"), "w") as f:
            f.write("off\n")
        self.sessions = [
            ("s-apple", "apples", {"cwd": os.path.join(self.apple, "src"), "branch": "main"}, rows("banana bread", 1000)),
            ("s-pear", "pears", {"cwd": self.pear}, rows("cherry tart", 2000)),
            ("s-notes", "notes", {"cwd": self.notes}, rows("durian smell", 3000)),
            ("s-home", "home", {"cwd": self.home}, rows("elderberry wine", 4000)),
            ("s-none", "none", {}, rows("fig jam", 5000)),
            ("s-off", "off", {"cwd": self.off}, rows("grape juice", 6000)),
        ]
        self.requests = [request("s-apple", 1, 1001), request("s-apple", 2, 1002, "m-small"), request("s-pear", 1, 2001)]
        ports.harness(KEY, "Fake", ("fake",), lambda cmd: "", order=99, source=lambda path: iter(self.sessions),
                      usage=lambda path: iter(self.requests))
        self.addCleanup(ports._harnesses.pop, KEY, None)

    def folder(self, *parts):
        path = os.path.join(self.home, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def run_sync(self):
        return sync.sync(KEY, None, self.base, on=True, exclude=(), home=self.home)

    def store_of(self, root):
        return scope.store_home(self.base, root)

    def sessions_of(self, folder):
        return sorted(n[len("sess-"):] for n in Refs(os.path.join(folder, "refs"), readonly=True).names() if n.startswith("sess-"))

    def test_each_session_goes_to_the_memory_of_its_project(self):
        out = self.run_sync()
        self.assertEqual((out["sessions"], out["new"]), (6, 6))
        self.assertEqual(self.sessions_of(self.store_of(self.apple)), ["s-apple"])  # a subfolder: the repository's memory
        self.assertEqual(self.sessions_of(self.store_of(self.pear)), ["s-pear"])
        self.assertEqual(self.sessions_of(self.store_of(self.notes)), ["s-notes"])  # no repository: the folder itself
        self.assertEqual(self.sessions_of(self.base), ["s-home", "s-none", "s-off"])  # no project: the default memory
        self.assertEqual({s["kind"] for s in scope.known(self.base)}, {"git", "explicit", "dir"})
        self.assertEqual(len(out["stores"]), 4)

    def test_a_second_sync_changes_nothing(self):
        self.run_sync()
        folders = [self.store_of(self.apple), self.store_of(self.pear), self.base]
        heads = [Refs(os.path.join(f, "refs"), readonly=True).get("main") for f in folders]
        again = self.run_sync()
        self.assertEqual(again["new"], 0)
        self.assertEqual(again["usage"], {"requests": 3, "added": 0})
        self.assertEqual([Refs(os.path.join(f, "refs"), readonly=True).get("main") for f in folders], heads)

    def test_a_session_that_grew_makes_one_commit_in_its_own_memory_only(self):
        self.run_sync()
        others = [self.store_of(self.pear), self.base]
        heads = [Refs(os.path.join(f, "refs"), readonly=True).get("main") for f in others + [self.store_of(self.apple)]]
        self.sessions[0] = self.sessions[0][:3] + (self.sessions[0][3] + rows("more bananas", 1100),)
        self.assertEqual(self.run_sync()["new"], 1)
        now = [Refs(os.path.join(f, "refs"), readonly=True).get("main") for f in others + [self.store_of(self.apple)]]
        self.assertEqual(now[:2], heads[:2])
        self.assertNotEqual(now[2], heads[2])

    def test_the_usage_of_a_request_goes_to_the_memory_of_its_session(self):
        out = self.run_sync()
        self.assertEqual(out["usage"], {"requests": 3, "added": 3})
        lines = lambda folder: usage.known(folder)  # noqa: E731
        self.assertEqual(sorted(lines(self.store_of(self.apple))), ["s-apple-r1", "s-apple-r2"])
        self.assertEqual(sorted(lines(self.store_of(self.pear))), ["s-pear-r1"])
        self.assertEqual(lines(self.base), set())

    def test_each_memory_gets_the_state_db_of_its_sessions(self):
        self.run_sync()
        db = statedb.locate(self.store_of(self.apple))
        self.assertEqual(db, os.path.join(self.store_of(self.apple), "state.db"))
        got = statedb.read(db, lambda c: (c.execute("SELECT id, created_source, message_count, api_call_count, input_tokens, "
                                                    "cache_read_tokens, cache_write_tokens, output_tokens, started_at, ended_at, "
                                                    "git_repo_root, git_branch, cwd FROM sessions").fetchall(),
                                          c.execute("SELECT model, task, cost_status FROM session_model_usage ORDER BY model").fetchall()))
        self.assertEqual(got[0], [("s-apple", KEY, 2, 2, 6, 300, 100, 40, 1000.0, 1005.0, self.apple, "main",
                                   os.path.join(self.apple, "src"))])
        self.assertEqual(got[1], [("m-big", "main", "unknown"), ("m-small", "main", "unknown")])
        default = statedb.read(statedb.locate(self.base), lambda c: c.execute("SELECT id, git_repo_root FROM sessions ORDER BY id").fetchall())
        self.assertEqual(default, [("s-home", None), ("s-none", None), ("s-off", None)])

    def test_scopes_off_puts_everything_in_the_default_memory(self):
        sync.sync(KEY, None, self.base, on=False, exclude=(), home=self.home)
        self.assertEqual(len(self.sessions_of(self.base)), 6)
        self.assertEqual(scope.known(self.base), [])

    def test_the_home_folder_and_the_temporary_folders_are_no_project(self):
        self.assertEqual(sync.route(self.base, self.home, home=self.home), ("", "", self.base))
        self.assertEqual(sync.route(self.base, os.path.dirname(self.home), home=self.home), ("", "", self.base))
        with tempfile.TemporaryDirectory() as scratch:
            self.assertEqual(sync.route(self.base, scratch), ("", "", self.base))
        kind, root, folder = sync.route(self.base, self.apple, exclude=(), home=self.home)
        self.assertEqual((kind, root, folder), ("git", self.apple, self.store_of(self.apple)))
        self.assertEqual(sync.route(self.base, "relative/folder", exclude=(), home=self.home)[2], self.base)

    def test_a_project_file_in_the_home_folder_does_not_swallow_the_folders_under_it(self):
        open(os.path.join(self.home, "package.json"), "w").close()  # the home folder is then a source tree for scope.py
        plain = self.folder("work", "plain")
        self.assertEqual(sync.route(self.base, plain, exclude=(), home=self.home)[:2], ("dir", plain))
        self.assertEqual(sync.route(self.base, self.apple, exclude=(), home=self.home)[:2], ("git", self.apple))
        self.assertEqual(sync.route(self.base, self.home, exclude=(), home=self.home), ("", "", self.base))
        os.makedirs(os.path.join(self.home, ".git"))  # nor does a repository there
        self.assertEqual(sync.route(self.base, plain, exclude=(), home=self.home)[:2], ("dir", plain))

    def test_a_folder_that_is_gone_keeps_the_repository_above_it_and_nothing_else(self):
        gone = os.path.join(self.apple, "deleted", "deeper")
        self.assertEqual(sync.route(self.base, gone, exclude=(), home=self.home)[:2], ("git", self.apple))
        lost = os.path.join(self.notes, "deleted")  # only "the folder itself" is above it: that folder is not the project
        self.assertEqual(sync.route(self.base, lost, exclude=(), home=self.home), ("", "", self.base))

    def test_the_command_reads_the_environment_for_scopes_and_here(self):
        self.assertTrue(sync.enabled({}) and sync.enabled({"BREADCRUMBS_SCOPE": "auto"}))
        self.assertFalse(sync.enabled({"BREADCRUMBS_SCOPE": "off"}))
        self.assertTrue(sync.here({"BREADCRUMBS_HERE": "1"}) and not sync.here({}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--home", self.base, "sync", KEY]), 0)
        got = json.loads(out.getvalue())
        self.assertEqual((got["harness"], got["sessions"]), (KEY, 6))  # the project folders here are temporary folders
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--home", self.base, "sync", "nobody"]), 2)

    def test_a_call_in_a_project_looks_in_that_project_only_unless_a_store_is_named(self):
        self.run_sync()
        here = self.store_of(self.apple)
        c = core.Core(core.Stores(local=here, root=os.path.join(self.home, ".hermes"), here=True))
        try:
            found = c.call("find", {"q": "banana"})
            self.assertTrue(found["groups"] and all(g["store"] == "local" for g in found["groups"]))
            self.assertEqual(c.call("find", {"q": "cherry"})["groups"], [])  # another project: not here
            wide = c.call("find", {"q": "cherry", "store": "*"})["groups"]
            self.assertTrue(wide and wide[0]["store"] == scope.scope_id(self.pear))
            listed = c.call("stores")
            self.assertEqual(listed["default"], ["local"])
            names = {s["name"] for s in listed["stores"]}
            self.assertTrue({"local", "base", scope.scope_id(self.pear), scope.scope_id(self.notes)} <= names)
            self.assertNotIn(scope.scope_id(self.apple), names)  # it is "local": no second name for the same store
        finally:
            c.close()

    def test_a_project_with_no_memory_yet_finds_nothing_instead_of_everything(self):
        self.run_sync()
        c = core.Core(core.Stores(local=os.path.join(self.base, "scopes", "new-00000000"), root=os.path.join(self.home, ".hermes"),
                                  here=True))
        try:
            self.assertEqual(c.call("find", {"q": "banana"})["groups"], [])
            self.assertTrue(c.call("find", {"q": "banana", "store": "*"})["groups"])
        finally:
            c.close()


class Here(unittest.TestCase):
    """--here and BREADCRUMBS_HERE: which folder a command opens."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = os.path.realpath(tmp.name)
        self.shown = []
        ports._commands.append({"name": "probe", "add": lambda sub: sub.add_parser("probe"), "after": None, "before": None,
                                "hidden": True, "run": lambda args, home: self.shown.append(home) or 0})
        self.addCleanup(ports._commands.pop)
        old = {k: os.environ.pop(k, None) for k in ("BREADCRUMBS_HERE", "BREADCRUMBS_HOME", "BREADCRUMBS_SCOPE")}
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) for k, v in old.items() if v is not None])
        self.repo = os.path.join(self.base, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        self.here = sync.where(self.base, self.repo, exclude=(), home="/nonexistent")  # its memory under the base

    def probe(self, *argv, **env):
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in env])
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            self.assertEqual(cli.main(list(argv) + ["probe"]), 0)
        finally:
            os.chdir(cwd)
        return self.shown[-1]

    def test_a_command_opens_the_home_that_it_is_given(self):
        self.assertEqual(self.probe("--home", self.base), self.base)

    def test_here_opens_the_memory_of_the_current_folder_under_the_home(self):
        self.assertEqual(self.here, scope.store_home(self.base, self.repo))
        with unittest.mock.patch.object(sync, "no_project", return_value=[]), \
                unittest.mock.patch.object(sync, "_holds_home", return_value=False):
            self.assertEqual(self.probe("--here", "--home", self.base), self.here)
            self.assertEqual(self.probe(BREADCRUMBS_HERE="1", BREADCRUMBS_HOME=self.base), self.here)  # no --home: the base

    def test_a_home_named_on_the_command_line_wins_over_the_environment(self):
        # the child that `viz` starts: --home is its store, and the environment of the plugin still says here
        with unittest.mock.patch.object(sync, "no_project", return_value=[]):
            self.assertEqual(self.probe("--home", self.here, BREADCRUMBS_HERE="1"), self.here)


class Locate(unittest.TestCase):
    def test_the_state_db_beside_a_store_wins_over_the_one_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "profile", "breadcrumbs")
            os.makedirs(store)
            self.assertEqual(statedb.locate(store), os.path.join(tmp, "profile", "state.db"))  # none: the path beside
            open(os.path.join(store, "state.db"), "w").close()
            self.assertEqual(statedb.locate(store), os.path.join(store, "state.db"))
            open(os.path.join(tmp, "profile", "state.db"), "w").close()
            self.assertEqual(statedb.locate(store), os.path.join(tmp, "profile", "state.db"))

    def test_reading_a_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            statedb.read("/nonexistent/state.db", lambda c: None)


if __name__ == "__main__":
    unittest.main()
