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
    return print_summary()


def print_summary() -> int:
    fails = [r for r in report if r["status"] == FAIL]
    print(f"== audit: {len(report) - len(fails)}/{len(report)} passed ==", flush=True)
    (ROOT / "data" / "audit_report.json").write_text(json.dumps(report, indent=1))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
