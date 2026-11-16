"""Demo data layer: runs the best pipeline and its controls, returns plain, testable results.

Kept free of any Streamlit import so it can be unit-tested and reused by notebooks and the benchmark.
Everything here uses synthetic data; the same functions take real data through ``qportfolio.config``.

"Best pipeline" = ``hybrid_decomposed``: Marchenko–Pastur denoising -> modularity decomposition into
small clusters -> a sampler on each cluster -> exact classical recombination -> independent audit.
The sampler is whichever registered algorithm the user picks; the controls run beside it.
"""

from __future__ import annotations

import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from qportfolio.analysis import exact_reference, sampling_study, summarise, uniform_level
from qportfolio.config.loader import config_from_dict
from qportfolio.config.schema import Config
from qportfolio.menu.menu import violates_hard_constraints
from qportfolio.pipeline import PipelineResult, prepare, run_pipeline
from qportfolio.reporting.verdict import verdict_text
from qportfolio.types import QUBO, Universe

# Qiskit 2.6.1 (Rust-backed circuits) was observed to segfault when a *third distinct thread* creates
# a QuantumCircuit or Parameter, even a bare ``QuantumCircuit(4)``. Streamlit runs every rerun on a
# new thread, so all Qiskit work is funnelled through ONE long-lived worker thread. Calls made from
# the worker itself run inline, so decorated functions can call each other without deadlocking.
_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qportfolio-worker")
_WORKER_IDENT: list[int] = []


def _mark_worker() -> None:
    _WORKER_IDENT.append(threading.get_ident())


_WORKER.submit(_mark_worker).result()


