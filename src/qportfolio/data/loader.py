"""Loading and validation of return histories, attributes and mandates (F1)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from qportfolio.config.schema import Config, DataConfig, MandateConfig
from qportfolio.data.synthetic import generate_synthetic
from qportfolio.utils.errors import DataError, InfeasibleError
from qportfolio.utils.logging import get_logger

log = get_logger("data")


def _read_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.is_file():
        raise DataError(f"Data file not found: {p}")
    suffix = p.suffix.lower()
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(p)
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(p, index_col=0, parse_dates=True)
    raise DataError(f"Unsupported file type {suffix!r} (use CSV or Parquet)")


def apply_missing_policy(
    returns: pd.DataFrame, policy: str, max_missing_frac: float
) -> pd.DataFrame:
    """Handle NaNs per configured policy (F1-R2)."""
    miss = returns.isna().mean()
    if policy == "reject":
        if miss.max() > 0:
            raise DataError(
                f"{int((miss > 0).sum())} assets contain missing values (policy=reject)"
            )
        return returns
    too_many = miss[miss > max_missing_frac].index
    if len(too_many):
        log.warning(
            "dropping %d assets with >%.0f%% missing", len(too_many), 100 * max_missing_frac
        )
        returns = returns.drop(columns=too_many)
    if policy == "forward_fill":
        returns = returns.ffill().bfill()
    # remaining NaN rows are dropped (policy == 'drop', or leftovers)
    return returns.dropna(axis=0, how="any")


def load_returns_and_attributes(
    cfg: DataConfig, seed: int
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    if cfg.source == "synthetic":
        return generate_synthetic(cfg.synthetic, seed)
    if cfg.path is None:
        raise DataError("data.path is required for csv/parquet sources")
    returns = _read_table(cfg.path)
    attrs = None
    if cfg.attributes_path:
        attrs = _read_table(cfg.attributes_path)
        if "asset" in attrs.columns:
            attrs = attrs.set_index("asset")
    return returns, attrs


def validate_returns(returns: pd.DataFrame, cfg: DataConfig) -> pd.DataFrame:
    """Schema checks, missing-value policy and history-length filtering (F1-R1..R3)."""
    if returns.empty:
        raise DataError("empty return history")
    if not all(np.issubdtype(t, np.number) for t in returns.dtypes):
        raise DataError("return history must be fully numeric")
    if returns.columns.duplicated().any():
        raise DataError("duplicate asset identifiers in return history")
    returns = apply_missing_policy(returns, cfg.missing_policy, cfg.max_missing_frac)
    if len(returns) < cfg.min_history_days:
        raise DataError(
            f"only {len(returns)} usable rows, need min_history_days={cfg.min_history_days}"
        )
    if not np.isfinite(returns.to_numpy()).all():
        raise DataError("non-finite values in return history")
    const = returns.columns[returns.std(axis=0) < 1e-12]
    if len(const):
        log.warning("dropping %d constant (zero-variance) assets", len(const))
        returns = returns.drop(columns=const)
    return returns


def build_attributes(
    returns: pd.DataFrame, attrs: pd.DataFrame | None
) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    ids = [str(c) for c in returns.columns]
    n = len(ids)
    if attrs is None:
        return ids, np.full(n, "ALL", dtype=object), np.ones(n), np.full(n, np.nan), np.zeros(n)
    attrs = attrs.copy()
    attrs.index = attrs.index.astype(str)
    missing = [a for a in ids if a not in attrs.index]
    if missing:
        raise DataError(f"{len(missing)} assets lack attributes, e.g. {missing[:3]}")
    attrs = attrs.loc[ids]
    sectors = attrs["sector"].astype(str).to_numpy() if "sector" in attrs else np.full(n, "ALL")
    liq = attrs["liquidity"].to_numpy(float) if "liquidity" in attrs else np.ones(n)
    esg = attrs["esg"].to_numpy(float) if "esg" in attrs else np.full(n, np.nan)
    cw = attrs["current_weight"].to_numpy(float) if "current_weight" in attrs else np.zeros(n)
    return ids, sectors.astype(object), liq, esg, np.nan_to_num(cw, nan=0.0)


def validate_mandate(
    mandate: MandateConfig,
    ids: list[str],
    sectors: np.ndarray,
    esg: np.ndarray,
) -> None:
    """Reject inconsistent constraints at load time (F1-R6)."""
    n = len(ids)
    idset = set(ids)
    unknown = [a for a in [*mandate.forced_in, *mandate.forced_out] if a not in idset]
    if unknown:
        raise InfeasibleError(f"forced_in/out reference unknown assets: {unknown[:5]}")
    both = set(mandate.forced_in) & set(mandate.forced_out)
    if both:
        raise InfeasibleError(f"assets both forced in and out: {sorted(both)[:5]}")

    forced_out = set(mandate.forced_out)
    eligible = np.array([a not in forced_out for a in ids])
    if mandate.min_esg is not None and not np.isnan(esg).all():
        eligible &= np.nan_to_num(esg, nan=-np.inf) >= mandate.min_esg
        for a in mandate.forced_in:
            if not eligible[ids.index(a)]:
                raise InfeasibleError(f"forced-in asset {a} violates min_esg")
    n_elig = int(eligible.sum())
    if n_elig < mandate.K:
        raise InfeasibleError(f"K={mandate.K} exceeds eligible assets ({n_elig} of {n})")
    if len(mandate.forced_in) > mandate.K:
        raise InfeasibleError("more forced_in assets than K")
    if mandate.w_max * mandate.K < 1.0 - 1e-9:
        raise InfeasibleError(
            f"w_max={mandate.w_max} too small: K={mandate.K} picks cannot reach 100% "
            f"(need w_max >= {1.0 / mandate.K:.4f})"
        )

    known = set(sectors.tolist())
    for s in mandate.sector_caps:
        if s not in known:
            raise InfeasibleError(f"sector cap given for unknown sector {s!r}")
    # capacity: total weight placeable given caps and w_max must reach 100%
    capacity = 0.0
    for s in known:
        members = int(((sectors == s) & eligible).sum())
        cap = mandate.sector_caps.get(s, 1.0)
        capacity += min(cap, members * mandate.w_max)
    if capacity < 1.0 - 1e-9:
        raise InfeasibleError(
            f"sector caps/w_max leave only {capacity:.2%} placeable capacity (< 100%)"
        )


def eligible_mask(mandate: MandateConfig, ids: list[str], esg: np.ndarray) -> np.ndarray:
    forced_out = set(mandate.forced_out)
    m = np.array([a not in forced_out for a in ids])
    if mandate.min_esg is not None and not np.isnan(esg).all():
        m &= np.nan_to_num(esg, nan=-np.inf) >= mandate.min_esg
    return m


__all__ = [
    "Config",
    "apply_missing_policy",
    "build_attributes",
    "eligible_mask",
    "load_returns_and_attributes",
    "validate_mandate",
    "validate_returns",
]
