"""F3 — clustering on residual correlation and allocation of picks per cluster.

Key research-driven step (RESEARCH_RATIONALE §6.3): the dominant *market mode* makes nearly all
equity pairs positively correlated, which blurs clusters. We remove the leading eigen-components of
the correlation matrix and cluster on the *residual* correlation.
"""

from __future__ import annotations

import numpy as np
from scipy.cluster.hierarchy import linkage, to_tree
from scipy.sparse.linalg import eigsh
from scipy.spatial.distance import squareform

from qportfolio.config.schema import DecompositionConfig, MandateConfig
from qportfolio.types import Cluster, KAllocation, Universe
from qportfolio.utils.errors import InfeasibleError
from qportfolio.utils.linalg import to_correlation
from qportfolio.utils.logging import get_logger

log = get_logger("clustering")


# ----------------------------------------------------------------------------- residual corr


def residual_correlation(sigma: np.ndarray, n_remove: int) -> np.ndarray:
    """Correlation after removing the top ``n_remove`` eigen-components (market mode first)."""
    corr = to_correlation(sigma)
    n = corr.shape[0]
    if n_remove <= 0 or n < 4:
        return corr
    k = min(n_remove, n - 2)
    if n <= 400:
        w, v = np.linalg.eigh(corr)
        w, v = w[-k:], v[:, -k:]
    else:
        w, v = eigsh(corr, k=k, which="LA")
    res = corr - (v * w) @ v.T
    d = np.sqrt(np.clip(np.diag(res), 1e-6, None))
    res = res / np.outer(d, d)
    np.fill_diagonal(res, 1.0)
    return np.clip((res + res.T) / 2.0, -1.0, 1.0)


def marchenko_pastur_edge(n: int, t: int, sigma2: float = 1.0) -> float:
    """Upper noise edge λ₊ = σ²(1+√(n/T))² of a pure-noise correlation spectrum (T observations)."""
    return float(sigma2 * (1.0 + np.sqrt(n / max(t, 1))) ** 2)


def mp_denoised_correlation(
    sigma: np.ndarray, n_obs: int, fit: str = "fixed"
) -> tuple[np.ndarray, dict[str, float]]:
    """Marchenko–Pastur cleaned correlation C* used **only to find clusters** (spec C2-R4/R5).

    Eigenvalues ≤ λ₊ are treated as noise and dropped; the largest (market mode) is dropped too.
    ``fit='market_adjusted'`` uses σ² = 1 − λ₁/n (the bulk's variance share) instead of 1.
    Falls back to market-mode removal, and says so in the info, if no structure survives.
    """
    corr = to_correlation(sigma)
    n = corr.shape[0]
    w, v = np.linalg.eigh(corr)
    w, v = w[::-1], v[:, ::-1]  # descending
    sigma2 = max(1.0 - float(w[0]) / n, 1e-6) if fit == "market_adjusted" else 1.0
    lam_plus = marchenko_pastur_edge(n, n_obs, sigma2)
    keep = np.flatnonzero(w > lam_plus)
    keep = keep[keep != 0]  # index 0 is the market mode
    info = {
        "mp_lambda_plus": lam_plus,
        "mp_sigma2": sigma2,
        "mp_modes_above_edge": float(int((w > lam_plus).sum())),
        "mp_modes_kept": float(len(keep)),
        "mp_variance_share_kept": float(w[keep].sum() / n) if len(keep) else 0.0,
        "mp_fallback_to_market_mode": 0.0,
    }
    if len(keep) == 0:
        info["mp_fallback_to_market_mode"] = 1.0
        return residual_correlation(sigma, 1), info
    cleaned = (v[:, keep] * w[keep]) @ v[:, keep].T
    d = np.sqrt(np.clip(np.diag(cleaned), 1e-6, None))
    cleaned = cleaned / np.outer(d, d)
    np.fill_diagonal(cleaned, 1.0)
    return np.clip((cleaned + cleaned.T) / 2.0, -1.0, 1.0), info


def correlation_distance(corr: np.ndarray) -> np.ndarray:
    return np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, None))


# ----------------------------------------------------------------------------- tree splitting


def _split_tree(z: np.ndarray, n: int, max_size: int) -> list[np.ndarray]:
    root = to_tree(z)
    out: list[np.ndarray] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.count <= max_size or node.is_leaf():
            out.append(np.array(node.pre_order(), dtype=int))
        else:
            stack.append(node.get_right())
            stack.append(node.get_left())
    return out


def _hierarchical(dist: np.ndarray, max_size: int, method: str) -> list[np.ndarray]:
    n = dist.shape[0]
    if n <= max_size:
        return [np.arange(n)]
    if n == 2:
        return [np.arange(2)]
    z = linkage(squareform(dist, checks=False), method=method)
    return _split_tree(z, n, max_size)


