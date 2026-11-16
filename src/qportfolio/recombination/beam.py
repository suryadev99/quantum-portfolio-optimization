"""F6 — recombination (master step) and swap polish.

Selecting one candidate per cluster is a small multiple-choice problem. Every combination is scored
with the **full** covariance (cross-cluster terms included), so decomposition error is repaired
here. A beam search explores combinations; a best-improvement single-swap local search then fixes
residual cut-induced errors. No infeasible portfolio is ever returned (F6-R3, NFR8).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from qportfolio.config.schema import MandateConfig, RecombinationConfig
from qportfolio.recombination.constraints import ConstraintModel
from qportfolio.types import CandidatePool, Cluster, Universe
from qportfolio.utils.linalg import selection_objective_batch
from qportfolio.utils.logging import get_logger

log = get_logger("recombination")

PoolKey = tuple[int, int]  # (cluster_id, k_free)


@dataclass
class Recombined:
    indicators: np.ndarray  # (R, N) int8
    objective: np.ndarray  # (R,) exact full-universe objective, ascending
    stats: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return int(self.indicators.shape[0])

    def selections(self) -> list[np.ndarray]:
        return [np.flatnonzero(r) for r in self.indicators]


# ----------------------------------------------------------------------------- beam search


def _beam_for_allocation(
    universe: Universe,
    clusters: list[Cluster],
    alloc: np.ndarray,
    pools: dict[PoolKey, CandidatePool],
    cm: ConstraintModel,
    mandate: MandateConfig,
    beam_width: int,
) -> np.ndarray | None:
    """Beam search for one allocation; returns final indicator matrix (B, N) or ``None``."""
    n = universe.n
    K, lam = mandate.K, mandate.lambda_risk
    sigma, mu = universe.sigma, universe.mu
    x0 = np.zeros((1, n))
    if len(cm.forced_in):
        x0[0, cm.forced_in] = 1.0
    obj = selection_objective_batch(sigma, mu, x0, lam, K)
    X = x0

    # larger / more demanding clusters first: they carry the most risk-interaction
    order = sorted(range(len(clusters)), key=lambda i: (-int(alloc[i]), clusters[i].id))
    picked = len(cm.forced_in)
    scale = lam / K**2
    for ci in order:
        cl = clusters[ci]
        k_free = int(alloc[ci]) - len(cl.forced_in)
        pool = pools.get((cl.id, k_free))
        if pool is None or len(pool) == 0:
            return None
        f = np.asarray(cl.asset_idx, dtype=int)
        bits = pool.bits.astype(float)  # (M, n_c)
        s_ff = sigma[np.ix_(f, f)]
        self_q = np.einsum("mi,ij,mj->m", bits, s_ff, bits, optimize=True)
        mu_c = bits @ mu[f]
        xs = X @ sigma[:, f]  # (B, n_c)
        cross = xs @ bits.T  # (B, M)
        total = obj[:, None] + scale * (self_q[None, :] + 2.0 * cross) - mu_c[None, :] / K
        flat = total.ravel()
        m = bits.shape[0]
        picked += k_free
        remaining = K - picked

        take = min(len(flat), 3 * beam_width)
        top = np.argpartition(flat, take - 1)[:take] if take < len(flat) else np.arange(len(flat))
        top = top[np.argsort(flat[top], kind="stable")]
        bi, mi = np.divmod(top, m)
        Xn = X[bi].copy()
        if bits.shape[1]:
            Xn[:, f] = bits[mi]
        # optimistic-capacity prune: even adding `remaining` assets at full w_max cannot reach 100%
        cap_ok = cm.capacity(Xn) + remaining * cm.w_max >= 1.0 - 1e-9
        keep = np.flatnonzero(cap_ok)[:beam_width]
        if len(keep) == 0:
            return None
        X = Xn[keep]
        obj = flat[top][keep]
    return X


def recombine(
    universe: Universe,
    clusters: list[Cluster],
    allocations: list[np.ndarray],
    pools: dict[PoolKey, CandidatePool],
    mandate: MandateConfig,
    cm: ConstraintModel,
    cfg: RecombinationConfig,
) -> Recombined:
    """Run beam search over every allocation, pool, exact-rescore, gate, polish, return top-R."""
    n = universe.n
    K, lam = mandate.K, mandate.lambda_risk
    rows: list[np.ndarray] = []
    n_raw = 0
    used = 0
    for alloc in allocations:
        X = _beam_for_allocation(universe, clusters, alloc, pools, cm, mandate, cfg.beam_width)
        if X is None:
            continue
        used += 1
        n_raw += len(X)
        rows.append(X.astype(np.int8))
    stats: dict = {"allocations_used": used, "allocations_total": len(allocations), "raw": n_raw}
    if not rows:
        return Recombined(np.zeros((0, n), dtype=np.int8), np.zeros(0), {**stats, "feasible": 0})

    X = np.unique(np.vstack(rows), axis=0)
    obj = selection_objective_batch(universe.sigma, universe.mu, X.astype(float), lam, K)
    order = np.argsort(obj, kind="stable")
    X, obj = X[order], obj[order]
    ok = cm.structural_ok(X)
    X, obj = X[ok], obj[ok]
    stats["unique"] = len(order)
    stats["structurally_feasible"] = len(X)

    # turnover LP gate (exact) — only on the leading candidates to bound cost
    if cm.turnover_active and len(X):
        lead = min(len(X), 4 * cfg.top_r)
        sels = [np.flatnonzero(r) for r in X[:lead]]
        good = cm.gate(X[:lead], sels)
        X, obj = X[:lead][good], obj[:lead][good]
    X, obj = X[: cfg.top_r], obj[: cfg.top_r]
    stats["feasible"] = len(X)
    stats["best_before_polish"] = float(obj[0]) if len(obj) else None

    if cfg.local_polish and cfg.polish_top > 0 and len(X):
        polished = []
        for r in range(min(cfg.polish_top, len(X))):
            sel, o = swap_polish(universe, cm, np.flatnonzero(X[r]), mandate, cfg.max_polish_iters)
            row = np.zeros(n, dtype=np.int8)
            row[sel] = 1
            polished.append((row, o))
        add = np.array([p[0] for p in polished])
        add_o = np.array([p[1] for p in polished])
        X = np.vstack([X, add])
        obj = np.concatenate([obj, add_o])
        X, uidx = np.unique(X, axis=0, return_index=True)
        obj = obj[uidx]
        order = np.argsort(obj, kind="stable")
        X, obj = X[order][: cfg.top_r], obj[order][: cfg.top_r]
        stats["best_after_polish"] = float(obj[0])

    if cfg.expand_neighbourhood and len(X):
        band = abs(float(obj[0])) * cfg.expansion_pct / 100.0
        extra_rows, extra_obj = expand_neighbourhood(
            universe, cm, mandate, X, obj, band, cfg.expansion_seeds, cfg.expansion_max
        )
        stats["expansion_added"] = len(extra_rows)
        if len(extra_rows):
            X = np.vstack([X, extra_rows])
            obj = np.concatenate([obj, extra_obj])
            X, uidx = np.unique(X, axis=0, return_index=True)
            obj = obj[uidx]
            order = np.argsort(obj, kind="stable")
            X, obj = X[order], obj[order]
    return Recombined(X, obj, stats)


# ----------------------------------------------------------------------------- pool expansion


def expand_neighbourhood(
    universe: Universe,
    cm: ConstraintModel,
    mandate: MandateConfig,
    X: np.ndarray,
    obj: np.ndarray,
    band: float,
    n_seeds: int,
    max_total: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Feasible single-swap neighbours of the best ``n_seeds`` portfolios within ``band`` of best."""
    n = universe.n
    K, lam = mandate.K, mandate.lambda_risk
    sigma, mu = universe.sigma, universe.mu
    scale = lam / K**2
    diag = np.diag(sigma)
    limit = float(obj[0]) + band
    forced = np.zeros(n, dtype=bool)
    if len(cm.forced_in):
        forced[cm.forced_in] = True
    allowed_out = cm.eligible & ~forced
    rows: list[np.ndarray] = []
    objs: list[float] = []
    for r in range(min(n_seeds, len(X))):
        x = X[r].astype(float)
        h = sigma @ x
        in_idx = np.flatnonzero((x > 0.5) & ~forced)
        out_idx = np.flatnonzero((x < 0.5) & allowed_out)
        if len(in_idx) == 0 or len(out_idx) == 0:
            continue
        d = (
            scale
            * (
                -2.0 * h[in_idx][:, None]
                + diag[in_idx][:, None]
                + 2.0 * h[out_idx][None, :]
                + diag[out_idx][None, :]
                - 2.0 * sigma[np.ix_(in_idx, out_idx)]
            )
            - (mu[out_idx][None, :] - mu[in_idx][:, None]) / K
        )
        new_obj = float(obj[r]) + d
        ii, jj = np.nonzero(new_obj <= limit)
        if len(ii) == 0:
            continue
        order = np.argsort(new_obj[ii, jj])[:max_total]
        cand = np.tile(X[r], (len(order), 1))
        cand[np.arange(len(order)), in_idx[ii[order]]] = 0
        cand[np.arange(len(order)), out_idx[jj[order]]] = 1
        ok = cm.structural_ok(cand)
        if cm.turnover_active:
            for c in np.flatnonzero(ok):
                mt = cm.min_turnover(np.flatnonzero(cand[c]))
                ok[c] = mt is not None and mt <= cm.turnover_limit + 1e-9
        rows.append(cand[ok])
        objs.append(new_obj[ii, jj][order][ok])
    if not rows:
        return np.zeros((0, n), dtype=np.int8), np.zeros(0)
    R = np.vstack(rows)
    obj = np.concatenate(objs)
    R, uidx = np.unique(R, axis=0, return_index=True)
    obj = obj[uidx]
    keep = np.argsort(obj, kind="stable")[:max_total]
    return R[keep].astype(np.int8), obj[keep]


