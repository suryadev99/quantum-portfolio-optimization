# Running on IBM Quantum

> The hardware path is implemented with Qiskit Runtime (`Batch`, `SamplerV2`) and tested with local simulators
> and fake devices only. Treat the first hardware run as an experiment.

1. **Credentials (never in config files).**
   ```bash
   export QISKIT_IBM_TOKEN=...          # or: QiskitRuntimeService.save_account(...)
   export QISKIT_IBM_INSTANCE=...       # optional
   export QISKIT_IBM_CHANNEL=ibm_quantum_platform
   ```
2. **Rehearse offline:** `qportfolio run cfg.yaml --backend fake_sherbrooke` (noise model, no credentials).
3. **Pre-flight:** `qportfolio dry-run cfg.yaml --backend ibm_brisbane` – qubits, transpiled depth, 2q gates, jobs, shots. Nothing is submitted.
4. **Budgets:** set `solver.limits.max_qpu_seconds` and `max_jobs`; when exhausted the run stops early, keeps what it has and
   reports `stopped early` (still feasible – classical re-scoring).
5. **Run:** `qportfolio run cfg.yaml --backend ibm_brisbane` (or `--backend ibm` for the least busy device).
   Enable `solver.mitigation.enabled: true` for dynamical decoupling + twirling.
6. **Audit:** `solver_report.json` and `manifest.json` hold job ids, usage, backend name and package versions.
   Keep clusters ≤ ~12 qubits; use `thin_couplings` and `p_layers: 1–2` to limit depth.
