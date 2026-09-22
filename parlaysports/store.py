"""SQLite storage: schema, hash-chained append-only ledger, write guards.

Rules enforced here:
  * ledger rows are insert-only (no UPDATE/DELETE methods exist).
  * settled parlays are immutable (settle refuses to touch status != upcoming/live).
  * every insert carries source + timestamp; verification status is explicit.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import DB_PATH
from .util import money, sha256_text, utcnow_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    data_type TEXT NOT NULL,
    access_method TEXT NOT NULL,
    cost TEXT NOT NULL,
    license TEXT NOT NULL,
    reliability TEXT NOT NULL,
    granularity TEXT NOT NULL,
    historical_depth TEXT NOT NULL,
    last_verified_utc TEXT NOT NULL,
    status TEXT NOT NULL,
    limitations TEXT NOT NULL,
    provenance_class TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
    game_key TEXT PRIMARY KEY,
    sport TEXT NOT NULL,
    league_game_id TEXT NOT NULL,
    season TEXT NOT NULL,
    game_type TEXT NOT NULL,
    game_date TEXT NOT NULL,
    start_utc TEXT,
    week_or_slate TEXT,
    away_team TEXT NOT NULL,
    home_team TEXT NOT NULL,
    neutral INTEGER NOT NULL DEFAULT 0,
    venue TEXT,
    status TEXT NOT NULL,
    away_score INTEGER,
    home_score INTEGER,
    overtime INTEGER,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_utc TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    verify_note TEXT,
    extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_games_sport_date ON games(sport, game_date);
CREATE INDEX IF NOT EXISTS idx_games_status ON games(status);
CREATE INDEX IF NOT EXISTS idx_games_away_team ON games(sport, away_team, game_date);
CREATE INDEX IF NOT EXISTS idx_games_home_team ON games(sport, home_team, game_date);
CREATE TABLE IF NOT EXISTS team_form (
    team_key TEXT NOT NULL,
    sport TEXT NOT NULL,
    team TEXT NOT NULL,
    season TEXT NOT NULL,
    as_of_utc TEXT NOT NULL,
    source_id TEXT NOT NULL,
    w INTEGER, l INTEGER,
    rs REAL, ra REAL,
    streak_code TEXT, streak_n INTEGER,
    last10_w INTEGER, last10_l INTEGER,
    home_w INTEGER, home_l INTEGER,
    away_w INTEGER, away_l INTEGER,
    xw REAL, xl REAL,
    note TEXT,
    PRIMARY KEY (team_key, as_of_utc)
);
CREATE TABLE IF NOT EXISTS ratings (
    sport TEXT NOT NULL,
    team TEXT NOT NULL,
    season TEXT NOT NULL,
    game_date TEXT NOT NULL,
    game_key TEXT NOT NULL,
    elo_before REAL NOT NULL,
    elo_after REAL NOT NULL,
    games_used INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    PRIMARY KEY (sport, team, game_key)
);
CREATE INDEX IF NOT EXISTS idx_ratings_team ON ratings(sport, team, game_date);
CREATE TABLE IF NOT EXISTS prices (
    price_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_key TEXT NOT NULL,
    market TEXT NOT NULL,
    selection TEXT NOT NULL,
    line REAL,
    odds_american REAL,
    odds_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    observed_utc TEXT NOT NULL,
    close_flag INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_prices_game ON prices(game_key, market, selection);
CREATE INDEX IF NOT EXISTS idx_prices_market ON prices(market, close_flag);
CREATE INDEX IF NOT EXISTS idx_prices_close ON prices(game_key, close_flag);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    username TEXT NOT NULL,
    name TEXT NOT NULL,
    sport TEXT NOT NULL,
    category TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    markets TEXT NOT NULL,
    selection_rules TEXT NOT NULL,
    construction_rules TEXT NOT NULL,
    required_data TEXT NOT NULL,
    min_edge REAL NOT NULL,
    stake REAL NOT NULL,
    max_legs INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    limitations TEXT NOT NULL,
    lineage TEXT,
    PRIMARY KEY (strategy_id, version)
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    game_key TEXT NOT NULL,
    market TEXT NOT NULL,
    selection TEXT NOT NULL,
    line REAL,
    model_prob REAL,
    market_prob REAL,
    edge REAL,
    decision_utc TEXT NOT NULL,
    features_json TEXT,
    price_id INTEGER,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_strat ON signals(strategy_id, version, decision_utc);
CREATE TABLE IF NOT EXISTS parlays (
    parlay_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    username TEXT NOT NULL,
    sport_scope TEXT NOT NULL,
    sports_json TEXT NOT NULL,
    n_legs INTEGER NOT NULL,
    market_mix TEXT NOT NULL,
    stake REAL NOT NULL,
    pricing_grade TEXT NOT NULL,
    combined_decimal REAL,
    combined_american REAL,
    potential_payout REAL,
    decision_utc TEXT NOT NULL,
    slate_date TEXT NOT NULL,
    status TEXT NOT NULL,
    settled_utc TEXT,
    settlement_source TEXT,
    result_detail TEXT,
    pnl REAL,
    roi_parlay REAL,
    test_mode TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_parlays_strat ON parlays(strategy_id, version, test_mode, status);
CREATE INDEX IF NOT EXISTS idx_parlays_status ON parlays(status, slate_date);
CREATE TABLE IF NOT EXISTS legs (
    leg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parlay_id TEXT NOT NULL,
    game_key TEXT NOT NULL,
    sport TEXT NOT NULL,
    market TEXT NOT NULL,
    selection TEXT NOT NULL,
    line REAL,
    odds_american REAL,
    odds_type TEXT,
    model_prob REAL,
    result TEXT NOT NULL DEFAULT 'pending',
    leg_detail TEXT,
    settle_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_legs_parlay ON legs(parlay_id);
CREATE INDEX IF NOT EXISTS idx_legs_game ON legs(game_key);
CREATE TABLE IF NOT EXISTS ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_ts TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    book TEXT NOT NULL,
    parlay_id TEXT,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    balance_after REAL NOT NULL,
    note TEXT,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_strat ON ledger(strategy_id, version, book);
CREATE TABLE IF NOT EXISTS bankroll (
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    book TEXT NOT NULL,
    start_amount REAL NOT NULL,
    current_amount REAL NOT NULL,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (strategy_id, version, book)
);
CREATE TABLE IF NOT EXISTS research (
    research_id TEXT PRIMARY KEY,
    created_utc TEXT NOT NULL,
    sport TEXT NOT NULL,
    title TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    method TEXT NOT NULL,
    data_window TEXT NOT NULL,
    result TEXT NOT NULL,
    status TEXT NOT NULL,
    ref_strategy TEXT
);
CREATE TABLE IF NOT EXISTS issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_utc TEXT NOT NULL,
    severity TEXT NOT NULL,
    area TEXT NOT NULL,
    sport TEXT,
    game_key TEXT,
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS verifications (
    verify_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_utc TEXT NOT NULL,
    subject TEXT NOT NULL,
    claim TEXT NOT NULL,
    source_url TEXT NOT NULL,
    result TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def connect(path: str | Path = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.executescript(SCHEMA)
    # Idempotent micro-migrations for older databases.
    try:
        con.execute("ALTER TABLE legs ADD COLUMN settle_detail TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE parlays ADD COLUMN payout REAL")
    except sqlite3.OperationalError:
        pass
    return con


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


# ------------------------------------------------------------------ ledger
GENESIS_HASH = "0" * 64


def ledger_head_hash(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT entry_hash FROM ledger ORDER BY entry_id DESC LIMIT 1").fetchone()
    return row["entry_hash"] if row else GENESIS_HASH


def ledger_append(con: sqlite3.Connection, *, strategy_id: str, version: str,
                  book: str, parlay_id: str | None, kind: str, amount: float,
                  note: str = "", entry_ts: str | None = None) -> dict[str, Any]:
    """Append one ledger entry and move the matching bankroll. Returns the row.

    Books are 'backtest' | 'forward'. Every strategy starts each book at the
    configured starting bankroll on first use (recorded as an opening entry).
    """
    from .config import STARTING_BANKROLL

    ts = entry_ts or utcnow_iso()
    brow = con.execute(
        "SELECT current_amount FROM bankroll WHERE strategy_id=? AND version=? AND book=?",
        (strategy_id, version, book)).fetchone()
    if brow is None:
        con.execute(
            "INSERT INTO bankroll(strategy_id, version, book, start_amount, current_amount, updated_utc)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (strategy_id, version, book, STARTING_BANKROLL, STARTING_BANKROLL, ts))
        # opening marker entry (amount 0) so the chain shows book creation
        prev = ledger_head_hash(con)
        payload = f"OPEN|{strategy_id}|{version}|{book}|{ts}|{STARTING_BANKROLL}"
        h = sha256_text(prev + payload)
        con.execute(
            "INSERT INTO ledger(entry_ts, strategy_id, version, book, parlay_id, kind, amount,"
            " balance_after, note, prev_hash, entry_hash)"
            " VALUES (?, ?, ?, ?, NULL, 'open', 0, ?, 'book opened', ?, ?)",
            (ts, strategy_id, version, book, money(STARTING_BANKROLL), prev, h))
        balance = STARTING_BANKROLL
    else:
        balance = float(brow["current_amount"])
    new_balance = money(balance + float(amount))
    prev = ledger_head_hash(con)
    payload = (f"{ts}|{strategy_id}|{version}|{book}|{parlay_id}|{kind}|"
               f"{money(amount)}|{new_balance}")
    h = sha256_text(prev + payload)
    cur = con.execute(
        "INSERT INTO ledger(entry_ts, strategy_id, version, book, parlay_id, kind, amount,"
        " balance_after, note, prev_hash, entry_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, strategy_id, version, book, parlay_id, kind, money(amount),
         new_balance, note, prev, h))
    con.execute(
        "UPDATE bankroll SET current_amount=?, updated_utc=? "
        "WHERE strategy_id=? AND version=? AND book=?",
        (new_balance, ts, strategy_id, version, book))
    return {"entry_id": cur.lastrowid, "balance_after": new_balance, "entry_hash": h}


def verify_ledger_chain(con: sqlite3.Connection) -> list[str]:
    """Recompute the hash chain. Returns a list of problems (empty = clean)."""
    problems: list[str] = []
    prev = GENESIS_HASH
    rows = con.execute("SELECT * FROM ledger ORDER BY entry_id").fetchall()
    for r in rows:
        if r["prev_hash"] != prev:
            problems.append(f"entry {r['entry_id']}: prev_hash mismatch (chain broken)")
            break
        if r["kind"] == "open":
            payload = (f"OPEN|{r['strategy_id']}|{r['version']}|{r['book']}|"
                       f"{r['entry_ts']}|{r['balance_after']}")
        else:
            payload = (f"{r['entry_ts']}|{r['strategy_id']}|{r['version']}|{r['book']}|"
                       f"{r['parlay_id']}|{r['kind']}|{r['amount']}|{r['balance_after']}")
        if sha256_text(prev + payload) != r["entry_hash"]:
            problems.append(f"entry {r['entry_id']}: entry_hash mismatch (tampered?)")
            break
        prev = r["entry_hash"]
    return problems


# ------------------------------------------------------------------ guards
def get_parlay(con: sqlite3.Connection, parlay_id: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM parlays WHERE parlay_id=?", (parlay_id,)).fetchone()
    return dict(row) if row else None


def is_settled(status: str) -> bool:
    return status in ("won", "lost", "push", "void")


def add_issue(con: sqlite3.Connection, *, severity: str, area: str, detail: str,
              sport: str | None = None, game_key: str | None = None) -> int:
    cur = con.execute(
        "INSERT INTO issues(created_utc, severity, area, sport, game_key, detail, status)"
        " VALUES (?, ?, ?, ?, ?, ?, 'open')",
        (utcnow_iso(), severity, area, sport, game_key, detail))
    return int(cur.lastrowid)


def add_verification(con: sqlite3.Connection, *, subject: str, claim: str,
                     source_url: str, result: str, detail: str) -> int:
    cur = con.execute(
        "INSERT INTO verifications(created_utc, subject, claim, source_url, result, detail)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (utcnow_iso(), subject, claim, source_url, result, detail))
    return int(cur.lastrowid)


def dump_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)
