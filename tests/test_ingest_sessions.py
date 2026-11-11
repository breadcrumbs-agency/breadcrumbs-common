import contextlib
import io
import os
import tempfile
import unittest

from breadcrumbs import cli, ports
from breadcrumbs.history import Refs, log
from breadcrumbs.index import Index
from breadcrumbs.ingest import ingest_sessions, leaves
from breadcrumbs.store import Store

ROWS = [{"role": "user", "content": "how hot for green tea?", "ts": 1}, {"role": "assistant", "content": "80 C", "ts": 2}]


def sessions(path=None):
    yield "s/1", "tea", {"source": "fake"}, ROWS
    yield "s2", "", {}, []  # no turn: no session
    yield "s3", "", {}, [dict(ROWS[0])]


class IngestSessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = dict(ports._harnesses)

    def tearDown(self):
        ports._harnesses.clear()
        ports._harnesses.update(self.saved)
        self.tmp.cleanup()

    def stored(self, fn):
        store, index, refs = (Store(os.path.join(self.tmp.name, "store")), Index(os.path.join(self.tmp.name, "index.db")),
                              Refs(os.path.join(self.tmp.name, "refs")))
        try:
            return fn(store, index, refs)
        finally:
            index.close()
            store.close()

    def test_the_sessions_become_session_crumbs_under_one_commit(self):
        def run(store, index, refs):
            stats = ingest_sessions(store, index, refs, sessions(), "ingest fake")
            self.assertEqual({k: stats[k] for k in ("sessions", "messages", "bytes")}, {"sessions": 2, "messages": 3, "bytes": 48})
            self.assertEqual(len(leaves(store, refs.get("sess-s_1"))), 2)  # the ref name has only letters that a ref may have
            self.assertIsNone(refs.get("sess-s2"))
            commits = list(log(store, refs.get("main")))
            self.assertEqual([(len(c.kids), c.meta["msg"]) for _cid, c in commits], [(2, "ingest fake")])
        self.stored(run)

    def test_the_ingest_command_runs_the_source_of_the_harness(self):
        seen = []
        ports.harness("fake", "Fake", ("fake",), lambda cmd: "", order=99, source=lambda path: seen.append(path) or sessions(path))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--home", self.tmp.name, "ingest", "fake", "/x/state.db"]), 0)
            self.assertEqual(cli.main(["--home", self.tmp.name, "ingest", "fake"]), 0)
        self.assertEqual(seen, ["/x/state.db", None])
        self.assertIn('"sessions": 2', out.getvalue())
        self.stored(lambda store, index, refs: self.assertEqual(
            [c.meta["msg"] for _cid, c in log(store, refs.get("main"))], ["ingest fake", "ingest state.db"]))

    def test_the_ingest_command_refuses_an_unknown_harness_and_one_without_a_source(self):
        ports.harness("bare", "Bare", ("bare",), lambda cmd: "", order=98)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--home", self.tmp.name, "ingest", "nobody"]), 2)
            self.assertEqual(cli.main(["--home", self.tmp.name, "ingest", "bare"]), 1)
        self.assertIn("harness is one of", err.getvalue())
        self.assertIn("bare has no session source", err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "store")))  # nothing opened

    def test_mcp_add_is_the_setup_line_of_a_cli(self):
        self.assertEqual(ports.mcp_add("x")(["env", "A=b c", "py"]), "x mcp add breadcrumbs -- env 'A=b c' py")


if __name__ == "__main__":
    unittest.main()
