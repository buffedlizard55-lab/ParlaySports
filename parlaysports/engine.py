"""Backtest, forward-test and settlement runners.

Backtest integrity:
  * slate dates iterate chronologically; signals use only prices/ratings with
    timestamps strictly before the slate (close prices for finals; ratings
    trail is point-in-time by construction).
  * MULTI strategies backtest through the cross-sport overlap engine
    (run_backtest_multi): one decision clock per slate (T12:00Z, same as the
    single-sport runners), closing prices only, and candidate slates limited
    to dates where the per-sport BACKTEST_WINDOWS actually overlap (>=2
    sports with finals in-window). Leg independence remains assumed and
    documented. The single-sport run_backtest still refuses MULTI ids loudly.
  * every staked parlay appends stake + settle entries to the hash-chained
    ledger under the matching book ('backtest' | 'forward').
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any

from .parlay import (build_parlays, insert_parlay, settle_leg, settle_parlay)
from .store import add_issue, get_parlay, is_settled, ledger_append
from .strategies import (CATALOG_BY_ID, MULTI_SOURCES, VERSION,
                         signals_for_game)
from .util import money, utcnow_iso

# Backtest windows per sport (priced history available). The first season of
# each dataset is a WARMUP (totals-sigma + ratings stabilization) and is never
# backtested: NFL 1999-2009, MLB 2015, NHL 2024-25, NBA 2013-14.
BACKTEST_WINDOWS = {
    # NFL: full line coverage 2010+; 2006/2008 partial (excluded: outside window).
    "NFL": {"seasons": [str(y) for y in range(2010, 2026)],
            "game_types": ["REG", "POST"]},
    # MLB: outcome-only 2023-2025 (full seasons). KNOWN GAP: the inherited
    # results file has no 2016-2022 seasons and only Apr-Jul 2015; the quality
    # checker flags the gap and the Research log records it (R-013). Backtests
    # never pad around missing seasons.
    "MLB": {"seasons": ["2023", "2024", "2025"],
            "game_types": ["R"]},
    # NHL: Kalshi closes (REG/POST only; preseason excluded).
    "NHL": {"seasons": ["20252026"], "game_types": ["REG", "POST"]},
    # NBA: SBR Oct-Dec closes 2014-2022 (partial seasons, flagged on site).
    "NBA": {"seasons": ["2014-15", "2015-16", "2016-17", "2017-18", "2018-19",
                        "2019-20", "2020-21", "2021-22", "2022-23"],
            "game_types": ["REG"]},
}

# Expected historical coverage actually held in the seed (for gap detection).
# 'span' = (first_season, last_season) of intended coverage; 'have' = seasons
# with data. Anything inside span but outside have is a flagged gap, not a
# guess-filled period.
EXPECTED_HISTORY = {
    "NFL": {"span": ("1999", "2026"), "have": [str(y) for y in range(1999, 2027)],
            # 2022 held 271 regular-season games: the Week 17 BUF@CIN game was
            # cancelled and declared a no contest (verified, see crosscheck).
            "known_short": {"2022": "BUF@CIN week 17 cancelled (no contest); "
                                    "BUF and CIN finished with 16 games"},
            "in_progress": ["2026"]},
    "MLB": {"span": ("2015", "2025"), "have": ["2015", "2023", "2024", "2025"],
            # 2024 held 2429 games: the 2024-09-29 HOU@CLE finale was rained
            # out and never made up (verified, see crosscheck).
            "known_short": {"2024": "2024-09-29 HOU@CLE rained out, never made up; "
                                    "CLE and HOU finished with 161 games"},
            "partial_seasons": ["2015"], "in_progress": ["2026"]},
    "NHL": {"span": ("20242025", "20252026"), "have": ["20242025", "20252026"],
            "known_short": {}, "in_progress": ["20262027"]},
    "NBA": {"span": ("2013-14", "2022-23"), "have": ["2013-14", "2014-15", "2015-16",
                                                    "2016-17", "2017-18", "2018-19",
                                                    "2019-20", "2021-22", "2022-23",
                                                    "2020-21"],
            # SBR archive holds October-December slices only (documented).
            "partial_seasons": ["2013-14", "2014-15", "2015-16", "2016-17", "2017-18",
                                "2018-19", "2019-20", "2020-21", "2021-22", "2022-23"],
            "known_short": {}, "in_progress": ["2023-24", "2024-25", "2025-26", "2026-27"]},
}


def existing_slate_tickets(con: sqlite3.Connection, strategy_id: str, version: str,
                           slate_date: str, test_mode: str) -> int:
    """Tickets this strategy already holds on this slate in this book."""
    return int(con.execute(
        """SELECT COUNT(*) AS n FROM parlays WHERE strategy_id=? AND version=?
           AND test_mode=? AND slate_date=?""",
        (strategy_id, version, test_mode, slate_date)).fetchone()["n"])


def slate_cap(con: sqlite3.Connection, strategy_id: str, version: str,
              slate_date: str, test_mode: str) -> int:
    """Remaining tickets allowed on a slate (documented per-slate cap)."""
    from .config import MAX_PARLAYS_PER_SLATE
    limit = 1 if strategy_id == "S-MULTI-04" else MAX_PARLAYS_PER_SLATE
    return max(0, limit - existing_slate_tickets(con, strategy_id, version,
                                                  slate_date, test_mode))


def slate_dates(con: sqlite3.Connection, sport: str, seasons: list[str],
                game_types: list[str], status: str) -> list[str]:
    rows = con.execute(
        f"""SELECT DISTINCT game_date FROM games WHERE sport=? AND status=?
            AND season IN ({','.join('?' for _ in seasons)})
            AND game_type IN ({','.join('?' for _ in game_types)})
            ORDER BY game_date""",
        (sport, status, *seasons, *game_types)).fetchall()
    return [r["game_date"] for r in rows]


def slate_games(con: sqlite3.Connection, sport: str, slate_date: str,
                seasons: list[str], game_types: list[str],
                status: str) -> list[sqlite3.Row]:
    return con.execute(
        f"""SELECT * FROM games WHERE sport=? AND status=? AND game_date=?
            AND season IN ({','.join('?' for _ in seasons)})
            AND game_type IN ({','.join('?' for _ in game_types)})
            ORDER BY game_key""",
        (sport, status, slate_date, *seasons, *game_types)).fetchall()


def run_backtest(con: sqlite3.Connection, strategy_id: str,
                 max_slates: int | None = None) -> dict[str, Any]:
    """Chronological backtest for one single-sport strategy."""
    if strategy_id in MULTI_SOURCES:
        raise ValueError(f"{strategy_id} is a MULTI strategy: use run_backtest_multi "
                         f"(cross-sport overlap engine)")
    strat = CATALOG_BY_ID[strategy_id]
    sport = strat["sport"]
    window = BACKTEST_WINDOWS[sport]
    dates = slate_dates(con, sport, window["seasons"], window["game_types"], "final")
    if max_slates:
        dates = dates[:max_slates]
    n_parlays = n_legs = skipped_cap = 0
    for slate in dates:
        games = slate_games(con, sport, slate, window["seasons"],
                            window["game_types"], "final")
        if not games:
            continue
        decision_utc = f"{slate}T12:00:00Z"  # midday UTC decision stamp (before US tipoffs)
        signals: list[dict[str, Any]] = []
        for g in games:
            signals.extend(signals_for_game(con, strategy_id, g, decision_utc, "backtest"))
        if len({s["game_key"] for s in signals}) < 2:
            continue
        cap = slate_cap(con, strategy_id, strat.get("version", "v1"), slate, "backtest")
        if cap <= 0:
            skipped_cap += 1
            continue
        for p in build_parlays(signals, strat, slate, decision_utc, "backtest",
                               max_parlays_override=cap):
            if get_parlay(con, p["parlay_id"]) is not None:
                continue
            insert_parlay(con, p)
            n_parlays += 1
            n_legs += p["n_legs"]
            if p["stake"] > 0:
                ledger_append(con, strategy_id=p["strategy_id"], version=p["version"],
                              book="backtest", parlay_id=p["parlay_id"],
                              kind="stake", amount=-p["stake"], note=f"backtest stake {slate}",
                              entry_ts=decision_utc)
    settle_all(con, test_mode="backtest", strategy_id=strategy_id)
    con.commit()
    return {"strategy_id": strategy_id, "slates": len(dates),
            "parlays": n_parlays, "legs": n_legs, "skipped_cap": skipped_cap}


# ------------------------------------------------- cross-sport overlap engine
# Per-run signal cache keyed by (source_strategy, slate_date): the four MULTI
# strategies share most source generators, so each (src, slate) pair is
# generated once per run. Cleared at the start of every public entry point --
# signals are only cacheable while games/prices/ratings are static (they are
# during a backtest phase; settle only touches parlays/legs/ledger).
_MULTI_SIGNAL_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}


def clear_multi_signal_cache() -> None:
    _MULTI_SIGNAL_CACHE.clear()


def _iso_week(slate_date: str) -> str:
    """ISO week tag (e.g. '2026-W39') for a YYYY-MM-DD slate date."""
    return datetime.strptime(slate_date, "%Y-%m-%d").strftime("%G-W%V")


def _lotto_weeks_from_db(con: sqlite3.Connection, strategy_id: str,
                         test_mode: str) -> set[str]:
    """ISO weeks that already hold a lottery ticket (parsed from note tags)."""
    weeks: set[str] = set()
    for r in con.execute(
            "SELECT note FROM parlays WHERE strategy_id=? AND test_mode=?",
            (strategy_id, test_mode)).fetchall():
        for m in re.finditer(r"\b(\d{4}-W\d{2})\b", r["note"] or ""):
            weeks.add(m.group(1))
    return weeks


def _stamp_lotto_week(con: sqlite3.Connection, parlay_id: str, slate_date: str) -> str:
    """Append the slate's ISO-week tag to a lottery ticket's note (at creation,
    before settlement -- note is not a settlement field). Returns the tag."""
    week = _iso_week(slate_date)
    con.execute("UPDATE parlays SET note = COALESCE(note, '') || ? WHERE parlay_id=?",
                (f" {week}", parlay_id))
    return week


def multi_gate(strategy_id: str, min_edge: float):
    """Shared admission gate for MULTI legs (forward AND backtest, identical):
    market legs need edge >= min_edge; MODEL legs (edge-vs-proxy ~= 0 by
    construction) are admitted on conviction: model_prob >= 0.60, i.e. clear
    favorites only. UNPRICED legs (no prob, no price) never enter a ticket
    (S-MULTI-04 lottery admits everything except it still needs prices to
    build a graded ticket)."""
    def _passes(s: dict[str, Any]) -> bool:
        if strategy_id == "S-MULTI-04":
            return True
        if s.get("edge") is not None and s["edge"] >= min_edge:
            return True
        return (s.get("odds_type") in ("model_fair", "assumed")
                and (s.get("model_prob") or 0) >= 0.60)
    return _passes


def multi_overlap_dates(con: sqlite3.Connection,
                        sources: list[str]) -> tuple[list[str], dict[str, set[str]]]:
    """Chronological candidate slates for a MULTI strategy: dates where >=2 of
    its source sports hold final games inside their backtest windows.

    Returns (dates, sport -> in-window final slate dates). Only verified
    finals inside the documented per-sport windows are ever considered; no
    date is padded or guessed.
    """
    sport_dates: dict[str, set[str]] = {}
    for src in sources:
        sp = CATALOG_BY_ID[src]["sport"]
        if sp in sport_dates:
            continue
        w = BACKTEST_WINDOWS[sp]
        sport_dates[sp] = set(slate_dates(con, sp, w["seasons"], w["game_types"], "final"))
    if not sport_dates:
        return [], {}
    all_dates = sorted(set().union(*sport_dates.values()))
    dates = [d for d in all_dates if sum(d in s for s in sport_dates.values()) >= 2]
    return dates, sport_dates


def run_backtest_multi(con: sqlite3.Connection, strategy_id: str,
                       max_slates: int | None = None,
                       _keep_cache: bool = False) -> dict[str, Any]:
    """Chronological cross-sport backtest for one MULTI strategy.

    One decision clock per slate (T12:00Z). Each in-window sport contributes
    signals from its source-strategy generators (closing prices only, exactly
    as in the single-sport backtests); the shared MULTI gate and builder apply.
    S-MULTI-04 keeps its documented 'max 1 ticket per ISO week' cap (same as
    forward) with the week tag stamped into the ticket note at creation.
    """
    if strategy_id not in MULTI_SOURCES:
        raise ValueError(f"{strategy_id} is not a MULTI strategy (use run_backtest)")
    if not _keep_cache:
        clear_multi_signal_cache()
    strat = CATALOG_BY_ID[strategy_id]
    sources = MULTI_SOURCES[strategy_id]
    dates, sport_dates = multi_overlap_dates(con, sources)
    if max_slates:
        dates = dates[:max_slates]
    is_lotto = strategy_id == "S-MULTI-04"
    weeks_done = _lotto_weeks_from_db(con, strategy_id, "backtest") if is_lotto else set()
    gate = multi_gate(strategy_id, strat["min_edge"])
    n_parlays = n_legs = skipped_week = skipped_cap = 0
    for slate in dates:
        if is_lotto and _iso_week(slate) in weeks_done:
            skipped_week += 1
            continue
        decision_utc = f"{slate}T12:00:00Z"  # same midday stamp as single-sport
        signals: list[dict[str, Any]] = []
        for src in sources:
            sp = CATALOG_BY_ID[src]["sport"]
            if slate not in sport_dates[sp]:
                continue
            cached = _MULTI_SIGNAL_CACHE.get((src, slate))
            if cached is None:
                gen: list[dict[str, Any]] = []
                w = BACKTEST_WINDOWS[sp]
                for g in slate_games(con, sp, slate, w["seasons"], w["game_types"], "final"):
                    gen.extend(signals_for_game(con, src, g, decision_utc, "backtest"))
                _MULTI_SIGNAL_CACHE[(src, slate)] = cached = gen
            for s in cached:
                s2 = dict(s)
                s2["features"] = dict(s.get("features") or {})
                s2["strategy_id"] = strategy_id  # pooled under the multi book
                signals.append(s2)
        signals = [s for s in signals if gate(s)]
        if is_lotto:
            # lottery prefers plus-money legs (stable sort keeps edge order)
            signals.sort(key=lambda s: (-(s.get("odds_american") or -9999)))
        if len({s["game_key"] for s in signals}) < 2:
            continue
        cap = slate_cap(con, strategy_id, strat.get("version", "v1"), slate, "backtest")
        if cap <= 0:
            skipped_cap += 1
            continue
        for p in build_parlays(signals, strat, slate, decision_utc, "backtest",
                               max_parlays_override=cap):
            if get_parlay(con, p["parlay_id"]) is not None:
                continue
            insert_parlay(con, p)
            n_parlays += 1
            n_legs += p["n_legs"]
            if is_lotto:
                weeks_done.add(_stamp_lotto_week(con, p["parlay_id"], p["slate_date"]))
            if p["stake"] > 0:
                ledger_append(con, strategy_id=p["strategy_id"], version=p["version"],
                              book="backtest", parlay_id=p["parlay_id"],
                              kind="stake", amount=-p["stake"],
                              note=f"backtest stake {slate} (multi overlap)",
                              entry_ts=decision_utc)
    settle_all(con, test_mode="backtest", strategy_id=strategy_id)
    con.commit()
    return {"strategy_id": strategy_id, "slates": len(dates),
            "parlays": n_parlays, "legs": n_legs, "skipped_week": skipped_week,
            "skipped_cap": skipped_cap}


def run_backtest_multi_all(con: sqlite3.Connection,
                           strategy_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Run several MULTI backtests sharing one signal cache (seed/CLI path)."""
    clear_multi_signal_cache()
    out: dict[str, dict[str, Any]] = {}
    try:
        for sid in strategy_ids:
            out[sid] = run_backtest_multi(con, sid, _keep_cache=True)
    finally:
        clear_multi_signal_cache()
    return out


