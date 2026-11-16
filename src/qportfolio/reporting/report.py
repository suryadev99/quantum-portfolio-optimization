"""Markdown / HTML benchmark report and plots (F8-R5)."""

from __future__ import annotations

import html
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from qportfolio.benchmark.metrics import summarize
from qportfolio.benchmark.runner import MethodRun
from qportfolio.reporting.verdict import Finding, verdict_text

DISCLAIMER = (
    "Outputs are decision-support and research results, not investment advice. "
    "No claim of quantum advantage is made unless explicitly shown by the verdict above."
)

_METRICS = [
    ("gap_pct", "Gap % (↓)"),
    ("n_band", "In-band (↑)"),
    ("mean_jaccard", "Diversity (↑)"),
    ("coverage", "Coverage (↑)"),
    ("menu_size", "Menu"),
    ("seconds", "Time s (↓)"),
]


def _fmt(x: float | None, nd: int = 3) -> str:
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{nd}f}"


def method_table(runs: list[MethodRun]) -> list[dict[str, Any]]:
    """Median ± 95% half-width per (method) across all instances and seeds."""
    by: dict[str, list[MethodRun]] = defaultdict(list)
    for r in runs:
        by[r.method].append(r)
    rows = []
    for m, rs in by.items():
        row: dict[str, Any] = {"method": m, "runs": len(rs)}
        for key, _ in _METRICS:
            s = summarize([getattr(r, key) for r in rs])
            row[key] = s
        row["feasible_rate"] = float(np.mean([r.feasible for r in rs]))
        rows.append(row)
    rows.sort(key=lambda r: (r["method"] != "quantum_qaoa", r["method"]))
    return rows


def _md_table(rows: list[dict[str, Any]]) -> str:
    head = "| Method | Runs | " + " | ".join(lbl for _, lbl in _METRICS) + " | Feasible |\n"
    head += "|---|---|" + "---|" * (len(_METRICS) + 1) + "\n"
    body = ""
    for r in rows:
        cells = []
        for key, _ in _METRICS:
            s = r[key]
            cells.append(
                f"{_fmt(s['median'])} ± {_fmt(s['ci95'], 2)}" if s["n"] > 1 else _fmt(s["median"])
            )
        body += (
            f"| {r['method']} | {r['runs']} | "
            + " | ".join(cells)
            + f" | {r['feasible_rate']:.0%} |\n"
        )
    return head + body


def _findings_md(findings: list[Finding]) -> str:
    md = "| ID | Claim | Status | Evidence |\n|---|---|---|---|\n"
    for f in findings:
        md += f"| {f.id} | {f.claim} | **{f.status}** | {f.detail} |\n"
    return md


def _ablation_md(ablations: dict[str, dict[str, dict[str, float]]]) -> str:
    out = ""
    for name, arms in ablations.items():
        out += f"\n#### {name}\n\n| Arm | best obj | pool | menu | min Jaccard | cut share | raw feasible | rank agree | depth | 2q gates | s |\n|---|---|---|---|---|---|---|---|---|---|---|\n"
        for arm, m in arms.items():
            out += (
                f"| {arm} | {_fmt(m.get('best'), 5)} | {_fmt(m.get('pool_size'), 0)} | {_fmt(m.get('menu_size'), 0)} | "
                f"{_fmt(m.get('menu_min_jaccard'))} | {_fmt(m.get('cut_share'))} | {_fmt(m.get('mean_raw_feasible_fraction'), 2)} | "
                f"{_fmt(m.get('mean_rank_agreement'), 2)} | {_fmt(m.get('max_depth'), 0)} | {_fmt(m.get('max_two_qubit_gates'), 0)} | {_fmt(m.get('seconds'), 1)} |\n"
            )
    return out