def on_worker(fn):
    """Run ``fn`` on the single persistent worker thread (inline if already on it)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if threading.get_ident() in _WORKER_IDENT:
            return fn(*args, **kwargs)
        return _WORKER.submit(fn, *args, **kwargs).result()

    return wrapper


DISCLAIMER = (
    "Simulation on Qiskit Aer with synthetic data. Demonstrates workflow and feasibility, "
    "not quantum advantage. Not investment advice."
)

# Presets: demo cluster ceiling 10, hard stop 12 (statevector and Dicke limits, spec C3-R3).
DEMO_HARD_STOP = 12
PRESETS: dict[str, dict[str, Any]] = {
    "Small (40 assets, K=10)": {"n_assets": 40, "n_sectors": 5, "K": 10, "w_max": 0.20},
    "Medium (80 assets, K=16)": {"n_assets": 80, "n_sectors": 8, "K": 16, "w_max": 0.12},
    "Large (150 assets, K=24)": {"n_assets": 150, "n_sectors": 10, "K": 24, "w_max": 0.08},
}

# The recommended ("best") configuration, and the baselines it is compared against.
BEST = {
    "denoise": "marchenko_pastur",
    "clusterer": "modularity",
    "risk_rebalance": "none",
    "algorithm": "qaoa",
    "start": "warm",
}


@dataclass
class DemoSettings:
    preset: str = "Small (40 assets, K=10)"
    seed: int = 7
    max_cluster_size: int = 10
    denoise: str = BEST["denoise"]
    clusterer: str = BEST["clusterer"]
    risk_rebalance: str = BEST["risk_rebalance"]
    algorithm: str = BEST["algorithm"]
    start: str = BEST["start"]
    warm_epsilon: float = 0.25
    p_layers: int = 2
    shots: int = 2048
    max_iterations: int = 40
    sector_cap: float | None = None
    min_esg: float | None = None
    lambda_risk: float = 3.0

    def to_config(self) -> Config:
        p = PRESETS[self.preset]
        size = min(self.max_cluster_size, DEMO_HARD_STOP)
        mandate: dict[str, Any] = {
            "K": p["K"],
            "lambda_risk": self.lambda_risk,
            "w_max": p["w_max"],
        }
        if self.min_esg is not None:
            mandate["min_esg"] = self.min_esg
        raw = {
            "run": {"seed": self.seed, "name": "demo"},
            "data": {
                "source": "synthetic",
                "synthetic": {
                    "n_assets": p["n_assets"],
                    "n_sectors": p["n_sectors"],
                    "n_factors": 3,
                    "n_days": 500,
                },
            },
            "mandate": mandate,
            "decomposition": {
                "max_cluster_size": size,
                "clusterer": self.clusterer,
                "denoise": self.denoise,
                "risk_rebalance": self.risk_rebalance,
            },
            "solver": {
                "algorithm": self.algorithm,
                "start": self.start if self.algorithm == "qaoa" else "cold",
                "warm_epsilon": self.warm_epsilon,
                "p_layers": self.p_layers,
                "shots": self.shots,
                "max_iterations": self.max_iterations,
                "top_candidates_per_cluster": 40,
                "backend": {"type": "simulator"},
            },
            "recombination": {"beam_width": 150, "top_r": 300},
            "menu": {"size": 8},
        }
        return config_from_dict(raw)


def _apply_sector_cap(cfg: Config, universe: Universe, cap: float | None) -> Config:
    if cap is None:
        return cfg
    names = sorted(set(universe.sectors.tolist()))
    caps = dict.fromkeys(names, cap)
    return cfg.model_copy(update={"mandate": cfg.mandate.model_copy(update={"sector_caps": caps})})


@dataclass
class PipelineRun:
    result: PipelineResult
    seconds: float
    audit: list[dict[str, Any]] = field(default_factory=list)
    verdict: str = ""

    @property
    def violations(self) -> int:
        return sum(len(a["violations"]) for a in self.audit)


def audit_menu(result: PipelineResult) -> list[dict[str, Any]]:
    """Independent re-check of every menu portfolio against the hard constraints (never trusts F6/F7)."""
    out = []
    for p in result.menu.portfolios:
        w = np.array([p.weights[i] for i in p.selected_ids])
        bad = violates_hard_constraints(
            result.universe, result.cm, result.config.mandate, p.selected_idx, w
        )
        out.append({"rank": p.rank, "violations": bad, "n": len(p.selected_ids)})
    return out


@on_worker
def run_demo(settings: DemoSettings) -> PipelineRun:
    cfg = settings.to_config()
    from qportfolio.data import build_universe

    universe = build_universe(cfg)
    cfg = _apply_sector_cap(cfg, universe, settings.sector_cap)
    t0 = time.perf_counter()
    res = run_pipeline(cfg, universe=universe)
    run = PipelineRun(res, time.perf_counter() - t0)
    run.audit = audit_menu(res)
    return run


@on_worker
def compare_with_baselines(settings: DemoSettings) -> list[dict[str, Any]]:
    """Best pipeline versus the strongest honest alternatives on the SAME universe and mandate."""
    from qportfolio.baselines import run_baseline
    from qportfolio.data import build_universe

    cfg = settings.to_config()
    universe = build_universe(cfg)
    cfg = _apply_sector_cap(cfg, universe, settings.sector_cap)
    _u, _cl, _al, _s, _q, cm, _t = prepare(cfg, universe)
    rows: list[dict[str, Any]] = []

    def row(name: str, best: float | None, seconds: float, menu_n: int, extra: str = "") -> None:
        rows.append(
            {
                "method": name,
                "best_objective": best,
                "seconds": seconds,
                "menu_size": menu_n,
                "note": extra,
            }
        )

    t0 = time.perf_counter()
    res = run_pipeline(cfg, universe=universe)
    row(
        "Best pipeline (selected sampler)",
        res.best_objective,
        time.perf_counter() - t0,
        len(res.menu.portfolios),
    )
    for name in ("equal_weight", "greedy", "multistart_classical", "miqp_timelimit"):
        r = run_baseline(name, universe, cm, cfg, settings.seed)
        row(name, r.best, r.seconds, len(r.menu.portfolios) if r.menu else 0)
    base = min((r["best_objective"] for r in rows if r["best_objective"] is not None), default=None)
    for r in rows:
        r["gap_to_best_found_pct"] = (
            None
            if r["best_objective"] is None or base is None or base == 0
            else 100.0 * (r["best_objective"] - base) / abs(base)
        )
    return rows


@on_worker
def pick_study_cluster(settings: DemoSettings) -> tuple[QUBO, dict[str, Any]]:
    """The largest non-trivial cluster sub-problem (so the exact reference is still cheap)."""
    cfg = settings.to_config()
    _u, _clusters, _alloc, _summary, qubos, _cm, _t = prepare(cfg)
    cand = [q for q in qubos.values() if 0 < q.k < q.n_assets and q.n_assets <= DEMO_HARD_STOP]
    if not cand:
        raise ValueError("no non-trivial cluster found; increase the universe or cluster size")
    q = max(cand, key=lambda x: (x.n_assets, x.k))
    return q, {
        "n": q.n_assets,
        "k": q.k,
        "states": 2**q.n_assets,
        "uniform_level": uniform_level(q.n_assets, q.k),
    }


@on_worker
def run_sampling_study(settings: DemoSettings, seeds: int = 5) -> dict[str, Any]:
    """Cold vs warm vs controls on one cluster, same shot budget, against the exact optimum."""
    qubo, meta = pick_study_cluster(settings)
    cfg = settings.to_config().solver
    algos = ["qaoa", "qaoa_warm", "round_relaxation", "random_feasible", "sa"]
    overrides = {"qaoa": {"start": "cold"}, "qaoa_warm": {"warm_epsilon": settings.warm_epsilon}}
    rows = sampling_study(qubo, algos, cfg, seeds=seeds, overrides=overrides)
    ref = exact_reference(qubo)
    return {
        "meta": meta,
        "rows": rows,
        "summary": summarise(rows),
        "exact_best": ref.best,
        "n_selections": len(ref.bits),
    }


@on_worker
def rank_mass(settings: DemoSettings, top: int = 12, seed: int = 0) -> dict[str, Any]:
    """Share of shots on each of the exact top-``top`` selections, per sampler (one seed, same budget).

    Repair merges infeasible shots into feasible selections, so mass here is *after repair* for the
    quantum and relaxation samplers; ``random_feasible`` needs no repair. Labelled as such in the UI.
    """
    from qportfolio.solvers.registry import build_solver

    qubo, meta = pick_study_cluster(settings)
    cfg = settings.to_config().solver
    ref = exact_reference(qubo)
    top = min(top, len(ref.bits))
    keys = [ref.bits[i].tobytes() for i in range(top)]
    algos = {
        "Cold-start QAOA": ("qaoa", {"start": "cold"}),
        "Warm-start QAOA": ("qaoa_warm", {"warm_epsilon": settings.warm_epsilon}),
        "Rounded relaxation (no quantum)": (
            "round_relaxation",
            {"warm_epsilon": settings.warm_epsilon},
        ),
        "Uniform random (control)": ("random_feasible", {}),
    }
    out: dict[str, list[float]] = {}
    for label, (algo, over) in algos.items():
        solver = build_solver(algo, cfg.model_copy(update=over), seed)
        try:
            pool = solver.solve(qubo, seed)
        finally:
            solver.close()
        total = float(sum(pool.counts)) or 1.0
        lookup = {b.tobytes(): c for b, c in zip(pool.bits, pool.counts, strict=True)}
        out[label] = [lookup.get(k, 0) / total for k in keys]
    return {
        "meta": meta,
        "ranks": list(range(1, top + 1)),
        "objective": [float(ref.objective[i]) for i in range(top)],
        "mass": out,
        "uniform_level": meta["uniform_level"],
    }


def warm_start_mass_table(ns=(6, 8, 10, 12), eps=(0.25, 0.10, 0.05)) -> list[dict[str, float]]:
    """Closed-form (1-ε)ⁿ: the initial probability warm start puts on the rounded relaxation."""
    return [{"n": n, **{f"eps={e}": (1 - e) ** n for e in eps}} for n in ns]


def verdict_for(run: PipelineRun, study: dict[str, Any] | None) -> str:
    """Computed, honest verdict from what was actually measured in this session."""
    lines = ["VERDICT (computed from this session's measured results)", ""]
    lines.append(
        f"- Correctness: {len(run.audit)} menu portfolios independently audited, "
        f"{run.violations} hard-constraint violations."
    )
    if study:
        s = study["summary"]
        cold, warm, rnd = s["qaoa"], s["qaoa_warm"], s["random_feasible"]
        rel = s["round_relaxation"]
        lines.append(
            f"- Sampling (cluster n={study['meta']['n']}, k={study['meta']['k']}): median mass on the exact optimum "
            f"cold {cold['mass_on_best']:.3f}, warm {warm['mass_on_best']:.3f}, rounded relaxation "
            f"{rel['mass_on_best']:.3f}, uniform random {rnd['mass_on_best']:.3f}."
        )
        if rel["mass_on_best"] >= warm["mass_on_best"] - 1e-3:
            lines.append(
                "- Attribution: the classical relaxation alone matched or beat warm-start QAOA, so the "
                "gain over cold start is attributable to the classical relaxation, not to quantum evolution."
            )
        elif warm["mass_on_best"] > cold["mass_on_best"]:
            lines.append(
                "- Attribution: warm start beat cold start AND the no-quantum rounded-relaxation control on "
                "this cluster; treat as one preliminary data point, not evidence of quantum advantage."
            )
        if all(v["found_optimum_rate"] == 1.0 for v in s.values() if v is not None):
            lines.append(
                "- Final quality: every sampler, including uniform random, reached the exact optimum after "
                "repair and recombination. At this size the classical steps dominate the result."
            )
    lines.append(
        "- In short: no quantum advantage is claimed or measured. The demonstrated value is the validated "
        "decomposition, the audited recombination and an honest measurement against controls."
    )
    return "\n".join(lines)


__all__ = [
    "BEST",
    "DEMO_HARD_STOP",
    "DISCLAIMER",
    "PRESETS",
    "DemoSettings",
    "PipelineRun",
    "audit_menu",
    "compare_with_baselines",
    "on_worker",
    "pick_study_cluster",
    "rank_mass",
    "run_demo",
    "run_sampling_study",
    "verdict_for",
    "verdict_text",
    "warm_start_mass_table",
]
