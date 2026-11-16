"""Hypothesis properties: whatever the inputs, emitted portfolios are feasible; Σ stays PSD."""

from __future__ import annotations

import numpy as np
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from qportfolio.menu.menu import violates_hard_constraints
from qportfolio.pipeline import run_pipeline
from qportfolio.utils.linalg import is_psd, nearest_psd
from tests.conftest import make_cfg

_SETTINGS = settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@_SETTINGS
@given(
    seed=st.integers(0, 10_000), k=st.integers(4, 9), algo=st.sampled_from(["sa", "exact", "qaoa"])
)
def test_every_emitted_portfolio_has_k_assets_and_passes_audit(seed, k, algo):
    cfg = make_cfg(
        run={"seed": seed},
        mandate={"K": k, "w_max": max(0.2, 1.5 / k)},
        solver={"algorithm": algo, "max_iterations": 6, "shots": 256},
    )
    res = run_pipeline(cfg)
    assert len(res.menu.portfolios) >= 1
    sels = set()
    for p in res.menu.portfolios:
        w = np.array([p.weights[i] for i in p.selected_ids])
        assert len(set(p.selected_ids)) == k
        assert violates_hard_constraints(res.universe, res.cm, cfg.mandate, p.selected_idx, w) == []
        assert abs(w.sum() - 1) < 1e-6
        sels.add(tuple(sorted(p.selected_idx.tolist())))
    assert len(sels) == len(res.menu.portfolios)  # no duplicates in the menu


@_SETTINGS
@given(st.integers(2, 12), st.integers(0, 10_000))
def test_nearest_psd_always_psd(n, seed):
    a = np.random.default_rng(seed).normal(size=(n, n))
    s = (a + a.T) / 2
    fixed, _ = nearest_psd(s)
    assert is_psd(fixed)
