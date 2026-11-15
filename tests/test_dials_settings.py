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


class Chain(unittest.TestCase):
    """A chat reads a chain of files, the widest first (a profile's, then its scope's)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile, self.scope = os.path.join(self.tmp.name, "p"), os.path.join(self.tmp.name, "p", "scopes", "s")
        os.makedirs(self.scope)
        self.write(self.profile, {"ace_tokens": 300000, "neighbor": 0.2, "models": {"m*": {"ace_fold_to": 0.8}}})
        self.write(self.scope, {"ace_tokens": 150000, "ace_fold_to": 0.5, "models": {"mod*": {"ace_ratchet": 1.0}}})

    def write(self, folder, cfg):
        with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def rows(self, model=None):
        chain = [("profile", dials.config(self.profile)), ("scope", dials.config(self.scope))]
        return {r.dial.key: (r.value, r.source) for r in dials.resolve(chain, model, {})}

    def test_the_scope_wins_at_the_top_level_and_the_profile_fills_the_rest(self):
        rows = self.rows()
        self.assertEqual(rows["ace_tokens"], (150000, "scope"))
        self.assertEqual(rows["neighbor"], (0.2, "profile"))
        self.assertEqual(rows["ace_fold_to"], (0.5, "scope"))

    def test_a_models_entry_of_either_file_beats_a_top_level_and_the_scope_entry_comes_last(self):
        rows = self.rows("model-x")
        self.assertEqual(rows["ace_fold_to"], (0.8, 'profile models["m*"]'))
        self.assertEqual(rows["ace_ratchet"], (1.0, 'scope models["mod*"]'))
        self.assertEqual(dials.options([("profile", {"models": {"m*": {"top_k": 1, "seed": 1}}}),
                                        ("scope", {"models": {"m*": {"top_k": 2}}})], "m1"), {"top_k": 2, "seed": 1})

    def test_one_dict_is_one_file_as_before(self):
        rows = {r.dial.key: r.source for r in dials.resolve(dials.config(self.profile), "model", {})}
        self.assertEqual(rows["ace_fold_to"], 'models["m*"]')
        self.assertEqual(rows["ace_tokens"], "config")

    def test_settings_chain_resolves_through_the_chain_and_marks_the_last_files_overrides(self):
        got = dials.settings_chain([("profile", self.profile), ("scope", self.scope)], environ={})
        rows = {r["key"]: r for g in got["groups"] for r in g["rows"]}
        self.assertEqual(got["path"], os.path.join(self.scope, "config.json"))
        self.assertEqual([c["label"] for c in got["chain"]], ["profile", "scope"])
        self.assertEqual((rows["neighbor"]["value"], rows["neighbor"]["overridden"]), (0.2, False))
        self.assertEqual((rows["ace_tokens"]["value"], rows["ace_tokens"]["overridden"]), (150000, True))


if __name__ == "__main__":
    unittest.main()
