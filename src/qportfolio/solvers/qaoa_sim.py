"""Fast exact QAOA simulation in NumPy, used to *train* circuit parameters (classically).

This mirrors the Qiskit circuit built in :mod:`qportfolio.solvers.qaoa` gate for gate (same cost
phases, same mixer ordering), so parameters trained here transfer to the circuit run on the backend.
A unit test checks equality against Qiskit's ``Statevector``.

Little-endian convention (Qiskit): variable/qubit ``i`` is bit ``i`` of the basis-state index.
"""

from __future__ import annotations

import numpy as np

from qportfolio.formulation.qubo import all_states


def qubo_to_ising_arrays(Q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``x'Qx`` (x∈{0,1}) → ``Σ h_i Z_i + Σ_{i<j} J_ij Z_i Z_j`` (+const), ``x=(1−Z)/2``."""
    Qs = (Q + Q.T) / 2.0
    diag = np.diag(Qs)
    off = Qs - np.diag(diag)
    h = -(diag + off.sum(axis=1)) / 2.0
    J = np.triu(off, 1) / 2.0
    return h, J


def ising_diagonal(h: np.ndarray, J: np.ndarray) -> np.ndarray:
    """Diagonal of the Ising Hamiltonian over all 2^n basis states (O(2^n·n))."""
    n = len(h)
    d = np.zeros(1)
    for i in range(n):
        z_prev = 1.0 - 2.0 * all_states(i).astype(float) if i else np.zeros((1, 0))
        field = h[i] + (z_prev @ J[:i, i] if i else 0.0)
        d = np.concatenate([d + field, d - field])
    return d


def uniform_state(n: int) -> np.ndarray:
    return np.full(1 << n, 1.0 / np.sqrt(1 << n), dtype=np.complex128)


def dicke_state(n: int, k: int) -> np.ndarray:
    pop = all_states(n).sum(axis=1)
    psi = (pop == k).astype(np.complex128)
    return psi / np.linalg.norm(psi)


def warm_angles(start: np.ndarray) -> np.ndarray:
    """θᵢ = 2·arcsin(√cᵢ): the Rᵧ(θᵢ)|0⟩ rotation whose P(1) equals the relaxed value cᵢ."""
    return 2.0 * np.arcsin(np.sqrt(np.clip(np.asarray(start, dtype=float), 0.0, 1.0)))


def warm_state(start: np.ndarray) -> np.ndarray:
    """Product state ⊗ᵢ Rᵧ(θᵢ)|0⟩ with qubit i = bit i (little-endian, like Qiskit)."""
    psi = np.ones(1, dtype=np.complex128)
    for theta in warm_angles(start):
        qubit = np.array([np.cos(theta / 2.0), np.sin(theta / 2.0)], dtype=np.complex128)
        psi = np.kron(qubit, psi)  # the new qubit becomes the most significant bit so far
    return psi


def _ry(theta: float) -> np.ndarray:
    c, s = np.cos(theta / 2.0), np.sin(theta / 2.0)
    return np.array([[c, -s], [s, c]], dtype=np.complex128)


def apply_warm_mixer(psi: np.ndarray, beta: float, thetas: np.ndarray) -> np.ndarray:
    """Per-qubit ``Rᵧ(θᵢ)·R_z(−2β)·Rᵧ(−θᵢ)``, whose ground state is the warm start (Egger et al.)."""
    n = len(thetas)
    rz = np.diag([np.exp(1j * beta), np.exp(-1j * beta)])  # R_z(-2β)
    for q in range(n):
        m = _ry(thetas[q]) @ rz @ _ry(-thetas[q])
        v = psi.reshape(1 << (n - q - 1), 2, 1 << q)
        psi = np.einsum("ab,xby->xay", m, v).reshape(-1)
    return psi


def apply_rx_all(psi: np.ndarray, beta: float, n: int) -> np.ndarray:
    """Apply ``RX(2β)`` to every qubit (the standard QAOA mixer)."""
    c, s = np.cos(beta), -1j * np.sin(beta)
    for q in range(n):
        v = psi.reshape(1 << (n - q - 1), 2, 1 << q)
        a0, a1 = v[:, 0, :], v[:, 1, :]
        psi = np.stack([c * a0 + s * a1, s * a0 + c * a1], axis=1).reshape(-1)
    return psi


def ring_pairs(n: int) -> list[tuple[int, int]]:
    """Even-then-odd ring ordering of XY-mixer pairs (identical in circuit and simulator)."""
    if n < 2:
        return []
    if n == 2:
        return [(0, 1)]
    even = [(i, (i + 1) % n) for i in range(0, n - 1, 2)]
    odd = [(i, (i + 1) % n) for i in range(1, n, 2)]
    if n % 2 == 1:
        # odd ring: the wrap-around pair (n-1, 0) closes the cycle
        odd = [(i, (i + 1) % n) for i in range(1, n - 1, 2)] + [(n - 1, 0)]
    return even + odd


class XYMixer:
    """Precomputed index maps for ``exp(-iβ(XX+YY)/2)`` on each ring pair."""

    def __init__(self, n: int) -> None:
        self.n = n
        idx = np.arange(1 << n)
        self.maps: list[tuple[np.ndarray, np.ndarray]] = []
        for a, b in ring_pairs(n):
            ba = (idx >> a) & 1
            bb = (idx >> b) & 1
            i01 = idx[(ba == 0) & (bb == 1)]
            i10 = i01 ^ ((1 << a) | (1 << b))
            self.maps.append((i01, i10))

    def apply(self, psi: np.ndarray, beta: float) -> np.ndarray:
        c, s = np.cos(beta), -1j * np.sin(beta)
        psi = psi.copy()
        for i01, i10 in self.maps:
            u, v = psi[i01], psi[i10]
            psi[i01] = c * u + s * v
            psi[i10] = s * u + c * v
        return psi


class QAOASim:
    """Exact statevector QAOA with CVaR (tail-focused) or mean objective."""

    def __init__(
        self,
        h: np.ndarray,
        J: np.ndarray,
        encoding: str,
        k_free: int,
        n_assets: int,
        start: np.ndarray | None = None,
    ) -> None:
        self.n = len(h)
        self.encoding = encoding
        self.diag = ising_diagonal(h, J)
        self.order = np.argsort(self.diag, kind="stable")
        self.sorted_e = self.diag[self.order]
        self.warm_thetas = warm_angles(start) if start is not None else None
        if start is not None:
            if encoding == "xy":
                raise ValueError("warm start is incompatible with the XY encoding")
            self.psi0 = warm_state(start)
            self.mixer = None
        elif encoding == "xy":
            self.psi0 = dicke_state(self.n, k_free)
            self.mixer = XYMixer(self.n)
        else:
            self.psi0 = uniform_state(self.n)
            self.mixer = None

    def state(self, gammas: np.ndarray, betas: np.ndarray) -> np.ndarray:
        psi = self.psi0
        for g, b in zip(gammas, betas, strict=True):
            psi = psi * np.exp(-1j * g * self.diag)
            if self.warm_thetas is not None:
                psi = apply_warm_mixer(psi, b, self.warm_thetas)
            elif self.mixer:
                psi = self.mixer.apply(psi, b)
            else:
                psi = apply_rx_all(psi, b, self.n)
        return psi

    def probs(self, gammas: np.ndarray, betas: np.ndarray) -> np.ndarray:
        psi = self.state(gammas, betas)
        p = psi.real**2 + psi.imag**2
        return p / p.sum()

    def cvar(self, probs: np.ndarray, alpha: float) -> float:
        """Average energy over the best ``alpha`` probability mass (alpha=1 → plain mean)."""
        if alpha >= 1.0:
            return float(probs @ self.diag)
        ps = probs[self.order]
        cum = np.cumsum(ps)
        j = int(np.searchsorted(cum, alpha, side="left"))
        j = min(j, len(ps) - 1)
        before = cum[j - 1] if j > 0 else 0.0
        tot = float(ps[:j] @ self.sorted_e[:j]) + (alpha - before) * self.sorted_e[j]
        return tot / alpha

    def loss(self, theta: np.ndarray, p: int, alpha: float) -> float:
        probs = self.probs(theta[:p], theta[p:])
        return self.cvar(probs, alpha)
