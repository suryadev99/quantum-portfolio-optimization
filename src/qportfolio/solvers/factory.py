"""Solver factory and parallel cluster execution (F5-R13)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from qportfolio.config.schema import Config, SolverConfig
from qportfolio.solvers.base import Solver
from qportfolio.solvers.qaoa import QAOASolver
from qportfolio.solvers.registry import build_solver
from qportfolio.types import QUBO, CandidatePool


def make_solver(cfg: SolverConfig, seed: int, algorithm: str | None = None) -> Solver:
    """Build the registered sampler named by ``algorithm`` (default ``cfg.algorithm``)."""
    return build_solver(algorithm or cfg.algorithm, cfg, seed)


def solve_all(solver: Solver, qubos: list[QUBO], seed: int, n_jobs: int = 1) -> list[CandidatePool]:
    """Solve independent cluster problems; classical solvers can run in parallel threads.

    QAOA runs sequentially: the sampler backend is a shared, stateful resource with a job budget.
    """
    if n_jobs == 1 or isinstance(solver, QAOASolver) or len(qubos) < 2:
        return [solver.solve(q, seed) for q in qubos]
    workers = None if n_jobs < 0 else n_jobs
    with ThreadPoolExecutor(max_workers=workers) as ex:  # numba releases the GIL only partially
        return list(ex.map(lambda q: solver.solve(q, seed), qubos))


__all__ = ["Config", "make_solver", "solve_all"]
