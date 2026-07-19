#!/usr/bin/env python3

"""
up_isrc matcher

Modifier plugin that enriches and deduplicates songs using ISRC-first matching,
then falls back to normalized fuzzy matching (title + artist).

Use this between an Input and Output to:
- Deduplicate songs across playlists that may have come from different sources.
- Merge metadata (e.g. attach an ISRC found on one service to a song missing it).
- Remove unresolvable duplicates using a strict matching hierarchy.

Matching order:
1. Exact ISRC match (highest confidence).
2. Exact service ID match (e.g. same spotify/navidrome/deezer ID).
3. Normalized fuzzy match: lowercase + strip parenthetical feat/remix tags,
   then weighted comparison of title, artists, album.
"""

import copy
import re
import unicodedata

from fuzzywuzzy import fuzz

from ultrasonics import logs
from ultrasonics.tools import fuzzymatch

log = logs.create_log(__name__)

handshake = {
    "name": "isrc matcher",
    "description": "deduplicate and enrich songs using isrc-first resolution with normalized fuzzy fallback",
    "type": ["modifiers"],
    "mode": ["playlists"],
    "version": "0.1",
    "settings": [
        {
            "type": "string",
            "value": "This modifier resolves duplicates across playlists using a strict matching hierarchy: ISRC first, then service IDs, then normalized fuzzy matching on title and artist.",
        },
        {
            "type": "text",
            "label": "Fuzzy Ratio",
            "name": "fuzzy_ratio",
            "value": "Recommended: 90",
        },
    ],
}

# Regex patterns to strip before fuzzy comparison
_FEAT_PATTERNS = [
    r"[(\[][^)\]]*(?:feat|ft|featuring)[^)\]]*[)\]]",
    r"\s*[-–—]\s*(?:feat|ft|featuring)\b.*",
]

_REMIX_PATTERN = r"[(\[][^)\]]*(?:remix|mix|edit|version|ver\.?)[^)\]]*[)\]]"


def _normalize(text):
    """
    Normalize a string for comparison:
    - Unicode NFKD normalization
    - Lowercase
    - Strip feat/ft tags
    - Strip remix/edit/version tags
    - Collapse whitespace
    """
    if not text:
        return ""
    # Unicode normalize
    text = unicodedata.normalize("NFKD", text)
    text = text.lower().strip()
    # Strip feat patterns
    for pat in _FEAT_PATTERNS:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)
    # Strip remix/version parentheticals
    text = re.sub(_REMIX_PATTERN, "", text, flags=re.IGNORECASE)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_artists(artists):
    """Normalize and sort a list of artist names for comparison."""
    if not artists:
        return []
    return sorted(_normalize(a) for a in artists)


def _isrc_match(song_a, song_b):
    """Check if two songs share the same ISRC."""
    isrc_a = song_a.get("isrc")
    isrc_b = song_b.get("isrc")
    if isrc_a and isrc_b:
        return isrc_a.strip().upper() == isrc_b.strip().upper()
    return False


def _id_match(song_a, song_b):
    """Check if two songs share any common service ID."""
    ids_a = song_a.get("id", {})
    ids_b = song_b.get("id", {})
    for provider, val in ids_a.items():
        if provider in ids_b and str(val) == str(ids_b[provider]):
            return True
    return False


def _normalized_fuzzy_score(song_a, song_b):
    """
    Compute a normalized fuzzy similarity score.
    Uses normalized title and artists for comparison.
    Weights: title=10, artists=8, album=3.
    Returns 0-100.
    """
    weights = []
    scores = []

    # Title comparison
    title_a = _normalize(song_a.get("title", ""))
    title_b = _normalize(song_b.get("title", ""))
    if title_a and title_b:
        score = fuzz.ratio(title_a, title_b)
        weights.append(10)
        scores.append(score)

    # Artist comparison
    artists_a = _normalize_artists(song_a.get("artists", []))
    artists_b = _normalize_artists(song_b.get("artists", []))
    if artists_a and artists_b:
        # Compare joined artist strings with partial token sort
        joined_a = " ".join(artists_a)
        joined_b = " ".join(artists_b)
        score = fuzz.token_sort_ratio(joined_a, joined_b)
        weights.append(8)
        scores.append(score)

    # Album comparison (lower weight, often differs across services)
    album_a = _normalize(song_a.get("album", ""))
    album_b = _normalize(song_b.get("album", ""))
    if album_a and album_b:
        score = fuzz.ratio(album_a, album_b)
        weights.append(3)
        scores.append(score)

    if not weights:
        return 0

    total_weight = sum(weights)
    weighted_score = sum(s * w for s, w in zip(scores, weights)) / total_weight
    return weighted_score


def _is_match(song_a, song_b, fuzzy_ratio):
    """
    Determine if two songs are the same track, using the matching hierarchy:
    1. ISRC match -> instant match
    2. Service ID match -> instant match
    3. Normalized fuzzy score >= fuzzy_ratio -> match
    """
    if _isrc_match(song_a, song_b):
        return True
    if _id_match(song_a, song_b):
        return True
    score = _normalized_fuzzy_score(song_a, song_b)
    return score >= fuzzy_ratio


def _merge_metadata(target, source):
    """
    Merge metadata from source into target (non-destructive).
    Adds ISRC, IDs, location, and date if target lacks them.
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
    1. Build an index of songs by ISRC for fast lookup.
    2. Deduplicate songs: keep the first occurrence, merge metadata from duplicates.
    3. Return cleaned songs_dict.
    """
    database = kwargs["database"]
    songs_dict = kwargs["songs_dict"]

    # Determine fuzzy ratio
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
                if _is_match(song, existing, fuzzy_ratio):
                    # Merge any additional metadata from the duplicate
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
            "value": "This modifier deduplicates songs across your playlists using a strict matching hierarchy: ISRC first, then service IDs, then normalized fuzzy matching.",
        },
        {
            "type": "string",
            "value": "Normalization strips feat/ft tags, remix/version parentheticals, and lowercases everything before comparing. This improves matching across services that format titles differently.",
        },
        {
            "type": "text",
            "label": "Fuzzy Ratio",
            "name": "fuzzy_ratio",
            "value": f"Currently: {database.get('fuzzy_ratio', '90')}",
        },
    ]

    return settings_dict
