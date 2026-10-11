"""Fitted upper-tail null model for spectral-library search significance.

Audit F12 / roadmap #12.  The search null used to be sampled at a fixed multiple
of the library size (``NULL_DECOY_MULTIPLIER``), which made the per-query
*statistical* cost linear in the library and floored the smallest expressible
q-value at ``m / (n_null + 1)``: an empirical count over ``n_null`` decoys cannot
express a p-value below ``1 / (n_null + 1)``, so a large library could not report
a strong match however good it was.

This module replaces the sampled null with a fitted one.  A fixed-size decoy
sample is drawn (constant cost), the upper tail of the decoy score distribution
is fitted with a generalised Pareto distribution (GPD) over exceedances above a
high threshold (peaks-over-threshold), and p-values above the threshold are
evaluated from the fitted survival function analytically.  The sample-size floor
therefore no longer applies.

What is claimed, and what is not
--------------------------------
* The model describes the *decoy* score distribution, so a p-value is "how often
  a permuted-intensity spectrum scores at least this high", nothing more.
* The fit is conservative and transparent.  A degenerate tail (too few
  exceedances, zero spread) falls back to an exponential tail — the ``xi -> 0``
  special case of the GPD — and the report names which model was used.
* A p-value is never reported as zero: :data:`P_FLOOR` keeps it finite and the
  report states the smallest attainable q, so a numerical floor cannot be read
  as a measured result.
* The GPD is fitted by the **method of moments**, which is closed-form and has no
  scipy dependency.  That is a choice, not a law: :func:`compare_to_sampled`
  measures the disagreement between the fitted and sampled p-values so the
  approximation is quantified rather than asserted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "DEFAULT_MIN_EXCEEDANCES",
    "DEFAULT_TAIL_FRACTION",
    "P_FLOOR",
    "SampledComparison",
    "TailFit",
    "compare_to_sampled",
    "fit_upper_tail",
]

DEFAULT_TAIL_FRACTION: Final[float] = 0.10
"""Fraction of the decoy sample treated as the fitted upper tail."""

DEFAULT_MIN_EXCEEDANCES: Final[int] = 50
"""Fewest exceedances a fit is attempted from."""

P_FLOOR: Final[float] = 1e-12
"""Smallest p-value the model reports.

