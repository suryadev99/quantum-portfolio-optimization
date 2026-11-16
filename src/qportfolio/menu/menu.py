"""F7 — menu selection, weighting and ranking.

1. ε-band: keep portfolios within a tolerance of the best (absolute, percentage or *uncertainty-
   derived* from the standard error of μ — RESEARCH_RATIONALE §6.2).
2. Diversity: greedy max-min distance selection inside the band.
3. Two-stage weighting: solve the smooth convex weighting QP for each selected set.
4. Ranking by secondary criteria (liquidity, ESG, turnover, sector concentration, objective).
5. Per-portfolio explanation payload.
"""

from __future__ import annotations

from collections.abc import Callable

import cvxpy as cp
import numpy as np

from qportfolio.config.schema import MandateConfig, MenuConfig
from qportfolio.recombination.beam import Recombined
from qportfolio.recombination.constraints import ConstraintModel
from qportfolio.types import Portfolio, PortfolioMenu, Universe
from qportfolio.utils.linalg import pairwise_hamming, pairwise_jaccard

# ----------------------------------------------------------------------------- weighting


def optimize_weights(
    universe: Universe,
    cm: ConstraintModel,
    mandate: MandateConfig,
    sel: np.ndarray,
    solver: str = "CLARABEL",
) -> tuple[np.ndarray | None, str]:
    """Minimise ``lam·w'Σw − μ'w`` over the selection with all linear constraints (F7-R3, §10.9)."""
    sel = np.asarray(sel, dtype=int)
    k = len(sel)
    sig = universe.sigma[np.ix_(sel, sel)]
    mu = universe.mu[sel]
    w = cp.Variable(k)
    cons = [cp.sum(w) == 1.0, w >= 0.0, w <= mandate.w_max]
    codes = cm.sector_code[sel]
    for s in np.unique(codes):
        if cm.caps[s] < 1.0 - 1e-12:
            cons.append(cp.sum(w[np.flatnonzero(codes == s)]) <= cm.caps[s])
    if cm.turnover_active:
        cur = cm.current_weight[sel]
        outside = float(cm.current_weight.sum() - cur.sum())
        cons.append(0.5 * (cp.norm1(w - cur) + outside) <= mandate.turnover_limit)
    prob = cp.Problem(
        cp.Minimize(mandate.lambda_risk * cp.quad_form(w, cp.psd_wrap(sig)) - mu @ w), cons
    )
    try:
        prob.solve(solver=getattr(cp, solver, cp.CLARABEL))
    except cp.SolverError:
        try:
            prob.solve(solver=cp.SCS)
        except cp.SolverError:
            return None, "solver_error"
    if prob.status not in ("optimal", "optimal_inaccurate") or w.value is None:
        return None, str(prob.status)
    wv = np.clip(np.asarray(w.value, dtype=float), 0.0, mandate.w_max)
    wv = wv / wv.sum()
    return wv, str(prob.status)


# ----------------------------------------------------------------------------- epsilon band


def epsilon_value(
    universe: Universe, objective: np.ndarray, mandate: MandateConfig, cfg: MenuConfig
) -> float:
    """Tolerance on the *selection objective* defining statistically tied portfolios."""
    best = float(objective[0])
    if cfg.epsilon_mode == "abs":
        return cfg.epsilon_abs
    if cfg.epsilon_mode == "pct":
        return abs(best) * cfg.epsilon_pct / 100.0
    # uncertainty-derived: SE of the equal-weight expected-return term (spec ideas §6.2)
    if universe.mu_se is None:
        return abs(best) * cfg.epsilon_pct / 100.0
    k = mandate.K
    se = float(np.sqrt((universe.mu_se**2).sum()) / k / np.sqrt(k))  # SE of mean over K picks
    return cfg.epsilon_z * se


# ----------------------------------------------------------------------------- ranking


def _minmax(v: np.ndarray, higher_better: bool) -> np.ndarray:
    lo, hi = float(v.min()), float(v.max())
    if hi - lo < 1e-12:
        return np.full_like(v, 0.5, dtype=float)
    z = (v - lo) / (hi - lo)
    return z if higher_better else 1.0 - z


CriterionFn = Callable[[np.ndarray, Universe, ConstraintModel, np.ndarray | None, float], float]


def _crit_liquidity(sel, u, cm, w, obj) -> float:
    wt = np.full(len(sel), 1.0 / len(sel)) if w is None else w
    return float(wt @ u.liquidity[sel])


def _crit_esg(sel, u, cm, w, obj) -> float:
    e = u.esg[sel]
    if np.isnan(e).all():
        return 0.0
    wt = np.full(len(sel), 1.0 / len(sel)) if w is None else w
    return float(wt @ np.nan_to_num(e, nan=float(np.nanmean(e))))


