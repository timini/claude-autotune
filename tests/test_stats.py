"""Known-value tests for autotune.stats.

Published values: Newcombe RG (1998), "Two-sided confidence intervals for
the single proportion: comparison of seven methods", Statistics in Medicine
17:857-872, Table I (Wilson score method without continuity correction with
z = 1.96, and the exact Clopper-Pearson method), for 15/148, 0/20, 1/29 and
81/263. The 0/10 Clopper-Pearson upper limit 0.3085 is the standard table
value 1 - 0.025 ** (1 / 10).
"""

from __future__ import annotations

import math
import unittest
from fractions import Fraction

from autotune import stats


def exact_cdf(x: int, n: int, p: Fraction) -> Fraction:
    return sum(math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(x + 1))


class WilsonTests(unittest.TestCase):
    def test_newcombe_table_values(self):
        cases = {
            (81, 263): (0.2553, 0.3662),
            (15, 148): (0.0624, 0.1605),
            (0, 20): (0.0, 0.1611),
            (1, 29): (0.0061, 0.1718),
        }
        for (x, n), (lo, hi) in cases.items():
            got = stats.wilson_interval(x, n, z=1.96)
            self.assertAlmostEqual(got[0], lo, places=4, msg=(x, n))
            self.assertAlmostEqual(got[1], hi, places=4, msg=(x, n))

    def test_longhand_value(self):
        # 8 of 10, z = 1.96: centre (0.8 + 0.192080) / 1.384160, half width
        # 1.96 / 1.384160 * sqrt(0.016 + 0.009604).
        z = 1.96
        centre = (0.8 + z * z / 20) / (1 + z * z / 10)
        half = z / (1 + z * z / 10) * math.sqrt(0.8 * 0.2 / 10 + z * z / 400)
        lo, hi = stats.wilson_interval(8, 10, z=z)
        self.assertAlmostEqual(lo, centre - half, places=12)
        self.assertAlmostEqual(hi, centre + half, places=12)
        self.assertAlmostEqual(lo, 0.4902, places=4)
        self.assertAlmostEqual(hi, 0.9433, places=4)

    def test_default_z_is_the_exact_quantile(self):
        self.assertAlmostEqual(stats.z_value(0.95), 1.959963985, places=8)
        lo, hi = stats.wilson_interval(15, 148)
        self.assertAlmostEqual(lo, 0.0624, places=3)
        self.assertAlmostEqual(hi, 0.1605, places=3)

    def test_edges_and_symmetry(self):
        self.assertIsNone(stats.wilson_interval(0, 0))
        self.assertEqual(stats.wilson_interval(0, 5)[0], 0.0)
        self.assertEqual(stats.wilson_interval(5, 5)[1], 1.0)
        lo, hi = stats.wilson_interval(3, 17)
        lo2, hi2 = stats.wilson_interval(14, 17)
        self.assertAlmostEqual(lo, 1 - hi2, places=12)
        self.assertAlmostEqual(hi, 1 - lo2, places=12)

    def test_bad_counts(self):
        for x, n in ((-1, 5), (6, 5), (1, -1)):
            with self.assertRaises(ValueError):
                stats.wilson_interval(x, n)
        with self.assertRaises(TypeError):
            stats.wilson_interval(1.0, 5)


