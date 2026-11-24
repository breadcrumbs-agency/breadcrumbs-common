import os
import tempfile
import unittest

from breadcrumbs.index import Index
from breadcrumbs.ingest import put_rows, put_turn
from breadcrumbs.lines import get_text, put_text, split_lines
from breadcrumbs.mask import mask
from breadcrumbs.crumbs import get_crumb
from breadcrumbs.store import Store

# Fake keys in the real formats (random, valid nowhere). Each is a prefix and a body joined here, so that no
# line of this file is a key that a secret scanner would stop a push for.
_BODY = "Qm4Zr8TnV2xLc9HbYw3Ek7UaPd5Rs"
KEYS = tuple(prefix + _BODY[:n] for prefix, n in (
    ("sk-proj-", 24), ("sk_" + "live_", 24), ("gh" + "p_", 29), ("AK" + "IA", 16), ("xo" + "xb-123456789012-", 14),
    ("hf_", 29), ("sk-ant-api03-", 29)))
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0Z\nq1w2e3r4t5y6u7i8o9p0\n-----END RSA PRIVATE KEY-----"


class MaskTest(unittest.TestCase):
    def test_each_shape_keeps_its_prefix_and_length(self):
        for key in KEYS:
            out = mask("my key %s, thanks" % key)
            self.assertNotIn(key[-12:], out, key)
            self.assertEqual(len(out), len("my key %s, thanks" % key), key)
            self.assertTrue(out.startswith("my key " + key[:3]), key)

    def test_a_private_key_block_keeps_its_markers_and_line_ends(self):
        out = mask("before\n" + PEM + "\nafter")
        self.assertEqual(out.count("\n"), PEM.count("\n") + 2)
        self.assertIn("-----BEGIN RSA PRIVATE KEY-----\n", out)
        self.assertIn("\n-----END RSA PRIVATE KEY-----", out)
        self.assertNotIn("MIIEow", out)
        self.assertNotIn("q1w2e3", out)

    def test_idempotent_and_bytes_equal_str(self):
        text = "a %s b\n%s\n" % (KEYS[0], PEM)
        once = mask(text)
        self.assertEqual(mask(once), once)
        self.assertEqual(mask(text.encode()), once.encode())

    def test_plain_text_is_unchanged(self):
        text = "sk-short, id 0f228567:0-12, MAX_TOKENS=100, a.b.c, skeleton-key-of-the-matter-is-long\n"
        self.assertEqual(mask(text), text)

    def test_line_count_is_the_unmasked_count(self):
        text = ("x " * 100 + KEYS[0] + " " + "y " * 100 + "\n" + PEM + "\n").encode()
        self.assertEqual(len(split_lines(mask(text))), len(split_lines(text)))


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "store"))
        self.index = Index(os.path.join(self.tmp.name, "index.db"))

    def tearDown(self):
        self.index.close()
        self.store.close()
        self.tmp.cleanup()

    def blobs(self):
        self.store.flush()
        return b"\n".join(bytes(self.store.get(i)) for i in self.store.ids())

    def test_a_typed_key_reaches_no_blob_title_or_index_row(self):
        said = "Here is my OpenAI key %s and my Stripe key %s. Remember them." % (KEYS[0], KEYS[1])
        cid = put_turn(self.store, self.index, "user", said, "s1")
        self.index.commit()
        for key in KEYS[:2]:
            self.assertNotIn(key[-12:].encode(), self.blobs())
        self.assertNotIn("Qm4Zr8TnV2xLc9HbYw3Ek7Ua", get_crumb(self.store, cid).title)
        self.assertEqual(self.index.search("Qm4Zr8TnV2xLc9HbYw3Ek7Ua"), [])
        self.assertTrue(self.index.search("Remember Stripe"))
        doc = get_crumb(self.store, cid).ranges[0][0]
        self.assertEqual(get_text(self.store, doc).decode(), mask(said))

    def test_put_text_masks_for_a_caller_that_skips_put_turn(self):
        put_text(self.store, ("slot recall: %s\n%s\n" % (KEYS[2], PEM)).encode())
        for secret in (KEYS[2][-12:], "MIIEow", "q1w2e3"):
            self.assertNotIn(secret.encode(), self.blobs())

    def test_a_tool_row_and_an_index_add_are_masked(self):
        put_rows(self.store, self.index, [{"role": "tool", "content": "env\n%s\n" % KEYS[5], "tool_name": "x"}], "s2")
        self.index.add("c1", "d:0-1", "again %s" % KEYS[5])
        self.index.commit()
        self.assertNotIn(KEYS[5][-12:].encode(), self.blobs())
        self.assertEqual(self.index.search(KEYS[5][3:]), [])


if __name__ == "__main__":
    unittest.main()
