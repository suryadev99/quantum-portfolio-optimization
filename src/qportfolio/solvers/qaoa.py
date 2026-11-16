"""F5 — QAOA sampling solver (Qiskit circuits, Sampler primitive, CVaR training).

Pipeline per cluster QUBO:
  1. QUBO → Ising (h, J);  optional coupling thinning (ablation A9).
  2. Train 2p angles *classically* with the exact NumPy twin of the circuit (:mod:`qaoa_sim`) using
     a tail-focused CVaR objective (hypothesis H4), or sampler-based CVaR for large instances.
  3. Build the Qiskit circuit (penalty: uniform start + RX mixer; xy: Dicke start + XY mixer),
     transpile for the backend, run it through the **Sampler** for many shots.
  4. Decode → repair → de-duplicate → exact classical re-scoring → keep the best (base.finalize_pool).
"""

from __future__ import annotations

import math
from functools import cached_property

import numpy as np
from qiskit import QuantumCircuit
from qiskit.circuit import ParameterVector
from qiskit.circuit.library import RYGate
from scipy.optimize import minimize

from qportfolio.config.schema import SolverConfig
from qportfolio.solvers.backends import ExecBackend, circuit_stats, counts_to_bits
from qportfolio.solvers.base import Solver, finalize_pool, trivial_pool
from qportfolio.solvers.qaoa_sim import QAOASim, qubo_to_ising_arrays, ring_pairs, warm_angles
from qportfolio.solvers.relaxation import clip_start, solve_relaxation, start_mass_on_rounded
from qportfolio.types import QUBO, CandidatePool
from qportfolio.utils.errors import ConfigError
from qportfolio.utils.logging import get_logger
from qportfolio.utils.rng import make_rng

log = get_logger("qaoa")


# ----------------------------------------------------------------------------- circuits


def _scs(qc: QuantumCircuit, m: int, k: int) -> None:
    """Split-and-cyclic-shift block of Bärtschi–Eidenbenz Dicke-state preparation."""
    qc.cx(m - 2, m - 1)
    qc.append(RYGate(2 * math.acos(math.sqrt(1 / m))).control(1), [m - 1, m - 2])
    qc.cx(m - 2, m - 1)
    for l in range(2, k + 1):  # noqa: E741 — index name follows the paper
        qc.cx(m - l - 1, m - 1)
        qc.append(RYGate(2 * math.acos(math.sqrt(l / m))).control(2), [m - 1, m - l, m - l - 1])
        qc.cx(m - l - 1, m - 1)


def dicke_circuit(n: int, k: int) -> QuantumCircuit:
    """Prepare the equal superposition of all weight-``k`` bitstrings (verified in tests)."""
    qc = QuantumCircuit(n, name=f"Dicke({n},{k})")
    if k <= 0 or k >= n:
        if k >= n:
            qc.x(range(n))
        return qc
    qc.x(range(n - k, n))
    for m in range(n, k, -1):
        _scs(qc, m, k)
    for m in range(k, 1, -1):
        _scs(qc, m, m - 1)
    return qc


def build_qaoa_circuit(
    h: np.ndarray,
    J: np.ndarray,
    p: int,
    encoding: str,
    k_free: int,
    measure: bool = True,
    start: np.ndarray | None = None,
) -> tuple[QuantumCircuit, ParameterVector, ParameterVector]:
    """QAOA circuit. ``start`` (relaxed values in [0,1]) selects the warm-start variant."""
    n = len(h)
    gam = ParameterVector("g", p)
    bet = ParameterVector("b", p)
    qc = QuantumCircuit(n, name="QAOA")
    thetas = warm_angles(start) if start is not None else None
    if thetas is not None:
        if encoding == "xy":
            raise ConfigError("warm start is incompatible with the XY encoding")
        for i in range(n):
            qc.ry(float(thetas[i]), i)
    elif encoding == "xy":
        qc.compose(dicke_circuit(n, k_free), inplace=True)
    else:
        qc.h(range(n))
    iu, ju = np.nonzero(np.triu(J, 1))
    for layer in range(p):
        for i in range(n):
            if abs(h[i]) > 1e-15:
                qc.rz(2.0 * gam[layer] * float(h[i]), i)
        for i, j in zip(iu.tolist(), ju.tolist(), strict=True):
            qc.rzz(2.0 * gam[layer] * float(J[i, j]), i, j)
        if thetas is not None:
            for i in range(n):  # Rᵧ(θ)·R_z(−2β)·Rᵧ(−θ), applied right to left
                qc.ry(-float(thetas[i]), i)
                qc.rz(-2.0 * bet[layer], i)
                qc.ry(float(thetas[i]), i)
        elif encoding == "xy":
            for a, b in ring_pairs(n):
                qc.rxx(bet[layer], a, b)
                qc.ryy(bet[layer], a, b)
        else:
            qc.rx(2.0 * bet[layer], range(n))
    if measure:
        qc.measure_all()
    return qc, gam, bet


