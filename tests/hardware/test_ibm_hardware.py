"""Opt-in tests against real IBM Quantum hardware. Run with: pytest -m hardware (needs credentials)."""

from __future__ import annotations

import os

import pytest

from qportfolio.pipeline import run_pipeline
from tests.conftest import make_cfg

pytestmark = pytest.mark.hardware


@pytest.mark.skipif(not os.environ.get("QISKIT_IBM_TOKEN"), reason="QISKIT_IBM_TOKEN not set")
def test_tiny_hardware_run_is_feasible():
    cfg = make_cfg(
        data={"synthetic": {"n_assets": 10, "n_sectors": 2, "n_factors": 2, "n_days": 300}},
        mandate={"K": 3, "w_max": 0.5},
        solver={
            "backend": {"type": "ibm_hardware", "name": None, "mode": "batch"},
            "max_iterations": 5,
            "shots": 1024,
            "limits": {"max_qpu_seconds": 60, "max_jobs": 20},
            "mitigation": {"enabled": True},
        },
    )
    res = run_pipeline(cfg)
    assert res.menu.portfolios
