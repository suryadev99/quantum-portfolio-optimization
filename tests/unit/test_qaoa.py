"""F5: Dicke circuit, NumPy QAOA twin vs. Qiskit statevector, repair, pool finalisation."""

from __future__ import annotations

import numpy as np
import pytest
from qiskit.quantum_info import Statevector, state_fidelity

from qportfolio.solvers.base import repair_to_cardinality
from qportfolio.solvers.qaoa import build_qaoa_circuit, dicke_circuit
from qportfolio.solvers.qaoa_sim import QAOASim, dicke_state, qubo_to_ising_arrays


@pytest.mark.parametrize(("n", "k"), [(4, 2), (6, 3), (7, 1), (8, 4), (5, 5)])
def test_dicke_circuit_is_uniform_on_weight_k(n, k):
    probs = Statevector(dicke_circuit(n, k)).probabilities()
    weights = np.array([bin(i).count("1") for i in range(2**n)])
    assert probs[weights != k].sum() == pytest.approx(0, abs=1e-10)
    on = probs[weights == k]
    assert np.allclose(on, on[0], atol=1e-10)


def _random_ising(n, seed):
    rng = np.random.default_rng(seed)
    Q = rng.normal(size=(n, n))
    return qubo_to_ising_arrays((Q + Q.T) / 2)


@pytest.mark.parametrize("encoding", ["penalty", "xy"])
def test_numpy_twin_matches_qiskit_statevector(encoding):
    n, k, p = 5, 2, 2
    h, J = _random_ising(n, 1)
    qc, gam, bet = build_qaoa_circuit(h, J, p, encoding, k, measure=False)
    g, b = np.array([0.3, -0.2]), np.array([0.5, 0.1])
    bound = qc.assign_parameters(
        {**dict(zip(gam, g, strict=True)), **dict(zip(bet, b, strict=True))}
    )
    ref = Statevector(bound)
    sim = QAOASim(h, J, encoding, k, n)
    assert state_fidelity(ref, Statevector(sim.state(g, b))) == pytest.approx(1.0, abs=1e-9)


def test_cvar_bounds():
    h, J = _random_ising(5, 2)
    sim = QAOASim(h, J, "penalty", 2, 5)
    probs = sim.probs(np.array([0.2]), np.array([0.4]))
    assert sim.cvar(probs, 1.0) == pytest.approx(float(probs @ sim.diag))
    assert sim.cvar(probs, 0.1) <= sim.cvar(probs, 1.0) + 1e-12
    assert sim.cvar(probs, 0.1) >= sim.diag.min() - 1e-12


def test_dicke_state_normalised():
    s = dicke_state(6, 3)
    assert np.linalg.norm(s) == pytest.approx(1.0)


@pytest.mark.parametrize("start", [0, 2, 5])
def test_repair_reaches_exact_cardinality(universe, start):
    from tests.unit.test_formulation import _qubo

    q = _qubo(universe, n=8, k=3)
    bits = np.zeros(8, dtype=np.int8)
    bits[:start] = 1
    fixed = repair_to_cardinality(q, bits)
    assert fixed.sum() == q.k
