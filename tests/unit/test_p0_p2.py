"""P0–P2: registry, MP denoising, modularity clustering, risk rebalancing, warm start, controls."""

from __future__ import annotations

import numpy as np
import pytest
from qiskit.quantum_info import Statevector, state_fidelity

from qportfolio.clustering.clusterers import (
    _modularity,
    marchenko_pastur_edge,
    mp_denoised_correlation,
    risk_rebalance_factor,
)
from qportfolio.config.loader import config_from_dict
from qportfolio.pipeline import run_pipeline
from qportfolio.solvers import available_solvers, get_spec, make_solver
from qportfolio.solvers.base import Solver
from qportfolio.solvers.controls import RandomFeasibleSolver, RoundRelaxationSolver
from qportfolio.solvers.qaoa import build_qaoa_circuit
from qportfolio.solvers.qaoa_sim import QAOASim, qubo_to_ising_arrays, warm_state
from qportfolio.solvers.registry import SolverSpec, register_solver
from qportfolio.solvers.relaxation import clip_start, solve_relaxation, start_mass_on_rounded
from qportfolio.utils.errors import ConfigError
from qportfolio.utils.linalg import to_correlation
from tests.conftest import make_cfg
from tests.unit.test_formulation import _qubo

# ----------------------------------------------------------------------------- registry


def test_registry_lists_builtins_and_declares_metadata():
    names = available_solvers()
    for n in ("qaoa", "qaoa_warm", "sa", "exact", "random_feasible", "round_relaxation"):
        assert n in names
        spec = get_spec(n)
        assert spec.kind in {"quantum", "classical", "control"}
        assert spec.cost
    assert get_spec("random_feasible").kind == "control"


def test_unknown_algorithm_rejected_at_config_validation():
    with pytest.raises(ConfigError, match="unknown solver algorithm"):
        make_cfg(solver={"algorithm": "does_not_exist"})


def test_new_algorithm_needs_no_pipeline_change(cfg):
    class First(Solver):
        name = "first_k"

        def solve(self, qubo, seed):
            from qportfolio.solvers.base import finalize_pool

            bits = np.zeros((1, qubo.n_assets), dtype=np.int8)
            bits[0, : qubo.k] = 1
            return finalize_pool(
                qubo, bits, np.ones(1, dtype=np.int64), 5, True, {"solver": "first_k"}
            )

    register_solver(
        SolverSpec("first_k", lambda c, s: First(), "test", "classical", None, "O(1)"), True
    )
    res = run_pipeline(make_cfg(solver={"algorithm": "first_k"}))
    assert len(res.menu.portfolios) >= 1


def test_existing_algorithms_still_build():
    cfg = make_cfg().solver
    for algo in ("sa", "exact", "qaoa"):
        s = make_solver(cfg, 1, algo)
        assert s.name == algo
        s.close()


# ----------------------------------------------------------------------------- endianness


def test_bitstring_endianness_matches_qiskit_for_asymmetric_state():
    """Variable i must be qubit i: an asymmetric product state exposes any bit-order mix-up."""
    start = np.array([0.9, 0.1, 0.1, 0.1])
    from qportfolio.solvers.backends import counts_to_bits

    qc, _, _ = build_qaoa_circuit(np.zeros(4), np.zeros((4, 4)), 1, "penalty", 2, False, start)
    bound = qc.assign_parameters(dict.fromkeys(qc.parameters, 0.0))
    sv = Statevector(bound)
    sv.seed(1)
    counts = sv.sample_counts(20000)
    bits, w = counts_to_bits({k: int(v) for k, v in counts.items()}, 4)
    freq = (bits * w[:, None]).sum(axis=0) / w.sum()
    assert freq[0] == pytest.approx(0.9, abs=0.02)
    assert freq[1:] == pytest.approx([0.1, 0.1, 0.1], abs=0.02)


# ----------------------------------------------------------------------------- warm start


def test_relaxation_is_feasible_and_bounded(universe):
    q = _qubo(universe, n=8, k=3)
    c, info = solve_relaxation(q)
    assert c.shape == (8,)
    assert c.sum() == pytest.approx(3, abs=1e-5)
    assert (c >= -1e-9).all()
    assert (c <= 1 + 1e-9).all()
    assert info["relaxation_status"].startswith("optimal")


def test_relaxation_lower_bounds_the_exact_optimum(universe):
    from qportfolio.formulation import brute_force

    q = _qubo(universe, n=8, k=3)
    c, _ = solve_relaxation(q)
    # the relaxation minimises c'(quad)c + lin'c (NOT c'obj_Q c: lin sits on obj_Q's diagonal,
    # which equals the linear term only for binary x since x_i^2 = x_i)
    relaxed = float(c @ q.info["obj_quad"] @ c + q.info["obj_lin"] @ c)
    _, best = brute_force(q)
    assert relaxed <= best[0] + 1e-9


