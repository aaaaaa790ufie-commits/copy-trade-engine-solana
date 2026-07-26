import os
import pathlib
import sqlite3
import tempfile
import time
import unittest

import config
import paper_engine as pe
from paper_engine import allowed, cleanup_wallets, enter, exits, init, weight

NOW = int(time.time())

# The engine validates addresses at the feed boundary, so fixtures must be real base58
# (32-44 chars). Short placeholders like "MINTX" would be dropped before reaching the
# code under test, and several assertions would then pass vacuously.
MINT_A = "Bb4jR951QtVjeFAYFLBYXDSMKjbTDroCLPbFLdd7pump"
MINT_B = "2FCerziZbakcdDbuQNgEWPxGRPYGmBzMxEJKkqJEpump"
MINT_C = "8TvnJa1NAWb1VYA2GRETSC83U57Co4hykEDvGwpPpump"
MINT_D = "DysiNUEUZXBdhnuCzkNd9p8zssWBQgXfKiYmVP5pumpQ"
WALLET_A = "6q2cfpsyeo9gA8wyybq8egKhNZsvGcfc5L2wC2K4mWtQ"
WALLET_B = "AUCuaE1ZfgKAReZedngX55iW1NaCjFcDQ1pRvP4caix8"
WALLET_C = "J9unx3pHCpPhJjatSTnZDg1vvbviWn2gAqU5uyGpumpX"


def mint_n(i):
    """Distinct valid mints for bulk fixtures."""
    return f"{i:04d}" + MINT_A[4:]


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
        c.execute("INSERT INTO paper_positions VALUES(?,'sol',1.0,1.0,0.025,?,1.0,4,'open')", (MINT_B, NOW))
        pe.token_price = lambda chain, mint: 1.0
        trades = [{"maker": WALLET_A, "base_address": MINT_A, "timestamp": NOW, "side": "buy",
                   "price_usd": 1.0, "launchpad": "pump"}]
        enter(c, "sol", trades, {WALLET_A: 1.0}, NOW)
        self.assertEqual(c.execute("SELECT bankrupt FROM paper_account WHERE id=1").fetchone()[0], 0)

    def test_bankrupt_only_when_zeroed_and_recovery_resets(self):
        c = fresh_db()
        c.execute("UPDATE paper_account SET budget_sol=0.0")
        pe.token_price = lambda chain, mint: 1.0
        trades = [{"maker": WALLET_A, "base_address": MINT_A, "timestamp": NOW, "side": "buy",
                   "price_usd": 1.0, "launchpad": "pump"}]
        enter(c, "sol", trades, {WALLET_A: 1.0}, NOW)
        self.assertEqual(c.execute("SELECT bankrupt FROM paper_account WHERE id=1").fetchone()[0], 1)
        # A winning exit brings the balance back above one stake -> flag resets, RECOVERY journaled.
        c.execute("INSERT INTO paper_positions VALUES(?,'sol',1.0,3.0,0.025,?,1.0,4,'open')", (MINT_C, NOW))
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
        c.execute("INSERT INTO paper_positions VALUES(?,'sol',1.0,1.0,0.025,?,1.0,4,'open')", (MINT_A, opened))
        pe.token_price = lambda chain, mint: 1.0  # flat price: no stop would ever fire
        exits(c, "sol", [], NOW)
        status = c.execute("SELECT status FROM paper_positions WHERE token_mint=?", (MINT_A,)).fetchone()[0]
        reason = c.execute("SELECT reason FROM paper_trades WHERE action='EXIT'").fetchone()[0]
        self.assertEqual(status, "closed")
        self.assertIn("max hold", reason)

    def test_zombie_position_priced_independently(self):
        # A token absent from the Smart Money feed must still be marked via token_price
        # and hard-stopped. With feed-only pricing this position would hang open forever.
        c = fresh_db()
        c.execute("INSERT INTO paper_positions VALUES(?,'sol',1.0,1.0,0.025,?,1.0,4,'open')", (MINT_A, NOW))
        pe.token_price = lambda chain, mint: 0.5  # -50% <= -45% hard stop
        exits(c, "sol", [], NOW)  # empty feed
        status = c.execute("SELECT status FROM paper_positions WHERE token_mint=?", (MINT_A,)).fetchone()[0]
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