# ----------------------------------------------------------------------------- optimisers


def _spsa(fun, x0: np.ndarray, maxiter: int, rng: np.random.Generator) -> tuple[np.ndarray, float]:
    a, c, A, alpha, gamma = 0.25, 0.15, 0.1 * maxiter, 0.602, 0.101
    x = x0.copy()
    best_x, best_f = x.copy(), fun(x)
    for k in range(maxiter):
        ak, ck = a / (k + 1 + A) ** alpha, c / (k + 1) ** gamma
        delta = rng.choice([-1.0, 1.0], size=x.size)
        fp, fm = fun(x + ck * delta), fun(x - ck * delta)
        x = x - ak * (fp - fm) / (2 * ck) * (1.0 / delta)
        f = min(fp, fm)
        if f < best_f:
            best_f = f
            best_x = (x + ck * delta).copy() if fp <= fm else (x - ck * delta).copy()
    return best_x, float(best_f)


def initial_angles(scale: float, p: int, kind: str, rng: np.random.Generator) -> np.ndarray:
    """Linear-ramp (annealing-inspired) or random initial angles, γ scaled by 1/energy spread."""
    s = max(scale, 1e-12)
    if kind == "random":
        return np.concatenate([rng.uniform(0, 1.0 / s, p), rng.uniform(0, 0.8, p)])
    t = (np.arange(p) + 0.5) / p
    return np.concatenate([t * 1.0 / s, (1 - t) * 0.6])


# ----------------------------------------------------------------------------- solver


