"""ports.image (a PNG beside a module, as a data URI) and the icon of a harness entry."""
import base64
import os
import tempfile
import unittest

from breadcrumbs import ports


class Image(unittest.TestCase):
    def test_a_png_beside_the_module_becomes_a_data_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "icon.png"), "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nbody")
            uri = ports.image(os.path.join(tmp, "adapter.py"), "icon.png")
        self.assertEqual(uri.partition(",")[0], "data:image/png;base64")
        self.assertEqual(base64.b64decode(uri.partition(",")[2]), b"\x89PNG\r\n\x1a\nbody")

    def test_a_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ports.image(os.path.join(tmp, "adapter.py"), "icon.png"))

    def test_the_harness_entry_keeps_its_icon(self):
        ports.harness("testicon", "Test", ("testicon",), lambda cmd: "", order=99, icon="data:image/png;base64,AA")
        self.assertEqual(ports.harnesses()["testicon"]["icon"], "data:image/png;base64,AA")
        ports.harness("testicon", "Test", ("testicon",), lambda cmd: "", order=99)
        self.assertIsNone(ports.harnesses()["testicon"]["icon"])


if __name__ == "__main__":
    unittest.main()
