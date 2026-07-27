"""Offline tests for the pump.fun harvester.

No network: `api_get` and `gmgn_cli` are replaced with stubs, so the whole
pipeline (paging, dedupe, gating, verification, export) is exercised against a
real temporary SQLite file. Run from the repository root:

    python -m unittest discover -s gmgn -p 'test_pumpfun_discovery.py' -v
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pumpfun_discovery as pf  # noqa: E402

A = "FjkwfPK91zbRKNZVRBEhz99EdjF46ytW8E95djHdG4RX"
B = "HrtVLLCBM4LXfRGLGcXBYvkkJ9xnbiJ3ob1MxKdmNs7o"
C = "Aeg9rNNqNPw6qQ2RgzBHvz25AF1fPqiVBKupd6YQNZmT"
D = "HZyJNKiMYYgpvd7xK36tGkdG1xf9SkNQjNy8koSDcGyA"
MINT1 = "2dEpwaujMteZsFYCX3hwaavYKkU7kRwAwJe9XGuWNRGA"
MINT2 = "3PFEFjRtEJydqiXSWbiMnaqMPwtUMdNwSfbwVZxtMrr9"


def make_db():
    """Harvester tables plus the two engine tables it reads."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = pf.connect(path)
    conn.executescript(
        "CREATE TABLE wallet_watch(address TEXT NOT NULL, chain TEXT NOT NULL, source TEXT NOT NULL,"
        " active INTEGER NOT NULL DEFAULT 1, last_seen INTEGER NOT NULL DEFAULT 0,"
        " winrate REAL NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,"
        " PRIMARY KEY(address, chain));"
        "CREATE TABLE wallet_blacklist(address TEXT PRIMARY KEY, chain TEXT,"
        " blacklisted_at INTEGER, reason TEXT);"
    )
    conn.commit()
    return conn, path


class Parsing(unittest.TestCase):
    def test_address_validation(self):
        self.assertTrue(pf.valid_address(A))
        # The EVM CSV must never leak into a Solana wallet list.
        self.assertFalse(pf.valid_address("0x9e7fb44039de8890299dbba78ddb5e1b56297054"))
        self.assertFalse(pf.valid_address(""))
        self.assertFalse(pf.valid_address("short"))

    def test_lamports_vs_sol(self):
        self.assertAlmostEqual(pf.trade_sol({"sol_amount": 2_500_000_000}), 2.5)
        self.assertAlmostEqual(pf.trade_sol({"sol_amount": 0.42}), 0.42)
        self.assertEqual(pf.trade_sol({"sol_amount": "junk"}), 0.0)

    def test_timestamps(self):
        self.assertEqual(pf.trade_ts({"timestamp": 1784800000}), 1784800000)
        self.assertEqual(pf.trade_ts({"timestamp": 1784800000123}), 1784800000)
        self.assertEqual(pf.trade_ts({}), 0)

    def test_winrate_normalisation(self):
        self.assertAlmostEqual(pf.number({"winrate": 72}, "winrate"), 0.72)
        self.assertAlmostEqual(pf.number({"winrate": 0.72}, "winrate"), 0.72)
        self.assertAlmostEqual(pf.number({"pnl_stat": {"winrate": 61}}, "pnl_stat.winrate"), 0.61)
        self.assertEqual(pf.number({}, "winrate"), 0.0)

    def test_row_shapes(self):
        self.assertEqual(pf.as_rows({"data": [{"mint": A}]})[0]["mint"], A)
        self.assertEqual(pf.stat_rows({"data": {"list": [{"wallet": A}]}})[0]["wallet"], A)
        self.assertEqual(pf.as_rows(None), [])