class AddressValidationTests(unittest.TestCase):
    """Feed data is untrusted: it lands in SQLite and is rendered by the Mini App.

    A mint like `<img src=x onerror=...>` would otherwise be stored verbatim and
    interpolated into the panel's HTML.
    """

    GOOD_MINT = "Bb4jR951QtVjeFAYFLBYXDSMKjbTDroCLPbFLdd7pump"
    GOOD_WALLET = "6q2cfpsyeo9gA8wyybq8egKhNZsvGcfc5L2wC2K4mWtQ"

    def test_well_formed_addresses_pass(self):
        self.assertTrue(pe.valid_address(self.GOOD_MINT))
        self.assertTrue(pe.valid_address(self.GOOD_WALLET))
        self.assertEqual(pe.mint({"base_address": self.GOOD_MINT}), self.GOOD_MINT)
        self.assertEqual(pe.wallet({"maker": self.GOOD_WALLET}), self.GOOD_WALLET)

    def test_html_payload_is_rejected(self):
        payload = "<img src=x onerror=alert(1)>"
        self.assertFalse(pe.valid_address(payload))
        self.assertEqual(pe.mint({"base_address": payload}), "")
        self.assertEqual(pe.wallet({"maker": payload}), "")

    def test_sql_metacharacters_are_rejected(self):
        self.assertEqual(pe.mint({"base_address": "'; DROP TABLE paper_trades;--"}), "")

    def test_boundary_lengths(self):
        self.assertFalse(pe.valid_address("1" * 31), "too short")
        self.assertTrue(pe.valid_address("1" * 32))
        self.assertTrue(pe.valid_address("1" * 44))
        self.assertFalse(pe.valid_address("1" * 45), "too long")

    def test_base58_excludes_ambiguous_characters(self):
        for bad in ("0", "O", "I", "l"):
            self.assertFalse(pe.valid_address(bad + "1" * 39), f"{bad!r} is not base58")

    def test_empty_and_missing_fields(self):
        self.assertFalse(pe.valid_address(""))
        self.assertFalse(pe.valid_address(None))
        self.assertEqual(pe.mint({}), "")
        self.assertEqual(pe.wallet({"maker": None}), "")

    def test_second_field_is_used_when_the_first_is_malformed(self):
        self.assertEqual(
            pe.mint({"base_address": "junk", "token_address": self.GOOD_MINT}), self.GOOD_MINT)

    def test_malformed_trades_never_reach_the_database(self):
        c = fresh_db()
        trades = [{"maker": "<script>", "base_address": "<img onerror=1>", "side": "buy",
                   "timestamp": NOW, "price_usd": 1.0, "launchpad": "pump"}]
        pe.save_token_scores(c, "sol", trades, {"<script>": 0.25}, NOW)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM token_scores").fetchone()[0], 0)


