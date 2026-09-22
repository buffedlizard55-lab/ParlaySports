"""PASS 2 audit: mechanical verification of every prompt requirement.

Fails (exit 1) with a checklist when anything is missing or contradictory.
Run:  python scripts/audit.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from parlaysports import quality, store
from parlaysports.config import DB_PATH, SITE_DIR

PASS, FAIL = "PASS", "FAIL"
report: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    report.append({"check": name, "status": PASS if ok else FAIL, "detail": detail})
    print(f"[{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""), flush=True)


def main() -> int:
    print("== ParlaySports PASS 2 audit ==", flush=True)
    # 1. unit tests
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                       cwd=ROOT, capture_output=True, text=True)
    check("unit-test-suite", r.returncode == 0,
          (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout) else "")
    if not DB_PATH.exists():
        check("database-exists", False, "run seed first")
        return print_summary()
    con = store.connect()
    # 2. sports coverage
    for sport in ("NFL", "MLB", "NHL", "NBA"):
        n = con.execute("SELECT COUNT(*) AS n FROM games WHERE sport=?",
                        (sport,)).fetchone()["n"]
        check(f"sport-{sport}-has-games", n > 100, f"{n} games")
    # 3. strategies
    n = con.execute("SELECT COUNT(*) AS n FROM strategies").fetchone()["n"]
    check("28-strategies", n == 28, f"{n} installed")
    usernames = con.execute("SELECT COUNT(DISTINCT username) AS n FROM strategies").fetchone()["n"]
    check("unique-usernames", usernames == 28, f"{usernames} distinct")
    for scope in ("NFL", "MLB", "NHL", "NBA", "MULTI"):
        n = con.execute("SELECT COUNT(*) AS n FROM strategies WHERE sport=?",
                        (scope,)).fetchone()["n"]
        check(f"scope-{scope}-count", (n == 6) if scope != "MULTI" else (n == 4), f"{n}")
    # 4. backtest + forward parlays exist per sport book
    for sport, book in (("NFL", "backtest"), ("MLB", "backtest"), ("NHL", "backtest"),
                        ("NBA", "backtest"), ("NFL", "forward"), ("MLB", "forward")):
        n = con.execute(
            """SELECT COUNT(*) AS n FROM parlays p JOIN strategies s
               ON s.strategy_id=p.strategy_id AND s.version=p.version
               WHERE s.sport=? AND p.test_mode=?""", (sport, book)).fetchone()["n"]
        check(f"{sport}-{book}-parlays", n > 0, f"{n}")
    # multi backtests exist and obey the overlap engine's integrity rules (R-017)
    from parlaysports.engine import BACKTEST_WINDOWS, slate_dates
    n = con.execute("SELECT COUNT(*) AS n FROM parlays WHERE strategy_id LIKE 'S-MULTI-%'"
                    " AND test_mode='backtest'").fetchone()["n"]
    check("multi-backtest-tickets-exist", n > 0, f"{n}")
    bad = 0
    for r in con.execute(
            "SELECT parlay_id, sports_json FROM parlays "
            "WHERE strategy_id LIKE 'S-MULTI-%' AND test_mode='backtest'"):
        try:
            if len(set(json.loads(r["sports_json"] or "[]"))) < 2:
                bad += 1
        except ValueError:
            bad += 1
    check("multi-backtest-two-sports", bad == 0, f"{bad} tickets spanning <2 sports")
    sport_dates = {sp: set(slate_dates(con, sp, w["seasons"], w["game_types"], "final"))
                   for sp, w in BACKTEST_WINDOWS.items()}
    bad = 0
    for r in con.execute(
            "SELECT DISTINCT slate_date FROM parlays "
            "WHERE strategy_id LIKE 'S-MULTI-%' AND test_mode='backtest'"):
        if sum(r["slate_date"] in s for s in sport_dates.values()) < 2:
            bad += 1
    check("multi-backtest-window-dates", bad == 0,
          f"{bad} slate dates outside a >=2-sport window overlap")
    # 5. settlement integrity
    n = con.execute("SELECT COUNT(*) AS n FROM parlays WHERE status IN ('won','lost','push')"
                    ).fetchone()["n"]
    check("settled-parlays-exist", n > 0, f"{n}")
    n = con.execute("SELECT COUNT(*) AS n FROM parlays WHERE status IN ('upcoming','live')"
                    ).fetchone()["n"]
    check("upcoming-parlays-exist", n > 0, f"{n}")
    # 6. bankroll math: ledger balances equal bankroll table
    bad = 0
    for b in con.execute("SELECT strategy_id, version, book, current_amount FROM bankroll"):
        last = con.execute(
            "SELECT balance_after FROM ledger WHERE strategy_id=? AND version=? AND book=? "
            "ORDER BY entry_id DESC LIMIT 1",
            (b["strategy_id"], b["version"], b["book"])).fetchone()
        if last is None or abs(float(last["balance_after"]) - float(b["current_amount"])) > 0.01:
            bad += 1
    check("ledger-bankroll-consistent", bad == 0, f"{bad} mismatches")
    # 7. UNPRICED never stakes
    n = con.execute("SELECT COUNT(*) AS n FROM parlays WHERE pricing_grade='UNPRICED'"
                    " AND stake > 0").fetchone()["n"]
    check("unpriced-never-stakes", n == 0, f"{n}")
    # 8. every priced leg has odds; every game has a source
    n = con.execute("SELECT COUNT(*) AS n FROM legs l JOIN parlays p ON p.parlay_id=l.parlay_id"
                    " WHERE p.pricing_grade != 'UNPRICED' AND l.odds_american IS NULL").fetchone()["n"]
    check("priced-legs-have-odds", n == 0, f"{n}")
    n = con.execute("SELECT COUNT(*) AS n FROM games WHERE source_id IS NULL OR source_id=''").fetchone()["n"]
    check("games-have-sources", n == 0, f"{n}")
    n = con.execute("SELECT COUNT(*) AS n FROM prices WHERE source_id IS NULL OR source_id=''").fetchone()["n"]
    check("prices-have-sources", n == 0, f"{n}")
    # 9. research + sources + quality exports
    for f in ("meta.json", "leaderboard_forward.json", "leaderboard_backtest.json",
              "upcoming.json", "completed.json", "strategies.json", "games.json",
              "performance.json", "research.json", "sources.json", "quality.json"):
        p = SITE_DIR / f
        ok = p.exists() and p.stat().st_size > 100
        check(f"export-{f}", ok, f"{p.stat().st_size}B" if p.exists() else "missing")
    # 10. site shell
    for f in ("index.html", "styles.css", "app.js"):
        p = ROOT / f
        check(f"site-{f}", p.exists() and p.stat().st_size > 1000,
              f"{p.stat().st_size}B" if p.exists() else "missing")
    # 11. quality gates
    q = quality.run_checks(con)
    con.commit()
    hard = [c for c in q["checks"]
            if c["check"] in ("ledger-chain", "settlement-recompute", "backtest-leakage",
                              "bankroll-integrity", "price-observed-after-decision",
                              "form-snapshot-leakage", "schedule-completeness")
            and c["problems"] > 0]
    check("hard-quality-gates", not hard, "; ".join(
        f"{c['check']}:{c['problems']}" for c in hard)
        or "chain+settle+leakage+bankroll+price-time+form-time+schedule clean")
    # 12. no fabricated markers: every strategy has limitations + required data
    n = con.execute("SELECT COUNT(*) AS n FROM strategies WHERE limitations='' OR required_data=''"
                    ).fetchone()["n"]
    check("strategies-documented", n == 0, f"{n} undocumented")
    # 13. settlement completeness: settled priced tickets record a payout
    n = con.execute(
        """SELECT COUNT(*) AS n FROM parlays WHERE status IN ('won','lost','push')
           AND pricing_grade != 'UNPRICED' AND payout IS NULL""").fetchone()["n"]
    check("settled-tickets-have-payout", n == 0, f"{n} missing payout")
    # 14. upcoming/completed books never mix
    up = json.loads((SITE_DIR / "upcoming.json").read_text())
    done = json.loads((SITE_DIR / "completed.json").read_text())
    bad = sum(1 for p in up if p["status"] not in ("upcoming", "live"))
    check("upcoming-list-is-unsettled-only", bad == 0, f"{bad} settled rows leaked into upcoming")
    bad = sum(1 for p in done if p["status"] not in ("won", "lost", "push", "void"))
    check("completed-list-is-settled-only", bad == 0, f"{bad} unsettled rows leaked into completed")
    # 15. per-strategy complete history files
    missing_hist = [r["strategy_id"] for r in con.execute("SELECT DISTINCT strategy_id FROM strategies")
                    if not (SITE_DIR / f"history_{r['strategy_id']}.json").exists()]
    check("strategy-history-files", not missing_hist,
          f"{len(missing_hist)} missing" if missing_hist else "all present")
    # 16. leaderboard rows expose every required column's data
    lb = json.loads((SITE_DIR / "leaderboard_forward.json").read_text())
    need = {"username", "strategy_id", "sport", "bankroll", "pnl", "roi", "parlays",
            "won", "lost", "push", "leg_hit_rate", "parlay_hit_rate",
            "max_drawdown", "last_activity", "avg_legs", "streak"}
    missing_cols = sorted({k for row in lb for k in (need - set(row))}) if lb else sorted(need)
    check("leaderboard-required-columns", not missing_cols,
          ",".join(missing_cols) if missing_cols else f"{len(need)} columns on all rows")
    # 17. completed rows carry settlement source + leg verification status
    bad = 0
    for p in done[:200]:
        if not p.get("settlement_source"):
            bad += 1
        for l in p.get("legs", []):
            if l.get("game_verified") is None:
                bad += 1
    check("completed-carry-settlement-provenance", bad == 0, f"{bad} fields missing")
    # 18. quality registry covers the full prompt checklist
    names = {c["check"] for c in q["checks"]}
    required_checks = {"missing-scores-past-games", "missing-odds-priced-parlays",
                       "duplicate-events", "conflicting-results", "invalid-stats",
                       "timestamp-order", "stale-snapshots", "settlement-recompute",
                       "ledger-chain", "parlay-math", "backtest-leakage", "odds-sanity",
                       "missing-historical-periods", "multi-backtest-integrity",
                       "bankroll-integrity", "price-observed-after-decision",
                       "form-snapshot-leakage", "schedule-completeness"}
    lack = sorted(required_checks - names)
    check("quality-registry-complete", not lack,
          ",".join(lack) if lack else f"{len(required_checks)} checks live")
    # 19. competitor accounts persist and are exported
    n = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    check("users-persisted", n == 28, f"{n} accounts")
    ok = (SITE_DIR / "users.json").exists() and len(
        json.loads((SITE_DIR / "users.json").read_text())) == n
    check("users-exported", bool(ok), "users.json")
    # 20. price import idempotency invariant: no two identical quotes
    n = con.execute(
        """SELECT COUNT(*) AS n FROM (
             SELECT game_key, market, selection, source_id, observed_utc, close_flag,
                    COUNT(*) AS c FROM prices
             GROUP BY game_key, market, selection, line, odds_american,
                      source_id, observed_utc, close_flag HAVING c > 1)""").fetchone()["n"]
    check("no-duplicate-price-quotes", n == 0, f"{n} duplicated quote groups")

    # 21. bankroll curves are recomputed in ECONOMIC time and reconcile exactly
    from parlaysports.config import MAX_PARLAYS_PER_SLATE
    from parlaysports.util import money
    drift, negative, over_dd = [], [], []
    for b in con.execute("SELECT strategy_id, book, start_amount, current_amount "
                         "FROM bankroll").fetchall():
        bal = float(b["start_amount"])
        peak, maxdd = bal, 0.0
        for r in con.execute(
                "SELECT entry_ts, amount FROM ledger WHERE strategy_id=? AND book=? "
                "ORDER BY entry_ts, entry_id", (b["strategy_id"], b["book"])).fetchall():
            bal = money(bal + float(r["amount"]))
            peak = max(peak, bal)
            if peak > 0:
                maxdd = max(maxdd, (peak - bal) / peak)
            if bal < 0:
                negative.append(f"{b['strategy_id']}/{b['book']}")
        if abs(bal - float(b["current_amount"])) > 0.01:
            drift.append(f"{b['strategy_id']}/{b['book']}:{bal}!={b['current_amount']}")
        if maxdd > 1.0:
            over_dd.append(f"{b['strategy_id']}/{b['book']}:{maxdd:.1%}")
    check("bankroll-economic-integrity", not (drift or negative or over_dd),
          "; ".join(drift[:3] + negative[:3] + over_dd[:3]) or
          "every book reconciles in economic time; no negative balance; no dd > 100%")

    # 22. exported leaderboards carry the economic fields
    bad_cols = []
    for f in ("leaderboard_backtest.json", "leaderboard_forward.json"):
        for row in json.loads((SITE_DIR / f).read_text()):
            for k in ("min_balance", "ledger_drift", "max_drawdown"):
                if k not in row:
                    bad_cols.append(f"{f}:{row.get('strategy_id')}:{k}")
            if abs(float(row.get("ledger_drift") or 0)) > 0.01:
                bad_cols.append(f"{f}:{row.get('strategy_id')}:drift={row.get('ledger_drift')}")
    check("leaderboard-economic-columns", not bad_cols,
          ",".join(sorted(set(bad_cols))[:5]) or "min_balance/ledger_drift/max_drawdown on all rows")

    # 23. forward book never bets a preseason exhibition
    n = con.execute(
        """SELECT COUNT(*) AS n FROM parlays p JOIN legs l ON l.parlay_id=p.parlay_id
           JOIN games g ON g.game_key=l.game_key
           WHERE p.test_mode='forward' AND g.game_type='PRE'""").fetchone()["n"]
    check("forward-excludes-preseason", n == 0, f"{n} preseason legs in the forward book")

    # 24. NBA game-type boundaries are derived, evidenced and within league length
    pre = {r["season"]: r["n"] for r in con.execute(
        "SELECT season, COUNT(*) AS n FROM games WHERE sport='NBA' AND game_type='PRE' "
        "GROUP BY season")}
    over = con.execute(
        """SELECT season, COUNT(*) AS n FROM (
             SELECT season, away_team AS t, COUNT(*) AS c FROM games
             WHERE sport='NBA' AND game_type='REG' GROUP BY season, away_team
             UNION ALL
             SELECT season, home_team AS t, COUNT(*) AS c FROM games
             WHERE sport='NBA' AND game_type='REG' GROUP BY season, home_team)
           GROUP BY season HAVING MAX(c) > 82""").fetchall()
    no_evidence = con.execute(
        "SELECT COUNT(*) AS n FROM games WHERE sport='NBA' AND game_type IN ('PRE','POST') "
        "AND (verify_note IS NULL OR verify_note NOT LIKE '%derived%')").fetchone()["n"]
    expected_pre = {"2023-24", "2024-25", "2025-26", "2026-27"}
    check("nba-game-type-boundaries",
          not over and no_evidence == 0 and expected_pre <= set(pre),
          f"PRE rows {pre}; seasons over 82/team "
          f"{[dict(r) for r in over]}; rows without derivation evidence {no_evidence}")

    # 25. documented per-slate ticket cap is actually enforced
    over_cap = []
    for r in con.execute(
            "SELECT strategy_id, test_mode, slate_date, COUNT(*) AS n FROM parlays "
            "GROUP BY strategy_id, test_mode, slate_date").fetchall():
        limit = 1 if r["strategy_id"] == "S-MULTI-04" else MAX_PARLAYS_PER_SLATE
        if r["n"] > limit:
            over_cap.append(f"{r['strategy_id']}/{r['test_mode']}/{r['slate_date']}:"
                            f"{r['n']}>{limit}")
    check("slate-cap-enforced", not over_cap,
          ",".join(over_cap[:5]) or f"cap {MAX_PARLAYS_PER_SLATE}/slate (lotto 1/week) respected")

    # 26. hand-verified pinned updates are present in the record
    missing_updates = []
    for path in sorted((ROOT / "data" / "seed" / "updates").glob("*.json")):
        for u in json.loads(path.read_text()).get("updates", []):
            row = con.execute("SELECT status, away_score, home_score, verified "
                              "FROM games WHERE game_key=?", (u["game_key"],)).fetchone()
            if row is None:
                missing_updates.append(f"{u['game_key']}:absent")
            elif (row["status"], row["away_score"], row["home_score"], row["verified"]) != (
                    u.get("status"), u.get("away_score"), u.get("home_score"), 1):
                missing_updates.append(f"{u['game_key']}:{dict(row)}")
    check("verified-updates-applied", not missing_updates,
          ",".join(missing_updates[:4]) or "all pinned verified finals are in the record")

    # 27. no ESPN row duplicates a league-log matchup (UTC rollover handled)
    dup = con.execute(
        """SELECT COUNT(*) AS n FROM games g WHERE g.game_key LIKE '%:espn:%' AND EXISTS (
             SELECT 1 FROM games h WHERE h.sport=g.sport AND h.away_team=g.away_team
               AND h.home_team=g.home_team AND h.game_key NOT LIKE '%:espn:%'
               AND h.game_date BETWEEN date(g.game_date,'-1 day') AND date(g.game_date,'+1 day'))
        """).fetchone()["n"]
    check("no-cross-source-duplicate-games", dup == 0, f"{dup} ESPN rows shadow a league-log game")

    # 29. workflow files parse as YAML (dependency-free structural lint).
    # A bad workflow file does not fail loudly: GitHub reports "workflow file
    # issue" and the scheduled job silently stops running. This lint catches the
    # realistic failure mode -- a block scalar that ends early because a
    # continuation line was not indented, leaving stray text at column 0.
    wf_dir = ROOT / ".github" / "workflows"
    wf_problems: list[str] = []
    for wf in sorted(wf_dir.glob("*.y*ml")):
        text = wf.read_text()
        if "jobs:" not in text:
            wf_problems.append(f"{wf.name}: no jobs: block")
        if "\t" in text:
            wf_problems.append(f"{wf.name}: tab character (YAML forbids tabs for indentation)")
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[0] != " " and not re.match(r"^[A-Za-z_][A-Za-z0-9_-]*:", line):
                wf_problems.append(f"{wf.name}:{n}: stray text at column 0 "
                                   f"({line[:40]!r}) - block scalar ended early")
    check("workflow-files-valid", not wf_problems,
          "; ".join(wf_problems[:4]) or f"{len(list(wf_dir.glob('*.y*ml')))} workflow file(s) linted")

    # 28. schedule completeness has no UNEXPLAINED deviation left
    sc = next((c for c in q["checks"] if c["check"] == "schedule-completeness"), None)
    check("schedule-completeness-clean", bool(sc) and sc["problems"] == 0,
          "; ".join(sc["detail"][:3]) if sc and sc["problems"] else
          "every season matches its verified league length (or is documented)")
    return print_summary()


def print_summary() -> int:
    fails = [r for r in report if r["status"] == FAIL]
    if fails:
        # A CI failure must be diagnosable from the log alone: print the first
        # failing checks with their detail instead of only a count.
        print("-- failing checks --", flush=True)
        for r in fails[:25]:
            print(f"  FAIL {r['check']}: {r['detail'][:400]}", flush=True)
    print(f"== audit: {len(report) - len(fails)}/{len(report)} passed ==", flush=True)
    (ROOT / "data" / "audit_report.json").write_text(json.dumps(report, indent=1))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
