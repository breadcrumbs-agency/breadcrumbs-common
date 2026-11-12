"""breadcrumbs/metrics.py: one metric type, one fold, one table. A ratio folds as a ratio of sums."""
import unittest

from breadcrumbs import metrics


class MetricTest(unittest.TestCase):

    def test_a_ratio_and_only_a_ratio_names_its_parts(self):
        m = metrics.metric("hit", "hit", "share", "ratio", of=("cr", ("in", "cr")))
        self.assertEqual(m["of"], ["cr", ["in", "cr"]])
        self.assertEqual(metrics.part(m, {"cr": 3, "in": 1}), (3, 4))
        with self.assertRaises(ValueError):
            metrics.metric("hit", "hit", "share", "ratio")
        with self.assertRaises(ValueError):
            metrics.metric("n", "n", "count", "sum", of=("a", ("b",)))
        with self.assertRaises(ValueError):
            metrics.metric("n", "n", "count", "mean")
        self.assertNotIn("of", metrics.metric("n", "n", "count"))

    def test_the_entry_has_the_fields_that_the_viewer_reads(self):
        m = metrics.metric("u_cr", "cache read", "tokens", group="usage", src="usage.log cr=", units=("time", "request"),
                           keyed=True)
        self.assertEqual(m, {"key": "u_cr", "label": "cache read", "unit": "tokens", "agg": "sum", "group": "usage",
                             "src": "usage.log cr=", "units": ["time", "request"], "keyed": True, "walks": False})

    def test_sum_and_max_fold_as_before(self):
        self.assertEqual(metrics.fold("sum", 2, 3), 5)
        self.assertEqual(metrics.fold("max", 2, 3), 3)
        self.assertEqual(metrics.value("sum", 5), 5)
        self.assertIsNone(metrics.value("sum", None))

    def test_a_ratio_folds_as_a_ratio_of_sums_at_cell_row_and_total(self):
        agg = {"hit": "ratio", "n": "sum", "top": "max"}
        row_a, row_b = {}, {}
        # row a, column 0: two records 1/2 and 9/10 -> the cell is 10/12, not the mean of 0.5 and 0.9
        metrics.put(row_a, (0, "hit"), (1, 2), 100, "x", "ratio")
        metrics.put(row_a, (0, "hit"), (9, 10), 101, "x", "ratio")
        metrics.put(row_a, (1, "hit"), (0, 8), 200, "x", "ratio")
        metrics.put(row_b, (0, "hit"), (3, 30), 150, "x", "ratio")
        for acc in (row_a, row_b):
            metrics.put(acc, (0, "n"), 2, 100, "x")
            metrics.put(acc, (0, "top"), 7 if acc is row_a else 5, 100, "x", "max")
        cell = row_a[(0, "hit")]
        self.assertEqual((cell.v, cell.n, cell.t0, cell.t1), ((10, 12), 2, 100, 101))
        self.assertAlmostEqual(metrics.value("ratio", cell.v), 10 / 12)
        # the row total: the parts of every column of the row
        row = {}
        metrics.roll(row, {k: c.v for k, c in row_a.items() if k[1] == "hit"}, lambda k: "ratio")
        both = (row[(0, "hit")][0] + row[(1, "hit")][0], row[(0, "hit")][1] + row[(1, "hit")][1])
        self.assertAlmostEqual(metrics.value("ratio", both), 10 / 20)
        # the grand total: the parts of every row
        total = {}
        for acc in (row_a, row_b):
            metrics.roll(total, {k: c.v for k, c in acc.items()}, lambda k: agg[k[1]])
        self.assertEqual(total[(0, "hit")], (13, 42))
        self.assertAlmostEqual(metrics.value("ratio", total[(0, "hit")]), 13 / 42)
        self.assertNotAlmostEqual(metrics.value("ratio", total[(0, "hit")]), (10 / 12 + 3 / 30) / 2, msg="never a mean of ratios")
        self.assertEqual((total[(0, "n")], total[(0, "top")]), (4, 7))

    def test_a_denominator_of_0_gives_no_number(self):
        self.assertIsNone(metrics.value("ratio", (0, 0)))
        self.assertIsNone(metrics.value("ratio", (5, 0)))
        self.assertIsNone(metrics.value("ratio", None))
        self.assertEqual(metrics.value("ratio", (0, 4)), 0)

    def test_the_table_keeps_the_order_and_refuses_a_second_entry_for_a_key(self):
        a = metrics.metric("test_metrics_a", "a", "count")
        b = metrics.metric("test_metrics_b", "b", "share", "ratio", of=("x", ("y",)))
        self.assertEqual(metrics.add(a, b), [a, b])
        self.assertEqual(metrics.add(a), [a], "the same entry again is no error")
        keys = [m["key"] for m in metrics.table()]
        self.assertLess(keys.index("test_metrics_a"), keys.index("test_metrics_b"))
        self.assertIs(metrics.get("test_metrics_b"), b)
        with self.assertRaises(ValueError):
            metrics.add(metrics.metric("test_metrics_a", "another a", "count"))
        self.assertIsNone(metrics.get("nope"))


if __name__ == "__main__":
    unittest.main()
