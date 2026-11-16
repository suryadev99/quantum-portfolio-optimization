"""Command line interface (F9-R2): ``qportfolio run | dry-run | benchmark | inspect | validate``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import typer
from rich.console import Console
from rich.table import Table

from qportfolio.config.loader import deep_update, dump_config, load_config
from qportfolio.utils.errors import QPortfolioError
from qportfolio.utils.logging import configure_logging

app = typer.Typer(
    name="qportfolio",
    help="Quantum-sampled, cluster-decomposed portfolio selection with built-in benchmarking.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

ConfigOpt = Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="YAML config file")]


def _overrides(
    backend: str | None, algorithm: str | None, seed: int | None, out: str | None
) -> dict[str, Any]:
    o: dict[str, Any] = {}
    if backend:
        o.setdefault("solver", {})["backend"] = (
            {"type": "simulator"}
            if backend == "simulator"
            else {"type": "noisy_simulator", "name": backend}
            if backend.startswith("fake_")
            else {"type": "ibm_hardware", "name": None if backend == "ibm" else backend}
        )
    if algorithm:
        o.setdefault("solver", {})["algorithm"] = algorithm
    if seed is not None:
        o.setdefault("run", {})["seed"] = seed
    if out:
        o.setdefault("run", {})["output_dir"] = out
    return o


def _fail(exc: Exception) -> typer.Exit:
    console.print(f"[bold red]error:[/bold red] {exc}")
    return typer.Exit(code=2)


@app.command()
def validate(config: ConfigOpt) -> None:
    """Validate a config file and the mandate against the (loaded) data."""
    try:
        cfg = load_config(config)
        from qportfolio.data import build_universe

        u = build_universe(cfg)
    except QPortfolioError as exc:
        raise _fail(exc) from exc
    console.print(f"[green]OK[/green] config valid · {u.n} assets · K={cfg.mandate.K}")


@app.command()
def run(
    config: ConfigOpt,
    backend: Annotated[
        str | None,
        typer.Option(help="simulator | fake_<device> | <ibm backend> | ibm (least busy)"),
    ] = None,
    algorithm: Annotated[str | None, typer.Option(help="qaoa | sa | exact")] = None,
    seed: Annotated[int | None, typer.Option()] = None,
    out: Annotated[str | None, typer.Option(help="output directory")] = None,
) -> None:
    """Run the full pipeline and write the portfolio menu, resource report and run manifest."""
    from qportfolio.pipeline import run_pipeline
    from qportfolio.reporting.io import write_run_outputs

    try:
        cfg = load_config(config, _overrides(backend, algorithm, seed, out))
        configure_logging(cfg.run.log_level)
        res = run_pipeline(cfg)
    except QPortfolioError as exc:
        raise _fail(exc) from exc
    paths = write_run_outputs(res, cfg.run.output_dir)

    menu = res.menu
    tbl = Table(title=f"Portfolio menu ({len(menu.portfolios)} of {cfg.menu.size})")
    for c in ("rank", "objective", "return", "vol", "sharpe", "eff.N", "dist→#1"):
        tbl.add_column(c, justify="right")
    for p in menu.portfolios:
        tbl.add_row(
            str(p.rank),
            f"{p.objective:.5f}",
            f"{p.stats['expected_return']:.3f}",
            f"{p.stats['volatility']:.3f}",
            f"{p.stats['sharpe']:.2f}",
            f"{p.stats['effective_n']:.1f}",
            f"{p.explanation.get('jaccard_distance_to_rank1', 0.0):.2f}",
        )
    console.print(tbl)
    for note in menu.notes:
        console.print(f"[yellow]note:[/yellow] {note}")
    if res.stopped_early:
        console.print(f"[yellow]stopped early:[/yellow] {res.stopped_early}")
    console.print(
        f"ε-band={menu.epsilon:.4g} · clusters={len(res.clusters)} · timings={res.timings}"
    )
    console.print("wrote: " + ", ".join(str(p) for p in paths))


@app.command("dry-run")
def dry_run_cmd(
    config: ConfigOpt,
    backend: Annotated[str | None, typer.Option()] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report qubits, depth, gates, shots and job counts WITHOUT submitting any job."""
    from qportfolio.pipeline import dry_run

    try:
        cfg = load_config(config, _overrides(backend, None, None, None))
        rep = dry_run(cfg)
    except QPortfolioError as exc:
        raise _fail(exc) from exc
    if as_json:
        console.print_json(
            json.dumps(rep, default=lambda o: o.tolist() if isinstance(o, np.ndarray) else str(o))
        )
        return
    console.print(
        f"[bold]Dry run[/bold] · backend={rep['backend']} ({rep['backend_kind']}) · assets={rep['n_assets']} · "
        f"clusters={rep['n_clusters']} · sub-problems={rep['n_nontrivial_subproblems']}/{rep['n_distinct_subproblems']}"
    )
    console.print(
        f"max qubits={rep['max_qubits']} · max depth={rep['max_depth']} · max 2q gates={rep['max_two_qubit_gates']} · "
        f"jobs={rep['sampling_jobs']} · shots={rep['total_shots']} · within job limit={rep['within_job_limit']} · "
        f"[green]submitted jobs={rep['submitted_jobs']}[/green]"
    )
    for n in rep["backend_notes"]:
        console.print(f"[yellow]{n}[/yellow]")


