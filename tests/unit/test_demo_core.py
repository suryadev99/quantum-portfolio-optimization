"""Demo data layer: settings, audit, sampling study, honest verdict, worker-thread safety."""

from __future__ import annotations

import threading

import numpy as np
import pytest

from qportfolio import concepts
from qportfolio.analysis import (
    exact_reference,
    measure_pool,
    sampling_study,
    summarise,
    uniform_level,
)
from qportfolio.demo_core import (
    DEMO_HARD_STOP,
    DISCLAIMER,
    PRESETS,
    DemoSettings,
    audit_menu,
    compare_with_baselines,
    on_worker,
    pick_study_cluster,
    rank_mass,
    run_demo,
    run_sampling_study,
    verdict_for,
    warm_start_mass_table,
)
from qportfolio.solvers.controls import RandomFeasibleSolver
from tests.unit.test_formulation import _qubo


def test_settings_cap_cluster_size_at_the_demo_hard_stop():
    cfg = DemoSettings(max_cluster_size=99).to_config()
    assert cfg.decomposition.max_cluster_size == DEMO_HARD_STOP


@pytest.mark.parametrize("preset", list(PRESETS))
def test_every_preset_builds_a_valid_config(preset):
    cfg = DemoSettings(preset=preset).to_config()
    assert PRESETS[preset]["K"] == cfg.mandate.K
    assert cfg.data.source == "synthetic"
    assert cfg.solver.backend.type == "simulator"


def test_warm_start_is_only_applied_to_the_qaoa_sampler():
    assert DemoSettings(algorithm="sa", start="warm").to_config().solver.start == "cold"
    assert DemoSettings(algorithm="qaoa", start="warm").to_config().solver.start == "warm"


def test_demo_run_is_audited_and_clean():
    run = run_demo(DemoSettings())
    assert len(run.result.menu.portfolios) >= 1
    assert len(run.audit) == len(run.result.menu.portfolios)
    assert run.violations == 0


def test_sector_cap_is_enforced_in_the_demo_pipeline():
    s = DemoSettings(sector_cap=0.30)
    run = run_demo(s)
    u = run.result.universe
    for p in run.result.menu.portfolios:
        by = {}
        for i, aid in zip(p.selected_idx, p.selected_ids, strict=True):
            by[u.sectors[i]] = by.get(u.sectors[i], 0.0) + p.weights[aid]
        assert max(by.values()) <= 0.30 + 1e-6
    assert run.violations == 0


def test_audit_catches_a_tampered_portfolio():
    run = run_demo(DemoSettings(algorithm="random_feasible"))
    p = run.result.menu.portfolios[0]
    first = p.selected_ids[0]
    p.weights[first] = 0.9  # break the weight cap and the sum-to-one rule
    bad = audit_menu(run.result)[0]["violations"]
    assert any("weight" in b for b in bad)


def test_concepts_cover_every_page_topic_and_state_limits():
    for key in (
        "mandate",
        "decomposition",
        "mp",
        "modularity",
        "qubo",
        "qaoa",
        "warm",
        "controls",
        "recombination",
        "menu",
        "advantage",
    ):
        c = concepts.CONCEPTS[key]
        assert c["what"] and c["why"] and c["honest"]
    assert (
        "no such claim" in concepts.CONCEPTS["advantage"]["honest"].lower()
        or "no such claim" in concepts.CONCEPTS["advantage"]["honest"]
    )
    assert "advantage" in DISCLAIMER.lower()
    assert "not investment advice" in DISCLAIMER.lower()


def test_warm_start_mass_table_matches_the_closed_form():
    rows = {r["n"]: r for r in warm_start_mass_table()}
    assert rows[8]["eps=0.25"] == pytest.approx(0.75**8)
    assert rows[10]["eps=0.1"] == pytest.approx(0.9**10)
    assert rows[12]["eps=0.05"] == pytest.approx(0.95**12)


def test_sampling_study_measures_against_the_exact_optimum(universe):
    q = _qubo(universe, n=8, k=3)
    ref = exact_reference(q)
    assert len(ref.bits) == 56
    assert ref.best == pytest.approx(brute_best(q))
    from tests.conftest import make_cfg

    cfg = make_cfg(solver={"shots": 400}).solver
    rows = sampling_study(q, ["random_feasible", "round_relaxation"], cfg, seeds=2)
    assert len(rows) == 4
    for r in rows:
        assert r["best_gap"] >= -1e-12  # nothing can beat the exact optimum
        assert 0.0 <= r["top_q_recall"] <= 1.0
    s = summarise(rows)
    assert set(s) == {"random_feasible", "round_relaxation"}
    assert uniform_level(8, 3) == pytest.approx(1 / 56)


def brute_best(q):
    from qportfolio.formulation import brute_force

    return float(brute_force(q)[1][0])


def test_measure_pool_mass_is_a_probability(universe):
    from tests.conftest import make_cfg

    q = _qubo(universe, n=8, k=3)
    ref = exact_reference(q)
    pool = RandomFeasibleSolver(make_cfg(solver={"shots": 500}).solver, 1).solve(q, 0)
    m = measure_pool(q, pool, ref)
    assert 0.0 <= m["mass_on_best"] <= 1.0
    assert m["feasible_fraction"] == 1.0


def test_exact_reference_refuses_large_clusters(universe):
    q = _qubo(universe, n=8, k=3)
    q.asset_idx = np.arange(25)  # n_assets is derived from asset_idx
    with pytest.raises(ValueError, match="limited"):
        exact_reference(q)


def test_study_cluster_is_small_enough_for_an_exact_reference():
    q, meta = pick_study_cluster(DemoSettings())
    assert 0 < q.k < q.n_assets <= DEMO_HARD_STOP
    assert meta["states"] == 2**q.n_assets


def test_sampling_study_and_rank_mass_end_to_end_and_honest_verdict():
    s = DemoSettings()
    study = run_sampling_study(s, seeds=2)
    assert set(study["summary"]) == {
        "qaoa",
        "qaoa_warm",
        "round_relaxation",
        "random_feasible",
        "sa",
    }
    rm = rank_mass(s, top=5)
    assert all(len(v) == 5 for v in rm["mass"].values())
    assert all(0.0 <= x <= 1.0 for v in rm["mass"].values() for x in v)
    text = verdict_for(run_demo(s), study)
    assert "no quantum advantage is claimed or measured" in text
    assert "0 hard-constraint violations" in text


def test_compare_with_baselines_returns_gaps_relative_to_the_best_found():
    rows = compare_with_baselines(DemoSettings())
    assert rows[0]["method"].startswith("Best pipeline")
    gaps = [r["gap_to_best_found_pct"] for r in rows if r["gap_to_best_found_pct"] is not None]
    assert min(gaps) == pytest.approx(0.0)
    assert all(g >= -1e-9 for g in gaps)


def test_qiskit_work_survives_many_distinct_calling_threads():
    """Regression: Qiskit 2.6.1 segfaulted on a third distinct thread; on_worker must prevent it."""

    @on_worker
    def build():
        from qiskit import QuantumCircuit

        return QuantumCircuit(4).num_qubits

    out: list[int] = []

    def call():
        out.append(build())

    for _ in range(6):
        t = threading.Thread(target=call)
        t.start()
        t.join()
    assert out == [4] * 6


def test_on_worker_is_reentrant():
    @on_worker
    def inner():
        return 1

    @on_worker
    def outer():
        return inner() + 1

    assert outer() == 2
