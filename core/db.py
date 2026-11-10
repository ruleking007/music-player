"""SQLite 存储层：歌曲、歌单、歌词、分析任务。WAL + 短事务，UI 读写都不阻塞。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  name TEXT, title TEXT, artist TEXT, album TEXT,
  ext TEXT, size INTEGER, mtime REAL, duration REAL, bitrate INTEGER, samplerate INTEGER,
  has_meta INTEGER DEFAULT 0,
  tempo REAL, energy REAL, danceability REAL, acoustic REAL, valence REAL,
  brightness REAL, roughness REAL, flatness REAL, vocal REAL, tonality REAL, dynamic REAL,
  mod REAL, v_raw REAL, a_raw REAL,
  mood TEXT, mood_label TEXT, mood_reason TEXT,
  folder TEXT,
  feat_ver INTEGER DEFAULT 0,
  analyzed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
  added REAL, lyric_id INTEGER
);
CREATE INDEX IF NOT EXISTS songs_mood ON songs(mood);
CREATE INDEX IF NOT EXISTS songs_title ON songs(title);
CREATE INDEX IF NOT EXISTS songs_artist ON songs(artist);

CREATE TABLE IF NOT EXISTS playlists(
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  emoji TEXT DEFAULT '',
  description TEXT,
  auto INTEGER DEFAULT 1,
  sort INTEGER DEFAULT 0,
  created REAL
);

CREATE TABLE IF NOT EXISTS playlist_items(
  playlist_id INTEGER, song_id INTEGER, pos INTEGER,
  PRIMARY KEY(playlist_id, song_id)
);

CREATE TABLE IF NOT EXISTS lyrics(
  id INTEGER PRIMARY KEY,
  song_path TEXT UNIQUE NOT NULL,
  title TEXT, artist TEXT, album TEXT,
  source TEXT, lang TEXT,
  lines TEXT,          -- JSON [{t:秒, text:''}]
  plain TEXT,          -- 全文（搜索用）
  updated REAL,
  song_id INTEGER
);
CREATE INDEX IF NOT EXISTS lyrics_songid ON lyrics(song_id);

CREATE TABLE IF NOT EXISTS scan_state(k TEXT PRIMARY KEY, v TEXT);
"""

class DB:
    """线程局部连接；每个线程一条 sqlite3 连接。"""

    def __init__(self, path: Path = config.DB_PATH):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self.tx() as c:
            c.executescript(SCHEMA)
            have = {r["name"] for r in c.execute("PRAGMA table_info(songs)")}
            for col, typ in (("tonality", "REAL"), ("dynamic", "REAL"), ("mod", "REAL"),
                             ("v_raw", "REAL"), ("a_raw", "REAL"),
                             ("folder", "TEXT"), ("feat_ver", "INTEGER DEFAULT 0"),
                             ("lyric_id", "INTEGER"),
                             # 音质评判 / 去重字段（quality.py）
                             ("q_score", "REAL"), ("q_label", "TEXT"), ("q_reason", "TEXT"),
                             ("q_kbps", "INTEGER"), ("q_sr", "INTEGER"),
                             ("q_cutoff", "INTEGER"), ("q_hf", "REAL"),
                             ("q_brick", "INTEGER DEFAULT 0"),
                             ("q_fake", "INTEGER DEFAULT 0"),
                             ("q_ver", "INTEGER DEFAULT 0"),
                             ("dup_key", "TEXT"), ("dup_best", "INTEGER DEFAULT 0"),
                             ("exact_key", "TEXT"), ("q_advice", "TEXT")):
                if col not in have:
                    c.execute(f"ALTER TABLE songs ADD COLUMN {col} {typ}")
            # 索引必须在列迁移之后建，否则旧库 executescript 阶段会报错
            c.execute("CREATE INDEX IF NOT EXISTS songs_dupkey ON songs(dup_key)")
            c.execute("CREATE INDEX IF NOT EXISTS songs_qscore ON songs(q_score)")

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA temp_store=MEMORY")
            c.execute("PRAGMA cache_size=-16000")
            self._local.conn = c
        return c

    class _Tx:
        def __init__(self, db: "DB", write: bool):
            self.db, self.write = db, write

        def __enter__(self) -> sqlite3.Connection:
            if self.write:
                self.db._write_lock.acquire()
            return self.db.conn

        def __exit__(self, exc, typ, tb):
            c = self.db.conn
            if exc is None:
                c.commit()
            else:
                c.rollback()
            if self.write:
                self.db._write_lock.release()
            return False

    def tx(self, write: bool = True) -> "DB._Tx":
        return self._Tx(self, write)

    def q(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()):
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    # ---------- scan state ----------
    def get_state(self, k: str, default=None):
        r = self.one("SELECT v FROM scan_state WHERE k=?", (k,))
        if not r:
            return default
        try:
            return json.loads(r["v"])
        except Exception:
            return r["v"]

    def set_state(self, k: str, v) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO scan_state(k,v) VALUES(?,?) "
                      "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v, ensure_ascii=False)))
