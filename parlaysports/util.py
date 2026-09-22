"""Pure helpers: timestamps, hashing, odds math, probability math.

No I/O, no network, no database. Everything here is unit-tested.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone


# ---------------------------------------------------------------- timestamps
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_file_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------- odds math
def american_to_decimal(odds: float) -> float:
    """American odds -> decimal odds (includes stake)."""
    o = float(odds)
    if o == 0:
        raise ValueError("american odds cannot be 0")
    if o > 0:
        return 1.0 + o / 100.0
    return 1.0 + 100.0 / abs(o)


def american_to_prob(odds: float) -> float:
    """American odds -> implied probability (with the book's hold still in)."""
    o = float(odds)
    if o == 0:
        raise ValueError("american odds cannot be 0")
    if o > 0:
        return 100.0 / (o + 100.0)
    return abs(o) / (abs(o) + 100.0)


def prob_to_decimal(p: float) -> float:
    p = float(p)
    if not 0.0 < p < 1.0:
        raise ValueError(f"probability out of range: {p}")
    return 1.0 / p


def prob_to_american(p: float) -> float:
    p = float(p)
    if not 0.0 < p < 1.0:
        raise ValueError(f"probability out of range: {p}")
    if p >= 0.5:
        return -100.0 * p / (1.0 - p)
    return 100.0 * (1.0 - p) / p


def decimal_to_american(d: float) -> float:
    d = float(d)
    if d <= 1.0:
        raise ValueError(f"decimal odds must exceed 1: {d}")
    if d >= 2.0:
        return (d - 1.0) * 100.0
    return -100.0 / (d - 1.0)


def devig_two_way(p_a: float, p_b: float) -> tuple[float, float]:
    """Normalize a two-way market to remove the overround. Returns (fair_a, fair_b)."""
    total = float(p_a) + float(p_b)
    if total <= 0:
        raise ValueError("probabilities must sum above 0")
    return p_a / total, p_b / total


def kalshi_ask_to_prob(ask_dollars: float) -> float:
    """Kalshi YES ask in dollars (0..1) -> implied probability. Fees excluded (documented)."""
    a = float(ask_dollars)
    if not 0.0 < a < 1.0:
        raise ValueError(f"kalshi ask out of range: {a}")
    return a


def kelly_fraction(p: float, decimal_odds: float, fraction: float = 0.25,
                   cap: float = 0.03) -> float:
    """Fractional Kelly stake as a fraction of bankroll, capped. Returns 0 if no edge."""
    d = float(decimal_odds)
    b = d - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - float(p)
    f = (float(p) * b - q) / b
    if f <= 0:
        return 0.0
    return min(f * fraction, cap)


# ------------------------------------------------------------- rating math
def elo_expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((rb - ra) / 400.0))


def elo_update(ra: float, rb: float, score_a: float, k: float = 20.0) -> tuple[float, float]:
    """Return (new_ra, new_rb). score_a in {1, 0.5, 0}."""
    ea = elo_expected(ra, rb)
    shift = k * (score_a - ea)
    return ra + shift, rb - shift


def log5(p_a: float, p_b: float) -> float:
    """Bill James log5 matchup probability from two team strengths."""
    pa, pb = float(p_a), float(p_b)
    if not (0.0 < pa < 1.0 and 0.0 < pb < 1.0):
        raise ValueError(f"log5 inputs out of range: {pa}, {pb}")
    num = pa * (1.0 - pb)
    return num / (num + (1.0 - pa) * pb)


def pythag(rs: float, ra: float, exp: float = 1.83) -> float:
    """Pythagorean win expectation. exp=1.83 baseball, ~2.0 hockey, ~13.5 basketball (per-game), ~2.4 football."""
    rs, ra = float(rs), float(ra)
    if rs < 0 or ra < 0 or (rs == 0 and ra == 0):
        raise ValueError(f"bad runs/goals/points: {rs}, {ra}")
    return rs ** exp / (rs ** exp + ra ** exp)


# ------------------------------------------------------------------- money
def money(x: float) -> float:
    return round(float(x) + 0.0, 2)


def parlay_decimal(decimals: list[float]) -> float:
    out = 1.0
    for d in decimals:
        out *= float(d)
    return out


def american_pair_from_probs(p_fav: float) -> tuple[float, float]:
    """Return (fav_american, dog_american) for a fair two-way prob. No hold added."""
    return round(prob_to_american(p_fav), 1), round(prob_to_american(1.0 - p_fav), 1)
