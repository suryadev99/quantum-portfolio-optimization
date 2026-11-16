"""F8 — classical baselines, run on the identical μ, Σ and mandate (spec §13.2).

Every baseline returns a :class:`BaselineResult` with a *pool* of feasible selections (indicator
matrix + exact objective), so quality **and** diversity can be compared with the quantum pipeline
through the same menu logic. All pools are gated by the same hard-constraint model.

  equal_weight        sanity floor: the K eligible assets with the best return per unit volatility
  greedy              simple heuristic: marginal-objective greedy construction
  miqp_timelimit      mixed-integer QP (SCIP) with a wall-clock limit, warm-started by greedy
  sa_qubo             simulated annealing on the full-universe selection problem (swap moves)
  decomposed_classical  the pipeline with the quantum step replaced by an exact/SA sub-solver
                      (isolates the quantum contribution — ablation A2)
  multistart_classical  many independent SA/greedy restarts + polish (fair sampler for H6/H7)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from qportfolio.config.schema import Config
from qportfolio.menu import build_menu
from qportfolio.pipeline import run_pipeline
from qportfolio.recombination import ConstraintModel, Recombined, swap_polish
from qportfolio.recombination.beam import expand_neighbourhood
from qportfolio.solvers.classical import _sa_swap
from qportfolio.types import PortfolioMenu, Universe
from qportfolio.utils.errors import QPortfolioError
from qportfolio.utils.linalg import selection_objective_batch
from qportfolio.utils.logging import get_logger
from qportfolio.utils.rng import make_rng

log = get_logger("baselines")


@dataclass
class BaselineResult:
    name: str
    rec: Recombined
    seconds: float
    menu: PortfolioMenu | None = None
    info: dict = field(default_factory=dict)

    @property
    def best(self) -> float | None:
        return float(self.rec.objective[0]) if len(self.rec) else None


# ----------------------------------------------------------------------------- shared helpers


def _finalize(
    universe: Universe,
    cm: ConstraintModel,
    cfg: Config,
    rows: list[np.ndarray],
    polish: bool = True,
    expand: bool = True,
) -> Recombined:
    """Gate + exact-rescore + (optional) polish/expansion; mirrors the pipeline's recombine tail."""
    n = universe.n
    K, lam = cfg.mandate.K, cfg.mandate.lambda_risk
    if not rows:
        return Recombined(np.zeros((0, n), dtype=np.int8), np.zeros(0), {})
    X = np.unique(np.vstack(rows).astype(np.int8), axis=0)
    X = X[cm.structural_ok(X)]
    if cm.turnover_active and len(X):
        good = cm.gate(X)
        X = X[good]
    if len(X) == 0:
        return Recombined(np.zeros((0, n), dtype=np.int8), np.zeros(0), {})
    obj = selection_objective_batch(universe.sigma, universe.mu, X.astype(float), lam, K)
    order = np.argsort(obj, kind="stable")
    X, obj = X[order][: cfg.recombination.top_r], obj[order][: cfg.recombination.top_r]
    if polish and cfg.recombination.local_polish:
        add, add_o = [], []
        for r in range(min(cfg.recombination.polish_top, len(X))):
            sel, o = swap_polish(
                universe, cm, np.flatnonzero(X[r]), cfg.mandate, cfg.recombination.max_polish_iters
            )
            row = np.zeros(n, dtype=np.int8)
            row[sel] = 1
            add.append(row)
            add_o.append(o)
        if add:
            X = np.vstack([X, np.array(add)])
            obj = np.concatenate([obj, np.array(add_o)])
            X, u = np.unique(X, axis=0, return_index=True)
            obj = obj[u]
            order = np.argsort(obj, kind="stable")
            X, obj = X[order], obj[order]
    if expand and cfg.recombination.expand_neighbourhood and len(X):
        band = abs(float(obj[0])) * cfg.recombination.expansion_pct / 100.0
        er, eo = expand_neighbourhood(
            universe,
            cm,
            cfg.mandate,
            X,
            obj,
            band,
            cfg.recombination.expansion_seeds,
            cfg.recombination.expansion_max,
        )
        if len(er):
            X = np.vstack([X, er])
            obj = np.concatenate([obj, eo])
            X, u = np.unique(X, axis=0, return_index=True)
            obj = obj[u]
            order = np.argsort(obj, kind="stable")
            X, obj = X[order], obj[order]
    return Recombined(X, obj, {})


