"""Evidence-based verdict (spec §13.6, RESEARCH_RATIONALE §7).

The verdict is *computed* from benchmark runs by explicit, auditable rules — never written by hand —
so an unfavourable result is reported as plainly as a favourable one. Each hypothesis resolves to
``supported`` / ``refuted`` / ``inconclusive`` / ``not_assessed`` with the numbers behind it.

Comparisons are paired by (instance, seed). A difference is "meaningful" only if it exceeds both a
practical tolerance and, when n ≥ 5 pairs, the paired 95% interval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from qportfolio.benchmark.runner import QUANTUM_METHOD, MethodRun

GAP_TOL_PCT = 0.5  # practical tolerance on objective gap (percentage points)
DIV_TOL = 0.02  # practical tolerance on diversity (absolute Jaccard)
COV_TOL = 0.05


@dataclass
class Finding:
    id: str
    claim: str
    status: str  # supported | refuted | inconclusive | not_assessed
    detail: str
    numbers: dict[str, Any] = field(default_factory=dict)


def _paired(runs: list[MethodRun], a: str, b: str, key) -> list[tuple[float, float]]:
    ra = {(r.instance, r.seed): key(r) for r in runs if r.method == a}
    rb = {(r.instance, r.seed): key(r) for r in runs if r.method == b}
    out = []
    for k in ra.keys() & rb.keys():
        if ra[k] is not None and rb[k] is not None and np.isfinite(ra[k]) and np.isfinite(rb[k]):
            out.append((float(ra[k]), float(rb[k])))
    return out


def _compare(pairs: list[tuple[float, float]], tol: float, lower_better: bool) -> tuple[str, dict]:
    """Return ('a_better'|'b_better'|'tie'|'inconclusive', stats) for paired samples (a vs b)."""
    if len(pairs) < 2:
        return "inconclusive", {"n_pairs": len(pairs)}
    d = np.array([a - b for a, b in pairs])
    if not lower_better:
        d = -d
    mean = float(d.mean())
    half = 1.96 * d.std(ddof=1) / np.sqrt(len(d)) if len(d) >= 5 else float("inf")
    stats = {
        "n_pairs": len(pairs),
        "mean_diff_a_minus_b": mean * (1 if lower_better else -1),
        "ci95_half": float(half),
    }
    if abs(mean) <= tol and abs(mean) <= half:
        return "tie", stats
    if len(d) < 5:
        return "inconclusive", stats
    if mean < -tol and abs(mean) > half:
        return "a_better", stats
    if mean > tol and abs(mean) > half:
        return "b_better", stats
    return "inconclusive", stats


def quantum_findings(runs: list[MethodRun], has_hardware: bool) -> list[Finding]:
    out: list[Finding] = []
    q = QUANTUM_METHOD
    dec = "decomposed_classical[exact]"
    multi = "multistart_classical"
    miqp = "miqp_timelimit"

    # ---- quality: quantum vs best classical alternatives
    for other, label in (
        (dec, "decomposed + exact sub-solver (isolates the quantum step)"),
        (miqp, "full-problem MIQP (time-limited)"),
    ):
        pairs = _paired(runs, q, other, lambda r: r.gap_pct)
        res, st = _compare(pairs, GAP_TOL_PCT, lower_better=True)
        if res == "a_better":
            status, txt = "supported", f"Quantum pipeline reached lower objective gap than {label}."
        elif res == "b_better":
            status, txt = (
                "refuted",
                f"{label} reached lower objective gap than the quantum pipeline.",
            )
        elif res == "tie":
            status, txt = (
                "refuted",
                f"No quality advantage: quantum ties {label} within {GAP_TOL_PCT} pp (as expected at these cluster sizes).",
            )
        else:
            status, txt = (
                "inconclusive",
                f"Not enough paired runs to separate quantum from {label}.",
            )
        out.append(
            Finding(
                f"Q-quality[{other}]", f"Quantum solution quality exceeds {label}", status, txt, st
            )
        )

    # ---- H6: efficiency of proposals (time) vs multistart sampler
    pairs = _paired(runs, q, multi, lambda r: r.seconds)
    if pairs:
        ratio = float(np.median([a / max(b, 1e-9) for a, b in pairs]))
        out.append(
            Finding(
                "H6",
                "Quantum reaches target quality in less time than an equal-cost classical sampler",
                "refuted" if ratio > 1 else "inconclusive",
                f"Median wall-clock ratio quantum/classical-multistart = {ratio:.1f}× (simulation overhead included).",
                {"median_time_ratio": ratio, "n_pairs": len(pairs)},
            )
        )
    else:
        out.append(
            Finding(
                "H6",
                "Quantum reaches target quality faster than a classical sampler",
                "not_assessed",
                "No paired runs.",
                {},
            )
        )

    # ---- H7: diversity (quantum vs classical samplers) at comparable quality
    best_cl = None
    for other in (multi, "sa_qubo", dec):
        pairs = _paired(runs, q, other, lambda r: r.mean_jaccard)
        if not pairs:
            continue
        res, st = _compare(pairs, DIV_TOL, lower_better=False)
        st["vs"] = other
        status = {
            "a_better": "supported",
            "b_better": "refuted",
            "tie": "refuted",
            "inconclusive": "inconclusive",
        }[res]
        txt = {
            "a_better": f"Quantum menu more diverse than {other}.",
            "b_better": f"{other} menu more diverse than quantum.",
            "tie": f"No diversity difference vs {other} (within {DIV_TOL}).",
            "inconclusive": f"Inconclusive vs {other} (n={st['n_pairs']}).",
        }[res]
        out.append(
            Finding(f"H7[{other}]", f"Quantum menu is more diverse than {other}", status, txt, st)
        )
        best_cl = best_cl or other
    if best_cl is None:
        out.append(
            Finding(
                "H7",
                "Quantum menu is more diverse than classical samplers",
                "not_assessed",
                "No paired runs.",
                {},
            )
        )

    # ---- H3: decomposition loss
    pairs = _paired(runs, dec, miqp, lambda r: r.gap_pct)
    if pairs:
        diffs = np.array([a - b for a, b in pairs])
        med = float(np.median(diffs))
        out.append(
            Finding(
                "H3",
                "Decomposition loss stays small after recombination and polish",
                "supported" if med <= 1.0 else "refuted",
                f"Median gap difference decomposed(exact) − full MIQP = {med:+.3f} pp.",
                {"median_diff_pp": med, "n_pairs": len(pairs)},
            )
        )

    # ---- feasibility / audit (hard requirement, not a hypothesis)
    viol = sum(r.audit_violations for r in runs)
    infeasible = sum(0 if r.feasible else 1 for r in runs)
    out.append(
        Finding(
            "S3",
            "No reported portfolio violates a hard constraint",
            "supported" if viol == 0 and infeasible == 0 else "refuted",
            f"{viol} audit violations and {infeasible} infeasible pools across {len(runs)} runs.",
            {"audit_violations": viol, "infeasible_pools": infeasible, "runs": len(runs)},
        )
    )
    if not has_hardware:
        out.append(
            Finding(
                "S5",
                "At least one run on real IBM hardware is documented",
                "not_assessed",
                "All quantum runs used simulators / local noise models; no QPU credentials were used.",
                {},
            )
        )
    return out


def ablation_findings(ablations: dict[str, dict[str, dict[str, float]]]) -> list[Finding]:
    out: list[Finding] = []

    def arm(a: str, name: str, key: str) -> float | None:
        v = ablations.get(a, {}).get(name, {}).get(key)
        return None if v is None or not np.isfinite(v) else float(v)

    if "A4_clustering_input" in ablations:
        raw, res = (
            arm("A4_clustering_input", "raw_correlation", "cut_share"),
            arm("A4_clustering_input", "residual_correlation", "cut_share"),
        )
        if raw is not None and res is not None:
            out.append(
                Finding(
                    "H2",
                    "Residual-correlation clustering lowers cut share vs raw correlation",
                    "supported"
                    if res < raw - 0.005
                    else ("refuted" if res > raw + 0.005 else "inconclusive"),
                    f"Median cut share: raw {raw:.3f} vs residual {res:.3f} (lower = less ignored covariance).",
                    {"raw": raw, "residual": res},
                )
            )
    if "A5_training_objective" in ablations:
        m, c = (
            arm("A5_training_objective", "mean_cost", "best"),
            arm("A5_training_objective", "cvar_0.25", "best"),
        )
        if m is not None and c is not None:
            out.append(
                Finding(
                    "H4",
                    "Tail-focused (CVaR) training beats mean-cost training",
                    "supported"
                    if c < m - 1e-4
                    else ("refuted" if c > m + 1e-4 else "inconclusive"),
                    f"Median best objective: mean-cost {m:.5f} vs CVaR {c:.5f}.",
                    {"mean_cost": m, "cvar": c},
                )
            )
    if "A6_encoding" in ablations:
        p, x = (
            arm("A6_encoding", "penalty", "mean_raw_feasible_fraction"),
            arm("A6_encoding", "xy_dicke", "mean_raw_feasible_fraction"),
        )
        pb, xb = arm("A6_encoding", "penalty", "best"), arm("A6_encoding", "xy_dicke", "best")
        if p is not None and x is not None:
            out.append(
                Finding(
                    "H5",
                    "Constraint-preserving (XY/Dicke) encoding yields more feasible, better pools than penalty",
                    "supported"
                    if (x > p + 0.05 and (xb is None or pb is None or xb <= pb + 1e-4))
                    else ("refuted" if x <= p + 0.05 else "inconclusive"),
                    f"Raw feasible-sample fraction: penalty {p:.2f} vs XY {x:.2f}; best objective penalty {pb} vs XY {xb}. "
                    "XY circuits are deeper; see depth/2q columns.",
                    {"penalty_feas": p, "xy_feas": x},
                )
            )
    if "A7_noise" in ablations:
        i, n_ = (
            arm("A7_noise", "ideal", "mean_raw_feasible_fraction"),
            arm("A7_noise", "noisy", "mean_raw_feasible_fraction"),
        )
        nm = arm("A7_noise", "noisy+mitigation", "mean_raw_feasible_fraction")
        if i is not None and n_ is not None:
            out.append(
                Finding(
                    "H8",
                    "Quality degrades with noise and mitigation recovers part of it",
                    "supported"
                    if n_ < i - 0.02 and (nm is None or nm >= n_ - 0.005)
                    else "inconclusive",
                    f"Raw feasible fraction: ideal {i:.2f}, noisy {n_:.2f}, noisy+mitigation {nm if nm is None else round(nm, 2)}.",
                    {"ideal": i, "noisy": n_, "mitigated": nm},
                )
            )
    return out


STATUS_ORDER = ["supported", "refuted", "inconclusive", "not_assessed"]


def verdict_text(findings: list[Finding]) -> str:
    """Plain-language verdict (mandatory section, spec §13.6)."""
    by = {f.id: f for f in findings}
    quality = [f for f in findings if f.id.startswith("Q-quality")]
    q_helps = any(f.status == "supported" for f in quality)
    q_hurts = any(f.status == "refuted" and "lower objective gap than" in f.detail for f in quality)
    div = [f for f in findings if f.id.startswith("H7")]
    div_helps = any(f.status == "supported" for f in div)

    lines = ["VERDICT (computed from measured results; see tables for numbers)", ""]
    if q_helps:
        lines.append(
            "• Quality: in at least one comparison the quantum pipeline achieved a meaningfully lower objective gap than a classical alternative."
        )
    elif q_hurts:
        lines.append(
            "• Quality: classical methods reached a meaningfully LOWER objective gap than the quantum pipeline. The quantum step did not improve solution quality."
        )
    else:
        lines.append(
            "• Quality: the quantum pipeline matched, but did not beat, the classical alternatives (differences within tolerance). This is expected: at cluster sizes current hardware can handle, classical solvers solve the sub-problems exactly."
        )
    if div_helps:
        lines.append(
            "• Diversity: the quantum menu was meaningfully more diverse than at least one classical sampler at comparable quality; check which baselines it did NOT beat before drawing conclusions."
        )
    elif div:
        lines.append(
            "• Diversity: no meaningful diversity advantage over strong classical samplers was observed; diversity here is largely a product of decomposition and recombination, which classical pipelines share."
        )
    else:
        lines.append("• Diversity: could not be assessed (no paired runs).")
    if "H6" in by:
        lines.append(f"• Speed: {by['H6'].detail}")
    if "S5" in by and by["S5"].status == "not_assessed":
        lines.append(
            "• Hardware: not assessed. Results are simulator / noise-model only, so no claim is made about real devices."
        )
    lines.append("• Correctness: " + by["S3"].detail if "S3" in by else "")
    lines.append("")
    lines.append(
        "In short: "
        + (
            "there is measured evidence of a quantum contribution in the comparisons flagged 'supported'; treat it as preliminary."
            if q_helps or div_helps
            else "no measured quantum advantage on these instances. The value of the platform is the validated hybrid method, the decomposition/recombination machinery, and the benchmark itself — which will detect an advantage if larger clusters, deeper circuits or better hardware ever produce one."
        )
    )
    return "\n".join(ln for ln in lines if ln is not None)
