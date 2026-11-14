"""statedb.locate: which state.db a viewer reads for a store."""
import os
import sqlite3
import tempfile
import unittest

from breadcrumbs import statedb


def touch_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sqlite3.connect(path).close()


class Locate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = os.path.join(self.tmp.name, "profile")
        self.base = os.path.join(self.profile, "breadcrumbs")
        self.scope = os.path.join(self.base, "scopes", "app-1234abcd")
        os.makedirs(self.scope)

    def test_a_base_reads_the_state_db_of_the_profile_above_it(self):
        touch_db(os.path.join(self.profile, "state.db"))
        self.assertEqual(statedb.locate(self.base), os.path.join(self.profile, "state.db"))

    def test_a_scope_with_no_state_db_of_its_own_reads_the_one_of_its_base(self):
        touch_db(os.path.join(self.profile, "state.db"))
        self.assertEqual(statedb.locate(self.scope), os.path.join(self.profile, "state.db"))

    def test_a_scope_that_sync_gave_a_state_db_keeps_it(self):
        touch_db(os.path.join(self.profile, "state.db"))
        touch_db(os.path.join(self.scope, "state.db"))
        self.assertEqual(statedb.locate(self.scope), os.path.join(self.scope, "state.db"))


if __name__ == "__main__":
    unittest.main()
