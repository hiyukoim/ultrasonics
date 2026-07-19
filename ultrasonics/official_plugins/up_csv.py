#!/usr/bin/env python3

"""
up_csv

Structured CSV import and export plugin for ultrasonics.
Provides a lossless, round-trip-safe interchange format — analogous to
Soundiiz's CSV export. Also reads exportify (watsonbox/exportify) CSVs.

Export: one file per playlist, canonical header + one row per song.
Import: header-driven alias mapping; tolerates our own export, exportify, and
        generic/Soundiiz-style CSVs. Unknown columns are ignored.
"""

import csv
import io
import os
import re

from ultrasonics import logs

log = logs.create_log(__name__)

# Canonical column order for export
EXPORT_COLUMNS = ["title", "artists", "album", "date", "isrc", "location", "ids"]

# Header alias map: canonical field -> list of accepted header names (lowercase)
HEADER_ALIASES = {
    "title":    ["title", "track name", "name", "track"],
    "artists":  ["artists", "artist name(s)", "artist"],
    "album":    ["album", "album name"],
    "date":     ["date", "album release date", "release date"],
    "isrc":     ["isrc"],
    "location": ["location", "path", "file"],
    # Spotify IDs handled separately via ids column or track uri
    "ids":      ["ids"],
    "track_uri":["track uri", "spotify uri"],
    "spotify_id":["spotify_id", "spotify id"],
    "playlist": ["playlist"],
}

handshake = {
    "name": "csv",
    "description": "import and export playlists as structured csv files (soundiiz-style interchange)",
    "type": ["inputs", "outputs"],
    "mode": ["playlists"],
    "version": "0.1",
    "settings": [
        {
            "type": "text",
            "label": "CSV Directory",
            "name": "csv_dir",
            "value": "/mnt/playlists/csv",
        },
        {
            "type": "string",
            "value": "All CSV files are read from / written to this directory.",
        },
    ],
}


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _artists_to_str(artists):
    """Encode artist list as semicolon-joined string for export."""
    if not artists:
        return ""
    return "; ".join(str(a) for a in artists)


def _artists_from_str(value, separator="; "):
    """Decode semicolon-joined artist string back to list."""
    if not value:
        return []
    return [a.strip() for a in value.split(separator) if a.strip()]


def _artists_from_exportify(value):
    """
    Decode exportify's artist field: comma-separated names with internal
    commas escaped as \\, (e.g. "Panic\\, At The Disco, The Beatles").
    """
    if not value:
        return []
    # Split on ', ' that are NOT preceded by backslash
    parts = re.split(r'(?<!\\),\s*', value)
    return [p.replace(r'\,', ',').strip() for p in parts if p.strip()]


def _ids_to_str(ids_dict):
    """Encode id dict as pipe-separated provider:value pairs for export."""
    if not ids_dict:
        return ""
    return "|".join(f"{k}:{v}" for k, v in ids_dict.items())


def _ids_from_str(value):
    """Decode pipe-separated provider:value string back to dict."""
    if not value:
        return {}
    result = {}
    for part in value.split("|"):
        if ":" in part:
            provider, _, val = part.partition(":")
            provider = provider.strip()
            val = val.strip()
            if provider and val:
                result[provider] = val
    return result


def _spotify_id_from_uri(uri):
    """Extract track ID from a Spotify URI or URL."""
    if not uri:
        return None
    # spotify:track:XXXX
    match = re.search(r"spotify:track:([A-Za-z0-9]+)", uri)
    if match:
        return match.group(1)
    # https://open.spotify.com/track/XXXX
    match = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]+)", uri)
    if match:
        return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

def _build_column_map(headers):
    """
    Given a list of CSV header strings, return a dict mapping
    canonical field name -> (column_index, matched_alias).
    Unrecognised headers are skipped.
    """
    headers_lower = [h.lower().strip() for h in headers]
    col_map = {}

    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in headers_lower:
                col_map[canonical] = (headers_lower.index(alias), alias)
                break

    return col_map


