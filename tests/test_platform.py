"""Unit + integration tests (stdlib unittest, no dependencies).

Covers: odds math, parlay math, leg/parlay settlement incl. push-reduction,
ledger append-only + hash chain, no-lookahead (ratings/signals), parlay
construction guards, pricing grades, settlement immutability.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from parlaysports import ratings, store
from parlaysports.parlay import (build_parlays, pricing_grade, settle_leg,
                                 settle_parlay)
from parlaysports.strategies import CATALOG_BY_ID
from parlaysports.util import (american_to_decimal, american_to_prob,
                               decimal_to_american, devig_two_way, elo_expected,
                               kelly_fraction, log5, parlay_decimal, prob_to_american,
                               pythag)


class TestOddsMath(unittest.TestCase):
    def test_american_roundtrip(self):
        for o in (-10000, -1000, -550, -110, -101, 100, 150, 1000):
            d = american_to_decimal(o)
            back = decimal_to_american(d)
            self.assertAlmostEqual(back, o, places=6)

    def test_prob_roundtrip(self):
        for p in (0.01, 0.25, 0.5, 0.524, 0.75, 0.99):
            self.assertAlmostEqual(american_to_prob(prob_to_american(p)), p, places=9)

    def test_devig(self):
        a, b = devig_two_way(0.55, 0.50)
        self.assertAlmostEqual(a + b, 1.0)
        self.assertGreater(a, b)

    def test_parlay_decimal(self):
        self.assertAlmostEqual(parlay_decimal([1.91, 1.91]), 1.91 ** 2)
        self.assertAlmostEqual(parlay_decimal([2.0, 3.0, 1.5]), 9.0)

    def test_kalshi_range(self):
        from parlaysports.util import kalshi_ask_to_prob
        self.assertAlmostEqual(kalshi_ask_to_prob(0.64), 0.64)
        with self.assertRaises(ValueError):
            kalshi_ask_to_prob(1.5)

    def test_kelly(self):
        self.assertEqual(kelly_fraction(0.4, 1.91), 0.0)  # no edge
        f = kelly_fraction(0.6, 2.0)
        self.assertGreater(f, 0)
        self.assertLessEqual(f, 0.03)  # capped

    def test_elo_log5_pythag(self):
        self.assertAlmostEqual(elo_expected(1500, 1500), 0.5)
        self.assertGreater(elo_expected(1600, 1500), 0.6)
        self.assertAlmostEqual(log5(0.6, 0.6), 0.5, places=6)
        self.assertGreater(log5(0.7, 0.5), 0.65)
        self.assertAlmostEqual(pythag(800, 600), 800 ** 1.83 / (800 ** 1.83 + 600 ** 1.83))


class TestSettlement(unittest.TestCase):
    def _game(self, a=20, h=17, **kw):
        g = {"status": "final", "away_score": a, "home_score": h,
             "away_team": "AWY", "home_team": "HME"}
        g.update(kw)
        return g

    def test_ml(self):
        self.assertEqual(settle_leg({"market": "ML", "selection": "away", "line": None},
                                    self._game(20, 17))["result"], "win")
        self.assertEqual(settle_leg({"market": "ML", "selection": "home", "line": None},
                                    self._game(20, 17))["result"], "loss")
        self.assertEqual(settle_leg({"market": "ML", "selection": "home", "line": None},
                                    self._game(17, 17))["result"], "push")

    def test_spread_push(self):
        leg = {"market": "SPREAD", "selection": "home", "line": -3.0}
        self.assertEqual(settle_leg(leg, self._game(17, 20))["result"], "push")
        self.assertEqual(settle_leg(leg, self._game(17, 21))["result"], "win")
        self.assertEqual(settle_leg(leg, self._game(17, 19))["result"], "loss")
        leg2 = {"market": "SPREAD", "selection": "away", "line": 6.5}
        self.assertEqual(settle_leg(leg2, self._game(20, 24))["result"], "win")
        self.assertEqual(settle_leg(leg2, self._game(20, 27))["result"], "loss")

    def test_total_push(self):
        leg = {"market": "TOTAL", "selection": "over", "line": 44.5}
        self.assertEqual(settle_leg(leg, self._game(20, 24))["result"], "loss")
        self.assertEqual(settle_leg(leg, self._game(21, 24))["result"], "win")
        leg2 = {"market": "TOTAL", "selection": "under", "line": 44.0}
        self.assertEqual(settle_leg(leg2, self._game(20, 24))["result"], "push")

    def test_parlay_win_loss(self):
        p = {"stake": 100.0, "combined_decimal": 6.0}
        legs = [{"result": "win", "odds_american": -110},
                {"result": "win", "odds_american": 150}]
        o = settle_parlay(p, legs)
        self.assertEqual(o["status"], "won")
        self.assertAlmostEqual(o["payout"], 100 * american_to_decimal(-110)
                               * american_to_decimal(150), places=2)
        legs2 = [{"result": "win", "odds_american": -110},
                 {"result": "loss", "odds_american": 150}]
        o2 = settle_parlay(p, legs2)
        self.assertEqual(o2["status"], "lost")
        self.assertEqual(o2["pnl"], -100.0)

    def test_push_reduction(self):
        p = {"stake": 100.0, "combined_decimal": 6.0}
        legs = [{"result": "win", "odds_american": 150},
                {"result": "push", "odds_american": -110}]
        o = settle_parlay(p, legs)
        self.assertEqual(o["status"], "won")
        self.assertAlmostEqual(o["payout"], 100 * american_to_decimal(150), places=2)

    def test_all_push_refund(self):
        p = {"stake": 100.0, "combined_decimal": 6.0}
        o = settle_parlay(p, [{"result": "push", "odds_american": -110},
                             {"result": "void", "odds_american": -110}])
        self.assertEqual(o["status"], "push")
        self.assertEqual(o["payout"], 100.0)
        self.assertEqual(o["pnl"], 0.0)

    def test_pending_blocks(self):
        p = {"stake": 100.0, "combined_decimal": 6.0}
        o = settle_parlay(p, [{"result": "win", "odds_american": -110},
                             {"result": "pending", "odds_american": -110}])
        self.assertEqual(o["status"], "live")


class TestGradesAndBuilder(unittest.TestCase):
    def test_grades(self):
        v = [{"odds_type": "market_verified"}]
        self.assertEqual(pricing_grade(v * 2), "VERIFIED")
        self.assertEqual(pricing_grade([{"odds_type": "market_verified"},
                                        {"odds_type": "market_reference"}]), "REFERENCE")
        self.assertEqual(pricing_grade([{"odds_type": "model_fair"}] * 2), "MODEL")
        self.assertEqual(pricing_grade([{"odds_type": "market_verified"},
                                        {"odds_type": "model_fair"}]), "MIXED")
        self.assertEqual(pricing_grade([{"odds_type": "market_verified"},
                                        {"odds_type": None}]), "UNPRICED")
        self.assertEqual(pricing_grade([]), "UNPRICED")

    def _sig(self, game_key, odds=-110, edge=0.05, mp=0.6, sport="NFL",
             market="ML", sel="home", line=None, otype="market_verified",
             sid="S-NFL-01"):
        return {"strategy_id": sid, "version": "v1", "game_key": game_key,
                "sport": sport, "market": market, "selection": sel, "line": line,
                "model_prob": mp, "market_prob": mp - edge, "edge": edge,
                "decision_utc": "2026-01-01T12:00:00Z", "features": {},
                "price_id": 1, "odds_american": odds, "odds_type": otype,
                "note": ""}

    def test_one_leg_per_game(self):
        strat = dict(CATALOG_BY_ID["S-NFL-01"])
        sigs = [self._sig("NFL:g1"), self._sig("NFL:g1", mp=0.7), self._sig("NFL:g2")]
        parlays = build_parlays(sigs, strat, "2026-01-02", "2026-01-01T12:00:00Z",
                                "backtest")
        self.assertEqual(len(parlays), 1)
        self.assertEqual(parlays[0]["n_legs"], 2)
        self.assertEqual(len({l["game_key"] for l in parlays[0]["legs"]}), 2)

    def test_multi_requires_two_sports(self):
        strat = dict(CATALOG_BY_ID["S-MULTI-01"])
        sigs = [self._sig("NFL:g1", sport="NFL"), self._sig("NFL:g2", sport="NFL")]
        self.assertEqual(build_parlays(sigs, strat, "2026-01-02",
                                       "2026-01-01T12:00:00Z", "forward"), [])
        sigs.append(self._sig("MLB:1", sport="MLB"))
        parlays = build_parlays(sigs, strat, "2026-01-02", "2026-01-01T12:00:00Z",
                                "forward")
        self.assertEqual(len(parlays), 1)
        self.assertGreaterEqual(len(parlays[0]["sports"]), 2)

    def test_unpriced_zero_stake(self):
        strat = dict(CATALOG_BY_ID["S-NFL-01"])
        sigs = [self._sig("NFL:g1", odds=None, otype=None),
                self._sig("NFL:g2", odds=None, otype=None)]
        parlays = build_parlays(sigs, strat, "2026-01-02", "2026-01-01T12:00:00Z",
                                "forward")
        self.assertEqual(parlays[0]["pricing_grade"], "UNPRICED")
        self.assertEqual(parlays[0]["stake"], 0.0)
        self.assertIsNone(parlays[0]["potential_payout"])

    def test_min_prob_guard(self):
        strat = dict(CATALOG_BY_ID["S-NFL-01"])
        sigs = [self._sig("NFL:g1", mp=0.1), self._sig("NFL:g2", mp=0.1)]
        self.assertEqual(build_parlays(sigs, strat, "2026-01-02",
                                       "2026-01-01T12:00:00Z", "backtest"), [])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_ledger_chain(self):
        store.ledger_append(self.con, strategy_id="S", version="v1", book="forward",
                            parlay_id=None, kind="stake", amount=-100.0)
        store.ledger_append(self.con, strategy_id="S", version="v1", book="forward",
                            parlay_id="p", kind="settle", amount=250.0)
        self.assertEqual(store.verify_ledger_chain(self.con), [])
        # tamper -> detected
        self.con.execute("UPDATE ledger SET amount=999 WHERE entry_id=2")
        self.assertTrue(store.verify_ledger_chain(self.con))

    def test_bankroll_moves(self):
        r = store.ledger_append(self.con, strategy_id="S", version="v1",
                                book="forward", parlay_id=None, kind="stake",
                                amount=-100.0)
        self.assertAlmostEqual(r["balance_after"], 9900.0)
        brow = self.con.execute("SELECT current_amount FROM bankroll").fetchone()
        self.assertAlmostEqual(brow["current_amount"], 9900.0)

    def test_elo_point_in_time(self):
        c = self.con
        for i, (a, h, as_, hs) in enumerate([("A", "B", 3, 1), ("B", "A", 0, 2)]):
            c.execute(
                """INSERT INTO games(game_key, sport, league_game_id, season, game_type,
                   game_date, start_utc, week_or_slate, away_team, home_team, neutral,
                   venue, status, away_score, home_score, overtime, source_id,
                   source_url, retrieved_utc, verified, verify_note, extra_json)
                   VALUES (?, 'MLB', ?, '2020', 'R', ?, NULL, NULL, ?, ?, 0, NULL,
                   'final', ?, ?, NULL, 't', 'u', '2020-01-01T00:00:00Z', 0, '', NULL)""",
                (f"MLB:{i}", str(i), f"2020-04-0{i + 1}", a, h, as_, hs))
        ratings.compute_elo(c, "MLB", ["2020"])
        # Before game 0: both 1500.
        e, u = ratings.elo_before(c, "MLB", "A", "2020-04-01", "MLB:0", "2020")
        self.assertEqual((e, u), (1500.0, 0))
        # Before game 1: A won once -> above 1500; B below.
        ea, _ = ratings.elo_before(c, "MLB", "A", "2020-04-02", "MLB:1", "2020")
        eb, _ = ratings.elo_before(c, "MLB", "B", "2020-04-02", "MLB:1", "2020")
        self.assertGreater(ea, 1500.0)
        self.assertLess(eb, 1500.0)
        # Season rollover regresses the LATEST rating toward 1500.
        after = c.execute("SELECT elo_after FROM ratings WHERE sport='MLB' AND team='A' "
                          "AND game_key='MLB:1'").fetchone()["elo_after"]
        ea2, u2 = ratings.elo_before(c, "MLB", "A", "2021-04-01", "MLB:x", "2021")
        self.assertAlmostEqual(ea2, 0.75 * after + 0.25 * 1500.0, places=6)
        self.assertLess(ea2, after)  # A was above 1500 after two wins
        self.assertEqual(u2, 0)

    def test_catalog_completeness(self):
        self.assertEqual(len(CATALOG_BY_ID), 28)
        for sid, s in CATALOG_BY_ID.items():
            for k in ("strategy_id", "version", "username", "name", "sport",
                      "hypothesis", "markets", "selection_rules",
                      "construction_rules", "required_data", "limitations"):
                self.assertTrue(s.get(k), f"{sid} missing {k}")


class TestNoLookahead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        c = self.con
        c.execute(
            """INSERT INTO games(game_key, sport, league_game_id, season, game_type,
               game_date, start_utc, week_or_slate, away_team, home_team, neutral,
               venue, status, away_score, home_score, overtime, source_id,
               source_url, retrieved_utc, verified, verify_note, extra_json)
               VALUES ('NFL:t1', 'NFL', 't1', '2020', 'REG', '2020-09-13', NULL,
               NULL, 'AWY', 'HME', 0, NULL, 'final', 20, 17, NULL, 't', 'u',
               '2020-01-01T00:00:00Z', 0, '', NULL)""")
        # Same market, two snapshots: early opener + official close.
        c.execute(
            """INSERT INTO prices(game_key, market, selection, line, odds_american,
               odds_type, source_id, source_url, observed_utc, close_flag, note)
               VALUES ('NFL:t1','ML','away',NULL,-105,'market_reference','t','u',
               '2020-09-10T00:00:00Z',0,'open')""")
        c.execute(
            """INSERT INTO prices(game_key, market, selection, line, odds_american,
               odds_type, source_id, source_url, observed_utc, close_flag, note)
               VALUES ('NFL:t1','ML','away',NULL,-150,'market_reference','t','u',
               '2020-09-13T16:00:00Z',1,'close')""")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_backtest_sees_closing_prices_only(self):
        from parlaysports.strategies import get_prices
        bt = get_prices(self.con, "NFL:t1", close_only=True)
        self.assertEqual(bt[("ML", "away")]["odds_american"], -150)
        self.assertEqual(bt[("ML", "away")]["close_flag"], 1)
        # Forward prefers the close too when one exists...
        fw = get_prices(self.con, "NFL:t1", close_only=False)
        self.assertEqual(fw[("ML", "away")]["odds_american"], -150)
        # ...but falls back to the latest snapshot when no close exists.
        self.con.execute("DELETE FROM prices WHERE close_flag=1")
        fw2 = get_prices(self.con, "NFL:t1", close_only=False)
        self.assertEqual(fw2[("ML", "away")]["odds_american"], -105)
        bt2 = get_prices(self.con, "NFL:t1", close_only=True)
        self.assertEqual(bt2, {})  # backtest refuses non-close prices

    def test_settle_preserves_decision_detail(self):
        from parlaysports import engine
        c = self.con
        c.execute(
            """INSERT INTO parlays(parlay_id, strategy_id, version, username,
               sport_scope, sports_json, n_legs, market_mix, stake, pricing_grade,
               combined_decimal, combined_american, potential_payout, decision_utc,
               slate_date, status, test_mode)
               VALUES ('T-1','S-NFL-01','v1','t','NFL','["NFL"]',1,'ML',10.0,
               'REFERENCE',1.67,150.0,16.7,'2020-09-13T12:00:00Z','2020-09-13',
               'upcoming','backtest')""")
        c.execute(
            """INSERT INTO legs(parlay_id, game_key, sport, market, selection, line,
               odds_american, odds_type, model_prob, result, leg_detail)
               VALUES ('T-1','NFL:t1','NFL','ML','away',NULL,150.0,
               'market_reference',0.6,'pending','{"price_id": 2}')""")
        c.commit()
        engine.settle_all(c, test_mode="backtest", settlement_source="test")
        leg = c.execute("SELECT result, leg_detail, settle_detail FROM legs").fetchone()
        self.assertEqual(leg["result"], "win")  # AWY 20 @ HME 17
        self.assertEqual(leg["leg_detail"], '{"price_id": 2}')  # untouched
        self.assertIn("20", leg["settle_detail"])  # result recorded separately
        row = c.execute("SELECT payout, pnl, roi_parlay FROM parlays").fetchone()
        self.assertAlmostEqual(row["pnl"], 15.0)      # payout 25 - stake 10
        self.assertAlmostEqual(row["payout"], 25.0)   # +150 -> decimal 2.5 x $10
        self.assertAlmostEqual(row["roi_parlay"], 1.5)


class TestPass2Fixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_insert_parlay_never_duplicates_legs(self):
        parlay = {
            "parlay_id": "BA-T-001", "strategy_id": "S-NFL-01", "version": "v1",
            "username": "t", "sport_scope": "NFL", "sports": ["NFL"], "n_legs": 1,
            "market_mix": "ML", "stake": 10.0, "pricing_grade": "REFERENCE",
            "combined_decimal": 2.0, "combined_american": 100.0,
            "potential_payout": 20.0, "model_prob": 0.5,
            "decision_utc": "2026-01-01T12:00:00Z", "slate_date": "2026-01-02",
            "test_mode": "backtest",
            "legs": [{"strategy_id": "S-NFL-01", "version": "v1", "game_key": "NFL:x",
                      "sport": "NFL", "market": "ML", "selection": "home", "line": None,
                      "odds_american": 100.0, "odds_type": "market_reference",
                      "model_prob": 0.5, "features": {}, "price_id": 1, "note": "",
                      "decision_utc": "2026-01-01T12:00:00Z"}],
        }
        from parlaysports.parlay import insert_parlay
        insert_parlay(self.con, parlay)
        insert_parlay(self.con, parlay)  # second call must be a no-op for legs
        n = self.con.execute("SELECT COUNT(*) AS n FROM legs WHERE parlay_id='BA-T-001'"
                             ).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_score_correction_is_logged_not_silent(self):
        from parlaysports.ingest import _insert_game
        g = {"game_key": "MLB:1", "sport": "MLB", "league_game_id": "1",
             "season": "2020", "game_type": "R", "game_date": "2020-04-01",
             "start_utc": None, "week_or_slate": None, "away_team": "A",
             "home_team": "B", "neutral": 0, "venue": None, "status": "final",
             "away_score": 3, "home_score": 1, "overtime": None,
             "source_id": "t1", "source_url": "u", "retrieved_utc": "2020-01-01T00:00:00Z",
             "verified": 0, "verify_note": "", "extra_json": None}
        _insert_game(self.con, dict(g))
        g2 = dict(g, source_id="t2", away_score=4)
        _insert_game(self.con, g2)  # conflicting final -> issue filed
        issues = self.con.execute("SELECT detail FROM issues").fetchall()
        self.assertTrue(any("conflicting-results" in i["detail"] and "3-1" in i["detail"]
                            for i in issues))
        # identical re-import of the CURRENT row -> no new issue
        _insert_game(self.con, dict(g2, source_id="t3"))
        n_after = self.con.execute("SELECT COUNT(*) AS n FROM issues").fetchone()["n"]
        self.assertEqual(n_after, len(issues))

    def test_cover_mapping_excludes_pushes(self):
        from parlaysports.strategies import _cover_mapping_uncached, clear_cover_cache
        c = self.con
        for i, (ml, line, aw, hm) in enumerate([
                (-200, -3.0, 10, 20),   # fav covers (home -3 wins by 10)
                (-200, -3.0, 17, 20),   # push on 3
                (-200, -3.0, 18, 20),   # fav fails
                (-200, -3.0, 10, 20)] * 12):  # 48 rows, 36 non-push
            c.execute("""INSERT INTO games(game_key, sport, league_game_id, season,
               game_type, game_date, start_utc, week_or_slate, away_team, home_team,
               neutral, venue, status, away_score, home_score, overtime, source_id,
               source_url, retrieved_utc, verified, verify_note, extra_json)
               VALUES (?, 'NFL', ?, '2020', 'REG', ?, NULL, NULL, 'A', 'H', 0, NULL,
               'final', ?, ?, NULL, 't', 'u', 'x', 0, '', NULL)""",
                      (f"NFL:{i}", str(i), f"2020-09-{10 + i // 4:02d}", aw, hm))
            c.execute("""INSERT INTO prices(game_key, market, selection, line,
               odds_american, odds_type, source_id, source_url, observed_utc,
               close_flag, note) VALUES (?, 'SPREAD', 'home', ?, -110,
               'market_reference', 't', 'u', 'x', 1, '')""", (f"NFL:{i}", line))
            c.execute("""INSERT INTO prices(game_key, market, selection, line,
               odds_american, odds_type, source_id, source_url, observed_utc,
               close_flag, note) VALUES (?, 'ML', 'home', NULL, ?,
               'market_reference', 't', 'u', 'x', 1, '')""", (f"NFL:{i}", ml))
        c.commit()
        clear_cover_cache()
        # american -200 -> implied 0.6667 lives in the 0.65-0.70 bucket
        cov = _cover_mapping_uncached(c, "NFL", "2021", "fav", 0.65, 0.70)
        # 36 decided games (12 pushes dropped): 24 covers -> 2/3 exactly
        self.assertIsNotNone(cov)
        self.assertAlmostEqual(cov, 24 / 36, places=6)

    def test_missing_historical_periods_flagged(self):
        from parlaysports import quality
        q = quality.run_checks(self.con)
        names = {c["check"] for c in q["checks"]}
        self.assertIn("missing-historical-periods", names)
        mm = next(c for c in q["checks"] if c["check"] == "missing-historical-periods")
        self.assertGreater(mm["problems"], 0)  # empty DB: every season is a gap

    def test_forward_refuses_past_gameday_without_start(self):
        from parlaysports import engine
        c = self.con
        c.execute("""INSERT INTO games(game_key, sport, league_game_id, season, game_type,
           game_date, start_utc, week_or_slate, away_team, home_team, neutral, venue,
           status, away_score, home_score, overtime, source_id, source_url,
           retrieved_utc, verified, verify_note, extra_json)
           VALUES ('NFL:past','NFL','p','2026','REG','2026-09-21',NULL,NULL,'A','B',
           0,NULL,'scheduled',NULL,NULL,NULL,'t','u','x',0,'',NULL)""")
        c.execute("""INSERT INTO prices(game_key, market, selection, line, odds_american,
           odds_type, source_id, source_url, observed_utc, close_flag, note)
           VALUES ('NFL:past','ML','away',NULL,-110,'market_reference','t','u','x',0,'')""")
        c.execute("""INSERT INTO prices(game_key, market, selection, line, odds_american,
           odds_type, source_id, source_url, observed_utc, close_flag, note)
           VALUES ('NFL:past','ML','home',NULL,-110,'market_reference','t','u','x',0,'')""")
        c.commit()
        r = engine.run_forward(c, "S-NFL-01", ["2026-09-21"],
                               decision_utc="2026-09-22T01:30:00Z")
        self.assertEqual(r["parlays"], 0)
        self.assertGreaterEqual(r["skipped_past"], 1)

    def test_catalog_version_immutable(self):
        from parlaysports.strategies import install_catalog
        c = self.con
        install_catalog(c)
        c.execute("UPDATE strategies SET hypothesis='TAMPERED' WHERE strategy_id='S-NFL-01'")
        c.commit()
        install_catalog(c)  # must refuse to overwrite the changed row
        row = c.execute("SELECT hypothesis FROM strategies WHERE strategy_id='S-NFL-01'"
                        ).fetchone()
        self.assertEqual(row["hypothesis"], "TAMPERED")  # preserved + issue filed
        self.assertTrue(any("refusing to overwrite" in i["detail"] for i in
                            c.execute("SELECT detail FROM issues")))


class TestMultiBacktest(unittest.TestCase):
    """Cross-sport overlap engine (R-017): one decision clock, close-only
    prices, window-overlap slates only, lottery weekly cap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        c = self.con
        # Point-in-time ratings pinned BEFORE every test slate: strong home
        # teams in both sports so S-NFL-01/S-NHL-01 fire on ML closes.
        for sport, season, away, home in (("NFL", "2025", "NA", "NH"),
                                          ("NHL", "20252026", "LA", "LH")):
            for team, elo in ((away, 1300.0), (home, 1700.0)):
                c.execute(
                    """INSERT INTO ratings(sport, team, season, game_date, game_key,
                       elo_before, elo_after, games_used, model_version)
                       VALUES (?, ?, ?, '2025-10-20', ?, ?, ?, 50, 'elo-v1')""",
                    (sport, team, season, f"{sport}:prior", elo, elo))
        # One verified overlap slate: NFL + NHL finals on 2025-11-05.
        self._dup_slate("2025-11-05", "NFL:n1", "NHL:h1")
        c.commit()

    def tearDown(self):
        from parlaysports import engine
        from parlaysports.strategies import clear_cover_cache
        engine.clear_multi_signal_cache()
        clear_cover_cache()
        self.con.close()
        self.tmp.cleanup()

    def _game(self, key, sport, season, date, away, home, status="final",
              a=17, h=24, start=None):
        final = status == "final"
        self.con.execute(
            """INSERT INTO games(game_key, sport, league_game_id, season, game_type,
               game_date, start_utc, week_or_slate, away_team, home_team, neutral,
               venue, status, away_score, home_score, overtime, source_id,
               source_url, retrieved_utc, verified, verify_note, extra_json)
               VALUES (?, ?, ?, ?, 'REG', ?, ?, NULL, ?, ?, 0, NULL, ?, ?, ?, NULL,
               't', 'u', '2025-10-01T00:00:00Z', 1, '', NULL)""",
            (key, sport, key, season, date, start or f"{date}T23:00:00Z",
             away, home, status, a if final else None, h if final else None))

    def _ml(self, key, home_odds=-150.0, away_odds=130.0, close=1):
        for sel, o in (("home", home_odds), ("away", away_odds)):
            self.con.execute(
                """INSERT INTO prices(game_key, market, selection, line, odds_american,
                   odds_type, source_id, source_url, observed_utc, close_flag, note)
                   VALUES (?, 'ML', ?, NULL, ?, 'market_reference', 't', 'u',
                   '2025-10-01T00:00:00Z', ?, '')""", (key, sel, o, close))

    def _dup_slate(self, date, nfl_key, nhl_key, close=1, status="final",
                   nfl_odds=(-160.0, 140.0), nhl_odds=(-150.0, 130.0)):
        self._game(nfl_key, "NFL", "2025", date, "NA", "NH", status=status)
        self._game(nhl_key, "NHL", "20252026", date, "LA", "LH", status=status)
        self._ml(nfl_key, nfl_odds[0], nfl_odds[1], close)
        self._ml(nhl_key, nhl_odds[0], nhl_odds[1], close)
        self.con.commit()

    def test_cross_sport_ticket_builds_and_settles(self):
        from parlaysports import engine
        r = engine.run_backtest_multi(self.con, "S-MULTI-01")
        self.assertEqual(r["slates"], 1)
        self.assertEqual(r["parlays"], 1)
        self.assertEqual(r["legs"], 2)
        p = self.con.execute(
            "SELECT * FROM parlays WHERE parlay_id='BA-S-MULTI-01-20251105-01'").fetchone()
        self.assertIsNotNone(p)
        self.assertEqual(sorted(json.loads(p["sports_json"])), ["NFL", "NHL"])
        self.assertEqual(p["status"], "won")        # both home teams won 24-17
        self.assertEqual(p["test_mode"], "backtest")
        self.assertEqual(p["pricing_grade"], "REFERENCE")
        # ledger: open + stake + settle, chain clean, bankroll exact
        self.assertEqual(store.verify_ledger_chain(self.con), [])
        bal = self.con.execute(
            "SELECT current_amount FROM bankroll WHERE strategy_id='S-MULTI-01' "
            "AND book='backtest'").fetchone()
        expected = 10000.0 - 10.0 + 10.0 * (1 + 100 / 160) * (1 + 100 / 150)
        self.assertAlmostEqual(bal["current_amount"], round(expected, 2), places=2)

    def test_single_sport_slate_never_backtested(self):
        from parlaysports import engine
        # 2025-11-12 holds an NFL final only: not a >=2-sport overlap date.
        self._game("NFL:only", "NFL", "2025", "2025-11-12", "NA", "NH")
        self._ml("NFL:only", -160.0, 140.0)
        self.con.commit()
        r = engine.run_backtest_multi(self.con, "S-MULTI-01")
        self.assertEqual(r["slates"], 1)   # only 2025-11-05 is a candidate
        self.assertEqual(r["parlays"], 1)
        n = self.con.execute("SELECT COUNT(*) AS n FROM parlays WHERE slate_date='2025-11-12'"
                             ).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_overlap_slate_uses_close_prices_only(self):
        from parlaysports import engine
        # 2025-11-08: NHL legs only carry a NON-close snapshot -> backtest
        # sees one sport only and must not build a ticket for that slate.
        self._dup_slate("2025-11-08", "NFL:n2", "NHL:h2", close=0)
        r = engine.run_backtest_multi(self.con, "S-MULTI-01")
        self.assertEqual(r["slates"], 2)
        self.assertEqual(r["parlays"], 1)  # only the 11-05 close-priced slate
        n = self.con.execute("SELECT COUNT(*) AS n FROM parlays WHERE slate_date='2025-11-08'"
                             ).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_lotto_backtest_weekly_cap(self):
        from parlaysports import engine
        # W45: 2025-11-05 (setUp) + 2025-11-08; W46: 2025-11-12.
        self._dup_slate("2025-11-08", "NFL:n2", "NHL:h2")
        self._dup_slate("2025-11-12", "NFL:n3", "NHL:h3")
        r = engine.run_backtest_multi(self.con, "S-MULTI-04")
        self.assertEqual(r["parlays"], 2)          # one per ISO week
        self.assertEqual(r["skipped_week"], 1)     # 11-08 shares W45 with 11-05
        rows = self.con.execute(
            "SELECT parlay_id, slate_date, stake, note FROM parlays "
            "WHERE strategy_id='S-MULTI-04' ORDER BY slate_date").fetchall()
        self.assertEqual([r["slate_date"] for r in rows], ["2025-11-05", "2025-11-12"])
        self.assertIn("2025-W45", rows[0]["note"])
        self.assertIn("2025-W46", rows[1]["note"])
        self.assertTrue(all(r["stake"] == 25.0 for r in rows))
        # re-running is idempotent and the week tags block re-filing
        r2 = engine.run_backtest_multi(self.con, "S-MULTI-04")
        self.assertEqual(r2["parlays"], 0)
        self.assertEqual(r2["skipped_week"], 3)

    def test_run_backtest_routes_multi_to_overlap_engine(self):
        from parlaysports import engine
        with self.assertRaises(ValueError) as ctx:
            engine.run_backtest(self.con, "S-MULTI-01")
        self.assertIn("run_backtest_multi", str(ctx.exception))
        with self.assertRaises(ValueError):
            engine.run_backtest_multi(self.con, "S-NFL-01")

    def test_forward_lotto_weekly_cap_stamped_at_creation(self):
        from parlaysports import engine
        # Two scheduled W47 slates (2025-11-19 Wed, 2025-11-22 Sat).
        self._dup_slate("2025-11-19", "NFL:f1", "NHL:g1", close=0, status="scheduled")
        self._dup_slate("2025-11-22", "NFL:f2", "NHL:g2", close=0, status="scheduled")
        r = engine.run_forward(self.con, "S-MULTI-04", ["2025-11-19", "2025-11-22"],
                               decision_utc="2025-11-18T12:00:00Z")
        self.assertEqual(r["parlays"], 1)          # cap: one ticket in 2025-W47
        self.assertEqual(r["skipped_week"], 1)
        row = self.con.execute(
            "SELECT slate_date, note FROM parlays WHERE strategy_id='S-MULTI-04'").fetchone()
        self.assertEqual(row["slate_date"], "2025-11-19")
        self.assertIn("2025-W47", row["note"])
        # A later run in the same week reads the stamp from the record and skips.
        r2 = engine.run_forward(self.con, "S-MULTI-04", ["2025-11-20"],
                                decision_utc="2025-11-18T13:00:00Z")
        self.assertEqual(r2["parlays"], 0)
        self.assertEqual(r2["skipped_week"], 1)


