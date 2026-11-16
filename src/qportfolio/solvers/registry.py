"""Registry of sub-problem samplers (spec C9, build step P0).

Every registered algorithm turns one cluster QUBO into a candidate pool through the common
``Solver`` interface. The same exact re-scoring, repair, recombination and audit then run for every
algorithm, so adding one needs no change to the pipeline, the demo or the benchmark runner (C9-R4).

Each entry declares its kind, size limit and cost model (C9-R3) so they can be shown and recorded.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from qportfolio.config.schema import SolverConfig
from qportfolio.solvers.base import Solver


@dataclass(frozen=True)
class SolverSpec:
    name: str
    factory: Callable[[SolverConfig, int], Solver]
    description: str
    kind: str  # "quantum" | "classical" | "control"
    max_qubits: int | None  # None = no hard limit declared
    cost: str  # short cost-model note

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "max_qubits": self.max_qubits,
            "cost": self.cost,
        }


_REGISTRY: dict[str, SolverSpec] = {}


def register_solver(spec: SolverSpec, replace: bool = False) -> None:
    if spec.name in _REGISTRY and not replace:
        raise ValueError(f"solver {spec.name!r} is already registered")
    _REGISTRY[spec.name] = spec


def available_solvers() -> list[str]:
    return sorted(_REGISTRY)


def get_spec(name: str) -> SolverSpec:
    if name not in _REGISTRY:
        raise ValueError(f"unknown solver algorithm {name!r}; registered: {available_solvers()}")
    return _REGISTRY[name]


def build_solver(name: str, cfg: SolverConfig, seed: int) -> Solver:
    return get_spec(name).factory(cfg, seed)


# ----------------------------------------------------------------------------- built-ins


def _qaoa(cfg: SolverConfig, seed: int) -> Solver:
    from qportfolio.solvers.backends import ExecBackend
    from qportfolio.solvers.qaoa import QAOASolver

    backend = ExecBackend(
        cfg.backend, cfg.mitigation, cfg.limits, opt_level=cfg.transpile_opt_level, seed=seed
    )
    return QAOASolver(cfg, backend, seed)


def _qaoa_warm(cfg: SolverConfig, seed: int) -> Solver:
    return _qaoa(cfg.model_copy(update={"start": "warm", "encoding": "penalty"}), seed)


def _sa(cfg: SolverConfig, seed: int) -> Solver:
    from qportfolio.solvers.classical import SASolver

    return SASolver(cfg, seed)


def _exact(cfg: SolverConfig, seed: int) -> Solver:
    from qportfolio.solvers.classical import ExactSolver

    return ExactSolver(cfg, seed)


def _random(cfg: SolverConfig, seed: int) -> Solver:
    from qportfolio.solvers.controls import RandomFeasibleSolver

    return RandomFeasibleSolver(cfg, seed)


def _round(cfg: SolverConfig, seed: int) -> Solver:
    from qportfolio.solvers.controls import RoundRelaxationSolver

    return RoundRelaxationSolver(cfg, seed)


def _register_builtins() -> None:
    for spec in (
        SolverSpec(
            "qaoa",
            _qaoa,
            "CVaR-trained QAOA sampler (cold or, via solver.start, warm start)",
            "quantum",
            24,
            "statevector twin O(2^n) to train; Sampler shots to sample",
        ),
        SolverSpec(
            "qaoa_warm",
            _qaoa_warm,
            "QAOA warm-started from the convex relaxation (penalty encoding)",
            "quantum",
            24,
            "as qaoa + one convex QP per cluster",
        ),
        SolverSpec(
            "sa",
            _sa,
            "Swap-move simulated annealing (classical sampler)",
            "classical",
            None,
            "O(restarts * sweeps * n^2)",
        ),
        SolverSpec(
            "exact",
            _exact,
            "Exhaustive enumeration of weight-k selections (ground truth, falls back to SA)",
            "classical",
            None,
            "O(C(n,k))",
        ),
        SolverSpec(
            "random_feasible",
            _random,
            "Uniformly random weight-k selections at equal shot budget (mandatory control)",
            "control",
            None,
            "O(shots * n)",
        ),
        SolverSpec(
            "round_relaxation",
            _round,
            "Rounded convex relaxation plus equal-budget biased sampling (no quantum step)",
            "control",
            None,
            "one convex QP + O(shots * n)",
        ),
    ):
        register_solver(spec, replace=True)


_register_builtins()