def _crit_turnover(sel, u, cm, w, obj) -> float:
    full = np.zeros(u.n)
    full[sel] = (1.0 / len(sel)) if w is None else w
    return cm.turnover_of(full)  # lower is better


def _crit_sector_hhi(sel, u, cm, w, obj) -> float:
    wt = np.full(len(sel), 1.0 / len(sel)) if w is None else w
    sec = cm.sector_code[sel]
    tot = np.bincount(sec, weights=wt, minlength=cm.n_sectors)
    return float((tot**2).sum())  # lower is better


def _crit_objective(sel, u, cm, w, obj) -> float:
    return float(obj)  # lower is better


CRITERIA: dict[str, tuple[CriterionFn, bool]] = {
    "liquidity": (_crit_liquidity, True),
    "esg": (_crit_esg, True),
    "turnover": (_crit_turnover, False),
    "sector_concentration": (_crit_sector_hhi, False),
    "objective": (_crit_objective, False),
}


def rank_portfolios(
    portfolios: list[Portfolio], universe: Universe, cm: ConstraintModel, cfg: MenuConfig
) -> list[Portfolio]:
    """Order by configurable secondary criteria (F7-R4); pluggable via :data:`CRITERIA`."""
    if not portfolios:
        return portfolios
    names = (
        list(cfg.ranking.weights) if cfg.ranking.method == "weighted" else list(cfg.ranking.order)
    )
    raw: dict[str, np.ndarray] = {}
    for name in names:
        if name not in CRITERIA:
            raise ValueError(f"unknown ranking criterion {name!r}; available {sorted(CRITERIA)}")
        fn, _ = CRITERIA[name]
        raw[name] = np.array(
            [
                fn(
                    p.selected_idx,
                    universe,
                    cm,
                    None
                    if p.weights is None
                    else np.array([p.weights[universe.ids[i]] for i in p.selected_idx]),
                    p.objective,
                )
                for p in portfolios
            ]
        )
    norm = {nm: _minmax(raw[nm], CRITERIA[nm][1]) for nm in names}
    if cfg.ranking.method == "weighted":
        wsum = sum(cfg.ranking.weights.values()) or 1.0
        score = sum(cfg.ranking.weights[nm] * norm[nm] for nm in names) / wsum
        order = np.argsort(-score, kind="stable")
    else:  # lexicographic on normalised criteria, best first
        keys = [-norm[nm] for nm in reversed(names)]
        order = np.lexsort(keys)
        score = sum(norm[nm] * (len(names) - i) for i, nm in enumerate(names))
    for p_i, p in enumerate(portfolios):
        p.rank_scores = {nm: float(raw[nm][p_i]) for nm in names}
        p.rank_scores["composite"] = float(score[p_i])
    return [portfolios[i] for i in order]


# ----------------------------------------------------------------------------- explanation


def portfolio_stats(universe: Universe, sel: np.ndarray, w: np.ndarray, rf: float = 0.0) -> dict:
    sig = universe.sigma[np.ix_(sel, sel)]
    ret = float(w @ universe.mu[sel])
    vol = float(np.sqrt(max(w @ sig @ w, 0.0)))
    return {
        "expected_return": ret,
        "volatility": vol,
        "sharpe": (ret - rf) / vol if vol > 0 else 0.0,
        "n_assets": len(sel),
        "max_weight": float(w.max()),
        "effective_n": float(1.0 / np.sum(w**2)),
    }


def constraint_slack(
    universe: Universe, cm: ConstraintModel, mandate: MandateConfig, sel: np.ndarray, w: np.ndarray
) -> dict[str, float]:
    full = np.zeros(universe.n)
    full[sel] = w
    out = {
        "weight_sum_error": float(abs(w.sum() - 1.0)),
        "w_max_slack": float(mandate.w_max - w.max()),
        "min_weight": float(w.min()),
    }
    sec = np.bincount(cm.sector_code, weights=full, minlength=cm.n_sectors)
    for s, name in enumerate(cm.sector_names):
        if cm.caps[s] < 1.0 - 1e-12:
            out[f"sector_cap_slack:{name}"] = float(cm.caps[s] - sec[s])
    if cm.turnover_active:
        out["turnover_slack"] = float(mandate.turnover_limit - cm.turnover_of(full))
    if len(cm.forced_in):
        out["forced_in_satisfied"] = float(np.all(full[cm.forced_in] > 0))
    if mandate.min_esg is not None:
        e = universe.esg[sel]
        out["min_esg_slack"] = float(np.nanmin(e) - mandate.min_esg)
    return out


