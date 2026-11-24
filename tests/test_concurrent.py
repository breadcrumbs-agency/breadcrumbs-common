"""Two writers on one store: what holds when processes write the same files at the same time, and when one of them dies.

Each test starts real processes (sys.executable) on one folder. They wait for a file named "go", so they start their work
together. No test sleeps for a result: a parent waits for its children to end.
"""
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

from breadcrumbs.crumbs import Crumb, get_crumb, put_crumb
from breadcrumbs.history import Refs, commit, log
from breadcrumbs.store import Store

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRELUDE = """
import json, os, sys, time
home, me = sys.argv[1], int(sys.argv[2])
while not os.path.exists(os.path.join(home, "go")):
    time.sleep(0.001)
"""


def spawn(code, home, *args):
    env = dict(os.environ, PYTHONPATH=HERE, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.Popen([sys.executable, "-W", "ignore", "-c", PRELUDE + textwrap.dedent(code), home, *map(str, args)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def go(home):
    with open(os.path.join(home, "go"), "w"):
        pass


def together(case, code, home, n, *args, during=None):
    """Run n processes of `code` on home at once; during(): what the parent does while they run. Returns their outputs."""
    procs = [spawn(code, home, i, *args) for i in range(n)]
    go(home)
    if during:
        during(procs)
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        case.assertEqual(p.returncode, 0, err[-2000:])
        outs.append(out)
    return outs


class CommitTest(unittest.TestCase):
    CODE = """
    from breadcrumbs.crumbs import Crumb, put_crumb
    from breadcrumbs.history import Refs, commit
    from breadcrumbs.store import Store
    store, refs = Store(os.path.join(home, "store")), Refs(os.path.join(home, "refs"))
    for i in range(int(sys.argv[3])):
        root = put_crumb(store, Crumb("turn", "w%d n%d" % (me, i)))
        commit(store, refs, "main", [root], "w%d n%d" % (me, i))
    store.close()
    """

    def test_the_commits_of_four_processes_are_all_in_the_chain(self):
        # two writers that read the same head must not both write a commit over it: one of the two was lost
        with tempfile.TemporaryDirectory() as home:
            together(self, self.CODE, home, 4, 40)
            store, refs = Store(os.path.join(home, "store"), readonly=True), Refs(os.path.join(home, "refs"), readonly=True)
            chain = list(log(store, refs.get("main")))
            store.close()
        self.assertEqual(len(chain), 160)
        self.assertEqual({c.meta["msg"] for _, c in chain}, {"w%d n%d" % (w, i) for w in range(4) for i in range(40)})

    def test_a_head_names_a_commit_that_is_on_disk(self):
        # the commit reaches the disk before its ref: a second process that reads the ref can read the commit at once
        with tempfile.TemporaryDirectory() as home:
            store, refs = Store(os.path.join(home, "store")), Refs(os.path.join(home, "refs"))
            head = commit(store, refs, "main", [put_crumb(store, Crumb("turn", "one"))], "one")
            other = Store(os.path.join(home, "store"), readonly=True)  # the writer has not closed and has not flushed
            self.assertEqual(get_crumb(other, refs.get("main")).meta["msg"], "one")
            self.assertEqual(get_crumb(other, get_crumb(other, head).kids[0]).title, "one")
            other.close()
            store.close()


class RefTest(unittest.TestCase):
    CODE = """
    from breadcrumbs.history import Refs
    refs = Refs(os.path.join(home, "refs"))
    for i in range(int(sys.argv[3])):
        refs.set("head", "%08x" % (me * 100000 + i))
    """

    def test_a_ref_that_four_processes_set_is_never_empty_and_no_writer_fails(self):
        with tempfile.TemporaryDirectory() as home:
            refs = Refs(os.path.join(home, "refs"))
            refs.set("head", "00000000")
            seen, stop = [], threading.Event()

            def read():
                while not stop.is_set():
                    seen.append(refs.get("head"))

            reader = threading.Thread(target=read)

            def during(procs):
                reader.start()

            try:
                together(self, self.CODE, home, 4, 300, during=during)  # a writer that lost its temporary file fails here
            finally:
                stop.set()
                reader.join()
            self.assertTrue(seen)
            self.assertEqual([v for v in seen if v is None or len(v) != 8], [])
            self.assertEqual(refs.names(), ["head"])  # no temporary file is a ref
            self.assertEqual([n for n in os.listdir(refs.path) if n.endswith(".tmp")], [])

    def test_eight_threads_set_one_ref(self):
        with tempfile.TemporaryDirectory() as home:
            refs, errors = Refs(os.path.join(home, "refs")), []

            def work(k):
                try:
                    for i in range(200):
                        refs.set("head", "%08x" % (k * 1000 + i))
                except Exception as e:  # noqa: BLE001 - the test reports any failure of a writer
                    errors.append(repr(e))

            threads = [threading.Thread(target=work, args=(k,)) for k in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(refs.get("head")), 8)


class StoreTest(unittest.TestCase):
    CODE = """
    import random
    from breadcrumbs.store import Store
    rnd = random.Random(me)
    store, ids = Store(os.path.join(home, "store")), {}
    for i in range(int(sys.argv[3])):
        for text in ("shared line %d\\n" % i, "writer %d line %d\\n" % (me, i)):
            ids[text] = store.put(text.encode())
        if rnd.random() < 0.02:
            store.flush()
    store.close()
    print(json.dumps(ids))
    """

    def test_four_writers_of_one_store_lose_no_blob_and_agree_on_the_ids(self):
        with tempfile.TemporaryDirectory() as home:
            outs = [json.loads(o) for o in together(self, self.CODE, home, 4, 1500)]
            store = Store(os.path.join(home, "store"), readonly=True)
            try:
                for ids in outs:
                    for text, blob_id in ids.items():
                        self.assertEqual(store.get(blob_id), text.encode())
                shared = [{t: i for t, i in ids.items() if t.startswith("shared")} for ids in outs]
                self.assertTrue(all(s == shared[0] for s in shared))  # the same bytes have one id for every writer
                self.assertEqual(len(store), 1500 + 4 * 1500)  # each distinct blob once, no dead copy counted
            finally:
                store.close()


class CrashTest(unittest.TestCase):
    CODE = """
    from breadcrumbs.crumbs import Crumb, put_crumb
    from breadcrumbs.history import Refs, commit
    from breadcrumbs.lines import put_text
    from breadcrumbs.store import Store
    store, refs = Store(os.path.join(home, "store")), Refs(os.path.join(home, "refs"))
    i = 0
    while True:
        i += 1
        doc = put_text(store, ("round %d of writer %d\\n" % (i, me)).encode() * 40)
        root = put_crumb(store, Crumb("turn", "t%d" % i, ranges=[[doc, 0, 40]]))
        commit(store, refs, "main", [root], "n%d" % i)
        refs.set("sess-%d" % me, root)
    """

    def test_a_writer_that_is_killed_leaves_heads_and_chains_that_can_be_read(self):
        rnd = random.Random(7)
        with tempfile.TemporaryDirectory() as home:
            go(home)
            for round_ in range(6):  # each round: two writers on the store that the killed ones left
                procs = [spawn(self.CODE, home, w) for w in range(2)]
                grown = os.path.join(home, "refs", "main")
                deadline = time.monotonic() + 20
                while not os.path.exists(grown) and time.monotonic() < deadline:
                    time.sleep(0.005)
                time.sleep(rnd.uniform(0.02, 0.12))  # the moment of the kill: anywhere in a put, a flush or a ref write
                for p in procs:
                    p.send_signal(signal.SIGKILL)
                for p in procs:
                    p.communicate()
                store, refs = Store(os.path.join(home, "store"), readonly=True), Refs(os.path.join(home, "refs"))
                try:
                    head = refs.get("main")
                    self.assertTrue(head, "round %d" % round_)
                    n = 0
                    for _cid, c in log(store, head):  # every commit of the chain and its root are on disk
                        n += 1
                        for kid in c.kids:
                            for doc, a, b in get_crumb(store, kid).ranges:
                                self.assertEqual(len(store.get(doc)) > 0, True)
                    self.assertGreater(n, 0)
                finally:
                    store.close()
            store = Store(os.path.join(home, "store"))  # and a writer can go on: the torn tail of the index is cut
            store.put(b"after the crashes\n")
            store.close()
            self.assertEqual(os.path.getsize(os.path.join(home, "store", "blobs.idx")) % 24, 0)


class ConfigTest(unittest.TestCase):
    CODE = """
    from breadcrumbs import dials
    for i in range(int(sys.argv[3])):
        dials.set_setting(home, "ace_tokens", 1000 + i, model="w%d-m%d" % (me, i))
    """

    def test_the_settings_of_four_processes_are_all_in_the_file(self):
        with tempfile.TemporaryDirectory() as home:
            together(self, self.CODE, home, 4, 25)
            with open(os.path.join(home, "config.json"), encoding="utf-8") as f:
                models = json.load(f)["models"]
        self.assertEqual(len(models), 100)
        self.assertEqual({k: v["ace_tokens"] for k, v in models.items()},
                         {"w%d-m%d" % (w, i): 1000 + i for w in range(4) for i in range(25)})


class UsageTest(unittest.TestCase):
    CODE = """
    from breadcrumbs import usage
    records = [usage.record("test", "s1", "req-%d" % i, 1700000000 + i, "m", "p", 10, 0, 0, 1, 0) for i in range(200)]
    print(usage.append(home, records))
    """

    def test_four_ingests_of_the_same_requests_add_each_request_once(self):
        with tempfile.TemporaryDirectory() as home:
            added = [int(o) for o in together(self, self.CODE, home, 4)]
            with open(os.path.join(home, "usage.log"), encoding="utf-8") as f:
                lines = f.read().splitlines()
        self.assertEqual(sum(added), 200)
        self.assertEqual(len(lines), 200)
        self.assertEqual(len({line.split(" req=")[1].split()[0] for line in lines}), 200)


class ScopeTest(unittest.TestCase):
    CODE = """
    from breadcrumbs import scope
    for i in range(50):
        scope.ensure(os.path.join(home, "base"), "git", os.path.join(home, "project-%d" % i))
    """

    def test_four_processes_make_the_same_scopes(self):
        with tempfile.TemporaryDirectory() as home:
            together(self, self.CODE, home, 4)
            from breadcrumbs import scope
            known = scope.known(os.path.join(home, "base"))
            self.assertEqual(len(known), 50)
            left = [n for sc in known for n in os.listdir(sc["path"]) if n.endswith(".tmp")]
            self.assertEqual(left, [])


class LaterTest(unittest.TestCase):
    def test_a_bulk_ingest_sets_no_session_ref_before_its_store_is_flushed(self):
        from breadcrumbs.index import Index
        from breadcrumbs.ingest import ingest_sessions

        class Watch(Refs):
            """Refs that note, at each set, whether the crumb is on disk for a second process."""
            seen = []

            def set(self, name, crumb_id, sync=True):
                other = Store(os.path.join(os.path.dirname(self.path), "store"), readonly=True)
                try:
                    self.seen.append((name, crumb_id in other))
                finally:
                    other.close()
                super().set(name, crumb_id, sync)

        with tempfile.TemporaryDirectory() as home:
            store, index = Store(os.path.join(home, "store")), Index(os.path.join(home, "index.db"))
            refs = Watch(os.path.join(home, "refs"))
            rows = [{"role": "user", "content": "the kettle is green", "ts": 1}, {"role": "assistant", "content": "yes", "ts": 2}]
            ingest_sessions(store, index, refs, [("s%d" % i, "t", {}, rows) for i in range(5)], "test")
            index.close()
            store.close()
        self.assertEqual(len(Watch.seen), 6)  # main, then the five sessions
        self.assertEqual([name for name, _ in Watch.seen][0], "main")
        self.assertEqual([on_disk for _, on_disk in Watch.seen], [True] * 6)

    def test_later_gives_the_waiting_value_and_writes_at_its_flush(self):
        from breadcrumbs.history import Later
        with tempfile.TemporaryDirectory() as home:
            refs = Refs(os.path.join(home, "refs"))
            refs.set("old", "00000001")
            later = Later(refs)
            later.set("new", "00000002")
            later.set("old", "00000003")
            self.assertEqual((later.get("new"), later.get("old"), later.get("none")), ("00000002", "00000003", None))
            self.assertEqual((refs.get("new"), refs.get("old")), (None, "00000001"))  # nothing is written yet
            self.assertEqual(later.names(), ["new", "old"])
            with self.assertRaises(ValueError):
                later.set("bad/name", "00000004")
            later.flush()
            self.assertEqual((refs.get("new"), refs.get("old")), ("00000002", "00000003"))
            later.flush()  # a second flush has nothing to write


if __name__ == "__main__":
    unittest.main()
