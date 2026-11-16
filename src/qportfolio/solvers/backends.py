"""F5 — execution backends: ideal simulator, noisy simulator, IBM hardware (F5-R3, R9, R10).

All three share one interface so the same pipeline code runs on any of them:

* ``transpile(circuit)``  → circuit adapted to the target (layout / routing / basis gates)
* ``sample(isa, values, shots)`` → ``{bitstring: count}`` (Qiskit little-endian keys, qubit 0 rightmost)

``fake_*`` device names (e.g. ``fake_sherbrooke``) run locally on an Aer noise model derived from
that device's snapshot — they exercise the full hardware code path with no credentials.
Credentials for real devices are *never* read from the config: they come from the environment
(``QISKIT_IBM_TOKEN`` / ``QISKIT_IBM_INSTANCE`` / ``QISKIT_IBM_CHANNEL``) or a saved account.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from qiskit import QuantumCircuit
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

from qportfolio.config.schema import BackendConfig, LimitsConfig, MitigationConfig
from qportfolio.utils.errors import BackendError, BudgetExceeded
from qportfolio.utils.logging import get_logger

log = get_logger("backend")

DEFAULT_FAKE = "fake_sherbrooke"


def get_fake_backend(name: str):
    """``fake_sherbrooke`` → ``FakeSherbrooke()`` from the Runtime fake provider."""
    from qiskit_ibm_runtime import fake_provider

    cls_name = "".join(part.capitalize() for part in name.split("_"))
    cls = getattr(fake_provider, cls_name, None)
    if cls is None:
        avail = sorted(n for n in dir(fake_provider) if n.startswith("Fake") and "V2" not in n)
        raise BackendError(f"unknown fake backend {name!r}; available: {avail[:12]}…")
    return cls()


@dataclass
class CircuitStats:
    depth: int
    two_qubit_gates: int
    size: int
    qubits_used: int
    ops: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "two_qubit_gates": self.two_qubit_gates,
            "size": self.size,
            "qubits_used": self.qubits_used,
            "ops": self.ops,
        }


def circuit_stats(isa: QuantumCircuit) -> CircuitStats:
    used = {isa.find_bit(q).index for inst in isa.data for q in inst.qubits}
    ops = {k: int(v) for k, v in isa.count_ops().items()}
    return CircuitStats(
        depth=int(isa.depth()),
        two_qubit_gates=int(isa.num_nonlocal_gates()),
        size=int(isa.size()),
        qubits_used=len(used),
        ops=ops,
    )


class ExecBackend:
    """Uniform wrapper over simulator / noisy simulator / IBM Runtime with budget tracking."""

    def __init__(
        self,
        cfg: BackendConfig,
        mitigation: MitigationConfig,
        limits: LimitsConfig,
        opt_level: int = 1,
        seed: int = 0,
        allow_fallback: bool = False,
    ) -> None:
        self.cfg = cfg
        self.mitigation = mitigation
        self.limits = limits
        self.opt_level = opt_level
        self.seed = seed
        self.kind = cfg.type
        self.name = "aer_simulator"
        self.jobs_used = 0
        self.qpu_seconds = 0.0
        self.shots_used = 0
        self.job_ids: list[str] = []
        self.notes: list[str] = []
        self._pm = None
        self._sampler = None
        self._mode = None
        self._device = None  # fake or real backend providing the target
        self._service = None
        self._setup(allow_fallback)

    # ------------------------------------------------------------------ setup
    def _setup(self, allow_fallback: bool) -> None:
        from qiskit_aer import AerSimulator
        from qiskit_aer.noise import NoiseModel
        from qiskit_aer.primitives import SamplerV2 as AerSampler

        kind = self.cfg.type
        if kind == "simulator":
            self._device = AerSimulator()
            self._sampler = AerSampler(default_shots=1024, seed=self.seed)
            self.name = "aer_simulator"
            return

        want = self.cfg.name
        is_fake = bool(want) and want.lower().startswith("fake_")
        if kind == "noisy_simulator" or is_fake:
            fake = get_fake_backend(want if want else DEFAULT_FAKE)
            nm = NoiseModel.from_backend(fake)
            self._device = fake
            self._sampler = AerSampler(
                default_shots=1024,
                seed=self.seed,
                options={
                    "backend_options": {
                        "noise_model": nm,
                        "coupling_map": fake.coupling_map,
                        "basis_gates": nm.basis_gates,
                    }
                },
            )
            self.name = fake.name
            self.kind = "noisy_simulator"
            if kind == "ibm_hardware":
                self.notes.append(f"{want} is a local noise-model device (no QPU time used)")
            return

        # ---- real IBM hardware
        try:
            from qiskit_ibm_runtime import QiskitRuntimeService

            kwargs: dict[str, Any] = {}
            if self.cfg.channel:
                kwargs["channel"] = self.cfg.channel
            if self.cfg.instance:
                kwargs["instance"] = self.cfg.instance
            self._service = QiskitRuntimeService(**kwargs)
            if want:
                self._device = self._service.backend(want)
            else:
                self._device = self._service.least_busy(
                    min_num_qubits=self.cfg.min_qubits, operational=True, simulator=False
                )
            self.name = self._device.name
        except Exception as exc:
            if allow_fallback:
                fake = get_fake_backend(DEFAULT_FAKE)
                self._device = fake
                self.name = fake.name
                self.kind = "noisy_simulator"
                self.notes.append(
                    f"IBM Runtime unavailable ({type(exc).__name__}); using {fake.name} for "
                    "dry-run resource estimates only"
                )
                self._sampler = None
                return
            raise BackendError(
                "Could not connect to IBM Quantum. Provide credentials via the environment "
                "(QISKIT_IBM_TOKEN, QISKIT_IBM_INSTANCE) or a saved account — never the config. "
                f"Cause: {exc}"
            ) from exc

    def _ensure_runtime_sampler(self):
        if self._sampler is not None:
            return self._sampler
        from qiskit_ibm_runtime import Batch, Session
        from qiskit_ibm_runtime import SamplerV2 as RTSampler

        mode = self.cfg.mode
        if mode == "batch":
            self._mode = Batch(backend=self._device)
        elif mode == "session":
            self._mode = Session(backend=self._device)
        else:
            self._mode = self._device
        sampler = RTSampler(mode=self._mode)
        if self.mitigation.enabled:
            if self.mitigation.dynamical_decoupling:
                sampler.options.dynamical_decoupling.enable = True
                sampler.options.dynamical_decoupling.sequence_type = "XpXm"
            if self.mitigation.twirling:
                sampler.options.twirling.enable_gates = True
                sampler.options.twirling.enable_measure = True
        self._sampler = sampler
        return sampler

    # ------------------------------------------------------------------ transpile
    def transpile(self, circuit: QuantumCircuit) -> QuantumCircuit:
        if self._pm is None:
            self._pm = generate_preset_pass_manager(
                optimization_level=self.opt_level, backend=self._device, seed_transpiler=self.seed
            )
        return self._pm.run(circuit)

    # ------------------------------------------------------------------ budgets
    def _check_budget(self) -> None:
        if self.jobs_used >= self.limits.max_jobs:
            raise BudgetExceeded(f"max_jobs={self.limits.max_jobs} reached")
        if self.qpu_seconds >= self.limits.max_qpu_seconds > 0 and self.kind == "ibm_hardware":
            raise BudgetExceeded(
                f"max_qpu_seconds={self.limits.max_qpu_seconds} reached "
                f"({self.qpu_seconds:.1f}s used)"
            )

    # ------------------------------------------------------------------ execution
    def sample(
        self, isa: QuantumCircuit, values: dict[str, float] | None, shots: int
    ) -> dict[str, int]:
        """Run ``isa`` bound to ``values`` and return bitstring counts (F5-R9)."""
        self._check_budget()
        if values is not None and isa.parameters:
            ordered = np.array([values[p.name] for p in isa.parameters], dtype=float)
            pub = (isa, ordered)
        else:
            pub = (isa,)
        if self.kind == "ibm_hardware":
            return self._sample_runtime(pub, shots)
        sampler = self._sampler
        t0 = time.perf_counter()
        res = sampler.run([pub], shots=shots).result()[0]
        self.jobs_used += 1
        self.shots_used += shots
        self.qpu_seconds += 0.0
        log.debug("sim job %d done in %.3fs", self.jobs_used, time.perf_counter() - t0)
        return _counts(res)

    def _sample_runtime(self, pub: tuple, shots: int) -> dict[str, int]:
        sampler = self._ensure_runtime_sampler()
        last: Exception | None = None
        for attempt in range(3):  # retry transient failures (F5-R9)
            try:
                job = sampler.run([pub], shots=shots)
                self.job_ids.append(job.job_id())
                res = job.result()[0]
                with contextlib.suppress(Exception):
                    self.qpu_seconds += float(job.usage())
                self.jobs_used += 1
                self.shots_used += shots
                return _counts(res)
            except BudgetExceeded:
                raise
            except Exception as exc:
                last = exc
                log.warning("runtime job failed (attempt %d/3): %s", attempt + 1, exc)
                time.sleep(min(2**attempt, 8))
        raise BackendError(f"runtime job failed after retries: {last}")

    def close(self) -> None:
        mode = self._mode
        if mode is not None and hasattr(mode, "close"):
            with contextlib.suppress(Exception):
                mode.close()

    def usage(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "kind": self.kind,
            "jobs": self.jobs_used,
            "shots": self.shots_used,
            "qpu_seconds": round(self.qpu_seconds, 3),
            "job_ids": list(self.job_ids),
            "notes": list(self.notes),
        }

    @property
    def num_qubits(self) -> int | None:
        return getattr(self._device, "num_qubits", None)


def _counts(pub_result) -> dict[str, int]:
    data = pub_result.data
    reg = getattr(data, "meas", None)
    if reg is None:
        reg = data[next(iter(data.keys()))] if hasattr(data, "keys") else next(iter(data.values()))
    return {k: int(v) for k, v in reg.get_counts().items()}


def counts_to_bits(counts: dict[str, int], n_keep: int) -> tuple[np.ndarray, np.ndarray]:
    """Decode counts into a (M, n_keep) int8 bit matrix (variable i = qubit i) and weights."""
    keys = list(counts.keys())
    if not keys:
        return np.zeros((0, n_keep), dtype=np.int8), np.zeros(0, dtype=np.int64)
    width = len(keys[0].replace(" ", ""))
    arr = np.array([[ch == "1" for ch in k.replace(" ", "")[::-1]] for k in keys], dtype=np.int8)
    arr = arr[:, :n_keep] if width >= n_keep else np.pad(arr, ((0, 0), (0, n_keep - width)))
    return arr, np.array([counts[k] for k in keys], dtype=np.int64)
