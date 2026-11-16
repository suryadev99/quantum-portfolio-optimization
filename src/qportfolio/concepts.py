"""Plain-English concept notes shown beside every demo page, and a glossary.

Each concept states what it is, why it is here, and what the evidence does and does not show.
Kept in a separate module so the text is reviewable and testable without launching the UI.
"""

from __future__ import annotations

CONCEPTS: dict[str, dict[str, str]] = {
    "mandate": {
        "title": "The mandate: pick K assets under rules",
        "what": "Choose exactly K assets from N, then weight them, so risk-adjusted return is as good "
        "as possible while obeying hard rules: a weight cap per asset, optional sector caps, "
        "optional minimum ESG, fully invested.",
        "why": "Choosing which K of N is a combinatorial problem: C(40,10) is already 847 million "
        "combinations. Weights, once the K are chosen, are an easy convex problem.",
        "honest": "At the sizes shown here an exact classical solver is faster than any quantum "
        "method. The demo says so on screen rather than hiding it.",
    },
    "decomposition": {
        "title": "Decomposition: break one big problem into small ones",
        "what": "Group assets that move together into clusters of at most 10 to 12. Solve each cluster "
        "as its own small problem, then recombine exactly.",
        "why": "A quantum register needs one qubit per asset. 150 assets would need 150 qubits; "
        "clusters of 10 need only 10, which a simulator can handle.",
        "honest": "Splitting ignores the covariance between clusters (the 'cut share'). That loss is "
        "repaired at recombination, which always scores with the full covariance. The "
        "JPMorgan/Amazon paper reports results close to the direct optimum, but with classical "
        "solvers (Gurobi, CPLEX), not quantum ones.",
    },
    "mp": {
        "title": "Marchenko–Pastur (MP) denoising",
        "what": "Random matrix theory says how large the eigenvalues of a correlation matrix can get "
        "from pure noise: up to λ₊ = (1 + √(N/T))². Eigenvalues below that edge are noise.",
        "why": "With few observations T relative to assets N, most of a sample correlation matrix is "
        "noise. Clustering on the noise gives unstable groups, so we cluster on the structure "
        "above the edge, after removing the market mode (the single dominant eigenvalue).",
        "honest": "The cleaned matrix is used ONLY to find clusters. Scoring and the audit always use "
        "the original covariance. This is a classical technique; it is not quantum.",
    },
    "modularity": {
        "title": "Modularity bisection",
        "what": "Repeatedly split a group in two using the leading eigenvector of the modularity "
        "matrix, which favours splits that keep strongly correlated assets together.",
        "why": "It gives communities with a hard size ceiling, so every cluster fits the qubit budget.",
        "honest": "Measured (pre-registered ablation A15, 108 synthetic instances, exact sub-solver): "
        "modularity did NOT give a lower objective loss than hierarchical or spectral clustering, and "
        "it left a larger share of correlation between clusters. It is offered as an option, not as "
        "an improvement.",
    },
    "qubo": {
        "title": "QUBO: the problem a quantum computer is given",
        "what": "Quadratic Unconstrained Binary Optimisation. Each asset is a bit (1 = held). Risk is "
        "the pairwise term, return is the linear term, and 'exactly k held' is enforced with a "
        "penalty that is calibrated so an infeasible state can never beat a feasible one.",
        "why": "It is the standard form quantum optimisers accept, and it maps onto an Ising "
        "Hamiltonian that QAOA acts on.",
        "honest": "The penalty can distort the landscape. The demo reports how much of the raw "
        "sampled mass is feasible before repair.",
    },
    "qaoa": {
        "title": "QAOA with a CVaR objective",
        "what": "A shallow circuit that alternates a cost phase and a mixing step, p times. The angles "
        "are trained so the best α fraction of samples (CVaR, α = 0.25) has low energy.",
        "why": "We do not need the average sample to be good; we need the best few to be excellent. "
        "CVaR trains for exactly that.",
        "honest": "The circuit only PROPOSES candidates. Every one is re-scored exactly on a classical "
        "machine, so noise can reduce usefulness but never correctness.",
    },
    "warm": {
        "title": "Warm start",
        "what": "First solve an easy continuous version of the problem classically (weights allowed "
        "between 0 and 1). Then start each qubit biased towards that answer instead of an even "
        "50/50 blend.",
        "why": "Starting nearer a good region can help a shallow circuit.",
        "honest": "Starting closer to one answer leaves less for the quantum steps to add. The "
        "probability on the single rounded answer is (1−ε)ⁿ, which is small for larger clusters "
        "(see the table on the sampling page). The right control is the rounded relaxation with "
        "NO quantum step; the demo runs it beside warm start.",
    },
    "controls": {
        "title": "Controls: what a fair test needs",
        "what": "Uniform random feasible sampling and the rounded relaxation, both at the same sample "
        "budget as the quantum sampler, scored by the same exact re-scoring.",
        "why": "A quantum sampler is only interesting if it beats these. The IBM multi-objective "
        "tutorial itself found QAOA about equal to uniform random sampling at 40 assets.",
        "honest": "Where the final portfolio is identical across every sampler, the quantum step "
        "contributed nothing to the answer. That is a legitimate result and the demo shows it.",
    },
    "recombination": {
        "title": "Recombination and audit",
        "what": "Candidates from each cluster are combined by beam search under the full covariance, "
        "then polished with swaps. Weights come from a convex solve. An independent audit "
        "re-checks every reported portfolio against every hard constraint.",
        "why": "Constraints that span clusters (sector caps, weight caps) cannot be seen by one "
        "cluster, so they are enforced here.",
        "honest": "No infeasible portfolio is ever emitted. If the audit finds a violation it is "
        "shown, not hidden.",
    },
    "menu": {
        "title": "A menu, not one 'best' portfolio",
        "what": "Many portfolios are statistically indistinguishable in objective given noisy inputs. "
        "The menu lists several near-optimal, deliberately different portfolios that a human "
        "can rank by liquidity, ESG, turnover or concentration.",
        "why": "Choosing a single winner from within the estimation noise is an illusion of precision.",
        "honest": "Diversity in the menu comes mostly from decomposition and recombination, which "
        "classical pipelines share.",
    },
    "advantage": {
        "title": "What 'quantum advantage' would require",
        "what": "Better quality, or less time or cost, than the best classical method at equal "
        "budget, on a problem size where classical methods struggle.",
        "why": "It is the only claim that would justify a quantum approach commercially.",
        "honest": "This demo makes no such claim and measures none. At 10 to 12 qubits an exact solver "
        "is instant. The value shown is the validated hybrid method and an honest benchmark that "
        "would detect an advantage if one ever appeared.",
    },
}

