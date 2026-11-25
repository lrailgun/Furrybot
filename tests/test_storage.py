import tempfile
import unittest
from pathlib import Path
from storage import Store
from test_engine import EXAMPLE

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.tmp.name)/'db.sqlite3')
        self.store=Store(self.path)
    def tearDown(self):
        self.store.close(); self.tmp.cleanup()
    def test_persistence_and_delete(self):
        self.store.save(1,2,EXAMPLE,b'avatar')
        self.store.close(); self.store=Store(self.path)
        self.assertEqual(self.store.get(1,2),(EXAMPLE,b'avatar'))
        self.assertIsNone(self.store.get(2,2))
        self.assertEqual(len(self.store.roster(1)),1)
        self.assertEqual(self.store.recent(1)[0]['name'],EXAMPLE['name'])
        self.assertTrue(self.store.delete(1,2)); self.assertIsNone(self.store.get(1,2))
    def test_quota(self):
        self.assertIsNone(self.store.reserve(1,1,2,60,now=1000))
        self.assertIn('Следующая', self.store.reserve(1,1,2,60,now=1010))
        self.assertIsNone(self.store.reserve(1,2,2,60,now=1020))
        self.assertIn('лимит', self.store.reserve(1,3,2,60,now=1030))
        self.assertIsNone(self.store.reserve(1,3,2,60,now=90000))
