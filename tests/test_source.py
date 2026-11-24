"""A text that a harness keeps (lines.put_text with a locator): the store holds its spine and the locator, no line;
doc_lines reads it through the port "source:<harness>", checks it against the spine and shows it as the harness's
scrub masks it, one line for each line of the spine. relocate turns the copies of a store into locators."""
import os
import re
import tempfile
import unittest

from breadcrumbs import ports
from breadcrumbs.lines import SOURCE_GONE, get_lines, get_text, masked_lines, put_text, relocate, split_lines
from breadcrumbs.store import Store

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
            self.assertEqual(relocate(path, texts), 2)  # two lines only the located texts used
            store = Store(path)
            try:
                self.assertEqual(len(ids) - len(set(store.ids())), 2)
                self.assertEqual((store.locator(kept), store.locator(masked), store.locator(own)),
                                 ("test:1", "test:2", None))
                self.assertIsNotNone(store.find(b"line two\n"))  # still used by the text of its own
                self.assertEqual(get_text(store, own), b"a summary only this store holds\nline two\n")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
