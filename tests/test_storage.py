import tempfile
import unittest
import sqlite3
from pathlib import Path
from storage import Store
from test_engine import EXAMPLE

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'db.sqlite3'); self.store=Store(self.path)
    def tearDown(self):
        self.store.close(); self.tmp.cleanup()
    def test_drafts_and_stale_accept(self):
        a=self.store.save_draft(1,2,EXAMPLE,b'a'); b=self.store.save_draft(1,2,EXAMPLE,b'b')
        self.assertNotEqual(a,b); self.assertFalse(self.store.accept(1,2,a)); self.assertTrue(self.store.accept(1,2,b))
        self.assertEqual(self.store.get(1,2),(EXAMPLE,b'b')); self.assertIsNone(self.store.draft(1,2))
        self.assertIsNone(self.store.save_draft(1,2,EXAMPLE,b'c')); self.assertFalse(self.store.accept(1,2,b))
    def test_admin_replace_only_final(self):
        a=self.store.save_draft(1,2,EXAMPLE,b'a'); self.assertFalse(self.store.admin_replace(1,2,EXAMPLE,b'b'))
        self.assertTrue(self.store.accept(1,2,a)); self.assertTrue(self.store.admin_replace(1,2,EXAMPLE,b'new'))
        self.assertEqual(self.store.get(1,2)[1],b'new')
    def test_isolation_and_restart(self):
        a=self.store.save_draft(1,2,EXAMPLE,b'a'); self.store.set_installation(1,4,5,6,7)
        self.store.close(); self.store=Store(self.path)
        self.assertEqual(self.store.draft(1,2)[0],a); self.assertIsNone(self.store.draft(3,2))
        self.assertEqual(self.store.installation(1)['admin_role'],6)
        self.assertFalse(self.store.accept(3,2,a)); self.assertEqual(self.store.roster(1),[])
        self.assertTrue(self.store.accept(1,2,a)); self.assertEqual(len(self.store.roster(1)),1)
    def test_old_db_records_are_locked(self):
        import json
        with self.store.db:
            self.store.db.execute('INSERT INTO characters VALUES(?,?,?,?,?)',(1,2,json.dumps(EXAMPLE),b'old',0))
        self.store.close(); self.store=Store(self.path)
        self.assertIsNone(self.store.save_draft(1,2,EXAMPLE,b'new')); self.assertEqual(self.store.get(1,2)[1],b'old')
    def test_unlimited_rerolls(self):
        for _ in range(100): self.assertIsNotNone(self.store.save_draft(1,2,EXAMPLE,b'a'))
        self.assertEqual(self.store.roster(1),[])
    def test_independent_installations_and_acceptance(self):
        self.store.set_installation(1,10,11,12,13)
        self.store.set_installation(2,20,21,22,23)
        a=self.store.save_draft(1,7,EXAMPLE,b'first')
        b=self.store.save_draft(2,7,EXAMPLE,b'second')
        self.assertFalse(self.store.accept(2,7,a))
        self.assertTrue(self.store.accept(1,7,a))
        self.assertIsNone(self.store.get(2,7))
        self.assertTrue(self.store.accept(2,7,b))
        self.assertEqual(self.store.get(1,7)[1],b'first')
        self.assertEqual(self.store.get(2,7)[1],b'second')
        self.assertEqual(self.store.installation(1)['channel'],10)
        self.assertEqual(self.store.installation(2)['channel'],20)

    def test_generation_history_keeps_discarded_drafts(self):
        for i in range(45):
            self.store.save_draft(1,2,dict(EXAMPLE,name=str(i)),b'a')
        self.assertEqual(len(self.store.recent(1)),20)
        self.assertEqual(self.store.recent(1)[0]['name'],'44')
        self.assertEqual(self.store.recent(1)[1]['name'],'43')
        self.assertEqual(self.store.recent(2),[])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM generation_history').fetchone()[0],40)