def test_clip_and_start_mass_formula():
    c = np.array([0.0, 1.0, 0.5, 0.3])
    clipped = clip_start(c, 0.25)
    assert clipped.min() == pytest.approx(0.25)
    assert clipped.max() == pytest.approx(0.75)
    # (1-eps)^n for a fully saturated start
    assert start_mass_on_rounded(np.array([0.25, 0.75] * 4)) == pytest.approx(0.75**8)
    assert start_mass_on_rounded(np.full(6, 0.5)) == pytest.approx(0.5**6)


@pytest.mark.parametrize("p", [1, 2])
def test_warm_numpy_twin_matches_qiskit_statevector(p):
    rng = np.random.default_rng(4)
    n = 5
    Q = rng.normal(size=(n, n))
    h, J = qubo_to_ising_arrays((Q + Q.T) / 2)
    start = rng.uniform(0.2, 0.8, n)
    qc, gam, bet = build_qaoa_circuit(h, J, p, "penalty", 2, measure=False, start=start)
    g, b = rng.normal(size=p), rng.normal(size=p)
    bound = qc.assign_parameters(
        {**dict(zip(gam, g, strict=True)), **dict(zip(bet, b, strict=True))}
    )
    sim = QAOASim(h, J, "penalty", 2, n, start)
    assert state_fidelity(Statevector(bound), Statevector(sim.state(g, b))) == pytest.approx(
        1.0, abs=1e-9
    )


def test_warm_start_with_epsilon_half_is_the_cold_start_distribution():
    n = 4
    s = warm_state(np.full(n, 0.5))
    assert np.allclose(np.abs(s) ** 2, 1 / 2**n)


def test_warm_start_with_xy_encoding_is_rejected():
    with pytest.raises(ConfigError, match="Hamming weight"):
        make_cfg(solver={"start": "warm", "encoding": "xy"})
    with pytest.raises(ValueError):
        QAOASim(np.zeros(3), np.zeros((3, 3)), "xy", 1, 3, np.full(3, 0.5))


def test_warm_pipeline_records_start_info_and_stays_feasible():
    res = run_pipeline(make_cfg(solver={"start": "warm", "warm_epsilon": 0.1}))
    infos = [p.solver_info for p in res.pools.values() if not p.solver_info.get("trivial")]
    assert infos
    assert all(i["start"] == "warm" for i in infos)
    assert all(0 < i["start_mass_on_rounded"] <= 1 for i in infos)
    assert len(res.menu.portfolios) >= 1
    assert all(len(p.selected_ids) == res.config.mandate.K for p in res.menu.portfolios)


# ----------------------------------------------------------------------------- controls


def test_controls_return_feasible_exactly_rescored_pools(universe):
    q = _qubo(universe, n=8, k=3)
    cfg = make_cfg(solver={"shots": 400}).solver
    for solver in (RandomFeasibleSolver(cfg, 1), RoundRelaxationSolver(cfg, 1)):
        pool = solver.solve(q, 0)
        assert len(pool) > 0
        assert (pool.bits.sum(axis=1) == q.k).all()
        assert np.allclose(pool.objective, q.objective(pool.bits.astype(float)))
        assert pool.solver_info["control"] is True


def test_controls_are_deterministic_for_a_fixed_seed(universe):
    q = _qubo(universe, n=8, k=3)
    cfg = make_cfg(solver={"shots": 300}).solver
    a = RandomFeasibleSolver(cfg, 5).solve(q, 0)
    b = RandomFeasibleSolver(cfg, 5).solve(q, 0)
    assert np.array_equal(a.bits, b.bits)


def test_round_relaxation_includes_the_rounded_relaxation(universe):
    q = _qubo(universe, n=8, k=3)
    c, _ = solve_relaxation(q)
    rounded = np.zeros(8, dtype=np.int8)
    rounded[np.argsort(-c, kind="stable")[:3]] = 1
    pool = RoundRelaxationSolver(make_cfg(solver={"shots": 50}).solver, 1).solve(q, 0)
    assert any(np.array_equal(row, rounded) for row in pool.bits)


@pytest.mark.parametrize("algo", ["random_feasible", "round_relaxation", "qaoa_warm"])
def test_registered_algorithms_never_emit_infeasible_portfolios(algo):
    res = run_pipeline(make_cfg(solver={"algorithm": algo, "shots": 300, "max_iterations": 8}))
    assert len(res.menu.portfolios) >= 1
    for p in res.menu.portfolios:
        assert len(p.selected_ids) == res.config.mandate.K
        assert p.weights is not None
        assert sum(p.weights.values()) == pytest.approx(1.0, abs=1e-6)
        assert max(p.weights.values()) <= res.config.mandate.w_max + 1e-6


