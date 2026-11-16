"""F4 — QUBO construction, exact penalty calibration, Ising conversion, brute force.

Selection objective for a cluster with free binaries ``x`` and forced-in set ``F`` (spec §10):

    f(x) = lam/K² (x'Σ_cc x + 2 x'Σ_cF 1) − (1/K) μ_c'x          (+ constant)
    E(x) = f(x) + P (Σx − K_free)²                                   (+ slack-bit penalties)

Cross-cluster covariance is intentionally absent here; F6 restores it exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from qiskit.quantum_info import SparsePauliOp

from qportfolio.config.schema import MandateConfig, QuboConfig
from qportfolio.types import QUBO, Cluster, Universe
from qportfolio.utils.errors import ConfigError

EXACT_CALIBRATION_MAX_N = 18
BRUTE_FORCE_MAX_N = 24


# ----------------------------------------------------------------------------- helpers


def _add_squared_linear(
    Q: np.ndarray, idx: np.ndarray, coef: np.ndarray, b: float, p: float
) -> float:
    """Add ``p (Σ coef_i v_i − b)²`` over binaries ``v`` to Q (symmetric); return the offset."""
    outer = p * np.outer(coef, coef)
    diag = np.diag(outer).copy()
    outer[np.diag_indices_from(outer)] = 0.0
    Q[np.ix_(idx, idx)] += outer
    Q[idx, idx] += diag - 2.0 * p * b * coef  # v² = v turns squares/linear terms into diagonal
    return p * b * b


def slack_coefficients(b: int) -> np.ndarray:
    """Binary slack coefficients covering exactly the integer range [0, b]."""
    if b < 1:
        return np.zeros(0)
    t = math.floor(math.log2(b)) + 1
    coefs = [2**i for i in range(t - 1)]
    coefs.append(b - (2 ** (t - 1) - 1))
    return np.array(coefs, dtype=float)


def all_states(n: int, start: int = 0, stop: int | None = None) -> np.ndarray:
    """Rows of the integer range [start, stop) as little-endian bit matrices (asset i = bit i)."""
    stop = (1 << n) if stop is None else stop
    ints = np.arange(start, stop, dtype=np.int64)
    return ((ints[:, None] >> np.arange(n)[None, :]) & 1).astype(np.int8)


def _chunked_energies(Q: np.ndarray, offset: float, n: int, chunk: int = 1 << 16):
    for s in range(0, 1 << n, chunk):
        bits = all_states(n, s, min(s + chunk, 1 << n))
        xf = bits.astype(float)
        e = np.einsum("bi,ij,bj->b", xf, Q, xf, optimize=True) + offset
        yield bits, e


# ----------------------------------------------------------------------------- calibration


def objective_min_by_cardinality(obj_Q: np.ndarray, obj_offset: float) -> np.ndarray:
    """Exact ``f*_m`` (best unpenalised objective with exactly m ones), m = 0..n. n ≤ 18."""
    n = obj_Q.shape[0]
    best = np.full(n + 1, np.inf)
    for bits, e in _chunked_energies(obj_Q, obj_offset, n):
        pop = bits.sum(axis=1)
        for m in np.unique(pop):
            v = e[pop == m].min()
            if v < best[m]:
                best[m] = v
    return best


def penalty_upper_bound(obj_Q: np.ndarray) -> float:
    """Rigorous sufficient penalty: any infeasible state has a strictly better neighbour."""
    off = np.abs(obj_Q - np.diag(np.diag(obj_Q))).sum(axis=1)
    d = np.abs(np.diag(obj_Q)) + 2.0 * off
    return float(d.max())


def calibrate_penalty(
    obj_Q: np.ndarray, obj_offset: float, k_free: int, margin: float = 1.15
) -> tuple[float, str]:
    """Smallest cardinality penalty guaranteeing the QUBO optimum is feasible (F4-R3).

    Exact for ``n ≤ 18`` via ``P ≥ max_{d≠0} (f*_K − f*_{K+d}) / d²``; otherwise the rigorous
    (looser) per-flip bound. Returns ``(P, method)``.
    """
    n = obj_Q.shape[0]
    ub = penalty_upper_bound(obj_Q)
    if n <= EXACT_CALIBRATION_MAX_N:
        f = objective_min_by_cardinality(obj_Q, obj_offset)
        need = 0.0
        for m in range(n + 1):
            d = m - k_free
            if d != 0 and np.isfinite(f[m]):
                need = max(need, (f[k_free] - f[m]) / (d * d))
        return float(min(max(need, 0.0) * margin + 1e-9 * (1 + ub), ub * 1.0 + 1e-12)), "exact"
    return float(ub * 1.0 + 1e-12), "upper_bound"


# ----------------------------------------------------------------------------- builder


def build_qubo(
    universe: Universe,
    cluster: Cluster,
    k_c: int,
    mandate: MandateConfig,
    qcfg: QuboConfig,
    lam_scale: float = 1.0,
) -> QUBO:
    """Build the cluster QUBO for ``k_c`` total picks (forced-in assets count toward ``k_c``).

    ``lam_scale`` multiplies the risk aversion inside the cluster problem only (risk rebalancing,
    spec C3-R4). Final scoring in recombination always uses the mandate's own ``lambda_risk``.
    """
    free = np.asarray(cluster.asset_idx, dtype=int)
    forced = np.asarray(cluster.forced_in, dtype=int)
    n = len(free)
    k_free = int(k_c) - len(forced)
    if k_free < 0 or k_free > n:
        raise ConfigError(
            f"cluster {cluster.id}: k_c={k_c} impossible (free={n}, forced={len(forced)})"
        )

    K, lam = mandate.K, mandate.lambda_risk * lam_scale
    sig = universe.sigma
    mu = universe.mu
    quad = lam / K**2 * sig[np.ix_(free, free)]
    lin = -mu[free] / K
    if len(forced):
        lin = lin + 2.0 * lam / K**2 * sig[np.ix_(free, forced)].sum(axis=1)
    obj_Q = quad.copy()
    obj_Q[np.diag_indices(n)] += lin
    obj_offset = 0.0  # constants that do not depend on x are irrelevant here

    # quad/lin are kept so the convex relaxation (warm start, round_relaxation) sees the PSD part.
    info: dict = {
        "k_free": k_free,
        "n_forced": len(forced),
        "obj_quad": quad,
        "obj_lin": lin,
        "lam_scale": float(lam_scale),
    }
    if isinstance(qcfg.penalty_weight, str):  # "auto"
        p, how = calibrate_penalty(obj_Q, obj_offset, k_free)
        p *= qcfg.penalty_scale
        info["penalty_method"] = how
    else:
        p, info["penalty_method"] = float(qcfg.penalty_weight), "user"

    num_slack = 0
    slack_blocks: list[tuple[np.ndarray, np.ndarray, int]] = []
    if qcfg.inequality_handling == "slack" and n:
        K_ = mandate.K
        sectors = universe.sectors[free]
        for sec, cap in mandate.sector_caps.items():
            b = math.floor(cap * K_ + 1e-9)  # equal-weight count cap (documented approximation)
            members = np.flatnonzero(sectors == sec)
            b_eff = b - int(np.sum(universe.sectors[forced] == sec)) if len(forced) else b
            if len(members) > b_eff >= 0 and b_eff < k_free:
                coefs = slack_coefficients(b_eff)
                slack_blocks.append((members, coefs, b_eff))
                num_slack += len(coefs)
        info["slack_constraints"] = len(slack_blocks)

    total = n + num_slack
    Q = np.zeros((total, total))
    Q[:n, :n] = obj_Q
    offset = obj_offset
    offset += _add_squared_linear(Q, np.arange(n), np.ones(n), float(k_free), p)
    pos = n
    slack_pen = p
    for members, coefs, b_eff in slack_blocks:
        ids = np.concatenate([members, np.arange(pos, pos + len(coefs))])
        a = np.concatenate([np.ones(len(members)), coefs])
        offset += _add_squared_linear(Q, ids, a, float(b_eff), slack_pen)
        pos += len(coefs)

    return QUBO(
        Q=Q,
        offset=float(offset),
        asset_idx=free,
        k=k_free,
        penalty=float(p),
        obj_Q=obj_Q,
        obj_offset=obj_offset,
        cluster_id=cluster.id,
        fixed_in=forced,
        num_slack=num_slack,
        info=info,
    )


# ----------------------------------------------------------------------------- Ising / export


def qubo_to_ising(Q: np.ndarray, offset: float = 0.0) -> tuple[SparsePauliOp, float]:
    """Convert ``x'Qx + offset`` (x∈{0,1}) to an Ising Hamiltonian with ``x = (1 − Z)/2``.

    Returns ``(H, const)`` with ``E(x) = <x|H|x> + const``. Qubit ``i`` ↔ variable ``i``.
    """
    n = Q.shape[0]
    Qs = (Q + Q.T) / 2.0
    diag = np.diag(Qs)
    off = Qs - np.diag(diag)
    h = -(diag + off.sum(axis=1)) / 2.0
    terms: list[tuple[str, list[int], float]] = []
    for i in range(n):
        if abs(h[i]) > 1e-15:
            terms.append(("Z", [i], float(h[i])))
    iu, ju = np.triu_indices(n, k=1)
    jv = off[iu, ju]
    # x'Qx = Σ Q_ii x_i + Σ_{i<j} 2 Q_ij x_i x_j and x_i x_j = (1 - Z_i - Z_j + Z_i Z_j)/4,
    # so each pair contributes (Q_ij/2)·Z_i Z_j plus -Q_ij/2 on each of Z_i, Z_j (in ``h``).
    for i, j, v in zip(iu, ju, jv, strict=True):
        if abs(v) > 1e-15:
            terms.append(("ZZ", [int(i), int(j)], float(v) / 2.0))
    const = float(offset + diag.sum() / 2.0 + np.triu(off, 1).sum() / 2.0)
    if not terms:
        return SparsePauliOp.from_list([("I" * n, 0.0)]), const
    return SparsePauliOp.from_sparse_list(terms, num_qubits=n).simplify(), const


def ising_from_qubo(qubo: QUBO, use_objective_only: bool = False) -> tuple[SparsePauliOp, float]:
    if use_objective_only:
        return qubo_to_ising(qubo.obj_Q, qubo.obj_offset)
    return qubo_to_ising(qubo.Q, qubo.offset)


def to_quadratic_program(qubo: QUBO):
    """Export as a Qiskit Optimization ``QuadraticProgram`` (F4-R1)."""
    from qiskit_optimization import QuadraticProgram

    n = qubo.Q.shape[0]
    qp = QuadraticProgram(name=f"cluster_{qubo.cluster_id}")
    for i in range(n):
        qp.binary_var(f"x{i}")
    linear = {f"x{i}": float(qubo.Q[i, i]) for i in range(n)}
    quad = {
        (f"x{i}", f"x{j}"): float(2.0 * qubo.Q[i, j])
        for i in range(n)
        for j in range(i + 1, n)
        if qubo.Q[i, j] != 0.0
    }
    qp.minimize(constant=qubo.offset, linear=linear, quadratic=quad)
    return qp


def export_qubo(qubo: QUBO) -> dict:
    """Solver-neutral dictionary (matrix + offset) for classical baselines (F4-R7)."""
    return {
        "Q": qubo.Q,
        "offset": qubo.offset,
        "asset_idx": qubo.asset_idx,
        "k": qubo.k,
        "penalty": qubo.penalty,
        "num_slack": qubo.num_slack,
    }


# ----------------------------------------------------------------------------- brute force


def brute_force(
    qubo: QUBO, feasible_only: bool = True, top: int = 1
) -> tuple[np.ndarray, np.ndarray]:
    """Exhaustive search over assets for the best unpenalised objective with ``k`` ones.

    Returns ``(bits[top, n], objective[top])`` (F4-R6). Only valid without slack variables.
    """
    n = qubo.n_assets
    if n > BRUTE_FORCE_MAX_N:
        raise ConfigError(f"brute force limited to n ≤ {BRUTE_FORCE_MAX_N}, got {n}")
    best_bits: list[np.ndarray] = []
    best_obj: list[float] = []
    for bits, _ in _chunked_energies(qubo.obj_Q, qubo.obj_offset, n):
        pop = bits.sum(axis=1)
        sel = bits[pop == qubo.k] if feasible_only else bits
        if sel.size == 0:
            continue
        xf = sel.astype(float)
        e = np.einsum("bi,ij,bj->b", xf, qubo.obj_Q, xf, optimize=True) + qubo.obj_offset
        order = np.argsort(e)[:top]
        best_bits.extend(sel[order])
        best_obj.extend(e[order].tolist())
    obj = np.array(best_obj)
    order = np.argsort(obj)[:top]
    return np.array(best_bits)[order], obj[order]


@dataclass
class QuboPreflight:
    """Pre-flight facts reported before any hardware submission (F4-R8)."""

    num_qubits: int
    num_interactions: int
    coef_min_abs: float
    coef_max_abs: float
    coef_range: float
    penalty: float
    penalty_method: str


def preflight(qubo: QUBO) -> QuboPreflight:
    h, _ = qubo_to_ising(qubo.Q, qubo.offset)
    coefs = np.abs(h.coeffs)
    coefs = coefs[coefs > 1e-15]
    nz_pairs = sum(1 for lbl in h.paulis if str(lbl).count("Z") == 2)
    lo = float(coefs.min()) if coefs.size else 0.0
    hi = float(coefs.max()) if coefs.size else 0.0
    return QuboPreflight(
        num_qubits=qubo.num_qubits,
        num_interactions=int(nz_pairs),
        coef_min_abs=lo,
        coef_max_abs=hi,
        coef_range=hi / lo if lo > 0 else float("inf"),
        penalty=qubo.penalty,
        penalty_method=str(qubo.info.get("penalty_method", "?")),
    )
