"""breadcrumbs/usage.py: the cache metrics, defined once, and the usage.log lines of a harness. The numbers are made up;
the price port is a fake."""
import os
import tempfile
import time
import unittest

from breadcrumbs import metrics, ports, usage


def req(sess, ts, cr, cw, inp=100, model="m1", req_id="", task="main", prov="p"):
    return {"sess": sess, "ts": ts, "req": req_id, "task": task, "model": model, "prov": prov, "in": inp, "cr": cr, "cw": cw,
            "out": 10, "rsn": 0, "prompt": inp + cr + cw}


RATES = {"m1": (3.0, 0.3, 3.75)}  # made-up USD per 1M: input, cache read, cache write


class CacheMetricTest(unittest.TestCase):

    def setUp(self):
        self.saved = ports.get("price")
        self.addCleanup(ports.give, "price", self.saved)
        ports.give("price", lambda model, provider: RATES.get(model))

    def test_the_cache_metrics_are_in_the_common_table(self):
        self.assertEqual(usage.KEYS, ("cache_hit", "cache_write_share", "cache_reuse", "cache_breaks", "break_tokens",
                                      "cache_saved_usd"))
        for key in usage.KEYS:
            self.assertIs(metrics.get(key), next(m for m in usage.CACHE if m["key"] == key))
        self.assertEqual(metrics.get("cache_hit")["of"], ["cr", ["in", "cr", "cw"]])
        self.assertEqual(metrics.get("cache_write_share")["of"], ["cw", ["in", "cr", "cw"]])
        self.assertEqual(metrics.get("cache_reuse")["of"], ["cr", ["cw"]])
        self.assertEqual({m["key"]: m["agg"] for m in usage.CACHE if m["agg"] != "ratio"},
                         {"cache_breaks": "sum", "break_tokens": "sum", "cache_saved_usd": "sum"})

    def test_a_break_is_a_drop_of_the_cache_read_with_a_write_in_one_session(self):
        rs = [req("a", 10, 0, 500, req_id="1"),       # the first request: no request before it
              req("a", 20, 500, 50, req_id="2"),
              req("a", 30, 100, 600, req_id="3"),     # break: 100 < 500 and a write
              req("a", 40, 90, 0, req_id="4"),        # a drop with no write: no break
              req("b", 35, 50, 20, req_id="1"),       # another session: compared with its own requests only
              req("a", 50, 700, 10, req_id="5"),
              req("a", 50, 60, 640, req_id="6"),      # the same time as 5: the id orders them, and this is a break
              req("a", 45, 0, 0, task="title", req_id="7")]  # another task: its own sequence
        self.assertEqual(usage.breaks(rs), {2, 6})
        vals = usage.values(rs)
        self.assertEqual([v["cache_breaks"] for v in vals], [0, 0, 1, 0, 0, 0, 1, 0])
        self.assertEqual(sum(v["break_tokens"] for v in vals), 600 + 640)
        # the order in the list does not matter: time, then id
        rv = list(reversed(rs))
        self.assertEqual({rv[i]["req"] + rv[i]["sess"] for i in usage.breaks(rv)}, {"3a", "6a"})

    def test_saved_dollars_need_the_rates_of_the_price_port(self):
        r = req("a", 10, 2_000_000, 100_000, model="m1")
        self.assertAlmostEqual(usage.values([r])[0]["cache_saved_usd"], 2 * (3.0 - 0.3) - 0.1 * (3.75 - 3.0))
        self.assertNotIn("cache_saved_usd", usage.values([req("a", 10, 5, 5, model="unknown")])[0])
        self.assertNotIn("cache_saved_usd", usage.values([r], price=None)[0])
        ports.give("price", lambda model, provider: (3.0, None, 3.75))  # a missing rate
        self.assertIsNone(usage.rates("m1", "p"))
        ports.give("price", lambda model, provider: 1 / 0)  # a port that fails
        self.assertIsNone(usage.rates("m1", "p"))
        ports.give("price", None)  # no port
        self.assertIsNone(usage.rates("m1", "p"))

    def test_the_summary_is_a_ratio_of_sums_by_model_and_leaves_out_what_has_no_number(self):
        rs = [req("a", 10, 0, 900, inp=100), req("a", 20, 900, 100, inp=0), req("a", 30, 50, 0, model="m2", inp=50)]
        s = usage.summary(rs)
        self.assertEqual((s["requests"], s["in"], s["cr"], s["cw"]), (3, 150, 950, 1000))
        self.assertAlmostEqual(s["cache_hit"], round(950 / 2100, 4))
        self.assertAlmostEqual(s["cache_reuse"], round(950 / 1000, 4))
        m1, m2 = s["models"]["m1"], s["models"]["m2"]
        self.assertAlmostEqual(m1["cache_hit"], round(900 / 2000, 4), msg="not the mean of 0 and 0.9")
        self.assertAlmostEqual(m1["cache_write_share"], round(1000 / 2000, 4))
        self.assertNotIn("cache_reuse", m2, "no write: a denominator of 0 gives no number")
        self.assertNotIn("cache_saved_usd", m2, "no rates for m2")
        self.assertIn("cache_saved_usd", m1)
        self.assertEqual(usage.summary([])["requests"], 0)
        self.assertNotIn("cache_hit", usage.summary([]))


