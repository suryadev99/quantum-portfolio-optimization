"""Numerical helpers shared across stages."""

from __future__ import annotations

import numpy as np


def symmetrize(a: np.ndarray) -> np.ndarray:
    return (a + a.T) / 2.0


def nearest_psd(sigma: np.ndarray, floor: float = 1e-10) -> tuple[np.ndarray, bool]:
    """Return a symmetric PSD matrix close to ``sigma`` (eigenvalue clipping).

    Returns the repaired matrix and a flag telling whether a repair was needed (F2-R3).
    """
    s = symmetrize(np.asarray(sigma, dtype=float))
    w, v = np.linalg.eigh(s)
    if w.min() >= -1e-12 * max(1.0, abs(w).max()):
        return s, False
    w = np.clip(w, floor, None)
    return symmetrize((v * w) @ v.T), True


def is_psd(sigma: np.ndarray, tol: float = 1e-9) -> bool:
    w = np.linalg.eigvalsh(symmetrize(sigma))
    return bool(w.min() >= -tol * max(1.0, abs(w).max()))


def to_correlation(sigma: np.ndarray) -> np.ndarray:
    d = np.sqrt(np.clip(np.diag(sigma), 1e-18, None))
    c = sigma / np.outer(d, d)
    np.fill_diagonal(c, 1.0)
    return np.clip(symmetrize(c), -1.0, 1.0)


def selection_objective(
    sigma: np.ndarray, mu: np.ndarray, selected: np.ndarray, lam: float, k: int
) -> float:
    """Equal-weight selection objective ``lam/K^2 s'Σs - (1/K) mu's`` (spec §10.1)."""
    idx = np.asarray(selected, dtype=int)
    if idx.size == 0:
        return 0.0
    sub = sigma[np.ix_(idx, idx)]
    return float(lam / k**2 * sub.sum() - mu[idx].sum() / k)


def selection_objective_batch(
    sigma: np.ndarray, mu: np.ndarray, indicators: np.ndarray, lam: float, k: int
) -> np.ndarray:
    """Vectorised objective for a (B, N) 0/1 indicator matrix."""
    x = np.asarray(indicators, dtype=float)
    quad = np.einsum("bi,ij,bj->b", x, sigma, x, optimize=True)
    return lam / k**2 * quad - (x @ mu) / k


def jaccard_distance(a: np.ndarray, b: np.ndarray) -> float:
    sa, sb = set(np.asarray(a).tolist()), set(np.asarray(b).tolist())
    union = len(sa | sb)
    return 0.0 if union == 0 else 1.0 - len(sa & sb) / union


def pairwise_jaccard(indicators: np.ndarray) -> np.ndarray:
    """Pairwise Jaccard distance matrix for a (B, N) 0/1 indicator matrix."""
    x = np.asarray(indicators, dtype=float)
    inter = x @ x.T
    sizes = x.sum(axis=1)
    union = sizes[:, None] + sizes[None, :] - inter
    with np.errstate(divide="ignore", invalid="ignore"):
        d = 1.0 - np.where(union > 0, inter / union, 1.0)
    np.fill_diagonal(d, 0.0)
    return d


def pairwise_hamming(indicators: np.ndarray) -> np.ndarray:
    x = np.asarray(indicators, dtype=float)
    return (x.sum(1)[:, None] + x.sum(1)[None, :] - 2 * (x @ x.T)) / max(1, x.shape[1])


def indicators_from_selections(selections: list[np.ndarray], n: int) -> np.ndarray:
    out = np.zeros((len(selections), n), dtype=np.int8)
    for r, s in enumerate(selections):
        out[r, np.asarray(s, dtype=int)] = 1
    return out
