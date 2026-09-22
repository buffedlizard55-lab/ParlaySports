"""CLI orchestration: seed | backtest | forward | settle | quality | export.

Examples:
  python -m parlaysports.run seed
  python -m parlaysports.run backtest --strategy S-NFL-01
  python -m parlaysports.run forward --days 7
  python -m parlaysports.run settle
  python -m parlaysports.run quality
  python -m parlaysports.run export
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from . import books, engine, export, ingest, quality, ratings, sources, store
from .config import DB_PATH
from .strategies import CATALOG_BY_ID, MULTI_SOURCES, install_catalog
from .util import utcnow_iso


def cmd_seed(args) -> dict:
    from scripts import seed as seedmod  # noqa: PLC0415 (runner import)
    return seedmod.main()


def cmd_backtest(args) -> dict:
    con = store.connect()
    out = {}
    ids = [args.strategy] if args.strategy else [s for s in CATALOG_BY_ID
                                                 if s not in MULTI_SOURCES]
    for sid in ids:
        out[sid] = engine.run_backtest(con, sid)
        print(f"backtest {sid}: {out[sid]}", flush=True)
    store.set_meta(con, "last_backtest_utc", utcnow_iso())
    con.commit()
    return out


def cmd_forward(args) -> dict:
    con = store.connect()
    days = args.days
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slates = [(datetime.now(timezone.utc) + timedelta(days=i)).strftime("%Y-%m-%d")
              for i in range(days)]
    # Only slates that actually have scheduled REG-type games.
    have = con.execute(
        f"""SELECT DISTINCT game_date FROM games WHERE status='scheduled'
            AND game_date IN ({','.join('?' for _ in slates)})
            AND ((sport='NFL' AND game_type IN ('REG','POST'))
              OR (sport='MLB' AND game_type='R')
              OR (sport='NHL' AND game_type IN ('REG','POST'))
              OR (sport='NBA' AND game_type IN ('REG','POST')))""",
        slates).fetchall()
    slate_list = [r["game_date"] for r in have]
    out = {"slates": slate_list, "today": today}
    ids = [args.strategy] if args.strategy else list(CATALOG_BY_ID.keys())
    for sid in ids:
        if sid == "S-MULTI-04":
            # lottery: at most one ticket per ISO week
            week = datetime.now(timezone.utc).strftime("%G-W%V")
            exists = con.execute(
                "SELECT 1 FROM parlays WHERE strategy_id='S-MULTI-04' "
                "AND test_mode='forward' AND note LIKE ? LIMIT 1",
                (f"%{week}%",)).fetchone()
            if exists:
                out[sid] = {"skipped": f"weekly ticket already exists for {week}"}
                continue
        r = engine.run_forward(con, sid, slate_list)
        out[sid] = r
        print(f"forward {sid}: {r}", flush=True)
    store.set_meta(con, "last_forward_utc", utcnow_iso())
    con.commit()
    return out


def cmd_settle(args) -> dict:
    con = store.connect()
    out = engine.settle_all(con)
    print(f"settle: {out}", flush=True)
    return out


def cmd_quality(args) -> dict:
    con = store.connect()
    out = quality.run_checks(con)
    store.set_meta(con, "last_quality_run", json.dumps(out))
    store.set_meta(con, "last_quality_utc", utcnow_iso())
    con.commit()
    print(f"quality: {out['total_problems']} problems", flush=True)
    return out


def cmd_export(args) -> dict:
    con = store.connect()
    out = export.export_all(con)
    print(f"export: {out}", flush=True)
    return out


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(prog="parlaysports")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    b = sub.add_parser("backtest")
    b.add_argument("--strategy", default=None)
    f = sub.add_parser("forward")
    f.add_argument("--days", type=int, default=7)
    f.add_argument("--strategy", default=None)
    sub.add_parser("settle")
    sub.add_parser("quality")
    sub.add_parser("export")
    args = ap.parse_args(argv)
    return {"seed": cmd_seed, "backtest": cmd_backtest, "forward": cmd_forward,
            "settle": cmd_settle, "quality": cmd_quality,
            "export": cmd_export}[args.cmd](args)


if __name__ == "__main__":
    print(json.dumps(main(), indent=1, default=str))