class TestMultiRules(unittest.TestCase):
    """R-018: the builder must honor versioned catalog construction rules."""

    def _sig(self, game_key, sport="NFL", edge=0.05, mp=0.6, odds=-110,
             otype="market_verified"):
        return {"strategy_id": "S-MULTI-01", "version": "v1", "game_key": game_key,
                "sport": sport, "market": "ML", "selection": "home", "line": None,
                "model_prob": mp, "market_prob": mp - edge, "edge": edge,
                "decision_utc": "2026-01-01T12:00:00Z", "features": {},
                "price_id": 1, "odds_american": odds, "odds_type": otype, "note": ""}

    def test_max_per_sport_cap_enforced(self):
        strat = dict(CATALOG_BY_ID["S-MULTI-01"])
        self.assertEqual(strat.get("max_per_sport"), 1)  # catalog carries the cap
        sigs = [self._sig("NFL:g1"), self._sig("NFL:g2"),
                self._sig("MLB:m1", sport="MLB")]
        parlays = build_parlays(sigs, strat, "2026-01-02",
                                "2026-01-01T12:00:00Z", "forward")
        self.assertEqual(len(parlays), 1)
        self.assertEqual(sorted(l["sport"] for l in parlays[0]["legs"]),
                         ["MLB", "NFL"])
        # Without the cap the same pool would pack all three legs together.
        uncapped = {k: v for k, v in strat.items() if k != "max_per_sport"}
        p2 = build_parlays(sigs, uncapped, "2026-01-02",
                           "2026-01-01T12:00:00Z", "forward")
        self.assertEqual(p2[0]["n_legs"], 3)


class TestSettleSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        c = self.con
        c.execute(
            """INSERT INTO games(game_key, sport, league_game_id, season, game_type,
               game_date, start_utc, week_or_slate, away_team, home_team, neutral,
               venue, status, away_score, home_score, overtime, source_id,
               source_url, retrieved_utc, verified, verify_note, extra_json)
               VALUES ('NFL:s1','NFL','s1','2020','REG','2020-09-13',NULL,NULL,
               'AWY','HME',0,NULL,'final',20,17,NULL,'t','u',
               '2020-01-01T00:00:00Z',0,'',NULL)""")
        for pid, market, sel, line, odds, dec in (
                ("S-W", "ML", "away", None, -110.0, 1.909),
                ("S-L", "ML", "home", None, -110.0, 1.909),
                ("S-P", "SPREAD", "home", 3.0, -110.0, 1.909)):
            c.execute(
                """INSERT INTO parlays(parlay_id, strategy_id, version, username,
                   sport_scope, sports_json, n_legs, market_mix, stake, pricing_grade,
                   combined_decimal, combined_american, potential_payout, decision_utc,
                   slate_date, status, test_mode)
                   VALUES (?,'S-NFL-01','v1','t','NFL','["NFL"]',1,?,10.0,
                   'REFERENCE',?,-110.0,19.09,'2020-09-13T12:00:00Z','2020-09-13',
                   'upcoming','backtest')""", (pid, market, dec))
            c.execute(
                """INSERT INTO legs(parlay_id, game_key, sport, market, selection, line,
                   odds_american, odds_type, model_prob, result, leg_detail)
                   VALUES (?,'NFL:s1','NFL',?,?,?,-110.0,'market_reference',0.55,
                   'pending','{}')""", (pid, market, sel, line))
        c.commit()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_summary_counts_win_loss_push(self):
        from parlaysports import engine
        out = engine.settle_all(self.con, test_mode="backtest")
        self.assertEqual(out["won"], 1)
        self.assertEqual(out["lost"], 1)
        self.assertEqual(out["push"], 1)   # S-P: home +3 on a 3-point win = push
        self.assertEqual(out["still_live"], 0)
        self.assertEqual(store.verify_ledger_chain(self.con), [])
        # push refunded the stake; won paid 10 * 1.909...; lost kept -10
        st = {r["parlay_id"]: r["status"] for r in
              self.con.execute("SELECT parlay_id, status FROM parlays")}
        self.assertEqual(st, {"S-W": "won", "S-L": "lost", "S-P": "push"})


