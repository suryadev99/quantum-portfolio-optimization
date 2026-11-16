"""F4: QUBO construction, penalty calibration, Ising conversion (AC: matches brute force)."""

from __future__ import annotations

import itertools

import numpy as np
import pytest
from qiskit.quantum_info import Statevector

from qportfolio.config.schema import MandateConfig, QuboConfig
from qportfolio.formulation.qubo import (
    all_states,
    brute_force,
    build_qubo,
    calibrate_penalty,
    qubo_to_ising,
)
from qportfolio.types import Cluster
from qportfolio.utils.errors import ConfigError


def _qubo(universe, n=8, k=3, **q):
    cl = Cluster(id=0, asset_idx=np.arange(n), forced_in=np.zeros(0, dtype=int))
    return build_qubo(
        universe, cl, k, MandateConfig(K=6, lambda_risk=3.0, w_max=0.3), QuboConfig(**q)
    )


def test_energy_equals_objective_when_feasible(universe):
    qubo = _qubo(universe)
    for combo in itertools.combinations(range(qubo.n_assets), qubo.k):
        x = np.zeros(qubo.n_assets)
        x[list(combo)] = 1
        assert qubo.energy(x) == pytest.approx(qubo.objective(x), abs=1e-12)


def test_penalised_optimum_is_feasible_and_matches_brute_force(universe):
    qubo = _qubo(universe, n=10, k=4)
    bits = all_states(qubo.n_assets)
    e = qubo.energy(bits)
    best = bits[int(np.argmin(e))]
    assert best.sum() == qubo.k
    bf_bits, bf_obj = brute_force(qubo)
    assert qubo.objective(best) == pytest.approx(bf_obj[0], abs=1e-12)
    assert bf_bits[0].sum() == qubo.k


def test_penalty_calibration_methods(universe):
    q = _qubo(universe, n=8, k=3)
    assert q.info["penalty_method"] == "exact"
    big = _qubo(universe, n=20, k=3)
    assert big.info["penalty_method"] == "upper_bound"
    assert _qubo(universe, penalty_weight=7.5).penalty == 7.5
    p, _ = calibrate_penalty(np.diag([1.0, -1.0, 0.5, 0.2]), 0.0, 2)
    assert p > 0


def test_ising_matches_qubo_energy(universe):
    qubo = _qubo(universe, n=7, k=3)
    H, const = qubo_to_ising(qubo.Q, qubo.offset)
    mat = H.to_matrix(sparse=False).diagonal().real
    for idx in range(2**7):
        # qiskit little-endian: qubit i is bit i of the index
        x = np.array([(idx >> i) & 1 for i in range(7)], dtype=float)
        assert mat[idx] + const == pytest.approx(qubo.energy(x), abs=1e-10)


def test_impossible_k_raises(universe):
    cl = Cluster(id=0, asset_idx=np.arange(4), forced_in=np.zeros(0, dtype=int))
    with pytest.raises(ConfigError):
        build_qubo(universe, cl, 9, MandateConfig(K=6), QuboConfig())


def test_forced_in_reduces_free_picks(universe):
    cl = Cluster(id=0, asset_idx=np.arange(1, 8), forced_in=np.array([0]))
    q = build_qubo(universe, cl, 3, MandateConfig(K=6), QuboConfig())
    assert q.k == 2 and q.n_assets == 7
    assert isinstance(Statevector.from_label("0"), Statevector)  # qiskit import smoke