def render_markdown(
    runs: list[MethodRun],
    findings: list[Finding],
    ablations: dict[str, dict[str, dict[str, float]]],
    manifest: dict[str, Any],
    title: str = "QPortfolio benchmark report",
) -> str:
    rows = method_table(runs)
    instances = sorted(
        {(r.instance, r.family, r.n) for r in runs}, key=lambda t: (t[1], t[2], t[0])
    )
    inst_md = "\n".join(f"- `{n}` (family {f}, N={sz})" for n, f, sz in instances)
    verdict = verdict_text(findings)
    pk = manifest.get("packages", {})
    return f"""# {title}

> {DISCLAIMER}

## Verdict

```
{verdict}
```

## Summary — method × metric

Median ± 95% half-width over instances × seeds. Gap is relative to the best objective found by *any* method
on the same instance (or the proven optimum for tiny instances). Diversity = mean pairwise Jaccard distance
of up to 10 portfolios inside the 3% band; coverage = share of the pooled near-optimal reference set within
Jaccard 0.2 of a found portfolio.

{_md_table(rows)}

## Hypotheses and findings

{_findings_md(findings)}

## Ablations
{_ablation_md(ablations) if ablations else "_No ablations were run._"}

## Instances

{inst_md}

## Reproducibility

- Config hash: `{manifest.get("config_hash")}`  ·  git: `{manifest.get("git_commit")}`
- Python {manifest.get("python")} · qiskit {pk.get("qiskit")} · qiskit-aer {pk.get("qiskit-aer")} · qiskit-ibm-runtime {pk.get("qiskit-ibm-runtime")}
- Created (UTC): {manifest.get("created_utc")}

---
{DISCLAIMER}
"""


def render_html(markdown_text: str, title: str = "QPortfolio benchmark report") -> str:
    """Minimal, dependency-free HTML wrapper preserving the Markdown in a readable layout."""
    body = html.escape(markdown_text)
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>"
        + html.escape(title)
        + "</title><style>body{font-family:-apple-system,Segoe UI,sans-serif;max-width:980px;margin:2em auto;"
        "padding:0 1em;line-height:1.5}pre{white-space:pre-wrap;background:#f7f8fa;border:1px solid #e5e7eb;"
        "padding:1em;border-radius:6px}</style></head><body><pre>" + body + "</pre></body></html>"
    )


def write_plots(runs: list[MethodRun], out_dir: str | Path) -> list[Path]:
    """Gap vs N, diversity by method, runtime vs N, qubits vs N (spec §13.6)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    methods = sorted({r.method for r in runs}, key=lambda m: (m != "quantum_qaoa", m))
    paths: list[Path] = []

    def line(metric: str, ylabel: str, fname: str, logy: bool = False) -> None:
        fig, ax = plt.subplots(figsize=(6.4, 4))
        for m in methods:
            xs = sorted({r.n for r in runs if r.method == m})
            ys = [
                np.nanmedian(
                    [
                        getattr(r, metric) if getattr(r, metric) is not None else np.nan
                        for r in runs
                        if r.method == m and r.n == x
                    ]
                )
                for x in xs
            ]
            ax.plot(xs, ys, marker="o", label=m, linewidth=2.2 if m == "quantum_qaoa" else 1.2)
        ax.set_xlabel("universe size N")
        ax.set_ylabel(ylabel)
        if logy:
            ax.set_yscale("log")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=7)
        fig.tight_layout()
        p = out / fname
        fig.savefig(p, dpi=130)
        plt.close(fig)
        paths.append(p)

    line("gap_pct", "median objective gap (%)", "gap_vs_n.png")
    line("seconds", "median wall-clock (s)", "runtime_vs_n.png", logy=True)
    line("mean_jaccard", "median menu diversity (Jaccard)", "diversity_vs_n.png")

    fig, ax = plt.subplots(figsize=(6.4, 4))
    data = [[r.mean_jaccard for r in runs if r.method == m] for m in methods]
    ax.boxplot(data, tick_labels=methods, showfliers=False)
    ax.set_ylabel("in-band diversity (Jaccard)")
    ax.tick_params(axis="x", rotation=60, labelsize=7)
    fig.tight_layout()
    p = out / "diversity_by_method.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    paths.append(p)

    q = [r for r in runs if r.method == "quantum_qaoa"]
    if q:
        xs = sorted({r.n for r in q})
        fig, ax = plt.subplots(figsize=(6.4, 4))
        ax.plot(
            xs,
            [np.nanmedian([r.extra.get("n_clusters", np.nan) for r in q if r.n == x]) for x in xs],
            marker="o",
        )
        ax.set_xlabel("universe size N")
        ax.set_ylabel("clusters (= independent quantum sub-problems)")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        p = out / "clusters_vs_n.png"
        fig.savefig(p, dpi=130)
        plt.close(fig)
        paths.append(p)
    return paths
