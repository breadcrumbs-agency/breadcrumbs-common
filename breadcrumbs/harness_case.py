"""The test case of a harness adapter, for the tests of the packages above common (their tests/test_adapter.py).

A package fills the values and gets the five checks of an adapter: it gives one entry to the harness table, identify
names its clients, the table lists it, its setup text is the one expected, and the health count names its calls. The
test module imports THIS MODULE, not the class, so that unittest discover does not run the empty case by itself:

    from breadcrumbs import harness_case

    class Adapter(harness_case.HarnessCase):
        KEY, LABEL, LAYER = "codex", "Codex", "codex"
        NAMES = ["codex-mcp-client", "codex"]
        SETUP = "codex mcp add breadcrumbs -- env PYTHONPATH=/x python -m breadcrumbs core mcp"
"""
import tempfile
import unittest

from breadcrumbs import core, ports


class HarnessCase(unittest.TestCase):
    KEY = LABEL = LAYER = SETUP = None  # the key of the harness table, its label, the name of its layer, the setup text for CMD
    NAMES = ()  # client names that identify the harness
    CMD = ["env", "PYTHONPATH=/x", "python", "-m", "breadcrumbs", "core", "mcp"]

    def setUp(self):
        self.entry = ports.harnesses()[self.KEY]

    def test_the_package_gives_one_entry_to_the_table(self):
        self.assertEqual(self.entry["label"], self.LABEL)
        self.assertEqual(ports.layer(self.LAYER, __file__), False)  # the adapter announced its layer once

    def test_identify_tells_the_client_names_apart(self):
        for name in self.NAMES:
            self.assertEqual(core.identify(name), self.KEY, name)
        for name in ("curl", "", None):
            self.assertEqual(core.identify(name), "unknown", name)

    def test_the_table_lists_it_in_its_place(self):
        self.assertIn(self.KEY, core.harnesses())

    def test_setup_is_the_text_that_adds_the_server(self):
        self.assertEqual(self.entry["setup"](self.CMD), self.SETUP)

    def test_health_counts_the_calls_of_this_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = core.Core(core.Stores(root=tmp))
            try:
                c.call("stores", harness=self.KEY)
                self.assertEqual(c.call("health")["calls"], {self.KEY: 1, "unknown": 1})
            finally:
                c.close()
