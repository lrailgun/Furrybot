"""Persistent drafts, final characters and Discord installation IDs."""
from __future__ import annotations
import json
import sqlite3
import time
import uuid
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS characters(
                guild INTEGER NOT NULL, user INTEGER NOT NULL, data TEXT NOT NULL,
                avatar BLOB NOT NULL, updated INTEGER NOT NULL, PRIMARY KEY(guild,user));
            CREATE TABLE IF NOT EXISTS drafts(
                guild INTEGER NOT NULL, user INTEGER NOT NULL, revision TEXT NOT NULL,
                data TEXT NOT NULL, avatar BLOB NOT NULL, updated INTEGER NOT NULL,
                PRIMARY KEY(guild,user));
            CREATE TABLE IF NOT EXISTS installations(
                guild INTEGER PRIMARY KEY, channel INTEGER NOT NULL, panel INTEGER NOT NULL,
                admin_role INTEGER NOT NULL, member_role INTEGER NOT NULL);
        ''')
        # v1.2 characters are conservatively treated as already accepted.

    def get(self, guild, user):
        row = self.db.execute('SELECT data,avatar FROM characters WHERE guild=? AND user=?', (guild,user)).fetchone()
        return (json.loads(row['data']), row['avatar']) if row else None

    def draft(self, guild, user):
        row = self.db.execute('SELECT revision,data,avatar FROM drafts WHERE guild=? AND user=?', (guild,user)).fetchone()
        return (row['revision'], json.loads(row['data']), row['avatar']) if row else None

    def save_draft(self, guild, user, data, avatar):
        revision = uuid.uuid4().hex
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.get(guild,user):
                return None
            self.db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?,?,?,?)',
                (guild,user,revision,json.dumps(data,ensure_ascii=False),avatar,int(time.time())))
        return revision

    def accept(self, guild, user, revision):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.get(guild,user):
                return False
            result = self.db.execute('''INSERT INTO characters(guild,user,data,avatar,updated)
                SELECT guild,user,data,avatar,? FROM drafts WHERE guild=? AND user=? AND revision=?''',
                (int(time.time()),guild,user,revision))
            if result.rowcount != 1:
                return False
            self.db.execute('DELETE FROM drafts WHERE guild=? AND user=?',(guild,user))
            return True

    def admin_replace(self, guild, user, data, avatar):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            changed = self.db.execute('UPDATE characters SET data=?,avatar=?,updated=? WHERE guild=? AND user=?',
                (json.dumps(data,ensure_ascii=False),avatar,int(time.time()),guild,user)).rowcount
            if changed:
                self.db.execute('DELETE FROM drafts WHERE guild=? AND user=?',(guild,user))
            return bool(changed)

    def installation(self, guild):
        row = self.db.execute('SELECT * FROM installations WHERE guild=?',(guild,)).fetchone()
        return dict(row) if row else None

    def set_installation(self, guild, channel, panel, admin_role, member_role):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO installations VALUES(?,?,?,?,?)',
                (guild,channel,panel,admin_role,member_role))

    def recent(self, guild):
        rows = self.db.execute('''SELECT data FROM (SELECT data,updated FROM characters WHERE guild=?
            UNION ALL SELECT data,updated FROM drafts WHERE guild=?) ORDER BY updated DESC LIMIT 20''',(guild,guild))
        return [{k:d[k] for k in ('name','species','class')} for row in rows if (d:=json.loads(row['data']))]

    def roster(self, guild):
        return [(r['user'],json.loads(r['data'])) for r in self.db.execute(
            'SELECT user,data FROM characters WHERE guild=? ORDER BY updated DESC',(guild,))]

    def close(self):
        self.db.close()
