"""Seeded synthetic factor-model market generator (F1-R4).

Returns follow ``r = F β' + ε`` with a market factor, sector factors and idiosyncratic noise, so the
true covariance structure (and its cluster structure) is known — ideal for controlled experiments.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from qportfolio.config.schema import SyntheticConfig
from qportfolio.utils.rng import make_rng

_DEFAULT_SECTORS = [
    "Technology",
    "Energy",
    "Financials",
    "Health",
    "Industrials",
    "Utilities",
    "Materials",
    "Consumer",
    "Telecom",
    "RealEstate",
    "Staples",
    "Transport",
]


def sector_names(n_sectors: int, names: list[str] | None = None) -> list[str]:
    if names:
        return list(names)[:n_sectors] + [f"Sector{i}" for i in range(len(names), n_sectors)]
    base = list(_DEFAULT_SECTORS)
    return [base[i] if i < len(base) else f"Sector{i}" for i in range(n_sectors)]


def generate_synthetic(cfg: SyntheticConfig, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(returns[T,N], attributes[N, ...])`` as DataFrames. Fully determined by ``seed``."""
    rng = make_rng(seed, "synthetic")
    n, s, f, t = cfg.n_assets, cfg.n_sectors, cfg.n_factors, cfg.n_days
    names = sector_names(s, cfg.sector_names)

    sector_of = np.sort(rng.integers(0, s, size=n)) if n >= s else np.arange(n) % s
    # make sure every sector appears at least once for n >= s
    if n >= s:
        sector_of[:s] = np.arange(s)
        sector_of = rng.permutation(sector_of)

    # factor loadings: market (all positive), style factors (mixed), sector factor (own sector)
    beta_m = rng.uniform(0.6, 1.4, size=n)
    beta_style = rng.normal(0.0, 0.4, size=(n, max(f - 1, 0)))
    beta_sec = rng.uniform(0.6, 1.2, size=n)

    vol_m = 0.010
    vol_style = 0.006
    vol_sec = 0.008
    vol_idio = rng.uniform(0.008, 0.016, size=n) * cfg.noise_level

    market = rng.normal(0.0, vol_m, size=t)
    styles = rng.normal(0.0, vol_style, size=(t, max(f - 1, 0)))
    sec_f = rng.normal(0.0, vol_sec, size=(t, s))
    idio = rng.normal(0.0, 1.0, size=(t, n)) * vol_idio

    rets = np.outer(market, beta_m)
    if styles.shape[1]:
        rets += styles @ beta_style.T
    rets += sec_f[:, sector_of] * beta_sec
    rets += idio

    # true (annual) expected returns: modest, partly tied to risk, heterogeneous
    total_vol = rets.std(axis=0)
    alpha_ann = 0.03 + 0.5 * total_vol * np.sqrt(252) * rng.uniform(0.2, 0.9, size=n)
    alpha_ann += rng.normal(0.0, 0.02, size=n)
    rets += alpha_ann / 252.0

    ids = [f"A{i:04d}" for i in range(n)]
    dates = pd.bdate_range(end="2024-12-31", periods=t)
    returns = pd.DataFrame(rets, index=dates, columns=ids)

    liq = np.clip(rng.lognormal(mean=0.0, sigma=0.7, size=n), 0.05, None)
    liq = liq / liq.max()
    esg = np.clip(rng.normal(55, 18, size=n), 0, 100)
    cw = np.zeros(n)
    held = rng.choice(n, size=min(n, max(1, n // 20)), replace=False)
    cw[held] = 1.0 / len(held)
    attrs = pd.DataFrame(
        {
            "asset": ids,
            "sector": [names[i] for i in sector_of],
            "liquidity": liq,
            "esg": esg,
            "current_weight": cw,
        }
    ).set_index("asset")
    return returns, attrs
