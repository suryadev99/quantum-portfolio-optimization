# Benchmarking

`qportfolio benchmark` runs the quantum pipeline and the baselines on seeded instances and writes
`report.md/html`, `runs.json`, `findings.json`, `manifest.json`, `plots/`.

**Families:** `S` (simple), `C` (sector caps + turnover), `H` (highly correlated). Reference set: `reference_instances()`.

**Baselines:** `equal_weight`, `greedy`, `miqp_timelimit` (SCIP, time-limited – reports its gap), `sa_qubo`,
`decomposed_classical` (same pipeline, exact sub-solves), `multistart_classical`.

**Metrics:** optimality gap vs. best known (exact where N ≤ 22), in-band count, band Jaccard diversity,
decomposition loss, runtime, audit violations, infeasible pools.

**Ablations:** A4 clustering input · A5 training objective · A6 encoding · A7 noise/mitigation · A8 cluster size ·
A9 coupling thinning (`--ablations A4_clustering_input,...` or `all`).

**Verdict.** `reporting.verdict` maps measured numbers to statuses (`supported | refuted | inconclusive | not_assessed`).
Hardware claims are `not_assessed` unless real-device runs are present. Classical post-processing is shared by every method,
so its contribution is never credited to the quantum sampler.