# ----------------------------------------------------------------------------- MP and modularity


def _planted(n_blocks=3, size=8, t=400, seed=0):
    rng = np.random.default_rng(seed)
    n = n_blocks * size
    factors = rng.normal(size=(t, n_blocks))
    load = np.zeros((n, n_blocks))
    for b in range(n_blocks):
        load[b * size : (b + 1) * size, b] = 1.0
    market = rng.normal(size=(t, 1))
    x = factors @ load.T * 0.9 + market * 0.6 + rng.normal(size=(t, n))
    return np.cov(x, rowvar=False), t


def test_mp_edge_formula():
    assert marchenko_pastur_edge(20, 252) == pytest.approx(1.64, abs=0.01)
    assert marchenko_pastur_edge(60, 252) == pytest.approx(2.21, abs=0.01)
    assert marchenko_pastur_edge(100, 252) == pytest.approx(2.66, abs=0.01)


def test_mp_denoising_keeps_planted_structure_and_reports_it():
    sigma, t = _planted()
    cleaned, info = mp_denoised_correlation(sigma, t)
    assert info["mp_modes_kept"] >= 2
    assert info["mp_lambda_plus"] == pytest.approx(marchenko_pastur_edge(24, t))
    assert np.allclose(np.diag(cleaned), 1.0)
    assert np.allclose(cleaned, cleaned.T)
    assert info["mp_fallback_to_market_mode"] == 0.0


def test_mp_falls_back_to_market_mode_on_pure_noise():
    rng = np.random.default_rng(1)
    sigma = np.cov(rng.normal(size=(120, 40)), rowvar=False)
    cleaned, info = mp_denoised_correlation(sigma, 120)
    assert cleaned.shape == (40, 40)
    assert "mp_modes_kept" in info


def test_modularity_recovers_planted_blocks_and_respects_the_cap():
    sigma, t = _planted(n_blocks=3, size=6)
    corr, _ = mp_denoised_correlation(sigma, t)
    groups = _modularity(corr, max_size=6)
    assert max(len(g) for g in groups) <= 6
    assert sorted(np.concatenate(groups).tolist()) == list(range(18))
    block_of = np.arange(18) // 6
    purity = np.mean([np.bincount(block_of[g]).max() / len(g) for g in groups])
    assert purity >= 0.8


@pytest.mark.parametrize("order", ["fifo", "size"])
def test_modularity_orders_partition_everything(order):
    sigma, _ = _planted(n_blocks=4, size=7)
    groups = _modularity(to_correlation(sigma), 9, order)
    assert max(len(g) for g in groups) <= 9
    assert sorted(np.concatenate(groups).tolist()) == list(range(28))


def test_risk_rebalance_factor_is_one_for_a_single_cluster(universe):
    f = risk_rebalance_factor(universe, [np.arange(universe.n)])
    assert f == pytest.approx(1.0)


def test_modularity_mp_rebalance_pipeline_is_feasible_and_reports_diagnostics():
    cfg = make_cfg(
        decomposition={
            "clusterer": "modularity",
            "denoise": "marchenko_pastur",
            "risk_rebalance": "norm_ratio",
            "max_cluster_size": 7,
        },
        solver={"algorithm": "sa"},
    )
    res = run_pipeline(cfg)
    s = res.cluster_summary
    assert s["max_size"] <= 7
    assert s["compression_ratio"] >= 1.0
    assert s["lam_scale"] > 0
    assert "mp_lambda_plus" in s
    assert len(res.menu.portfolios) >= 1


def test_defaults_reproduce_the_previous_behaviour():
    a = run_pipeline(make_cfg(solver={"algorithm": "sa"}))
    b = run_pipeline(
        make_cfg(
            solver={"algorithm": "sa", "start": "cold"},
            decomposition={"denoise": "market_mode", "risk_rebalance": "none"},
        )
    )
    assert a.best_objective == pytest.approx(b.best_objective)
    assert a.cluster_summary["lam_scale"] == 1.0


def test_config_from_dict_roundtrip_includes_new_fields():
    cfg = config_from_dict({"mandate": {"K": 5}})
    dumped = cfg.model_dump()
    assert dumped["decomposition"]["denoise"] == "market_mode"
    assert dumped["solver"]["start"] == "cold"
    assert dumped["solver"]["warm_epsilon"] == 0.25
