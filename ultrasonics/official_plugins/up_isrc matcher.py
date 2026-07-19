#!/usr/bin/env python3

"""
up_isrc matcher

Modifier plugin that deduplicates songs across playlists and merges metadata
from duplicates into the surviving entry.

Scoring/matching is delegated entirely to ultrasonics.tools.fuzzymatch.similarity
(which already handles ISRC, service IDs, location, and fuzzy title/artist/album).
This plugin's unique value is the metadata-merge step: when a duplicate is found,
any ISRC, IDs, location, date, or album that the survivor is missing gets copied
from the duplicate before it is discarded.
"""

import copy

from ultrasonics import logs
from ultrasonics.tools import fuzzymatch

log = logs.create_log(__name__)

handshake = {
    "name": "isrc matcher",
    "description": "deduplicate songs and merge metadata from duplicates using fuzzymatch scoring",
    "type": ["modifiers"],
    "mode": ["playlists"],
    "version": "0.2",
    "settings": [
        {
            "type": "string",
            "value": "This modifier deduplicates songs across your playlists and merges metadata "
            "(ISRC, service IDs, location, date, album) from duplicates into the surviving entry.",
        },
        {
            "type": "text",
            "label": "Fuzzy Ratio",
            "name": "fuzzy_ratio",
            "value": "Recommended: 90",
        },
    ],
}


def _merge_metadata(target, source):
    """
    Merge metadata from source into target (non-destructive).
    Adds ISRC, IDs, location, date, and album if target lacks them.
    """
    if not target.get("isrc") and source.get("isrc"):
        target["isrc"] = source["isrc"]

    if source.get("id"):
        if "id" not in target:
            target["id"] = {}
        for provider, val in source["id"].items():
            if provider not in target["id"]:
                target["id"][provider] = val

    if not target.get("location") and source.get("location"):
        target["location"] = source["location"]

    if not target.get("date") and source.get("date"):
        target["date"] = source["date"]

    if not target.get("album") and source.get("album"):
        target["album"] = source["album"]


def run(settings_dict, **kwargs):
    """
    For each playlist in songs_dict:
    1. Deduplicate songs using fuzzymatch.similarity for scoring.
    2. Merge metadata from duplicates into the surviving entry.
    3. Return cleaned songs_dict.
    """
    database = kwargs["database"]
    songs_dict = kwargs["songs_dict"]

    fuzzy_ratio = 90
    try:
        val = settings_dict.get("fuzzy_ratio") or database.get("fuzzy_ratio")
        if val:
            fuzzy_ratio = float(val)
    except (ValueError, TypeError):
        pass

    log.info(f"ISRC Matcher using fuzzy ratio: {fuzzy_ratio}")

    for playlist in songs_dict:
        songs = playlist.get("songs", [])
        if not songs:
            continue

        deduplicated = []

        for song in songs:
            is_dup = False
            for existing in deduplicated:
                score = fuzzymatch.similarity(song, existing)
                if score and score >= fuzzy_ratio:
                    _merge_metadata(existing, song)
                    is_dup = True
                    break

            if not is_dup:
                deduplicated.append(copy.deepcopy(song))

        removed = len(songs) - len(deduplicated)
        if removed > 0:
            log.info(
                f"Playlist '{playlist.get('name', '?')}': removed {removed} duplicate(s), "
                f"{len(deduplicated)} songs remain."
            )

        playlist["songs"] = deduplicated

    return songs_dict


def builder(**kwargs):
    database = kwargs["database"]

    settings_dict = [
        {
            "type": "string",
            "value": "This modifier deduplicates songs across your playlists and merges metadata "
            "from duplicates into the surviving entry. Scoring uses the standard fuzzymatch "
            "hierarchy: location, ISRC, service IDs, then fuzzy title/artist/album.",
        },
        {
            "type": "text",
            "label": "Fuzzy Ratio",
            "name": "fuzzy_ratio",
            "value": f"Currently: {database.get('fuzzy_ratio', '90')}",
        },
    ]

    return settings_dict
