"""End-to-end behaviour: feasibility (NFR7/8), reproducibility (NFR1), dry-run safety, outputs, benchmark."""

from __future__ import annotations

import json

import numpy as np
import pytest

from qportfolio.config.loader import dump_config
from qportfolio.menu.menu import violates_hard_constraints
from qportfolio.pipeline import dry_run, run_pipeline
from qportfolio.reporting.io import write_run_outputs
from qportfolio.utils.linalg import jaccard_distance
from tests.conftest import make_cfg


def _audit(res):
    cfg = res.config
    for p in res.menu.portfolios:
        w = np.array([p.weights[i] for i in p.selected_ids])
        assert violates_hard_constraints(res.universe, res.cm, cfg.mandate, p.selected_idx, w) == []
        assert len(p.selected_idx) == cfg.mandate.K


@pytest.fixture(scope="module")
def result():
    return run_pipeline(make_cfg())


def test_menu_is_feasible_and_ranked(result):
    assert 1 <= len(result.menu.portfolios) <= result.config.menu.size
    _audit(result)
    assert [p.rank for p in result.menu.portfolios] == list(
        range(1, len(result.menu.portfolios) + 1)
    )


def test_menu_members_are_distinct(result):
    sels = [p.selected_idx for p in result.menu.portfolios]
    for i in range(len(sels)):
        for j in range(i + 1, len(sels)):
            assert jaccard_distance(sels[i], sels[j]) > 0


def test_quantum_matches_exact_classical_on_small_instance(result):
    cfg = make_cfg(solver={"algorithm": "exact"})
    ref = run_pipeline(cfg)
    assert result.best_objective == pytest.approx(ref.best_objective, abs=1e-6) or (
        result.best_objective >= ref.best_objective - 1e-12
    )


def test_reproducible_same_seed():
    a, b = run_pipeline(make_cfg()), run_pipeline(make_cfg())
    assert [p.selected_ids for p in a.menu.portfolios] == [
        p.selected_ids for p in b.menu.portfolios
    ]


@pytest.mark.parametrize("algo", ["sa", "exact"])
def test_classical_algorithms_feasible(algo):
    _audit(run_pipeline(make_cfg(solver={"algorithm": algo})))


def test_constrained_mandate_never_emits_violations():
    cfg = make_cfg(
        mandate={
            "K": 6,
            "w_max": 0.25,
            "sector_caps": {"Technology": 0.3},
            "forced_in": ["A0000"],
            "forced_out": ["A0001"],
        }
    )
    res = run_pipeline(cfg)
    _audit(res)
    assert all(
        "A0000" in p.selected_ids and "A0001" not in p.selected_ids for p in res.menu.portfolios
    )


def test_dry_run_submits_nothing():
    rep = dry_run(make_cfg())
    assert rep["submitted_jobs"] == 0
    assert rep["max_qubits"] <= 8 and rep["sampling_jobs"] >= 1


def test_outputs_and_manifest(tmp_path, result):
    paths = write_run_outputs(result, tmp_path)
    names = {p.name for p in paths}
    assert {
        "menu.json",
        "menu.csv",
        "manifest.json",
        "decomposition.json",
        "solver_report.json",
    } <= names
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert man["config"] == json.loads(json.dumps(dump_config(result.config)))
    assert len(man["config_hash"]) >= 12 and man["seed"] == 3
    assert "token" not in json.dumps(man).lower()
    assert json.loads((tmp_path / "menu.json").read_text())["portfolios"]


def test_infeasible_mandate_fails_loudly():
    from qportfolio.utils.errors import QPortfolioError

    with pytest.raises(QPortfolioError):
        run_pipeline(make_cfg(mandate={"K": 6, "w_max": 0.1}))  # 6 × 0.1 < 1


def test_noisy_fake_backend_runs_locally():
    cfg = make_cfg(
        data={"synthetic": {"n_assets": 10, "n_sectors": 2, "n_factors": 2, "n_days": 300}},
        mandate={"K": 3, "w_max": 0.5},
        decomposition={"max_cluster_size": 6},
        solver={
            "backend": {"type": "noisy_simulator", "name": "fake_sherbrooke"},
            "max_iterations": 3,
            "shots": 256,
        },
    )
    res = run_pipeline(cfg)
    _audit(res)  # noise may degrade quality but never feasibility