A fitted tail has no *sampling* floor, but a bounded score distribution has a
finite endpoint and the q-value arithmetic needs a positive number, so a
p-value is clamped here.  The report prints the q this implies so the clamp is
visible rather than hidden.
"""

_XI_LIMIT: Final[float] = 0.5
"""GPD shape at or above which the variance is unbounded; not used."""

_XI_FLOOR: Final[float] = -0.9
"""GPD shape below which the fit is rejected in favour of the exponential."""

_GPD: Final[str] = "generalized Pareto upper tail (method of moments)"
_EXPONENTIAL: Final[str] = "exponential upper tail"


@dataclass(frozen=True, slots=True)
class TailFit:
    """A fitted upper-tail null model over an ordered decoy score sample.

    ``p_value`` returns the empirical estimate at or below ``threshold`` and the
    fitted GPD survival above it, so the two agree in the body and the fitted
    tail only governs the extreme scores it exists to describe.
    """

    n: int
    """Number of decoy scores the model was fitted to."""

    threshold: float
    """Score above which the fitted tail (rather than the empirical count) applies."""

    n_exceed: int
    """Decoy scores at or above ``threshold`` used for the fit."""

    sigma: float
    """GPD scale parameter."""

    xi: float
    """GPD shape parameter (``0`` for the exponential fallback)."""

    model: str
    """Human-readable name of the fitted model."""

    null_sorted: tuple[float, ...]
    """The decoy scores, ascending, for the empirical body."""

    def survival(self, excess: float) -> float:
        """Return the fitted survival ``P(score - threshold >= excess)``."""
        if excess <= 0.0:
            return 1.0
        if self.xi == 0.0:
            return math.exp(-excess / self.sigma)
        base = 1.0 + self.xi * excess / self.sigma
        if base <= 0.0:  # beyond the fitted upper endpoint (xi < 0)
            return 0.0
        return float(base ** (-1.0 / self.xi))

    def p_value(self, score: float) -> float:
        """Return the p-value of *score* under the fitted null model."""
        if score <= self.threshold:
            null = np.asarray(self.null_sorted, dtype=np.float64)
            count_ge = self.n - int(np.searchsorted(null, score, side="left"))
            return min(1.0, (1.0 + count_ge) / (1.0 + self.n))
        fraction_above = self.n_exceed / self.n
        p = fraction_above * self.survival(score - self.threshold)
        return min(1.0, max(p, P_FLOOR))

    def p_values(self, scores: list[float]) -> list[float]:
        """Return p-values for *scores* (see :meth:`p_value`)."""
        return [self.p_value(s) for s in scores]

    def smallest_attainable_p(self) -> float:
        """Return the smallest p the fitted model assigns to a perfect match.

        Evaluated at the maximum cosine of ``1.0`` and clamped at
        :data:`P_FLOOR`, so the number the report prints is the model's floor,
        not a measurement.
        """
        return max(self.p_value(1.0), P_FLOOR)

    def smallest_attainable_q(self, n_targets: int) -> float:
        """Return the smallest q the fitted model can express for *n_targets*.

        The top-ranked hit is charged the full Benjamini-Hochberg factor, so the
        floor is ``p_min * n_targets`` (capped at 1).
        """
        return min(1.0, self.smallest_attainable_p() * max(n_targets, 1))

    def describe(self) -> str:
        """Return a compact, report-ready description of the fitted model."""
        return (
            f"{self.model}, threshold {self.threshold:.4g}, "
            f"shape {self.xi:+.3f}, scale {self.sigma:.4g}, "
            f"{self.n_exceed:,} exceedances from {self.n:,} decoy scores"
        )


def fit_upper_tail(
    scores: list[float] | tuple[float, ...] | np.ndarray,
    *,
    tail_fraction: float = DEFAULT_TAIL_FRACTION,
    min_exceedances: int = DEFAULT_MIN_EXCEEDANCES,
) -> TailFit:
    """Fit an upper-tail GPD (by moments) to *scores*.

    The threshold is the ``1 - tail_fraction`` sample quantile, raised to at
    least *min_exceedances* exceedances.  The exceedances are the top *k* scores
    minus the threshold (so an exceedance may be exactly ``0`` when ties sit on
    the threshold, which the GPD handles as ``survival(0) = 1``).

    Method-of-moments estimates are ``xi = (1 - mean^2 / var) / 2`` and
    ``sigma = mean * (1 - xi)``; they are closed-form and need no scipy.  A
    non-positive spread, or a shape outside ``(-0.9, 0.5)``, falls back to an
    exponential tail fitted to the positive scores, because a GPD that cannot be
    estimated must not be reported as one.

    Raises
    ------
    ValueError
        If *scores* holds fewer than two values.
    """
    arr = np.sort(np.asarray(scores, dtype=np.float64))
    n = int(arr.size)
    if n < 2:
        raise ValueError(
            f"cannot fit a null model to {n} decoy score(s); at least two are needed"
        )

    k = max(min_exceedances, round(tail_fraction * n))
    k = min(max(k, 2), n)
    threshold = float(arr[n - k])
    top = arr[n - k :] - threshold
    mean = float(np.mean(top))
    var = float(np.var(top, ddof=1)) if k > 1 else 0.0

    if mean > 0.0 and var > 0.0:
        xi = (1.0 - (mean * mean) / var) / 2.0
        sigma = mean * (1.0 - xi)
    else:
        xi, sigma = 0.0, 0.0

    if sigma > 0.0 and _XI_FLOOR < xi < _XI_LIMIT:
        model = _GPD
    else:
        positives = arr[arr > 0.0]
        sigma = float(np.mean(positives)) if positives.size else P_FLOOR
        xi = 0.0
        model = _EXPONENTIAL

    return TailFit(
        n=n,
        threshold=threshold,
        n_exceed=k,
        sigma=sigma,
        xi=xi,
        model=model,
        null_sorted=tuple(float(s) for s in arr),
    )


@dataclass(frozen=True, slots=True)
class SampledComparison:
    """Disagreement between the fitted p-values and the sampled empirical ones."""

    n_points: int
    max_abs_diff: float
    mean_abs_diff: float

    def describe(self) -> str:
        """Return a report-ready summary of the disagreement."""
        return (
            f"fitted vs sampled p-value: max |Δ| = {self.max_abs_diff:.3g}, "
            f"mean |Δ| = {self.mean_abs_diff:.3g} over {self.n_points} points"
        )


def compare_to_sampled(
    fit: TailFit,
    sampled_scores: list[float] | tuple[float, ...] | np.ndarray,
    *,
    points: int = 256,
) -> SampledComparison:
    """Quantify how far the fitted p-values sit from the sampled ones.

    Both p-values are evaluated on a grid of scores spanning the fit threshold to
    ``max(sampled, threshold)`` (and ``1.0``), using the same inclusive tie
    convention, and the absolute differences are summarised.  This is the check
    that turns "the tail is fitted" from an assertion into a measurement: a fit
    that departs wildly from the decoy sample it came from is visible here.
    """
    sample = np.sort(np.asarray(sampled_scores, dtype=np.float64))
    n_sample = int(sample.size)
    if n_sample == 0:
        raise ValueError("cannot compare against an empty decoy sample")

    upper = max(float(sample[-1]), float(fit.threshold), 1.0)
    grid = np.linspace(float(fit.threshold), upper, max(points, 2))

    diffs: list[float] = []
    for score in grid:
        count_ge = n_sample - int(np.searchsorted(sample, score, side="left"))
        p_sampled = min(1.0, (1.0 + count_ge) / (1.0 + n_sample))
        diffs.append(abs(fit.p_value(float(score)) - p_sampled))

    arr = np.asarray(diffs, dtype=np.float64)
    return SampledComparison(
        n_points=int(arr.size),
        max_abs_diff=float(np.max(arr)),
        mean_abs_diff=float(np.mean(arr)),
    )
