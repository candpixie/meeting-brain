import json
import sqlite3
import time

import numpy as np

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
  id INTEGER PRIMARY KEY, title TEXT, created REAL, duration REAL,
  audio_path TEXT, audio_expires REAL, engine TEXT,
  summary TEXT, status TEXT DEFAULT 'new', error TEXT, auto_title INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS speakers (          -- one row per cluster in a meeting
  id INTEGER PRIMARY KEY, meeting_id INTEGER, label TEXT,
  person_id INTEGER, match_score REAL, embedding BLOB, seconds REAL
);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY, meeting_id INTEGER, start REAL, end REAL,
  speaker_id INTEGER, acoustic_speaker_id INTEGER, lang TEXT, overlap INTEGER DEFAULT 0,
  raw TEXT, clean TEXT, edited TEXT, alt TEXT, hint TEXT
);
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, embedding BLOB, n INTEGER DEFAULT 0, notes TEXT
);
CREATE TABLE IF NOT EXISTS vocab (
  id INTEGER PRIMARY KEY, term TEXT UNIQUE, misheard TEXT, source TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS corrections (      -- every edit you make, for fine-tuning + research
  id INTEGER PRIMARY KEY, segment_id INTEGER, kind TEXT, before TEXT, after TEXT,
  clip_path TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS seg_fts USING fts5(text, segment_id UNINDEXED, meeting_id UNINDEXED, tokenize='trigram');
"""


def conn():
    c = sqlite3.connect(config.DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(meetings)")}
        if "job_pid" not in cols:  # process doing the heavy work, so a restart can tell live jobs from dead ones
            c.execute("ALTER TABLE meetings ADD COLUMN job_pid INTEGER")
        if "lang" not in cols:
            c.execute("ALTER TABLE meetings ADD COLUMN lang TEXT")
        if "source" not in cols:  # where the audio came from, e.g. voicememos:<file>, so it's never processed twice
            c.execute("ALTER TABLE meetings ADD COLUMN source TEXT")


def setting(c, key, default=None):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(c, key, value):
    c.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, value))


def vec(blob):
    return None if blob is None else np.frombuffer(blob, dtype=np.float32)


def blob(v):
    return np.asarray(v, dtype=np.float32).tobytes()


def seg_text(row):
    return row["edited"] or row["clean"] or row["raw"] or ""


def reindex(c, meeting_id):
    c.execute("DELETE FROM seg_fts WHERE meeting_id=?", (meeting_id,))
    for r in c.execute("SELECT * FROM segments WHERE meeting_id=?", (meeting_id,)).fetchall():
        c.execute("INSERT INTO seg_fts(text, segment_id, meeting_id) VALUES (?,?,?)",
                  (seg_text(r), r["id"], meeting_id))


def vocab_terms(c):
    return [dict(r) for r in c.execute("SELECT term, misheard FROM vocab ORDER BY term")]


def add_vocab(c, term, misheard=None, source="manual"):
    term = term.strip()
    if not term:
        return
    c.execute("INSERT INTO vocab(term, misheard, source, created) VALUES (?,?,?,?) "
              "ON CONFLICT(term) DO UPDATE SET misheard=COALESCE(excluded.misheard, vocab.misheard)",
              (term, misheard, source, time.time()))


def dumps(x):
    return json.dumps(x, ensure_ascii=False)
