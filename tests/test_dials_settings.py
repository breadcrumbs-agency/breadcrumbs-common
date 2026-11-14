"""CRUD for the common Breadcrumbs dials, including safe reset behavior."""
import json
import os
import tempfile
import unittest

from breadcrumbs import dials


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def read(self):
        with open(os.path.join(self.root, "config.json"), encoding="utf-8") as stream:
            return json.load(stream)

    def test_set_delete_and_reset_keep_non_dial_settings(self):
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as stream:
            json.dump({"hive": {"ms": 150}, "models": {"gpt-*": {"temperature": 0.2, "ace_tokens": 300000},
                                                               "other": {"ace_tokens": 123000}}}, stream)

        profile = dials.settings(self.root, environ={})
        self.assertEqual(next(r for g in profile["groups"] for r in g["rows"] if r["key"] == "ace_tokens")["value"], 200000)

        # The public mutation functions return a JSON-ready view; model-specific
        # writes use the exact model key so they can be reverted independently.
        dials.set_setting(self.root, "ace_tokens", 400000, "gpt-5")
        self.assertEqual(self.read()["models"]["gpt-5"]["ace_tokens"], 400000)
        dials.delete_setting(self.root, "ace_tokens", "gpt-5")
        self.assertNotIn("gpt-5", self.read()["models"])
        self.assertEqual(self.read()["models"]["gpt-*"]["temperature"], 0.2)

        dials.set_setting(self.root, "update_ms", 750)
        dials.reset_settings(self.root)
        cfg = self.read()
        self.assertNotIn("update_ms", cfg)
        self.assertEqual(cfg["hive"], {"ms": 150})
        self.assertEqual(cfg["models"]["gpt-*"]["ace_tokens"], 300000)

    def test_reset_all_models_removes_only_common_dial_keys(self):
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as stream:
            json.dump({"ace_tokens": 220000, "models": {"gpt-*": {"temperature": 0.2, "ace_tokens": 300000},
                                                            "empty": {"ace_tokens": 123000}}}, stream)

        dials.reset_settings(self.root, all_models=True)
        cfg = self.read()
        self.assertNotIn("ace_tokens", cfg)
        self.assertEqual(cfg["models"], {"gpt-*": {"temperature": 0.2}})

    def test_invalid_values_and_invalid_json_are_rejected_without_overwrite(self):
        with self.assertRaisesRegex(ValueError, "ace_tokens"):
            dials.set_setting(self.root, "ace_tokens", 0)
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as stream:
            stream.write("not json")
        with self.assertRaisesRegex(ValueError, "invalid breadcrumbs config"):
            dials.set_setting(self.root, "ace_tokens", 220000)
        with open(os.path.join(self.root, "config.json"), encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "not json")

    def test_the_ace_dials_are_grouped_as_thresholds_grow_fold_and_gears(self):
        names = [g["name"] for g in dials.settings(self.root, environ={})["groups"]]
        self.assertEqual(names[:4], ["ACE thresholds (per model)", "ACE grow (per model)",
                                     "ACE fold (per model)", "ACE gears (per model)"])
        keys = {r["key"]: g["name"] for g in dials.settings(self.root, environ={})["groups"] for r in g["rows"]}
        self.assertEqual(keys["ace_ratchet"], "ACE grow (per model)")
        self.assertEqual(keys["ace_fold_to"], "ACE fold (per model)")


if __name__ == "__main__":
    unittest.main()
