"""Exception hierarchy for qportfolio."""

from __future__ import annotations


class QPortfolioError(Exception):
    """Base class for all qportfolio errors."""


class ConfigError(QPortfolioError):
    """Invalid or inconsistent configuration."""


class DataError(QPortfolioError):
    """Invalid input data."""


class InfeasibleError(QPortfolioError):
    """The mandate cannot be satisfied (F1-R6, NFR8: fail loudly)."""


class BackendError(QPortfolioError):
    """Backend / credential / transpilation problem."""


class BudgetExceeded(QPortfolioError):
    """A configured QPU-time or job budget was exhausted (F5-R10)."""
