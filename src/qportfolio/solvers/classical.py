"""Classical solvers sharing the quantum ``Solver`` interface (F5-R1, spec §13.2 baselines).

* :class:`ExactSolver` — exhaustive enumeration of weight-``k`` selections when C(n,k) is small;
  returns the true best pool. Provides ground truth and the "decomposed + classical" baseline.
* :class:`SASolver` — multi-restart simulated annealing with *swap* moves (stays on weight ``k``)
  accelerated with Numba. Produces many distinct near-optimal selections (the classical sampler).
"""

from __future__ import annotations

import itertools
import math

import numpy as np
from numba import njit

from qportfolio.config.schema import SolverConfig
from qportfolio.solvers.base import Solver, finalize_pool, trivial_pool
from qportfolio.types import QUBO, CandidatePool
from qportfolio.utils.rng import make_rng


@njit(cache=True, fastmath=True)
def _sa_swap(
    Q, k, sweeps, restarts, t0, t1, seed, snap_every, out_bits, out_n
):  # pragma: no cover (jit)
    n = Q.shape[0]
    np.random.seed(seed)
    for r in range(restarts):
        x = np.zeros(n)
        perm = np.random.permutation(n)
        for i in range(k):
            x[perm[i]] = 1.0
        h = Q @ x
        e = 0.0
        for i in range(n):
            e += x[i] * h[i]
        steps = sweeps * n
        cnt = 0
        for s in range(steps):
            t = t0 * (t1 / t0) ** (s / max(steps - 1, 1))
            # pick a selected i and an unselected j
            i = np.random.randint(n)
            while x[i] < 0.5:
                i = np.random.randint(n)
            j = np.random.randint(n)
            while x[j] > 0.5:
                j = np.random.randint(n)
            # ΔE for x_i:1→0 and x_j:0→1 :  -2h_i + Q_ii + 2h_j + Q_jj - 2Q_ij  (valid since x_i=1,x_j=0)
            d = -2.0 * h[i] + Q[i, i] + 2.0 * h[j] + Q[j, j] - 2.0 * Q[i, j]
            if d <= 0.0 or np.random.random() < np.exp(-d / t):
                x[i] = 0.0
                x[j] = 1.0
                for a in range(n):
                    h[a] += Q[a, j] - Q[a, i]
                e += d
            # snapshot the chain during the colder half of the schedule -> many distinct good states
            if s >= steps // 2 and (s % snap_every) == 0 and cnt < out_bits.shape[1]:
                out_bits[r, cnt, :] = x
                cnt += 1
        out_n[r] = cnt


@njit(cache=True)
def _warmup() -> None:  # pragma: no cover
    Q = np.eye(4)
    ob = np.zeros((1, 4, 4))
    on = np.zeros(1, dtype=np.int64)
    _sa_swap(Q, 2, 2, 1, 1.0, 0.1, 0, 4, ob, on)


class SASolver(Solver):
    name = "sa"

    def __init__(self, cfg: SolverConfig, seed: int = 0) -> None:
        self.cfg = cfg
        self.seed = seed

    def solve(self, qubo: QUBO, seed: int) -> CandidatePool:
        trivial = trivial_pool(qubo, self.name)
        if trivial is not None:
            return trivial
        n = qubo.n_assets
        Q = np.ascontiguousarray(qubo.obj_Q)
        rng = make_rng(self.seed, "sa", qubo.cluster_id, seed)
        scale = float(np.abs(Q).mean() + 1e-12) * max(2, qubo.k)
        t0, t1 = scale * 2.0, scale * 0.01
        restarts = self.cfg.sa_restarts
        snap_every = max(1, n // 2)
        max_snaps = (self.cfg.sa_sweeps * n // 2) // snap_every + 2
        out_bits = np.zeros((restarts, max_snaps, n))
        out_n = np.zeros(restarts, dtype=np.int64)
        seed_i = int(rng.integers(1, 2**31 - 1))
        _sa_swap(
            Q, qubo.k, self.cfg.sa_sweeps, restarts, t0, t1, seed_i, snap_every, out_bits, out_n
        )
        bits = np.concatenate([out_bits[r, : out_n[r]] for r in range(restarts)]).astype(np.int8)
        info = {
            "solver": "sa",
            "restarts": restarts,
            "sweeps": self.cfg.sa_sweeps,
            "backend": "classical",
            "backend_kind": "classical",
        }
        return finalize_pool(
            qubo,
            bits,
            np.zeros(len(bits), dtype=np.int64),
            self.cfg.top_candidates_per_cluster,
            False,
            info,
        )


class ExactSolver(Solver):
    name = "exact"

    def __init__(self, cfg: SolverConfig, seed: int = 0) -> None:
        self.cfg = cfg
        self.seed = seed
        self._fallback = SASolver(cfg, seed)

    def solve(self, qubo: QUBO, seed: int) -> CandidatePool:
        trivial = trivial_pool(qubo, self.name)
        if trivial is not None:
            return trivial
        n, k = qubo.n_assets, qubo.k
        if math.comb(n, k) > self.cfg.exact_max_combinations:
            pool = self._fallback.solve(qubo, seed)
            pool.solver_info["exact_fallback_to_sa"] = True
            return pool
        combos = np.array(list(itertools.combinations(range(n), k)), dtype=np.int64)
        bits = np.zeros((len(combos), n), dtype=np.int8)
        np.put_along_axis(bits, combos, 1, axis=1)
        info = {
            "solver": "exact",
            "combinations": len(combos),
            "backend": "classical",
            "backend_kind": "classical",
        }
        return finalize_pool(
            qubo,
            bits,
            np.zeros(len(bits), dtype=np.int64),
            self.cfg.top_candidates_per_cluster,
            False,
            info,
        )
