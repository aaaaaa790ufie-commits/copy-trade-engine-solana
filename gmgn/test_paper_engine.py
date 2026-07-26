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
