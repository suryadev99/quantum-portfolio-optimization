# Architecture

```mermaid
flowchart LR
  A[F1 data + validation] --> B[F2 μ, Σ estimation]
  B --> C[F3 cluster + allocate K]
  C --> D[F4 QUBO per cluster / pick-count]
  D --> E[F5 sampler: QAOA | SA | exact]
  E -->|exactly re-scored candidate pools| F[F6 beam recombination + polish + neighbourhood]
  F --> G[F7 ε-band, diversity, CVXPY weights, ranking, audit]
  G --> H[outputs + manifest]
  F -.-> I[F8 classical baselines + benchmark + verdict]
```

| Package | Responsibility |
|---|---|
| `config` | strict pydantic schema (`extra=forbid`), YAML loader, secret-key rejection |
| `data`, `estimation` | ingestion, validation, synthetic generator, shrinkage μ, Ledoit-Wolf/OAS Σ, PSD repair |
| `clustering` | residual-correlation hierarchical/spectral clustering, picks-per-cluster allocation |
| `formulation` | QUBO build, exact penalty calibration, Ising conversion, brute force, pre-flight |
| `solvers` | `ExecBackend` (Aer / fake device / IBM Runtime), QAOA (+NumPy twin), SA (numba), exact |
| `recombination` | `ConstraintModel`, beam search over cluster pools, swap polish, neighbourhood expansion |
| `menu` | ε-band, farthest-point diversity, CVXPY weights, ranking, independent constraint audit |
| `baselines`, `benchmark`, `reporting` | six baselines, instances, ablations A4–A9, verdict, MD/HTML report |
| `manifest`, `cli` | reproducibility record, `qportfolio` command |

## Design principles

1. The device only proposes; every candidate is re-scored classically and exactly (`finalize_pool`).
2. No infeasible portfolio is emitted: cardinality repair → constraint gate in recombination → independent audit
   (`violates_hard_constraints`) in the menu.
3. Determinism: every random draw derives from `run.seed` through `utils.rng.derive_seed`.
4. Honest reporting: the verdict text is computed from measured results; a not-investment-advice disclaimer is always present.
5. Credentials come from the environment only.
