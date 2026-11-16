"""Structure study: ablations A12 (denoising), A13 (risk rebalancing) and A15 (clusterer).

Implements ``benchmarks/structure_study.yaml`` exactly as pre-registered. The sub-solver is exact in
every arm so only the decomposition choices differ. The outcome rule is applied mechanically; this
module never decides a hypothesis by hand.
"""

from __future__ import annotations

import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from qportfolio.benchmark.instances import make_instance
from qportfolio.config.loader import deep_update
from qportfolio.menu.menu import violates_hard_constraints
from qportfolio.pipeline import prepare, run_pipeline

SPEC_PATH = Path(__file__).resolve().parents[3] / "benchmarks" / "structure_study.yaml"
STAT_SEED = 20261115


def load_spec(path: Path | str = SPEC_PATH) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text())


def _arm_override(arm: dict[str, Any], size: int) -> dict[str, Any]:
    return {
        "decomposition": {
            "max_cluster_size": size,
            "denoise": arm["denoise"],
            "remove_market_mode": arm["remove_market_mode"],
            "clusterer": arm["clusterer"],
            "risk_rebalance": arm["risk_rebalance"],
        },
        "solver": {"algorithm": "exact"},
    }


def run_one_instance(
    task: tuple[str, int, int, dict[str, Any], int, float],
) -> list[dict[str, Any]]:
    """All arms plus the time-limited MIQP reference on one instance (runs in a worker process)."""
    family, n, seed, arms, size, miqp_time = task
    from qportfolio.baselines import run_baseline

    inst = make_instance(family, n, seed=seed, base={"benchmark": {"miqp_time_limit": miqp_time}})
    universe = inst.universe()
    rows: list[dict[str, Any]] = []
    for name, arm in arms.items():
        raw = deep_update(inst.cfg.model_dump(mode="json"), _arm_override(arm, size))
        cfg = type(inst.cfg).model_validate(raw)
        t0 = time.perf_counter()
        res = run_pipeline(cfg, universe=universe)
        secs = time.perf_counter() - t0
        viol = 0
        for p in res.menu.portfolios:
            w = np.array([p.weights[i] for i in p.selected_ids])
            viol += len(violates_hard_constraints(universe, res.cm, cfg.mandate, p.selected_idx, w))
        s = res.cluster_summary
        rows.append(
            {
                "family": family,
                "n": n,
                "seed": seed,
                "arm": name,
                "best": res.best_objective,
                "menu_size": len(res.menu.portfolios),
                "audit_violations": viol,
                "feasible": res.best_objective is not None,
                "cut_share": s["cut_share_residual_corr"],
                "n_clusters": s["n_clusters"],
                "max_size": s["max_size"],
                "lam_scale": s["lam_scale"],
                "seconds": secs,
            }
        )
    _u, _c, _a, _s, _q, cm, _t = prepare(inst.cfg, universe)
    b = run_baseline("miqp_timelimit", universe, cm, inst.cfg, seed)
    rows.append(
        {
            "family": family,
            "n": n,
            "seed": seed,
            "arm": "__miqp__",
            "best": b.best,
            "seconds": b.seconds,
            "miqp_status": b.info.get("status"),
        }
    )
    return rows


def run_study(spec: dict[str, Any], workers: int = 6, progress=None) -> list[dict[str, Any]]:
    d = spec["design"]
    tasks = [
        (fam, n, 100 + s, spec["arms"], d["max_cluster_size"], 6.0)
        for fam in d["families"]
        for n in d["sizes"]
        for s in range(1, d["seeds"] + 1)
    ]
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, part in enumerate(ex.map(run_one_instance, tasks), 1):
            rows.extend(part)
            if progress:
                progress(i, len(tasks))
    return rows


# ----------------------------------------------------------------------------- statistics


