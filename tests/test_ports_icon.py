"""The icon of a harness: ports.harness takes it, ports.image reads it from a file beside the adapter."""
import base64
import os
import tempfile
import unittest

from breadcrumbs import ports

PNG = b"\x89PNG\r\n\x1a\n" + b"not a whole image, but the bytes are what travels"


class IconTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(ports._harnesses)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.adapter = os.path.join(self.dir.name, "adapter.py")

    def tearDown(self):
        ports._harnesses.clear()
        ports._harnesses.update(self.saved)

    def put(self, name, data):
        with open(os.path.join(self.dir.name, name), "wb") as f:
            f.write(data)

    def test_image_is_a_data_uri_of_the_bytes_of_the_file_beside_the_adapter(self):
        self.put("icon.png", PNG)
        uri = ports.image(self.adapter, "icon.png")
        head, _, body = uri.partition(",")
        self.assertEqual(head, "data:image/png;base64")
        self.assertEqual(base64.b64decode(body), PNG)

    def test_image_knows_svg_and_says_none_for_a_missing_file_an_empty_file_or_a_file_that_is_no_image(self):
        self.put("icon.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        self.assertTrue(ports.image(self.adapter, "icon.svg").startswith("data:image/svg+xml;base64,"))
        self.put("empty.png", b"")
        self.put("notes.txt", b"hello")
        for name in ("missing.png", "empty.png", "notes.txt"):
            self.assertIsNone(ports.image(self.adapter, name), name)

    def test_a_harness_gives_its_icon_to_the_table_and_a_harness_with_none_has_none(self):
        self.put("icon.png", PNG)
        ports.harness("iconic", "Iconic", ("iconic",), lambda cmd: "", order=97, icon=ports.image(self.adapter, "icon.png"))
        ports.harness("plain", "Plain", ("plain",), lambda cmd: "", order=98)
        table = ports.harnesses()
        self.assertTrue(table["iconic"]["icon"].startswith("data:image/png;base64,"))
        self.assertIsNone(table["plain"]["icon"])

    def test_each_entry_of_the_table_has_the_key_icon(self):
        for key, entry in ports.harnesses().items():
            self.assertIn("icon", entry, key)


if __name__ == "__main__":
    unittest.main()
