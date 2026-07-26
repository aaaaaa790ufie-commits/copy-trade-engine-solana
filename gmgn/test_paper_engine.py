import os
import sqlite3
import tempfile
import time
import unittest

import paper_engine as pe
from paper_engine import allowed, cleanup_wallets, enter, exits, init, weight

NOW = int(time.time())


def fresh_db():
    c = sqlite3.connect(":memory:")
    init(c)
    return c


class PaperEngineTests(unittest.TestCase):
    def setUp(self):
        self._token_price = pe.token_price

    def tearDown(self):
        pe.token_price = self._token_price

    def test_weights(self):
        self.assertEqual(weight(.50), .03125)
        self.assertEqual(weight(.60), .0625)
        self.assertEqual(weight(.70), .25)
        self.assertEqual(weight(.49), 0.0)

    def test_account_starts_at_point_one(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            c = sqlite3.connect(path)
            init(c)
            self.assertEqual(c.execute('SELECT budget_sol FROM paper_account WHERE id=1').fetchone()[0], .1)
        finally:
            c.close()
            os.unlink(path)

    def test_allowed_is_case_insensitive(self):
        self.assertTrue(allowed({"base_token": {"launchpad": "Pump.fun"}}, "sol"))
        self.assertTrue(allowed({"launchpad": "PUMP"}, "sol"))
        self.assertTrue(allowed({}, "robinhood"))
        self.assertFalse(allowed({"launchpad": "raydium"}, "sol"))

    def test_fully_invested_is_not_bankrupt(self):
        c = fresh_db()
        c.execute("UPDATE paper_account SET budget_sol=0.0")
        c.execute("INSERT INTO paper_positions VALUES('OPENMINT','sol',1.0,1.0,0.025,?,1.0,4,'open')", (NOW,))
        pe.token_price = lambda chain, mint: 1.0
        trades = [{"maker": "w1", "base_address": "MINTX", "timestamp": NOW, "side": "buy",
                   "price_usd": 1.0, "launchpad": "pump"}]
        enter(c, "sol", trades, {"w1": 1.0}, NOW)
        self.assertEqual(c.execute("SELECT bankrupt FROM paper_account WHERE id=1").fetchone()[0], 0)

    def test_bankrupt_only_when_zeroed_and_recovery_resets(self):
        c = fresh_db()
        c.execute("UPDATE paper_account SET budget_sol=0.0")
        pe.token_price = lambda chain, mint: 1.0
        trades = [{"maker": "w1", "base_address": "MINTX", "timestamp": NOW, "side": "buy",
                   "price_usd": 1.0, "launchpad": "pump"}]
        enter(c, "sol", trades, {"w1": 1.0}, NOW)
        self.assertEqual(c.execute("SELECT bankrupt FROM paper_account WHERE id=1").fetchone()[0], 1)
        # A winning exit brings the balance back above one stake -> flag resets, RECOVERY journaled.
        c.execute("INSERT INTO paper_positions VALUES('WINMINT','sol',1.0,3.0,0.025,?,1.0,4,'open')", (NOW,))
        pe.token_price = lambda chain, mint: 2.0  # below peak*(1-15%) -> trailing exit at +100%
        exits(c, "sol", [], NOW)
        budget, bankrupt = c.execute("SELECT budget_sol,bankrupt FROM paper_account WHERE id=1").fetchone()
        self.assertGreaterEqual(budget, pe.STAKE)
        self.assertEqual(bankrupt, 0)
        kinds = {r[0] for r in c.execute("SELECT kind FROM engine_events")}
        self.assertIn("RECOVERY", kinds)

    def test_max_hold_exit(self):
        c = fresh_db()
        opened = NOW - pe.MAX_HOLD - 10
        c.execute("INSERT INTO paper_positions VALUES('OLDMINT','sol',1.0,1.0,0.025,?,1.0,4,'open')", (opened,))
        pe.token_price = lambda chain, mint: 1.0  # flat price: no stop would ever fire
        exits(c, "sol", [], NOW)
        status = c.execute("SELECT status FROM paper_positions WHERE token_mint='OLDMINT'").fetchone()[0]
        reason = c.execute("SELECT reason FROM paper_trades WHERE action='EXIT'").fetchone()[0]
        self.assertEqual(status, "closed")
        self.assertIn("max hold", reason)

    def test_zombie_position_priced_independently(self):
        # A token absent from the Smart Money feed must still be marked via token_price
        # and hard-stopped. With feed-only pricing this position would hang open forever.
        c = fresh_db()
        c.execute("INSERT INTO paper_positions VALUES('RUGMINT','sol',1.0,1.0,0.025,?,1.0,4,'open')", (NOW,))
        pe.token_price = lambda chain, mint: 0.5  # -50% <= -45% hard stop
        exits(c, "sol", [], NOW)  # empty feed
        status = c.execute("SELECT status FROM paper_positions WHERE token_mint='RUGMINT'").fetchone()[0]
        self.assertEqual(status, "closed")

    def test_cleanup_blacklists_only_confirmed_low_winrate(self):
        c = fresh_db()
        stale = NOW - pe.ZERO_TTL - 5
        rows = [
            ("confirmed_low", "sol", "gmgn", 0, 0.30, NOW),
            ("stats_never_fetched", "sol", "gmgn", 0, 0.0, stale),
            ("fresh_zero", "sol", "gmgn", 0, 0.0, NOW),
            ("manual_zero", "sol", "manual_seed", 0, 0.0, stale),
        ]
        c.executemany(
            "INSERT INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) VALUES(?,?,?,?,?,?)",
            rows,
        )
        cleanup_wallets(c, "sol", NOW)
        watch = {r[0] for r in c.execute("SELECT address FROM wallet_watch")}
        black = {r[0] for r in c.execute("SELECT address FROM wallet_blacklist")}
        self.assertEqual(black, {"confirmed_low"})       # only confirmed low winrate is blacklisted
        self.assertNotIn("confirmed_low", watch)
        self.assertNotIn("stats_never_fetched", watch)   # stale zero-winrate row is dropped...
        self.assertNotIn("stats_never_fetched", black)   # ...but never blacklisted
        self.assertIn("fresh_zero", watch)               # still within ZERO_TTL
        self.assertIn("manual_zero", watch)              # manual seeds are never auto-dropped


class WalletEligibilityTests(unittest.TestCase):
    """"Not eligible yet" and "confirmed bad trader" must not share a mechanism.

    refresh_wallet_stats used to overwrite the win rate with a synthetic 0.49 for
    wallets with too small a sample or no recent buys, purely so cleanup_wallets
    would sweep them up. cleanup_wallets then blacklisted them permanently — and a
    blacklisted address is never re-added by discovery. A dormant wallet with an 80%
    win rate was therefore banned forever, contradicting cleanup_wallets' own
    docstring ("only ... a CONFIRMED sub-50% winrate").
    """

    def setUp(self):
        self._get_stats = pe.get_stats

    def tearDown(self):
        pe.get_stats = self._get_stats

    def _refresh(self, c, stats, at=NOW):
        pe.get_stats = lambda chain, wallets, max_batches=0: stats
        pe.refresh_wallet_stats(c, "sol", at)

    def _seed(self, c, address, winrate):
        c.execute("INSERT INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) "
                  "VALUES(?,?,?,?,?,?)", (address, "sol", "gmgn", 0, winrate, 0))

    def test_dormant_high_winrate_wallet_is_parked_not_banned(self):
        c = fresh_db()
        self._seed(c, "dormant", 0.80)
        self._refresh(c, {"dormant": {"winrate": 0.80, "buy": 0, "sell": 3}})
        cleanup_wallets(c, "sol", NOW)

        row = c.execute("SELECT winrate,active FROM wallet_watch WHERE address='dormant'").fetchone()
        self.assertIsNotNone(row, "a dormant wallet must stay on the watch list")
        self.assertAlmostEqual(row[0], 0.80, msg="its real win rate must not be overwritten")
        self.assertEqual(row[1], 0, "but it must be inactive, so it carries no weight")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM wallet_blacklist").fetchone()[0], 0)
        self.assertNotIn("dormant", pe.cached_weights(c, "sol"))

    def test_small_sample_wallet_is_parked_not_banned(self):
        c = fresh_db()
        self._seed(c, "newbie", 0.0)
        self._refresh(c, {"newbie": {"winrate": 1.0, "buy": 1, "sell": 0}})
        cleanup_wallets(c, "sol", NOW)

        row = c.execute("SELECT active FROM wallet_watch WHERE address='newbie'").fetchone()
        self.assertIsNotNone(row, "one perfect trade is not evidence of anything, but not a crime")
        self.assertEqual(row[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM wallet_blacklist").fetchone()[0], 0)

    def test_confirmed_low_winrate_is_still_banned(self):
        c = fresh_db()
        self._seed(c, "loser", 0.0)
        self._refresh(c, {"loser": {"winrate": 0.31, "buy": 20, "sell": 18}})
        cleanup_wallets(c, "sol", NOW)

        self.assertIsNone(c.execute("SELECT 1 FROM wallet_watch WHERE address='loser'").fetchone())
        self.assertTrue(pe.is_blacklisted(c, "loser", "sol"))

    def test_wallet_that_resumes_trading_is_reactivated(self):
        c = fresh_db()
        self._seed(c, "returner", 0.72)
        self._refresh(c, {"returner": {"winrate": 0.72, "buy": 0, "sell": 5}})
        self.assertEqual(
            c.execute("SELECT active FROM wallet_watch WHERE address='returner'").fetchone()[0], 0)

        # A parked wallet is only re-queried once its stats go stale, so advance past the TTL.
        later = NOW + pe.STATS_REFRESH_SEC + 1
        self._refresh(c, {"returner": {"winrate": 0.72, "buy": 9, "sell": 7}}, at=later)
        row = c.execute("SELECT active,winrate FROM wallet_watch WHERE address='returner'").fetchone()
        self.assertEqual(row[0], 1, "trading again must restore eligibility")
        self.assertIn("returner", pe.cached_weights(c, "sol"))


class ReEntryTests(unittest.TestCase):
    """paper_positions.token_mint is a PRIMARY KEY, so a token traded once leaves a
    closed row behind. Re-entering it after the cooldown used to raise IntegrityError,
    which escaped cycle() and killed the process — and since the signal was still
    inside the 30-minute cluster window, the supervisor restarted straight into the
    same crash, leaving open positions unchecked against their stops.
    """

    def setUp(self):
        self._token_price = pe.token_price
        pe.token_price = lambda chain, mint: 1.0

    def tearDown(self):
        pe.token_price = self._token_price

    def _signal(self, ts):
        return [{"maker": "w1", "base_address": "MINTX", "timestamp": ts, "side": "buy",
                 "price_usd": 1.0, "launchpad": "pump"}]

    def test_reentry_after_cooldown_reopens_the_position(self):
        c = fresh_db()
        pe.enter(c, "sol", self._signal(NOW), {"w1": 1.0}, NOW)
        c.execute("UPDATE paper_positions SET status='closed'")
        c.execute("DELETE FROM paper_cooldowns")

        pe.enter(c, "sol", self._signal(NOW + 1), {"w1": 1.0}, NOW + 1)

        rows = c.execute("SELECT status,opened_at FROM paper_positions WHERE token_mint='MINTX'").fetchall()
        self.assertEqual(len(rows), 1, "the row is reused, not duplicated")
        self.assertEqual(rows[0][0], "open")
        self.assertEqual(rows[0][1], NOW + 1, "opened_at reflects the new entry, not the old one")
        # Both entries must remain in the trade journal.
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM paper_trades WHERE action='ENTRY'").fetchone()[0], 2)

    def test_open_position_is_never_re_entered(self):
        c = fresh_db()
        pe.enter(c, "sol", self._signal(NOW), {"w1": 1.0}, NOW)
        before = c.execute("SELECT budget_sol FROM paper_account WHERE id=1").fetchone()[0]
        pe.enter(c, "sol", self._signal(NOW + 1), {"w1": 1.0}, NOW + 1)
        after = c.execute("SELECT budget_sol FROM paper_account WHERE id=1").fetchone()[0]
        self.assertEqual(before, after, "an already-open position must not be staked twice")


class CycleOrderingTests(unittest.TestCase):
    """The cycle must enforce stops before it does any wallet bookkeeping.

    Regression guard: exits() used to run after a get_stats() sweep that could take
    tens of minutes, so hard stops fired hours late and positions closed at -99%.
    """

    def setUp(self):
        self._saved = {k: getattr(pe, k) for k in ("cli", "token_price", "get_stats", "discover_wallets", "CHAINS")}
        pe._last_maint.clear()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(pe, k, v)
        pe._last_maint.clear()

    def test_exits_run_before_wallet_stats(self):
        c = fresh_db()
        c.execute("INSERT INTO paper_positions VALUES('RUGMINT','sol',1.0,1.0,0.025,?,1.0,4,'open')", (NOW,))
        order = []

        def fake_cli(args):
            if args[:2] == ["track", "smartmoney"]:
                return [{"maker": "w1", "base_address": "MINTX", "timestamp": NOW,
                         "side": "buy", "price_usd": 1.0, "launchpad": "pump"}]
            return {}

        def fake_price(chain, mint):
            order.append("exit-check")
            return 0.1  # -90%, well past the hard stop

        def fake_stats(chain, wallets, max_batches=0):
            order.append("stats")
            return {}

        pe.CHAINS = ["sol"]
        pe.cli, pe.token_price, pe.get_stats = fake_cli, fake_price, fake_stats
        pe.discover_wallets = lambda *a: None
        pe.cycle(c)

        self.assertEqual(order[0], "exit-check", f"stops must be checked first, got {order}")
        self.assertEqual(
            c.execute("SELECT status FROM paper_positions WHERE token_mint='RUGMINT'").fetchone()[0], "closed")

    def test_cached_weights_need_no_api_call(self):
        c = fresh_db()
        c.executemany(
            "INSERT INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) VALUES(?,?,?,?,?,?)",
            [("elite", "sol", "gmgn", NOW, 0.75, NOW),
             ("mid", "sol", "gmgn", NOW, 0.62, NOW),
             ("weak", "sol", "gmgn", NOW, 0.41, NOW)],
        )

        def boom(*a, **k):
            raise AssertionError("cached_weights must not hit the API")

        pe.get_stats = boom
        w = pe.cached_weights(c, "sol")
        self.assertEqual(w, {"elite": 0.25, "mid": 0.0625})  # sub-50% carries no weight

    def test_get_stats_batches_are_capped(self):
        calls = []
        pe.cli = lambda args: calls.append(args) or []
        pe.get_stats(pe.__dict__["cli"] and "sol", [f"w{i}" for i in range(100)], max_batches=3)
        self.assertEqual(len(calls), 3, "max_batches must bound the number of API round-trips")

    def test_heartbeat_is_written(self):
        c = fresh_db()
        pe.CHAINS = ["sol"]
        pe.cli = lambda args: []
        pe.cycle(c)
        row = c.execute("SELECT updated_at FROM engine_state WHERE key='last_cycle'").fetchone()
        self.assertIsNotNone(row, "every cycle must record a heartbeat")
        self.assertGreaterEqual(row[0], NOW)

    def test_maintenance_is_throttled(self):
        c = fresh_db()
        runs = []
        pe.CHAINS = ["sol"]
        pe.cli = lambda args: []
        pe.discover_wallets = lambda *a: runs.append(1)
        pe.cycle(c)
        pe.cycle(c)  # immediately again — must not repeat the slow pass
        self.assertEqual(len(runs), 0, "empty feed short-circuits before maintenance")

        pe.cli = lambda args: [{"maker": "w1", "base_address": "M", "timestamp": NOW,
                                "side": "buy", "price_usd": 1.0, "launchpad": "pump"}]
        pe.get_stats = lambda *a, **k: {}
        pe._last_maint.clear()
        pe.cycle(c)
        pe.cycle(c)
        self.assertEqual(len(runs), 1, "maintenance must run at most once per MAINT_INTERVAL")


class ResilienceTests(unittest.TestCase):
    """A transient cycle failure must not take the stop-loss loop down with it."""

    def setUp(self):
        self._saved = {k: getattr(pe, k) for k in ("cycle", "POLL", "MAX_CYCLE_FAILURES")}
        pe.POLL = 0

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(pe, k, v)

    def test_transient_failure_is_journalled_and_retried(self):
        c = fresh_db()
        calls = []

        def flaky(conn):
            calls.append(1)
            if len(calls) == 1:
                conn.execute("UPDATE paper_account SET budget_sol=0.0")  # partial write
                raise RuntimeError("boom")

        pe.cycle = flaky
        pe.MAX_CYCLE_FAILURES = 5
        # once=True still runs the retry path for the failing cycle, so drive two passes
        pe.run_forever(c, once=True)
        pe.run_forever(c, once=True)

        self.assertEqual(len(calls), 2, "the loop kept going after the failure")
        kinds = [r[0] for r in c.execute("SELECT kind FROM engine_events")]
        self.assertIn("ERROR", kinds, "the failure must be journalled, not swallowed")
        budget = c.execute("SELECT budget_sol FROM paper_account WHERE id=1").fetchone()[0]
        self.assertEqual(budget, 0.1, "the partial write must be rolled back")

    def test_persistent_failure_escalates(self):
        c = fresh_db()
        pe.MAX_CYCLE_FAILURES = 3

        def always_broken(conn):
            raise RuntimeError("still broken")

        pe.cycle = always_broken
        with self.assertRaises(RuntimeError):
            pe.run_forever(c)  # must give up so the supervisor restarts cleanly

    def test_keyboard_interrupt_is_not_caught(self):
        c = fresh_db()

        def interrupted(conn):
            raise KeyboardInterrupt

        pe.cycle = interrupted
        with self.assertRaises(KeyboardInterrupt):
            pe.run_forever(c)


class InitDataTests(unittest.TestCase):
    """Telegram WebApp initData must be verified before the API answers anything."""

    def _signed(self, token, user_id, auth_date=None):
        import hashlib
        import hmac
        import json as _json
        import time as _time
        import urllib.parse

        fields = {"user": _json.dumps({"id": user_id}), "auth_date": str(auth_date or int(_time.time()))}
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urllib.parse.urlencode(fields)

    def setUp(self):
        import webapp
        self.webapp = webapp
        self._token, self._chat = webapp.config.TELEGRAM_BOT_TOKEN, webapp.config.TELEGRAM_CHAT_ID
        webapp.config.TELEGRAM_BOT_TOKEN, webapp.config.TELEGRAM_CHAT_ID = "123:TESTTOKEN", "555"

    def tearDown(self):
        self.webapp.config.TELEGRAM_BOT_TOKEN, self.webapp.config.TELEGRAM_CHAT_ID = self._token, self._chat

    def test_valid_signature_accepted(self):
        data = self._signed("123:TESTTOKEN", 555)
        self.assertEqual(self.webapp.verify_init_data(data)["id"], 555)

    def test_tampered_payload_rejected(self):
        data = self._signed("123:TESTTOKEN", 555).replace("555", "666")
        self.assertIsNone(self.webapp.verify_init_data(data))

    def test_wrong_bot_token_rejected(self):
        self.assertIsNone(self.webapp.verify_init_data(self._signed("999:OTHER", 555)))

    def test_other_user_rejected(self):
        self.assertIsNone(self.webapp.verify_init_data(self._signed("123:TESTTOKEN", 777)))

    def test_stale_signature_rejected(self):
        old = int(time.time()) - self.webapp.INIT_DATA_MAX_AGE - 60
        self.assertIsNone(self.webapp.verify_init_data(self._signed("123:TESTTOKEN", 555, auth_date=old)))

    def test_empty_rejected(self):
        self.assertIsNone(self.webapp.verify_init_data(""))


if __name__ == '__main__':
    unittest.main()
