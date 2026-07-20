#!/usr/bin/env python3

"""
vault
Canonical library store — the persistent source of truth for tracks and
playlists across all platforms.

Schema
------
tracks(canonical_id, isrc, upc, title, artists_json, album, date, image, created, updated)
track_links(canonical_id, platform, platform_id, status, updated, run_id)  -- linked | orphan
playlists(canonical_id, name, description, image, is_main, tags_json, updated)
playlist_links(canonical_id, platform, platform_id)       -- playlist platform IDs
playlist_tracks(playlist_canonical_id, track_canonical_id, position)
playlist_groups(group_id, playlist_canonical_id, role)    -- role: main | copy

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
            run_id       TEXT,
            PRIMARY KEY (canonical_id, platform),
            FOREIGN KEY (canonical_id) REFERENCES tracks(canonical_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS playlists (
            canonical_id TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            description  TEXT,
            image        TEXT,
            is_main      INTEGER NOT NULL DEFAULT 0,
            tags_json    TEXT NOT NULL DEFAULT '[]',
            updated      INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS playlist_groups (
            group_id              TEXT NOT NULL,
            playlist_canonical_id TEXT NOT NULL,
            role                  TEXT NOT NULL DEFAULT 'copy',
            PRIMARY KEY (group_id, playlist_canonical_id),
            FOREIGN KEY (playlist_canonical_id) REFERENCES playlists(canonical_id) ON DELETE CASCADE
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

        CREATE INDEX IF NOT EXISTS idx_tl_platform  ON track_links(platform, platform_id);
        CREATE INDEX IF NOT EXISTS idx_tl_status    ON track_links(platform, status);
        CREATE INDEX IF NOT EXISTS idx_tl_run_id    ON track_links(run_id);
        CREATE INDEX IF NOT EXISTS idx_tracks_isrc  ON tracks(isrc);
        CREATE INDEX IF NOT EXISTS idx_pl_links     ON playlist_links(platform, platform_id);
        CREATE INDEX IF NOT EXISTS idx_pt_playlist  ON playlist_tracks(playlist_canonical_id, position);
        CREATE INDEX IF NOT EXISTS idx_pg_group     ON playlist_groups(group_id);
        CREATE INDEX IF NOT EXISTS idx_pg_playlist  ON playlist_groups(playlist_canonical_id);
    """)

    # Additive migrations for databases created before this schema version
    _add_column_if_missing(conn, "track_links", "run_id", "TEXT")
    _add_column_if_missing(conn, "playlists",   "is_main",   "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "playlists",   "tags_json", "TEXT NOT NULL DEFAULT '[]'")
    _create_table_if_missing(conn, "playlist_groups",
        """CREATE TABLE playlist_groups (
               group_id              TEXT NOT NULL,
               playlist_canonical_id TEXT NOT NULL,
               role                  TEXT NOT NULL DEFAULT 'copy',
               PRIMARY KEY (group_id, playlist_canonical_id),
               FOREIGN KEY (playlist_canonical_id)
                   REFERENCES playlists(canonical_id) ON DELETE CASCADE
           )""")
    conn.commit()
    conn.commit()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _add_column_if_missing(conn, table, column, definition):
    existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _create_table_if_missing(conn, table, create_sql):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        conn.execute(create_sql)


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


def link(canonical_id, platform, platform_id, status="linked", run_id=None):
    """
    Upsert a track_links row.
    status must be 'linked' or 'orphan'.
    platform_id may be None for orphan links.
    run_id (optional) — history run ID for traceability.
    """
    conn = _get_db()
    now = int(time.time())
    conn.execute(
        """INSERT INTO track_links (canonical_id, platform, platform_id, status, updated, run_id)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(canonical_id, platform)
           DO UPDATE SET platform_id = COALESCE(excluded.platform_id, platform_id),
                         status      = excluded.status,
                         updated     = excluded.updated,
                         run_id      = COALESCE(excluded.run_id, run_id)""",
        (canonical_id, platform, str(platform_id) if platform_id is not None else None, status, now, run_id),
    )
    conn.commit()


def mark_orphan(canonical_id, platform, run_id=None):
    """Mark (or create) a track_links entry as orphan for this platform."""
    link(canonical_id, platform, None, "orphan", run_id=run_id)


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


# ── Playlist metadata helpers ─────────────────────────────────────────────────

def set_tags(playlist_canonical_id, tags):
    """Replace the tags list on a playlist. tags must be a list of strings."""
    conn = _get_db()
    conn.execute(
        "UPDATE playlists SET tags_json = ?, updated = ? WHERE canonical_id = ?",
        (json.dumps([t.strip() for t in tags if t.strip()], ensure_ascii=False),
         int(time.time()), playlist_canonical_id),
    )
    conn.commit()


def set_main(playlist_canonical_id, is_main):
    """
    Toggle the is_main flag on a playlist.
    When setting is_main=True, clears the flag on all group-siblings first
    so exactly one playlist per group is the main.
    """
    conn = _get_db()
    now = int(time.time())
    if is_main:
        group_row = conn.execute(
            "SELECT group_id FROM playlist_groups WHERE playlist_canonical_id = ?",
            (playlist_canonical_id,),
        ).fetchone()
        if group_row:
            siblings = conn.execute(
                "SELECT playlist_canonical_id FROM playlist_groups WHERE group_id = ? AND playlist_canonical_id != ?",
                (group_row["group_id"], playlist_canonical_id),
            ).fetchall()
            for s in siblings:
                conn.execute(
                    "UPDATE playlists SET is_main = 0, updated = ? WHERE canonical_id = ?",
                    (now, s["playlist_canonical_id"]),
                )
            conn.execute(
                "UPDATE playlist_groups SET role = 'copy' WHERE group_id = ? AND playlist_canonical_id != ?",
                (group_row["group_id"], playlist_canonical_id),
            )
            conn.execute(
                "UPDATE playlist_groups SET role = 'main' WHERE group_id = ? AND playlist_canonical_id = ?",
                (group_row["group_id"], playlist_canonical_id),
            )
    conn.execute(
        "UPDATE playlists SET is_main = ?, updated = ? WHERE canonical_id = ?",
        (1 if is_main else 0, now, playlist_canonical_id),
    )
    conn.commit()


def group_playlists(playlist_ids, main_id=None):
    """
    Link multiple playlist canonical_ids into a group.
    If main_id is given, it is marked as the main; others are copies.
    Returns group_id.
    """
    conn = _get_db()
    # Check if any already belong to a group (reuse existing group_id)
    existing = conn.execute(
        "SELECT group_id FROM playlist_groups WHERE playlist_canonical_id IN ({})".format(
            ",".join("?" * len(playlist_ids))
        ),
        playlist_ids,
    ).fetchone()
    group_id = existing["group_id"] if existing else str(uuid.uuid4())

    now = int(time.time())
    for pid in playlist_ids:
        role = "main" if pid == main_id else "copy"
        conn.execute(
            "INSERT OR REPLACE INTO playlist_groups (group_id, playlist_canonical_id, role) VALUES (?,?,?)",
            (group_id, pid, role),
        )
        if main_id:
            conn.execute(
                "UPDATE playlists SET is_main = ?, updated = ? WHERE canonical_id = ?",
                (1 if pid == main_id else 0, now, pid),
            )
    conn.commit()
    return group_id


def get_group(playlist_canonical_id):
    """
    Return group info for a playlist, or None if not in any group.
    Returns {group_id, members: [{canonical_id, name, is_main, role, platforms}]}.
    """
    conn = _get_db()
    row = conn.execute(
        "SELECT group_id FROM playlist_groups WHERE playlist_canonical_id = ?",
        (playlist_canonical_id,),
    ).fetchone()
    if not row:
        return None
    group_id = row["group_id"]
    members = conn.execute(
        """SELECT p.canonical_id, p.name, p.is_main, pg.role
           FROM playlist_groups pg
           JOIN playlists p ON p.canonical_id = pg.playlist_canonical_id
           WHERE pg.group_id = ?""",
        (group_id,),
    ).fetchall()
    result = []
    for m in members:
        platforms = [
            lr["platform"]
            for lr in conn.execute(
                "SELECT platform FROM playlist_links WHERE canonical_id = ?", (m["canonical_id"],)
            ).fetchall()
        ]
        result.append({
            "canonical_id": m["canonical_id"],
            "name": m["name"],
            "is_main": bool(m["is_main"]),
            "role": m["role"],
            "platforms": platforms,
        })
    return {"group_id": group_id, "members": result}


def ungroup_playlist(playlist_canonical_id):
    """Remove a playlist from its group (if any)."""
    conn = _get_db()
    conn.execute(
        "DELETE FROM playlist_groups WHERE playlist_canonical_id = ?",
        (playlist_canonical_id,),
    )
    conn.commit()


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


def export_for_platform(playlist_canonical_id, target_platform, run_id=None):
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
            mark_orphan(cid, target_platform, run_id=run_id)

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


def list_playlists(tag=None, main_only=False):
    """Return all playlists ordered by: main first, then updated DESC.
    Optionally filter by tag or restrict to main-only."""
    conn = _get_db()
    rows = conn.execute(
        """SELECT p.canonical_id, p.name, p.description, p.image,
                  p.is_main, p.tags_json, p.updated,
                  COUNT(pt.track_canonical_id) AS track_count
           FROM playlists p
           LEFT JOIN playlist_tracks pt ON pt.playlist_canonical_id = p.canonical_id
           GROUP BY p.canonical_id
           ORDER BY p.is_main DESC, p.updated DESC"""
    ).fetchall()
    result = []
    for r in rows:
        tags = json.loads(r["tags_json"] or "[]")
        if tag and tag not in tags:
            continue
        if main_only and not r["is_main"]:
            continue
        pl = dict(r)
        pl["tags"] = tags
        pl["platforms"] = [
            lr["platform"]
            for lr in conn.execute(
                "SELECT platform FROM playlist_links WHERE canonical_id = ?",
                (r["canonical_id"],),
            ).fetchall()
        ]
        group_row = conn.execute(
            "SELECT group_id FROM playlist_groups WHERE playlist_canonical_id = ?",
            (r["canonical_id"],),
        ).fetchone()
        pl["group_id"] = group_row["group_id"] if group_row else None
        pl["group_size"] = (
            conn.execute(
                "SELECT COUNT(*) FROM playlist_groups WHERE group_id = ?",
                (group_row["group_id"],),
            ).fetchone()[0]
            if group_row else 1
        )
        result.append(pl)
    return result


def get_playlist(playlist_canonical_id):
    """Return a single playlist row with platform links and tags, or None."""
    conn = _get_db()
    row = conn.execute(
        """SELECT canonical_id, name, description, image,
                  is_main, tags_json, updated
           FROM playlists WHERE canonical_id = ?""",
        (playlist_canonical_id,),
    ).fetchone()
    if not row:
        return None
    pl = dict(row)
    pl["tags"] = json.loads(row["tags_json"] or "[]")
    pl["platforms"] = [
        dict(lr)
        for lr in conn.execute(
            "SELECT platform, platform_id FROM playlist_links WHERE canonical_id = ?",
            (playlist_canonical_id,),
        ).fetchall()
    ]
    return pl


def get_playlist_tracks_with_links(playlist_canonical_id):
    """
    Like get_playlist_tracks() but also returns per-track link status across all platforms.
    Each item: songs_dict fields + 'links': [{platform, platform_id, status}].
    """
    conn = _get_db()
    rows = conn.execute(
        """SELECT t.canonical_id, t.title, t.artists_json, t.album, t.date, t.isrc, t.image
           FROM playlist_tracks pt
           JOIN tracks t ON t.canonical_id = pt.track_canonical_id
           WHERE pt.playlist_canonical_id = ?
           ORDER BY pt.position""",
        (playlist_canonical_id,),
    ).fetchall()

    result = []
    for r in rows:
        links = [
            dict(lr)
            for lr in conn.execute(
                "SELECT platform, platform_id, status FROM track_links WHERE canonical_id = ?",
                (r["canonical_id"],),
            ).fetchall()
        ]
        result.append({
            "canonical_id": r["canonical_id"],
            "title": r["title"],
            "artists": json.loads(r["artists_json"] or "[]"),
            "album": r["album"],
            "date": r["date"],
            "isrc": r["isrc"],
            "image": r["image"],
            "links": links,
        })
    return result


def delete_playlist(playlist_canonical_id):
    """Remove a playlist and its membership/group rows (tracks kept)."""
    conn = _get_db()
    conn.execute("DELETE FROM playlist_groups WHERE playlist_canonical_id = ?", (playlist_canonical_id,))
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_canonical_id = ?", (playlist_canonical_id,))
    conn.execute("DELETE FROM playlist_links WHERE canonical_id = ?", (playlist_canonical_id,))
    conn.execute("DELETE FROM playlists WHERE canonical_id = ?", (playlist_canonical_id,))
    conn.commit()


def dump_json():
    """
    Export the entire vault as a JSON-serialisable dict.
    Structure: {tracks, track_links, playlists, playlist_links, playlist_tracks}
    """
    conn = _get_db()
    return {
        "tracks": [dict(r) for r in conn.execute("SELECT * FROM tracks").fetchall()],
        "track_links": [dict(r) for r in conn.execute("SELECT * FROM track_links").fetchall()],
        "playlists": [dict(r) for r in conn.execute("SELECT * FROM playlists").fetchall()],
        "playlist_links": [dict(r) for r in conn.execute("SELECT * FROM playlist_links").fetchall()],
        "playlist_groups":  [dict(r) for r in conn.execute("SELECT * FROM playlist_groups").fetchall()],
        "playlist_tracks":  [dict(r) for r in conn.execute("SELECT * FROM playlist_tracks").fetchall()],
    }


def restore_json(data):
    """
    Restore vault from a dump_json() dict. Merges into existing data (upserts).
    Safe to call on a fresh or existing vault.
    """
    conn = _get_db()
    now = int(time.time())
    for r in data.get("tracks", []):
        conn.execute(
            """INSERT INTO tracks (canonical_id,isrc,upc,title,artists_json,album,date,image,created,updated)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(canonical_id) DO UPDATE SET
                 isrc=COALESCE(excluded.isrc,isrc), upc=COALESCE(excluded.upc,upc),
                 title=excluded.title, artists_json=excluded.artists_json,
                 album=COALESCE(excluded.album,album), date=COALESCE(excluded.date,date),
                 image=COALESCE(excluded.image,image), updated=excluded.updated""",
            (r["canonical_id"], r.get("isrc"), r.get("upc"), r["title"],
             r.get("artists_json","[]"), r.get("album"), r.get("date"),
             r.get("image"), r.get("created", now), r.get("updated", now)),
        )
    for r in data.get("track_links", []):
        conn.execute(
            """INSERT INTO track_links (canonical_id,platform,platform_id,status,updated,run_id)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(canonical_id,platform) DO UPDATE SET
                 platform_id=COALESCE(excluded.platform_id,platform_id),
                 status=excluded.status, updated=excluded.updated,
                 run_id=COALESCE(excluded.run_id,run_id)""",
            (r["canonical_id"], r["platform"], r.get("platform_id"), r["status"],
             r.get("updated", now), r.get("run_id")),
        )
    for r in data.get("playlists", []):
        conn.execute(
            """INSERT INTO playlists (canonical_id,name,description,image,is_main,tags_json,updated)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(canonical_id) DO UPDATE SET
                 name=excluded.name, description=COALESCE(excluded.description,description),
                 image=COALESCE(excluded.image,image),
                 is_main=excluded.is_main, tags_json=excluded.tags_json,
                 updated=excluded.updated""",
            (r["canonical_id"], r["name"], r.get("description"), r.get("image"),
             r.get("is_main", 0), r.get("tags_json", "[]"), r.get("updated", now)),
        )
    for r in data.get("playlist_links", []):
        conn.execute(
            "INSERT OR REPLACE INTO playlist_links (canonical_id,platform,platform_id) VALUES (?,?,?)",
            (r["canonical_id"], r["platform"], r["platform_id"]),
        )
    for r in data.get("playlist_groups", []):
        conn.execute(
            "INSERT OR REPLACE INTO playlist_groups (group_id, playlist_canonical_id, role) VALUES (?,?,?)",
            (r["group_id"], r["playlist_canonical_id"], r.get("role", "copy")),
        )
    for r in data.get("playlist_tracks", []):
        conn.execute(
            "INSERT OR REPLACE INTO playlist_tracks (playlist_canonical_id,track_canonical_id,position) VALUES (?,?,?)",
            (r["playlist_canonical_id"], r["track_canonical_id"], r.get("position", 0)),
        )
    conn.commit()


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
