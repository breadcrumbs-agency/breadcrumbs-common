"""The ACE core (breadcrumbs/ace_core.py): the layer math, the messages, the logs and the engine, with a plain dict of
dials. Common imports no harness: the last test reads the module."""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from breadcrumbs import ace_core as core
from breadcrumbs.store import Store

COMMON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # breadcrumbs-common
SID = "abcdefghijklmnopqrstuvwxyz0123"  # a session id: a log line holds its first LOG_SESS characters


def msg(role, content="", **extra):
    return dict({"role": role, "content": content}, **extra)


def chat(turns, body=1000):
    """turns user turns, each with one tool call and a result of about body bytes."""
    out = []
    for t in range(turns):
        out.append(msg("user", "question %d about the shelf" % t))
        out.append(msg("assistant", "", tool_calls=[{"id": "c%d" % t, "type": "function",
                                                     "function": {"name": "read_file", "arguments": '{"path": "a%d"}' % t}}]))
        out.append(msg("tool", ("result %d line\n" % t) * (body // 14), tool_call_id="c%d" % t))
        out.append(msg("assistant", "answer %d" % t))
    return out


class Dials(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("BREADCRUMBS_ACE_TOKENS", None)

    def test_the_defaults_are_the_dials_of_the_harness(self):
        self.assertEqual(core.DIAL_DEFAULTS, {
            "ace_tokens": 200_000, "ace_fold_to": 0.9, "ace_fold_cap": 1.35, "ace_ratchet": 1.45, "ace_ceiling": 0,
            "ace_lockup": False, "ace_room": 0, "ace_downshift": 0.0, "ace_turn_fold": False, "ace_persist": 0.0,
            "ace_diverse": 4})

    def test_no_values_give_the_defaults(self):
        e = core.Engine()
        e._apply({})
        self.assertEqual((e.target_tokens, e.fold_to, e.fold_cap, e.step, e.diverse, e.ceiling, e.lockup),
                         (200_000, 0.9, 1.35, 1.45, 4, 0, False))
        self.assertEqual(e.dial_values("any", "any-model"), {})  # the base engine reads no config

    def test_a_dict_of_values_sets_the_dials(self):
        e = core.Engine()
        e._apply({"ace_tokens": 50_000, "ace_fold_to": 0.8, "ace_ratchet": 1.0, "ace_diverse": 0, "ace_ceiling": 60_000,
                  "ace_lockup": True, "ace_room": 30_000, "ace_downshift": 0.2, "ace_turn_fold": True, "ace_persist": 0.5,
                  "not_a_dial": 1})
        self.assertEqual((e.target_tokens, e.fold_to, e.step, e.diverse, e.ceiling), (50_000, 0.8, 1.0, 0, 60_000))
        self.assertEqual((e.lockup, e.room, e.downshift, e.turn_fold, e.persist), (True, 30_000, 0.2, True, 0.5))
        self.assertEqual(e.gear, "lock")  # a lockup chat starts in lockup

    def test_the_constructor_target_is_kept_over_the_dial(self):
        e = core.Engine(target_tokens=777)
        e._apply({"ace_tokens": 50_000})
        self.assertEqual(e.target_tokens, 777)

    def test_the_environment_sets_the_target_when_no_target_is_given(self):
        os.environ["BREADCRUMBS_ACE_TOKENS"] = "12345"
        self.assertEqual(core.Engine().target_tokens, 12345)


class Messages(unittest.TestCase):
    def test_a_tool_call_is_a_line_of_its_name_and_arguments(self):
        m = msg("assistant", "", tool_calls=[{"function": {"name": "read", "arguments": "{}"}}])
        self.assertEqual(core.message_text(m), "→ read {}")
        self.assertEqual(core.message_text(msg("assistant", "hi", tool_calls=[{"function": {"name": "read",
                                                                                          "arguments": "{}"}}])),
                         "hi\n→ read {}")

    def test_a_list_content_keeps_its_text_parts(self):
        m = msg("user", [{"type": "text", "text": "one"}, {"type": "image_url"}])
        self.assertEqual(core.message_text(m), "one\n[image_url]")

    def test_the_thinking_is_read_from_its_fields(self):
        self.assertEqual(core.reasoning_text(msg("assistant", reasoning_details=json.dumps(
            [{"thinking": "ab"}, {"text": "cd"}]))), "abcd")
        self.assertEqual(core.reasoning_text(msg("assistant", reasoning="xyz")), "xyz")
        self.assertEqual(core.reasoning_text(msg("assistant")), "")

    def test_the_request_size_counts_text_thinking_and_sixteen_bytes_a_message(self):
        self.assertEqual(core.request_bytes([msg("user", "abc")]), 3 + 16)
        self.assertEqual(core.request_bytes([msg("assistant", "a", reasoning="bc")]), 1 + 2 + 16)

    def test_split_gives_the_head_and_one_turn_per_user_message(self):
        messages = [msg("system", "s"), msg("user", "u1"), msg("assistant", "a1"), msg("tool", "t1"),
                    msg("user", "u2"), msg("assistant", "a2")]
        head, turns = core.split(messages)
        self.assertEqual([m["content"] for m in head], ["s"])
        self.assertEqual([[m["content"] for m in t] for t in turns], [["u1", "a1", "t1"], ["u2", "a2"]])

    def test_cut_keeps_everything_that_fits_and_folds_the_rest(self):
        turns = core.split(chat(6, body=400))[1]
        self.assertIsNone(core.cut(turns, 10 ** 9))
        b, k = core.cut(turns, 1000)
        self.assertLess(b, len(turns))

    def test_a_ref_is_the_text_stored_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "store"))
            try:
                ref = core.ref_of(store, "one\ntwo\n")
                self.assertRegex(ref, r"^[0-9a-f]{8}(?:\.\d+)?:0-2$")
                self.assertEqual(core.ref_of(store, "one\ntwo\n"), ref)
            finally:
                store.close()


class Layer(unittest.TestCase):
    def test_the_levels_fit_the_budget_and_come_from_levels(self):
        entries = [{"id": "a", "group": 0, "size": 400, "weight": 5.0},
                   {"id": "b", "group": 0, "size": 40000, "weight": 0.2},
                   {"id": "c", "group": 1, "size": 900, "weight": 1.0}]
        levels = core.allocate(entries, 2000)
        self.assertLessEqual(core.layer_cost(entries, levels), 2000)
        self.assertTrue({v for v in levels.values() if v is not None} <= set(core.LEVELS))

    def test_a_tight_budget_folds_the_oldest_group_into_one_line(self):
        # three handles of group 0 (120 bytes) cost more than one folded line (96): group 0 folds, group 1 keeps its handle
        entries = [{"id": "o%d" % i, "group": 0, "size": 500, "weight": 1.0} for i in range(3)]
        entries.append({"id": "n1", "group": 1, "size": 500, "weight": 1.0})
        levels = core.allocate(entries, 150)
        self.assertEqual([levels["o%d" % i] for i in range(3)], [None, None, None])
        self.assertEqual(levels["n1"], 0.0)
        self.assertLessEqual(core.layer_cost(entries, levels), 150)

    def test_a_zero_level_is_a_handle_only(self):
        self.assertEqual(core.cost({"size": 100}, 0.0), core.HANDLE_BYTES)
        self.assertEqual(core.cost({"size": 100}, None), 0)


class Logs(unittest.TestCase):
    LINE = ("2026-11-12T10:00:00 pid=1 sess=%s act=%s in=%d out=%d last=%d base=%s tpb=0.3038 r=0.4777 folds=1 layer=2 "
            "target=%d gain=1.0000 lay=10 gear=- think=0 calls=3 work=0 room=0\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def write(self, *lines):
        with open(os.path.join(self.root, "ace.log"), "w", encoding="utf-8") as f:
            f.writelines(lines)

    def test_the_counts_of_the_newest_line_of_the_session(self):
        self.write(self.LINE % (SID[:24], "fold", 20000, 16000, 19000, "1234", 16000),
                   self.LINE % ("other-session", "fold", 9, 9, 9, "99", 16000))
        self.assertEqual(core.logged_counts(self.root, SID, 16000), (1234, 0.3038, 0.4777))

    def test_a_base_at_or_over_the_target_is_no_count(self):
        self.write(self.LINE % (SID[:24], "fold", 20000, 16000, 19000, "20000", 16000))
        self.assertIsNone(core.logged_counts(self.root, SID, 16000))

    def test_the_status_lines_name_a_request_over_its_target(self):
        self.write(self.LINE % (SID[:24], "fold", 20000, 20000, 19000, "1234", 16000))
        (line,) = core.status_lines(self.root)
        self.assertIn("OVER by 4,000", line)
        self.assertIn(SID[:24], line)

    def test_a_line_joins_the_request_it_shaped(self):
        lines = [{"sess": SID, "ts": 89, "act": "fold"}, {"sess": SID, "ts": 95, "act": "pass"}]
        requests = [{"sess": SID, "ts": 100, "req": "r1", "dur": 10, "task": "main"},
                    {"sess": SID, "ts": 100, "req": "r2", "dur": 0, "task": "main"}]
        got = [(line["act"], r["req"]) for line, r in core.join(lines, requests)]
        self.assertEqual(got, [("fold", "r1"), ("pass", "r2")])

    def test_a_line_with_no_session_or_request_joins_nothing(self):
        self.assertEqual(core.join([{"sess": "-", "ts": 0, "act": "pass"}], [{"sess": "-", "ts": 0, "task": "main"}]), [])
        self.assertEqual(core.join([{"sess": SID, "ts": 0, "act": "pass"}], []), [])


class Engine_(unittest.TestCase):
    """The engine over a folder, with the dials given as a dict by a subclass (no config file, no harness)."""

    class Dialled(core.Engine):
        def dial_values(self, root, model):
            return {"ace_tokens": 2000, "ace_diverse": 0}

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("BREADCRUMBS_ACE_TOKENS", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "breadcrumbs"))

    def test_a_request_under_the_target_passes_and_one_over_folds(self):
        e = self.Dialled()
        e.start_session(SID, home=self.tmp.name)
        e.model = "a-model"
        small = [msg("system", "sys")] + chat(2, body=200)
        self.assertIsNone(e.select_context(small))
        big = [msg("system", "sys")] + chat(12, body=1000)
        out = e.select_context(big)
        self.assertIsNotNone(out)
        self.assertEqual(out[0]["content"], "sys")
        self.assertTrue(any(core.LAYER_HEAD in str(m.get("content")) for m in out))
        self.assertEqual(e.get_status()["ace_target_tokens"], 2000)
        e.close()
        with open(os.path.join(self.tmp.name, "breadcrumbs", "ace.log"), encoding="utf-8") as f:
            acts = [re.search(r" act=(\w+) ", line).group(1) for line in f]
        self.assertEqual(acts, ["pass", "fold"])

    def test_a_harness_that_names_a_dead_end_gets_the_request_as_it_is(self):
        class Dead(self.Dialled):
            def _cannot_open(self, head):
                return True

        e = Dead()
        e.start_session(SID, home=self.tmp.name)
        big = [msg("system", "sys")] + chat(12, body=1000)
        self.assertIsNone(e.select_context(big))

    def test_the_counts_of_the_model_set_the_bytes_of_a_token(self):
        e = core.Engine()
        e.counted(1000, 5, 1005)  # nothing sent yet: no count is read
        self.assertEqual(e.last_prompt_tokens, 1000)
        e._sent = 2000
        e.counted(1000, 5, 1005)
        self.assertEqual(e.ratio, 0.5)
        self.assertEqual(e.base, int(1000 - 0.3 * 2000))


class Standalone(unittest.TestCase):
    def test_common_imports_no_harness_or_hermes_module(self):
        path = os.path.join(COMMON, "breadcrumbs", "ace_core.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names.append(node.module or "")
        bad = [n for n in names if n.split(".")[0] in ("agent", "hermes", "hermes_constants", "dials")
                or n.startswith("breadcrumbs_")]
        self.assertEqual(bad, [])

    def test_importing_the_core_loads_no_harness_package(self):
        code = ("import sys; import breadcrumbs.ace_core; "
                "bad = [m for m in sys.modules if m.startswith(('breadcrumbs_', 'agent', 'hermes', 'dials'))]; "
                "print(bad)")
        out = subprocess.run([sys.executable, "-c", code], cwd=COMMON, env={**os.environ, "PYTHONPATH": COMMON},
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
