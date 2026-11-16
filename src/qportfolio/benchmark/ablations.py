"""Ablation studies (RESEARCH_RATIONALE §7.2) — each toggles exactly one ingredient.

  A1  full-problem classical vs decomposed classical      (cost/benefit of decomposition)
  A2  decomposed classical sampler vs decomposed quantum  (the quantum contribution itself)
  A3  single-best vs menu output                           (value of the menu; same solver)
  A4  raw vs residual-correlation clustering               (H2)
  A5  mean-cost vs tail-focused (CVaR) training             (H4)
  A6  penalty vs constraint-preserving (XY/Dicke) encoding  (H5)
  A7  ideal vs noisy simulator, ± mitigation               (H8)
  A8  cluster-size sweep                                   (where fidelity breaks, where classical exactness stops)
  A9  coupling-thinning sweep                              (formulation vs execution fidelity)

A1/A2/A3 fall out of the main benchmark (baselines ``sa_qubo``/``decomposed_classical`` and the
single-best vs menu columns). This module provides the *configuration sweeps* A4–A9.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from qportfolio.benchmark.instances import Instance
from qportfolio.config.loader import deep_update
from qportfolio.config.schema import Config
from qportfolio.pipeline import prepare, run_pipeline

NOISY = {"type": "noisy_simulator", "name": "fake_sherbrooke"}


@dataclass
class AblationArm:
    label: str
    override: dict[str, Any]


ABLATIONS: dict[str, list[AblationArm]] = {
    "A4_clustering_input": [
        AblationArm("raw_correlation", {"decomposition": {"remove_market_mode": False}}),
        AblationArm("residual_correlation", {"decomposition": {"remove_market_mode": True}}),
    ],
    "A5_training_objective": [
        AblationArm("mean_cost", {"solver": {"cvar_alpha": 1.0}}),
        AblationArm("cvar_0.25", {"solver": {"cvar_alpha": 0.25}}),
    ],
    "A6_encoding": [
        AblationArm("penalty", {"solver": {"encoding": "penalty"}}),
        AblationArm("xy_dicke", {"solver": {"encoding": "xy"}}),
    ],
    "A7_noise": [
        AblationArm("ideal", {"solver": {"backend": {"type": "simulator"}}}),
        AblationArm("noisy", {"solver": {"backend": NOISY}}),
        AblationArm(
            "noisy+mitigation", {"solver": {"backend": NOISY, "mitigation": {"enabled": True}}}
        ),
    ],
    "A8_cluster_size": [
        AblationArm(f"max_cluster={m}", {"decomposition": {"max_cluster_size": m}})
        for m in (6, 9, 12, 14)
    ],
    "A9_coupling_thinning": [
        AblationArm(f"thin={t}", {"solver": {"thin_couplings": t}}) for t in (0.0, 0.25, 0.5, 0.75)
    ],
}


def run_ablation(
    name: str,
    inst: Instance,
    seeds: int = 3,
    overrides_base: dict[str, Any] | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Run each arm of ablation ``name`` on ``inst`` for ``seeds`` seeds."""
    if name not in ABLATIONS:
        raise KeyError(f"unknown ablation {name!r}; available {sorted(ABLATIONS)}")
    universe = inst.universe()
    rows: list[dict[str, Any]] = []
    for arm in ABLATIONS[name]:
        for s in range(seeds):
            if progress:
                progress(f"{name}:{arm.label} seed {s}")
            raw = deep_update(
                inst.cfg.model_dump(mode="json"), deep_update(overrides_base or {}, arm.override)
            )
            raw["run"]["seed"] = inst.seed * 1000 + s
            cfg = Config.model_validate(raw)
            _u, _cl, _alloc, summary, _q, _cm, _t = prepare(cfg, universe)
            res = run_pipeline(cfg, universe=universe)
            pools = res.pools.values()
            feas = [
                p.solver_info.get("raw_feasible_fraction", 1.0)
                for p in pools
                if not p.solver_info.get("trivial")
            ]
            rank_ag = [
                p.solver_info["rank_agreement"] for p in pools if "rank_agreement" in p.solver_info
            ]
            depth = [
                p.solver_info["circuit_depth"] for p in pools if "circuit_depth" in p.solver_info
            ]
            twoq = [
                p.solver_info["circuit_two_qubit_gates"]
                for p in pools
                if "circuit_two_qubit_gates" in p.solver_info
            ]
            rows.append(
                {
                    "ablation": name,
                    "arm": arm.label,
                    "seed": s,
                    "best": res.best_objective,
                    "pool_size": len(res.recombined),
                    "menu_size": len(res.menu.portfolios),
                    "menu_min_jaccard": res.menu.diversity_stats.get("min_pairwise_jaccard", 0.0),
                    "cut_share": summary["cut_share_residual_corr"],
                    "n_clusters": summary["n_clusters"],
                    "max_qubits": max((q.num_qubits for q in res.qubos.values()), default=0),
                    "mean_raw_feasible_fraction": float(np.mean(feas)) if feas else None,
                    "mean_rank_agreement": float(np.mean(rank_ag)) if rank_ag else None,
                    "max_depth": max(depth) if depth else None,
                    "max_two_qubit_gates": max(twoq) if twoq else None,
                    "seconds": sum(res.timings.values()),
                }
            )
    return rows


def ablation_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Per-arm medians of the headline metrics."""
    arms: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        arms.setdefault(r["arm"], []).append(r)
    out: dict[str, dict[str, float]] = {}
    for arm, rs in arms.items():

        def med(key: str, rs: list[dict[str, Any]] = rs) -> float:
            v = [x[key] for x in rs if x.get(key) is not None]
            return float(np.median(v)) if v else float("nan")

        out[arm] = {
            k: med(k)
            for k in (
                "best",
                "pool_size",
                "menu_size",
                "menu_min_jaccard",
                "cut_share",
                "mean_raw_feasible_fraction",
                "mean_rank_agreement",
                "max_depth",
                "max_two_qubit_gates",
                "seconds",
            )
        }
        out[arm]["n_runs"] = float(len(rs))
    return out