class Harvest(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = make_db()
        self._api = pf.api_get
        pf.TRADES_PAGE = 2
        pf.TRADES_MAX_PAGES = 5
        pf.REQUEST_DELAY = 0

    def tearDown(self):
        pf.api_get = self._api
        self.conn.close()
        os.unlink(self.path)

    def test_same_wallet_twice_on_one_mint_counts_one_mint(self):
        pages = {
            0: [
                {"user": A, "sol_amount": 1_000_000_000, "timestamp": 100},
                {"user": A, "sol_amount": 2_000_000_000, "timestamp": 200},
            ],
            2: [],
        }
        pf.api_get = lambda path, params=None: pages.get(params["offset"], [])
        pf.scan_mint(self.conn, MINT1, 1000)
        trades, mints, volume = self.conn.execute(
            "SELECT trade_count, mint_count, sol_volume FROM pumpfun_candidates WHERE address=?", (A,)
        ).fetchone()
        self.assertEqual(trades, 2)
        self.assertEqual(mints, 1)  # not 2 — the guard against inflating mint_count per page
        self.assertAlmostEqual(volume, 3.0)

    def test_second_mint_increments_distinct_mint_count(self):
        pf.api_get = lambda path, params=None: (
            [{"user": A, "sol_amount": 1_000_000_000, "timestamp": 100}] if params["offset"] == 0 else []
        )
        pf.scan_mint(self.conn, MINT1, 1000)
        pf.scan_mint(self.conn, MINT2, 1001)
        mints = self.conn.execute("SELECT mint_count FROM pumpfun_candidates WHERE address=?", (A,)).fetchone()[0]
        self.assertEqual(mints, 2)

    def test_garbage_addresses_are_dropped(self):
        pf.api_get = lambda path, params=None: (
            [{"user": "0xdeadbeef", "sol_amount": 1e9}] if params["offset"] == 0 else []
        )
        pf.scan_mint(self.conn, MINT1, 1000)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pumpfun_candidates").fetchone()[0], 0)


class Verify(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = make_db()
        self._cli = pf.gmgn_cli
        pf.MIN_MINTS = 2
        pf.MIN_TRADES = 3
        pf.STATS_DELAY = 0
        now = int(time.time())
        for addr, trades, mints, volume in ((A, 9, 4, 12.0), (B, 8, 3, 9.0), (C, 7, 3, 5.0), (D, 1, 1, 0.1)):
            self.conn.execute(
                "INSERT INTO pumpfun_candidates(address, trade_count, mint_count, sol_volume,"
                " last_trade_ts, first_seen) VALUES(?,?,?,?,?,?)",
                (addr, trades, mints, volume, now, now),
            )
        self.conn.commit()

    def tearDown(self):
        pf.gmgn_cli = self._cli
        self.conn.close()
        os.unlink(self.path)

    def test_low_activity_wallet_is_never_offered(self):
        batch = pf.pending_batch(self.conn, 10, set(), set())
        self.assertIn(A, batch)
        self.assertNotIn(D, batch)

    def test_banned_and_watched_are_skipped(self):
        batch = pf.pending_batch(self.conn, 10, {B}, {C})
        self.assertEqual(batch, [A])
        status = self.conn.execute("SELECT status FROM pumpfun_candidates WHERE address=?", (B,)).fetchone()[0]
        self.assertEqual(status, "known")

    def test_verify_splits_good_bad_and_unknown(self):
        payload = {
            "data": {
                "list": [
                    {"wallet": A, "winrate": 81, "buy_count_30d": 20},
                    {"wallet": B, "winrate": 31, "buy_count_30d": 40},
                ]
            }
        }
        pf.gmgn_cli = lambda args: payload
        checked, accepted = pf.verify(self.conn, 10)
        self.assertEqual(checked, 3)
        self.assertEqual(accepted, 1)
        statuses = dict(self.conn.execute("SELECT address, status FROM pumpfun_candidates").fetchall())
        self.assertEqual(statuses[A], "ok")
        self.assertEqual(statuses[B], "rejected")
        # C had no row in the response: missing data must not be read as a bad wallet.
        self.assertEqual(statuses[C], "new")
        source, winrate = self.conn.execute(
            "SELECT source, winrate FROM wallet_watch WHERE address=?", (A,)
        ).fetchone()
        self.assertEqual(source, "pumpfun")
        self.assertAlmostEqual(winrate, 0.81)

    def test_high_winrate_but_no_sample_is_rejected(self):
        pf.gmgn_cli = lambda args: [{"wallet": A, "winrate": 100, "buy_count_30d": 1}]
        pf.verify(self.conn, 10)
        status = self.conn.execute("SELECT status FROM pumpfun_candidates WHERE address=?", (A,)).fetchone()[0]
        self.assertEqual(status, "rejected")

    def test_failing_batch_does_not_kill_the_run(self):
        def boom(args):
            raise RuntimeError("429 RATE_LIMIT_BANNED")

        pf.gmgn_cli = boom
        self.assertEqual(pf.verify(self.conn, 10), (0, 0))
        status = self.conn.execute("SELECT status FROM pumpfun_candidates WHERE address=?", (A,)).fetchone()[0]
        self.assertEqual(status, "new")

    def test_export_only_writes_verified(self):
        pf.gmgn_cli = lambda args: [{"wallet": A, "winrate": 77, "buy_count_30d": 30}]
        pf.verify(self.conn, 10)
        out = Path(tempfile.mkdtemp()) / "wallets-pumpfun.txt"
        count = pf.export(self.conn, out)
        body = out.read_text(encoding="utf-8")
        self.assertEqual(count, 1)
        self.assertIn(A, body)
        self.assertNotIn(D, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
