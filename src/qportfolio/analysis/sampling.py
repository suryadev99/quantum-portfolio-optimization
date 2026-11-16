"""Sampling-quality measurement for one cluster sub-problem (spec C5-R10, hypotheses H9/H10/H18).

Compares samplers on the SAME cluster QUBO at the SAME shot budget against the exact optimum, using
numbers that do not depend on how the sampler is trained:

* ``mass_on_best``      share of shots that landed on the exact best selection;
* ``top_q_recall``      share of the exact top-q selections that appear in the returned pool;
* ``feasible_fraction`` share of raw shots with exactly k ones (before any repair);
* ``best_gap``          objective gap of the best returned selection to the exact optimum.

Enumeration makes this exact only for small clusters (``MAX_STUDY_N``); larger ones are refused.
"""

from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from qportfolio.config.schema import SolverConfig
from qportfolio.solvers.registry import build_solver
from qportfolio.types import QUBO

MAX_STUDY_N = 20


@dataclass
class ExactReference:
    bits: np.ndarray  # (C(n,k), n) all weight-k selections sorted by objective
    objective: np.ndarray

    @property
    def best(self) -> float:
        return float(self.objective[0])


def exact_reference(qubo: QUBO) -> ExactReference:
    n, k = qubo.n_assets, qubo.k
    if n > MAX_STUDY_N:
        raise ValueError(f"exact reference limited to n <= {MAX_STUDY_N}, got {n}")
    combos = np.array(list(itertools.combinations(range(n), k)), dtype=np.int64)
    bits = np.zeros((len(combos), n), dtype=np.int8)
    np.put_along_axis(bits, combos, 1, axis=1)
    obj = qubo.objective(bits.astype(float))
    order = np.argsort(obj, kind="stable")
    return ExactReference(bits[order], obj[order])


def _key(row: np.ndarray) -> bytes:
    return np.asarray(row, dtype=np.int8).tobytes()


def measure_pool(qubo: QUBO, pool, ref: ExactReference, top_q: int = 10) -> dict[str, Any]:
    info = pool.solver_info
    shots = float(info.get("shots") or 0)
    best_key = _key(ref.bits[0])
    counts = {_key(b): int(c) for b, c in zip(pool.bits, pool.counts, strict=True)}
    in_pool = set(counts)
    q = min(top_q, len(ref.bits))
    recall = sum(_key(ref.bits[i]) in in_pool for i in range(q)) / q if q else float("nan")
    best = float(pool.objective[0]) if len(pool) else float("nan")
    return {
        "mass_on_best": counts.get(best_key, 0) / shots if shots else float("nan"),
        "top_q_recall": recall,
        "feasible_fraction": float(info.get("raw_feasible_fraction", 1.0)),
        "best_gap": best - ref.best,
        "found_optimum": bool(len(pool) and _key(pool.bits[0]) == best_key),
        "pool_size": len(pool),
        "start_mass_on_rounded": info.get("start_mass_on_rounded"),
    }


def uniform_level(n: int, k: int) -> float:
    """Probability of the exact best selection under uniform random feasible sampling."""
    return 1.0 / math.comb(n, k)


def sampling_study(
    qubo: QUBO,
    algorithms: list[str],
    cfg: SolverConfig,
    seeds: int = 3,
    top_q: int = 10,
    overrides: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    """Run each registered algorithm on one cluster QUBO for several seeds and score the pools."""
    ref = exact_reference(qubo)
    rows: list[dict[str, Any]] = []
    for algo in algorithms:
        acfg = cfg.model_copy(update=(overrides or {}).get(algo, {}))
        for s in range(seeds):
            solver = build_solver(algo, acfg, s)
            t0 = time.perf_counter()
            try:
                pool = solver.solve(qubo, s)
            finally:
                solver.close()
            row = measure_pool(qubo, pool, ref, top_q)
            row.update({"algorithm": algo, "seed": s, "seconds": time.perf_counter() - t0})
            rows.append(row)
    return rows


def summarise(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Per-algorithm medians (distributions, not champions)."""
    out: dict[str, dict[str, float]] = {}
    for algo in dict.fromkeys(r["algorithm"] for r in rows):
        sub = [r for r in rows if r["algorithm"] == algo]
        agg: dict[str, float] = {"runs": float(len(sub))}
        for key in ("mass_on_best", "top_q_recall", "feasible_fraction", "best_gap", "seconds"):
            vals = np.array([r[key] for r in sub], dtype=float)
            vals = vals[np.isfinite(vals)]
            agg[key] = float(np.median(vals)) if len(vals) else float("nan")
        agg["found_optimum_rate"] = float(np.mean([r["found_optimum"] for r in sub]))
        out[algo] = agg
    return out
