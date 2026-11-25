"""SQLite persistence and transactional generation quotas. No local content generation."""
from __future__ import annotations
import sqlite3
import json
import time
from pathlib import Path
from datetime import datetime, timezone

class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS characters(
                guild INTEGER NOT NULL, user INTEGER NOT NULL, data TEXT NOT NULL,
                avatar BLOB NOT NULL, updated INTEGER NOT NULL, PRIMARY KEY(guild,user));
            CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS cooldowns(guild INTEGER NOT NULL,user INTEGER NOT NULL,
                last INTEGER NOT NULL, PRIMARY KEY(guild,user));
        ''')

    def get(self, guild, user):
        row = self.db.execute('SELECT data, avatar FROM characters WHERE guild=? AND user=?',(guild,user)).fetchone()
        return (json.loads(row['data']), row['avatar']) if row else None

    def save(self, guild, user, data, avatar):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO characters VALUES(?,?,?,?,?)',
                            (guild,user,json.dumps(data,ensure_ascii=False),avatar,int(time.time())))

    def delete(self, guild, user):
        with self.db:
            return bool(self.db.execute('DELETE FROM characters WHERE guild=? AND user=?',(guild,user)).rowcount)

    def reserve(self, guild, user, daily_limit, cooldown, now=None):
        now = int(time.time()) if now is None else now
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            last = self.db.execute('SELECT last FROM cooldowns WHERE guild=? AND user=?',(guild,user)).fetchone()
            if last and now - last['last'] < cooldown:
                return f'Следующая генерация доступна через <t:{last["last"]+cooldown}:R>.'
            count = self.db.execute('SELECT count FROM daily WHERE day=?',(day,)).fetchone()
            if count and count['count'] >= daily_limit:
                return 'Суточный лимит генераций исчерпан. Сброс в 00:00 UTC.'
            self.db.execute('INSERT INTO daily VALUES(?,1) ON CONFLICT(day) DO UPDATE SET count=count+1',(day,))
            self.db.execute('INSERT OR REPLACE INTO cooldowns VALUES(?,?,?)',(guild,user,now))
            # Retain only operationally necessary quota history.
            self.db.execute('DELETE FROM daily WHERE day < ?', (day,))
            self.db.execute('DELETE FROM cooldowns WHERE last < ?', (now-max(86400,cooldown),))
        return None

    def recent(self, guild):
        rows = self.db.execute('SELECT data FROM characters WHERE guild=? ORDER BY updated DESC LIMIT 20',(guild,))
        return [{k:d[k] for k in ('name','species','class')} for row in rows if (d:=json.loads(row['data']))]

    def roster(self, guild):
        rows = self.db.execute('SELECT user,data FROM characters WHERE guild=? ORDER BY updated DESC',(guild,))
        return [(row['user'],json.loads(row['data'])) for row in rows]

    def close(self):
        self.db.close()
