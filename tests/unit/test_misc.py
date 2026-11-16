"""Config, secrets, RNG, linear algebra, metrics, clustering, constraints, classical solvers."""

from __future__ import annotations

import numpy as np
import pytest

from qportfolio.baselines import run_baseline
from qportfolio.benchmark.metrics import (
    band_diversity,
    count_in_band,
    decomposition_loss_pct,
    optimality_gap_pct,
)
from qportfolio.clustering.clusterers import build_clusters, residual_correlation
from qportfolio.config.loader import config_from_dict, deep_update, load_config
from qportfolio.config.schema import DecompositionConfig
from qportfolio.data import eligible_mask
from qportfolio.recombination.constraints import ConstraintModel
from qportfolio.solvers.classical import ExactSolver, SASolver
from qportfolio.utils.errors import ConfigError
from qportfolio.utils.linalg import is_psd, jaccard_distance, nearest_psd, pairwise_jaccard
from qportfolio.utils.rng import derive_seed, make_rng
from tests.conftest import make_cfg
from tests.unit.test_formulation import _qubo


def test_rng_is_deterministic_and_salted():
    assert derive_seed(1, "a") == derive_seed(1, "a")
    assert derive_seed(1, "a") != derive_seed(1, "b")
    assert make_rng(5, 1).integers(1 << 30) == make_rng(5, 1).integers(1 << 30)


@pytest.mark.parametrize("key", ["token", "api_key", "password"])
def test_secret_keys_rejected(key):
    with pytest.raises(ConfigError, match="Credentials"):
        config_from_dict({"mandate": {"K": 3}, "solver": {"backend": {key: "x"}}})


def test_unknown_key_and_bad_values_rejected():
    with pytest.raises(ConfigError):
        config_from_dict({"mandate": {"K": 3}, "bogus": 1})
    with pytest.raises(ConfigError):
        config_from_dict({"mandate": {"K": 0}})
    with pytest.raises(ConfigError):
        config_from_dict({"mandate": {"K": 3, "sector_caps": {"X": 1.5}}})
    with pytest.raises(ConfigError):
        load_config("does/not/exist.yaml")


def test_deep_update_does_not_mutate():
    a = {"x": {"y": 1, "z": 2}}
    b = deep_update(a, {"x": {"y": 9}})
    assert a["x"]["y"] == 1 and b["x"] == {"y": 9, "z": 2}


@pytest.mark.parametrize("name", ["demo_small", "demo_1000", "benchmark_full"])
def test_shipped_configs_validate(name):
    from pathlib import Path

    import yaml

    raw = yaml.safe_load((Path(__file__).parents[2] / "configs" / f"{name}.yaml").read_text())
    if "mandate" not in raw:
        raw["mandate"] = {"K": 5}
    config_from_dict(raw)


def test_psd_repair():
    s = np.array([[1.0, 2.0], [2.0, 1.0]])
    fixed, changed = nearest_psd(s)
    assert changed and is_psd(fixed)


def test_jaccard():
    a, b = np.array([1, 1, 0, 0]), np.array([0, 1, 1, 0])
    assert jaccard_distance(np.flatnonzero(a), np.flatnonzero(b)) == pytest.approx(1 - 1 / 3)
    d = pairwise_jaccard(np.array([a, b, a]))
    assert d[0, 2] == 0 and d[0, 1] == pytest.approx(2 / 3)


def test_metrics():
    assert optimality_gap_pct(-0.9, -1.0) == pytest.approx(10.0)
    assert optimality_gap_pct(None, -1.0) is None
    assert decomposition_loss_pct(-0.99, -1.0) == pytest.approx(1.0)
    obj = np.array([-1.0, -0.99, -0.5])
    assert count_in_band(obj, -1.0, 2.0) == 2
    ind = np.array([[1, 1, 0], [1, 0, 1], [0, 1, 1]])
    out = band_diversity(ind, obj, -1.0, 100.0)
    assert out["n_band"] == 3 and out["mean_jaccard"] > 0


def test_residual_correlation_is_valid(universe):
    c = residual_correlation(universe.sigma, 1)
    assert np.allclose(np.diag(c), 1.0) and np.allclose(c, c.T)


def test_clusters_partition_and_respect_size(cfg, universe):
    elig = eligible_mask(cfg.mandate, universe.ids, universe.esg)
    clusters, alloc, _, _ = build_clusters(universe, elig, cfg.mandate, cfg.decomposition, 1)
    ids = np.concatenate([c.asset_idx for c in clusters])
    assert sorted(ids.tolist()) == sorted(np.flatnonzero(elig).tolist())
    assert max(c.size for c in clusters) <= cfg.decomposition.max_cluster_size
    for a in alloc.all():
        assert a.sum() == cfg.mandate.K
        assert all(a[i] <= clusters[i].size for i in range(len(clusters)))


@pytest.mark.parametrize("clusterer", ["hierarchical", "spectral"])
def test_clusterers_run(cfg, universe, clusterer):
    elig = eligible_mask(cfg.mandate, universe.ids, universe.esg)
    d = DecompositionConfig(clusterer=clusterer, max_cluster_size=6)
    clusters, *_ = build_clusters(universe, elig, cfg.mandate, d, 1)
    assert max(c.size for c in clusters) <= 6


def test_constraint_model_gate(cfg, universe):
    elig = eligible_mask(cfg.mandate, universe.ids, universe.esg)
    cm = ConstraintModel.build(universe, cfg.mandate, elig)
    ok = np.zeros((1, universe.n), dtype=np.int8)
    ok[0, : cfg.mandate.K] = 1
    assert cm.structural_ok(ok).shape == (1,)


@pytest.mark.parametrize("solver_cls", [ExactSolver, SASolver])
def test_classical_solvers_agree_with_brute_force(cfg, universe, solver_cls):
    from qportfolio.formulation.qubo import brute_force

    q = _qubo(universe, n=10, k=4)
    pool = solver_cls(cfg.solver, 1).solve(q, 1)
    _, ref = brute_force(q)
    assert pool.objective[0] == pytest.approx(ref[0], abs=1e-9)
    assert (pool.bits.sum(axis=1) == q.k).all()
    assert np.all(np.diff(pool.objective) >= -1e-15)


@pytest.mark.parametrize(
    "name", ["equal_weight", "greedy", "multistart_classical", "sa_qubo", "decomposed_classical"]
)
def test_baselines_are_feasible(cfg, universe, name):
    from qportfolio.menu.menu import violates_hard_constraints

    elig = eligible_mask(cfg.mandate, universe.ids, universe.esg)
    cm = ConstraintModel.build(universe, cfg.mandate, elig)
    res = run_baseline(name, universe, cm, make_cfg(), 1)
    assert res.menu is not None and res.menu.portfolios
    for p in res.menu.portfolios:
        w = np.array([p.weights[i] for i in p.selected_ids])
        assert violates_hard_constraints(universe, cm, cfg.mandate, p.selected_idx, w) == []