GLOSSARY: dict[str, str] = {
    "QUBO": "Quadratic Unconstrained Binary Optimisation: minimise xᵀQx over bits x.",
    "Ising": "The physics form of a QUBO with spins ±1; what quantum circuits act on.",
    "QAOA": "Quantum Approximate Optimisation Algorithm: alternating cost and mixer layers.",
    "CVaR": "Conditional Value at Risk: the mean of the best α fraction; here, of sampled energies.",
    "Cluster": "A group of correlated assets solved as one small sub-problem.",
    "Cut share": "Share of absolute covariance lying between clusters, which splitting ignores.",
    "Compression ratio": "Qubits needed without decomposition divided by the largest cluster.",
    "Market mode": "The single dominant eigenvector of a correlation matrix: 'everything moves together'.",
    "λ₊": "The Marchenko–Pastur upper edge: eigenvalues below it are indistinguishable from noise.",
    "Warm start": "Biasing the initial quantum state towards a classical relaxed solution.",
    "ε (epsilon)": "Warm-start clip: relaxed values are clipped to [ε, 1−ε]; ε = 0.5 is the cold start.",
    "Mass on best": "Share of shots that landed on the exact optimal selection.",
    "Top-q recall": "Share of the exact top-q selections present in the returned pool.",
    "Repair": "Greedy fix-up of a sampled bitstring to exactly k ones, using exact objective changes.",
    "Beam search": "Keeps the best partial combinations while stepping through clusters.",
    "ε-band": "Objective range around the best within which portfolios are treated as equivalent.",
    "Audit": "Independent re-check of constraints on every reported portfolio.",
}

PIPELINE_DOT = """
digraph G {
  rankdir=LR; node [shape=box, style="rounded", fontsize=11]; edge [fontsize=9];
  U [label="Universe\\n(synthetic or real)"];
  D [label="MP denoise\\n(find clusters only)"];
  C [label="Modularity clusters\\n(<= 10-12 assets)"];
  Q [label="One QUBO per cluster"];
  S [label="Sampler\\n(quantum or control)"];
  R [label="Exact re-score\\n+ repair"];
  B [label="Beam recombination\\nfull covariance"];
  M [label="Menu + weights"];
  A [label="Independent audit", style="rounded,filled", fillcolor="#e8f0fb"];
  U -> D -> C -> Q -> S -> R -> B -> M -> A;
}
"""
