"""Interactive demo of the best pipeline: decomposition -> sampling with controls -> audited menu.

Run:  streamlit run apps/demo/app.py --server.address 127.0.0.1

Four pages, each with a concepts panel. Synthetic data only. Localhost only. No credentials are ever
entered here and no hardware job can be started from this UI.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import networkx as nx
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from qportfolio import concepts
from qportfolio.clustering.clusterers import marchenko_pastur_edge, residual_correlation
from qportfolio.demo_core import (
    DEMO_HARD_STOP,
    DISCLAIMER,
    PRESETS,
    DemoSettings,
    compare_with_baselines,
    rank_mass,
    run_demo,
    run_sampling_study,
    verdict_for,
    warm_start_mass_table,
)
from qportfolio.manifest.manifest import _jsonable, build_manifest
from qportfolio.solvers import available_solvers, get_spec
from qportfolio.utils.linalg import to_correlation

st.set_page_config(page_title="QPortfolio demo", layout="wide")

PAGES = [
    "1. Business problem",
    "2. Decomposition",
    "3. Quantum sampling",
    "4. Portfolio menu and audit",
]
PALETTE = ["#3b82d4", "#7c5cd8", "#2f9e6e", "#d98b2b", "#c2410c", "#0f766e", "#9333ea", "#64748b"]


# ----------------------------------------------------------------------------- helpers


def concept(key: str) -> None:
    c = concepts.CONCEPTS[key]
    with st.expander(f"Concept: {c['title']}", expanded=False):
        st.markdown(f"**What it is.** {c['what']}")
        st.markdown(f"**Why it is here.** {c['why']}")
        st.markdown(f"**Honest limits.** {c['honest']}")


def banner() -> None:
    st.info(f"{DISCLAIMER}  Data: synthetic (sectors, ESG and liquidity scores are synthetic).")


@st.cache_data(show_spinner="Running the pipeline...")
def cached_run(key: str):
    run = run_demo(DemoSettings(**json.loads(key)))
    return run


@st.cache_data(show_spinner="Measuring samplers against the exact optimum...")
def cached_study(key: str, seeds: int):
    return run_sampling_study(DemoSettings(**json.loads(key)), seeds=seeds)


@st.cache_data(show_spinner="Measuring mass per rank...")
def cached_rank_mass(key: str):
    return rank_mass(DemoSettings(**json.loads(key)))


@st.cache_data(show_spinner="Running baselines on the same universe...")
def cached_baselines(key: str):
    return compare_with_baselines(DemoSettings(**json.loads(key)))


def sidebar() -> DemoSettings:
    st.sidebar.title("QPortfolio")
    st.sidebar.caption(
        "Best pipeline: MP denoise, modularity clusters, warm-start QAOA, exact recombination."
    )
    page = st.sidebar.radio("Page", PAGES, label_visibility="collapsed")
    st.session_state["page"] = page
    st.sidebar.divider()
    st.sidebar.subheader("Settings")
    preset = st.sidebar.selectbox("Universe", list(PRESETS), index=0)
    seed = st.sidebar.number_input("Seed", 0, 10_000, 7)
    size = st.sidebar.slider(
        "Max cluster size (qubits)",
        4,
        DEMO_HARD_STOP,
        10,
        help=f"Hard stop {DEMO_HARD_STOP}: statevector limit.",
    )
    with st.sidebar.expander("Pipeline choices", expanded=False):
        denoise = st.selectbox("Denoising", ["marchenko_pastur", "market_mode"], 0)
        clusterer = st.selectbox("Clusterer", ["modularity", "hierarchical", "spectral"], 0)
        rebalance = st.selectbox("Risk rebalancing", ["none", "norm_ratio"], 0)
        algos = [a for a in available_solvers() if a != "qaoa_warm"]
        algorithm = st.selectbox("Sampler", algos, algos.index("qaoa"))
        start = st.selectbox("QAOA start", ["warm", "cold"], 0)
        eps = st.slider("Warm-start clip ε", 0.05, 0.5, 0.25, 0.05)
        p_layers = st.slider("QAOA layers p", 1, 3, 2)
        shots = st.select_slider("Shots", [512, 1024, 2048, 4096], 2048)
    with st.sidebar.expander("Mandate", expanded=False):
        lam = st.slider("Risk aversion λ", 0.5, 8.0, 3.0, 0.5)
        cap_on = st.checkbox("Sector cap", False)
        cap = st.slider("Max weight per sector", 0.2, 0.6, 0.35, 0.05) if cap_on else None
    s = DemoSettings(
        preset=preset,
        seed=int(seed),
        max_cluster_size=size,
        denoise=denoise,
        clusterer=clusterer,
        risk_rebalance=rebalance,
        algorithm=algorithm,
        start=start,
        warm_epsilon=float(eps),
        p_layers=p_layers,
        shots=int(shots),
        sector_cap=cap,
        lambda_risk=float(lam),
    )
    st.sidebar.divider()
    st.sidebar.caption("Localhost only. No credentials. No hardware jobs from this UI.")
    return s


# ----------------------------------------------------------------------------- pages


def page_problem(s: DemoSettings, run) -> None:
    st.header("1. Business problem")
    banner()
    res = run.result
    u, m = res.universe, res.config.mandate
    n, k = u.n, m.K
    import math

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Assets (N)", n)
    c2.metric("Hold exactly (K)", k)
    c3.metric("Possible selections", f"{math.comb(n, k):.2e}")
    c4.metric("Weight cap per asset", f"{m.w_max:.0%}")
    st.markdown(
        f"**Mandate.** Choose exactly {k} of {n} assets and weight them (fully invested, each at most "
        f"{m.w_max:.0%}) to maximise return minus {m.lambda_risk:g} x risk."
        + (f" No sector above {s.sector_cap:.0%}." if s.sector_cap else "")
    )
    st.warning(
        "Honest context: for a problem this size a classical exact or MIQP solver finds the optimum in "
        "seconds. The point of this demo is the workflow and the measurements, not a speed claim."
    )
    concept("mandate")
    codes, _names = u.sector_codes()
    df = pd.DataFrame(
        {
            "asset": u.ids,
            "sector": u.sectors,
            "expected return": u.mu,
            "volatility": np.sqrt(np.diag(u.sigma)),
            "ESG (synthetic)": u.esg,
            "liquidity (synthetic)": u.liquidity,
        }
    )
    st.subheader("Universe")
    st.dataframe(df, height=260, width="stretch")
    fig = go.Figure(
        go.Scatter(
            x=df["volatility"],
            y=df["expected return"],
            mode="markers",
            marker={"color": [PALETTE[c % len(PALETTE)] for c in codes], "size": 8},
            text=df["asset"] + " (" + df["sector"] + ")",
            hovertemplate="%{text}<br>vol %{x:.3f}<br>return %{y:.3f}<extra></extra>",
        )
    )
    fig.update_layout(
        height=340, xaxis_title="Volatility", yaxis_title="Expected return", margin={"t": 10}
    )
    st.plotly_chart(fig, width="stretch")
    st.subheader("Pipeline")
    st.graphviz_chart(concepts.PIPELINE_DOT)
    concept("qubo")


def page_decomposition(s: DemoSettings, run) -> None:
    st.header("2. Decomposition")
    banner()
    res = run.result
    u, sm = res.universe, res.cluster_summary
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Clusters", int(sm["n_clusters"]))
    c2.metric("Largest cluster (qubits)", int(sm["max_size"]))
    c3.metric(
        "Compression ratio",
        f"{sm['compression_ratio']:.1f}x",
        help="qubits without decomposition / largest cluster",
    )
    c4.metric(
        "Cut share",
        f"{sm['cut_share_residual_corr']:.2f}",
        help="share of |correlation| between clusters; ignored by splitting, restored at recombination",
    )
    st.caption(
        f"Without decomposition {u.n} assets need {u.n} qubits; the largest cluster needs {int(sm['max_size'])}. "
        "Cut share is information the split ignores; recombination restores it exactly."
    )
    concept("decomposition")

    left, right = st.columns(2)
    with left:
        st.subheader("Eigenvalue spectrum vs the noise edge λ₊")
        corr = to_correlation(u.sigma)
        w = np.sort(np.linalg.eigvalsh(corr))[::-1]
        t = int(u.metadata.get("n_days", 252))
        edge = marchenko_pastur_edge(u.n, t)
        fig = go.Figure(
            go.Bar(
                x=list(range(1, len(w) + 1)),
                y=w,
                marker_color=["#3b82d4" if x > edge else "#c9ced6" for x in w],
            )
        )
        fig.add_hline(
            y=edge, line_dash="dash", line_color="#c2410c", annotation_text=f"λ₊ = {edge:.2f}"
        )
        fig.update_layout(
            height=340,
            xaxis_title="Eigenvalue rank",
            yaxis_title="Eigenvalue",
            margin={"t": 10},
            yaxis_type="log",
        )
        st.plotly_chart(fig, width="stretch")
        if "mp_modes_above_edge" in sm:
            st.caption(
                f"{int(sm['mp_modes_above_edge'])} eigenvalues exceed λ₊; after removing the market mode "
                f"{int(sm['mp_modes_kept'])} are kept, carrying {sm['mp_variance_share_kept']:.0%} of variance. "
                "The rest is treated as noise for clustering only."
            )
            if sm.get("mp_fallback_to_market_mode"):
                st.warning("No structure above λ₊: fell back to market-mode removal.")
        else:
            st.caption(
                "Denoising is set to market-mode removal; choose marchenko_pastur in the sidebar to see the kept modes."
            )
        concept("mp")
    with right:
        st.subheader("Cluster topology")
        n_remove = 1
        rc = residual_correlation(u.sigma, n_remove)
        thr = st.slider("Show edges above residual correlation", 0.05, 0.6, 0.2, 0.05)
        member = np.full(u.n, -1)
        for cl in res.clusters:
            for i in np.concatenate([cl.asset_idx, cl.forced_in]).astype(int):
                member[i] = cl.id
        g = nx.Graph()
        g.add_nodes_from(range(u.n))
        iu, ju = np.where(np.triu(rc, 1) > thr)
        g.add_edges_from(zip(iu.tolist(), ju.tolist(), strict=True))
        pos = nx.spring_layout(g, seed=3, k=1.2 / np.sqrt(max(u.n, 1)))
        ex, ey = [], []
        for a, b in g.edges:
            ex += [pos[a][0], pos[b][0], None]
            ey += [pos[a][1], pos[b][1], None]
        best = res.menu.portfolios[0] if res.menu.portfolios else None
        weight = np.zeros(u.n)
        if best:
            for i, aid in zip(best.selected_idx, best.selected_ids, strict=True):
                weight[i] = best.weights[aid]
        fig = go.Figure(
            go.Scatter(
                x=ex, y=ey, mode="lines", line={"width": 0.5, "color": "#d0d5dc"}, hoverinfo="skip"
            )
        )
        for cid in sorted(set(member.tolist())):
            idx = np.flatnonzero(member == cid)
            fig.add_trace(
                go.Scatter(
                    x=[pos[i][0] for i in idx],
                    y=[pos[i][1] for i in idx],
                    mode="markers",
                    name=f"cluster {cid} ({len(idx)})",
                    marker={
                        "size": [8 + 90 * weight[i] for i in idx],
                        "color": PALETTE[cid % len(PALETTE)],
                        "line": {"width": 1, "color": "white"},
                    },
                    text=[f"{u.ids[i]} {u.sectors[i]}<br>weight {weight[i]:.1%}" for i in idx],
                    hoverinfo="text",
                )
            )
        fig.update_layout(
            height=380,
            margin={"t": 10},
            xaxis={"visible": False},
            yaxis={"visible": False},
            legend={"font": {"size": 10}},
        )
        st.plotly_chart(fig, width="stretch")
        st.caption(
            "Node colour = cluster; node size = weight in the top menu portfolio; edges = residual correlation above the threshold."
        )
        concept("modularity")

    st.subheader("Clusters")
    rows = []
    for cl in res.clusters:
        rows.append(
            {
                "cluster": cl.id,
                "assets": cl.size,
                "k picks": int(res.allocation.base[cl.id])
                if cl.id < len(res.allocation.base)
                else None,
                "intra residual corr": round(
                    cl.quality.get("intra_residual_corr", float("nan")), 3
                ),
                "sectors": ", ".join(
                    sorted(
                        set(
                            u.sectors[
                                np.concatenate([cl.asset_idx, cl.forced_in]).astype(int)
                            ].tolist()
                        )
                    )
                ),
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def page_sampling(s: DemoSettings) -> None:
    st.header("3. Quantum sampling, with controls")
    banner()
    key = json.dumps(asdict(s))
    seeds = st.slider("Seeds per sampler", 1, 10, 5)
    study = cached_study(key, seeds)
    meta = study["meta"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cluster qubits (n)", meta["n"])
    c2.metric("Picks (k)", meta["k"])
    c3.metric("Selections to search", f"{study['n_selections']:,}")
    c4.metric(
        "Uniform-random level",
        f"{meta['uniform_level']:.4f}",
        help="Chance a random feasible pick is the optimum: 1 / C(n,k)",
    )
    st.caption(
        "Every sampler gets the same cluster problem and the same shot budget, and is scored against the exact "
        "optimum found by enumeration. 'Mass' is after the same repair step for every sampler."
    )
    concept("qaoa")
    concept("warm")
    concept("controls")

    rm = cached_rank_mass(key)
    st.subheader("Where does the probability go? (one seed)")
    fig = go.Figure()
    for i, (label, vals) in enumerate(rm["mass"].items()):
        fig.add_trace(
            go.Bar(name=label, x=[f"#{r}" for r in rm["ranks"]], y=vals, marker_color=PALETTE[i])
        )
    fig.add_hline(
        y=rm["uniform_level"],
        line_dash="dash",
        line_color="#c2410c",
        annotation_text="uniform random level",
    )
    fig.update_layout(
        barmode="group",
        height=360,
        xaxis_title="Exact rank of the selection (#1 = optimum)",
        yaxis_title="Share of shots",
        margin={"t": 10},
    )
    st.plotly_chart(fig, width="stretch")

    st.subheader("Medians over seeds")
    names = {
        "qaoa": "Cold-start QAOA",
        "qaoa_warm": "Warm-start QAOA",
        "round_relaxation": "Rounded relaxation (no quantum)",
        "random_feasible": "Uniform random (control)",
        "sa": "Simulated annealing (classical)",
    }
    rows = []
    for algo, v in study["summary"].items():
        rows.append(
            {
                "Sampler": names.get(algo, algo),
                "Kind": get_spec(algo).kind,
                "Mass on optimum": v["mass_on_best"],
                "Top-10 recall": v["top_q_recall"],
                "Raw feasible fraction": v["feasible_fraction"],
                "Found the optimum": v["found_optimum_rate"],
                "Seconds": v["seconds"],
            }
        )
    st.dataframe(
        pd.DataFrame(rows).style.format(
            {
                "Mass on optimum": "{:.3f}",
                "Top-10 recall": "{:.2f}",
                "Raw feasible fraction": "{:.2f}",
                "Found the optimum": "{:.0%}",
                "Seconds": "{:.3f}",
            },
            na_rep="n/a",
        ),
        width="stretch",
        hide_index=True,
    )
    st.caption(
        "n/a: simulated annealing returns a pool, not shot counts, so mass is undefined for it."
    )

    st.subheader("How much does warm start put on its own rounded answer?")
    st.markdown(
        "Initial probability on the single rounded bitstring is **(1 − ε)ⁿ** (closed form, not a measurement)."
    )
    tbl = pd.DataFrame(warm_start_mass_table())
    st.dataframe(
        tbl.style.format({c: "{:.1%}" for c in tbl.columns if c != "n"}),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "At the library default ε = 0.25 the start is spread over many bitstrings; a smaller ε concentrates it but leaves less for the quantum steps to do."
    )


def page_menu(s: DemoSettings, run) -> None:
    st.header("4. Portfolio menu and audit")
    banner()
    res = run.result
    u = res.universe
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Portfolios in menu", len(res.menu.portfolios))
    c2.metric(
        "Audit violations", run.violations, help="independent re-check of every hard constraint"
    )
    c3.metric(
        "Best objective", f"{res.best_objective:.5f}" if res.best_objective is not None else "n/a"
    )
    c4.metric("Pipeline seconds", f"{run.seconds:.1f}")
    if run.violations == 0:
        st.success(f"All {len(run.audit)} reported portfolios pass the independent audit.")
    else:
        st.error(
            "Audit found violations. They are listed below and the portfolio must not be used."
        )
    concept("recombination")
    concept("menu")
    if not res.menu.portfolios:
        st.error("No feasible portfolio was found for these settings.")
        return

    rows = []
    for p in res.menu.portfolios:
        rows.append(
            {
                "rank": p.rank,
                "objective": p.objective,
                "return": p.stats["expected_return"],
                "volatility": p.stats["volatility"],
                "Sharpe": p.stats["sharpe"],
                "effective N": p.stats["effective_n"],
                "avg ESG (synthetic)": float(np.mean(u.esg[p.selected_idx])),
                "max weight": p.stats["max_weight"],
            }
        )
    menu_df = pd.DataFrame(rows)
    st.subheader("Menu")
    st.dataframe(
        menu_df.style.format(
            {
                "objective": "{:.5f}",
                "return": "{:.3f}",
                "volatility": "{:.3f}",
                "Sharpe": "{:.2f}",
                "effective N": "{:.1f}",
                "avg ESG (synthetic)": "{:.2f}",
                "max weight": "{:.1%}",
            }
        ),
        hide_index=True,
        width="stretch",
    )
    pick = st.selectbox("Inspect portfolio", [p.rank for p in res.menu.portfolios])
    p = next(x for x in res.menu.portfolios if x.rank == pick)
    left, right = st.columns(2)
    with left:
        wdf = pd.DataFrame(
            {
                "asset": p.selected_ids,
                "weight": [p.weights[a] for a in p.selected_ids],
                "sector": u.sectors[p.selected_idx],
            }
        ).sort_values("weight", ascending=False)
        fig = go.Figure(go.Bar(x=wdf["asset"], y=wdf["weight"], marker_color="#3b82d4"))
        fig.update_layout(height=300, yaxis_tickformat=".0%", margin={"t": 10})
        st.plotly_chart(fig, width="stretch")
    with right:
        sec = wdf.groupby("sector")["weight"].sum().reset_index()
        fig = go.Figure(go.Pie(labels=sec["sector"], values=sec["weight"], hole=0.5))
        fig.update_layout(height=300, margin={"t": 10})
        st.plotly_chart(fig, width="stretch")
    st.json({"constraint_slack": p.constraint_slack, "explanation": p.explanation}, expanded=False)
    with st.expander("Audit detail"):
        st.dataframe(
            pd.DataFrame(
                [{**a, "violations": "; ".join(a["violations"]) or "none"} for a in run.audit]
            ),
            hide_index=True,
        )

    st.subheader("Against honest baselines")
    cmp_rows = cached_baselines(json.dumps(asdict(s)))
    cdf = pd.DataFrame(cmp_rows).rename(
        columns={
            "method": "Method",
            "best_objective": "Best objective",
            "seconds": "Seconds",
            "menu_size": "Menu size",
            "gap_to_best_found_pct": "Gap to best found (%)",
        }
    )
    st.dataframe(
        cdf[
            ["Method", "Best objective", "Gap to best found (%)", "Seconds", "Menu size"]
        ].style.format(
            {"Best objective": "{:.5f}", "Gap to best found (%)": "{:.2f}", "Seconds": "{:.3f}"},
            na_rep="n/a",
        ),
        hide_index=True,
        width="stretch",
    )
    concept("advantage")

    st.subheader("Verdict")
    study = cached_study(json.dumps(asdict(s)), 5)
    st.code(verdict_for(run, study), language=None)

    manifest = build_manifest(
        res.config.model_dump(mode="json"),
        u.data_hash(),
        res.timings,
        res.solver_usage,
        extra={"settings": asdict(s), "audit_violations": run.violations, "disclaimer": DISCLAIMER},
    )
    st.download_button(
        "Download run manifest (JSON)",
        json.dumps(_jsonable(manifest), indent=2),
        "manifest.json",
        "application/json",
    )
    with st.expander("Glossary"):
        for term, text in concepts.GLOSSARY.items():
            st.markdown(f"**{term}.** {text}")


# ----------------------------------------------------------------------------- main


def main() -> None:
    s = sidebar()
    page = st.session_state["page"]
    key = json.dumps(asdict(s))
    st.title("Hybrid quantum portfolio selection: the best pipeline, with its controls")
    try:
        if page == PAGES[2]:
            page_sampling(s)
            return
        run = cached_run(key)
        if page == PAGES[0]:
            page_problem(s, run)
        elif page == PAGES[1]:
            page_decomposition(s, run)
        else:
            page_menu(s, run)
    except Exception as exc:  # show the failure; never a blank page and never a fake result
        st.error(f"{type(exc).__name__}: {exc}")
        raise


main()
