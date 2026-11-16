"""Shared solver machinery: interface, exact re-scoring, repair, candidate-pool finalisation.

The device (or any heuristic) only *proposes* bitstrings. Every proposal is re-scored here with the
exact classical objective (F5-R7, spec §5.1 principle 2): noise can reduce usefulness, never
correctness.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from qportfolio.types import QUBO, CandidatePool

MAX_REPAIR_ROWS = 4000


class Solver(ABC):
    """``QUBO -> CandidatePool``. Quantum and classical solvers share this interface (F5-R1)."""

    name: str = "solver"

    @abstractmethod
    def solve(self, qubo: QUBO, seed: int) -> CandidatePool:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - default no-op
        return None

    def usage(self) -> dict:
        return {}


def trivial_pool(qubo: QUBO, solver_name: str) -> CandidatePool | None:
    """Pools for clusters whose answer is forced (k=0 or k=n), needing no solver at all."""
    n, k = qubo.n_assets, qubo.k
    if k == 0:
        bits = np.zeros((1, n), dtype=np.int8)
    elif k == n:
        bits = np.ones((1, n), dtype=np.int8)
    else:
        return None
    return CandidatePool(
        cluster_id=qubo.cluster_id,
        k=k,
        bits=bits,
        objective=qubo.objective(bits.astype(float)),
        counts=np.zeros(1, dtype=np.int64),
        solver_info={"solver": solver_name, "trivial": True},
    )


def repair_to_cardinality(qubo: QUBO, bits: np.ndarray) -> np.ndarray:
    """Greedy fix-up to exactly ``qubo.k`` ones using exact marginal objective changes (F5-R8)."""
    Q = qubo.obj_Q
    diag = np.diag(Q)
    x = bits.astype(float).copy()
    h = Q @ x  # (Qx)_i
    k = qubo.k
    cur = int(x.sum())
    while cur > k:  # drop the one whose removal lowers the objective the most
        delta = diag - 2.0 * h  # ΔE(remove i) = Q_ii - 2 (Qx)_i  (x_i = 1)
        delta = np.where(x > 0.5, delta, np.inf)
        i = int(np.argmin(delta))
        x[i] = 0.0
        h -= Q[:, i]
        cur -= 1
    while cur < k:  # add the one whose insertion raises the objective the least
        delta = 2.0 * h + diag  # ΔE(add i) = 2(Qx)_i + Q_ii
        delta = np.where(x < 0.5, delta, np.inf)
        i = int(np.argmin(delta))
        x[i] = 1.0
        h += Q[:, i]
        cur += 1
    return x.astype(np.int8)


def finalize_pool(
    qubo: QUBO,
    bits: np.ndarray,
    counts: np.ndarray,
    top_m: int,
    repair: bool,
    solver_info: dict,
) -> CandidatePool:
    """Decode → (optionally repair) → de-duplicate → exact re-score → keep best ``top_m``."""
    n = qubo.n_assets
    bits = np.asarray(bits, dtype=np.int8)[:, :n]
    counts = np.asarray(counts, dtype=np.int64)
    total_shots = int(counts.sum()) if counts.size else 0
    pop = bits.sum(axis=1)
    feasible = pop == qubo.k
    info = dict(solver_info)
    info["raw_distinct"] = len(bits)
    info["raw_feasible_fraction"] = (
        float(counts[feasible].sum() / total_shots) if total_shots else 0.0
    )

    if repair and (~feasible).any():
        order = np.argsort(-counts)
        fixed_rows: list[np.ndarray] = []
        fixed_counts: list[int] = []
        n_fixed = 0
        for idx in order:
            if feasible[idx]:
                continue
            if n_fixed >= MAX_REPAIR_ROWS:
                break
            fixed_rows.append(repair_to_cardinality(qubo, bits[idx]))
            fixed_counts.append(int(counts[idx]))
            n_fixed += 1
        keep = bits[feasible]
        keep_c = counts[feasible]
        if fixed_rows:
            bits = np.vstack([keep, np.array(fixed_rows, dtype=np.int8)])
            counts = np.concatenate([keep_c, np.array(fixed_counts, dtype=np.int64)])
        else:
            bits, counts = keep, keep_c
        info["repaired_rows"] = n_fixed
    else:
        bits, counts = bits[feasible], counts[feasible]

    if len(bits) == 0:
        return CandidatePool(
            cluster_id=qubo.cluster_id,
            k=qubo.k,
            bits=np.zeros((0, n), dtype=np.int8),
            objective=np.zeros(0),
            counts=np.zeros(0, dtype=np.int64),
            solver_info={**info, "empty": True},
        )

    uniq, inv = np.unique(bits, axis=0, return_inverse=True)
    merged = np.zeros(len(uniq), dtype=np.int64)
    np.add.at(merged, inv.reshape(-1), counts)
    obj = qubo.objective(uniq.astype(float))
    order = np.argsort(obj, kind="stable")[:top_m]
    uniq, obj, merged = uniq[order], obj[order], merged[order]

    if len(obj) >= 4 and merged.sum() > 0 and np.ptp(obj) > 0 and np.ptp(merged) > 0:
        # F5-R7: does the device's own ranking (by frequency) agree with the exact ranking?
        r1 = np.argsort(np.argsort(-merged))
        r2 = np.argsort(np.argsort(obj))
        info["rank_agreement"] = float(np.corrcoef(r1, r2)[0, 1])
    info["pool_size"] = len(obj)
    return CandidatePool(
        cluster_id=qubo.cluster_id,
        k=qubo.k,
        bits=uniq,
        objective=obj,
        counts=merged,
        solver_info=info,
    )
