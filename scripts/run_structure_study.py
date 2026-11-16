"""Run the pre-registered structure study (benchmarks/structure_study.yaml) and save raw + analysed JSON."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from qportfolio.benchmark.structure_study import add_gaps, analyse, load_spec, run_study

OUT = Path(__file__).resolve().parents[1] / "benchmarks" / "results"


def main() -> None:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    spec = load_spec()
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    def progress(i: int, total: int) -> None:
        print(f"[{time.perf_counter() - t0:7.1f}s] instance {i}/{total}", flush=True)

    rows = run_study(spec, workers=workers, progress=progress)
    (OUT / "structure_study_rows.json").write_text(
        json.dumps(add_gaps(rows), indent=1, default=str)
    )
    result = analyse(rows, spec)
    result["wall_seconds"] = time.perf_counter() - t0
    result["workers"] = workers
    (OUT / "structure_study_analysis.json").write_text(json.dumps(result, indent=1, default=str))
    print("DONE", json.dumps(result["hypotheses"], indent=1), flush=True)


if __name__ == "__main__":
    main()
