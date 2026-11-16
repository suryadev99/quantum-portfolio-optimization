"""Run manifest for reproducibility and audit (F8-R6, NFR1, NFR7, spec §11.4)."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np

_PACKAGES = [
    "qiskit",
    "qiskit-aer",
    "qiskit-ibm-runtime",
    "qiskit-optimization",
    "numpy",
    "scipy",
    "pandas",
    "scikit-learn",
    "cvxpy",
    "pyscipopt",
    "numba",
    "qportfolio",
]


def package_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in _PACKAGES:
        try:
            out[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            out[p] = "not-installed"
    return out


def git_commit(cwd: str | Path | None = None) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


def config_hash(cfg_dict: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(_jsonable(cfg_dict), sort_keys=True).encode()).hexdigest()


def build_manifest(
    cfg_dict: dict[str, Any],
    data_hash: str,
    timings: dict[str, float],
    solver_usage: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything needed to reproduce (simulator) or audit (hardware) a run."""
    return _jsonable(
        {
            "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "git_commit": git_commit(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "packages": package_versions(),
            "config": cfg_dict,
            "config_hash": config_hash(cfg_dict),
            "seed": cfg_dict.get("run", {}).get("seed"),
            "input_data_hash": data_hash,
            "timings_seconds": timings,
            "solver_usage": solver_usage or {},
            **(extra or {}),
        }
    )


def write_json(path: str | Path, payload: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_jsonable(payload), indent=2))
    return p


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text())