class EliteCalloutTests(unittest.TestCase):
    """The feed replays its recent trades every poll, so call-outs must fire per trade."""

    def _seed_elite(self, c, address=WALLET_A, winrate=0.95):
        c.execute("INSERT INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) "
                  "VALUES(?,?,?,?,?,?)", (address, "sol", "gmgn", 0, winrate, 0))

    def _buy(self, maker, mint_, ts):
        return {"maker": maker, "base_address": mint_, "side": "buy", "timestamp": ts,
                "price_usd": 1.0, "launchpad": "pump"}

    def _count(self, c):
        return c.execute("SELECT COUNT(*) FROM engine_events WHERE kind='WALLET_BUY'").fetchone()[0]

    def test_known_elite_wallet_triggers_a_callout(self):
        # Regression: eligibility used to come from the freshly fetched stats of newly
        # seen makers, so a wallet already on the watch list never triggered anything.
        c = fresh_db()
        self._seed_elite(c)
        pe.elite_buy_callouts(c, "sol", [self._buy(WALLET_A, MINT_A, NOW)], NOW, since=NOW - 10)
        self.assertEqual(self._count(c), 1)

    def test_replayed_trade_is_not_announced_twice(self):
        c = fresh_db()
        self._seed_elite(c)
        trades = [self._buy(WALLET_A, MINT_A, NOW - 5)]
        pe.elite_buy_callouts(c, "sol", trades, NOW, since=NOW - 10)
        pe.elite_buy_callouts(c, "sol", trades, NOW + 15, since=NOW)  # same trade, next poll
        self.assertEqual(self._count(c), 1, "the same trade must announce once")

    def test_first_cycle_after_start_announces_nothing(self):
        c = fresh_db()
        self._seed_elite(c)
        trades = [self._buy(WALLET_A, mint_n(i), NOW - i) for i in range(50)]
        pe.elite_buy_callouts(c, "sol", trades, NOW, since=0)
        self.assertEqual(self._count(c), 0, "a fresh start must not replay the whole feed")

    def test_backlog_is_capped(self):
        c = fresh_db()
        self._seed_elite(c)
        trades = [self._buy(WALLET_A, mint_n(i), NOW - i) for i in range(100)]
        pe.elite_buy_callouts(c, "sol", trades, NOW, since=NOW - 100000)
        self.assertLessEqual(self._count(c), pe.ELITE_CALLOUTS_MAX)

    def test_sub_elite_wallet_is_ignored(self):
        c = fresh_db()
        self._seed_elite(c, WALLET_B, 0.75)
        pe.elite_buy_callouts(c, "sol", [self._buy(WALLET_B, MINT_A, NOW)], NOW, since=NOW - 10)
        self.assertEqual(self._count(c), 0)

    def test_sells_are_ignored(self):
        c = fresh_db()
        self._seed_elite(c)
        sell = self._buy(WALLET_A, MINT_A, NOW) | {"side": "sell"}
        pe.elite_buy_callouts(c, "sol", [sell], NOW, since=NOW - 10)
        self.assertEqual(self._count(c), 0)


class ExitRobustnessTests(unittest.TestCase):
    """exits() is the stop-loss path: one bad row must not abort the whole sweep."""

    def setUp(self):
        self._token_price = pe.token_price

    def tearDown(self):
        pe.token_price = self._token_price

    def test_zero_entry_price_does_not_abort_the_sweep(self):
        c = fresh_db()
        c.execute("INSERT INTO paper_positions VALUES(?,'sol',0.0,0.0,0.025,?,1.0,4,'open')", (MINT_B, NOW))
        c.execute("INSERT INTO paper_positions VALUES(?,'sol',1.0,1.0,0.025,?,1.0,4,'open')", (MINT_C, NOW))
        pe.token_price = lambda chain, mint: 0.4  # -60%, past the hard stop

        pe.exits(c, "sol", [], NOW)

        # The good position must still be closed even though a broken row precedes it.
        status = dict(c.execute("SELECT token_mint,status FROM paper_positions"))
        self.assertEqual(status[MINT_C], "closed",
                         "a division-by-zero row used to abort every later stop check")
        self.assertEqual(status[MINT_B], "open")

    def test_stuck_position_is_alerted_once_then_throttled(self):
        c = fresh_db()
        opened = NOW - pe.STUCK_AFTER - 1
        c.execute("INSERT INTO paper_positions VALUES(?,'sol',1.0,1.0,0.025,?,1.0,4,'open')", (MINT_A, opened))
        pe.token_price = lambda chain, mint: 0.0  # unpriceable, e.g. delisted

        pe.exits(c, "sol", [], NOW)
        pe.exits(c, "sol", [], NOW + 1)  # next poll, seconds later

        stuck = c.execute("SELECT COUNT(*) FROM engine_events WHERE kind='STUCK'").fetchone()[0]
        self.assertEqual(stuck, 1, "the poll loop must not flood Telegram with repeats")
        self.assertEqual(
            c.execute("SELECT status FROM paper_positions WHERE token_mint=?", (MINT_A,)).fetchone()[0],
            "open", "valuing a delisted token is the operator's call, not the engine's")

        pe.exits(c, "sol", [], NOW + pe.STUCK_REMIND + 1)
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM engine_events WHERE kind='STUCK'").fetchone()[0], 2,
            "but it must remind eventually")