def _greedy_selection(
    universe: Universe,
    cm: ConstraintModel,
    cfg: Config,
    rng: np.random.Generator | None = None,
    noise: float = 0.0,
) -> np.ndarray | None:
    """Marginal-objective greedy construction honouring eligibility, forced-in, sector capacity."""
    n = universe.n
    K, lam = cfg.mandate.K, cfg.mandate.lambda_risk
    sigma, mu = universe.sigma, universe.mu
    scale = lam / K**2
    x = np.zeros(n)
    if len(cm.forced_in):
        x[cm.forced_in] = 1.0
    h = sigma @ x
    avail = cm.eligible & (x < 0.5)
    diag = np.diag(sigma)
    while int(x.sum()) < K:
        delta = scale * (2.0 * h + diag) - mu / K
        if noise > 0 and rng is not None:
            delta = delta + rng.normal(0.0, noise * (np.abs(delta).mean() + 1e-12), size=n)
        delta = np.where(avail, delta, np.inf)
        remaining = K - int(x.sum()) - 1
        placed = False
        for j in np.argsort(delta):
            if not np.isfinite(delta[j]):
                break
            xn = x.copy()
            xn[j] = 1.0
            if cm.capacity(xn[None, :])[0] + remaining * cm.w_max >= 1.0 - 1e-9:
                x = xn
                h = h + sigma[:, j]
                avail[j] = False
                placed = True
                break
        if not placed:
            return None
    return np.flatnonzero(x)


def _feasible_start(universe, cm, cfg, rng) -> np.ndarray | None:
    for noise in (0.0, 0.3, 1.0, 3.0):
        sel = _greedy_selection(universe, cm, cfg, rng, noise)
        if sel is not None and cm.structural_ok(_ind(sel, universe.n)[None, :])[0]:
            return sel
    return None


def _ind(sel: np.ndarray, n: int) -> np.ndarray:
    v = np.zeros(n, dtype=np.int8)
    v[np.asarray(sel, dtype=int)] = 1
    return v


# ----------------------------------------------------------------------------- baselines


def b_equal_weight(universe, cm, cfg, seed):
    """Sanity floor: hold the K eligible assets with the best return per unit variance."""
    t0 = time.perf_counter()
    score = universe.mu / np.sqrt(np.clip(np.diag(universe.sigma), 1e-12, None))
    score = np.where(cm.eligible, score, -np.inf)
    forced = set(cm.forced_in.tolist())
    order = [i for i in np.argsort(-score) if i not in forced and np.isfinite(score[i])]
    sel = list(forced) + order[: cfg.mandate.K - len(forced)]
    rows = [_ind(np.array(sel), universe.n)]
    rec = _finalize(universe, cm, cfg, rows, polish=False, expand=False)
    return BaselineResult(
        "equal_weight", rec, time.perf_counter() - t0, info={"feasible": len(rec) > 0}
    )


def b_greedy(universe, cm, cfg, seed):
    t0 = time.perf_counter()
    sel = _greedy_selection(universe, cm, cfg)
    rows = [_ind(sel, universe.n)] if sel is not None else []
    rec = _finalize(universe, cm, cfg, rows, polish=False, expand=False)
    return BaselineResult("greedy", rec, time.perf_counter() - t0)


def b_miqp(universe, cm, cfg, seed):
    """Full-universe MIQP via SCIP with a time limit; warm-started with the greedy solution."""
    from pyscipopt import Model, quicksum

    t0 = time.perf_counter()
    n = universe.n
    K, lam = cfg.mandate.K, cfg.mandate.lambda_risk
    sigma, mu = universe.sigma, universe.mu
    m = Model("miqp")
    m.hideOutput()
    m.setParam("limits/time", cfg.benchmark.miqp_time_limit)
    m.setParam("randomization/randomseedshift", int(seed) % 1000)
    x = [m.addVar(vtype="B", name=f"x{i}") for i in range(n)]
    t = m.addVar(lb=None, name="t")
    for i in range(n):
        if not cm.eligible[i]:
            m.addCons(x[i] == 0)
    for i in cm.forced_in:
        m.addCons(x[int(i)] == 1)
    m.addCons(quicksum(x) == K)
    scale = lam / K**2
    # Sector rule: a selection is feasible iff Σ_s min(cap_s, n_s·w_max) ≥ 1 (exact, see
    # constraints.py). That is linear after splitting n_s·w_max vs cap_s with auxiliary variables:
    #   cap_s_used ≤ cap_s,  cap_s_used ≤ w_max · n_s(x),  Σ_s cap_s_used ≥ 1.
    used = []
    for s in range(cm.n_sectors):
        members = np.flatnonzero(cm.sector_code == s)
        u = m.addVar(lb=0.0, ub=float(cm.caps[s]), name=f"cap{s}")
        m.addCons(u <= cfg.mandate.w_max * quicksum(x[int(i)] for i in members))
        used.append(u)
    m.addCons(quicksum(used) >= 1.0)
    m.addCons(
        scale * quicksum(float(sigma[i, j]) * x[i] * x[j] for i in range(n) for j in range(n))
        - quicksum(float(mu[i]) / K * x[i] for i in range(n))
        <= t
    )
    m.setObjective(t, "minimize")
    start = _greedy_selection(universe, cm, cfg)
    if start is not None:
        sol = m.createSol()
        for i in range(n):
            m.setSolVal(sol, x[i], 1.0 if i in set(start.tolist()) else 0.0)
        m.addSol(sol)
    m.optimize()
    rows: list[np.ndarray] = []
    for sol in m.getSols()[:50]:
        sel = np.array([i for i in range(n) if m.getSolVal(sol, x[i]) > 0.5])
        if len(sel) == K:
            rows.append(_ind(sel, n))
    if start is not None:
        rows.append(_ind(start, n))
    gap = m.getGap() if m.getNSols() else None
    rec = _finalize(universe, cm, cfg, rows, polish=True, expand=True)
    return BaselineResult(
        "miqp_timelimit",
        rec,
        time.perf_counter() - t0,
        info={"status": m.getStatus(), "gap": gap},
    )


