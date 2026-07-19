#!/usr/bin/env python3

"""
unmatched
Unified read/write access to the unmatched-song stores used by output plugins.
Provides the web view with a single API regardless of which plugin generated
the unmatched record.
"""

import json
import os
import sqlite3
import time

from ultrasonics import logs

log = logs.create_log(__name__)


def _get_store_paths():
    """Discover all unmatched.db files in config subdirectories."""
    from app import _ultrasonics
    config_dir = _ultrasonics["config_dir"]
    paths = []
    if not os.path.isdir(config_dir):
        return paths
    for dirpath, _, filenames in os.walk(config_dir):
        for f in filenames:
            if f == "unmatched.db":
                paths.append(os.path.join(dirpath, f))
    return paths


def get_all_pending(limit=200, offset=0):
    """Return all pending unmatched songs across all plugins.
    Each result includes the db_path and rowid for identification."""
    results = []
    for db_path in _get_store_paths():
        plugin_name = os.path.basename(os.path.dirname(db_path)).replace("up_", "")
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT rowid, applet_id, playlist_id, song_json, first_seen, last_tried "
                    "FROM unmatched WHERE status = 'pending' ORDER BY last_tried DESC"
                )
                for row in cursor.fetchall():
                    song = json.loads(row["song_json"])
                    results.append({
                        "db_path": db_path,
                        "rowid": row["rowid"],
                        "plugin": plugin_name,
                        "applet_id": row["applet_id"],
                        "playlist_id": row["playlist_id"],
                        "song": song,
                        "first_seen": row["first_seen"],
                        "last_tried": row["last_tried"],
                    })
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            log.warning(f"Could not read {db_path}: {e}")
    results.sort(key=lambda x: x.get("last_tried", 0), reverse=True)
    return results[offset:offset + limit]


def resolve(db_path, rowid):
    """Mark an unmatched entry as resolved (delete it)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM unmatched WHERE rowid = ?", (rowid,))
        conn.commit()


def dismiss(db_path, rowid):
    """Mark an unmatched entry as dismissed (won't retry)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE unmatched SET status = 'dismissed' WHERE rowid = ?", (rowid,)
        )
        conn.commit()


def count_pending():
    """Return total count of pending unmatched songs."""
    total = 0
    for db_path in _get_store_paths():
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM unmatched WHERE status = 'pending'"
                )
                total += cursor.fetchone()[0]
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass
    return total
