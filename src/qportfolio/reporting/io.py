"""Write run artefacts: menu (JSON+CSV), decomposition/resource reports, raw samples, manifest."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from qportfolio.config.loader import dump_config
from qportfolio.manifest import build_manifest, write_json

if TYPE_CHECKING:
    from qportfolio.pipeline import PipelineResult


def write_run_outputs(res: PipelineResult, out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    u = res.universe
    paths: list[Path] = []

    paths.append(write_json(out / "menu.json", res.menu.to_dict()))
    rows = []
    for p in res.menu.portfolios:
        for aid, w in (p.weights or {}).items():
            rows.append(
                {"rank": p.rank, "asset": aid, "weight": w, "sector": u.sectors[u.ids.index(aid)]}
            )
    pd.DataFrame(rows).to_csv(out / "menu.csv", index=False)
    paths.append(out / "menu.csv")

    decomposition = {
        "summary": res.cluster_summary,
        "clusters": [
            {
                "id": c.id,
                "size": c.size,
                "assets": [
                    u.ids[i] for i in np.concatenate([c.asset_idx, c.forced_in]).astype(int)
                ],
                "quality": c.quality,
            }
            for c in res.clusters
        ],
        "k_allocation": {"base": res.allocation.base, "alternatives": res.allocation.alternatives},
        "recombination": res.recombined.stats,
    }
    paths.append(write_json(out / "decomposition.json", decomposition))

    quantum = []
    for (cid, kf), pool in sorted(res.pools.items()):
        quantum.append(
            {
                "cluster": cid,
                "k_free": kf,
                "pool_size": len(pool),
                "best_objective": float(pool.objective[0]) if len(pool) else None,
                **pool.solver_info,
            }
        )
    paths.append(write_json(out / "solver_report.json", quantum))

    samples = []
    for (cid, kf), pool in sorted(res.pools.items()):
        for b, o, c in zip(pool.bits, pool.objective, pool.counts, strict=True):
            samples.append(
                {
                    "cluster": cid,
                    "k_free": kf,
                    "bitstring": "".join(map(str, b.tolist())),
                    "objective": float(o),
                    "device_count": int(c),
                }
            )
    pd.DataFrame(samples).to_parquet(out / "samples.parquet", index=False)
    paths.append(out / "samples.parquet")

    manifest = build_manifest(
        dump_config(res.config),
        u.data_hash(),
        res.timings,
        res.solver_usage,
        extra={
            "universe": {"n": u.n, **u.metadata},
            "stopped_early": res.stopped_early,
            "n_clusters": len(res.clusters),
        },
    )
    paths.append(write_json(out / "manifest.json", manifest))
    return paths