def _full_sa(universe, cm, cfg, seed, restarts, sweeps, snaps=True) -> list[np.ndarray]:
    n = universe.n
    K, lam = cfg.mandate.K, cfg.mandate.lambda_risk
    # full-universe selection QUBO matrix (forced-in fixed; ineligible pinned out by big penalty)
    Q = lam / K**2 * universe.sigma.copy()
    Q[np.diag_indices(n)] += -universe.mu / K
    if len(cm.forced_in):
        f = cm.forced_in
        Q[np.diag_indices(n)] += 2.0 * lam / K**2 * universe.sigma[:, f].sum(axis=1)
    big = 10.0 * (np.abs(Q).max() + 1.0)
    Q[np.diag_indices(n)] += np.where(cm.eligible, 0.0, big)
    free = np.ones(n, dtype=bool)
    free[cm.forced_in] = False
    fi = np.flatnonzero(free)
    Qf = np.ascontiguousarray(Q[np.ix_(fi, fi)])
    k_free = K - len(cm.forced_in)
    rng = make_rng(seed, "full_sa")
    m = len(fi)
    snap_every = max(1, m)
    max_snaps = (sweeps * m // 2) // snap_every + 2
    ob = np.zeros((restarts, max_snaps, m))
    on = np.zeros(restarts, dtype=np.int64)
    scale = float(np.abs(Qf).mean() + 1e-12) * max(2, k_free)
    _sa_swap(
        Qf,
        k_free,
        sweeps,
        restarts,
        scale * 2.0,
        scale * 0.01,
        int(rng.integers(1, 2**31 - 1)),
        snap_every,
        ob,
        on,
    )
    rows = []
    for r in range(restarts):
        for s in range(on[r]):
            full = np.zeros(n, dtype=np.int8)
            full[fi[ob[r, s] > 0.5]] = 1
            full[cm.forced_in] = 1
            rows.append(full)
    return rows


def b_sa_qubo(universe, cm, cfg, seed):
    t0 = time.perf_counter()
    rows = _full_sa(universe, cm, cfg, seed, restarts=24, sweeps=150)
    rec = _finalize(universe, cm, cfg, rows, polish=True, expand=True)
    return BaselineResult("sa_qubo", rec, time.perf_counter() - t0, info={"raw_samples": len(rows)})


def b_multistart(universe, cm, cfg, seed):
    """Many randomised greedy starts, each polished — a fair classical diversity competitor."""
    t0 = time.perf_counter()
    rng = make_rng(seed, "multistart")
    rows: list[np.ndarray] = []
    for r in range(60):
        sel = _greedy_selection(universe, cm, cfg, rng, noise=0.5 + 0.1 * (r % 10))
        if sel is None:
            continue
        sel2, _ = swap_polish(universe, cm, sel, cfg.mandate, cfg.recombination.max_polish_iters)
        rows.append(_ind(sel2, universe.n))
        rows.append(_ind(sel, universe.n))
    rec = _finalize(universe, cm, cfg, rows, polish=False, expand=True)
    return BaselineResult(
        "multistart_classical", rec, time.perf_counter() - t0, info={"starts": 60}
    )


def b_decomposed_classical(universe, cm, cfg, seed, algorithm="exact"):
    """The full pipeline with the quantum step replaced by a classical sub-solver (ablation A2)."""
    t0 = time.perf_counter()
    c2 = cfg.model_copy(deep=True)
    c2.solver.algorithm = algorithm  # type: ignore[assignment]
    c2.run.seed = seed
    res = run_pipeline(c2, universe=universe)
    return BaselineResult(
        f"decomposed_classical[{algorithm}]",
        res.recombined,
        time.perf_counter() - t0,
        menu=res.menu,
        info={"clusters": len(res.clusters)},
    )


REGISTRY: dict[str, Callable] = {
    "equal_weight": b_equal_weight,
    "greedy": b_greedy,
    "miqp_timelimit": b_miqp,
    "sa_qubo": b_sa_qubo,
    "multistart_classical": b_multistart,
    "decomposed_classical": b_decomposed_classical,
}


def run_baseline(
    name: str, universe: Universe, cm: ConstraintModel, cfg: Config, seed: int
) -> BaselineResult:
    if name not in REGISTRY:
        raise QPortfolioError(f"unknown baseline {name!r}; available: {sorted(REGISTRY)}")
    res = REGISTRY[name](universe, cm, cfg, seed)
    if res.menu is None:
        res.menu = build_menu(universe, cm, cfg.mandate, res.rec, cfg.menu)
    return res
