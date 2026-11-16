"""Deterministic seed derivation (NFR1)."""

from __future__ import annotations

import hashlib

import numpy as np


def _salt_to_int(salt: object) -> int:
    if isinstance(salt, int):
        return salt & 0xFFFFFFFF
    digest = hashlib.sha256(str(salt).encode()).digest()
    return int.from_bytes(digest[:4], "little")


def derive_seed(seed: int, *salt: object) -> int:
    """Derive a stable 32-bit child seed from ``seed`` and arbitrary salt values."""
    ss = np.random.SeedSequence([seed & 0xFFFFFFFF, *[_salt_to_int(s) for s in salt]])
    return int(ss.generate_state(1)[0])


def make_rng(seed: int, *salt: object) -> np.random.Generator:
    return np.random.default_rng(derive_seed(seed, *salt))
