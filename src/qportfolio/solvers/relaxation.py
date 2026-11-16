"""Convex relaxation of a cluster sub-problem (spec C5-R4).

The cluster objective is ``x'(quad)x + lin'x`` with ``quad = λ/K² Σ_cc`` positive semi-definite.
Relaxing ``x ∈ {0,1}ⁿ`` to ``x ∈ [0,1]ⁿ`` with ``Σx = k`` gives a convex QP that a classical solver
solves in milliseconds. Its solution ``c`` is used by

* warm-start QAOA, which biases every qubit towards ``c`` (Egger, Mareček, Woerner), and
* the ``round_relaxation`` control, which uses ``c`` with **no quantum step at all**.

The relaxation only ever *proposes*; every candidate is re-scored exactly downstream.
"""

from __future__ import annotations

import numpy as np

from qportfolio.types import QUBO
from qportfolio.utils.logging import get_logger

log = get_logger("relaxation")


def solve_relaxation(qubo: QUBO) -> tuple[np.ndarray, dict]:
    """Return ``(c, info)`` with ``c ∈ [0,1]^n_assets`` and ``Σc = k`` (or a uniform fallback)."""
    import cvxpy as cp

    n, k = qubo.n_assets, qubo.k
    uniform = np.full(n, k / n if n else 0.0)
    quad = qubo.info.get("obj_quad")
    lin = qubo.info.get("obj_lin")
    if quad is None or lin is None:  # QUBO not built by build_qubo
        return uniform, {"relaxation_status": "no_objective_parts_uniform"}

    quad = (np.asarray(quad) + np.asarray(quad).T) / 2.0
    scale = max(float(np.abs(quad).max(initial=0.0)), float(np.abs(lin).max(initial=0.0)), 1e-12)
    x = cp.Variable(n)
    objective = cp.quad_form(x, cp.psd_wrap(quad / scale)) + (lin / scale) @ x
    problem = cp.Problem(cp.Minimize(objective), [cp.sum(x) == k, x >= 0, x <= 1])
    try:
        problem.solve()
    except cp.error.SolverError as exc:  # pragma: no cover - solver breakdown is rare
        log.warning("relaxation solver failed (%s); using the uniform start", exc)
        return uniform, {"relaxation_status": "solver_error_uniform"}
    if x.value is None or problem.status not in ("optimal", "optimal_inaccurate"):
        return uniform, {"relaxation_status": f"{problem.status}_uniform"}
    c = np.clip(np.asarray(x.value, dtype=float), 0.0, 1.0)
    return c, {"relaxation_status": str(problem.status)}


def clip_start(c: np.ndarray, epsilon: float) -> np.ndarray:
    """Clip to ``[ε, 1-ε]`` (Egger et al.): ε=0.5 is the cold start, small ε is a hard start."""
    return np.clip(np.asarray(c, dtype=float), epsilon, 1.0 - epsilon)


def start_mass_on_rounded(c: np.ndarray) -> float:
    """Initial probability of the single most likely bitstring: ``Π max(cᵢ, 1-cᵢ)`` = (1-ε)ⁿ."""
    c = np.asarray(c, dtype=float)
    return float(np.prod(np.maximum(c, 1.0 - c)))
