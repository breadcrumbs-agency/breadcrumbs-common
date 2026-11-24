"""A text that a harness keeps (lines.put_text with a locator): the store holds its spine and the locator, no line;
doc_lines reads it through the port "source:<harness>", checks it against the spine and shows it as the harness's
scrub masks it, one line for each line of the spine. relocate turns the copies of a store into locators."""
import os
import re
import tempfile
import unittest

from breadcrumbs import ports
from breadcrumbs.lines import SOURCE_GONE, get_lines, get_text, masked_lines, put_text, relocate, split_lines
from breadcrumbs.crumbs import Crumb, get_crumb, put_crumb
from breadcrumbs.index import Index
from breadcrumbs.ingest import put_rows, put_tree, title_of
from breadcrumbs.store import Store, StoreBusy, old

KEY = "sk-proj-" + "Q7f2" * 12


def scrub(text):
    """A scrub that masks a key, and a span "BEGIN ... END" over line ends as one line (as a PEM mask does)."""
    text = text.replace(KEY, KEY[:6] + "..." + KEY[-4:])
    return re.sub(r"BEGIN.*?END", "[block]", text, flags=re.S)


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "store"))
        self.addCleanup(self.store.close)
        self.rows = {}  # the harness: locator -> its bytes as it keeps them
        saved = ports.get("source:test")
        self.addCleanup(ports.give, "source:test", saved)
        ports.give("source:test", lambda store, loc: (self.rows[loc], scrub(self.rows[loc].decode()).encode(), scrub)
                   if loc in self.rows else None)

    def keep(self, loc, raw, stored=None):
        """The harness keeps raw at loc; the store gets the spine of stored (raw by default) and the locator."""
        self.rows[loc] = raw
        return put_text(self.store, stored if stored is not None else raw, loc)

    def test_no_line_is_stored_and_the_text_reads_back(self):
        doc = self.keep("test:1", b"one\ntwo\n")
        self.assertIsNone(self.store.find(b"one\n"))
        self.assertEqual(self.store.locator(doc), "test:1")
        self.assertEqual(get_text(self.store, doc), b"one\ntwo\n")

    def test_a_spine_of_the_raw_text_reads_masked(self):
        doc = self.keep("test:1", ("my key %s\nnext\n" % KEY).encode())
        self.assertEqual(get_lines(self.store, doc), [("my key %s\n" % scrub(KEY)).encode(), b"next\n"])

    def test_a_spine_of_the_masked_text_reads_masked(self):
        raw = ("my key %s\nnext\n" % KEY).encode()
        doc = self.keep("test:1", raw, scrub(raw.decode()).encode())
        self.assertEqual(get_text(self.store, doc), scrub(raw.decode()).encode())

    def test_a_mask_over_line_ends_keeps_one_line_for_each_line_of_the_spine(self):
        raw = b"head\nBEGIN\nsecret body\nEND\ntail\n"
        doc = self.keep("test:1", raw)
        got = get_lines(self.store, doc)
        self.assertEqual(len(got), 5)
        self.assertEqual(got[0], b"head\n")
        self.assertEqual(got[4], b"tail\n")
        self.assertNotIn(b"secret", b"".join(got))
        self.assertEqual(get_lines(self.store, doc, 2, 3), [b"***\n"])  # a range in the span still aligns

    def test_a_row_changed_in_place_with_its_line_count_is_shown(self):
        doc = self.keep("test:1", b"one\ntwo\n")
        self.rows["test:1"] = b"one\nTWO\n"
        self.assertEqual(get_text(self.store, doc), b"one\nTWO\n")

    def test_a_row_that_is_gone_or_changed_in_count_reads_as_source_gone(self):
        doc = self.keep("test:1", b"one\ntwo\n")
        self.rows["test:1"] = b"one\n"
        self.assertEqual(get_lines(self.store, doc), [SOURCE_GONE, b"\n"])
        del self.rows["test:1"]
        self.assertEqual(get_lines(self.store, doc), [SOURCE_GONE, b"\n"])

    def test_masked_lines_without_a_scrub_are_the_lines(self):
        lines = split_lines(b"a\nb\n")
        self.assertIs(masked_lines(lines, None), lines)


