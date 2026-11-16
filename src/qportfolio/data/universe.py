"""Build a validated :class:`Universe` from configuration (F1 + F2 glue)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qportfolio.config.schema import Config
from qportfolio.data.loader import (
    build_attributes,
    load_returns_and_attributes,
    validate_mandate,
    validate_returns,
)
from qportfolio.estimation.estimators import estimate_inputs
from qportfolio.types import Universe
from qportfolio.utils.logging import get_logger

log = get_logger("universe")


def universe_from_returns(
    returns: pd.DataFrame, attrs: pd.DataFrame | None, cfg: Config
) -> Universe:
    returns = validate_returns(returns, cfg.data)
    ids, sectors, liq, esg, cw = build_attributes(returns, attrs)
    validate_mandate(cfg.mandate, ids, sectors, esg)
    mu, sigma, mu_se, info = estimate_inputs(returns, cfg.estimation)
    return Universe(
        ids=ids,
        mu=mu,
        sigma=sigma,
        sectors=sectors,
        liquidity=liq,
        esg=esg,
        current_weight=cw,
        mu_se=mu_se,
        metadata={"n_days": len(returns), **info},
    )


def build_universe(cfg: Config) -> Universe:
    returns, attrs = load_returns_and_attributes(cfg.data, cfg.run.seed)
    return universe_from_returns(returns, attrs, cfg)


def universe_from_arrays(
    mu: np.ndarray,
    sigma: np.ndarray,
    sectors: list[str] | None = None,
    ids: list[str] | None = None,
    liquidity: np.ndarray | None = None,
    esg: np.ndarray | None = None,
    current_weight: np.ndarray | None = None,
) -> Universe:
    """Direct construction from precomputed μ and Σ (tests, benchmarks, notebooks)."""
    n = len(mu)
    return Universe(
        ids=ids or [f"A{i:04d}" for i in range(n)],
        mu=np.asarray(mu, float),
        sigma=np.asarray(sigma, float),
        sectors=np.array(sectors if sectors is not None else ["ALL"] * n, dtype=object),
        liquidity=np.ones(n) if liquidity is None else np.asarray(liquidity, float),
        esg=np.full(n, np.nan) if esg is None else np.asarray(esg, float),
        current_weight=np.zeros(n) if current_weight is None else np.asarray(current_weight, float),
        mu_se=None,
        metadata={},
    )
