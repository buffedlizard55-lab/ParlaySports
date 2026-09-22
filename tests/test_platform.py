"""Unit + integration tests (stdlib unittest, no dependencies).

Covers: odds math, parlay math, leg/parlay settlement incl. push-reduction,
ledger append-only + hash chain, no-lookahead (ratings/signals), parlay
construction guards, pricing grades, settlement immutability.
"""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