class AfterTest(unittest.TestCase):
    """The cache writes beside the ACE acts: what a joined ace.log line and its request give (usage.after)."""

    def test_the_metrics_of_each_act_are_in_the_common_table_in_group_ace(self):
        keys = [p + a for a in usage.ACTS for p in ("acts_", "cw_after_", "breaks_after_", "cw_per_", "break_rate_")]
        self.assertEqual(usage.AFTER_KEYS, tuple(keys) + ("fold_cw_share",))
        for m in usage.AFTER:
            self.assertIs(metrics.get(m["key"]), m)
            self.assertEqual((m["group"], m["units"], m["keyed"]), ("ace", ["time", "request"], True))
        for a in usage.ACTS:
            self.assertEqual([metrics.get(k + a)["agg"] for k in ("acts_", "cw_after_", "breaks_after_")], ["sum"] * 3)
            self.assertEqual(metrics.get("cw_per_" + a)["of"], ["cw_after_" + a, ["acts_" + a]])
            self.assertEqual(metrics.get("break_rate_" + a)["of"], ["breaks_after_" + a, ["acts_" + a]])
        self.assertEqual(metrics.get("fold_cw_share")["of"], ["cw_after_fold", ["cw"]])

    def test_a_joined_line_gives_the_write_and_the_break_of_its_request(self):
        rs = [req("a", 10, 0, 800, req_id="1"),
              req("a", 20, 800, 30, req_id="2"),
              req("a", 25, 0, 50, task="compression", req_id="3"),  # a helper call: no share of the main writes
              req("a", 30, 100, 900, req_id="4"),                    # a break (100 < 800, a write)
              req("a", 40, 1000, 5, req_id="5")]
        got = usage.after(rs, [("pass", 0), ("keep", 1), ("fold", 3), ("fold", 3), ("gear", 4)])
        self.assertEqual(sorted(got), [0, 1, 3, 4], "every main request; the helper call gives nothing")
        self.assertEqual(got[0], {"fold_cw_share": (0, 800), "acts_pass": 1, "cw_after_pass": 800, "breaks_after_pass": 0,
                                  "cw_per_pass": (800, 1), "break_rate_pass": (0, 1)})
        self.assertEqual(got[1]["cw_after_keep"], 30)
        self.assertEqual(got[3], {"fold_cw_share": (900, 900), "acts_fold": 1, "cw_after_fold": 900, "breaks_after_fold": 1,
                                  "cw_per_fold": (900, 1), "break_rate_fold": (1, 1)},
                         "two lines joined to one request: the request counts once")
        self.assertEqual(got[4], {"fold_cw_share": (0, 5)}, "an act with no metrics gives nothing")
        self.assertEqual(usage.after(rs, []), {i: {"fold_cw_share": (0, rs[i]["cw"])} for i in (0, 1, 3, 4)})

    def test_a_request_counts_once_for_the_act_of_highest_rank_of_its_lines(self):
        self.assertEqual(usage.RANK, ("fold", "ratchet", "keep", "pass"))
        self.assertEqual(set(usage.RANK), set(usage.ACTS))
        rs = [req("a", 10, 800, 50, req_id="1"),
              req("a", 20, 100, 700, req_id="2"),  # a break (100 < 800, a write)
              req("a", 30, 800, 60, req_id="3"),
              req("a", 40, 860, 20, req_id="4"),
              req("a", 50, 880, 10, req_id="5")]
        got = usage.after(rs, [("keep", 1), ("fold", 1), ("keep", 2), ("keep", 2), ("keep", 3), ("ratchet", 3),
                               ("intercept", 4), ("pass", 4), ("pass", 4)])
        self.assertEqual(got[1], {"fold_cw_share": (700, 700), "acts_fold": 1, "cw_after_fold": 700, "breaks_after_fold": 1,
                                  "cw_per_fold": (700, 1), "break_rate_fold": (1, 1)},
                         "fold+keep: the fold only; the keep gives nothing, not its cw and not its break")
        self.assertEqual(got[2], {"fold_cw_share": (0, 60), "acts_keep": 1, "cw_after_keep": 60, "breaks_after_keep": 0,
                                  "cw_per_keep": (60, 1), "break_rate_keep": (0, 1)}, "keep+keep: counted once")
        self.assertEqual(sorted(k for k in got[3] if k != "fold_cw_share"),
                         sorted(p + "ratchet" for p in ("acts_", "cw_after_", "breaks_after_", "cw_per_", "break_rate_")),
                         "keep+ratchet: the ratchet")
        self.assertEqual((got[4]["acts_pass"], got[4]["cw_after_pass"]), (1, 10), "pass+pass (and an intercept): once")
        self.assertEqual(got[0], {"fold_cw_share": (0, 50)}, "a request with no line")
        # one request, one act: the sum of cw_after over the acts is never more than the cw of the joined requests
        cw_after = sum(v.get("cw_after_" + a, 0) for v in got.values() for a in usage.ACTS)
        self.assertEqual(cw_after, 700 + 60 + 20 + 10)
        self.assertEqual(sum(v.get("acts_" + a, 0) for v in got.values() for a in usage.ACTS), 4)

    def test_fold_cw_share_never_passes_one(self):
        """Every mix of the lines on a request (up to three lines of the four acts on each of three requests): the share,
        folded over the requests as a ratio of sums, is at most 1."""
        import itertools
        rs = [req("a", 10, 0, 900), req("a", 20, 900, 300), req("a", 30, 100, 800)]
        mixes = [()] + [m for n in (1, 2, 3) for m in itertools.combinations_with_replacement(usage.ACTS, n)]
        agg = metrics.get("fold_cw_share")["agg"]
        for lines in itertools.product(mixes, repeat=3):
            joins = [(act, i) for i, acts in enumerate(lines) for act in acts]
            share = (0, 0)
            for vals in usage.after(rs, joins).values():
                share = metrics.fold(agg, share, vals["fold_cw_share"])
            self.assertLessEqual(share[0], share[1], lines)
            self.assertEqual(share[0], sum(rs[i]["cw"] for i, acts in enumerate(lines) if "fold" in acts), lines)

    def test_each_ratio_of_an_act_is_a_ratio_of_sums_at_cell_row_and_total(self):
        rows = {"a": [req("a", 10, 0, 600), req("a", 20, 600, 0), req("a", 30, 0, 700)],
                "b": [req("b", 10, 0, 100), req("b", 20, 100, 0)]}
        joins = {"a": [("fold", 0), ("keep", 1), ("fold", 2)], "b": [("fold", 0), ("keep", 1)]}
        total, agg = {}, {m["key"]: m["agg"] for m in usage.AFTER}
        for sess, rs in rows.items():
            cells = {}
            for i, vals in usage.after(rs, joins[sess]).items():
                for k, v in vals.items():
                    metrics.put(cells, (i // 2, k), v, rs[i]["ts"], sess, agg[k])  # columns: requests 0-1, 2
            row = {}
            for (col, k), c in cells.items():  # the row total: every column of the row, folded
                metrics.roll(row, {(0, k): c.v}, lambda key: agg[key[1]])
            metrics.roll(total, row, lambda key: agg[key[1]])
            if sess == "a":
                self.assertEqual(cells[(0, "cw_per_fold")].v, (600, 1))
                self.assertEqual(cells[(1, "break_rate_fold")].v, (1, 1), "700 written at a cr drop: a break")
                self.assertEqual(row[(0, "cw_per_fold")], (1300, 2))
                self.assertEqual(row[(0, "fold_cw_share")], (1300, 1300))
                self.assertEqual(row[(0, "break_rate_keep")], (0, 1))
        self.assertAlmostEqual(metrics.value("ratio", total[(0, "cw_per_fold")]), 1400 / 3, msg="not the mean of 650 and 100")
        self.assertEqual(total[(0, "break_rate_fold")], (1, 3))
        self.assertEqual(metrics.value("ratio", total[(0, "fold_cw_share")]), 1.0)
        self.assertEqual(metrics.value("ratio", total[(0, "cw_per_keep")]), 0)
        self.assertNotIn((0, "cw_per_ratchet"), total, "no ratchet line: no number")


class UsageLogTest(unittest.TestCase):

    def test_a_record_is_one_line_of_ids_and_numbers_with_its_source(self):
        r = usage.record("demo", "s 1", "q1", 1767323045, "m1", None, "7", -3, 20, None, 2.9)
        self.assertEqual((r["in"], r["cr"], r["cw"], r["out"], r["rsn"], r["prompt"]), (7, 0, 20, 0, 2, 27))
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(1767323045))
        self.assertEqual(usage.line(r), stamp + " sess=s_1 turn=- req=q1 n=0 task=main model=m1 resp=m1 prov=- in=7 cr=0 cw=20"
                                              " out=0 rsn=2 prompt=27 est=0 msgs=0 tools=0 dur=0.000 fin=- src=demo")
        self.assertNotIn("src=", usage.line(dict(r, src=None), "2026-01-01T00:00:00"))

    def test_append_adds_a_request_once(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, "usage.log.1"), "w") as f:  # an older line of the same request id
                f.write(usage.line(req("a", 5, 0, 0, req_id="old")) + "\n")
            rs = [req("a", 10, 0, 9, req_id="r1"), req("a", 20, 9, 0, req_id="r1"), req("a", 30, 9, 0, req_id="old"),
                  req("a", 40, 9, 0, req_id="")]
            self.assertEqual(usage.append(home, rs), 1, "r1 once; old is in usage.log.1; no id: never")
            self.assertEqual(usage.append(home, rs), 0)
            self.assertEqual(usage.known(home), {"r1", "old"})
            with open(os.path.join(home, "usage.log")) as f:
                self.assertEqual(f.read().count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