def _forward_sports_and_games(con: sqlite3.Connection, strategy_id: str,
                              slate: str) -> list[sqlite3.Row]:
    strat = CATALOG_BY_ID[strategy_id]
    if strat["sport"] != "MULTI":
        sport = strat["sport"]
        return con.execute(
            """SELECT * FROM games WHERE sport=? AND game_date=? AND status='scheduled'
               ORDER BY game_key""", (sport, slate)).fetchall()
    # MULTI: pool scheduled games from in-season sports (REG/regular only;
    # preseason excluded: NHL PRE, NBA preseason rows, MLB spring).
    return con.execute(
        """SELECT * FROM games WHERE game_date=? AND status='scheduled'
           AND ((sport='NFL' AND game_type IN ('REG','POST'))
             OR (sport='MLB' AND game_type='R')
             OR (sport='NHL' AND game_type IN ('REG','POST'))
             OR (sport='NBA' AND game_type IN ('REG','POST')))
           ORDER BY sport, game_key""", (slate,)).fetchall()


def run_forward(con: sqlite3.Connection, strategy_id: str,
                slates: list[str], decision_utc: str | None = None) -> dict[str, Any]:
    """Generate upcoming parlays for future slates. Refuses past/live games."""
    strat = CATALOG_BY_ID[strategy_id]
    decision_utc = decision_utc or utcnow_iso()
    is_lotto = strategy_id == "S-MULTI-04"
    # Lottery cap ('max 1 ticket per ISO week', per the catalog): enforced here
    # at creation time and stamped into the ticket note, so nightly runs can
    # never file a second ticket in a week the record already covers.
    weeks_done = _lotto_weeks_from_db(con, strategy_id, "forward") if is_lotto else set()
    n_parlays = n_legs = skipped_past = skipped_week = skipped_cap = 0
    for slate in slates:
        if is_lotto and _iso_week(slate) in weeks_done:
            skipped_week += 1
            continue
        games = _forward_sports_and_games(con, strategy_id, slate)
        games = [g for g in games if g["status"] == "scheduled"]
        if not games:
            continue
        # Guard: every leg must start after the decision timestamp. When a game
        # has no recorded start time, fall back to a conservative whole-date
        # cutoff: anything whose local gameday is strictly before the decision
        # date is skipped (better to miss a ticket than bet a started game).
        decision_date = (decision_utc or "")[:10]
        future_games = [g for g in games
                        if (g["start_utc"] and g["start_utc"] > decision_utc)
                        or (not g["start_utc"] and g["game_date"] >= decision_date)]
        skipped_past += len(games) - len(future_games)
        signals: list[dict[str, Any]] = []
        if strategy_id in MULTI_SOURCES:
            gate = multi_gate(strategy_id, strat["min_edge"])
            for src in MULTI_SOURCES[strategy_id]:
                src_sport = CATALOG_BY_ID[src]["sport"]
                for g in future_games:
                    if g["sport"] != src_sport:
                        continue
                    for s in signals_for_game(con, src, g, decision_utc, "forward"):
                        s["strategy_id"] = strategy_id  # pooled under the multi book
                        signals.append(s)
            # Shared multi gate (identical in forward and backtest): market
            # legs need edge >= min_edge; MODEL legs are admitted on conviction
            # (model_prob >= 0.60); UNPRICED legs never enter a multi ticket.
            signals = [s for s in signals if gate(s)]
            if is_lotto:
                # lottery prefers plus-money legs
                signals.sort(key=lambda s: (-(s.get("odds_american") or -9999)))
        else:
            for g in future_games:
                signals.extend(signals_for_game(con, strategy_id, g, decision_utc, "forward"))
        if len({s["game_key"] for s in signals}) < 2:
            continue
        cap = slate_cap(con, strategy_id, strat.get("version", "v1"), slate, "forward")
        if cap <= 0:
            skipped_cap += 1
            continue
        for p in build_parlays(signals, strat, slate, decision_utc, "forward",
                               max_parlays_override=cap):
            if get_parlay(con, p["parlay_id"]) is not None:
                continue
            insert_parlay(con, p)
            n_parlays += 1
            n_legs += p["n_legs"]
            if is_lotto:
                weeks_done.add(_stamp_lotto_week(con, p["parlay_id"], p["slate_date"]))
            if p["stake"] > 0:
                ledger_append(con, strategy_id=p["strategy_id"], version=p["version"],
                              book="forward", parlay_id=p["parlay_id"],
                              kind="stake", amount=-p["stake"],
                              note=f"forward stake {slate}", entry_ts=decision_utc)
    con.commit()
    return {"strategy_id": strategy_id, "slates": len(slates),
            "parlays": n_parlays, "legs": n_legs, "skipped_past": skipped_past,
            "skipped_week": skipped_week, "skipped_cap": skipped_cap}


