"""Small exact statistics for calibration and gate reports.

Stdlib only. Every function takes counts or rating lists and returns plain
floats (or None when the statistic is undefined), so reports can store them
as JSON directly.
"""

from __future__ import annotations

import math
from statistics import NormalDist

_BISECT_ITERATIONS = 200


def _check_counts(x: int, n: int) -> None:
    if isinstance(x, bool) or isinstance(n, bool) or not isinstance(x, int) or not isinstance(n, int):
        raise TypeError("x and n must be integers")
    if n < 0 or x < 0 or x > n:
        raise ValueError(f"need 0 <= x <= n, got x={x}, n={n}")


def _check_confidence(confidence: float) -> None:
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")


def z_value(confidence: float = 0.95) -> float:
    """Two-sided standard normal quantile, 1.959964 for 0.95."""
    _check_confidence(confidence)
    return NormalDist().inv_cdf((1.0 + confidence) / 2.0)


def wilson_interval(x: int, n: int, confidence: float = 0.95, *, z: float | None = None):
    """Wilson score interval for x successes in n trials, without continuity
    correction. Returns (low, high), or None when n is 0. `z` overrides the
    quantile (for example 1.96 to match tables that round it)."""
    _check_counts(x, n)
    if n == 0:
        return None
    if z is None:
        z = z_value(confidence)
    p = x / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    low = 0.0 if x == 0 else max(0.0, centre - half)
    high = 1.0 if x == n else min(1.0, centre + half)
    return low, high


def _log_pmf(k: int, n: int, p: float) -> float:
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def binom_cdf(x: int, n: int, p: float) -> float:
    """P(X <= x) for X ~ Binomial(n, p)."""
    _check_counts(x, n)
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if x >= n else 0.0
    return min(1.0, sum(math.exp(_log_pmf(k, n, p)) for k in range(x + 1)))


def binom_sf(x: int, n: int, p: float) -> float:
    """P(X >= x) for X ~ Binomial(n, p)."""
    _check_counts(x, n)
    if x == 0:
        return 1.0
    return max(0.0, 1.0 - binom_cdf(x - 1, n, p))


def _bisect(fn, target: float, increasing: bool) -> float:
    """p in [0, 1] with fn(p) == target for a monotone fn."""
    lo, hi = 0.0, 1.0
    for _ in range(_BISECT_ITERATIONS):
        mid = (lo + hi) / 2.0
        value = fn(mid)
        if (value < target) == increasing:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-15:
            break
    return (lo + hi) / 2.0


def clopper_pearson(x: int, n: int, confidence: float = 0.95):
    """Exact two-sided Clopper-Pearson interval. Returns (low, high), or None
    when n is 0. Each tail has probability (1 - confidence) / 2."""
    _check_counts(x, n)
    _check_confidence(confidence)
    if n == 0:
        return None
    tail = (1.0 - confidence) / 2.0
    low = 0.0 if x == 0 else _bisect(lambda p: binom_sf(x, n, p), tail, increasing=True)
    high = 1.0 if x == n else _bisect(lambda p: binom_cdf(x, n, p), tail, increasing=False)
    return low, high


def clopper_pearson_upper(x: int, n: int, confidence: float = 0.95, *, one_sided: bool = True):
    """Exact Clopper-Pearson upper bound on a rate from x events in n trials.

    One-sided by default: the p with P(X <= x; n, p) = 1 - confidence. For
    x = 0 that is 1 - (1 - confidence) ** (1 / n), 0.0722 for n = 40 at 95%.
    With one_sided=False it is the upper end of the two-sided interval
    (tail (1 - confidence) / 2), 0.0881 for 0 of 40. None when n is 0."""
    _check_counts(x, n)
    _check_confidence(confidence)
    if n == 0:
        return None
    if x == n:
        return 1.0
    tail = (1.0 - confidence) if one_sided else (1.0 - confidence) / 2.0
    if x == 0:
        return 1.0 - tail ** (1.0 / n)
    return _bisect(lambda p: binom_cdf(x, n, p), tail, increasing=False)


def quadratic_weighted_kappa(a, b, min_rating: int = 0, max_rating: int = 4):
    """Cohen's kappa with quadratic weights for two paired rating lists.

    Weights are (i - j)^2 / (k - 1)^2 over the categories min_rating..max_rating.
    kappa = 1 - sum(w * observed) / sum(w * expected), where expected is the
    outer product of the two raters' marginal proportions. Returns None when
    there are no pairs or the expected disagreement is 0 (both raters used
    one and the same category, so kappa is undefined)."""
    a, b = list(a), list(b)
    if len(a) != len(b):
        raise ValueError("rating lists differ in length")
    if max_rating <= min_rating:
        raise ValueError("max_rating must exceed min_rating")
    if not a:
        return None
    k = max_rating - min_rating + 1
    for r in a + b:
        if isinstance(r, bool) or not isinstance(r, int) or not min_rating <= r <= max_rating:
            raise ValueError(f"rating {r!r} outside {min_rating}..{max_rating}")
    n = len(a)
    observed = [[0] * k for _ in range(k)]
    for x, y in zip(a, b):
        observed[x - min_rating][y - min_rating] += 1
    row = [sum(observed[i]) for i in range(k)]
    col = [sum(observed[i][j] for i in range(k)) for j in range(k)]
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            w = (i - j) ** 2 / (k - 1) ** 2
            num += w * observed[i][j] / n
            den += w * (row[i] / n) * (col[j] / n)
    if den == 0.0:
        return None
    return 1.0 - num / den


def agreement(pairs) -> tuple[int, int, float | None]:
    """Exact agreement over (x, y) pairs: (agreeing, total, rate or None)."""
    pairs = list(pairs)
    agree = sum(1 for x, y in pairs if x == y)
    return agree, len(pairs), (agree / len(pairs) if pairs else None)