def _row_to_song(row, col_map):
    """
    Convert a CSV row (list of strings) to an ultrasonics song dict.
    Returns None if neither title nor artist is present.
    """
    def get(canonical):
        entry = col_map.get(canonical)
        if entry is None:
            return ""
        idx, _ = entry
        if idx >= len(row):
            return ""
        return row[idx].strip()

    def get_alias(canonical):
        entry = col_map.get(canonical)
        return entry[1] if entry is not None else None

    # --- title ---
    title = get("title")

    # --- artists ---
    if "artists" in col_map:
        raw = get("artists")
        # Branch on the matched header alias, not cell contents.
        # Only exportify's "artist name(s)" column uses comma-separation with \,-escaping.
        # Our own export and all other aliases are always ; -joined.
        if get_alias("artists") == "artist name(s)":
            artists = _artists_from_exportify(raw)
        else:
            artists = _artists_from_str(raw, "; ")
    else:
        artists = []

    if not title and not artists:
        return None

    song = {}
    if title:
        song["title"] = title
    if artists:
        song["artists"] = artists

    album = get("album")
    if album:
        song["album"] = album

    date = get("date")
    if date:
        song["date"] = date

    isrc = get("isrc")
    if isrc:
        song["isrc"] = isrc.upper()

    location = get("location")
    if location:
        song["location"] = location

    # Build id dict: start from ids column
    ids = {}
    raw_ids = get("ids")
    if raw_ids:
        ids.update(_ids_from_str(raw_ids))

    # Also accept Track URI / Spotify URI columns
    track_uri = get("track_uri")
    if track_uri:
        spotify_id = _spotify_id_from_uri(track_uri)
        if spotify_id:
            ids.setdefault("spotify", spotify_id)

    spotify_id_col = get("spotify_id")
    if spotify_id_col:
        ids.setdefault("spotify", spotify_id_col)

    if ids:
        song["id"] = ids

    return song


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(settings_dict, **kwargs):
    database = kwargs["database"]
    global_settings = kwargs["global_settings"]
    component = kwargs["component"]
    applet_id = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    csv_dir = (settings_dict.get("csv_dir") or database.get("csv_dir", "")).strip()
    if not csv_dir:
        raise Exception("CSV directory not configured.")

    if component == "outputs":
        # ------------------------------------------------------------------
        # EXPORT: write one CSV per playlist
        # ------------------------------------------------------------------
        os.makedirs(csv_dir, exist_ok=True)

        file_mode = "w" if settings_dict.get("existing_files", "Overwrite") == "Overwrite" else "a"

        for playlist in songs_dict:
            playlist_name = playlist.get("name", "Untitled")
            safe_name = re.sub(r'[\\/*?:"<>|]', "_", playlist_name)
            filepath = os.path.join(csv_dir, f"{safe_name}.csv")

            write_header = file_mode == "w" or not os.path.exists(filepath)

            with io.open(filepath, file_mode, newline="", encoding="utf-8") as f:
                writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)

                if write_header:
                    writer.writerow(EXPORT_COLUMNS)

                for song in playlist.get("songs", []):
                    row = [
                        song.get("title", ""),
                        _artists_to_str(song.get("artists", [])),
                        song.get("album", ""),
                        song.get("date", ""),
                        song.get("isrc", ""),
                        song.get("location", ""),
                        _ids_to_str(song.get("id", {})),
                    ]
                    writer.writerow(row)

            log.info(f"Wrote {len(playlist.get('songs', []))} songs to {filepath}")

    else:
        # ------------------------------------------------------------------
        # IMPORT: read CSV files and return songs_dict
        # ------------------------------------------------------------------
        source = settings_dict.get("source_path", "").strip() or csv_dir

        if os.path.isfile(source):
            csv_files = [source]
        elif os.path.isdir(source):
            csv_files = sorted(
                os.path.join(source, f)
                for f in os.listdir(source)
                if f.lower().endswith(".csv")
            )
        else:
            raise Exception(f"CSV source path does not exist: {source}")

        if not csv_files:
            raise Exception(f"No CSV files found in: {source}")

        songs_dict = []

        for filepath in csv_files:
            filename = os.path.splitext(os.path.basename(filepath))[0]
            log.info(f"Importing CSV: {filepath}")

            with io.open(filepath, "r", newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)

                try:
                    headers = next(reader)
                except StopIteration:
                    log.warning(f"Empty file: {filepath}, skipping.")
                    continue

                col_map = _build_column_map(headers)

                if not col_map:
                    log.warning(f"No recognised columns in {filepath}, skipping.")
                    continue

                # Group rows by playlist name column if present
                playlists_in_file = {}

                for row in reader:
                    if not any(cell.strip() for cell in row):
                        continue

                    # Determine playlist name for this row
                    playlist_entry = col_map.get("playlist")
                    if playlist_entry is not None:
                        playlist_col = playlist_entry[0]
                        playlist_name = row[playlist_col].strip() if playlist_col < len(row) else filename
                        playlist_name = playlist_name or filename
                    else:
                        playlist_name = filename

                    song = _row_to_song(row, col_map)
                    if song is None:
                        log.debug(f"Skipping row with no title or artist: {row}")
                        continue

                    if playlist_name not in playlists_in_file:
                        playlists_in_file[playlist_name] = []
                    playlists_in_file[playlist_name].append(song)

                for playlist_name, songs in playlists_in_file.items():
                    songs_dict.append({
                        "name": playlist_name,
                        "id": {},
                        "songs": songs,
                    })
                    log.info(f"Imported '{playlist_name}': {len(songs)} songs from {os.path.basename(filepath)}")

        return songs_dict


def test(database, **kwargs):
    """Verify the configured CSV directory exists and is readable."""
    csv_dir = database.get("csv_dir", "").strip()
    if not csv_dir:
        raise Exception("CSV directory is not configured.")
    if not os.path.exists(csv_dir):
        raise Exception(f"CSV directory does not exist: {csv_dir}")
    if not os.access(csv_dir, os.R_OK):
        raise Exception(f"CSV directory is not readable: {csv_dir}")
    log.info(f"CSV directory OK: {csv_dir}")


def builder(**kwargs):
    component = kwargs["component"]
    database = kwargs["database"]

    if component == "inputs":
        settings_dict = [
            {
                "type": "string",
                "value": "Import playlists from CSV files. Reads the configured directory (or a single file path). "
                "Supports ultrasonics CSV export, exportify, and Soundiiz-style CSVs.",
            },
            {
                "type": "text",
                "label": "Source Path (file or directory)",
                "name": "source_path",
                "value": database.get("csv_dir", ""),
            },
            {
                "type": "string",
                "value": "Recognised columns (case-insensitive): title / track name, artists / artist name(s), "
                "album / album name, date / album release date, isrc, location / path, ids, track uri / spotify uri.",
            },
        ]
        return settings_dict

    else:
        settings_dict = [
            {
                "type": "string",
                "value": "Export playlists to CSV files (one file per playlist). "
                "Columns: title, artists, album, date, isrc, location, ids.",
            },
            {
                "type": "radio",
                "label": "Existing Files",
                "name": "existing_files",
                "id": "existing_files",
                "options": ["Overwrite", "Append"],
                "required": True,
            },
        ]
        return settings_dict
