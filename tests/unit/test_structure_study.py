"""The pre-registered statistics must give known answers on known data before any study is trusted."""

from __future__ import annotations

import numpy as np
import pytest

from qportfolio.benchmark.structure_study import (
    STAT_SEED,
    add_gaps,
    analyse,
    bootstrap_ci,
    classify,
    holm,
    load_spec,
    paired_diffs,
    run_one_instance,
    sign_flip_p,
)

SPEC = load_spec()


def test_preregistration_file_declares_the_agreed_design():
    assert SPEC["design"]["sub_solver"] == "exact"
    assert SPEC["design"]["seeds"] >= 10
    assert set(SPEC["hypotheses"]) == {"H11", "H12", "H19"}
    assert SPEC["tolerances"]["gap_pp"] == pytest.approx(0.10)
    assert {"default", "mp", "modularity", "spectral", "rebalance", "no_denoise"} <= set(
        SPEC["arms"]
    )


def test_bootstrap_ci_covers_the_true_mean_and_is_deterministic():
    rng = np.random.default_rng(0)
    d = rng.normal(-1.0, 0.5, 40)
    m, lo, hi = bootstrap_ci(d, STAT_SEED)
    assert lo < -1.0 < hi
    assert (m, lo, hi) == bootstrap_ci(d, STAT_SEED)
    assert np.isnan(bootstrap_ci(np.array([]), 1)[0])


def test_sign_flip_p_is_small_for_a_real_effect_and_large_for_noise():
    rng = np.random.default_rng(1)
    assert sign_flip_p(rng.normal(-1.0, 0.3, 40), 1) < 0.01
    assert sign_flip_p(rng.normal(0.0, 1.0, 40), 1) > 0.05
    assert sign_flip_p(np.zeros(10), 1) == 1.0


def test_holm_is_monotone_and_never_below_the_raw_p():
    adj = holm({"a": 0.001, "b": 0.02, "c": 0.04, "d": 0.9})
    vals = [adj[k] for k in ("a", "b", "c", "d")]
    assert vals == sorted(vals)
    assert all(adj[k] >= p for k, p in {"a": 0.001, "b": 0.02, "c": 0.04, "d": 0.9}.items())
    assert adj["a"] == pytest.approx(0.004)


@pytest.mark.parametrize(
    ("mean", "lo", "hi", "p", "expected"),
    [
        (-0.5, -0.8, -0.2, 0.01, "supported"),  # clearly better
        (-0.5, -0.8, -0.2, 0.20, "inconclusive"),  # better but not significant after Holm
        (-0.05, -0.08, -0.02, 0.001, "refuted"),  # significant but below the practical tolerance
        (0.0, -0.03, 0.03, 0.9, "refuted"),  # interval entirely inside +/- tolerance: no difference
        (0.6, 0.3, 0.9, 0.01, "refuted"),  # arm is meaningfully WORSE
        (-0.2, -0.9, 0.4, 0.3, "inconclusive"),  # wide interval
        (float("nan"), float("nan"), float("nan"), 1.0, "inconclusive"),
    ],
)
def test_outcome_rule(mean, lo, hi, p, expected):
    assert classify(mean, lo, hi, p, tol=0.10) == expected


def _rows(effect: float, n: int = 30, noise: float = 0.1, seed: int = 0):
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(n):
        base = -1.0
        for arm in SPEC["arms"]:
            shift = effect if arm in ("mp", "modularity", "rebalance") else 0.0
            rows.append(
                {
                    "family": "S",
                    "n": 60,
                    "seed": s,
                    "arm": arm,
                    "best": base + shift + rng.normal(0, noise),
                    "cut_share": 0.5,
                    "n_clusters": 5.0,
                    "max_size": 10.0,
                    "seconds": 0.1,
                    "audit_violations": 0,
                    "feasible": True,
                }
            )
    return rows


def test_analyse_detects_a_planted_improvement():
    out = analyse(_rows(effect=-0.05, noise=0.005), SPEC)
    assert out["hypotheses"]["H11"]["outcome"] == "supported"
    assert out["hypotheses"]["H19"]["outcome"] == "supported"
    assert out["hypotheses"]["H12"]["outcome"] == "supported"
    assert out["gate"]["passed"]


def test_analyse_reports_no_effect_when_there_is_none():
    out = analyse(_rows(effect=0.0, noise=0.0005), SPEC)
    for h in ("H11", "H12", "H19"):
        assert out["hypotheses"][h]["outcome"] == "refuted"


def test_analyse_flags_an_audit_violation_in_the_gate():
    rows = _rows(effect=0.0)
    rows[3]["audit_violations"] = 1
    assert analyse(rows, SPEC)["gate"]["passed"] is False


def test_gap_reference_is_the_best_found_and_gaps_are_nonnegative():
    rows = add_gaps(_rows(effect=-0.05, n=5))
    assert all(r["gap_pct"] >= -1e-12 for r in rows)
    d = paired_diffs(rows, "mp", "default", "gap_pct")
    assert len(d) == 5


def test_run_one_instance_returns_every_arm_plus_the_reference_and_is_clean():
    rows = run_one_instance(("S", 40, 7, SPEC["arms"], 10, 1.0))
    arms = {r["arm"] for r in rows}
    assert arms == set(SPEC["arms"]) | {"__miqp__"}
    for r in rows:
        if r["arm"] != "__miqp__":
            assert r["audit_violations"] == 0
            assert r["feasible"]
            assert r["max_size"] <= 10
