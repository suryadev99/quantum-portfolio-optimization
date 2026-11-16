"""Shared fixtures: tiny, fast, deterministic problem instances."""

from __future__ import annotations

import numpy as np
import pytest

from qportfolio.config.loader import config_from_dict, deep_update
from qportfolio.data import build_universe
from qportfolio.types import Cluster


def make_cfg(**over):
    raw = {
        "run": {"seed": 3},
        "data": {
            "synthetic": {"n_assets": 24, "n_sectors": 4, "n_factors": 2, "n_days": 400},
            "min_history_days": 250,
        },
        "mandate": {"K": 6, "lambda_risk": 3.0, "w_max": 0.3},
        "decomposition": {"max_cluster_size": 8},
        "solver": {
            "algorithm": "qaoa",
            "p_layers": 1,
            "max_iterations": 15,
            "shots": 512,
            "top_candidates_per_cluster": 20,
        },
        "recombination": {"beam_width": 60, "top_r": 100},
        "menu": {"size": 5},
    }
    return config_from_dict(deep_update(raw, over))


@pytest.fixture(scope="session")
def cfg():
    return make_cfg()


@pytest.fixture(scope="session")
def universe(cfg):
    return build_universe(cfg)


@pytest.fixture()
def one_cluster(universe):
    return Cluster(id=0, asset_idx=np.arange(8), forced_in=np.zeros(0, dtype=int))