def _spectral(corr: np.ndarray, max_size: int, seed: int) -> list[np.ndarray]:
    from sklearn.cluster import SpectralClustering

    n = corr.shape[0]
    if n <= max_size:
        return [np.arange(n)]
    k = int(np.ceil(n / max(2, int(max_size * 0.75))))
    aff = np.clip((corr + 1.0) / 2.0, 0.0, 1.0)
    labels = SpectralClustering(
        n_clusters=k, affinity="precomputed", random_state=seed, assign_labels="kmeans"
    ).fit_predict(aff)
    out: list[np.ndarray] = []
    for lab in np.unique(labels):
        members = np.flatnonzero(labels == lab)
        if len(members) > max_size:
            sub = _hierarchical(
                correlation_distance(corr[np.ix_(members, members)]), max_size, "average"
            )
            out.extend(members[s] for s in sub)
        else:
            out.append(members)
    return out


def _modularity_split(corr: np.ndarray, members: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Bisect ``members`` by the leading eigenvector of the generalised modularity matrix."""
    sub = corr[np.ix_(members, members)].copy()
    np.fill_diagonal(sub, 0.0)
    deg = sub.sum(axis=1)
    total = deg.sum()
    b = sub - np.outer(deg, deg) / total if abs(total) > 1e-12 else sub
    b = b - np.diag(b.sum(axis=1))  # generalised form for a sub-graph (Newman 2006)
    b = (b + b.T) / 2.0
    vals, vecs = np.linalg.eigh(b)
    lead = vecs[:, -1]
    side = lead > 0
    if vals[-1] <= 1e-10 or side.all() or (~side).all():
        side = lead > np.median(lead)  # indivisible by modularity: still must respect the size cap
        if side.all() or (~side).all():
            side = np.arange(len(members)) < len(members) // 2
    return members[side], members[~side]


def _modularity(corr: np.ndarray, max_size: int, order: str = "fifo") -> list[np.ndarray]:
    """Recursive modularity bisection until every community is ≤ ``max_size`` (spec C3-R2)."""
    n = corr.shape[0]
    if n <= max_size:
        return [np.arange(n)]
    done: list[np.ndarray] = []
    queue: list[np.ndarray] = [np.arange(n)]
    while queue:
        if order == "size":
            queue.sort(key=len, reverse=True)
        members = queue.pop(0)
        if len(members) <= max_size:
            done.append(members)
            continue
        left, right = _modularity_split(corr, members)
        queue.extend([left, right])
    return done


def _merge_small(groups: list[np.ndarray], corr: np.ndarray, max_size: int) -> list[np.ndarray]:
    """Absorb undersized clusters into their most-correlated neighbour (respecting the cap)."""
    min_size = max(2, max_size // 4)
    groups = [g.copy() for g in groups]
    changed = True
    while changed:
        changed = False
        order = sorted(range(len(groups)), key=lambda i: len(groups[i]))
        for i in order:
            if len(groups[i]) >= min_size:
                break
            best, best_c = -1, -np.inf
            for j in range(len(groups)):
                if j == i or len(groups[i]) + len(groups[j]) > max_size:
                    continue
                c = corr[np.ix_(groups[i], groups[j])].mean()
                if c > best_c:
                    best, best_c = j, c
            if best >= 0:
                groups[best] = np.concatenate([groups[best], groups[i]])
                groups.pop(i)
                changed = True
                break
    return groups


def cluster_indices(
    corr: np.ndarray, cfg: DecompositionConfig, seed: int, sector_codes: np.ndarray | None = None
) -> list[np.ndarray]:
    """Partition ``range(n)`` into groups of at most ``cfg.max_cluster_size`` (F3-R1..R3, R7)."""
    n = corr.shape[0]
    max_size = cfg.max_cluster_size
    if cfg.sector_homogeneous and sector_codes is not None:
        groups: list[np.ndarray] = []
        for s in np.unique(sector_codes):
            members = np.flatnonzero(sector_codes == s)
            sub = cluster_indices(
                corr[np.ix_(members, members)],
                cfg.model_copy(update={"sector_homogeneous": False}),
                seed,
                None,
            )
            groups.extend(members[g] for g in sub)
        return groups
    if cfg.clusterer == "spectral":
        groups = _spectral(corr, max_size, seed)
    elif cfg.clusterer == "modularity":
        groups = _modularity(corr, max_size)
    else:
        groups = _hierarchical(correlation_distance(corr), max_size, cfg.linkage)
    groups = _merge_small(groups, corr, max_size)
    # canonical order: by smallest member index -> deterministic ids
    groups = [np.sort(g) for g in groups]
    groups.sort(key=lambda g: int(g[0]))
    assert sum(len(g) for g in groups) == n
    return groups


# ----------------------------------------------------------------------------- allocation


def _apportion(share: np.ndarray, lo: np.ndarray, hi: np.ndarray, total: int) -> np.ndarray:
    """Largest-remainder apportionment of ``total`` units with per-cluster bounds."""
    if lo.sum() > total or hi.sum() < total:
        raise InfeasibleError(
            f"cannot allocate K={total} picks across clusters (bounds [{lo.sum()}, {hi.sum()}])"
        )
    x = np.clip(np.floor(share).astype(int), lo, hi)
    while x.sum() < total:
        room = x < hi
        deficit = np.where(room, share - x, -np.inf)
        x[int(np.argmax(deficit))] += 1
    while x.sum() > total:
        slack = x > lo
        surplus = np.where(slack, x - share, -np.inf)
        x[int(np.argmax(surplus))] -= 1
    return x


def _effective_bets(corr_sub: np.ndarray) -> float:
    """Participation ratio of the intra-cluster correlation spectrum (≈ independent bets)."""
    if corr_sub.shape[0] == 1:
        return 1.0
    w = np.clip(np.linalg.eigvalsh(corr_sub), 0, None)
    s2 = float((w**2).sum())
    return float(w.sum() ** 2 / s2) if s2 > 0 else 1.0


def allocate_k(
    universe: Universe,
    clusters: list[Cluster],
    corr_res: np.ndarray,
    mandate: MandateConfig,
    cfg: DecompositionConfig,
) -> KAllocation:
    """Decide picks per cluster (F3-R5) plus neighbour allocations for exploration (F6-R4)."""
    k = mandate.K
    c = len(clusters)
    sizes = np.array([cl.size for cl in clusters], dtype=float)
    forced = np.array([len(cl.forced_in) for cl in clusters], dtype=int)
    cap = np.array([cl.size for cl in clusters], dtype=int)

    if cfg.k_allocation == "user":
        user = np.asarray(cfg.user_k_allocation, dtype=int)
        if len(user) != c or user.sum() != k:
            raise InfeasibleError(
                f"user_k_allocation must have {c} entries summing to K={k}; got "
                f"{len(user)} entries summing to {int(user.sum())}"
            )
        base = user
    else:
        if cfg.k_allocation == "proportional":
            weight = sizes.copy()
        else:  # risk_aware: ∝ effective independent bets × tilt toward attractive clusters
            sd = np.sqrt(np.clip(np.diag(universe.sigma), 1e-12, None))
            score = universe.mu / sd
            members = [np.concatenate([cl.asset_idx, cl.forced_in]).astype(int) for cl in clusters]
            neff = np.array([_effective_bets(corr_res[np.ix_(m, m)]) for m in members])
            cl_score = np.array([score[m].mean() for m in members])
            z = (cl_score - cl_score.mean()) / (cl_score.std() + 1e-12) if c > 1 else cl_score * 0
            weight = neff * np.exp(0.35 * np.clip(z, -3, 3))
        share = weight / weight.sum() * k
        base = _apportion(share, forced, cap, k)

    alts: list[np.ndarray] = []
    if cfg.k_neighbor_delta > 0 and cfg.max_neighbor_allocations > 0 and c > 1:
        density = base / np.maximum(sizes, 1.0)
        # move `delta` picks from over-served to under-served clusters
        seen = {tuple(base.tolist())}
        for delta in range(1, cfg.k_neighbor_delta + 1):
            donors = [i for i in np.argsort(-density) if base[i] - delta >= forced[i]]
            takers = [i for i in np.argsort(density) if base[i] + delta <= cap[i]]
            pairs = [(a, b) for a in donors for b in takers if a != b]
            pairs.sort(key=lambda ab: density[ab[0]] - density[ab[1]], reverse=True)
            for a, b in pairs:
                alt = base.copy()
                alt[a] -= delta
                alt[b] += delta
                key = tuple(alt.tolist())
                if key in seen:
                    continue
                seen.add(key)
                alts.append(alt)
                if len(alts) >= cfg.max_neighbor_allocations:
                    break
            if len(alts) >= cfg.max_neighbor_allocations:
                break
    return KAllocation(base=base.astype(int), alternatives=alts)


# ----------------------------------------------------------------------------- quality


def cluster_quality(
    universe: Universe, groups: list[np.ndarray], corr_res: np.ndarray
) -> tuple[dict[str, float], list[dict[str, float]]]:
    """Intra/inter correlation and the share of covariance 'cut' by the split (F3-R4)."""
    n = universe.n
    label = np.empty(n, dtype=int)
    for i, g in enumerate(groups):
        label[g] = i
    same = label[:, None] == label[None, :]
    off = ~np.eye(n, dtype=bool)
    abs_cov = np.abs(universe.sigma)
    total = abs_cov[off].sum()
    cut_raw = abs_cov[off & ~same].sum() / total if total > 0 else 0.0
    abs_res = np.abs(corr_res)
    total_r = abs_res[off].sum()
    cut_res = abs_res[off & ~same].sum() / total_r if total_r > 0 else 0.0

    per: list[dict[str, float]] = []
    for g in groups:
        if len(g) > 1:
            sub = corr_res[np.ix_(g, g)]
            intra = float((sub.sum() - len(g)) / (len(g) * (len(g) - 1)))
        else:
            intra = 1.0
        per.append({"size": float(len(g)), "intra_residual_corr": intra})
    inter_mask = off & ~same
    summary = {
        "n_clusters": float(len(groups)),
        "max_size": float(max(len(g) for g in groups)),
        "mean_size": float(np.mean([len(g) for g in groups])),
        "cut_share_raw_cov": float(cut_raw),
        "cut_share_residual_corr": float(cut_res),
        "intra_residual_corr_mean": float(np.mean([p["intra_residual_corr"] for p in per])),
        "inter_residual_corr_mean": float(corr_res[inter_mask].mean()) if inter_mask.any() else 0.0,
    }
    return summary, per


def risk_rebalance_factor(universe: Universe, groups: list[np.ndarray]) -> float:
    """q'/q = [Σₖ‖μₖ‖ / ‖μ‖] / [Σₖ‖Σₖ‖_F / ‖Σ‖_F] (JPMorgan/Amazon Eq. 15; empirical, ablation A13)."""
    members = np.concatenate(groups)
    mu = universe.mu[members]
    sig = universe.sigma[np.ix_(members, members)]
    num = sum(np.linalg.norm(universe.mu[g]) for g in groups) / max(np.linalg.norm(mu), 1e-12)
    den = sum(np.linalg.norm(universe.sigma[np.ix_(g, g)]) for g in groups) / max(
        np.linalg.norm(sig), 1e-12
    )
    return float(num / den) if den > 0 else 1.0


def build_clusters(
    universe: Universe,
    eligible: np.ndarray,
    mandate: MandateConfig,
    cfg: DecompositionConfig,
    seed: int,
) -> tuple[list[Cluster], KAllocation, dict[str, float], np.ndarray]:
    """End-to-end F3: cluster eligible assets, split forced-in, allocate picks."""
    elig_idx = np.flatnonzero(eligible)
    n_remove = cfg.n_factors_removed if cfg.remove_market_mode else 0
    mp_info: dict[str, float] = {}
    if cfg.denoise == "marchenko_pastur":
        n_obs = int(universe.metadata.get("n_days", 252))
        corr_res_full, mp_info = mp_denoised_correlation(universe.sigma, n_obs, cfg.mp_fit)
    else:
        corr_res_full = residual_correlation(universe.sigma, n_remove)
    sub_corr = corr_res_full[np.ix_(elig_idx, elig_idx)]
    sector_codes, _ = universe.sector_codes()
    groups_local = cluster_indices(sub_corr, cfg, seed, sector_codes[elig_idx])
    forced_in = set(universe.index_of(mandate.forced_in).tolist())

    clusters: list[Cluster] = []
    for cid, g in enumerate(groups_local):
        glob = elig_idx[g]
        fi = np.array([i for i in glob if i in forced_in], dtype=int)
        free = np.array([i for i in glob if i not in forced_in], dtype=int)
        clusters.append(Cluster(id=cid, asset_idx=free, forced_in=fi))

    groups_global = [np.concatenate([c.asset_idx, c.forced_in]).astype(int) for c in clusters]
    # Quality is always measured on the SAME market-removed yardstick, whichever matrix was used to
    # cluster, so raw- vs residual-correlation clustering (ablation A4 / H2) compare like for like.
    yardstick = residual_correlation(universe.sigma, max(1, cfg.n_factors_removed))
    summary, per = cluster_quality(universe, groups_global, yardstick)
    summary.update(mp_info)
    summary["lam_scale"] = (
        risk_rebalance_factor(universe, groups_global)
        if cfg.risk_rebalance == "norm_ratio"
        else 1.0
    )
    # qubits needed without decomposition (one register for all eligible assets) vs the largest cluster
    summary["compression_ratio"] = float(len(elig_idx)) / float(summary["max_size"])
    for cl, q in zip(clusters, per, strict=True):
        cl.quality = q
    alloc = allocate_k(universe, clusters, corr_res_full, mandate, cfg)
    return clusters, alloc, summary, corr_res_full
