"""ParlaySports: multi-sport parlay research, simulation, tracking & competition.

Paper-trading research platform only. No real-money wagering. No order
placement. Every price, score and result carries source + timestamp +
verification status. Model estimates are always labeled as models.
"""
from .config import (
    SPORTS,
    STARTING_BANKROLL,
    UNIT_STAKE,
    LOTTO_STAKE,
    SEASON_STATE,
    __version__,
)

__all__ = [
    "SPORTS",
    "STARTING_BANKROLL",
    "UNIT_STAKE",
    "LOTTO_STAKE",
    "SEASON_STATE",
    "__version__",
]
