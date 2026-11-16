"""No-quantum control samplers (spec C8-R2, C5-R5).

These share the quantum ``Solver`` interface and the same exact re-scoring, repair and de-duplication,
so any measured benefit of a quantum sampler is relative to a fair, equal-budget classical control:

* :class:`RandomFeasibleSolver` draws ``shots`` uniformly random weight-``k`` selections. This is the
  control the IBM multi-objective tutorial uses; at 40 assets it found QAOA ≈ uniform random sampling.
* :class:`RoundRelaxationSolver` solves the same convex relaxation as warm-start QAOA, rounds it, and
  draws ``shots`` Bernoulli samples from the *same biased start distribution* warm start would prepare,
  then repairs them. It isolates "the classical relaxation did the work" from "the quantum evolution
  added something".
"""

from __future__ import annotations

import numpy as np

from qportfolio.config.schema import SolverConfig
from qportfolio.solvers.base import Solver, finalize_pool, trivial_pool
from qportfolio.solvers.relaxation import clip_start, solve_relaxation, start_mass_on_rounded
from qportfolio.types import QUBO, CandidatePool
from qportfolio.utils.rng import make_rng


def _dedupe(bits: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    uniq, counts = np.unique(bits, axis=0, return_counts=True)
    return uniq.astype(np.int8), counts.astype(np.int64)


class RandomFeasibleSolver(Solver):
    name = "random_feasible"

    def __init__(self, cfg: SolverConfig, seed: int = 0) -> None:
        self.cfg = cfg
        self.seed = seed

    def solve(self, qubo: QUBO, seed: int) -> CandidatePool:
        trivial = trivial_pool(qubo, self.name)
        if trivial is not None:
            return trivial
        n, k, s = qubo.n_assets, qubo.k, self.cfg.shots
        rng = make_rng(self.seed, "random_feasible", qubo.cluster_id, seed)
        order = np.argsort(rng.random((s, n)), axis=1)[:, :k]
        bits = np.zeros((s, n), dtype=np.int8)
        np.put_along_axis(bits, order, 1, axis=1)
        uniq, counts = _dedupe(bits)
        info = {
            "solver": self.name,
            "shots": s,
            "backend": "classical",
            "backend_kind": "classical",
            "control": True,
        }
        return finalize_pool(qubo, uniq, counts, self.cfg.top_candidates_per_cluster, False, info)


class RoundRelaxationSolver(Solver):
    name = "round_relaxation"

    def __init__(self, cfg: SolverConfig, seed: int = 0) -> None:
        self.cfg = cfg
        self.seed = seed

    def solve(self, qubo: QUBO, seed: int) -> CandidatePool:
        trivial = trivial_pool(qubo, self.name)
        if trivial is not None:
            return trivial
        n, k, s = qubo.n_assets, qubo.k, self.cfg.shots
        c, rinfo = solve_relaxation(qubo)
        start = clip_start(c, self.cfg.warm_epsilon)
        rng = make_rng(self.seed, "round_relaxation", qubo.cluster_id, seed)
        draws = (rng.random((s, n)) < start[None, :]).astype(np.int8)
        rounded = np.zeros((1, n), dtype=np.int8)
        rounded[0, np.argsort(-c, kind="stable")[:k]] = 1  # deterministic round-to-top-k
        uniq, counts = _dedupe(np.vstack([draws, rounded]))
        info = {
            "solver": self.name,
            "shots": s,
            "warm_epsilon": self.cfg.warm_epsilon,
            "start_mass_on_rounded": start_mass_on_rounded(start),
            "backend": "classical",
            "backend_kind": "classical",
            "control": True,
            **rinfo,
        }
        return finalize_pool(qubo, uniq, counts, self.cfg.top_candidates_per_cluster, True, info)
