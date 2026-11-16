"""End-to-end orchestration of stages F1–F7 (+ manifest hooks for F8).

``run_pipeline(config)`` is the one-call entry point (F9-R1). ``dry_run`` reports qubits, depth,
gate counts and shots without submitting any job (AC8, NFR5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from qportfolio.clustering import build_clusters
from qportfolio.config.schema import Config
from qportfolio.data import build_universe, eligible_mask
from qportfolio.formulation import build_qubo, preflight
from qportfolio.menu import build_menu
from qportfolio.recombination import ConstraintModel, Recombined, recombine
from qportfolio.solvers import estimate_resources, make_solver
from qportfolio.solvers.backends import ExecBackend
from qportfolio.solvers.base import Solver
from qportfolio.types import QUBO, CandidatePool, Cluster, KAllocation, PortfolioMenu, Universe
from qportfolio.utils.errors import BudgetExceeded
from qportfolio.utils.logging import get_logger
from qportfolio.utils.timing import StageTimer

log = get_logger("pipeline")


@dataclass
class PipelineResult:
    config: Config
    universe: Universe
    clusters: list[Cluster]
    allocation: KAllocation
    cluster_summary: dict[str, float]
    qubos: dict[tuple[int, int], QUBO]
    pools: dict[tuple[int, int], CandidatePool]
    recombined: Recombined
    menu: PortfolioMenu
    cm: ConstraintModel
    timings: dict[str, float] = field(default_factory=dict)
    solver_usage: dict[str, Any] = field(default_factory=dict)
    stopped_early: str | None = None

    @property
    def best_objective(self) -> float | None:
        return float(self.recombined.objective[0]) if len(self.recombined) else None


def _needed_qubos(
    universe: Universe,
    clusters: list[Cluster],
    allocations: list[np.ndarray],
    cfg: Config,
    lam_scale: float = 1.0,
) -> dict[tuple[int, int], QUBO]:
    """One QUBO per distinct (cluster, free-pick-count) across all allocations."""
    out: dict[tuple[int, int], QUBO] = {}
    for alloc in allocations:
        for cl, k_c in zip(clusters, alloc, strict=True):
            k_free = int(k_c) - len(cl.forced_in)
            key = (cl.id, k_free)
            if key not in out:
                out[key] = build_qubo(universe, cl, int(k_c), cfg.mandate, cfg.qubo, lam_scale)
    return out


def prepare(cfg: Config, universe: Universe | None = None, timer: StageTimer | None = None):
    """Stages F1–F4: universe, clusters, allocations, QUBOs."""
    timer = timer or StageTimer()
    with timer.stage("F1F2_data_estimation"):
        universe = universe or build_universe(cfg)
    with timer.stage("F3_clustering"):
        eligible = eligible_mask(cfg.mandate, universe.ids, universe.esg)
        clusters, alloc, summary, _ = build_clusters(
            universe, eligible, cfg.mandate, cfg.decomposition, cfg.run.seed
        )
    with timer.stage("F4_formulation"):
        allocations = alloc.all()
        qubos = _needed_qubos(universe, clusters, allocations, cfg, summary.get("lam_scale", 1.0))
    cm = ConstraintModel.build(universe, cfg.mandate, eligible)
    return universe, clusters, alloc, summary, qubos, cm, timer


def run_pipeline(
    cfg: Config,
    universe: Universe | None = None,
    solver: Solver | None = None,
) -> PipelineResult:
    """Run the full decomposed pipeline and return every intermediate artefact."""
    universe, clusters, alloc, summary, qubos, cm, timer = prepare(cfg, universe)
    own_solver = solver is None
    solver = solver or make_solver(cfg.solver, cfg.run.seed)
    pools: dict[tuple[int, int], CandidatePool] = {}
    stopped: str | None = None
    try:
        with timer.stage("F5_sampling"):
            keys = list(qubos)
            try:
                if cfg.run.n_jobs != 1 and solver.name != "qaoa":
                    from qportfolio.solvers.factory import solve_all

                    res = solve_all(solver, [qubos[k] for k in keys], cfg.run.seed, cfg.run.n_jobs)
                    pools = dict(zip(keys, res, strict=True))
                else:
                    for key in keys:
                        pools[key] = solver.solve(qubos[key], cfg.run.seed)
            except BudgetExceeded as exc:
                stopped = str(exc)
                log.error("budget exhausted: %s — recombining with pools gathered so far", exc)
        usage = solver.usage()
    finally:
        if own_solver:
            solver.close()

    # allocations whose pools are all available
    ready = [
        a
        for a in alloc.all()
        if all(
            (cl.id, int(k) - len(cl.forced_in)) in pools for cl, k in zip(clusters, a, strict=True)
        )
    ]
    with timer.stage("F6_recombination"):
        rec = recombine(universe, clusters, ready, pools, cfg.mandate, cm, cfg.recombination)
    with timer.stage("F7_menu"):
        menu = build_menu(universe, cm, cfg.mandate, rec, cfg.menu)
    return PipelineResult(
        config=cfg,
        universe=universe,
        clusters=clusters,
        allocation=alloc,
        cluster_summary=summary,
        qubos=qubos,
        pools=pools,
        recombined=rec,
        menu=menu,
        cm=cm,
        timings=timer.as_dict(),
        solver_usage=usage,
        stopped_early=stopped,
    )


def dry_run(cfg: Config, universe: Universe | None = None) -> dict[str, Any]:
    """Resource report without executing any circuit or submitting any job (AC8)."""
    universe, clusters, _alloc, summary, qubos, _cm, _t = prepare(cfg, universe)
    backend = ExecBackend(
        cfg.solver.backend,
        cfg.solver.mitigation,
        cfg.solver.limits,
        opt_level=cfg.solver.transpile_opt_level,
        seed=cfg.run.seed,
        allow_fallback=True,
    )
    rows = []
    for key, q in sorted(qubos.items()):
        row = estimate_resources(q, cfg.solver, backend)
        pf = preflight(q)
        row.update(
            {
                "k_free": key[1],
                "penalty": pf.penalty,
                "penalty_method": pf.penalty_method,
                "coef_range": pf.coef_range,
            }
        )
        rows.append(row)
    real = [r for r in rows if not r.get("trivial")]
    shots = cfg.solver.shots
    total_shots = shots * len(real)
    return {
        "n_assets": universe.n,
        "n_clusters": len(clusters),
        "cluster_summary": summary,
        "n_distinct_subproblems": len(qubos),
        "n_nontrivial_subproblems": len(real),
        "max_qubits": max((r["qubits"] for r in rows), default=0),
        "max_depth": max((r.get("depth", 0) for r in real), default=0),
        "max_two_qubit_gates": max((r.get("two_qubit_gates", 0) for r in real), default=0),
        "sampling_jobs": len(real),
        "total_shots": total_shots,
        "backend": backend.name,
        "backend_kind": backend.kind,
        "backend_notes": backend.notes,
        "limits": cfg.solver.limits.model_dump(),
        "within_job_limit": len(real) <= cfg.solver.limits.max_jobs,
        "subproblems": rows,
        "submitted_jobs": 0,
    }
