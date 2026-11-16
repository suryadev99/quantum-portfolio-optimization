# Adding a new solver

1. Subclass `qportfolio.solvers.base.Solver` and implement `solve(qubo, seed) -> CandidatePool`.
2. Generate candidate bit-arrays, then **always** return `finalize_pool(qubo, bits, counts, ...)` – it repairs cardinality,
   de-duplicates and re-scores exactly. Use `trivial_pool` for degenerate sub-problems.
3. Register it in `solvers/factory.py::make_solver`, add the name to `SolverConfig.algorithm`, and (for timing/usage)
   override `usage()`.
4. Tests: add the solver to the parametrised tests in `tests/unit/test_misc.py::test_classical_solvers_agree_with_brute_force`
   and `tests/integration/test_pipeline.py`.
5. Benchmark it by running the pipeline with it (`qportfolio run cfg.yaml --algorithm <name>`) and compare against the baselines in `qportfolio.baselines`.
