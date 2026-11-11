"""Rates with honest intervals: the 95% Beta interval every report prints beside a rate."""

from __future__ import annotations

import random
from typing import Any

#: The seed behind every sampled interval. Fixed, and that is the point: a
#: gauntlet re-reported must not move its own confidence bounds, or a reader
#: comparing two runs is reading Monte Carlo noise as signal.
INTERVAL_SEED = 20260910

#: Draws behind each interval. `gauntlet_ev.py` uses 4000 for the same reason
#: it gives - 200 resolved a 2.5th percentile off ten order statistics and
#: moved by more than a percentage point on the seed alone.
INTERVAL_SAMPLES = 4000


def beta_interval(successes: int, trials: int) -> dict[str, Any]:
    """A 95% central interval for a rate, from `Beta(1 + k, 1 + n - k)`.

    Uniform prior, because the alternative is to seed the answer with an
    opinion about how often a seat *should* win. Sampled rather than solved,
    following `mtg-decks/scripts/gauntlet_ev.py::_beta_ci95` - the exact
    alternative is an incomplete-beta inverse, and this is a reported
    interval on a row rather than the quantity a verdict turns on - but with
    a fixed seed, so the same ledger reports the same bounds every time.

    `point` is what was observed, `k / n`, and is `None` at zero trials
    because nothing was observed. `mean` is the posterior mean, which is
    always inside the interval and is what a reader should quote when `k` is
    0 or `n`: 0 wins in 4 games is not a 0% win rate, it is "somewhere under
    a half, and the interval says how far under".
    """
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError(f"{successes} successes out of {trials} trials is not a rate")
    alpha, beta = 1.0 + successes, 1.0 + trials - successes
    rng = random.Random(INTERVAL_SEED)
    draws = sorted(rng.betavariate(alpha, beta) for _ in range(INTERVAL_SAMPLES))
    low = draws[int(0.025 * (INTERVAL_SAMPLES - 1))]
    high = draws[int(0.975 * (INTERVAL_SAMPLES - 1))]
    return {
        "successes": successes,
        "trials": trials,
        "point": (successes / trials) if trials else None,
        "mean": alpha / (alpha + beta),
        "low": low,
        "high": high,
        "half_width": (high - low) / 2.0,
    }
