# Mathematical formulation

Choose a set *S* of exactly *K* assets and weights *w* minimising

    f(w) = λ · wᵀΣw − μᵀw

subject to Σw = 1, 0 ≤ w ≤ w_max, sector caps, optional turnover, forced-in/out, ESG floor.

**Selection surrogate (QUBO).** With equal weights 1/K on the selected set, for binary *x*:

    E(x) = (λ/K²) xᵀΣx − (1/K) μᵀx + P (Σxᵢ − k_c)²

for each cluster *c* and pick count *k_c* (forced-in assets count toward *k_c* and enter as a linear term).

**Penalty calibration.** `calibrate_penalty` returns the smallest *P* for which the QUBO optimum is feasible:
exactly (`P ≥ max_{d≠0}(f*_k − f*_{k+d})/d²`, n ≤ 18) or via a rigorous per-flip bound otherwise.

**Ising.** x = (1 − Z)/2 gives H = Σ hᵢZᵢ + Σ Jᵢⱼ ZᵢZⱼ (+ const); verified against brute force and `qiskit_optimization`.

**QAOA.** Penalty encoding: |+⟩ⁿ start, RX mixer. XY encoding: Dicke(n, k) start, ring RXX+RYY mixer
(preserves Hamming weight). Training minimises CVaR_α of the energy distribution (statevector twin or sampler).

**Inequalities.** Default: handled in recombination (`recombination`), exact. Optional binary-slack encoding (`slack`).

**Recombination.** Beam search over the per-cluster candidate pools for each allocation of picks, gated by the
`ConstraintModel`, followed by single-swap polish and neighbourhood expansion; the final objective is always
the exact full-problem selection objective.

**Weights.** For each menu portfolio, the convex QP above is solved with CVXPY (CLARABEL) over the fixed selection.
