"""Global configuration: paths, sports, bankroll rules, season state.

All dates are UTC. All money is simulated paper USD.
"""
from __future__ import annotations

from pathlib import Path

__version__ = "1.1.0"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SEED_DIR = DATA_DIR / "seed"
SITE_DIR = DATA_DIR / "site"
DB_PATH = DATA_DIR / "parlaysports.db"

SPORTS = ("NFL", "MLB", "NHL", "NBA")

# Paper bankroll rules (standardized across every strategy/persona).
STARTING_BANKROLL = 10_000.0
UNIT_STAKE = 100.0          # flat stake per parlay (1% of starting bankroll)
LOTTO_STAKE = 25.0          # reduced stake for longshot parlays
MAX_PARLAYS_PER_SLATE = 3   # per strategy per slate date
MAX_LEGS_STANDARD = 4
MAX_LEGS_LOTTO = 8
MIN_MODEL_PROB_STANDARD = 0.03   # skip non-lotto parlays below 3% model hit prob
MIN_MODEL_PROB_LOTTO = 0.002     # skip lotto parlays below 0.2%

# Season state as verified on 2026-09-22 (UTC). The nightly collector refreshes
# games; this table documents what the seed covers so the site can say so.
SEASON_STATE = {
    "as_of_utc": "2026-09-22T00:30:00Z",
    "NFL": {
        "season": "2026",
        "phase": "regular-season-week-2-complete-plus-mnf-pregame",
        "completed": 31,
        "upcoming_window": "2026-09-21 (MNF NYG@LA, pregame) + Week 3 2026-09-24..28",
        "prices": "nflverse lines (DraftKings consensus), cross-verified vs ESPN/DK block and Kalshi KXNFLGAME sample",
    },
    "MLB": {
        "season": "2026",
        "phase": "regular-season-final-week",
        "completed": "~156 games/team; game-level 2026 log NOT yet ingested",
        "upcoming_window": "2026-09-22..28 (final week) + postseason",
        "prices": "model fair prices only (Pythag/log5); no verified market odds feed yet",
    },
    "NHL": {
        "season": "2026-27",
        "phase": "preseason",
        "completed": "preseason games since 2026-09-19; regular season starts 2026-09-29 per imported schedule",
        "upcoming_window": "preseason through 2026-09-26 (EXCLUDED from forward betting) + regular season",
        "prices": "Kalshi KXNHLGAME/SPREAD/TOTAL historical closes + live; NHL partner-feed DK snapshots from 2026-09-20",
    },
    "NBA": {
        "season": "2026-27",
        "phase": "preseason-not-started (first games 2026-10-03), openers 2026-10-20",
        "completed": 0,
        "upcoming_window": "preseason 2026-10-03+ (excluded until lines exist), openers 2026-10-20/21",
        "prices": "ESPN/DK forward lines for openers; Kalshi KXNBAGAME opener markets; SBR historical closes Oct-Dec 2013-2022",
    },
}

# Sportsbook-style settlement constants.
PUSH = "push"
WIN = "win"
LOSS = "loss"
VOID = "void"
PENDING = "pending"

# Parlay pricing grades (worst leg determines the grade; UNPRICED never stakes).
GRADE_VERIFIED = "VERIFIED"     # every leg market-verified (exchange/sportsbook quote)
GRADE_REFERENCE = "REFERENCE"   # every leg market-sourced (verified or reference lines)
GRADE_MODEL = "MODEL"           # every priced leg is a labeled model/assumption price
GRADE_MIXED = "MIXED"           # blend of market and model legs
GRADE_UNPRICED = "UNPRICED"     # at least one leg has no price -> tracked hit-only, $0 stake
