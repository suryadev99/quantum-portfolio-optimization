"""Hard-constraint logic shared by recombination, menu selection and baselines (F6-R3, NFR8).

A *selection* (set of K assets) is feasible for the mandate iff weights exist with
``0 ≤ w_i ≤ w_max``, ``Σ w = 1``, sector sums ≤ caps and (optionally) turnover ≤ limit. Sector
feasibility reduces to an exact capacity test:  ``Σ_s min(cap_s, n_s · w_max) ≥ 1``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog

from qportfolio.config.schema import MandateConfig
from qportfolio.types import Universe

TOL = 1e-9


@dataclass
class ConstraintModel:
    """Vectorised constraint checker built once per run."""

    k: int
    w_max: float
    sector_code: np.ndarray  # (N,) int
    sector_names: list[str]
    caps: np.ndarray  # (S,) cap per sector (1.0 when uncapped)
    eligible: np.ndarray  # (N,) bool
    forced_in: np.ndarray  # (F,) global idx
    turnover_limit: float | None
    current_weight: np.ndarray
    has_holdings: bool

    @classmethod
    def build(
        cls, universe: Universe, mandate: MandateConfig, eligible: np.ndarray
    ) -> ConstraintModel:
        codes, names = universe.sector_codes()
        caps = np.array([mandate.sector_caps.get(s, 1.0) for s in names], dtype=float)
        cw = np.asarray(universe.current_weight, dtype=float)
        return cls(
            k=mandate.K,
            w_max=mandate.w_max,
            sector_code=codes,
            sector_names=names,
            caps=caps,
            eligible=np.asarray(eligible, dtype=bool),
            forced_in=universe.index_of(mandate.forced_in)
            if mandate.forced_in
            else np.zeros(0, int),
            turnover_limit=mandate.turnover_limit,
            current_weight=cw,
            has_holdings=bool(cw.sum() > 1e-12),
        )

    @property
    def n_sectors(self) -> int:
        return len(self.sector_names)

    # ------------------------------------------------------------------ capacity
    def sector_counts(self, indicators: np.ndarray) -> np.ndarray:
        """(B, S) counts of selected assets per sector for a (B, N) 0/1 matrix."""
        x = np.atleast_2d(indicators)
        onehot = np.zeros((x.shape[1], self.n_sectors))
        onehot[np.arange(x.shape[1]), self.sector_code] = 1.0
        return x @ onehot

    def capacity(self, indicators: np.ndarray) -> np.ndarray:
        counts = self.sector_counts(indicators)
        return np.minimum(self.caps[None, :], counts * self.w_max).sum(axis=1)

    def capacity_deficit(self, indicators: np.ndarray) -> np.ndarray:
        return np.maximum(0.0, 1.0 - self.capacity(indicators))

    # ------------------------------------------------------------------ batch gate
    def structural_ok(self, indicators: np.ndarray) -> np.ndarray:
        """Cardinality, eligibility, forced-in and sector capacity (no LP needed)."""
        x = np.atleast_2d(indicators)
        ok = x.sum(axis=1) == self.k
        ok &= (x[:, ~self.eligible].sum(axis=1) == 0) if (~self.eligible).any() else True
        if len(self.forced_in):
            ok &= x[:, self.forced_in].all(axis=1)
        ok &= self.capacity(x) >= 1.0 - 1e-9
        return np.asarray(ok, dtype=bool)

    # ------------------------------------------------------------------ turnover
    @property
    def turnover_active(self) -> bool:
        return self.turnover_limit is not None and self.has_holdings

    def min_turnover(self, sel: np.ndarray, sigma_free: bool = True) -> float | None:
        """Smallest achievable turnover over feasible weights for selection ``sel`` (LP).

        Turnover = ½(Σ_{i∈S}|w_i − c_i| + Σ_{i∉S} c_i). Returns ``None`` if no feasible weights.
        """
        sel = np.asarray(sel, dtype=int)
        k = len(sel)
        c = self.current_weight[sel]
        out_of_set = float(self.current_weight.sum() - c.sum())
        # variables [w (k), t (k)], minimise Σ t ; t ≥ |w − c|
        cost = np.concatenate([np.zeros(k), np.ones(k)])
        a_ub = []
        b_ub = []
        eye = np.eye(k)
        a_ub.append(np.hstack([eye, -eye]))  # w − t ≤ c
        b_ub.append(c)
        a_ub.append(np.hstack([-eye, -eye]))  # −w − t ≤ −c
        b_ub.append(-c)
        codes = self.sector_code[sel]
        for s in np.unique(codes):
            if self.caps[s] < 1.0 - 1e-12:
                row = np.zeros(2 * k)
                row[:k] = (codes == s).astype(float)
                a_ub.append(row[None, :])
                b_ub.append(np.array([self.caps[s]]))
        a_eq = np.concatenate([np.ones(k), np.zeros(k)])[None, :]
        res = linprog(
            cost,
            A_ub=np.vstack(a_ub),
            b_ub=np.concatenate(b_ub),
            A_eq=a_eq,
            b_eq=[1.0],
            bounds=[(0.0, self.w_max)] * k + [(0.0, None)] * k,
            method="highs",
        )
        if res.status != 0:
            return None
        return 0.5 * (float(res.fun) + out_of_set)

    def gate(
        self, indicators: np.ndarray, selections: list[np.ndarray] | None = None
    ) -> np.ndarray:
        """Full hard-constraint gate: structural checks, then the turnover LP when active."""
        ok = self.structural_ok(indicators)
        if self.turnover_active:
            x = np.atleast_2d(indicators)
            for r in np.flatnonzero(ok):
                sel = selections[r] if selections is not None else np.flatnonzero(x[r] > 0.5)
                mt = self.min_turnover(sel)
                ok[r] = mt is not None and mt <= self.turnover_limit + 1e-9
        return ok

    def turnover_of(self, weights_full: np.ndarray) -> float:
        return 0.5 * float(np.abs(weights_full - self.current_weight).sum())