class PriceCacheTests(unittest.TestCase):
    """The engine prices every token it sees, so the mark cache must stay bounded."""

    def setUp(self):
        self._cli, self._max = pe.cli, pe.PRICE_CACHE_MAX
        pe._price_cache.clear()

    def tearDown(self):
        pe.cli, pe.PRICE_CACHE_MAX = self._cli, self._max
        pe._price_cache.clear()

    def test_cache_stays_under_the_cap(self):
        pe.PRICE_CACHE_MAX = 50
        pe.cli = lambda args: [{"price": "1.5"}]
        for i in range(500):
            pe.token_price("sol", f"mint{i}")
        self.assertLessEqual(len(pe._price_cache), pe.PRICE_CACHE_MAX,
                             "an unbounded cache leaks for the lifetime of the process")

    def test_nested_price_dict_is_parsed(self):
        pe.cli = lambda args: [{"price": {"address": "x", "price": "0.0000013"}}]
        self.assertAlmostEqual(pe.token_price("sol", "nested"), 0.0000013)

    def test_unparsable_price_is_not_fatal(self):
        pe.cli = lambda args: [{"price": "not-a-number"}]
        self.assertEqual(pe.token_price("sol", "junk"), 0.0)

    def test_negative_price_is_treated_as_unavailable(self):
        # A negative mark would flip change=current/entry-1 and could fake a profit.
        pe.cli = lambda args: [{"price": "-5"}]
        self.assertEqual(pe.token_price("sol", "negative"), 0.0)

    def test_api_failure_returns_zero_rather_than_raising(self):
        def boom(args):
            raise RuntimeError("gmgn down")

        pe.cli = boom
        self.assertEqual(pe.token_price("sol", "down"), 0.0)


