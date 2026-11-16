"""Benchmark metrics (spec §13.3): quality, diversity, coverage, efficiency, decomposition.

All metrics operate on *indicator pools* (rows = feasible selections with exact objectives), the
common currency produced by the quantum pipeline and every baseline.
"""

from __future__ import annotations

import numpy as np

from qportfolio.utils.linalg import pairwise_jaccard


def optimality_gap_pct(found: float | None, best_known: float) -> float | None:
    """``100·(found − best)/|best|`` (spec §10.8); ``None`` if nothing feasible was found."""
    if found is None or not np.isfinite(found):
        return None
    return 100.0 * (found - best_known) / max(abs(best_known), 1e-12)


def count_in_band(objective: np.ndarray, best_known: float, eps_pct: float) -> int:
    """Distinct portfolios within ``eps_pct`` % of the best known objective."""
    if len(objective) == 0:
        return 0
    return int((objective <= best_known + abs(best_known) * eps_pct / 100.0 + 1e-15).sum())


def band_diversity(
    indicators: np.ndarray, objective: np.ndarray, best_known: float, eps_pct: float, m: int = 10
) -> dict[str, float]:
    """Jaccard diversity of up to ``m`` farthest-point-selected portfolios inside the band."""
    if len(objective) == 0:
        return {"n_band": 0.0, "mean_jaccard": 0.0, "min_jaccard": 0.0}
    mask = objective <= best_known + abs(best_known) * eps_pct / 100.0 + 1e-15
    X = indicators[mask]
    n_band = float(len(X))
    if len(X) < 2:
        return {"n_band": n_band, "mean_jaccard": 0.0, "min_jaccard": 0.0}
    d = pairwise_jaccard(X)
    chosen = [0]
    mind = d[0].copy()
    while len(chosen) < min(m, len(X)):
        mind[chosen] = -1.0
        j = int(np.argmax(mind))
        if mind[j] <= 0:
            break
        chosen.append(j)
        mind = np.minimum(mind, d[j])
    sub = d[np.ix_(chosen, chosen)]
    iu = np.triu_indices(len(chosen), 1)
    return {
        "n_band": n_band,
        "mean_jaccard": float(sub[iu].mean()) if len(iu[0]) else 0.0,
        "min_jaccard": float(sub[iu].min()) if len(iu[0]) else 0.0,
    }


def coverage_of_reference(
    indicators: np.ndarray, reference: np.ndarray, radius: float = 0.2
) -> float:
    """Fraction of reference near-optimal portfolios that have a found portfolio within
    Jaccard distance ``radius`` — "coverage of near-optimal regions" (spec §7.4)."""
    if len(reference) == 0 or len(indicators) == 0:
        return 0.0
    x = np.asarray(indicators, dtype=float)
    r = np.asarray(reference, dtype=float)
    inter = r @ x.T
    union = r.sum(1)[:, None] + x.sum(1)[None, :] - inter
    d = 1.0 - inter / np.maximum(union, 1.0)
    return float((d.min(axis=1) <= radius).mean())


def decomposition_loss_pct(decomposed_best: float | None, full_best: float | None) -> float | None:
    """F6-R7: how much worse the decomposed best is than the best full-problem result."""
    if decomposed_best is None or full_best is None:
        return None
    return 100.0 * (decomposed_best - full_best) / max(abs(full_best), 1e-12)


def summarize(values: list[float | None]) -> dict[str, float]:
    """Median and a bootstrap-free normal 95% half-width over seeds (distributions, not champions)."""
    v = np.array([x for x in values if x is not None and np.isfinite(x)], dtype=float)
    if len(v) == 0:
        return {"n": 0, "median": float("nan"), "mean": float("nan"), "ci95": float("nan")}
    ci = 1.96 * v.std(ddof=1) / np.sqrt(len(v)) if len(v) > 1 else float("nan")
    return {"n": len(v), "median": float(np.median(v)), "mean": float(v.mean()), "ci95": float(ci)}
