"""Benchmark instance families (spec §13.4) and fixed reference instances.

Families: S (synthetic factor model), C (constrained), H (hard: dense, strongly correlated).
Each :class:`Instance` is a fully specified, seeded problem — μ, Σ and the mandate — so every method
sees identical inputs (fairness rule 1, RESEARCH_RATIONALE §7.6).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from qportfolio.config.loader import config_from_dict, deep_update
from qportfolio.config.schema import Config
from qportfolio.data.universe import universe_from_returns
from qportfolio.types import Universe

FAMILIES = ("S", "C", "H")


@dataclass
class Instance:
    name: str
    family: str
    n: int
    k: int
    seed: int
    cfg: Config

    def universe(self) -> Universe:
        from qportfolio.data import generate_synthetic

        returns, attrs = generate_synthetic(self.cfg.data.synthetic, self.seed)
        return universe_from_returns(returns, attrs, self.cfg)


def make_instance(
    family: str, n: int, seed: int = 0, k: int | None = None, base: dict | None = None
) -> Instance:
    """Build one benchmark instance. ``base`` is deep-merged under the family defaults."""
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {FAMILIES}")
    k = k or max(4, n // 8)
    n_sectors = max(3, min(12, n // 12))
    synth = {
        "n_assets": n,
        "n_sectors": n_sectors,
        "n_factors": 3,
        "n_days": 750,
        "noise_level": 1.0,
    }
    mandate: dict = {"K": k, "lambda_risk": 3.0, "w_max": min(1.0, max(2.5 / k, 0.08))}
    if family == "C":
        mandate["sector_caps"] = {"Technology": 0.25, "Energy": 0.15, "Financials": 0.3}
        mandate["turnover_limit"] = 0.8
    if family == "H":
        synth.update(noise_level=0.5, n_factors=2, n_sectors=max(2, n_sectors // 3))
    raw = {
        "run": {"seed": seed, "name": f"{family}{n}_s{seed}"},
        "data": {"synthetic": synth, "min_history_days": 250},
        "mandate": mandate,
        "decomposition": {"max_cluster_size": 12},
        "solver": {
            "algorithm": "qaoa",
            "p_layers": 2,
            "shots": 2048,
            "max_iterations": 40,
            "top_candidates_per_cluster": 40,
        },
        "recombination": {"beam_width": 150, "top_r": 400},
        "menu": {"size": 10, "epsilon_pct": 3.0},
        "benchmark": {"miqp_time_limit": 8.0},
    }
    cfg = config_from_dict(deep_update(raw, base or {}))
    return Instance(name=f"{family}-N{n}-K{k}-s{seed}", family=family, n=n, k=k, seed=seed, cfg=cfg)


def reference_instances() -> list[Instance]:
    """The fixed, seeded set checked into the repo for regression/comparison across time."""
    return [
        make_instance("S", 40, seed=1, k=8),
        make_instance("S", 80, seed=2, k=12),
        make_instance("C", 80, seed=3, k=12),
        make_instance("H", 60, seed=4, k=10),
    ]


def exact_optimum(universe: Universe, k: int, lam: float, max_n: int = 22) -> float | None:
    """Brute-force unconstrained-selection optimum for tiny universes (ground truth, AC1)."""
    n = universe.n
    if n > max_n:
        return None
    from itertools import combinations

    sig, mu = universe.sigma, universe.mu
    best = np.inf
    for c in combinations(range(n), k):
        idx = np.array(c)
        v = lam / k**2 * sig[np.ix_(idx, idx)].sum() - mu[idx].sum() / k
        best = min(best, v)
    return float(best)