def violates_hard_constraints(
    universe: Universe, cm: ConstraintModel, mandate: MandateConfig, sel: np.ndarray, w: np.ndarray
) -> list[str]:
    """Independent post-hoc audit of a finished portfolio (NFR7/NFR8)."""
    tol = 1e-7
    bad: list[str] = []
    full = np.zeros(universe.n)
    full[sel] = w
    if len(sel) != mandate.K:
        bad.append(f"cardinality {len(sel)} != K={mandate.K}")
    if abs(w.sum() - 1.0) > 1e-6:
        bad.append("weights do not sum to 1")
    if (w < -tol).any() or (w > mandate.w_max + tol).any():
        bad.append("weight bounds violated")
    if not cm.eligible[sel].all():
        bad.append("ineligible asset selected")
    if len(cm.forced_in) and not np.isin(cm.forced_in, sel).all():
        bad.append("forced-in asset missing")
    sec = np.bincount(cm.sector_code, weights=full, minlength=cm.n_sectors)
    for s, name in enumerate(cm.sector_names):
        if sec[s] > cm.caps[s] + 1e-6:
            bad.append(f"sector cap exceeded: {name}")
    if cm.turnover_active and cm.turnover_of(full) > mandate.turnover_limit + 1e-6:
        bad.append("turnover limit exceeded")
    return bad


def _explain(universe: Universe, p: Portfolio, ref: Portfolio | None) -> dict:
    if ref is None or ref is p:
        return {"is_reference": True}
    a, b = set(p.selected_ids), set(ref.selected_ids)
    return {
        "added_vs_rank1": sorted(a - b),
        "removed_vs_rank1": sorted(b - a),
        "jaccard_distance_to_rank1": 1.0 - len(a & b) / max(len(a | b), 1),
        "objective_delta_vs_rank1": float(p.objective - ref.objective),
    }


# ----------------------------------------------------------------------------- selection


def diverse_subset(dist: np.ndarray, m: int, min_dist: float, seed_idx: int = 0) -> list[int]:
    """Greedy farthest-point (max-min distance) selection starting from the best (index 0)."""
    chosen = [seed_idx]
    mind = dist[seed_idx].copy()
    while len(chosen) < m:
        mind[chosen] = -1.0
        j = int(np.argmax(mind))
        if mind[j] < min_dist:
            break
        chosen.append(j)
        mind = np.minimum(mind, dist[j])
    return chosen


def build_menu(
    universe: Universe,
    cm: ConstraintModel,
    mandate: MandateConfig,
    rec: Recombined,
    cfg: MenuConfig,
) -> PortfolioMenu:
    """Turn the recombined pool into the final ranked menu (F7)."""
    notes: list[str] = []
    if len(rec) == 0:
        return PortfolioMenu([], {}, 0.0, ["no feasible portfolios were found"])
    eps = epsilon_value(universe, rec.objective, mandate, cfg)
    band = rec.objective <= rec.objective[0] + eps + 1e-15
    X = rec.indicators[band]
    obj = rec.objective[band]
    n_band = len(X)
    dist = pairwise_jaccard(X) if cfg.distance == "jaccard" else pairwise_hamming(X)
    chosen = diverse_subset(dist, cfg.size, cfg.min_distance)

    portfolios: list[Portfolio] = []
    for idx in chosen:
        sel = np.flatnonzero(X[idx])
        w, status = optimize_weights(universe, cm, mandate, sel)
        if w is None:
            notes.append(f"weight optimisation failed for a candidate ({status}); skipped")
            continue
        bad = violates_hard_constraints(universe, cm, mandate, sel, w)
        if bad:
            notes.append(f"candidate rejected by audit: {bad}")
            continue
        p = Portfolio(
            selected_idx=sel,
            selected_ids=[universe.ids[i] for i in sel],
            objective=float(obj[idx]),
            weights={universe.ids[i]: float(wi) for i, wi in zip(sel, w, strict=True)},
            stats=portfolio_stats(universe, sel, w),
            constraint_slack=constraint_slack(universe, cm, mandate, sel, w),
        )
        portfolios.append(p)

    if len(portfolios) < cfg.size:
        notes.append(
            f"menu has {len(portfolios)} of {cfg.size} requested portfolios: only {n_band} lie in "
            f"the ε-band (ε={eps:.3g}) and satisfy min_distance={cfg.min_distance}"
        )
    portfolios = rank_portfolios(portfolios, universe, cm, cfg)
    ref = portfolios[0] if portfolios else None
    for r, p in enumerate(portfolios, start=1):
        p.rank = r
        p.explanation = _explain(universe, p, ref)

    stats: dict[str, float] = {"n_in_band": float(n_band), "n_candidates": float(len(rec))}
    if len(portfolios) > 1:
        ind = np.zeros((len(portfolios), universe.n))
        for r, p in enumerate(portfolios):
            ind[r, p.selected_idx] = 1.0
        d = pairwise_jaccard(ind)
        iu = np.triu_indices(len(portfolios), 1)
        stats.update(
            mean_pairwise_jaccard=float(d[iu].mean()),
            min_pairwise_jaccard=float(d[iu].min()),
            max_pairwise_jaccard=float(d[iu].max()),
        )
    return PortfolioMenu(portfolios, stats, float(eps), notes)
