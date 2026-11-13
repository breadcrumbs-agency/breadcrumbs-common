"""costs.py: the cost profiles (built-in Haiku 5.5, costs.json, the price step), the tiers of the price port, the usage
metrics priced by the prompt of each request, and the update interval of the viewers (tuning.py)."""
import json
import os
import tempfile
import unittest

from breadcrumbs import costs, tuning, usage


def good(**kw):
    p = {"tiers": [{"upto": 50_000, "input": 1, "output": 2, "cache_read": 0.1, "cache_write": 1.25},
                   {"input": 3, "output": 6, "cache_read": 0.3, "cache_write": 3.75}],
         "context_window": 200_000,
         "sources": [{"url": "https://example.com/pricing", "retrieved": "2026-11-13", "quote": "a rate"}]}
    p.update(kw)
    return p


class Validate(unittest.TestCase):
    def test_a_good_profile_is_clean(self):
        prof, bad = costs.validate(good())
        self.assertEqual(bad, [])
        self.assertEqual(prof["tiers"][0][0], 50_000)
        self.assertIsNone(prof["tiers"][1][0])
        self.assertEqual(prof["context_window"], 200_000)

    def test_every_fault_is_named(self):
        cases = {
            "unknown field": {"contxt": 3},
            "tiers": {"tiers": []},
            "missing rate": {"tiers": [{"input": 1, "output": 1, "cache_read": 1}]},
            "negative": {"tiers": [{"input": -1, "output": 1, "cache_read": 1, "cache_write": 1}]},
            "bool": {"tiers": [{"input": True, "output": 1, "cache_read": 1, "cache_write": 1}]},
            "last tier with upto": {"tiers": [{"upto": 10, "input": 1, "output": 1, "cache_read": 1, "cache_write": 1}]},
            "tier without upto in the middle": {"tiers": [
                {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1},
                {"upto": 10, "input": 1, "output": 1, "cache_read": 1, "cache_write": 1}]},
            "upto not increasing": {"tiers": [
                {"upto": 10, "input": 1, "output": 1, "cache_read": 1, "cache_write": 1},
                {"upto": 10, "input": 1, "output": 1, "cache_read": 1, "cache_write": 1},
                {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}]},
            "context window small": {"context_window": 5},
            "http source": {"sources": [{"url": "http://x", "retrieved": "2026-11-13"}]},
            "bad date": {"sources": [{"url": "https://x", "retrieved": "2026-13-01"}]},
            "no sources": {"sources": []},
            "long quote": {"sources": [{"url": "https://x", "retrieved": "2026-11-13", "quote": "q" * 301}]},
        }
        for name, change in cases.items():
            prof, bad = costs.validate(good(**change))
            self.assertIsNone(prof, name)
            self.assertTrue(bad, name)

    def test_a_template_with_nulls_is_refused(self):
        prof, bad = costs.validate(json.loads(costs.template("m")))
        self.assertIsNone(prof)
        self.assertTrue(bad)


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_the_built_in_haiku_profile_is_the_stepped_price(self):
        name, prof = costs.lookup("claude-haiku-5-5")
        self.assertEqual(name, "*haiku-5[.-]5*")
        self.assertEqual(costs.HAIKU_STEP, 100_000)
        self.assertEqual(costs.price_tiers(prof), [
            (100_000, (0.10, 0.01, 0.125)),
            (None, (0.50, 0.05, 0.625))])
        self.assertEqual(prof["context_window"], 1_000_000)
        self.assertEqual(costs.lookup("anthropic/claude-haiku-5.5")[0], "*haiku-5[.-]5*")
        self.assertIsNone(costs.lookup("claude-sonnet-5-5")[1])

    def test_pick_steps_at_the_step_and_not_above_it(self):
        tiers = costs.price_tiers(costs.lookup("claude-haiku-5-5")[1])
        self.assertEqual(costs.pick(tiers, 100_000), (0.10, 0.01, 0.125))  # up to: the low tier
        self.assertEqual(costs.pick(tiers, 100_001), (0.50, 0.05, 0.625))  # over: the high tier
        self.assertEqual(costs.pick((1.0, 2.0, 3.0), 10**9), (1.0, 2.0, 3.0))  # a flat price stays as it is
        self.assertIsNone(costs.pick(None, 5))

    def test_a_file_profile_wins_over_built_in_and_is_read_again_when_it_changes(self):
        ok, _ = costs.set_profile(self.root, "claude-haiku-5-5", good(context_window=300_000))
        self.assertTrue(ok)
        self.assertEqual(costs.context_window("claude-haiku-5-5", self.root), 300_000)
        self.assertEqual(costs.context_window("claude-haiku-5-5"), 1_000_000)  # no root: the built in one
        self.assertEqual(costs.context_window("claude-haiku-5-5", self.root), 300_000)
        costs.set_profile(self.root, "claude-haiku-5-5", good(context_window=400_000))
        self.assertEqual(costs.context_window("claude-haiku-5-5", self.root), 400_000)

    def test_an_exact_name_wins_over_a_glob(self):
        costs.set_profile(self.root, "claude-haiku-5-5", good(context_window=123_000))
        self.assertEqual(costs.lookup("claude-haiku-5-5", self.root)[0], "claude-haiku-5-5")

    def test_set_keeps_the_other_profiles_and_remove_takes_one_out(self):
        costs.set_profile(self.root, "a-model", good())
        costs.set_profile(self.root, "b-model", good(context_window=9_000))
        self.assertTrue(costs.remove(self.root, "a-model"))
        self.assertFalse(costs.remove(self.root, "a-model"))
        with open(os.path.join(self.root, "costs.json"), encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)["models"]), ["b-model"])

    def test_a_refused_profile_writes_nothing(self):
        ok, bad = costs.set_profile(self.root, "x", {"tiers": []})
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(os.path.join(self.root, "costs.json")))

    def test_a_bad_file_gives_the_built_in_profiles_and_a_note(self):
        with open(os.path.join(self.root, "costs.json"), "w") as f:
            f.write("{not json")
        table, notes = costs.profiles(self.root)
        self.assertIn("*haiku-5[.-]5*", table)
        self.assertEqual(notes, [])  # unreadable: nothing to say; a readable one with a wrong entry says so
        with open(os.path.join(self.root, "costs.json"), "w") as f:
            json.dump({"models": {"m": {"tiers": []}}}, f)
        table, notes = costs.profiles(self.root)
        self.assertNotIn("m", table)
        self.assertTrue(any("costs.json m" in n for n in notes))

    def test_the_brief_has_the_known_profile_the_form_and_the_commands(self):
        text = costs.fetch_brief("claude-haiku-5-5", self.root, (0.1, 0.01, 0.125))
        for needle in ("Known now", "up to 100,000 tokens", "1,000,000 tokens", "Quote the line", "Do not guess",
                       "cost set FILE", "cost show claude-haiku-5-5", "harness's own table gives"):
            self.assertIn(needle, text)
        self.assertIn('"tiers"', text)
        self.assertIn("Known now: no profile.", costs.fetch_brief("some-new-model", self.root))

    def test_the_command_sets_from_a_file_and_refuses_a_bad_one(self):
        import types
        path = os.path.join(self.root, "in.json")
        with open(path, "w") as f:
            json.dump({"models": {"m": good()}}, f)
        args = types.SimpleNamespace(action="set", items=[path], file=path, model=None, provider=None)
        self.assertEqual(costs.command(args, self.root), 0)
        self.assertIsNotNone(costs.lookup("m", self.root)[1])
        with open(path, "w") as f:
            json.dump(good(contxt=1), f)
        args.model = "m2"
        self.assertEqual(costs.command(args, self.root), 1)
        self.assertIsNone(costs.lookup("m2", self.root)[1])


