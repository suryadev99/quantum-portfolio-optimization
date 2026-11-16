"""Configuration schema (spec §14). Credentials are never part of the config (NFR6)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunConfig(_Strict):
    name: str = "run"
    seed: int = 42
    output_dir: str = "outputs/run"
    log_level: str = "INFO"
    n_jobs: int = 1  # parallel workers for classical parts of the solver stage (-1 = all cores)


class SyntheticConfig(_Strict):
    n_assets: int = Field(100, ge=4)
    n_sectors: int = Field(8, ge=1)
    n_factors: int = Field(3, ge=1)
    n_days: int = Field(750, ge=30)
    noise_level: float = Field(1.0, gt=0)
    sector_names: list[str] | None = None


class DataConfig(_Strict):
    source: Literal["synthetic", "csv", "parquet"] = "synthetic"
    path: str | None = None
    attributes_path: str | None = None
    min_history_days: int = 250
    missing_policy: Literal["drop", "forward_fill", "reject"] = "forward_fill"
    max_missing_frac: float = Field(0.1, ge=0, le=1)
    synthetic: SyntheticConfig = SyntheticConfig()


class EstimationConfig(_Strict):
    covariance: Literal["sample", "ledoit_wolf", "oas"] = "ledoit_wolf"
    annualize: bool = True
    periods_per_year: int = 252
    mu_shrinkage: float = Field(0.5, ge=0, le=1)
    mu_path: str | None = None  # user-supplied mu (F2-R4); CSV/Parquet single column
    sigma_path: str | None = None  # user-supplied Σ (F2-R4); CSV/Parquet square matrix


class MandateConfig(_Strict):
    K: int = Field(..., ge=1)
    lambda_risk: float = Field(3.0, ge=0)
    w_max: float = Field(0.1, gt=0, le=1)
    sector_caps: dict[str, float] = {}
    turnover_limit: float | None = Field(None, gt=0, le=1)
    min_esg: float | None = None
    forced_in: list[str] = []
    forced_out: list[str] = []

    @field_validator("sector_caps")
    @classmethod
    def _caps_in_range(cls, v: dict[str, float]) -> dict[str, float]:
        for s, c in v.items():
            if not 0 < c <= 1:
                raise ValueError(f"sector cap for {s!r} must be in (0, 1], got {c}")
        return v


class DecompositionConfig(_Strict):
    clusterer: Literal["hierarchical", "spectral", "modularity"] = "hierarchical"
    linkage: Literal["average", "complete", "ward"] = "average"
    max_cluster_size: int = Field(14, ge=2)
    k_allocation: Literal["proportional", "risk_aware", "user"] = "risk_aware"
    user_k_allocation: list[int] | None = None
    k_neighbor_delta: int = Field(1, ge=0)
    max_neighbor_allocations: int = Field(6, ge=0)
    remove_market_mode: bool = True
    n_factors_removed: int = Field(1, ge=0)
    # Matrix used ONLY to find clusters (scoring always uses the original Σ). ``market_mode`` is the
    # historical behaviour; ``marchenko_pastur`` keeps only eigen-modes above the MP noise edge λ₊.
    denoise: Literal["market_mode", "marchenko_pastur"] = "market_mode"
    mp_fit: Literal["fixed", "market_adjusted"] = "fixed"
    # ``norm_ratio`` rescales the risk aversion used inside cluster sub-problems (JPMorgan/Amazon Eq. 15).
    risk_rebalance: Literal["none", "norm_ratio"] = "none"
    sector_homogeneous: bool = False


class QuboConfig(_Strict):
    inequality_handling: Literal["recombination", "slack"] = "recombination"
    penalty_weight: float | Literal["auto"] = "auto"
    penalty_scale: float = Field(1.0, gt=0)


class BackendConfig(_Strict):
    type: Literal["simulator", "noisy_simulator", "ibm_hardware"] = "simulator"
    # simulator: ignored; noisy_simulator/ibm_hardware: backend name, e.g. "fake_sherbrooke" or
    # "ibm_brisbane". "fake_*" names run locally (no credentials); None => least busy device.
    name: str | None = None
    channel: str | None = None
    instance: str | None = None
    mode: Literal["job", "batch", "session"] = "batch"
    min_qubits: int | None = None


class MitigationConfig(_Strict):
    enabled: bool = False
    dynamical_decoupling: bool = True
    twirling: bool = True


class LimitsConfig(_Strict):
    max_qpu_seconds: float = 600.0
    max_jobs: int = 200
    max_sim_qubits: int = 24


class SolverConfig(_Strict):
    # Name of a registered sub-problem sampler (qportfolio.solvers.registry); validated below.
    algorithm: str = "qaoa"
    p_layers: int = Field(2, ge=1, le=20)
    encoding: Literal["penalty", "xy"] = "penalty"
    init: Literal["ramp", "random"] = "ramp"  # initial *angles* (not the quantum start state)
    # Quantum start state: ``cold`` = uniform superposition; ``warm`` = biased by the classical
    # convex relaxation (Egger et al.), clipped to [warm_epsilon, 1 - warm_epsilon].
    start: Literal["cold", "warm"] = "cold"
    warm_epsilon: float = Field(0.25, gt=0, le=0.5)
    optimizer: Literal["cobyla", "spsa"] = "cobyla"
    max_iterations: int = Field(60, ge=1)
    n_restarts: int = Field(1, ge=1)
    cvar_alpha: float = Field(0.25, gt=0, le=1)
    train_shots: int = Field(1024, ge=1)
    shots: int = Field(4096, ge=1)
    top_candidates_per_cluster: int = Field(50, ge=1)
    train_mode: Literal["statevector", "sampler"] = "statevector"
    dicke_max_qubits: int = Field(12, ge=2)
    repair: bool = True
    transpile_opt_level: int = Field(1, ge=0, le=3)
    thin_couplings: float = Field(0.0, ge=0, lt=1)
    backend: BackendConfig = BackendConfig()
    mitigation: MitigationConfig = MitigationConfig()
    limits: LimitsConfig = LimitsConfig()
    sa_sweeps: int = Field(300, ge=1)
    sa_restarts: int = Field(32, ge=1)
    exact_max_combinations: int = Field(2_000_000, ge=1)

    @field_validator("algorithm")
    @classmethod
    def _known_algorithm(cls, v: str) -> str:
        from qportfolio.solvers.registry import available_solvers  # lazy: avoids an import cycle

        if v not in available_solvers():
            raise ValueError(f"unknown solver algorithm {v!r}; registered: {available_solvers()}")
        return v


class RecombinationConfig(_Strict):
    method: Literal["beam"] = "beam"
    beam_width: int = Field(200, ge=1)
    top_r: int = Field(500, ge=1)
    local_polish: bool = True
    polish_top: int = Field(50, ge=0)
    max_polish_iters: int = Field(50, ge=1)
    # Solution-pool expansion: after polishing, add feasible single-swap neighbours of the best
    # portfolios that stay within `expansion_pct` % of the best objective (classical step; the
    # benchmark ablates it so its contribution is never credited to the quantum sampler).
    expand_neighbourhood: bool = True
    expansion_pct: float = Field(5.0, ge=0)
    expansion_seeds: int = Field(5, ge=1)
    expansion_max: int = Field(600, ge=1)


class RankingConfig(_Strict):
    method: Literal["weighted", "lexicographic"] = "weighted"
    weights: dict[str, float] = {"liquidity": 0.4, "esg": 0.3, "turnover": 0.3}
    order: list[str] = ["objective"]


class MenuConfig(_Strict):
    size: int = Field(10, ge=1)
    epsilon_mode: Literal["pct", "abs", "uncertainty"] = "pct"
    epsilon_pct: float = Field(2.0, ge=0)
    epsilon_abs: float = Field(0.0, ge=0)
    epsilon_z: float = Field(1.0, ge=0)
    distance: Literal["jaccard", "hamming"] = "jaccard"
    min_distance: float = Field(0.05, ge=0, le=1)
    ranking: RankingConfig = RankingConfig()


class BenchmarkConfig(_Strict):
    enabled: bool = False
    baselines: list[str] = [
        "equal_weight",
        "greedy",
        "miqp_timelimit",
        "sa_qubo",
        "decomposed_classical",
        "multistart_classical",
    ]
    seeds: int = Field(5, ge=1)
    miqp_time_limit: float = Field(10.0, gt=0)
    include_quantum: bool = True


class Config(_Strict):
    run: RunConfig = RunConfig()
    data: DataConfig = DataConfig()
    estimation: EstimationConfig = EstimationConfig()
    mandate: MandateConfig
    decomposition: DecompositionConfig = DecompositionConfig()
    qubo: QuboConfig = QuboConfig()
    solver: SolverConfig = SolverConfig()
    recombination: RecombinationConfig = RecombinationConfig()
    menu: MenuConfig = MenuConfig()
    benchmark: BenchmarkConfig = BenchmarkConfig()

    @model_validator(mode="after")
    def _cross_checks(self) -> Config:
        if self.menu.size < 1:
            raise ValueError("menu.size must be >= 1")
        if self.decomposition.k_allocation == "user" and not self.decomposition.user_k_allocation:
            raise ValueError("decomposition.user_k_allocation required when k_allocation='user'")
        if self.solver.start == "warm" and self.solver.encoding == "xy":
            raise ValueError(
                "solver.start='warm' replaces the mixer and does not preserve Hamming weight; "
                "use encoding='penalty' (spec C5-R4)"
            )
        return self