class RelocateTest(unittest.TestCase):
    def test_copies_become_locators_once_and_every_id_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "store")
            store = Store(path)
            kept = put_text(store, b"the harness keeps this\nline two\n")
            masked = put_text(store, ("key %s\n" % scrub(KEY)).encode())
            own = put_text(store, b"a summary only this store holds\nline two\n")  # shares "line two"
            ids = set(store.ids())
            store.close()
            texts = [("test:1", (b"the harness keeps this\nline two\n",)),
                     ("test:2", (("key %s\n" % KEY).encode(), lambda: ("key %s\n" % scrub(KEY)).encode())),
                     ("test:3", (b"a row this store never held\n",))]
            located, dropped = relocate(path, texts)
            self.assertEqual((dropped, set(located.values())), (2, {"test:1", "test:2"}))  # lines only they used
            self.assertFalse(old(path))
            store = Store(path)
            try:
                self.assertEqual(len(ids) - len(set(store.ids())), 2)
                self.assertEqual((store.locator(kept), store.locator(masked), store.locator(own)),
                                 ("test:1", "test:2", None))
                self.assertIsNotNone(store.find(b"line two\n"))  # still used by the text of its own
                self.assertEqual(get_text(store, own), b"a summary only this store holds\nline two\n")
            finally:
                store.close()

    def test_a_store_open_anywhere_is_not_converted_and_none_opens_while_it_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "store")
            store = Store(path)
            put_text(store, b"x\n")
            store.flush()
            with self.assertRaises(StoreBusy):
                relocate(path, [])
            store.close()
            relocate(path, [])
            Store(path).close()

    def test_old_is_a_store_with_blobs_and_no_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "store")
            store = Store(path)
            put_text(store, b"x\n")
            store.close()
            self.assertFalse(old(path))  # a new store has its format
            os.remove(os.path.join(path, "format"))  # as a release before wrote it
            self.assertTrue(old(path))
            relocate(path, [])
            self.assertFalse(old(path))


class OutsideTest(unittest.TestCase):
    """A text that a harness keeps leaves no words in the index or in a title: the harness's own search and counts
    stand in (find:<harness>, vocab:<harness>), and a view reads the title from the harness."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "store"))
        self.addCleanup(self.store.close)
        self.index = Index(os.path.join(self.tmp.name, "index.db"))
        self.addCleanup(self.index.close)
        self.rows = {"test:1": b"where is the kettle\n", "test:2": b"on the top shelf\n"}
        for name, port in (("source:test", lambda store, loc: (self.rows[loc], self.rows[loc], None)
                            if loc in self.rows else None),
                           ("find:test", lambda root, query, k: [(loc, 2.5) for loc, t in self.rows.items()
                                                                 if b"kettle" in t and '"kettl"*' in query]),
                           ("vocab:test", lambda root, stems: (2, 7, 6, lambda stem: (1, 1) if stem == "kettl" else (0, 0)))):
            saved = ports.get(name)
            self.addCleanup(ports.give, name, saved)
            ports.give(name, port)
        turns, _ = put_rows(self.store, self.index, [
            {"role": "user", "content": "where is the kettle\n", "loc": "test:1"},
            {"role": "assistant", "content": "on the top shelf\n", "loc": "test:2"},
            {"role": "user", "content": "a note of this store only, kettle too"}], "s1")
        self.turns = turns

    def test_no_word_of_a_harness_text_is_in_the_index_or_its_title(self):
        self.index.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.v USING fts5vocab('main', 't', 'row')")
        terms = {t for (t,) in self.index.db.execute("SELECT term FROM temp.v")}
        self.assertNotIn("shelf", terms)
        self.assertIn("note", terms)
        self.assertEqual(get_crumb(self.store, self.turns[0]).title, "user")

    def test_search_finds_both_and_counts_add_the_harness(self):
        found = [c for c, _r, _s in self.index.search("kettle")]
        self.assertEqual(set(found), {self.turns[0], self.turns[2]})
        self.index._fresh()
        self.assertEqual(self.index._count("kettl"), (2, 2))

    def test_a_view_shows_the_title_from_the_harness(self):
        self.assertEqual(title_of(self.store, get_crumb(self.store, self.turns[0])), "user: where is the kettle")
        sess = get_crumb(self.store, put_tree(self.store, "sess", "", self.turns))
        self.assertEqual(title_of(self.store, sess), "user: where is the kettle")
        self.assertEqual(title_of(self.store, get_crumb(self.store, self.turns[2])),
                         "user: a note of this store only, kettle too")

    def test_index_relocate_drops_the_words_of_located_texts(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Index(os.path.join(tmp, "index.db"))
            try:
                index.add("c1", "d1:0-1", "copied shelf words")
                index.add("c2", "d2:0-1", "summary words")
                self.assertEqual(index.relocate({"d1": "test:9"}, lambda ref: "summary words"), 1)
                self.assertEqual([c for c, _r, _s in index.search("shelf")], [])
                self.assertEqual([c for c, _r, _s in index.search("summary")], ["c2"])
            finally:
                index.close()


if __name__ == "__main__":
    unittest.main()
