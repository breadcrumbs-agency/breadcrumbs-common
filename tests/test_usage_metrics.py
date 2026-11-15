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
                                      "cache_saved_usd", "cache_store_usd", "cache_read_usd", "cache_uncached_usd"))
        for key in usage.KEYS:
            self.assertIs(metrics.get(key), next(m for m in usage.CACHE if m["key"] == key))
        self.assertEqual(metrics.get("cache_hit")["of"], ["cr", ["in", "cr", "cw"]])
        self.assertEqual(metrics.get("cache_write_share")["of"], ["cw", ["in", "cr", "cw"]])
        self.assertEqual(metrics.get("cache_reuse")["of"], ["cr", ["cw"]])
        self.assertEqual({m["key"]: m["agg"] for m in usage.CACHE if m["agg"] != "ratio"},
                         {"cache_breaks": "sum", "break_tokens": "sum", "cache_saved_usd": "sum",
                          "cache_store_usd": "sum", "cache_read_usd": "sum", "cache_uncached_usd": "sum"})

    def test_the_cache_costs_are_the_same_tokens_priced_with_and_without_the_cache(self):
        # 1M-token rates: input 1.0, cache read 0.1, cache write 1.25 USD. cw=2000 stored, cr=1000 read, in=100 fresh.
        price = lambda m, p: (1.0, 0.1, 1.25)
        v = usage.values([req("a", 0, 1000, 2000, inp=100)], price=price)[0]
        self.assertAlmostEqual(v["cache_store_usd"], 2000 * 1.25 / 1e6)
        self.assertAlmostEqual(v["cache_read_usd"], 1000 * 0.1 / 1e6)
        self.assertAlmostEqual(v["cache_uncached_usd"], (1000 + 2000) * 1.0 / 1e6)  # cr and cw at the input rate
        self.assertNotIn("cache_store_usd", usage.values([req("a", 0, 1000, 2000)], price=None)[0])

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