class ClopperPearsonTests(unittest.TestCase):
    def test_newcombe_table_values(self):
        cases = {
            (15, 148): (0.0578, 0.1617),
            (0, 20): (0.0, 0.1684),
            (1, 29): (0.0009, 0.1776),
        }
        for (x, n), (lo, hi) in cases.items():
            got = stats.clopper_pearson(x, n)
            self.assertAlmostEqual(got[0], lo, places=4, msg=(x, n))
            self.assertAlmostEqual(got[1], hi, places=4, msg=(x, n))

    def test_zero_of_ten_table_value(self):
        self.assertAlmostEqual(stats.clopper_pearson(0, 10)[1], 0.3085, places=4)

    def test_bounds_solve_the_binomial_tails_exactly(self):
        lo, hi = stats.clopper_pearson(81, 263)
        self.assertAlmostEqual(lo, 0.2527, places=4)
        self.assertAlmostEqual(hi, 0.3676, places=4)
        self.assertAlmostEqual(float(exact_cdf(81, 263, Fraction(hi))), 0.025, places=9)
        self.assertAlmostEqual(float(1 - exact_cdf(80, 263, Fraction(lo))), 0.025, places=9)

    def test_one_sided_upper_closed_form_at_zero(self):
        for n in (10, 40, 52, 200):
            self.assertAlmostEqual(stats.clopper_pearson_upper(0, n), 1 - 0.05 ** (1 / n), places=12)
        self.assertAlmostEqual(stats.clopper_pearson_upper(0, 40), 0.0722, places=4)
        # Rule of three: about 3 / n for large n.
        self.assertAlmostEqual(stats.clopper_pearson_upper(0, 300) / (3 / 300), 1.0, delta=0.01)

    def test_one_sided_upper_with_events(self):
        up = stats.clopper_pearson_upper(1, 40)
        self.assertAlmostEqual(float(exact_cdf(1, 40, Fraction(up))), 0.05, places=9)
        self.assertAlmostEqual(up, 0.1132, places=4)

    def test_two_sided_upper_matches_interval(self):
        self.assertAlmostEqual(stats.clopper_pearson_upper(0, 40, one_sided=False), 0.0881, places=4)
        self.assertAlmostEqual(stats.clopper_pearson_upper(1, 29, one_sided=False),
                               stats.clopper_pearson(1, 29)[1], places=12)

    def test_edges(self):
        self.assertIsNone(stats.clopper_pearson(0, 0))
        self.assertIsNone(stats.clopper_pearson_upper(0, 0))
        self.assertEqual(stats.clopper_pearson_upper(7, 7), 1.0)
        self.assertEqual(stats.clopper_pearson(7, 7)[1], 1.0)
        self.assertEqual(stats.clopper_pearson(0, 7)[0], 0.0)
        with self.assertRaises(ValueError):
            stats.clopper_pearson(1, 5, confidence=1.0)


class KappaTests(unittest.TestCase):
    def test_hand_computed_three_category_table(self):
        # Pairs (0,0) (1,2) (2,2) (2,1); weights (i-j)^2/4. Observed weighted
        # disagreement (1/4 + 1/4) / 4 = 1/8. Both marginals (1/4, 1/4, 1/2),
        # expected weighted disagreement 11/32. kappa = 1 - (1/8)/(11/32) = 7/11.
        got = stats.quadratic_weighted_kappa([0, 1, 2, 2], [0, 2, 2, 1], 0, 2)
        self.assertAlmostEqual(got, 7 / 11, places=12)

    def test_hand_computed_five_category_table(self):
        # Pairs (4,4) (3,4) (0,1) (2,2) (1,1) (0,0): w = d^2/16. Observed
        # disagreement (1/16 + 1/16) / 6 = 1/48. Marginals a: 0:2,1:1,2:1,3:1,4:1;
        # b: 0:1,1:2,2:1,4:2. Hand sum of n_i * m_j * (i-j)^2 by row of a:
        # 76 + 20 + 14 + 20 + 38 = 168, so expected disagreement 168 / (16 * 36)
        # = 7/24 and kappa = 1 - (1/48)/(7/24) = 13/14.
        a = [4, 3, 0, 2, 1, 0]
        b = [4, 4, 1, 2, 1, 0]
        num = Fraction(1, 48)
        den = Fraction(sum(a.count(i) * b.count(j) * (i - j) ** 2 for i in range(5) for j in range(5)),
                       16 * 36)
        self.assertEqual(den, Fraction(7, 24))
        self.assertAlmostEqual(stats.quadratic_weighted_kappa(a, b), float(1 - num / den), places=12)
        self.assertAlmostEqual(stats.quadratic_weighted_kappa(a, b), 13 / 14, places=12)

    def test_perfect_agreement_is_one(self):
        self.assertEqual(stats.quadratic_weighted_kappa([0, 1, 2, 3, 4], [0, 1, 2, 3, 4]), 1.0)

    def test_observed_equal_to_expected_is_zero(self):
        # Every cell of the 2x2 table on {0, 4} has one pair: observed equals expected.
        self.assertAlmostEqual(stats.quadratic_weighted_kappa([0, 0, 4, 4], [0, 4, 0, 4]), 0.0, places=12)

    def test_full_disagreement_is_negative(self):
        self.assertAlmostEqual(stats.quadratic_weighted_kappa([0, 4], [4, 0]), -1.0, places=12)

    def test_undefined_cases(self):
        self.assertIsNone(stats.quadratic_weighted_kappa([], []))
        self.assertIsNone(stats.quadratic_weighted_kappa([2, 2, 2], [2, 2, 2]))

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            stats.quadratic_weighted_kappa([1], [1, 2])
        with self.assertRaises(ValueError):
            stats.quadratic_weighted_kappa([5], [1])
        with self.assertRaises(ValueError):
            stats.quadratic_weighted_kappa([True], [1])


class AgreementTests(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(stats.agreement([(1, 1), (0, 1), (True, True), (None, None)]), (3, 4, 0.75))
        self.assertEqual(stats.agreement([]), (0, 0, None))


if __name__ == "__main__":
    unittest.main()
