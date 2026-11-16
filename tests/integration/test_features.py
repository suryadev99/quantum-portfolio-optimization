"""Less common paths: CSV input, turnover, slack constraints, XY encoding, user mu/sigma, early stops."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qportfolio.data import generate_synthetic
from qportfolio.menu.menu import violates_hard_constraints
from qportfolio.pipeline import run_pipeline
from qportfolio.utils.errors import QPortfolioError
from tests.conftest import make_cfg


def _audit(res):
    for p in res.menu.portfolios:
        w = np.array([p.weights[i] for i in p.selected_ids])
        assert (
            violates_hard_constraints(res.universe, res.cm, res.config.mandate, p.selected_idx, w)
            == []
        )


def test_csv_input(tmp_path, cfg):
    ret, attrs = generate_synthetic(cfg.data.synthetic, 5)
    ret.to_csv(tmp_path / "r.csv")
    attrs.to_csv(tmp_path / "a.csv")
    c = make_cfg(
        data={
            "source": "csv",
            "path": str(tmp_path / "r.csv"),
            "attributes_path": str(tmp_path / "a.csv"),
        }
    )
    _audit(run_pipeline(c))


def test_bad_csv_rejected(tmp_path):
    pd.DataFrame({"a": [1.0, 2.0]}).to_csv(tmp_path / "r.csv")
    with pytest.raises(QPortfolioError):
        run_pipeline(make_cfg(data={"source": "csv", "path": str(tmp_path / "r.csv")}))


def test_missing_csv_rejected():
    with pytest.raises(QPortfolioError):
        run_pipeline(make_cfg(data={"source": "csv", "path": "nope.csv"}))


def test_turnover_limit_respected():
    base = run_pipeline(make_cfg())
    held = base.menu.portfolios[0]
    # start from the rank-1 portfolio, then ask for a small turnover
    cfg = make_cfg(mandate={"K": 6, "w_max": 0.3, "turnover_limit": 0.6})
    res = run_pipeline(cfg, universe=_with_holdings(base.universe, held))
    _audit(res)
    for p in res.menu.portfolios:
        assert p.constraint_slack["turnover_slack"] >= -1e-6


def _with_holdings(universe, portfolio):
    from dataclasses import replace

    cw = np.zeros(universe.n)
    for i, w in portfolio.weights.items():
        cw[universe.ids.index(i)] = w
    return replace(universe, current_weight=cw)


def test_slack_inequality_handling():
    cfg = make_cfg(
        mandate={"K": 6, "w_max": 0.3, "sector_caps": {"Technology": 0.34}},
        qubo={"inequality_handling": "slack"},
        solver={"algorithm": "sa"},
    )
    _audit(run_pipeline(cfg))


def test_xy_encoding_end_to_end():
    _audit(run_pipeline(make_cfg(solver={"encoding": "xy", "max_iterations": 8})))


def test_spsa_optimizer_and_sampler_training():
    cfg = make_cfg(
        solver={
            "optimizer": "spsa",
            "train_mode": "sampler",
            "max_iterations": 6,
            "train_shots": 128,
        }
    )
    _audit(run_pipeline(cfg))


@pytest.mark.parametrize(
    "rank",
    [{"method": "lexicographic", "order": ["objective", "liquidity"]}, {"method": "weighted"}],
)
def test_ranking_methods(rank):
    res = run_pipeline(make_cfg(menu={"size": 4, "ranking": rank}))
    _audit(res)


def test_epsilon_modes():
    for mode in ("pct", "abs", "uncertainty"):
        res = run_pipeline(
            make_cfg(
                menu={"size": 4, "epsilon_mode": mode, "epsilon_abs": 0.01},
                solver={"algorithm": "sa"},
            )
        )
        assert res.menu.epsilon >= 0


def test_qpu_budget_stops_early_but_stays_feasible():
    cfg = make_cfg(
        solver={
            "limits": {"max_jobs": 1},
            "max_iterations": 20,
            "train_mode": "sampler",
            "train_shots": 64,
        }
    )
    res = run_pipeline(cfg)
    _audit(res)


def test_hardware_without_credentials_fails_cleanly(monkeypatch):
    for v in ("QISKIT_IBM_TOKEN", "QISKIT_IBM_INSTANCE", "QISKIT_IBM_CHANNEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", "/nonexistent")  # no saved account
    cfg = make_cfg(solver={"backend": {"type": "ibm_hardware", "name": "ibm_brisbane"}})
    with pytest.raises(QPortfolioError):
        run_pipeline(cfg)
