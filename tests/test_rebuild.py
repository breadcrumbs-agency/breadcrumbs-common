import hashlib
import os
import tempfile
import unittest
from unittest import mock

from breadcrumbs import tags
from breadcrumbs.crumbs import Crumb, get_crumb, is_crumb, put_crumb
from breadcrumbs.history import Refs, commit, log
from breadcrumbs.index import Index
from breadcrumbs.ingest import leaves, put_tree, put_turn
from breadcrumbs.lines import get_text, split_lines
from breadcrumbs.mask import mask
from breadcrumbs.rebuild import rebuild
from breadcrumbs.store import Store

KEY = "sk-" + "proj-" + "Qm4Zr8TnV2xLc9HbYw3Ek7UaPd5Rs"  # joined here: no line of this file is a key a scanner stops
BODY = KEY[-14:]


def write_before_the_mask(home):
    """A memory as the versions before mask.py wrote it: the key is in a blob, a title, an index row, a receipt
    (q, ctx and items), a summary, and the message of a tag commit names the turn that holds it."""
    store, index, refs = (Store(os.path.join(home, "store")), Index(os.path.join(home, "index.db")),
                          Refs(os.path.join(home, "refs")))
    with mock.patch("breadcrumbs.lines.mask", lambda x: x), mock.patch("breadcrumbs.ingest.mask", lambda x: x), \
            mock.patch("breadcrumbs.index.mask", lambda x: x):
        said = put_turn(store, index, "user", "walrus kettle %s\nplease keep it" % KEY, "s1", 100)
        answer = put_turn(store, index, "assistant", "saved the walrus kettle", "s1", 101, prev=said)
        sess = put_tree(store, "sess", "user: walrus kettle %s" % KEY[:30], [said, answer], meta={"sess": "s1"})
        refs.set("sess-s1", sess)
        doc = get_crumb(store, said).ranges[0][0]
        put_crumb(store, Crumb("rcpt", "recall", ranges=[[doc, 0, 2]], meta={
            "sess": "s1", "ctx": doc, "items": [said, "%s:0-1" % doc], "q": "walrus kettle %s" % KEY, "prev": ""}))
        commit(store, refs, "main", [sess], "turn s1", ts=102)
        index.set_summary(sess, "the user gave a key %s" % KEY)
        index.touch(said, 103)
        store.flush()
        tags.supersede(store, index, refs, said, answer, ts=104)
        store.flush()
        index.commit()
    index.close()
    store.close()
    return {"said": said, "answer": answer, "sess": sess, "doc": doc}


def read(*path):
    with open(os.path.join(*path), "rb") as f:
        return f.read()


def blobs(home):
    """Every blob of a store, as text: blobs.pack is compressed, so its bytes show no key either way."""
    store = Store(os.path.join(home, "store"), readonly=True)
    try:
        return b"\n".join(bytes(store.get(i)) for i in store.ids())
    finally:
        store.close()


def hits(home, words):
    index = Index(os.path.join(home, "index.db"))
    try:
        return index.search(words)
    finally:
        index.close()


class RebuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src, self.dst = os.path.join(self.tmp.name, "src"), os.path.join(self.tmp.name, "dst")
        self.old = write_before_the_mask(self.src)
        self.before = hashlib.sha256(read(self.src, "store", "blobs.pack")).hexdigest()
        self.stats = rebuild(self.src, self.dst)
        self.store = Store(os.path.join(self.dst, "store"))
        self.refs = Refs(os.path.join(self.dst, "refs"))
        self.index = Index(os.path.join(self.dst, "index.db"))

    def tearDown(self):
        self.index.close()
        self.store.close()
        self.tmp.cleanup()

    def test_the_old_memory_held_the_key_and_the_new_one_holds_none(self):
        self.assertIn(BODY.encode(), blobs(self.src))
        self.assertTrue(hits(self.src, KEY[8:]))
        self.assertNotIn(BODY.encode(), blobs(self.dst))
        self.assertFalse(hits(self.dst, KEY[8:]))

    def test_the_old_memory_is_not_changed(self):
        self.assertEqual(hashlib.sha256(read(self.src, "store", "blobs.pack")).hexdigest(), self.before)

    def test_ids_follow_the_text_and_a_clean_crumb_keeps_its_id(self):
        sess = self.refs.get("sess-s1")
        turns = leaves(self.store, sess)
        self.assertNotEqual(turns[0], self.old["said"])
        self.assertEqual(turns[1], self.old["answer"])  # no key in it: the same bytes, the same id
        doc = get_crumb(self.store, turns[0]).ranges[0][0]
        self.assertEqual(get_text(self.store, doc).decode(), mask("walrus kettle %s\nplease keep it" % KEY))
        self.assertEqual(len(split_lines(get_text(self.store, doc))), 2)
        self.assertNotIn(BODY, get_crumb(self.store, sess).title)

    def test_a_commit_names_the_new_session_and_a_tag_message_the_new_ids(self):
        main = [c for _, c in log(self.store, self.refs.get("main"))]
        self.assertEqual(len(main), 1)
        self.assertEqual(main[0].kids, [self.refs.get("sess-s1")])
        tag = [c for _, c in log(self.store, self.refs.get("tags"))][0]
        self.assertNotIn(self.old["said"], tag.meta["msg"])
        self.assertIn(self.old["answer"], tag.meta["msg"])

    def test_a_receipt_names_the_new_text_and_holds_no_key(self):
        found = (get_crumb(self.store, i) for i in self.store.ids() if is_crumb(self.store.get(i)))
        rcpt = next(c for c in found if c.kind == "rcpt")
        self.assertNotIn(BODY, rcpt.meta["q"])
        self.assertEqual(rcpt.meta["ctx"], rcpt.ranges[0][0])
        self.assertNotEqual(rcpt.meta["ctx"], self.old["doc"])
        self.assertEqual(rcpt.meta["items"][1], "%s:0-1" % rcpt.meta["ctx"])

    def test_the_index_is_made_again_from_the_new_ids(self):
        hits = self.index.search("walrus kettle")
        self.assertTrue(hits)
        for crumb, ref, _ in hits:
            self.assertIn(crumb, self.store)
            self.assertIn(ref.partition(":")[0], self.store)
        self.assertEqual(self.index.search(BODY), [])
        sess = self.refs.get("sess-s1")
        first = leaves(self.store, sess)[0]
        self.assertEqual(self.index.summary_of(sess), mask("the user gave a key %s" % KEY))
        self.assertEqual(self.index.heat([first]).get(first), (103, 1))
        near = self.index.db.execute("SELECT a, b FROM nbr").fetchall()
        self.assertEqual(len(near), 2)
        for a, b in near:
            self.assertIn(a, self.store)
            self.assertIn(b, self.store)
        keys = [k for (k,) in self.index.db.execute("SELECT key FROM tags")]
        self.assertTrue(keys)
        for k in keys:
            self.assertIn(k.partition(":")[0], self.store)

    def test_a_memory_with_no_key_is_rebuilt_to_the_same_ids(self):
        again = os.path.join(self.tmp.name, "again")
        stats = rebuild(self.dst, again)
        self.assertEqual((stats["changed_crumbs"], stats["changed_texts"]), (0, 0))
        self.assertEqual(Refs(os.path.join(again, "refs")).get("sess-s1"), self.refs.get("sess-s1"))

    def test_a_folder_that_holds_something_is_not_written(self):
        with self.assertRaises(FileExistsError):
            rebuild(self.src, self.dst)

    def test_it_counts_what_changed(self):
        self.assertGreater(self.stats["changed_crumbs"], 0)
        self.assertGreater(self.stats["changed_texts"], 0)


if __name__ == "__main__":
    unittest.main()