class Metrics(unittest.TestCase):
    def rec(self, cr, cw, inp, model="claude-haiku-5-5"):
        return {"sess": "s", "ts": 1, "req": "r", "model": model, "prov": "anthropic", "in": inp, "cr": cr, "cw": cw,
                "out": 0, "rsn": 0}

    def test_a_request_is_priced_by_its_own_prompt_size(self):
        table = {("claude-haiku-5-5", "anthropic"): costs.price_tiers(costs.lookup("claude-haiku-5-5")[1])}
        low = self.rec(cr=50_000, cw=0, inp=10_000)     # prompt 60k: low tier
        high = self.rec(cr=150_000, cw=0, inp=10_000)   # prompt 160k: high tier
        vals = usage.values([low, high], price=lambda m, p: table[(m, p)])
        self.assertAlmostEqual(vals[0]["cache_saved_usd"], (50_000 * (0.10 - 0.01)) / 1e6)
        self.assertAlmostEqual(vals[1]["cache_saved_usd"], (150_000 * (0.50 - 0.05)) / 1e6)

    def test_a_flat_price_is_the_same_as_before(self):
        r = self.rec(cr=100, cw=0, inp=0)
        vals = usage.values([r], price=lambda m, p: (3.0, 0.3, 3.75))
        self.assertAlmostEqual(vals[0]["cache_saved_usd"], 100 * (3.0 - 0.3) / 1e6)

    def test_the_port_gives_tiers_or_none(self):
        class Ports:
            @staticmethod
            def get(name):
                return lambda m, p: [(10, (1.0, 0.1, 1.0)), (None, (2.0, 0.2, 2.0))]
        original = usage.ports
        usage.ports = Ports
        try:
            self.assertEqual(usage.rates("m", "p"), [(10, (1.0, 0.1, 1.0)), (None, (2.0, 0.2, 2.0))])
        finally:
            usage.ports = original


class Tuning(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_the_default_is_two_seconds(self):
        self.assertEqual(tuning.update_ms(None, {}), 2000)
        self.assertEqual(tuning.DEFAULT_MS, 2000)

    def test_the_environment_then_the_file_then_the_default(self):
        with open(os.path.join(self.root, "config.json"), "w") as f:
            json.dump({"update_ms": 750}, f)
        self.assertEqual(tuning.update_ms(self.root, {}), 750)
        self.assertEqual(tuning.update_ms(self.root, {"BREADCRUMBS_UPDATE_MS": "300"}), 300)

    def test_a_value_out_of_range_or_not_a_whole_number_is_the_default(self):
        for raw in ("10", "99999", "abc", "1.5", "-4"):
            self.assertEqual(tuning.update_ms(None, {"BREADCRUMBS_UPDATE_MS": raw}), 2000, raw)
        with open(os.path.join(self.root, "config.json"), "w") as f:
            json.dump({"update_ms": True}, f)
        self.assertEqual(tuning.update_ms(self.root, {}), 2000)
        self.assertEqual(tuning.update_ms(self.root, {"BREADCRUMBS_UPDATE_MS": "250"}), 250)


if __name__ == "__main__":
    unittest.main()
