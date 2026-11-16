"""Benchmark harness: honest verdict, ablations, report rendering."""

from __future__ import annotations

from qportfolio.benchmark import ablation_summary, make_instance, run_ablation, run_instance
from qportfolio.manifest import build_manifest
from qportfolio.reporting import quantum_findings, render_html, render_markdown


def _small():
    return make_instance(
        "S",
        24,
        seed=1,
        k=5,
        base={
            "solver": {"p_layers": 1, "max_iterations": 8, "shots": 256},
            "benchmark": {"miqp_time_limit": 2.0},
        },
    )


def test_run_instance_and_report():
    inst = _small()
    runs = run_instance(
        inst, ["equal_weight", "greedy", "multistart_classical", "decomposed_classical"], 1
    )
    methods = {r.method for r in runs}
    assert "quantum_qaoa" in methods and "greedy" in methods
    findings = quantum_findings(runs, has_hardware=False)
    assert findings and any(
        f.status == "not_assessed" for f in findings
    )  # hardware claim honestly not assessed
    md = render_markdown(runs, findings, {}, build_manifest({"run": {"seed": 1}}, "benchmark", {}))
    assert "not investment advice" in md.lower()
    assert "<html" in render_html(md).lower()
    assert all(r.audit_violations == 0 for r in runs)


def test_ablation_runs():
    out = ablation_summary(run_ablation("A4_clustering_input", _small(), seeds=2))
    assert out
