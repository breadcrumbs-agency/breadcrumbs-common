"""The fold policy (breadcrumbs/fold.py): the ceiling of a model, the thinning of a result in each shape, the block."""
import unittest

from breadcrumbs import fold

HAIKU = "claude-haiku-5-5"
OPUS = "claude-opus-5-5"


class Ceiling(unittest.TestCase):
    def test_haiku_has_the_step_ceiling(self):
        self.assertEqual(fold.ceiling(HAIKU, {}), 94_000)
        self.assertEqual(fold.ceiling("claude-haiku-5.5", {}), 94_000)

    def test_other_models_have_no_step(self):
        self.assertIsNone(fold.ceiling(OPUS, {}))
        self.assertIsNone(fold.ceiling(None, {}))

    def test_the_environment_sets_it_for_any_model(self):
        self.assertEqual(fold.ceiling(OPUS, {"BREADCRUMBS_FOLD_CEILING": "5000"}), 5000)
        self.assertEqual(fold.ceiling(HAIKU, {"BREADCRUMBS_FOLD_CEILING": "0"}), 94_000)  # not a ceiling: the default
        self.assertEqual(fold.ceiling(HAIKU, {"BREADCRUMBS_FOLD_CEILING": "lots"}), 94_000)


class Thin(unittest.TestCase):
    def test_a_text_under_the_room_is_kept(self):
        self.assertEqual(fold.thin("short", 100), "short")

    def test_a_long_text_is_cut_at_a_line_end_with_a_note(self):
        text = "line one\nline two\n" + "x" * 400
        out = fold.thin(text, 10)  # 40 bytes: the first two lines are 18 bytes, the third line is cut away
        self.assertTrue(out.startswith("line one\nline two\n[breadcrumbs:"))
        self.assertNotIn("xxxx", out)
        self.assertIn("/compact", out)

    def test_a_room_of_zero_keeps_only_the_note(self):
        out = fold.thin("a long result", 0)
        self.assertTrue(out.startswith("\n[breadcrumbs:"))


class Shrink(unittest.TestCase):
    def test_a_string_stays_a_string(self):
        self.assertIsInstance(fold.shrink("x" * 400, 10), str)

    def test_a_content_list_keeps_its_shape(self):
        resp = {"content": [{"type": "text", "text": "x" * 400}], "isError": False}
        out = fold.shrink(resp, 10)
        self.assertEqual(set(out), {"content", "isError"})
        self.assertEqual(out["content"][0]["type"], "text")
        self.assertIn("[breadcrumbs:", out["content"][0]["text"])

    def test_a_bare_list_of_text_parts_keeps_its_shape(self):
        out = fold.shrink([{"type": "text", "text": "x" * 400}], 10)
        self.assertIsInstance(out, list)

    def test_nothing_to_cut_is_none(self):
        self.assertIsNone(fold.shrink({"content": [{"type": "text", "text": "ok"}]}, 10))
        self.assertIsNone(fold.shrink({"other": 1}, 10))
        self.assertIsNone(fold.shrink(None, 10))


class Policy(unittest.TestCase):
    def test_a_result_is_untouched_under_the_ceiling(self):
        self.assertIsNone(fold.post_result(50_000, HAIKU, "x" * 4000, {}))

    def test_a_result_is_thinned_at_the_ceiling(self):
        new = fold.post_result(93_000, HAIKU, "x" * 40_000, {})
        self.assertIsNotNone(new)
        self.assertLess(len(new.encode()), 40_000)

    def test_a_model_without_a_step_is_never_thinned(self):
        self.assertIsNone(fold.post_result(200_000, OPUS, "x" * 40_000, {}))

    def test_a_prompt_is_blocked_at_the_ceiling_only(self):
        self.assertIsNone(fold.block_reason(93_999, HAIKU, {}))
        reason = fold.block_reason(94_000, HAIKU, {})
        self.assertIn("94,000 prompt tokens", reason)
        self.assertIn("/compact", reason)
        self.assertIsNone(fold.block_reason(165_000, OPUS, {}))


if __name__ == "__main__":
    unittest.main()
