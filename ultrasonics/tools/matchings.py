#!/usr/bin/env python3

"""
matchings
Centralized store for learned source→destination track matchings.
When a user (or plugin) confirms a match between a source track and a
destination track, it is persisted here and auto-applied on subsequent runs.
"""

import json
import os
import sqlite3
import time

from ultrasonics import logs

log = logs.create_log(__name__)

_db_path = None


def _get_db_path():
    global _db_path
    if _db_path is None:
        from app import _ultrasonics
        db_dir = os.path.join(_ultrasonics["config_dir"], "matchings")
        os.makedirs(db_dir, exist_ok=True)
        _db_path = os.path.join(db_dir, "matchings.db")
    return _db_path


def _init_db():
    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS matchings ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  user_key TEXT,"
            "  src_platform TEXT NOT NULL,"
            "  src_id TEXT,"
            "  src_isrc TEXT,"
            "  src_title TEXT,"
            "  src_artist TEXT,"
            "  dst_platform TEXT NOT NULL,"
            "  dst_id TEXT NOT NULL,"
            "  created INTEGER NOT NULL,"
            "  UNIQUE(src_platform, src_id, dst_platform)"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_matchings_lookup "
            "ON matchings(src_platform, src_id, dst_platform)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_matchings_isrc "
            "ON matchings(src_isrc, dst_platform)"
        )
        conn.commit()
    return db_path


def save(src_platform, src_id, dst_platform, dst_id,
         user_key=None, src_isrc=None, src_title=None, src_artist=None):
    """Persist a confirmed source→destination matching."""
    db_path = _init_db()
    now = int(time.time())
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO matchings "
            "(user_key, src_platform, src_id, src_isrc, src_title, src_artist, dst_platform, dst_id, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(src_platform, src_id, dst_platform) "
            "DO UPDATE SET dst_id = ?, created = ?",
            (user_key, src_platform, src_id, src_isrc, src_title, src_artist,
             dst_platform, dst_id, now, dst_id, now),
        )
        conn.commit()
    log.info(f"Saved matching: {src_platform}:{src_id} → {dst_platform}:{dst_id}")


def lookup(src_platform, src_id, dst_platform):
    """Look up a previously saved dst_id for a given source track.
    Returns dst_id string or None."""
    db_path = _init_db()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT dst_id FROM matchings "
            "WHERE src_platform = ? AND src_id = ? AND dst_platform = ?",
            (src_platform, src_id, dst_platform),
        )
        row = cursor.fetchone()
    return row[0] if row else None


def lookup_by_isrc(src_isrc, dst_platform):
    """Fallback lookup using ISRC when source ID is unavailable.
    Returns dst_id string or None."""
    if not src_isrc:
        return None
    db_path = _init_db()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT dst_id FROM matchings "
            "WHERE src_isrc = ? AND dst_platform = ?",
            (src_isrc, dst_platform),
        )
        row = cursor.fetchone()
    return row[0] if row else None


def list_all(limit=200, offset=0):
    """Return all matchings for admin view."""
    db_path = _init_db()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM matchings ORDER BY created DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(row) for row in cursor.fetchall()]


def delete(matching_id):
    """Remove a matching by ID."""
    db_path = _init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM matchings WHERE id = ?", (matching_id,))
        conn.commit()


def count():
    """Return total number of stored matchings."""
    db_path = _init_db()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM matchings")
        return cursor.fetchone()[0]
