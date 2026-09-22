"""PASS 2 audit: mechanical verification of every prompt requirement.

Fails (exit 1) with a checklist when anything is missing or contradictory.
Run:  python scripts/audit.py
"""
from __future__ import annotations

import json
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
    # multi forward only
    n = con.execute("SELECT COUNT(*) AS n FROM parlays WHERE strategy_id LIKE 'S-MULTI-%'"
                    " AND test_mode='backtest'").fetchone()["n"]
    check("multi-never-backtested", n == 0, f"{n} (must be 0)")
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
            if c["check"] in ("ledger-chain", "settlement-recompute", "backtest-leakage")
            and c["problems"] > 0]
    check("hard-quality-gates", not hard, "; ".join(
        f"{c['check']}:{c['problems']}" for c in hard) or "chain+settle+leakage clean")
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
                       "missing-historical-periods"}
    lack = sorted(required_checks - names)
    check("quality-registry-complete", not lack, ",".join(lack) if lack else "13 checks live")
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
    return print_summary()


def print_summary() -> int:
    fails = [r for r in report if r["status"] == FAIL]
    print(f"== audit: {len(report) - len(fails)}/{len(report)} passed ==", flush=True)
    (ROOT / "data" / "audit_report.json").write_text(json.dumps(report, indent=1))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