def settle_all(con: sqlite3.Connection, test_mode: str | None = None,
               strategy_id: str | None = None,
               settlement_source: str = "verified finals import") -> dict[str, Any]:
    """Settle every unsettled parlay whose legs are all final. Never rewrites history."""
    q = "SELECT * FROM parlays WHERE status IN ('upcoming','live')"
    args: list[Any] = []
    if test_mode:
        q += " AND test_mode=?"
        args.append(test_mode)
    if strategy_id:
        q += " AND strategy_id=?"
        args.append(strategy_id)
    settled = {"won": 0, "lost": 0, "push": 0, "still_live": 0}
    settled_now = utcnow_iso()
    for p in con.execute(q, args).fetchall():
        if is_settled(p["status"]):
            continue  # immutable; belt and suspenders
        legs = con.execute("SELECT * FROM legs WHERE parlay_id=?",
                           (p["parlay_id"],)).fetchall()
        leg_results: list[dict[str, Any]] = []
        for leg in legs:
            g = con.execute("SELECT * FROM games WHERE game_key=?",
                            (leg["game_key"],)).fetchone()
            if g is None:
                add_issue(con, severity="error", area="settle", sport=leg["sport"],
                          game_key=leg["game_key"],
                          detail=f"leg references unknown game {leg['game_key']}")
                leg_results.append({"result": "pending", "odds_american": leg["odds_american"]})
                continue
            res = settle_leg(dict(leg), dict(g))
            leg_results.append({"result": res["result"],
                                "odds_american": leg["odds_american"]})
            if res["result"] != "pending" and leg["result"] == "pending":
                # leg_detail (decision-time features) is immutable; the result
                # goes to settle_detail so the price link is never clobbered.
                con.execute("UPDATE legs SET result=?, settle_detail=? WHERE leg_id=?",
                            (res["result"], res["detail"], leg["leg_id"]))
        outcome = settle_parlay(dict(p), leg_results)
        if outcome["status"] == "live":
            settled["still_live"] += 1
            continue
        # Finalize (single write; settle refuses settled rows ever after).
        # Historical fields (odds, selections, decision-time details) are never
        # touched; only settlement columns are written, once.
        roi = (outcome["pnl"] / p["stake"]) if p["stake"] else None
        con.execute(
            """UPDATE parlays SET status=?, settled_utc=?, settlement_source=?,
               result_detail=?, pnl=?, roi_parlay=?, payout=? WHERE parlay_id=? AND status IN ('upcoming','live')""",
            (outcome["status"], settled_now, settlement_source,
             outcome["detail"], outcome["pnl"], roi, outcome["payout"],
             p["parlay_id"]))
        # Ledger entries carry the ECONOMIC time of the cash movement, so the
        # equity curve and drawdown are chronological: a replayed (backtest)
        # ticket settles when its slate finished, a forward ticket settles when
        # the platform actually settled it. `parlays.settled_utc` always keeps
        # the real wall-clock settlement moment (the record is never rewritten).
        econ_ts = settled_now if p["test_mode"] != "backtest" else \
            f"{p['slate_date']}T23:59:00Z"
        if p["stake"] > 0 and outcome["payout"]:
            ledger_append(con, strategy_id=p["strategy_id"], version=p["version"],
                          book=p["test_mode"], parlay_id=p["parlay_id"],
                          kind="settle", amount=outcome["payout"],
                          note=f"settle {outcome['status']}: {outcome['detail']}",
                          entry_ts=econ_ts)
        elif p["stake"] > 0 and outcome["status"] == "push":
            ledger_append(con, strategy_id=p["strategy_id"], version=p["version"],
                          book=p["test_mode"], parlay_id=p["parlay_id"],
                          kind="settle", amount=p["stake"], note="stake refunded",
                          entry_ts=econ_ts)
        # Summary counters: statuses outside the tracked set (e.g. a future
        # 'void') fold into 'push' -- increment the FOLDED key, never clobber
        # it from the unfolded one.
        key = outcome["status"] if outcome["status"] in settled else "push"
        settled[key] += 1
    con.commit()
    return settled