class ClusterTests(unittest.TestCase):
    """/weights must report the same score that enter() acts on."""

    def setUp(self):
        self._token_price = pe.token_price
        pe.token_price = lambda chain, mint: 1.0

    def tearDown(self):
        pe.token_price = self._token_price

    def _t(self, maker, mint_, side, ts):
        return {"maker": maker, "base_address": mint_, "side": side, "timestamp": ts,
                "price_usd": 1.0, "launchpad": "pump"}

    def test_latest_action_per_wallet_wins(self):
        # WALLET_A bought then sold: it is a seller and must not count toward the score.
        trades = [self._t(WALLET_A, MINT_A, "buy", NOW - 100), self._t(WALLET_A, MINT_A, "sell", NOW - 10)]
        got = pe.cluster("sol", trades, {WALLET_A: 0.25}, NOW)
        # cluster() returns a defaultdict, so assert the mint is really there — otherwise
        # an empty result would satisfy the score assertions for the wrong reason.
        self.assertIn(MINT_A, got)
        self.assertEqual(set(got[MINT_A]), {WALLET_A})
        buys, score = pe.score_of(got[MINT_A], {WALLET_A: 0.25})
        self.assertEqual(buys, {})
        self.assertEqual(score, 0.0)

    def test_trades_outside_the_window_are_ignored(self):
        inside = self._t(WALLET_A, MINT_A, "buy", NOW - 10)
        outside = self._t(WALLET_A, MINT_B, "buy", NOW - pe.WINDOW - 1)
        got = pe.cluster("sol", [inside, outside], {WALLET_A: 0.25}, NOW)
        self.assertIn(MINT_A, got, "the in-window trade proves the fixture is otherwise valid")
        self.assertNotIn(MINT_B, got)

    def test_unweighted_wallets_are_ignored(self):
        trades = [self._t(WALLET_B, MINT_A, "buy", NOW)]
        self.assertEqual(pe.cluster("sol", trades, {WALLET_A: 0.25}, NOW), {})
        # ...and the same trade from a weighted wallet does land, so the fixture is sound.
        self.assertIn(MINT_A, pe.cluster("sol", trades, {WALLET_B: 0.25}, NOW))

    def test_non_pump_launchpad_is_ignored(self):
        t = self._t(WALLET_A, MINT_A, "buy", NOW) | {"launchpad": "raydium"}
        self.assertEqual(pe.cluster("sol", [t], {WALLET_A: 0.25}, NOW), {})

    def test_published_score_matches_the_entry_decision(self):
        c = fresh_db()
        weights = {WALLET_A: 0.25, WALLET_B: 0.0625, WALLET_C: 0.03125}
        trades = [self._t(WALLET_A, MINT_A, "buy", NOW), self._t(WALLET_B, MINT_A, "buy", NOW),
                  self._t(WALLET_C, MINT_B, "buy", NOW),
                  self._t(WALLET_A, MINT_C, "buy", NOW - 50), self._t(WALLET_A, MINT_C, "sell", NOW)]
        pe.save_token_scores(c, "sol", trades, weights, NOW)
        published = dict(c.execute("SELECT token_mint,score FROM token_scores"))

        for m, ws in pe.cluster("sol", trades, weights, NOW).items():
            _, score = pe.score_of(ws, weights)
            if score > 0:
                self.assertAlmostEqual(published[m], score, msg=f"{m} disagrees with enter()")
        self.assertAlmostEqual(published[MINT_A], 0.3125)
        self.assertNotIn(MINT_C, published, "a wallet that sold out leaves no score behind")


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
        return [{"maker": WALLET_A, "base_address": MINT_A, "timestamp": ts, "side": "buy",
                 "price_usd": 1.0, "launchpad": "pump"}]

    def test_reentry_after_cooldown_reopens_the_position(self):
        c = fresh_db()
        pe.enter(c, "sol", self._signal(NOW), {WALLET_A: 1.0}, NOW)
        c.execute("UPDATE paper_positions SET status='closed'")
        c.execute("DELETE FROM paper_cooldowns")

        pe.enter(c, "sol", self._signal(NOW + 1), {WALLET_A: 1.0}, NOW + 1)

        rows = c.execute("SELECT status,opened_at FROM paper_positions WHERE token_mint=?", (MINT_A,)).fetchall()
        self.assertEqual(len(rows), 1, "the row is reused, not duplicated")
        self.assertEqual(rows[0][0], "open")
        self.assertEqual(rows[0][1], NOW + 1, "opened_at reflects the new entry, not the old one")
        # Both entries must remain in the trade journal.
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM paper_trades WHERE action='ENTRY'").fetchone()[0], 2)

    def test_open_position_is_never_re_entered(self):
        c = fresh_db()
        pe.enter(c, "sol", self._signal(NOW), {WALLET_A: 1.0}, NOW)
        before = c.execute("SELECT budget_sol FROM paper_account WHERE id=1").fetchone()[0]
        pe.enter(c, "sol", self._signal(NOW + 1), {WALLET_A: 1.0}, NOW + 1)
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
        c.execute("INSERT INTO paper_positions VALUES(?,'sol',1.0,1.0,0.025,?,1.0,4,'open')", (MINT_A, NOW))
        order = []

        def fake_cli(args):
            if args[:2] == ["track", "smartmoney"]:
                return [{"maker": WALLET_A, "base_address": MINT_A, "timestamp": NOW,
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
            c.execute("SELECT status FROM paper_positions WHERE token_mint=?", (MINT_A,)).fetchone()[0], "closed")

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


class WebappPositionTests(unittest.TestCase):
    """What the panel reports about an open position must not flatter it."""

    def setUp(self):
        import webapp
        self.webapp = webapp
        webapp._prices.clear()

    def tearDown(self):
        self.webapp._prices.clear()

    def _open(self, c, mint, entry, peak):
        c.execute("INSERT INTO paper_positions VALUES(?,'sol',?,?,0.025,?,1.0,4,'open')",
                  (mint, entry, peak, NOW))
        # _positions() reads columns by name; webapp.db() sets this in production.
        c.row_factory = sqlite3.Row

    def test_unpriced_position_is_valued_at_cost_not_at_peak(self):
        # Regression: the fallback was peak_price, the best price ever seen, so a token
        # that had run to 3x and then rugged displayed +200% for as long as it stayed
        # unquotable — the most flattering number for the position we know least about.
        c = fresh_db()
        self._open(c, MINT_A, entry=1.0, peak=3.0)
        positions = self.webapp._positions(c)

        self.assertEqual(len(positions), 1)
        p = positions[0]
        self.assertFalse(p["priced"])
        self.assertEqual(p["current_price"], 1.0, "valued at cost")
        self.assertEqual(p["change_pct"], 0.0)
        self.assertEqual(p["pnl_sol"], 0.0)

    def test_priced_position_reports_the_live_mark(self):
        c = fresh_db()
        self._open(c, MINT_A, entry=1.0, peak=1.0)
        self.webapp._prices[MINT_A] = 1.5
        p = self.webapp._positions(c)[0]

        self.assertTrue(p["priced"])
        self.assertAlmostEqual(p["change_pct"], 50.0)
        self.assertAlmostEqual(p["pnl_sol"], 0.025 * 0.5)

    def test_stop_price_reflects_whichever_stop_binds(self):
        c = fresh_db()
        self._open(c, MINT_A, entry=1.0, peak=2.0)   # +100% peak: trailing is armed
        self.webapp._prices[MINT_A] = 2.0
        p = self.webapp._positions(c)[0]

        self.assertTrue(p["trailing_armed"])
        self.assertAlmostEqual(p["stop_price"], 2.0 * (1 - config.TRAILING_DISTANCE_PCT / 100))

    def test_marks_are_bounded_to_open_positions(self):
        merged = self.webapp.merge_marks(["a", "b"], {"a": 1.0, "b": 2.0}, {})
        self.assertEqual(merged, {"a": 1.0, "b": 2.0})

        # "b" closed, "c" opened, "c" failed to price this pass.
        merged = self.webapp.merge_marks(["a", "c"], {"a": 1.1}, merged)
        self.assertEqual(set(merged), {"a", "c"}, "a closed position must not linger")
        self.assertEqual(merged["a"], 1.1)
        self.assertEqual(merged["c"], 0.0)

    def test_failed_refresh_keeps_the_previous_mark(self):
        merged = self.webapp.merge_marks(["a"], {"a": 5.0}, {})
        merged = self.webapp.merge_marks(["a"], {}, merged)  # API failed this pass
        self.assertEqual(merged["a"], 5.0, "one bad poll must not blank the position")


class ConfigParsingTests(unittest.TestCase):
    """.env is the single source of truth for credentials and tunables."""

    def _parse(self, text):
        fd, path = tempfile.mkstemp(suffix=".env")
        os.close(fd)
        try:
            pathlib.Path(path).write_text(text, encoding="utf-8")
            return config._parse_env_file(pathlib.Path(path))
        finally:
            os.unlink(path)

    def test_basic_forms(self):
        got = self._parse('A=1\nB="two"\nC=\'three\'\nexport D=4\n')
        self.assertEqual(got, {"A": "1", "B": "two", "C": "three", "D": "4"})

    def test_comments_and_blanks_are_skipped(self):
        self.assertEqual(self._parse("# note\n\n   \nA=1\n"), {"A": "1"})

    def test_trailing_comment_is_stripped_from_an_unquoted_value(self):
        # `GMGN_POLL_SECONDS=15  # fast` used to parse as the string "15  # fast",
        # which get_int() then silently discarded in favour of the default.
        got = self._parse("GMGN_POLL_SECONDS=15  # fast\n")
        self.assertEqual(got["GMGN_POLL_SECONDS"], "15")

    def test_hash_inside_a_quoted_value_is_preserved(self):
        got = self._parse('TOKEN="abc#def"\n')
        self.assertEqual(got["TOKEN"], "abc#def")

    def test_value_may_contain_equals(self):
        got = self._parse("URL=https://x/y?a=b\n")
        self.assertEqual(got["URL"], "https://x/y?a=b")

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(config._parse_env_file(pathlib.Path("no-such-file.env")), {})

    def test_typed_getters_fall_back_on_junk(self):
        self.assertEqual(config.get_int("SENTINEL_NOT_A_REAL_KEY", 7), 7)
        self.assertEqual(config.get_float("SENTINEL_NOT_A_REAL_KEY", 1.5), 1.5)
        self.assertFalse(config.get_bool("SENTINEL_NOT_A_REAL_KEY"))
        os.environ["SENTINEL_JUNK_INT"] = "banana"
        try:
            self.assertEqual(config.get_int("SENTINEL_JUNK_INT", 7), 7)
        finally:
            del os.environ["SENTINEL_JUNK_INT"]

    def test_process_environment_wins_over_the_file(self):
        os.environ["SENTINEL_OVERRIDE_CHECK"] = "from-env"
        try:
            config.FILE_ENV["SENTINEL_OVERRIDE_CHECK"] = "from-file"
            self.assertEqual(config.get("SENTINEL_OVERRIDE_CHECK"), "from-env")
        finally:
            del os.environ["SENTINEL_OVERRIDE_CHECK"]
            config.FILE_ENV.pop("SENTINEL_OVERRIDE_CHECK", None)

    def test_secrets_are_masked_never_echoed(self):
        self.assertNotIn("SUPERSECRETVALUE", config.mask("SUPERSECRETVALUE"))
        self.assertEqual(config.mask(""), "(unset)")
        self.assertTrue(config.is_secret("TELEGRAM_BOT_TOKEN"))
        self.assertTrue(config.is_secret("GMGN_PRIVATE_KEY"))
        self.assertFalse(config.is_secret("GMGN_POLL_SECONDS"))

    def test_summary_leaks_no_credential(self):
        out = config.summary()
        for key in ("TELEGRAM_BOT_TOKEN", "GMGN_API_KEY", "GMGN_PRIVATE_KEY"):
            value = config.get(key)
            if value:
                self.assertNotIn(value, out, f"{key} must never appear in full")


class BotPushTests(unittest.TestCase):
    """Events must not be dropped by a restart, nor replayed in full after an outage."""

    def setUp(self):
        import telegram_bot as bot
        self.bot = bot
        self.sent = []
        self._api, self._state = bot.api, bot.STATE_PATH
        bot.api = lambda method, data=None: self.sent.append(data) or {"ok": True}
        fd, self.state_file = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.state_file)  # start with no cursor on disk
        bot.STATE_PATH = pathlib.Path(self.state_file)
        bot._last_event = 0

    def tearDown(self):
        self.bot.api, self.bot.STATE_PATH = self._api, self._state
        self.bot._last_event = 0
        if os.path.exists(self.state_file):
            os.unlink(self.state_file)

    def _events(self, c, n, kind="EXIT"):
        for i in range(n):
            c.execute("INSERT INTO engine_events VALUES(NULL,?,?,?)", (NOW, kind, f"event {i}"))

    def test_first_run_does_not_replay_the_journal(self):
        c = fresh_db()
        self._events(c, 5)
        self.bot.catch_up(c)
        self.bot.push_events(c)
        self.assertEqual(self.sent, [], "a first start must not dump the whole history")

    def test_restart_delivers_events_missed_while_down(self):
        c = fresh_db()
        self.bot.catch_up(c)          # first start, cursor at 0 events
        self._events(c, 3)            # engine works while the bot is down
        self.bot._last_event = 0      # simulate the process restarting
        self.bot.catch_up(c)          # cursor is reloaded from disk, not reset to MAX(id)
        self.bot.push_events(c)
        self.assertEqual(len(self.sent), 3, "a restart used to silently drop these")

    def test_long_outage_is_summarised_not_replayed(self):
        c = fresh_db()
        self.bot.catch_up(c)
        self._events(c, self.bot.CATCHUP_LIMIT + 10, kind="ENTRY")
        self.bot._last_event = 0
        self.bot.catch_up(c)
        self.assertEqual(len(self.sent), 1, "a long outage sends one summary")
        self.assertIn("Пропущено", self.sent[0]["text"])
        self.sent.clear()
        self.bot.push_events(c)
        self.assertEqual(self.sent, [], "and the backlog is not then replayed as well")

    def test_failed_send_is_retried_not_skipped(self):
        c = fresh_db()
        self.bot.catch_up(c)
        self._events(c, 2)
        calls = []

        def flaky(method, data=None):
            calls.append(data)
            if len(calls) == 1:
                raise RuntimeError("telegram down")
            return {"ok": True}

        self.bot.api = flaky
        self.bot.push_events(c)                      # first send fails
        self.assertEqual(self.bot._last_event, 0, "the cursor must not advance past it")
        self.bot.push_events(c)                      # retry delivers both
        self.assertEqual(len(calls), 3)

    def test_non_pushable_kinds_advance_the_cursor(self):
        c = fresh_db()
        self.bot.catch_up(c)
        c.execute("INSERT INTO engine_events VALUES(NULL,?,?,?)", (NOW, "DEBUG", "internal"))
        self.bot.push_events(c)
        self.assertEqual(self.sent, [])
        self.assertGreater(self.bot._last_event, 0, "an unsent kind must not block the queue")


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
