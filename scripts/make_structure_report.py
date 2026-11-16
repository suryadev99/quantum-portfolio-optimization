"""Generate benchmarks/results/structure_study_report.md from the saved study rows and analysis."""

from __future__ import annotations

import collections
import json
from pathlib import Path

RES = Path(__file__).resolve().parents[1] / "benchmarks" / "results"


def main() -> None:
    rows = json.loads((RES / "structure_study_rows.json").read_text())
    ana = json.loads((RES / "structure_study_analysis.json").read_text())
    by: dict[tuple, dict[str, dict]] = collections.defaultdict(dict)
    for r in rows:
        by[(r["family"], r["n"], r["seed"])][r["arm"]] = r
    arms = list(ana["arms"])
    n_inst = len(by)
    all_fail = [k for k, v in by.items() if all(not x["feasible"] for x in v.values())]
    mixed = [k for k, v in by.items() if 0 < sum(not x["feasible"] for x in v.values()) < len(v)]
    size_fail = collections.Counter(
        (k[0], k[1]) for k, v in by.items() for x in v.values() if not x["feasible"]
    )

    L: list[str] = []
    L.append("# Structure study report (ablations A12, A13, A15)\n")
    L.append(
        "Generated from `benchmarks/results/structure_study_{rows,analysis}.json` by "
        "`scripts/make_structure_report.py`. Pre-registration: `benchmarks/structure_study.yaml` "
        "(unedited). Synthetic data, simulator-free (exact sub-solver in every arm). "
        "Not investment advice.\n"
    )
    g = ana["gate"]
    L.append("## Correctness gate\n")
    L.append(
        f"- Audit violations: **{g['audit_violations']}** (no infeasible portfolio was emitted).\n"
        f"- Runs with no feasible result: **{g['infeasible_runs']} of {g['runs']}**.\n"
        f"- Gate as pre-registered (`audit_violations == 0 and infeasible_runs == 0`): "
        f"**{'PASSED' if g['passed'] else 'FAILED'}**.\n"
    )
    L.append(
        "All failures are in family C (turnover-constrained mandate): "
        + ", ".join(f"{a} n={b}: {c}" for (a, b), c in sorted(size_fail.items()))
        + f". Instances where every arm failed: {len(all_fail)} of {n_inst}; instances where "
        f"only some arms failed: {len(mixed)}.\n"
    )
    L.append(
        "Mechanism, checked on C n=140 seed 103: all 1,050 beam candidates passed the structural "
        "checks and none passed the exact turnover LP. The QUBO and the exact sub-solver have no "
        "turnover term, so candidates are ranked without regard to it. The pipeline then returns "
        "no portfolio rather than an infeasible one. This is a pipeline limitation exposed by the "
        "study, not a decomposition effect.\n"
    )

    L.append("## Hypothesis outcomes (pre-registered rule, applied mechanically)\n")
    L.append("| Hypothesis | Outcome |\n|---|---|")
    for hid, h in ana["hypotheses"].items():
        L.append(f"| {hid}: {h['claim']} | **{h['outcome']}** |")
    L.append("")
    L.append(
        "| Test | pairs | mean diff (pp) | 95% CI | identical pairs | Holm p | outcome |\n|---|---|---|---|---|---|---|"
    )
    for k, t in ana["tests"].items():
        L.append(
            f"| {k} | {t['n_pairs']} | {t['mean_diff_pp']:.4f} | "
            f"[{t['ci95'][0]:.4f}, {t['ci95'][1]:.4f}] | {t['identical_share']:.1%} | "
            f"{t['p_holm']:.3f} | {t['outcome']} |"
        )
    L.append("")
    L.append(
        "**How to read 'refuted' here.** The rule calls an arm refuted when its 95% CI sits inside "
        "±0.10 pp. With an exact sub-solver at cluster cap 10, 97-100% of paired instances are "
        "identical: the final objective is the same whatever the decomposition. There is no headroom "
        "to show a benefit. So 'refuted' means 'no practically meaningful difference was measured "
        "under these conditions', not 'the technique is harmful'. It says nothing about larger "
        "clusters, an approximate sub-solver or real data.\n"
    )
    L.append(
        "**Selection effect.** Paired tests keep only pairs where both arms returned a portfolio "
        "(99-102 of 108). The instances where arms differ in feasibility are excluded from the "
        "tests above. The table below shows them.\n"
    )

    L.append("## Per-arm summary (all 108 instances)\n")
    L.append(
        "| Arm | feasible runs | mean gap % | share at reference | median cut share | median clusters | median s |\n"
        "|---|---|---|---|---|---|---|"
    )
    for a in arms:
        t = ana["arms"][a]
        feas = sum(1 for v in by.values() if v[a]["feasible"])
        L.append(
            f"| {a} | {feas}/{n_inst} | {t['mean_gap_pct']:.4f} | {t['share_at_reference']:.1%} | "
            f"{t['median_cut_share']:.3f} | {t['median_clusters']:.1f} | {t['median_seconds']:.3f} |"
        )
    L.append("")

    L.append("## Feasibility against the default arm (descriptive, no decision attached)\n")
    L.append(
        "| Arm | feasible where default is not | default feasible where arm is not |\n|---|---|---|"
    )
    for a in arms:
        if a == "default":
            continue
        up = sum(1 for v in by.values() if v[a]["feasible"] and not v["default"]["feasible"])
        dn = sum(1 for v in by.values() if v["default"]["feasible"] and not v[a]["feasible"])
        L.append(f"| {a} | {up} | {dn} |")
    L.append("")
    L.append(
        "These counts are small, unadjusted and were not pre-registered as a test. They suggest "
        "whether an arm happens to land on a turnover-feasible selection, which is close to chance "
        "because no arm models turnover.\n"
    )

    L.append("## Descriptive comparisons (no multiplicity budget spent)\n")
    L.append(
        "| Arm vs default | gap diff pp [mean, lo, hi] | cut-share diff [mean, lo, hi] |\n|---|---|---|"
    )
    for a, d in ana["descriptive"].items():
        if a == "mp_vs_no_denoise":
            continue
        gg, cc = d["gap_vs_default"], d["cut_share_vs_default"]
        L.append(
            f"| {a} | {gg[0]:.4f} [{gg[1]:.4f}, {gg[2]:.4f}] | {cc[0]:.4f} [{cc[1]:.4f}, {cc[2]:.4f}] |"
        )
    mp = ana["descriptive"]["mp_vs_no_denoise"]["gap"]
    L.append(f"\n`mp` vs `no_denoise` gap diff: {mp[0]:.4f} pp [{mp[1]:.4f}, {mp[2]:.4f}].\n")
    L.append(
        "Cut share is *higher* than default for every non-default arm that changes the clustering "
        "or denoising, and the CIs exclude zero. By the common yardstick (residual correlation "
        "between clusters, lower is better) none of the alternatives found a better partition. This "
        "agrees with the earlier H2 result.\n"
    )

    L.append("## Limitations\n")
    L.append(
        "- The reference for each gap is the best objective found by any arm or by the 6 s MIQP "
        "(MIQP rows feed the reference but are not written to the saved rows file). It is not a "
        "proven optimum, and the MIQP returned no incumbent on several turnover instances. Gaps "
        "are lower bounds on true loss.\n"
        "- Ceiling effect: with an exact sub-solver, decomposition rarely changes the answer. The "
        "study cannot show a benefit it has no room to show.\n"
        "- Synthetic data; three families; sizes 60-140; cluster cap 10.\n"
        "- The gate failed as written, so this study is not a clean pass. The failures come from "
        "the turnover mandate, not from the arms.\n"
        "- The Holm correction covers the four pre-registered tests only.\n"
    )
    (RES / "structure_study_report.md").write_text("\n".join(L))
    print("wrote", RES / "structure_study_report.md")


if __name__ == "__main__":
    main()
