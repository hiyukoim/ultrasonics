#!/usr/bin/env python3

"""
vault
Canonical library store — the persistent source of truth for tracks and
playlists across all platforms.

Schema
------
tracks(canonical_id, isrc, upc, title, artists_json, album, date, image, created, updated)
track_links(canonical_id, platform, platform_id, status)  -- linked | orphan
playlists(canonical_id, name, description, image, updated)
playlist_links(canonical_id, platform, platform_id)       -- playlist platform IDs
playlist_tracks(playlist_canonical_id, track_canonical_id, position)

Sync contract
-------------
Import: upsert_track(song) → canonical_id, then link(canonical_id, platform, platform_id, 'linked').
Export: for each vault track get linked id for target; if none, attempt resolution; if still none
        → link(canonical_id, target_platform, None, 'orphan') — track is never dropped.
Re-run after resolution: link status flips orphan→linked automatically.
"""

import json
import os
import sqlite3
import time
import uuid

from ultrasonics import logs
from ultrasonics.tools import fuzzymatch

log = logs.create_log(__name__)

_db_path = None

# ── DB bootstrap ─────────────────────────────────────────────────────────────

def _get_db():
    global _db_path
    if _db_path is None:
        from app import _ultrasonics
        db_dir = os.path.join(_ultrasonics["config_dir"], "vault")
        os.makedirs(db_dir, exist_ok=True)
        _db_path = os.path.join(db_dir, "vault.db")
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tracks (
            canonical_id TEXT PRIMARY KEY,
            isrc         TEXT,
            upc          TEXT,
            title        TEXT NOT NULL,
            artists_json TEXT NOT NULL DEFAULT '[]',
            album        TEXT,
            date         TEXT,
            image        TEXT,
            created      INTEGER NOT NULL,
            updated      INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS track_links (
            canonical_id TEXT NOT NULL,
            platform     TEXT NOT NULL,
            platform_id  TEXT,
            status       TEXT NOT NULL CHECK(status IN ('linked','orphan')),
            updated      INTEGER NOT NULL,
            PRIMARY KEY (canonical_id, platform),
            FOREIGN KEY (canonical_id) REFERENCES tracks(canonical_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS playlists (
            canonical_id TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            description  TEXT,
            image        TEXT,
            updated      INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS playlist_links (
            canonical_id TEXT NOT NULL,
            platform     TEXT NOT NULL,
            platform_id  TEXT NOT NULL,
            PRIMARY KEY (canonical_id, platform),
            FOREIGN KEY (canonical_id) REFERENCES playlists(canonical_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS playlist_tracks (
            playlist_canonical_id TEXT NOT NULL,
            track_canonical_id    TEXT NOT NULL,
            position              INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (playlist_canonical_id, track_canonical_id),
            FOREIGN KEY (playlist_canonical_id) REFERENCES playlists(canonical_id) ON DELETE CASCADE,
            FOREIGN KEY (track_canonical_id)    REFERENCES tracks(canonical_id)    ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tl_platform ON track_links(platform, platform_id);
        CREATE INDEX IF NOT EXISTS idx_tl_status   ON track_links(platform, status);
        CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks(isrc);
        CREATE INDEX IF NOT EXISTS idx_pl_links    ON playlist_links(platform, platform_id);
        CREATE INDEX IF NOT EXISTS idx_pt_playlist ON playlist_tracks(playlist_canonical_id, position);
    """)
    conn.commit()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _song_to_row(song):
    isrc = song.get("isrc") or None
    upc = song.get("upc") or None
    title = (song.get("title") or "").strip()
    artists = song.get("artists") or []
    artists_json = json.dumps(artists, ensure_ascii=False)
    album = song.get("album") or None
    date = song.get("date") or None
    image = song.get("image") or None
    return isrc, upc, title, artists_json, album, date, image


def _find_existing(conn, song):
    """
    Try to locate an existing tracks row for `song`.
    Resolution order: ISRC → any platform_id in song["id"] → fuzzy (title+artist ≥95).
    Returns canonical_id or None.
    """
    # 1. ISRC exact match
    isrc = song.get("isrc")
    if isrc:
        row = conn.execute(
            "SELECT canonical_id FROM tracks WHERE isrc = ?", (isrc,)
        ).fetchone()
        if row:
            return row["canonical_id"]

    # 2. Any platform_id already in track_links
    for platform, pid in (song.get("id") or {}).items():
        if not pid or platform == "vault":
            continue
        row = conn.execute(
            "SELECT canonical_id FROM track_links WHERE platform = ? AND platform_id = ?",
            (platform, str(pid)),
        ).fetchone()
        if row:
            return row["canonical_id"]

    # 3. Fuzzy title+artist (only when title present; high threshold to avoid false merges)
    title = (song.get("title") or "").strip()
    if not title:
        return None

    candidates = conn.execute(
        "SELECT canonical_id, title, artists_json, album, date, isrc FROM tracks "
        "WHERE title = ? OR title LIKE ? LIMIT 100",
        (title, title[:4] + "%"),
    ).fetchall()
    if not candidates:
        return None

    best_score, best_id = 0, None
    for c in candidates:
        candidate_song = {
            "title": c["title"],
            "artists": json.loads(c["artists_json"] or "[]"),
            "album": c["album"],
            "date": c["date"],
            "isrc": c["isrc"],
        }
        score = fuzzymatch.similarity(song, candidate_song)
        if score and score > best_score:
            best_score, best_id = score, c["canonical_id"]

    if best_score >= 95:
        return best_id
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def upsert_track(song):
    """
    Insert or update a track in the vault.
    Returns canonical_id (str).
    """
    conn = _get_db()
    now = int(time.time())
    isrc, upc, title, artists_json, album, date, image = _song_to_row(song)

    if not title:
        raise ValueError("Cannot upsert a track with no title.")

    existing_id = _find_existing(conn, song)

    if existing_id:
        conn.execute(
            """UPDATE tracks SET
                isrc         = COALESCE(isrc, ?),
                upc          = COALESCE(upc, ?),
                title        = ?,
                artists_json = CASE WHEN artists_json = '[]' THEN ? ELSE artists_json END,
                album        = COALESCE(album, ?),
                date         = COALESCE(date, ?),
                image        = COALESCE(image, ?),
                updated      = ?
               WHERE canonical_id = ?""",
            (isrc, upc, title, artists_json, album, date, image, now, existing_id),
        )
        conn.commit()
        return existing_id

    canonical_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO tracks
           (canonical_id, isrc, upc, title, artists_json, album, date, image, created, updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (canonical_id, isrc, upc, title, artists_json, album, date, image, now, now),
    )
    conn.commit()
    return canonical_id


def link(canonical_id, platform, platform_id, status="linked"):
    """
    Upsert a track_links row.
    status must be 'linked' or 'orphan'.
    platform_id may be None for orphan links.
    """
    conn = _get_db()
    now = int(time.time())
    conn.execute(
        """INSERT INTO track_links (canonical_id, platform, platform_id, status, updated)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(canonical_id, platform)
           DO UPDATE SET platform_id = COALESCE(excluded.platform_id, platform_id),
                         status      = excluded.status,
                         updated     = excluded.updated""",
        (canonical_id, platform, str(platform_id) if platform_id is not None else None, status, now),
    )
    conn.commit()


def mark_orphan(canonical_id, platform):
    """Mark (or create) a track_links entry as orphan for this platform."""
    link(canonical_id, platform, None, "orphan")


def get_orphans(platform):
    """
    Return all tracks that have an orphan link for `platform`.
    Each item: {canonical_id, title, artists, album, isrc}.
    """
    conn = _get_db()
    rows = conn.execute(
        """SELECT t.canonical_id, t.title, t.artists_json, t.album, t.isrc
           FROM track_links tl
           JOIN tracks t ON t.canonical_id = tl.canonical_id
           WHERE tl.platform = ? AND tl.status = 'orphan'""",
        (platform,),
    ).fetchall()
    return [
        {
            "canonical_id": r["canonical_id"],
            "title": r["title"],
            "artists": json.loads(r["artists_json"] or "[]"),
            "album": r["album"],
            "isrc": r["isrc"],
        }
        for r in rows
    ]


def get_link(canonical_id, platform):
    """Return the track_links row for (canonical_id, platform) or None."""
    conn = _get_db()
    row = conn.execute(
        "SELECT platform_id, status FROM track_links WHERE canonical_id = ? AND platform = ?",
        (canonical_id, platform),
    ).fetchone()
    return dict(row) if row else None


def upsert_playlist(name, platform=None, platform_id=None, description=None, image=None):
    """
    Insert or update a playlist in the vault.
    Matches on (platform, platform_id) if given, else on name.
    Returns canonical_id.
    """
    conn = _get_db()
    now = int(time.time())

    # Try to find existing by platform_id
    existing_id = None
    if platform and platform_id:
        row = conn.execute(
            "SELECT canonical_id FROM playlist_links WHERE platform = ? AND platform_id = ?",
            (platform, str(platform_id)),
        ).fetchone()
        if row:
            existing_id = row["canonical_id"]

    if not existing_id:
        row = conn.execute(
            "SELECT canonical_id FROM playlists WHERE name = ?", (name,)
        ).fetchone()
        if row:
            existing_id = row["canonical_id"]

    if existing_id:
        conn.execute(
            "UPDATE playlists SET name=?, description=COALESCE(?,description), "
            "image=COALESCE(?,image), updated=? WHERE canonical_id=?",
            (name, description, image, now, existing_id),
        )
        conn.commit()
        return existing_id

    canonical_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO playlists (canonical_id, name, description, image, updated) VALUES (?,?,?,?,?)",
        (canonical_id, name, description, image, now),
    )
    conn.commit()

    if platform and platform_id:
        conn.execute(
            "INSERT OR REPLACE INTO playlist_links (canonical_id, platform, platform_id) VALUES (?,?,?)",
            (canonical_id, platform, str(platform_id)),
        )
        conn.commit()

    return canonical_id


def set_membership(playlist_canonical_id, track_canonical_ids):
    """Replace the full membership of a playlist (by position order)."""
    conn = _get_db()
    conn.execute(
        "DELETE FROM playlist_tracks WHERE playlist_canonical_id = ?",
        (playlist_canonical_id,),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO playlist_tracks (playlist_canonical_id, track_canonical_id, position) "
        "VALUES (?, ?, ?)",
        [(playlist_canonical_id, tid, pos) for pos, tid in enumerate(track_canonical_ids)],
    )
    conn.commit()


def get_playlist_tracks(playlist_canonical_id):
    """
    Return ordered list of songs_dict items for a playlist.
    Includes all linked platform IDs in each song's id dict.
    """
    conn = _get_db()
    rows = conn.execute(
        """SELECT t.canonical_id, t.title, t.artists_json, t.album, t.date, t.isrc
           FROM playlist_tracks pt
           JOIN tracks t ON t.canonical_id = pt.track_canonical_id
           WHERE pt.playlist_canonical_id = ?
           ORDER BY pt.position""",
        (playlist_canonical_id,),
    ).fetchall()

    result = []
    for r in rows:
        song = {
            "title": r["title"],
            "artists": json.loads(r["artists_json"] or "[]"),
            "album": r["album"],
            "date": r["date"],
            "isrc": r["isrc"],
            "id": {"vault": r["canonical_id"]},
        }
        link_rows = conn.execute(
            "SELECT platform, platform_id FROM track_links "
            "WHERE canonical_id = ? AND status = 'linked'",
            (r["canonical_id"],),
        ).fetchall()
        for lr in link_rows:
            if lr["platform_id"]:
                song["id"][lr["platform"]] = lr["platform_id"]
        song = {k: v for k, v in song.items() if v is not None and v != [] and v != {}}
        result.append(song)
    return result


def import_songs_dict(songs_dict_item, source_platform):
    """
    Import a single playlist dict (with songs) into the vault.
    Returns (playlist_canonical_id, list[canonical_id]).

    For each song: upsert_track → link(source_platform, platform_id, 'linked').
    """
    name = songs_dict_item.get("name", "Untitled")
    platform_id = (songs_dict_item.get("id") or {}).get(source_platform)

    playlist_cid = upsert_playlist(
        name=name,
        platform=source_platform,
        platform_id=platform_id,
    )

    track_cids = []
    for song in songs_dict_item.get("songs", []):
        try:
            cid = upsert_track(song)
        except ValueError:
            continue
        track_cids.append(cid)
        src_pid = (song.get("id") or {}).get(source_platform)
        if src_pid:
            link(cid, source_platform, src_pid, "linked")
        # Cross-link via ISRC to matchings store for backward compat
        isrc = song.get("isrc")
        if isrc:
            for plat, pid in (song.get("id") or {}).items():
                if plat not in ("vault",) and pid:
                    try:
                        from ultrasonics.tools import matchings
                        matchings.save(
                            plat, str(pid), "vault", cid,
                            src_isrc=isrc,
                            src_title=song.get("title"),
                            src_artist="; ".join(song.get("artists") or []),
                        )
                    except Exception:
                        pass

    set_membership(playlist_cid, track_cids)
    return playlist_cid, track_cids


def export_for_platform(playlist_canonical_id, target_platform):
    """
    Build a songs_dict-style list for `target_platform`.

    - linked track → include platform_id.
    - orphan entry → include without platform_id (adapter will attempt match).
    - no entry     → create orphan entry, include without platform_id.
    """
    conn = _get_db()
    rows = conn.execute(
        """SELECT t.canonical_id, t.title, t.artists_json, t.album, t.date, t.isrc
           FROM playlist_tracks pt
           JOIN tracks t ON t.canonical_id = pt.track_canonical_id
           WHERE pt.playlist_canonical_id = ?
           ORDER BY pt.position""",
        (playlist_canonical_id,),
    ).fetchall()

    result = []
    for r in rows:
        cid = r["canonical_id"]
        song = {
            "title": r["title"],
            "artists": json.loads(r["artists_json"] or "[]"),
            "album": r["album"],
            "date": r["date"],
            "isrc": r["isrc"],
            "id": {"vault": cid},
        }

        tl = get_link(cid, target_platform)
        if tl:
            if tl["status"] == "linked" and tl["platform_id"]:
                song["id"][target_platform] = tl["platform_id"]
        else:
            mark_orphan(cid, target_platform)

        # Attach other linked IDs to aid downstream matching
        other_links = conn.execute(
            "SELECT platform, platform_id FROM track_links "
            "WHERE canonical_id = ? AND status = 'linked' AND platform != ?",
            (cid, target_platform),
        ).fetchall()
        for ol in other_links:
            if ol["platform_id"]:
                song["id"][ol["platform"]] = ol["platform_id"]

        song = {k: v for k, v in song.items() if v is not None and v != [] and v != {}}
        result.append(song)

    return result


def flip_orphan_to_linked(canonical_id, platform, platform_id):
    """
    Called by an output adapter when it successfully resolves a previously
    orphaned track. Flips status orphan→linked.
    """
    link(canonical_id, platform, platform_id, "linked")
    log.info(f"Vault: flipped orphan→linked {canonical_id} on {platform} ({platform_id})")


# ── Migration ─────────────────────────────────────────────────────────────────

def migrate_unmatched_stores():
    """
    One-shot migration: read all existing per-plugin unmatched.db files and
    insert their pending song rows into the vault as tracks with source links.
    Idempotent — safe to call multiple times.
    """
    import glob
    try:
        from app import _ultrasonics
        config_dir = _ultrasonics["config_dir"]
    except Exception:
        log.warning("vault.migrate_unmatched_stores: could not access config_dir, skipping.")
        return 0

    pattern = os.path.join(config_dir, "**", "unmatched.db")
    db_files = glob.glob(pattern, recursive=True)

    migrated = 0
    for db_file in db_files:
        try:
            with sqlite3.connect(db_file) as src:
                src.row_factory = sqlite3.Row
                rows = src.execute(
                    "SELECT applet_id, playlist_id, song_json FROM unmatched WHERE status = 'pending'"
                ).fetchall()
            for row in rows:
                try:
                    song = json.loads(row["song_json"])
                    cid = upsert_track(song)
                    for plat, pid in (song.get("id") or {}).items():
                        if plat != "vault" and pid:
                            link(cid, plat, str(pid), "linked")
                    migrated += 1
                except Exception as e:
                    log.debug(f"vault.migrate: skipped row from {db_file}: {e}")
        except Exception as e:
            log.warning(f"vault.migrate: could not read {db_file}: {e}")

    if migrated:
        log.info(f"vault.migrate_unmatched_stores: migrated {migrated} rows.")
    return migrated


def stats():
    """Return basic vault statistics."""
    conn = _get_db()
    return {
        "tracks":          conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0],
        "playlists":       conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0],
        "links_total":     conn.execute("SELECT COUNT(*) FROM track_links").fetchone()[0],
        "links_orphan":    conn.execute("SELECT COUNT(*) FROM track_links WHERE status='orphan'").fetchone()[0],
        "playlist_tracks": conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0],
    }
