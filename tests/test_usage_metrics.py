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
