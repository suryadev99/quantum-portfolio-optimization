"""Core data structures shared by all pipeline stages (spec §11.1)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class Universe:
    """Validated investable universe with estimated inputs (F1/F2 output)."""

    ids: list[str]
    mu: np.ndarray
    sigma: np.ndarray
    sectors: np.ndarray
    liquidity: np.ndarray
    esg: np.ndarray
    current_weight: np.ndarray
    mu_se: np.ndarray | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.ids)

    def index_of(self, names: list[str]) -> np.ndarray:
        lookup = {a: i for i, a in enumerate(self.ids)}
        return np.array([lookup[a] for a in names], dtype=int)

    def sector_codes(self) -> tuple[np.ndarray, list[str]]:
        names = sorted(set(self.sectors.tolist()))
        lookup = {s: i for i, s in enumerate(names)}
        return np.array([lookup[s] for s in self.sectors], dtype=int), names

    def data_hash(self) -> str:
        h = hashlib.sha256()
        for arr in (self.mu, self.sigma, self.current_weight):
            h.update(np.ascontiguousarray(arr, dtype=np.float64).tobytes())
        h.update("|".join(self.ids).encode())
        h.update("|".join(self.sectors.tolist()).encode())
        return h.hexdigest()


@dataclass
class Cluster:
    id: int
    asset_idx: np.ndarray  # global indices of *selectable* assets (forced-out removed)
    forced_in: np.ndarray  # global indices forced into the portfolio (subset of cluster)
    quality: dict[str, float] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return int(len(self.asset_idx) + len(self.forced_in))


@dataclass
class KAllocation:
    base: np.ndarray  # picks per cluster (includes forced-in)
    alternatives: list[np.ndarray] = field(default_factory=list)

    def all(self) -> list[np.ndarray]:
        return [self.base, *self.alternatives]


@dataclass
class QUBO:
    """Binary quadratic problem for one cluster / one pick-count (spec §10).

    Energy of ``x`` (free variables first, then slack bits) is ``x'Qx + offset`` with Q symmetric.
    ``obj_Q``/``obj_offset`` hold the *unpenalised* objective over the free variables and are used
    for exact classical re-scoring (F5-R7).
    """

    Q: np.ndarray
    offset: float
    asset_idx: np.ndarray
    k: int
    penalty: float
    obj_Q: np.ndarray
    obj_offset: float
    cluster_id: int = -1
    fixed_in: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))
    num_slack: int = 0
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def n_assets(self) -> int:
        return len(self.asset_idx)

    @property
    def num_qubits(self) -> int:
        return self.n_assets + self.num_slack

    def energy(self, x: np.ndarray) -> np.ndarray:
        """Penalised energy for (B, num_qubits) or (num_qubits,) 0/1 arrays."""
        xx = np.atleast_2d(np.asarray(x, dtype=float))
        e = np.einsum("bi,ij,bj->b", xx, self.Q, xx, optimize=True) + self.offset
        return e if np.ndim(x) > 1 else e[0]

    def objective(self, x: np.ndarray) -> np.ndarray:
        """Unpenalised local objective for (B, n_assets) 0/1 arrays."""
        xx = np.atleast_2d(np.asarray(x, dtype=float))[:, : self.n_assets]
        e = np.einsum("bi,ij,bj->b", xx, self.obj_Q, xx, optimize=True) + self.obj_offset
        return e if np.ndim(x) > 1 else e[0]


@dataclass
class CandidatePool:
    """Distinct, exactly re-scored candidate selections for one QUBO (F5)."""

    cluster_id: int
    k: int
    bits: np.ndarray  # (M, n_assets) int8
    objective: np.ndarray  # (M,) exact local objective, ascending
    counts: np.ndarray  # (M,) device frequency (0 for classical solvers)
    solver_info: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return int(self.bits.shape[0])


@dataclass
class Portfolio:
    selected_idx: np.ndarray
    selected_ids: list[str]
    objective: float
    weights: dict[str, float] | None = None
    stats: dict[str, float] = field(default_factory=dict)
    constraint_slack: dict[str, float] = field(default_factory=dict)
    rank_scores: dict[str, float] = field(default_factory=dict)
    explanation: dict[str, Any] = field(default_factory=dict)
    rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "selected_ids": self.selected_ids,
            "weights": self.weights,
            "objective": self.objective,
            "stats": self.stats,
            "constraint_slack": self.constraint_slack,
            "rank_scores": self.rank_scores,
            "explanation": self.explanation,
        }


@dataclass
class PortfolioMenu:
    portfolios: list[Portfolio]
    diversity_stats: dict[str, float] = field(default_factory=dict)
    epsilon: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "epsilon": self.epsilon,
            "diversity_stats": self.diversity_stats,
            "notes": self.notes,
            "portfolios": [p.to_dict() for p in self.portfolios],
        }
