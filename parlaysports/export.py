"""Export JSON for the static site. The committed JSON is the persisted record.

Exports: meta, leaderboard (both books), upcoming, completed, strategies,
games, performance, research, sources, quality.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .books import by_sport_market, leaderboard, strategy_books
from .config import SITE_DIR, __version__
from .store import get_meta
from .util import utcnow_iso


def _write(name: str, obj: Any) -> Path:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    p = SITE_DIR / name
    p.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str))
    return p


def export_all(con: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    counts = {}
    for t in ("games", "prices", "ratings", "strategies", "signals", "parlays",
              "legs", "ledger", "research", "issues", "verifications", "users"):
        counts[t] = con.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
    meta = {
        "exported_utc": utcnow_iso(),
        "data_as_of_utc": get_meta(con, "data_as_of_utc"),
        "seed_manifest_sha256": get_meta(con, "seed_manifest_sha256"),
        "engine_version": __version__,
        "counts": counts,
        "books": ["backtest", "forward"],
        "disclaimer": ("SIMULATED PAPER COMPETITION. No real money. Model prices are "
                       "labeled MODEL and are estimates, never market facts."),
    }
    _write("meta.json", meta)
    out["meta"] = meta

    for book in ("forward", "backtest"):
        board = leaderboard(con, book)
        _write(f"leaderboard_{book}.json", board)
        out[f"leaderboard_{book}"] = len(board)

    upcoming = _parlays_export(con, "WHERE p.status IN ('upcoming','live')",
                               order="p.slate_date, p.parlay_id")
    _write("upcoming.json", upcoming)
    out["upcoming"] = len(upcoming)

    completed = _parlays_export(con, "WHERE p.status IN ('won','lost','push')",
                                order="p.settled_utc DESC, p.slate_date DESC",
                                limit=2000)
    _write("completed.json", completed)
    out["completed"] = len(completed)

    strategies = []
    for r in con.execute("SELECT * FROM strategies ORDER BY strategy_id").fetchall():
        s = dict(r)
        s["books"] = strategy_books(con, s["strategy_id"], s["version"])
        s["splits"] = {b: by_sport_market(con, s["strategy_id"], s["version"], b)
                       for b in ("forward", "backtest")}
        strategies.append(s)
    _write("strategies.json", strategies)
    out["strategies"] = len(strategies)

    games = _games_export(con)
    _write("games.json", games)

    perf = _performance_export(con)
    _write("performance.json", perf)

    research = [dict(r) for r in con.execute(
        "SELECT * FROM research ORDER BY created_utc DESC").fetchall()]
    _write("research.json", research)

    sources = [dict(r) for r in con.execute(
        "SELECT * FROM sources ORDER BY source_id").fetchall()]
    _write("sources.json", sources)

    users = [dict(r) for r in con.execute(
        "SELECT * FROM users ORDER BY username").fetchall()]
    _write("users.json", users)

    issues = [dict(r) for r in con.execute(
        "SELECT * FROM issues ORDER BY issue_id DESC LIMIT 500").fetchall()]
    checks = json.loads(get_meta(con, "last_quality_run") or "{}")
    verifies = [dict(r) for r in con.execute(
        "SELECT * FROM verifications ORDER BY verify_id DESC LIMIT 200").fetchall()]
    _write("quality.json", {"issues": issues, "last_checks": checks,
                            "verifications": verifies,
                            "open": sum(1 for i in issues if i["status"] == "open")})
    out["history"] = _history_export(con)
    out["done_utc"] = utcnow_iso()
    return out


def _history_export(con: sqlite3.Connection) -> int:
    """Complete, immutable ticket history per strategy (one JSON file each).

    The browse views (upcoming.json / completed.json) are capped for page
    weight; these files hold EVERY ticket a strategy has ever filed so its
    page can show a true full record.
    """
    sids = [r["strategy_id"] for r in con.execute(
        "SELECT DISTINCT strategy_id FROM strategies ORDER BY strategy_id")]
    n = 0
    for sid in sids:
        tickets = _parlays_export(
            con, "WHERE p.strategy_id=?",
            order="p.test_mode, p.slate_date, p.parlay_id", params=(sid,))
        _write(f"history_{sid}.json", {"strategy_id": sid, "tickets": tickets,
                                       "count": len(tickets)})
        n += len(tickets)
    return n


def _parlays_export(con: sqlite3.Connection, where: str, order: str,
                    limit: int | None = None,
                    params: tuple = ()) -> list[dict[str, Any]]:
    q = f"""SELECT p.*, (SELECT COUNT(*) FROM legs l WHERE l.parlay_id=p.parlay_id) AS legs_n
            FROM parlays p {where} ORDER BY {order}"""
    if limit:
        q += f" LIMIT {limit}"
    out = []
    for p in con.execute(q, params).fetchall():
        d = dict(p)
        legs = []
        for l in con.execute(
                """SELECT l.*, g.away_team AS g_away, g.home_team AS g_home,
                          g.game_date, g.start_utc, g.status AS game_status,
                          g.away_score, g.home_score, g.sport AS g_sport,
                          g.verified AS game_verified, g.source_id AS game_source_id
                   FROM legs l LEFT JOIN games g ON g.game_key=l.game_key
                   WHERE l.parlay_id=? ORDER BY l.leg_id""",
                (p["parlay_id"],)).fetchall():
            legs.append(dict(l))
        d["legs"] = legs
        d["sports"] = json.loads(d.get("sports_json") or "[]")
        out.append(d)
    return out


def _games_export(con: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {"by_sport": {}}
    for sport in ("NFL", "MLB", "NHL", "NBA"):
        finals = [dict(r) for r in con.execute(
            """SELECT game_key, league_game_id, season, game_type, game_date, start_utc,
                      week_or_slate, away_team, home_team, away_score, home_score,
                      source_id, retrieved_utc, verified
               FROM games WHERE sport=? AND status='final'
               ORDER BY game_date DESC, game_key DESC LIMIT 40""", (sport,)).fetchall()]
        upcoming = [dict(r) for r in con.execute(
            """SELECT game_key, league_game_id, season, game_type, game_date, start_utc,
                      week_or_slate, away_team, home_team, venue, source_id
               FROM games WHERE sport=? AND status IN ('scheduled','live')
               ORDER BY game_date, game_key LIMIT 60""", (sport,)).fetchall()]
        # attach best prices to upcoming games
        for g in upcoming:
            g["prices"] = [dict(r) for r in con.execute(
                """SELECT market, selection, line, odds_american, odds_type, source_id,
                          observed_utc, note FROM prices WHERE game_key=?
                   ORDER BY market, selection""", (g["game_key"],)).fetchall()]
        form = [dict(r) for r in con.execute(
            "SELECT * FROM team_form WHERE sport=? ORDER BY as_of_utc DESC LIMIT 40",
            (sport,)).fetchall()]
        out["by_sport"][sport] = {"recent_finals": finals, "upcoming": upcoming,
                                  "form_snapshot": form}
    return out


def _performance_export(con: sqlite3.Connection) -> dict[str, Any]:
    # Equity curves per strategy per book + daily competition PnL.
    curves: dict[str, Any] = {}
    for r in con.execute("SELECT strategy_id, version FROM strategies").fetchall():
        books = strategy_books(con, r["strategy_id"], r["version"])
        curves[f"{r['strategy_id']}|{r['version']}"] = {
            b: books[b].get("equity", []) for b in ("forward", "backtest")}
    daily = [dict(r) for r in con.execute(
        """SELECT slate_date, test_mode,
                 SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END) AS pnl,
                 COUNT(*) AS n
           FROM parlays WHERE status IN ('won','lost','push')
           GROUP BY slate_date, test_mode ORDER BY slate_date""").fetchall()]
    return {"equity": curves, "daily_pnl": daily,
            "by_sport": _split_stats(con, "l.sport", "p.sport_scope"),
            "by_market": _split_stats(con, "l.market", "p.market_mix"),
            "by_strategy": _split_stats(con, "p.strategy_id", "p.strategy_id")}


def _split_stats(con: sqlite3.Connection, leg_col: str,
                 parlay_col: str | None) -> dict[str, Any]:
    """Global settled-leg hit rates + settled-parlay PnL grouped by columns.

    leg_col groups the leg table (joined to parlays); parlay_col groups the
    parlay table itself (e.g. sport_scope / market_mix / strategy_id).
    """
    out: dict[str, Any] = {}
    legs = con.execute(
        f"""SELECT {leg_col} AS k,
                 SUM(CASE WHEN l.result='win' THEN 1 ELSE 0 END) AS w,
                 SUM(CASE WHEN l.result='loss' THEN 1 ELSE 0 END) AS losses,
                 SUM(CASE WHEN l.result='push' THEN 1 ELSE 0 END) AS p
            FROM legs l JOIN parlays p ON p.parlay_id=l.parlay_id
            WHERE l.result IN ('win','loss','push') GROUP BY {leg_col}""").fetchall()
    for r in legs:
        n = (r["w"] or 0) + (r["losses"] or 0)
        out[r["k"]] = {"leg_win": r["w"] or 0, "leg_loss": r["losses"] or 0,
                       "leg_push": r["p"] or 0,
                       "leg_hit": round((r["w"] or 0) / n, 4) if n else None}
    if parlay_col:
        par = con.execute(
            f"""SELECT {parlay_col} AS k,
                 COUNT(*) AS n,
                 SUM(CASE WHEN p.status='won' THEN 1 ELSE 0 END) AS won,
                 SUM(CASE WHEN p.status='lost' THEN 1 ELSE 0 END) AS lost,
                 SUM(CASE WHEN p.status='push' THEN 1 ELSE 0 END) AS pushed,
                 SUM(COALESCE(p.pnl, 0)) AS pnl,
                 SUM(COALESCE(p.stake, 0)) AS staked
            FROM parlays p WHERE p.status IN ('won','lost','push')
            GROUP BY {parlay_col}""").fetchall()
        for r in par:
            rec = out.setdefault(r["k"], {})
            rec.update({"parlays": r["n"], "won": r["won"] or 0, "lost": r["lost"] or 0,
                        "push": r["pushed"] or 0,
                        "pnl": round(r["pnl"] or 0.0, 2),
                        "staked": round(r["staked"] or 0.0, 2),
                        "roi": round((r["pnl"] or 0.0) / r["staked"], 4)
                        if r["staked"] else None})
    return out