@app.command()
def benchmark(
    config: Annotated[
        Path | None, typer.Argument(help="optional YAML with a `benchmark:` section")
    ] = None,
    instances: Annotated[
        str, typer.Option(help="'reference' or comma list like S:40,C:80,H:60")
    ] = "reference",
    seeds: Annotated[int, typer.Option()] = 3,
    ablations: Annotated[
        str, typer.Option(help="comma list of ablation names, 'all', or 'none'")
    ] = "none",
    no_quantum: Annotated[bool, typer.Option("--no-quantum")] = False,
    backend: Annotated[str | None, typer.Option()] = None,
    out: Annotated[str, typer.Option()] = "outputs/benchmark",
    miqp_time: Annotated[float, typer.Option(help="MIQP time limit per run (s)")] = 8.0,
) -> None:
    """Run the quantum pipeline and all classical baselines, write the report with its verdict."""
    from qportfolio.benchmark import (
        ABLATIONS,
        ablation_summary,
        make_instance,
        reference_instances,
        run_ablation,
        run_instance,
    )
    from qportfolio.manifest import build_manifest, write_json
    from qportfolio.reporting import (
        ablation_findings,
        quantum_findings,
        render_html,
        render_markdown,
        write_plots,
    )

    base: dict[str, Any] = {"benchmark": {"miqp_time_limit": miqp_time}}
    if config:
        base = deep_update(base, __import__("yaml").safe_load(config.read_text()) or {})
    base = deep_update(base, _overrides(backend, None, None, None))
    try:
        if instances == "reference":
            insts = reference_instances()
            if base:
                from qportfolio.benchmark.instances import Instance

                insts = [
                    Instance(
                        i.name,
                        i.family,
                        i.n,
                        i.k,
                        i.seed,
                        type(i.cfg).model_validate(
                            deep_update(i.cfg.model_dump(mode="json"), base)
                        ),
                    )
                    for i in insts
                ]
        else:
            insts = []
            for tok in instances.split(","):
                fam, n = tok.split(":")
                insts.append(
                    make_instance(fam.strip().upper(), int(n), seed=len(insts) + 1, base=base)
                )
    except (ValueError, QPortfolioError) as exc:
        raise _fail(exc) from exc

    methods = list(insts[0].cfg.benchmark.baselines)
    runs = []
    with console.status("benchmarking…") as st:
        for inst in insts:
            runs += run_instance(
                inst,
                methods,
                seeds,
                include_quantum=not no_quantum,
                progress=lambda m: st.update(m),
            )
    abl_summ: dict[str, dict[str, dict[str, float]]] = {}
    names = (
        list(ABLATIONS)
        if ablations == "all"
        else ([] if ablations == "none" else [a.strip() for a in ablations.split(",")])
    )
    abl_inst = insts[0]
    for name in names:
        with console.status(f"ablation {name}…"):
            abl_summ[name] = ablation_summary(run_ablation(name, abl_inst, seeds=max(2, seeds - 1)))

    has_hw = any(r.extra.get("usage", {}).get("kind") == "ibm_hardware" for r in runs)
    findings = quantum_findings(runs, has_hardware=has_hw) + ablation_findings(abl_summ)
    manifest = build_manifest(
        dump_config(insts[0].cfg),
        "benchmark",
        {},
        extra={"instances": [i.name for i in insts], "seeds": seeds},
    )
    outp = Path(out)
    outp.mkdir(parents=True, exist_ok=True)
    md = render_markdown(runs, findings, abl_summ, manifest)
    (outp / "report.md").write_text(md)
    (outp / "report.html").write_text(render_html(md))
    write_json(outp / "runs.json", [r.to_dict() for r in runs])
    write_json(outp / "findings.json", [f.__dict__ for f in findings])
    write_json(outp / "manifest.json", manifest)
    plots = write_plots(runs, outp / "plots")
    from qportfolio.reporting.verdict import verdict_text

    console.print(verdict_text(findings))
    console.print(f"\nwrote {outp / 'report.md'} (+ html, json, {len(plots)} plots)")


@app.command()
def inspect(
    path: Annotated[Path, typer.Argument(exists=True, help="a run output directory")],
) -> None:
    """Summarise a finished run directory (menu, resource use, manifest)."""
    from qportfolio.manifest import read_json

    menu = read_json(path / "menu.json")
    man = read_json(path / "manifest.json")
    console.print(
        f"[bold]{path}[/bold] · created {man.get('created_utc')} · seed {man.get('seed')} · config {man.get('config_hash', '')[:12]}"
    )
    for p in menu["portfolios"]:
        console.print(
            f"  #{p['rank']}: obj={p['objective']:.5f} ret={p['stats']['expected_return']:.3f} vol={p['stats']['volatility']:.3f} assets={len(p['selected_ids'])}"
        )
    for n in menu.get("notes", []):
        console.print(f"[yellow]note:[/yellow] {n}")
    console.print(f"solver usage: {man.get('solver_usage')}")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