class LeaseTest(unittest.TestCase):
    """usage.leases: the cache of a session as leases, rectangles of tokens in time. Each request reads the lease that
    ends at its cr and writes its cw on top. Prices are made up."""
    PRICE = staticmethod(lambda model, provider: (3.0, 0.3, 3.75))  # USD per 1M: input, cache read, cache write
    STEPS = staticmethod(lambda model, provider: [(1000, (1.0, 0.1, 1.25)), (None, (5.0, 0.5, 6.25))])  # steps up over 1000

    def rec(self, ts, cr=0, cw=0, inp=100, task="main", sess="s", dur=None, model="m1"):
        r = req(sess, ts, cr, cw, inp=inp, model=model, req_id="r%s" % ts, task=task)
        if dur is not None:
            r["dur"] = dur
        return r

    def grow(self, *records, **kw):
        kw.setdefault("ttl", 300)
        kw.setdefault("price", self.PRICE)
        return usage.leases(list(records), **kw)

    def test_a_write_is_a_lease_and_each_read_makes_it_last_the_ttl_more(self):
        found, marks, orphan = self.grow(self.rec(0, cw=1000), self.rec(100, cr=1000), self.rec(200, cr=1000))
        self.assertEqual(len(found), 1)
        ls = found[0]
        self.assertEqual((ls["lo"], ls["hi"], ls["up"]), (0, 1000, None))
        self.assertEqual((ls["start"], ls["last"], ls["end"], ls["secs"], ls["why"]), (0, 200, 500, 500, "expired"))
        self.assertEqual((ls["write_tok"], ls["reads"], ls["read_tok"]), (1000, 2, 2000))
        self.assertEqual([m["kind"] for m in marks], ["miss", "hit", "hit"])
        self.assertEqual([(m["read"], m["wrote"]) for m in marks], [(None, ls["id"]), (ls["id"], None), (ls["id"], None)])
        self.assertEqual(orphan["reads"], 0)

    def test_a_write_on_top_stands_on_the_lease_that_its_request_read(self):
        found, marks, _ = self.grow(self.rec(0, cw=1000), self.rec(50, cr=1000, cw=200))
        low, top = found
        self.assertEqual((low["lo"], low["hi"], top["lo"], top["hi"]), (0, 1000, 1000, 1200))
        self.assertEqual(top["up"], low["id"])
        self.assertEqual((low["reads"], top["reads"]), (1, 0))
        self.assertEqual((low["end"], top["end"]), (350, 350))  # one read reads both from now on: they end together
        self.assertEqual(marks[1]["kind"], "hit")

    def test_the_size_of_a_hit_is_the_height_of_the_leases_that_it_read(self):
        found, marks, _ = self.grow(self.rec(0, cw=1000), self.rec(10, cr=1000, cw=5000), self.rec(20, cr=6000, cw=100),
                                    self.rec(30, cr=6100, cw=50))
        by = {ls["id"]: ls for ls in found}
        for m in marks:
            height, at = 0, m["read"]
            while at is not None:
                height += by[at]["hi"] - by[at]["lo"]
                at = by[at]["up"]
            self.assertEqual(height, m["cr"])
        self.assertEqual(by[marks[3]["read"]]["hi"], 6100)

    def test_a_gap_past_the_ttl_ends_the_lease_and_the_read_after_it_has_no_lease(self):
        found, marks, orphan = self.grow(self.rec(0, cw=1000), self.rec(400, cr=1000))
        self.assertEqual((found[0]["end"], found[0]["why"], found[0]["reads"]), (300, "expired", 0))
        self.assertEqual((orphan["reads"], orphan["read_tok"]), (1, 1000))
        self.assertEqual(marks[1]["read"], None)
        unseen = found[1]  # the cache that it read is kept from here on: a lease with no write in the log
        self.assertEqual((unseen["unseen"], unseen["lo"], unseen["hi"], unseen["start"], unseen["write_tok"]), (True, 0, 1000, 400, 0))

    def test_a_request_that_reads_less_writes_beside_what_it_did_not_read(self):
        # a fold, or a second agent under the same session id: the cache above what it read stays until its own end
        found, marks, _ = self.grow(self.rec(0, cw=1000), self.rec(10, cr=1000, cw=500), self.rec(20, cr=1000, cw=800))
        base, old, new = found
        self.assertEqual([m["kind"] for m in marks], ["miss", "hit", "part"])
        self.assertEqual((old["lo"], old["hi"], new["lo"], new["hi"]), (1000, 1500, 1000, 1800))
        self.assertEqual((old["up"], new["up"]), (base["id"], base["id"]))
        self.assertEqual((base["reads"], base["end"]), (2, 320))
        self.assertEqual((old["reads"], old["end"]), (0, 310))  # never read: it ends the ttl after its write
        self.assertEqual(new["end"], 320)

    def test_the_chat_reads_its_own_leases_again_after_the_second_agent(self):
        found, marks, _ = self.grow(self.rec(0, cw=1000), self.rec(10, cr=1000, cw=5000), self.rec(20, cr=1000, cw=9000),
                                    self.rec(30, cr=6000, cw=100))
        self.assertEqual([m["kind"] for m in marks], ["miss", "hit", "part", "hit"])
        side = found[2]
        self.assertEqual((side["lo"], side["hi"], side["reads"], side["end"]), (1000, 10000, 0, 320))
        self.assertEqual(found[1]["reads"], 1)

    def test_a_read_that_ends_inside_a_lease_cuts_it_in_two(self):
        found, marks, _ = self.grow(self.rec(0, cw=1000), self.rec(10, cr=400, cw=300))
        low, rest, new = found
        self.assertEqual([(ls["lo"], ls["hi"]) for ls in found], [(0, 400), (400, 1000), (400, 700)])
        self.assertEqual((rest["up"], new["up"]), (low["id"], low["id"]))
        self.assertEqual((low["reads"], low["end"], rest["reads"], rest["end"]), (1, 310, 0, 300))
        self.assertEqual(marks[1]["kind"], "part")
        self.assertAlmostEqual(low["store_usd"], 400 * 3.75 / 1e6)  # the money of the one write, shared by the tokens
        self.assertAlmostEqual(rest["store_usd"], 600 * 3.75 / 1e6)
        self.assertEqual(sum(ls["write_tok"] for ls in found), 1300)

    def test_the_ttl_is_read_off_the_requests_when_none_is_given(self):
        short, _, orphan = self.grow(self.rec(0, cw=1000), self.rec(200, cr=1000), self.rec(900, cw=1000), ttl=None)
        self.assertEqual({ls["ttl"] for ls in short}, {usage.LEASE_TTL})
        self.assertEqual(short[0]["end"], 500)
        long, _, orphan = self.grow(self.rec(0, cw=1000), self.rec(900, cr=1000), ttl=None)  # a read bridged 900 s
        self.assertEqual({ls["ttl"] for ls in long}, {usage.LEASE_TTL_LONG})
        self.assertEqual((long[0]["end"], orphan["reads"]), (900 + 3600, 0))

    def test_a_read_of_only_the_lowest_part_does_not_say_the_long_ttl(self):
        # the system prompt can be the same bytes in another chat, which keeps it alive: only a read of all the cache counts
        found, _, _ = self.grow(self.rec(0, cw=1000), self.rec(10, cr=1000, cw=5000), self.rec(900, cr=1000, cw=5000), ttl=None)
        self.assertEqual({ls["ttl"] for ls in found}, {usage.LEASE_TTL})

    def test_a_lease_that_still_runs_is_open(self):
        found, _, _ = self.grow(self.rec(0, cw=1000), now=100)
        self.assertEqual((found[0]["why"], found[0]["end"]), ("open", 300))
        self.assertEqual(usage.lease_totals(found, [], {})["open"], 1)

    def test_a_request_begins_its_dur_before_its_stamp(self):
        found, marks, _ = self.grow(self.rec(100, cw=1000, dur=30.0), self.rec(140, cr=1000, dur=5.0))
        self.assertEqual((found[0]["start"], found[0]["last"], found[0]["end"]), (70.0, 135.0, 435.0))
        self.assertEqual([(m["at"], m["ts"]) for m in marks], [(70.0, 100), (135.0, 140)])

    def test_the_money_of_a_lease_at_the_rates(self):
        found, marks, orphan = self.grow(self.rec(0, cw=1_000_000), self.rec(10, cr=1_000_000))
        ls = found[0]
        self.assertAlmostEqual(ls["store_usd"], 3.75)   # cw at the cache write rate
        self.assertAlmostEqual(ls["read_usd"], 0.3)     # cr at the cache read rate
        self.assertAlmostEqual(ls["uncached_usd"], 6.0)  # the write and the read at the input rate
        t = usage.lease_totals(found, marks, orphan)
        self.assertEqual((t["n"], t["requests"], t["reads"], t["write_tok"], t["read_tok"]), (1, 2, 1, 1_000_000, 1_000_000))
        self.assertEqual(t["kinds"], {"miss": 1, "hit": 1})
        self.assertAlmostEqual(t["cache_usd"], 4.05)
        self.assertAlmostEqual(t["saved_usd"], 1.95)
        self.assertAlmostEqual(t["ratio"], 0.675)

    def test_a_request_over_a_step_of_the_price_pays_the_higher_rates_for_all_of_it(self):
        found, marks, _ = self.grow(self.rec(0, cw=900, inp=0), self.rec(10, cr=900, cw=200, inp=0), price=self.STEPS)
        self.assertEqual([(m["tier"], ls["tier"]) for m, ls in zip(marks, found)], [(0, 0), (1, 1)])
        self.assertAlmostEqual(found[0]["store_usd"], 900 * 1.25 / 1e6)
        self.assertAlmostEqual(found[1]["store_usd"], 200 * 6.25 / 1e6)
        self.assertAlmostEqual(marks[1]["read_usd"], 900 * 0.5 / 1e6)   # its read too: the prompt is 1100
        self.assertAlmostEqual(found[0]["read_usd"], 900 * 0.5 / 1e6)

    def test_a_write_that_ended_with_no_read_is_in_unread(self):
        found, marks, orphan = self.grow(self.rec(0, cw=1000), self.rec(10, cr=1000, cw=500), now=10_000)
        t = usage.lease_totals(found, marks, orphan)
        self.assertEqual((t["unread"]["n"], t["unread"]["tok"]), (1, 500))
        self.assertAlmostEqual(t["unread"]["usd"], 500 * 3.75 / 1e6)

    def test_no_price_gives_leases_with_no_money(self):
        found, _, _ = self.grow(self.rec(0, cw=1000), self.rec(100, cr=1000), price=lambda model, provider: None)
        self.assertEqual((found[0]["store_usd"], found[0]["read_usd"], found[0]["uncached_usd"]), (0.0, 0.0, 0.0))
        self.assertEqual((found[0]["write_tok"], found[0]["tier"]), (1000, 0))

    def test_helper_calls_are_left_out(self):
        found, marks, orphan = self.grow(self.rec(0, cw=1000, task="goal_judge"), self.rec(100, cr=1000, task="goal_judge"))
        self.assertEqual((found, marks, orphan["reads"]), ([], [], 0))

    def test_sessions_and_models_have_caches_of_their_own(self):
        found, marks, orphan = self.grow(self.rec(0, cw=500, sess="a"), self.rec(5, cr=500, sess="b"),
                                         self.rec(6, cr=500, sess="a", model="m2"))
        self.assertEqual(sorted((ls["sess"], ls["model"], ls["unseen"]) for ls in found),
                         [("a", "m1", False), ("a", "m2", True), ("b", "m1", True)])
        self.assertEqual(orphan["reads"], 2)
        self.assertEqual(len({ls["id"] for ls in found}), 3)

    def test_the_tokens_of_the_leases_are_the_tokens_of_the_log(self):
        rs = [self.rec(0, cw=1000), self.rec(10, cr=1000, cw=5000), self.rec(20, cr=6000, cw=100), self.rec(30, cr=1000, cw=4000),
              self.rec(40, cr=5000, cw=10), self.rec(50, cr=300, cw=20), self.rec(900, cr=5010, cw=7), self.rec(910, cr=5017)]
        found, marks, orphan = self.grow(*rs)
        self.assertEqual(sum(ls["write_tok"] for ls in found), sum(r["cw"] for r in rs))
        self.assertEqual(sum(ls["read_tok"] for ls in found) + orphan["read_tok"], sum(r["cr"] for r in rs))
        self.assertAlmostEqual(sum(ls["store_usd"] for ls in found), sum(r["cw"] for r in rs) * 3.75 / 1e6)
        by = {ls["id"]: ls for ls in found}
        for ls in found:  # a lease stands on the top of an earlier lease, which lasts at least as long
            if ls["up"] is not None:
                self.assertEqual(by[ls["up"]]["hi"], ls["lo"])
                self.assertLess(ls["up"], ls["id"])
                self.assertGreaterEqual(by[ls["up"]]["end"], ls["end"])

if __name__ == "__main__":
    unittest.main()
