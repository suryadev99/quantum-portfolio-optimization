"""F2 — robust estimation of expected returns and covariance, plus input uncertainty."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.covariance import OAS, LedoitWolf

from qportfolio.config.schema import EstimationConfig
from qportfolio.utils.errors import DataError
from qportfolio.utils.linalg import nearest_psd, symmetrize
from qportfolio.utils.logging import get_logger

log = get_logger("estimation")


def estimate_covariance(returns: np.ndarray, method: str) -> np.ndarray:
    """Sample / Ledoit-Wolf / OAS covariance (F2-R1, F2-R2)."""
    if method == "sample":
        return np.cov(returns, rowvar=False)
    est = LedoitWolf() if method == "ledoit_wolf" else OAS()
    return est.fit(returns).covariance_


def estimate_mu(returns: np.ndarray, shrinkage: float) -> np.ndarray:
    """Sample mean shrunk toward the cross-sectional grand mean (F2-R6)."""
    m = returns.mean(axis=0)
    return (1.0 - shrinkage) * m + shrinkage * m.mean()


def bootstrap_objective_se(
    returns: np.ndarray, k: int, lam: float, cfg: EstimationConfig, n_boot: int = 30, seed: int = 0
) -> np.ndarray:
    """Per-asset standard error of the sample mean (analytic), used for the uncertainty ε-band."""
    t = returns.shape[0]
    se = returns.std(axis=0, ddof=1) / np.sqrt(t)
    if cfg.annualize:
        se = se * cfg.periods_per_year
    return se


def _read_vec_or_mat(path: str) -> np.ndarray:
    p = Path(path)
    if not p.is_file():
        raise DataError(f"estimation input not found: {p}")
    df = (
        pd.read_parquet(p)
        if p.suffix.lower() in {".parquet", ".pq"}
        else pd.read_csv(p, index_col=0)
    )
    return df.to_numpy(float)


def estimate_inputs(
    returns: pd.DataFrame, cfg: EstimationConfig
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """Return ``(mu, sigma, mu_se, info)``; honours user-supplied μ/Σ (F2-R4)."""
    r = returns.to_numpy(float)
    n = r.shape[1]
    scale = cfg.periods_per_year if cfg.annualize else 1.0
    info: dict = {"covariance": cfg.covariance, "annualized": cfg.annualize}

    if cfg.sigma_path:
        sigma = _read_vec_or_mat(cfg.sigma_path)
        if sigma.shape != (n, n):
            raise DataError(f"sigma_path shape {sigma.shape} != ({n},{n})")
        info["sigma_source"] = "user"
    else:
        sigma = estimate_covariance(r, cfg.covariance) * scale
        info["sigma_source"] = "estimated"

    if cfg.mu_path:
        mu = _read_vec_or_mat(cfg.mu_path).reshape(-1)
        if mu.shape[0] != n:
            raise DataError(f"mu_path length {mu.shape[0]} != {n}")
        info["mu_source"] = "user"
    else:
        mu = estimate_mu(r, cfg.mu_shrinkage) * scale
        info["mu_source"] = "estimated"

    sigma, repaired = nearest_psd(symmetrize(sigma))
    info["psd_repaired"] = repaired
    if repaired:
        log.warning("covariance was not PSD; repaired by eigenvalue clipping (F2-R3)")
    mu_se = bootstrap_objective_se(r, 1, 1.0, cfg)
    info["condition_number"] = float(np.linalg.cond(sigma))
    return mu, sigma, mu_se, info
