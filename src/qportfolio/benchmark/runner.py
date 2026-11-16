"""Benchmark runner: methods × instances × seeds → distributions (spec §13.5, §7.6).

Methods compared on identical μ, Σ and constraints:
  quantum        the full decomposed pipeline with the Qiskit QAOA sampler
  baselines      equal_weight, greedy, miqp_timelimit, sa_qubo, multistart_classical,
                 decomposed_classical (quantum step → exact sub-solver; isolates the quantum part)
Ablations (RESEARCH_RATIONALE §7.2) are separate switchable runs of the quantum pipeline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from qportfolio.baselines import run_baseline
from qportfolio.benchmark.instances import Instance, exact_optimum
from qportfolio.benchmark.metrics import (
    band_diversity,
    count_in_band,
    coverage_of_reference,
    decomposition_loss_pct,
    optimality_gap_pct,
)
from qportfolio.config.schema import Config
from qportfolio.menu import violates_hard_constraints
from qportfolio.pipeline import PipelineResult, prepare, run_pipeline
from qportfolio.recombination import ConstraintModel, Recombined
from qportfolio.types import PortfolioMenu, Universe
from qportfolio.utils.errors import BudgetExceeded
from qportfolio.utils.logging import get_logger

log = get_logger("benchmark")

QUANTUM_METHOD = "quantum_qaoa"


@dataclass
class MethodRun:
    instance: str
    family: str
    n: int
    method: str
    seed: int
    best: float | None
    pool_size: int
    n_band: int
    mean_jaccard: float
    min_jaccard: float
    menu_size: int
    menu_min_jaccard: float
    feasible: bool
    seconds: float
    gap_pct: float | None = None
    coverage: float | None = None
    audit_violations: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__ | {"extra": self.extra}


def _audit_menu(universe: Universe, cm: ConstraintModel, cfg: Config, menu: PortfolioMenu) -> int:
    bad = 0
    for p in menu.portfolios:
        w = np.array([p.weights[universe.ids[i]] for i in p.selected_idx])
        bad += len(violates_hard_constraints(universe, cm, cfg.mandate, p.selected_idx, w))
    return bad


def _row(
    inst: Instance,
    method: str,
    seed: int,
    rec: Recombined,
    menu: PortfolioMenu,
    seconds: float,
    universe: Universe,
    cm: ConstraintModel,
    cfg: Config,
    extra: dict | None = None,
) -> MethodRun:
    best = float(rec.objective[0]) if len(rec) else None
    feas = bool(len(rec) == 0 or cm.structural_ok(rec.indicators).all())
    mstats = menu.diversity_stats if menu else {}
    return MethodRun(
        instance=inst.name,
        family=inst.family,
        n=inst.n,
        method=method,
        seed=seed,
        best=best,
        pool_size=len(rec),
        n_band=0,
        mean_jaccard=0.0,
        min_jaccard=0.0,
        menu_size=len(menu.portfolios) if menu else 0,
        menu_min_jaccard=float(mstats.get("min_pairwise_jaccard", 0.0)),
        feasible=feas,
        seconds=seconds,
        audit_violations=_audit_menu(universe, cm, cfg, menu) if menu else 0,
        extra=extra or {},
    )


def run_instance(
    inst: Instance,
    methods: list[str],
    seeds: int,
    include_quantum: bool = True,
    quantum_cfg_override: Config | None = None,
    reference_eps_pct: float = 3.0,
    progress=None,
) -> list[MethodRun]:
    """Run every method for ``seeds`` seeds on one instance and fill relative metrics."""
    cfg0 = quantum_cfg_override or inst.cfg
    universe = inst.universe()
    runs: list[MethodRun] = []
    pools: dict[tuple[str, int], Recombined] = {}

    # ground truth for tiny instances
    truth = (
        exact_optimum(universe, cfg0.mandate.K, cfg0.mandate.lambda_risk)
        if not (cfg0.mandate.sector_caps or cfg0.mandate.forced_in or cfg0.mandate.turnover_limit)
        else None
    )

    for s in range(seeds):
        seed = inst.seed * 1000 + s
        cfg = cfg0.model_copy(deep=True)
        cfg.run.seed = seed
        _, _, _, summary, _, cm, _ = prepare(cfg, universe)
        if include_quantum:
            if progress:
                progress(f"{inst.name} seed {s} {QUANTUM_METHOD}")
            t0 = time.perf_counter()
            try:
                res: PipelineResult = run_pipeline(cfg, universe=universe)
                dt = time.perf_counter() - t0
                runs.append(
                    _row(
                        inst,
                        QUANTUM_METHOD,
                        seed,
                        res.recombined,
                        res.menu,
                        dt,
                        universe,
                        cm,
                        cfg,
                        extra={
                            "timings": res.timings,
                            "cut_share": summary["cut_share_residual_corr"],
                            "n_clusters": summary["n_clusters"],
                            "usage": res.solver_usage,
                            "stopped_early": res.stopped_early,
                        },
                    )
                )
                pools[(QUANTUM_METHOD, seed)] = res.recombined
            except BudgetExceeded as exc:
                log.error("quantum run aborted: %s", exc)
        for name in methods:
            if progress:
                progress(f"{inst.name} seed {s} {name}")
            r = run_baseline(name, universe, cm, cfg, seed)
            runs.append(
                _row(inst, r.name, seed, r.rec, r.menu, r.seconds, universe, cm, cfg, extra=r.info)
            )
            pools[(r.name, seed)] = r.rec

    _fill_relative_metrics(runs, pools, truth, reference_eps_pct)
    return runs


def _fill_relative_metrics(
    runs: list[MethodRun], pools: dict[tuple[str, int], Recombined], truth: float | None, eps: float
) -> None:
    """Best-known objective is shared per instance; gap/diversity/coverage are relative to it."""
    finite = [r.best for r in runs if r.best is not None]
    if not finite:
        return
    best_known = min(finite) if truth is None else min(truth, min(finite))
    # reference near-optimal set = union of all pools' in-band portfolios (shared yardstick)
    ref_rows = [
        rec.indicators[rec.objective <= best_known + abs(best_known) * eps / 100.0]
        for rec in pools.values()
        if len(rec)
    ]
    ref = np.unique(np.vstack(ref_rows), axis=0) if ref_rows else np.zeros((0, 1))
    if len(ref) > 1500:
        ref = ref[np.random.default_rng(0).choice(len(ref), 1500, replace=False)]
    quantum_best = {r.seed: r.best for r in runs if r.method == QUANTUM_METHOD}
    for r in runs:
        rec = pools.get((r.method, r.seed))
        r.gap_pct = optimality_gap_pct(r.best, best_known)
        if rec is not None and len(rec):
            r.n_band = count_in_band(rec.objective, best_known, eps)
            bd = band_diversity(rec.indicators, rec.objective, best_known, eps)
            r.mean_jaccard, r.min_jaccard = bd["mean_jaccard"], bd["min_jaccard"]
            r.coverage = coverage_of_reference(
                rec.indicators[rec.objective <= best_known + abs(best_known) * eps / 100.0], ref
            )
        if r.method.startswith("decomposed_classical") and r.seed in quantum_best:
            r.extra["quantum_minus_this_gap_pct"] = decomposition_loss_pct(
                quantum_best[r.seed], r.best
            )
    for r in runs:
        r.extra["best_known"] = best_known
        r.extra["truth_known"] = truth is not None
