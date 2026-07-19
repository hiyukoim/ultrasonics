#!/usr/bin/env python3

"""
up_remove duplicates

Simple modifier plugin that removes duplicate songs within each playlist.
Unlike the isrc matcher, this does NOT merge metadata — it just keeps the
first occurrence of each song and discards later duplicates.
"""

from ultrasonics import logs
from ultrasonics.tools import fuzzymatch

log = logs.create_log(__name__)

handshake = {
    "name": "remove duplicates",
    "description": "remove duplicate songs within each playlist",
    "type": ["modifiers"],
    "mode": ["playlists"],
    "version": "0.1",
    "settings": [
        {
            "type": "string",
            "value": "Removes duplicate songs from each playlist. "
            "The first occurrence is kept; subsequent duplicates are removed.",
        },
        {
            "type": "text",
            "label": "Fuzzy Ratio (similarity threshold)",
            "name": "fuzzy_ratio",
            "value": "Recommended: 90",
        },
    ],
}


def run(settings_dict, **kwargs):
    """
    Modifier: iterate through each playlist and remove duplicate songs.
    """
    database = kwargs.get("database")
    songs_dict = kwargs.get("songs_dict", [])

    try:
        fuzzy_ratio = float(settings_dict.get("fuzzy_ratio") or 90)
    except (ValueError, TypeError):
        fuzzy_ratio = 90

    total_removed = 0

    for playlist in songs_dict:
        songs = playlist.get("songs", [])
        if not songs:
            continue

        unique_songs = []
        for song in songs:
            is_dup = fuzzymatch.duplicate(song, unique_songs, fuzzy_ratio)
            if is_dup:
                total_removed += 1
                log.debug(
                    f"Removing duplicate: {song.get('title', '?')} - "
                    f"{'; '.join(song.get('artists', ['?']))}"
                )
            else:
                unique_songs.append(song)

        playlist["songs"] = unique_songs

    log.info(f"Removed {total_removed} duplicate(s) across all playlists.")
    return songs_dict


def builder(**kwargs):
    component = kwargs.get("component")
    if component == "modifiers":
        return handshake.get("settings", [])
    return []


def test(database, **kwargs):
    return True
