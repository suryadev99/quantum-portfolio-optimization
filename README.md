# qportfolio — quantum-sampled, cluster-decomposed portfolio selection

A hybrid engine for **cardinality-constrained portfolio selection** (pick *K* of *N* assets) on IBM Qiskit,
with a built-in, honest classical benchmark.

> **Disclaimer.** This is research software. It is **not investment advice**, makes no claim of profitability,
> and has not been validated on real market data or on real quantum hardware by its authors.

## The idea in one paragraph

Choosing *K* of *N* assets is a combinatorial (QUBO) problem. Instead of forcing it onto a small quantum device,
we (1) **cluster** correlated assets into sub-problems of ≤ ~12 qubits, (2) run **QAOA** (CVaR-trained, optional
XY-mixer/Dicke start) on each cluster to **sample many good candidate sub-selections**, (3) **re-score every
candidate classically and exactly** and **recombine** them with a constraint-aware beam search plus local polish,
and (4) turn the result into a **menu of near-optimal, diverse, constraint-feasible portfolios** with
convex-optimised weights. The device only *proposes*; classical code *decides*. No infeasible portfolio is ever emitted
(independent audit on every output).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

qportfolio validate configs/demo_small.yaml
qportfolio dry-run  configs/demo_1000.yaml      # qubits / depth / jobs / shots – submits nothing
qportfolio run      configs/demo_small.yaml     # local ideal simulator, ~5 s
qportfolio inspect  outputs/demo_small
qportfolio benchmark --instances S:40,C:60 --seeds 3 --ablations A4_clustering_input --out outputs/benchmark
```

Backends: `--backend simulator` (default) · `--backend fake_sherbrooke` (local noisy device model) ·
`--backend ibm_brisbane` / `--backend ibm` (real hardware, least busy). See
[`docs/runbooks/running_on_ibm_quantum.md`](docs/runbooks/running_on_ibm_quantum.md).
Algorithms: `--algorithm qaoa | sa | exact | random_feasible | round_relaxation | qaoa_warm`
(all registered in `qportfolio.solvers.registry`; add your own with `register_solver`).

## Interactive demo (the best pipeline, with its controls)

```bash
pip install -e ".[demo]"          # streamlit, plotly, networkx
./scripts/run_demo_app.sh         # http://127.0.0.1:8501 — localhost only, synthetic data
qportfolio run configs/demo_best.yaml   # the same pipeline from the command line
```

Four pages, each with a collapsible **Concept** panel (what it is, why it is here, honest limits):
1. **Business problem** — the mandate, the universe, the pipeline diagram.
2. **Decomposition** — Marchenko–Pastur spectrum against the noise edge λ₊, cluster topology, qubits before and after.
3. **Quantum sampling** — cold vs warm-start QAOA beside the no-quantum **rounded relaxation**, uniform random and
   simulated annealing, at equal shot budget, scored against the exact optimum.
4. **Portfolio menu and audit** — the audited menu, baselines, a computed verdict and a downloadable manifest.

The demo pipeline is: Marchenko–Pastur denoising (to find clusters only) → modularity clusters of ≤ 10–12 assets →
warm-start CVaR-QAOA per cluster → exact classical recombination → independent audit. This combination was chosen by
design; the structure study below did **not** show it beats the defaults. New config options:
`decomposition.denoise | clusterer: modularity | risk_rebalance`, `solver.start: cold | warm`, `solver.warm_epsilon`.
Defaults reproduce the previous behaviour exactly.

>**Measured, not claimed.** Over 44 cluster sub-problems (40 and 80 assets, 3 seeds each), warm start put a median
>0.75 of its shots on the exact optimum against 0.34 for cold start, but the **no-quantum rounded relaxation put
>0.63**, and warm start beat it in 41 of 44. So most of the gain over cold start comes from the classical relaxation.
>After repair and recombination every sampler I ran, including uniform random, reached the same best objective on the reference configs. No quantum
>advantage is claimed or measured; see the verdict on page 4. Design: [`docs/FEATURE_SPEC_COMBINED.md`](docs/FEATURE_SPEC_COMBINED.md).

>**Structure study (pre-registered, 108 synthetic instances, exact sub-solver, 7 arms).** Marchenko–Pastur denoising,
>modularity clustering and risk rebalancing each gave **no practically meaningful change** in objective loss versus the
>defaults (97–100% of paired instances identical; all three hypotheses *refuted* by the pre-registered rule, which here
>means "no difference measured", not "harmful"). Modularity and MP left a *larger* share of correlation between
>clusters (+0.02 to +0.04). The correctness gate **failed**: 46 of 756 runs returned no portfolio, all on the
>turnover-constrained family. In the one case traced, all 1,050 candidates failed the exact turnover check, and the
>QUBO has no turnover term. No infeasible portfolio was ever emitted (0 audit violations).
>Full report: [`benchmarks/results/structure_study_report.md`](benchmarks/results/structure_study_report.md).

## Python API

```python
from qportfolio.config.loader import load_config
from qportfolio.pipeline import run_pipeline

res = run_pipeline(load_config("configs/demo_small.yaml"))
for p in res.menu.portfolios:
    print(p.rank, p.objective, p.selected_ids)
```

## Outputs of `qportfolio run`

`menu.json` / `menu.csv` (portfolios, weights, stats, constraint slack, explanations) · `decomposition.json` ·
`solver_report.json` (qubits, depth, shots, jobs, QPU seconds) · `samples.parquet` · `manifest.json`
(config + hash, seed, data hash, package versions, git commit, timings).

## What the benchmark says (measured, simulator only)

On the reference instances the quantum pipeline **matches but does not beat** the classical baselines on solution
quality or diversity, and is one to two orders of magnitude slower on a simulator. An ablation also found that
clustering on market-mode-removed (residual) correlation did **not** reduce the cut share versus raw correlation.
The report's verdict is computed from the numbers, and states "no measured quantum advantage" when that is the case.
Value of the platform: a validated hybrid method, decomposition/recombination machinery, and a benchmark that
will detect an advantage if larger clusters, deeper circuits or better hardware ever produce one.

## Security

Credentials are **never** read from config or code. Use `QISKIT_IBM_TOKEN`, `QISKIT_IBM_INSTANCE`,
`QISKIT_IBM_CHANNEL`, or an account saved with `QiskitRuntimeService.save_account`. Config files containing
keys such as `token`, `api_key`, `password` are rejected at load time.

## Development

```bash
ruff check . && ruff format --check .
pytest --cov=qportfolio          # unit + property + integration (hardware tests excluded)
pytest -m hardware               # opt-in; needs QISKIT_IBM_TOKEN
```

Docs: [`docs/architecture.md`](docs/architecture.md) · [`docs/formulation.md`](docs/formulation.md) ·
[`docs/benchmarking.md`](docs/benchmarking.md) · [`docs/glossary.md`](docs/glossary.md) ·
[`docs/runbooks/adding_a_new_solver.md`](docs/runbooks/adding_a_new_solver.md) · full spec in [`docs/FEATURE_SPEC.md`](docs/FEATURE_SPEC.md).

License: Apache-2.0.
