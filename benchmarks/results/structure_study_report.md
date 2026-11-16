# Structure study report (ablations A12, A13, A15)

Generated from `benchmarks/results/structure_study_{rows,analysis}.json` by `scripts/make_structure_report.py`. Pre-registration: `benchmarks/structure_study.yaml` (unedited). Synthetic data, simulator-free (exact sub-solver in every arm). Not investment advice.

## Correctness gate

- Audit violations: **0** (no infeasible portfolio was emitted).
- Runs with no feasible result: **46 of 756**.
- Gate as pre-registered (`audit_violations == 0 and infeasible_runs == 0`): **FAILED**.

All failures are in family C (turnover-constrained mandate): C n=100: 8, C n=140: 38. Instances where every arm failed: 2 of 108; instances where only some arms failed: 9.

Mechanism, checked on C n=140 seed 103: all 1,050 beam candidates passed the structural checks and none passed the exact turnover LP. The QUBO and the exact sub-solver have no turnover term, so candidates are ranked without regard to it. The pipeline then returns no portfolio rather than an infeasible one. This is a pipeline limitation exposed by the study, not a decomposition effect.

## Hypothesis outcomes (pre-registered rule, applied mechanically)

| Hypothesis | Outcome |
|---|---|
| H11: Marchenko-Pastur denoising gives a lower decomposition gap than market-mode removal alone. | **refuted** |
| H12: Modularity bisection gives a lower decomposition gap than hierarchical AND than spectral clustering at the same size cap. | **refuted** |
| H19: Risk rebalancing (norm_ratio) gives a lower decomposition gap than no rebalancing. | **refuted** |

| Test | pairs | mean diff (pp) | 95% CI | identical pairs | Holm p | outcome |
|---|---|---|---|---|---|---|
| H11:mp_vs_default | 99 | 0.0007 | [0.0000, 0.0020] | 99.0% | 1.000 | refuted |
| H12:modularity_vs_default | 99 | 0.0216 | [0.0000, 0.0647] | 99.0% | 1.000 | refuted |
| H12:modularity_vs_spectral | 100 | 0.0060 | [-0.0461, 0.0640] | 97.0% | 1.000 | refuted |
| H19:rebalance_vs_default | 102 | 0.0000 | [0.0000, 0.0000] | 100.0% | 1.000 | refuted |

**How to read 'refuted' here.** The rule calls an arm refuted when its 95% CI sits inside ±0.10 pp. With an exact sub-solver at cluster cap 10, 97-100% of paired instances are identical: the final objective is the same whatever the decomposition. There is no headroom to show a benefit. So 'refuted' means 'no practically meaningful difference was measured under these conditions', not 'the technique is harmful'. It says nothing about larger clusters, an approximate sub-solver or real data.

**Selection effect.** Paired tests keep only pairs where both arms returned a portfolio (99-102 of 108). The instances where arms differ in feasibility are excluded from the tests above. The table below shows them.

## Per-arm summary (all 108 instances)

| Arm | feasible runs | mean gap % | share at reference | median cut share | median clusters | median s |
|---|---|---|---|---|---|---|
| default | 102/108 | 0.0000 | 100.0% | 0.800 | 17.0 | 0.117 |
| no_denoise | 101/108 | 0.0076 | 98.0% | 0.809 | 19.5 | 0.125 |
| mp | 100/108 | 0.0007 | 99.0% | 0.830 | 16.0 | 0.116 |
| spectral | 103/108 | 0.0303 | 96.1% | 0.822 | 16.5 | 0.174 |
| modularity | 100/108 | 0.0213 | 99.0% | 0.852 | 14.0 | 0.127 |
| rebalance | 102/108 | 0.0000 | 100.0% | 0.800 | 17.0 | 0.130 |
| best_combo | 102/108 | 0.0110 | 98.0% | 0.853 | 15.0 | 0.114 |

## Feasibility against the default arm (descriptive, no decision attached)

| Arm | feasible where default is not | default feasible where arm is not |
|---|---|---|
| no_denoise | 1 | 2 |
| mp | 1 | 3 |
| spectral | 2 | 1 |
| modularity | 1 | 3 |
| rebalance | 0 | 0 |
| best_combo | 4 | 4 |

These counts are small, unadjusted and were not pre-registered as a test. They suggest whether an arm happens to land on a turnover-feasible selection, which is close to chance because no arm models turnover.

## Descriptive comparisons (no multiplicity budget spent)

| Arm vs default | gap diff pp [mean, lo, hi] | cut-share diff [mean, lo, hi] |
|---|---|---|
| no_denoise | 0.0045 [-0.0000, 0.0136] | 0.0107 [0.0087, 0.0127] |
| mp | 0.0007 [0.0000, 0.0020] | 0.0191 [0.0160, 0.0223] |
| spectral | 0.0131 [-0.0000, 0.0378] | 0.0189 [0.0152, 0.0229] |
| modularity | 0.0216 [0.0000, 0.0647] | 0.0377 [0.0316, 0.0438] |
| rebalance | 0.0000 [0.0000, 0.0000] | 0.0000 [0.0000, 0.0000] |
| best_combo | 0.0026 [0.0000, 0.0077] | 0.0370 [0.0315, 0.0425] |

`mp` vs `no_denoise` gap diff: -0.0070 pp [-0.0178, 0.0000].

Cut share is *higher* than default for every non-default arm that changes the clustering or denoising, and the CIs exclude zero. By the common yardstick (residual correlation between clusters, lower is better) none of the alternatives found a better partition. This agrees with the earlier H2 result.

## Limitations

- The reference for each gap is the best objective found by any arm or by the 6 s MIQP (MIQP rows feed the reference but are not written to the saved rows file). It is not a proven optimum, and the MIQP returned no incumbent on several turnover instances. Gaps are lower bounds on true loss.
- Ceiling effect: with an exact sub-solver, decomposition rarely changes the answer. The study cannot show a benefit it has no room to show.
- Synthetic data; three families; sizes 60-140; cluster cap 10.
- The gate failed as written, so this study is not a clean pass. The failures come from the turnover mandate, not from the arms.
- The Holm correction covers the four pre-registered tests only.