def add_gaps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gap to the best objective found on the instance by ANY arm or the MIQP (lower bound on loss)."""
    by: dict[tuple, list[float]] = {}
    for r in rows:
        if r.get("best") is not None:
            by.setdefault((r["family"], r["n"], r["seed"]), []).append(r["best"])
    ref = {k: min(v) for k, v in by.items()}
    out = []
    for r in rows:
        if r["arm"] == "__miqp__":
            continue
        k = (r["family"], r["n"], r["seed"])
        g = None
        if r.get("best") is not None and k in ref and ref[k] != 0:
            g = 100.0 * (r["best"] - ref[k]) / abs(ref[k])
        out.append({**r, "gap_pct": g, "reference": ref.get(k)})
    return out


def paired_diffs(rows: list[dict[str, Any]], arm: str, control: str, key: str) -> np.ndarray:
    a = {
        (r["family"], r["n"], r["seed"]): r[key]
        for r in rows
        if r["arm"] == arm and r.get(key) is not None
    }
    c = {
        (r["family"], r["n"], r["seed"]): r[key]
        for r in rows
        if r["arm"] == control and r.get(key) is not None
    }
    keys = sorted(a.keys() & c.keys())
    return np.array([a[k] - c[k] for k in keys], dtype=float)


def bootstrap_ci(d: np.ndarray, seed: int, n_boot: int = 5000) -> tuple[float, float, float]:
    if len(d) == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(n_boot, len(d)))
    means = d[idx].mean(axis=1)
    return float(d.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def sign_flip_p(d: np.ndarray, seed: int, n_perm: int = 20000) -> float:
    """Two-sided paired sign-flip permutation p-value for mean(d) = 0 (no distribution assumed)."""
    if len(d) == 0 or not np.any(d):
        return 1.0
    rng = np.random.default_rng(seed)
    signs = rng.choice([-1.0, 1.0], size=(n_perm, len(d)))
    perm = np.abs((signs * d).mean(axis=1))
    return float((np.sum(perm >= abs(d.mean()) - 1e-15) + 1) / (n_perm + 1))


def holm(pvals: dict[str, float]) -> dict[str, float]:
    """Holm-Bonferroni adjusted p-values (monotone step-down)."""
    order = sorted(pvals, key=lambda k: pvals[k])
    m = len(order)
    out: dict[str, float] = {}
    running = 0.0
    for i, k in enumerate(order):
        running = max(running, min(1.0, (m - i) * pvals[k]))
        out[k] = running
    return out


def classify(mean: float, lo: float, hi: float, p_adj: float, tol: float) -> str:
    """The pre-registered outcome rule, applied mechanically."""
    if not np.isfinite(mean):
        return "inconclusive"
    if mean < -tol and hi < 0 and p_adj < 0.05:
        return "supported"
    if (lo >= -tol and hi <= tol) or (mean > tol and lo > 0):
        return "refuted"
    return "inconclusive"


def analyse(rows: list[dict[str, Any]], spec: dict[str, Any]) -> dict[str, Any]:
    rows = add_gaps(rows)
    tol = spec["tolerances"]["gap_pp"]
    tests: dict[str, tuple[str, str]] = {}
    for hid, h in spec["hypotheses"].items():
        for t in h.get("tests", [h["test"]] if "test" in h else []):
            tests[f"{hid}:{t['arm']}_vs_{t['control']}"] = (t["arm"], t["control"])
    stats: dict[str, dict[str, Any]] = {}
    for name, (arm, ctl) in tests.items():
        d = paired_diffs(rows, arm, ctl, "gap_pct")
        mean, lo, hi = bootstrap_ci(d, STAT_SEED)
        stats[name] = {
            "arm": arm,
            "control": ctl,
            "n_pairs": len(d),
            "mean_diff_pp": mean,
            "ci95": [lo, hi],
            "identical_share": float(np.mean(np.abs(d) < 1e-12)) if len(d) else float("nan"),
            "p": sign_flip_p(d, STAT_SEED),
        }
    adj = holm({k: v["p"] for k, v in stats.items()})
    for name, s in stats.items():
        s["p_holm"] = adj[name]
        s["outcome"] = classify(s["mean_diff_pp"], s["ci95"][0], s["ci95"][1], s["p_holm"], tol)
    hyp: dict[str, dict[str, Any]] = {}
    for hid, h in spec["hypotheses"].items():
        keys = [k for k in stats if k.startswith(f"{hid}:")]
        outs = [stats[k]["outcome"] for k in keys]
        if outs and all(o == "supported" for o in outs):
            final = "supported"
        elif any(o == "refuted" for o in outs):
            final = "refuted"
        else:
            final = "inconclusive"
        hyp[hid] = {"claim": h["claim"], "outcome": final, "tests": keys}
    desc: dict[str, Any] = {}
    for arm in spec["arms"]:
        if arm == "default":
            continue
        desc[arm] = {
            "gap_vs_default": bootstrap_ci(
                paired_diffs(rows, arm, "default", "gap_pct"), STAT_SEED
            ),
            "cut_share_vs_default": bootstrap_ci(
                paired_diffs(rows, arm, "default", "cut_share"), STAT_SEED
            ),
        }
    desc["mp_vs_no_denoise"] = {
        "gap": bootstrap_ci(paired_diffs(rows, "mp", "no_denoise", "gap_pct"), STAT_SEED)
    }
    arm_tbl: dict[str, dict[str, float]] = {}
    for arm in spec["arms"]:
        rs = [r for r in rows if r["arm"] == arm]
        g = [r["gap_pct"] for r in rs if r["gap_pct"] is not None]
        arm_tbl[arm] = {
            "runs": float(len(rs)),
            "median_gap_pct": float(np.median(g)) if g else float("nan"),
            "mean_gap_pct": float(np.mean(g)) if g else float("nan"),
            "share_at_reference": float(np.mean([x < 1e-9 for x in g])) if g else float("nan"),
            "median_cut_share": float(np.median([r["cut_share"] for r in rs])),
            "median_clusters": float(np.median([r["n_clusters"] for r in rs])),
            "median_seconds": float(np.median([r["seconds"] for r in rs])),
        }
    gate = {
        "audit_violations": int(sum(r["audit_violations"] for r in rows)),
        "infeasible_runs": int(sum(0 if r["feasible"] else 1 for r in rows)),
        "runs": len(rows),
    }
    gate["passed"] = gate["audit_violations"] == 0 and gate["infeasible_runs"] == 0
    return {"tests": stats, "hypotheses": hyp, "descriptive": desc, "arms": arm_tbl, "gate": gate}