class QAOASolver(Solver):
    name = "qaoa"

    def __init__(self, cfg: SolverConfig, backend: ExecBackend, seed: int = 0) -> None:
        self.cfg = cfg
        self.backend = backend
        self.seed = seed
        self.dry_run = False
        self.records: list[dict] = []

    # --------------------------------------------------------------- model prep
    def _ising(self, qubo: QUBO) -> tuple[np.ndarray, np.ndarray]:
        h, J = qubo_to_ising_arrays(qubo.Q)
        t = self.cfg.thin_couplings
        if t > 0:
            nz = np.abs(J[np.triu_indices_from(J, 1)])
            nz = nz[nz > 0]
            if nz.size:
                cut = np.quantile(nz, t)
                J = np.where(np.abs(J) < cut, 0.0, J)
        return h, J

    def _encoding(self, qubo: QUBO) -> str:
        if (
            self.cfg.encoding == "xy"
            and qubo.num_slack == 0
            and qubo.n_assets <= self.cfg.dicke_max_qubits
        ):
            return "xy"
        return "penalty"

    # --------------------------------------------------------------- training
    def _warm_start(self, qubo: QUBO, enc: str) -> tuple[np.ndarray | None, dict]:
        """Relaxed, clipped start vector (slack qubits start unbiased) and its audit facts."""
        if self.cfg.start != "warm" or enc != "penalty":
            return None, {"start": "cold"}
        c, rinfo = solve_relaxation(qubo)
        clipped = clip_start(c, self.cfg.warm_epsilon)
        full = np.concatenate([clipped, np.full(qubo.num_slack, 0.5)])
        info = {
            "start": "warm",
            "warm_epsilon": self.cfg.warm_epsilon,
            "start_mass_on_rounded": start_mass_on_rounded(clipped),
            **rinfo,
        }
        return full, info

    def _train(
        self,
        h: np.ndarray,
        J: np.ndarray,
        qubo: QUBO,
        enc: str,
        rng: np.random.Generator,
        start: np.ndarray | None = None,
    ) -> tuple[np.ndarray, float, str]:
        p = self.cfg.p_layers
        n = len(h)
        alpha = self.cfg.cvar_alpha
        use_sv = self.cfg.train_mode == "statevector" and n <= self.backend.limits.max_sim_qubits
        if use_sv:
            sim = QAOASim(h, J, enc, qubo.k, qubo.n_assets, start)
            spread = float(sim.diag.std())
            fun = lambda th: sim.loss(th, p, alpha)  # noqa: E731
            how = "statevector-cvar"
        else:
            sim, spread = None, float(np.sqrt((h**2).sum() + (J**2).sum()))
            circuit, gam, bet = build_qaoa_circuit(h, J, p, enc, qubo.k, start=start)
            isa = self.backend.transpile(circuit)
            Qs = (qubo.Q + qubo.Q.T) / 2.0

            def fun(th: np.ndarray) -> float:
                vals = {gam[i].name: th[i] for i in range(p)} | {
                    bet[i].name: th[p + i] for i in range(p)
                }
                counts = self.backend.sample(isa, vals, self.cfg.train_shots)
                bits, w = counts_to_bits(counts, n)
                e = np.einsum("bi,ij,bj->b", bits.astype(float), Qs, bits.astype(float))
                order = np.argsort(e)
                cw = np.cumsum(w[order])
                keep = max(1, int(np.searchsorted(cw, alpha * w.sum(), side="left")) + 1)
                return float(np.average(e[order][:keep], weights=w[order][:keep]))

            how = "sampler-cvar"

        best_th, best_f = None, np.inf
        for r in range(self.cfg.n_restarts):
            x0 = initial_angles(spread, p, self.cfg.init if r == 0 else "random", rng)
            if self.cfg.optimizer == "spsa" or not use_sv:
                th, f = _spsa(fun, x0, self.cfg.max_iterations, rng)
            else:
                res = minimize(
                    fun,
                    x0,
                    method="COBYLA",
                    options={"maxiter": self.cfg.max_iterations, "rhobeg": 0.3},
                )
                th, f = res.x, float(res.fun)
            if f < best_f:
                best_th, best_f = th, f
        return np.asarray(best_th), float(best_f), how

    # --------------------------------------------------------------- main entry
    def solve(self, qubo: QUBO, seed: int) -> CandidatePool:
        trivial = trivial_pool(qubo, self.name)
        if trivial is not None:
            return trivial
        n = qubo.num_qubits
        if (
            self._encoding(qubo) == "xy"
            and self.cfg.encoding == "xy"
            and qubo.n_assets > self.cfg.dicke_max_qubits
        ):
            log.info("cluster %s too large for XY encoding; using penalty", qubo.cluster_id)
        rng = make_rng(self.seed, "qaoa", qubo.cluster_id, seed)
        h, J = self._ising(qubo)
        enc = self._encoding(qubo)
        start, start_info = self._warm_start(qubo, enc)
        theta, loss, how = self._train(h, J, qubo, enc, rng, start)
        p = self.cfg.p_layers

        circuit, gam, bet = build_qaoa_circuit(h, J, p, enc, qubo.k, start=start)
        isa = self.backend.transpile(circuit)
        st = circuit_stats(isa)
        vals = {gam[i].name: theta[i] for i in range(p)} | {
            bet[i].name: theta[p + i] for i in range(p)
        }
        counts = self.backend.sample(isa, vals, self.cfg.shots)
        bits, w = counts_to_bits(counts, n)
        info = {
            "solver": "qaoa",
            "encoding": enc,
            "p": p,
            "shots": self.cfg.shots,
            "training": how,
            "train_loss": loss,
            "cvar_alpha": self.cfg.cvar_alpha,
            "backend": self.backend.name,
            "backend_kind": self.backend.kind,
            "qubits": n,
            "thin_couplings": self.cfg.thin_couplings,
            **start_info,
            **{f"circuit_{k}": v for k, v in st.to_dict().items() if k != "ops"},
        }
        pool = finalize_pool(
            qubo, bits, w, self.cfg.top_candidates_per_cluster, self.cfg.repair, info
        )
        self.records.append(pool.solver_info)
        return pool

    def usage(self) -> dict:
        return self.backend.usage()

    def close(self) -> None:
        self.backend.close()

    @cached_property
    def description(self) -> str:
        return f"qaoa(p={self.cfg.p_layers}, enc={self.cfg.encoding}, backend={self.backend.name})"


def estimate_resources(qubo: QUBO, cfg: SolverConfig, backend: ExecBackend) -> dict:
    """Dry-run resource estimate for one cluster: qubits, depth, gates, shots (NFR5, AC8)."""
    if qubo.k in (0, qubo.n_assets):
        return {"cluster": qubo.cluster_id, "trivial": True, "qubits": qubo.num_qubits}
    solver = QAOASolver(cfg, backend)
    h, J = solver._ising(qubo)
    enc = solver._encoding(qubo)
    start, _ = solver._warm_start(qubo, enc)
    circuit, _, _ = build_qaoa_circuit(h, J, cfg.p_layers, enc, qubo.k, start=start)
    isa = backend.transpile(circuit)
    st = circuit_stats(isa)
    if enc == "xy" and cfg.encoding == "xy":
        pass
    return {
        "cluster": qubo.cluster_id,
        "trivial": False,
        "qubits": qubo.num_qubits,
        "encoding": enc,
        "interactions": int(np.count_nonzero(np.triu(J, 1))),
        "depth": st.depth,
        "two_qubit_gates": st.two_qubit_gates,
        "shots": cfg.shots,
        "train_circuit_evals": 0
        if cfg.train_mode == "statevector"
        else cfg.max_iterations * 2 * cfg.n_restarts,
    }


def validate_solver_cfg(cfg: SolverConfig) -> None:
    if cfg.encoding == "xy" and cfg.dicke_max_qubits > 20:
        raise ConfigError("dicke_max_qubits > 20 is unsupported")