# ----------------------------------------------------------------------------- swap polish


def swap_polish(
    universe: Universe,
    cm: ConstraintModel,
    sel: np.ndarray,
    mandate: MandateConfig,
    max_iters: int = 50,
    max_tests: int = 25,
) -> tuple[np.ndarray, float]:
    """Best-improvement single-swap local search that preserves every hard constraint."""
    n = universe.n
    K, lam = mandate.K, mandate.lambda_risk
    sigma, mu = universe.sigma, universe.mu
    scale = lam / K**2
    x = np.zeros(n)
    x[np.asarray(sel, dtype=int)] = 1.0
    forced = np.zeros(n, dtype=bool)
    if len(cm.forced_in):
        forced[cm.forced_in] = True
    movable_in = ~forced
    allowed_out = cm.eligible & ~forced
    diag = np.diag(sigma)
    h = sigma @ x
    obj = float(selection_objective_batch(sigma, mu, x[None, :], lam, K)[0])

    for _ in range(max_iters):
        in_idx = np.flatnonzero((x > 0.5) & movable_in)
        out_idx = np.flatnonzero((x < 0.5) & allowed_out)
        if len(in_idx) == 0 or len(out_idx) == 0:
            break
        d = (
            scale
            * (
                -2.0 * h[in_idx][:, None]
                + diag[in_idx][:, None]
                + 2.0 * h[out_idx][None, :]
                + diag[out_idx][None, :]
                - 2.0 * sigma[np.ix_(in_idx, out_idx)]
            )
            - (mu[out_idx][None, :] - mu[in_idx][:, None]) / K
        )
        flat = d.ravel()
        cand = np.flatnonzero(flat < -1e-12)
        if len(cand) == 0:
            break
        cand = cand[np.argsort(flat[cand])][:max_tests]
        accepted = False
        for c in cand:
            i, j = in_idx[c // len(out_idx)], out_idx[c % len(out_idx)]
            xn = x.copy()
            xn[i], xn[j] = 0.0, 1.0
            if not cm.structural_ok(xn[None, :])[0]:
                continue
            if cm.turnover_active:
                mt = cm.min_turnover(np.flatnonzero(xn))
                if mt is None or mt > cm.turnover_limit + 1e-9:
                    continue
            x = xn
            h = h + sigma[:, j] - sigma[:, i]
            obj += float(flat[c])
            accepted = True
            break
        if not accepted:
            break
    obj = float(selection_objective_batch(sigma, mu, x[None, :], lam, K)[0])
    return np.flatnonzero(x), obj
