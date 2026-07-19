#!/usr/bin/env python3

"""
history
Tracks applet run results over time (not just last-run).
Stored in a dedicated SQLite database in the config directory.
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
        db_dir = _ultrasonics["config_dir"]
        os.makedirs(db_dir, exist_ok=True)
        _db_path = os.path.join(db_dir, "history.db")
    return _db_path


def _init_db():
    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS run_history ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  applet_id TEXT NOT NULL,"
            "  started INTEGER NOT NULL,"
            "  finished INTEGER,"
            "  success INTEGER,"
            "  summary TEXT"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_applet "
            "ON run_history(applet_id, started DESC)"
        )
        conn.commit()
    return db_path


def record_start(applet_id):
    """Record that an applet run has started. Returns the row ID."""
    db_path = _init_db()
    now = int(time.time())
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO run_history (applet_id, started) VALUES (?, ?)",
            (applet_id, now),
        )
        conn.commit()
        return cursor.lastrowid


def record_finish(row_id, success, summary=None):
    """Record that an applet run has finished."""
    db_path = _init_db()
    now = int(time.time())
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE run_history SET finished = ?, success = ?, summary = ? WHERE id = ?",
            (now, 1 if success else 0, summary, row_id),
        )
        conn.commit()


def get_recent(limit=50, applet_id=None):
    """Return recent run history entries."""
    db_path = _init_db()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if applet_id:
            cursor = conn.execute(
                "SELECT * FROM run_history WHERE applet_id = ? ORDER BY started DESC LIMIT ?",
                (applet_id, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM run_history ORDER BY started DESC LIMIT ?",
                (limit,),
            )
        return [dict(row) for row in cursor.fetchall()]


def clear(applet_id=None):
    """Clear history for an applet, or all history."""
    db_path = _init_db()
    with sqlite3.connect(db_path) as conn:
        if applet_id:
            conn.execute("DELETE FROM run_history WHERE applet_id = ?", (applet_id,))
        else:
            conn.execute("DELETE FROM run_history")
        conn.commit()
