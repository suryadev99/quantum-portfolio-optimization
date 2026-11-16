"""CLI smoke tests (F9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from qportfolio.cli.main import app

runner = CliRunner()
CONFIGS = Path(__file__).parents[2] / "configs"


@pytest.fixture()
def small_cfg(tmp_path):
    raw = yaml.safe_load((CONFIGS / "demo_small.yaml").read_text())
    raw["data"]["synthetic"].update(n_assets=20, n_sectors=4, n_days=300)
    raw["mandate"].update(K=5, w_max=0.3)
    raw["solver"].update(max_iterations=8, shots=256)
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


def test_validate_run_inspect_dry_run(small_cfg, tmp_path):
    assert runner.invoke(app, ["validate", str(small_cfg)]).exit_code == 0
    out = tmp_path / "out"
    r = runner.invoke(app, ["run", str(small_cfg), "--out", str(out), "--algorithm", "sa"])
    assert r.exit_code == 0, r.output
    assert json.loads((out / "menu.json").read_text())["portfolios"]
    assert runner.invoke(app, ["inspect", str(out)]).exit_code == 0
    r = runner.invoke(app, ["dry-run", str(small_cfg)])
    assert r.exit_code == 0 and "submitted jobs=0" in r.output.replace("\n", " ")
    r = runner.invoke(app, ["dry-run", str(small_cfg), "--json"])
    assert json.loads(r.output)["submitted_jobs"] == 0


def test_bad_config_exits_nonzero(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("mandate: {K: 3}\nsolver: {backend: {token: abc}}\n")
    r = runner.invoke(app, ["validate", str(bad)])
    assert r.exit_code == 2 and "Credentials" in r.output.replace("\n", " ")


def test_benchmark_command(tmp_path):
    out = tmp_path / "b"
    r = runner.invoke(
        app,
        ["benchmark", "--instances", "S:20", "--seeds", "1", "--out", str(out), "--miqp-time", "1"],
    )
    assert r.exit_code == 0, r.output
    assert (out / "report.md").exists() and (out / "findings.json").exists()
    assert "VERDICT" in r.output
