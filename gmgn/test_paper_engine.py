import os
import sqlite3
import tempfile
import unittest
from paper_engine import init, weight

class PaperEngineTests(unittest.TestCase):
    def test_weights(self):
        self.assertEqual(weight(.50), .03125)
        self.assertEqual(weight(.60), .0625)
        self.assertEqual(weight(.70), .25)
        self.assertEqual(weight(.49), 0.0)
    def test_account_starts_at_point_one(self):
        fd,path=tempfile.mkstemp(); os.close(fd)
        try:
            c=sqlite3.connect(path); init(c)
            self.assertEqual(c.execute('SELECT budget_sol FROM paper_account WHERE id=1').fetchone()[0], .1)
        finally:
            c.close(); os.unlink(path)

if __name__ == '__main__': unittest.main()