class TestPass3Fixes(unittest.TestCase):
    """PASS-3 line-by-line verification fixes (data labelling, economic time,
    provenance-gated updates and nightly matching)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _game(self, key, sport="NFL", season="2026", gtype="REG", date="2026-09-21",
              away="NYG", home="LA", status="scheduled", a=None, h=None, **kw):
        from parlaysports.ingest import _insert_game
        g = {"game_key": key, "sport": sport, "league_game_id": key.split(":", 1)[1],
             "season": season, "game_type": gtype, "game_date": date,
             "start_utc": None, "week_or_slate": None, "away_team": away,
             "home_team": home, "neutral": 0, "venue": None, "status": status,
             "away_score": a, "home_score": h, "overtime": None,
             "source_id": "t", "source_url": "u",
             "retrieved_utc": "2026-09-22T01:30:00Z", "verified": 0,
             "verify_note": "", "extra_json": None}
        g.update(kw)
        _insert_game(self.con, g)
        return g

    # ---- verified league facts drive the season-length expectations -------
    def test_season_length_uses_verified_league_facts(self):
        from parlaysports.config import season_length
        self.assertEqual(season_length("NHL", "20262027"), 84)   # new CBA, first since 1993-94
        self.assertEqual(season_length("NHL", "20252026"), 82)
        self.assertEqual(season_length("NFL", "2020"), 16)
        self.assertEqual(season_length("NFL", "2021"), 17)
        self.assertEqual(season_length("MLB", "2024"), 162)
        self.assertEqual(season_length("NBA", "2024-25"), 82)

    def test_known_short_seasons_are_documented(self):
        from parlaysports.engine import EXPECTED_HISTORY
        self.assertIn("2022", EXPECTED_HISTORY["NFL"]["known_short"])   # BUF@CIN no contest
        self.assertIn("2024", EXPECTED_HISTORY["MLB"]["known_short"])   # HOU@CLE rainout
        self.assertIn("20262027", EXPECTED_HISTORY["NHL"]["in_progress"])

    # ---- NBA game_type is derived from verified boundaries, never guessed --
    def test_nba_game_type_boundaries(self):
        from parlaysports.ingest import _nba_game_type
        t, ev = _nba_game_type("2023-24", "2023-10-05")
        self.assertEqual(t, "PRE")
        self.assertIn("http", ev)                       # evidence is a citation
        self.assertEqual(_nba_game_type("2023-24", "2023-10-24")[0], "REG")
        self.assertEqual(_nba_game_type("2023-24", "2024-04-14")[0], "REG")
        self.assertEqual(_nba_game_type("2023-24", "2024-04-16")[0], "POST")
        self.assertEqual(_nba_game_type("2026-27", "2026-10-16")[0], "PRE")
        self.assertEqual(_nba_game_type("2026-27", "2026-10-20")[0], "REG")
        # a season with no verified boundary keeps the inherited label
        self.assertEqual(_nba_game_type("2015-16", "2015-10-27")[0], "REG")

    def test_schedule_completeness_flags_overlength_and_spread(self):
        from parlaysports.quality import _schedule_completeness
        c = self.con
        # 4-team NBA season where one team is given 90 REG games (mislabeled
        # exhibitions/playoffs) and another only 78 -> both must be reported.
        teams = ["AAA", "BBB", "CCC", "DDD"]
        i = 0
        # 28 meetings per pairing -> 84 games per team, i.e. two above the
        # verified 82-game NBA regular season (mislabeled exhibitions).
        for a in range(len(teams)):
            for b in range(a + 1, len(teams)):
                for _ in range(28):
                    i += 1
                    self._game(f"NBA:t{i}", sport="NBA", season="2099-00", gtype="REG",
                               date=f"2099-11-{(i % 28) + 1:02d}", away=teams[a],
                               home=teams[b], status="final", a=110, h=104)
        out = _schedule_completeness(c)
        self.assertTrue(any("NBA 2099-00" in o and "exceed" in o for o in out), out)

    def test_schedule_completeness_accepts_verified_short_season(self):
        from parlaysports.quality import _schedule_completeness
        # NFL 2022 shape: 32 teams, BUF and CIN one game short (verified no
        # contest). Nothing may be reported for a documented known_short season.
        # Real 2022 shape: 32 teams, everyone 17 games except BUF (T00) and
        # CIN (T01) at 16 -- the cancelled week-17 no contest. 271 games.
        teams = [f"T{i:02d}" for i in range(32)]
        i = 0
        for cycle in range(8):            # 8 cycles x 32 games = 256, 16/team
            for a in range(32):
                i += 1
                self._game(f"NFL:s{i}", sport="NFL", season="2022", gtype="REG",
                           date=f"2022-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
                           away=teams[a], home=teams[(a + 1) % 32],
                           status="final", a=20, h=17)
        for a in range(2, 32, 2):         # 15 more games: T02..T31 reach 17
            i += 1
            self._game(f"NFL:s{i}", sport="NFL", season="2022", gtype="REG",
                       date=f"2022-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
                       away=teams[a], home=teams[a + 1], status="final", a=20, h=17)
        self.assertEqual(i, 271)
        out = [o for o in _schedule_completeness(self.con) if o.startswith("NFL 2022")]
        self.assertEqual(out, [])

    # ---- verified updates: provenance is mandatory, results are applied ----
    def test_verified_updates_require_full_provenance(self):
        import json as _json
        from parlaysports.ingest import ingest_verified_updates
        d = Path(self.tmp.name) / "updates"
        d.mkdir()
        (d / "bad.json").write_text(_json.dumps({"updates": [
            {"game_key": "NFL:x", "sport": "NFL", "status": "final",
             "away_score": 1, "home_score": 2}]}))          # no source/url/stamp
        (d / "good.json").write_text(_json.dumps({"updates": [
            {"game_key": "NFL:2026_02_NYG_LA", "sport": "NFL", "season": "2026",
             "game_type": "REG", "game_date": "2026-09-21", "away_team": "NYG",
             "home_team": "LA", "status": "final", "away_score": 6, "home_score": 28,
             "source_id": "SRC_ESPN_SCOREBOARD",
             "source_url": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                           "scoreboard?dates=20260921",
             "retrieved_utc": "2026-09-22T18:45:00Z",
             "verification": {"subject": "NYG @ LAR 2026-09-21",
                              "claim": "Rams 28, Giants 6",
                              "source_url": "https://www.espn.com/nfl/",
                              "result": "match", "detail": "scoreboard event 401872947"}}]}))
        self._game("NFL:2026_02_NYG_LA")
        out = ingest_verified_updates(self.con, d)
        self.assertEqual(out["refused"], 1)
        self.assertEqual(out["updated"], 1)
        row = self.con.execute("SELECT status, away_score, home_score, verified, "
                               "verify_note FROM games WHERE game_key='NFL:2026_02_NYG_LA'"
                               ).fetchone()
        self.assertEqual((row["status"], row["away_score"], row["home_score"]),
                         ("final", 6, 28))
        self.assertEqual(row["verified"], 1)
        self.assertIn("good.json", row["verify_note"])
        v = self.con.execute("SELECT COUNT(*) AS n FROM verifications "
                             "WHERE subject LIKE '%NYG @ LAR%'").fetchone()["n"]
        self.assertEqual(v, 1)
        self.assertTrue(self.con.execute(
            "SELECT COUNT(*) AS n FROM issues WHERE detail LIKE '%refused%'").fetchone()["n"])
        # the refused row was never written
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM games WHERE game_key='NFL:x'").fetchone())

    # ---- equity curves are economic-time and reconcile exactly -------------
    def test_equity_curve_is_economic_time_and_reconciles(self):
        from parlaysports import books
        c = self.con
        # A replayed backtest writes every stake before settling: insertion
        # order would show an impossible trough, economic order must not.
        for pid, dec in (("p1", "2026-01-01T12:00:00Z"), ("p2", "2026-01-02T12:00:00Z")):
            store.ledger_append(c, strategy_id="S", version="v1", book="backtest",
                                parlay_id=pid, kind="stake", amount=-100.0,
                                entry_ts=dec)
        for pid, dec, amt in (("p1", "2026-01-01T12:00:00Z", 190.0),
                              ("p2", "2026-01-02T12:00:00Z", 0.0)):
            store.ledger_append(c, strategy_id="S", version="v1", book="backtest",
                                parlay_id=pid, kind="settle", amount=amt,
                                entry_ts=dec)
        c.commit()
        summ = books.strategy_books(c, "S", "v1")["backtest"]
        self.assertEqual(summ["ledger_drift"], 0.0)
        self.assertAlmostEqual(summ["bankroll"], 10000.0 - 200.0 + 190.0, places=2)
        balances = [e["b"] for e in summ["equity"]]
        # the book-open row (amount 0) anchors the curve at the starting bankroll
        self.assertEqual(balances[0], 10000.0)
        self.assertEqual(balances[1:], [9900.0, 10090.0, 9990.0, 9990.0])
        self.assertLessEqual(summ["max_drawdown"], 0.02)
        self.assertAlmostEqual(summ["min_balance"], 9900.0, places=2)
        self.assertEqual(store.verify_ledger_chain(c), [])

    def test_bankroll_integrity_flags_impossible_states(self):
        from parlaysports.quality import _bankroll_integrity
        c = self.con
        store.ledger_append(c, strategy_id="S", version="v1", book="backtest",
                            parlay_id="p", kind="stake", amount=-20000.0,
                            entry_ts="2026-01-01T12:00:00Z")
        c.commit()
        out = _bankroll_integrity(c)
        self.assertTrue(any("negative" in o for o in out), out)
        self.assertTrue(any("exceeds 100%" in o for o in out), out)

    # ---- price/form time-travel guards ------------------------------------
    def _parlay_with_leg(self, decision, features, price_id=None, note="MODEL-grade leg"):
        from parlaysports.parlay import insert_parlay
        insert_parlay(self.con, {
            "parlay_id": "FW-T-1", "strategy_id": "S-MLB-01", "version": "v1",
            "username": "t", "sport_scope": "MLB", "sports": ["MLB"], "n_legs": 1,
            "market_mix": "ML", "stake": 0.0, "pricing_grade": "MODEL",
            "combined_decimal": 1.9, "combined_american": -111.0,
            "potential_payout": 0.0, "model_prob": 0.55,
            "decision_utc": decision, "slate_date": decision[:10],
            "test_mode": "forward", "status": "upcoming",
            "legs": [{"strategy_id": "S-MLB-01", "version": "v1", "game_key": "MLB:1",
                      "sport": "MLB", "market": "ML", "selection": "home", "line": None,
                      "odds_american": -111.0, "odds_type": "model",
                      "model_prob": 0.55, "features": features, "price_id": price_id,
                      "note": note, "decision_utc": decision}]})

    def test_form_snapshot_leakage_is_flagged(self):
        from parlaysports.quality import _form_snapshot_leakage
        self._parlay_with_leg("2026-09-21T12:00:00Z",
                              {"form_as_of": "2026-09-22T01:30:00Z"})
        out = _form_snapshot_leakage(self.con)
        self.assertEqual(len(out), 1)
        self.assertIn("FW-T-1", out[0])

    def test_form_snapshot_before_decision_is_clean(self):
        from parlaysports.quality import _form_snapshot_leakage
        self._parlay_with_leg("2026-09-22T12:00:00Z",
                              {"form_as_of": "2026-09-22T01:30:00Z"})
        self.assertEqual(_form_snapshot_leakage(self.con), [])

    def test_live_price_after_decision_is_flagged_and_close_exempt(self):
        from parlaysports.quality import _price_after_decision
        c = self.con
        c.execute("""INSERT INTO prices(price_id, game_key, market, selection, line,
                     odds_american, odds_type, source_id, source_url, observed_utc,
                     close_flag, note) VALUES
                     (1,'MLB:1','ML','home',NULL,-110.0,'market_reference','s','u',
                      '2026-09-21T20:00:00Z',0,'live'),
                     (2,'MLB:1','ML','home',NULL,-115.0,'market_close','s','u',
                      '2026-09-22T01:30:00Z',1,'close')""")
        c.commit()
        self._parlay_with_leg("2026-09-21T12:00:00Z", {}, price_id=1,
                              note="REFERENCE-grade leg")
        out = _price_after_decision(c)
        self.assertTrue(any("AFTER" in o for o in out), out)
        # historical closes are pre-game by definition: exempt from the stamp rule
        c.execute("DELETE FROM parlays"); c.execute("DELETE FROM legs"); c.commit()
        self._parlay_with_leg("2026-09-21T12:00:00Z", {}, price_id=2,
                              note="VERIFIED-grade leg")
        out = [o for o in _price_after_decision(c) if "AFTER" in o]
        self.assertEqual(out, [])

    def test_backtest_leg_may_not_use_live_snapshot(self):
        from parlaysports.quality import _price_after_decision
        c = self.con
        c.execute("""INSERT INTO prices(price_id, game_key, market, selection, line,
                     odds_american, odds_type, source_id, source_url, observed_utc,
                     close_flag, note) VALUES
                     (1,'MLB:1','ML','home',NULL,-110.0,'market_reference','s','u',
                      '2015-04-05T12:00:00Z',0,'live')""")
        c.commit()
        from parlaysports.parlay import insert_parlay
        insert_parlay(c, {
            "parlay_id": "BA-T-9", "strategy_id": "S-MLB-01", "version": "v1",
            "username": "t", "sport_scope": "MLB", "sports": ["MLB"], "n_legs": 1,
            "market_mix": "ML", "stake": 10.0, "pricing_grade": "REFERENCE",
            "combined_decimal": 1.9, "combined_american": -111.0,
            "potential_payout": 19.0, "model_prob": 0.55,
            "decision_utc": "2015-04-05T12:00:00Z", "slate_date": "2015-04-05",
            "test_mode": "backtest", "status": "upcoming",
            "legs": [{"strategy_id": "S-MLB-01", "version": "v1", "game_key": "MLB:1",
                      "sport": "MLB", "market": "ML", "selection": "home", "line": None,
                      "odds_american": -110.0, "odds_type": "market_reference",
                      "model_prob": 0.55, "features": {}, "price_id": 1,
                      "note": "", "decision_utc": "2015-04-05T12:00:00Z"}]})
        out = _price_after_decision(c)
        self.assertTrue(any("live snapshot" in o for o in out), out)

    def test_unpriced_leg_must_declare_its_grade(self):
        from parlaysports.quality import _price_after_decision
        # model fair value with no grade note -> flagged
        self._parlay_with_leg("2026-09-21T12:00:00Z", {}, price_id=None, note="")
        out = _price_after_decision(self.con)
        self.assertTrue(any("grade" in o for o in out), out)
        # an honestly declared MODEL leg and an UNPRICED leg are both clean
        self.con.execute("DELETE FROM parlays"); self.con.execute("DELETE FROM legs")
        self.con.commit()
        self._parlay_with_leg("2026-09-21T12:00:00Z", {}, price_id=None,
                              note="MODEL-grade leg")
        self.assertEqual(_price_after_decision(self.con), [])

    def test_postponed_game_voids_the_leg(self):
        from parlaysports.parlay import settle_leg, settle_parlay
        game = {"status": "postponed", "game_date": "2025-01-11", "away_team": "SAS",
                "home_team": "LAL", "away_score": None, "home_score": None}
        leg = {"market": "ML", "selection": "home", "line": None, "odds_american": -150.0}
        out = settle_leg(leg, game)
        self.assertEqual(out["result"], "void")
        self.assertIn("postponed", out["detail"])
        par = settle_parlay({"stake": 100.0, "combined_decimal": 1.67},
                            [dict(out, odds_american=-150.0)])
        self.assertEqual(par["status"], "push")       # all legs void -> refund
        self.assertEqual(par["payout"], 100.0)
        self.assertEqual(par["pnl"], 0.0)
        # a two-legger keeps the surviving leg at its RECORDED odds
        win = {"result": "win", "detail": "final", "odds_american": 100.0}
        par2 = settle_parlay({"stake": 100.0, "combined_decimal": 3.34},
                             [win, dict(out, odds_american=-150.0)])
        self.assertEqual(par2["status"], "won")
        self.assertAlmostEqual(par2["payout"], 200.0, places=2)

    def test_impossible_zero_zero_final_is_reported(self):
        from parlaysports.quality import _invalid_stats
        self._game("NBA:zz", sport="NBA", season="2024-25", gtype="REG",
                   date="2025-01-11", away="SAS", home="LAL", status="final", a=0, h=0)
        out = _invalid_stats(self.con)
        self.assertTrue(any("impossible scoreline" in o for o in out), out)

    def test_postponed_games_do_not_count_toward_season_length(self):
        from parlaysports.quality import _schedule_completeness
        teams = ["AAA", "BBB", "CCC", "DDD"]
        i = 0
        for a in range(4):            # 21 meetings per pair -> 42 games/team... 
            for b in range(a + 1, 4):
                for _ in range(21):
                    i += 1
                    self._game(f"NBA:p{i}", sport="NBA", season="2098-99", gtype="REG",
                               date=f"2098-11-{(i % 28) + 1:02d}", away=teams[a],
                               home=teams[b], status="final", a=100, h=99)
        # ... plus one postponed fixture per team: must NOT be counted as played
        for t in teams:
            i += 1
            self._game(f"NBA:p{i}", sport="NBA", season="2098-99", gtype="REG",
                       date="2098-12-01", away=t, home=teams[0], status="postponed")
        out = [o for o in _schedule_completeness(self.con) if "2098-99" in o]
        self.assertTrue(all("exceed" not in o for o in out), out)

    def test_workflow_files_are_lint_clean(self):
        """A malformed workflow file silently stops the scheduled job: the audit
        lints for "block scalar ended early" text at column 0 and tabs."""
        import re as _re
        wf_dir = ROOT / ".github" / "workflows"
        self.assertTrue(wf_dir.exists())
        for wf in sorted(wf_dir.glob("*.y*ml")):
            text = wf.read_text()
            self.assertIn("jobs:", text, wf.name)
            self.assertNotIn("\t", text, f"{wf.name} uses a tab for indentation")
            for n, line in enumerate(text.splitlines(), 1):
                if not line.strip() or line.lstrip().startswith("#") or line[0] == " ":
                    continue
                self.assertRegex(line, _re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:"),
                                 f"{wf.name}:{n} stray text at column 0")

    # ---- nightly collector: dedupe + UTC rollover matching -----------------
    def _nightly(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "nightly_mod", ROOT / "scripts" / "nightly.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_nightly_price_dedupe(self):
        nightly = self._nightly()
        c = self.con
        args = ("NFL:1", "ML", "home", None, -110.0, "market_reference",
                "SRC_ESPN_SCOREBOARD", "u", "2026-09-22T08:05:00Z", "n")
        self.assertTrue(nightly._price(c, *args))
        self.assertFalse(nightly._price(c, *args))            # identical stamp
        self.assertFalse(nightly._price(c, *args[:8], "2026-09-22T09:00:00Z", "n"))
        # a moved line at a LATER stamp is a new snapshot and is stored
        self.assertTrue(nightly._price(c, *args[:4], -120.0, *args[5:8],
                                       "2026-09-22T10:00:00Z", "n"))
        # two different values under one identical stamp is contradictory:
        # the first provenance stands and the conflict is logged, not silent
        self.assertFalse(nightly._price(c, *args[:4], -130.0, *args[5:]))
        self.assertTrue(c.execute("SELECT COUNT(*) AS n FROM issues "
                                 "WHERE detail LIKE '%conflicting-price-same-stamp%'"
                                 ).fetchone()["n"])
        n = c.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
        self.assertEqual(n, 2)

    def test_nightly_matches_espn_event_across_utc_rollover(self):
        nightly = self._nightly()
        c = self.con
        # nflverse files the Monday game under the local gameday
        self._game("NFL:2026_02_NYG_LA", date="2026-09-21")
        key = nightly._guarded_upsert_game(
            c, sport="NFL", espn_id="401872947", season="2026", game_type="REG",
            date="2026-09-22", start_utc="2026-09-22T00:15Z", week="Week 2",
            away="NYG", home="LA", venue="SoFi Stadium", status="final",
            away_score=6, home_score=28, url="u")
        self.assertEqual(key, "NFL:2026_02_NYG_LA")            # matched, not duplicated
        row = c.execute("SELECT game_date, status, away_score, home_score, start_utc, "
                        "verify_note FROM games WHERE game_key=?", (key,)).fetchone()
        self.assertEqual(row["game_date"], "2026-09-21")        # league gameday preserved
        self.assertEqual((row["status"], row["away_score"], row["home_score"]),
                         ("final", 6, 28))
        self.assertEqual(row["start_utc"], "2026-09-22T00:15Z")  # kickoff filled from ESPN
        self.assertIn("UTC date rollover", row["verify_note"])
        n = c.execute("SELECT COUNT(*) AS n FROM games WHERE sport='NFL'").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_nightly_conflicting_final_is_logged_not_silent(self):
        nightly = self._nightly()
        c = self.con
        self._game("NFL:2026_02_NYG_LA", date="2026-09-21", status="final", a=6, h=28,
                   source_id="SRC_NFLVERSE_GAMES")
        nightly._guarded_upsert_game(
            c, sport="NFL", espn_id="401872947", season="2026", game_type="REG",
            date="2026-09-21", start_utc=None, week="Week 2", away="NYG", home="LA",
            venue=None, status="final", away_score=7, home_score=28, url="u")
        det = [r["detail"] for r in c.execute("SELECT detail FROM issues")]
        self.assertTrue(any("conflicting-results" in d for d in det), det)

    # ---- documented per-slate cap ------------------------------------------
    def test_slate_cap_counts_existing_tickets(self):
        from parlaysports import engine
        from parlaysports.config import MAX_PARLAYS_PER_SLATE
        from parlaysports.parlay import insert_parlay
        c = self.con
        for i in range(MAX_PARLAYS_PER_SLATE):
            insert_parlay(c, {
                "parlay_id": f"BA-CAP-{i}", "strategy_id": "S-NFL-01", "version": "v1",
                "username": "t", "sport_scope": "NFL", "sports": ["NFL"], "n_legs": 2,
                "market_mix": "ML", "stake": 10.0, "pricing_grade": "REFERENCE",
                "combined_decimal": 3.6, "combined_american": 260.0,
                "potential_payout": 36.0, "model_prob": 0.3,
                "decision_utc": "2020-09-13T12:00:00Z", "slate_date": "2020-09-13",
                "test_mode": "backtest", "status": "upcoming", "legs": []})
        c.commit()
        self.assertEqual(engine.slate_cap(c, "S-NFL-01", "v1", "2020-09-13", "backtest"), 0)
        self.assertEqual(engine.slate_cap(c, "S-NFL-01", "v1", "2020-09-14", "backtest"),
                         MAX_PARLAYS_PER_SLATE)
        insert_parlay(c, {
            "parlay_id": "FW-LOTTO-1", "strategy_id": "S-MULTI-04", "version": "v1",
            "username": "t", "sport_scope": "MULTI", "sports": ["NFL", "MLB"],
            "n_legs": 6, "market_mix": "ML", "stake": 25.0, "pricing_grade": "MIXED",
            "combined_decimal": 40.0, "combined_american": 3900.0,
            "potential_payout": 1000.0, "model_prob": 0.02,
            "decision_utc": "2026-09-22T01:30:00Z", "slate_date": "2026-09-22",
            "test_mode": "forward", "status": "upcoming", "legs": []})
        c.commit()
        self.assertEqual(engine.slate_cap(c, "S-MULTI-04", "v1", "2026-09-22", "forward"), 0)


if __name__ == "__main__":
    unittest.main()
