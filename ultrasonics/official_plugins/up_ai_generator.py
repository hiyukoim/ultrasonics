#!/usr/bin/env python3

"""
up_ai_generator

Input plugin: generates a playlist via a free LLM (Claude via claude.ai API
or Google Gemini via generativelanguage.googleapis.com) based on genre, mood,
decade, and count settings.

The plugin returns {title, artists} items only — no platform IDs.
The downstream matching pipeline (fuzzymatch + output adapters) resolves them.

Supported providers:
  - anthropic  (requires ANTHROPIC_API_KEY in environment or settings)
  - gemini     (requires GEMINI_API_KEY in environment or settings)
"""

import json
import os
import re

import requests

from app import _ultrasonics
from ultrasonics import logs

log = logs.create_log(__name__)

handshake = {
    "name": "ai generator",
    "description": "generate a playlist using a free llm (claude / gemini) by genre, mood, decade, or count",
    "type": ["inputs"],
    "mode": ["playlists"],
    "version": "0.1",
    "settings": [
        {
            "type": "select",
            "label": "LLM Provider",
            "name": "provider",
            "options": ["anthropic", "gemini"],
            "value": "gemini",
        },
        {
            "type": "text",
            "label": "API Key",
            "name": "api_key",
            "value": "Leave blank to use ANTHROPIC_API_KEY / GEMINI_API_KEY env var",
        },
        {
            "type": "string",
            "value": "Configure what the AI should generate. All fields are optional; combine them for more specific results.",
        },
        {
            "type": "text",
            "label": "Genre (e.g. jazz, indie rock)",
            "name": "genre",
            "value": "",
        },
        {
            "type": "text",
            "label": "Mood (e.g. upbeat, melancholic)",
            "name": "mood",
            "value": "",
        },
        {
            "type": "text",
            "label": "Decade (e.g. 1980s, 2000s)",
            "name": "decade",
            "value": "",
        },
        {
            "type": "text",
            "label": "Extra prompt (e.g. 'for a road trip')",
            "name": "extra_prompt",
            "value": "",
        },
        {
            "type": "text",
            "label": "Number of tracks",
            "name": "count",
            "value": "20",
        },
        {
            "type": "text",
            "label": "Playlist name",
            "name": "playlist_name",
            "value": "AI Generated Playlist",
        },
    ],
}

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"


def _build_prompt(genre, mood, decade, extra, count):
    parts = []
    if genre:
        parts.append(f"genre: {genre}")
    if mood:
        parts.append(f"mood: {mood}")
    if decade:
        parts.append(f"decade: {decade}")
    if extra:
        parts.append(extra)

    criteria = ", ".join(parts) if parts else "a good mix of popular music"

    return (
        f"Generate a playlist of exactly {count} songs ({criteria}).\n"
        "Return ONLY a JSON array of objects with keys \"title\" and \"artist\". "
        "No explanations, no markdown, no extra text. Example:\n"
        '[{"title": "Song Name", "artist": "Artist Name"}, ...]\n'
        "Make sure all songs are real, well-known tracks."
    )


def _call_anthropic(api_key, prompt):
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-3-haiku-20240307",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(_ANTHROPIC_URL, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["content"][0]["text"]


def _call_gemini(api_key, prompt):
    url = f"{_GEMINI_URL}?key={api_key}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 4096},
    }
    resp = requests.post(url, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise Exception(f"Unexpected Gemini response structure: {e}\n{data}")


def _parse_tracks(raw_text):
    """Extract JSON array from LLM response, tolerating markdown fences."""
    # Strip markdown code fences
    text = re.sub(r"```(?:json)?", "", raw_text).strip()

    # Find first '[' to last ']'
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise Exception(f"LLM did not return a JSON array. Response:\n{raw_text[:500]}")

    raw_json = text[start:end + 1]
    try:
        items = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise Exception(f"Failed to parse LLM JSON: {e}\nRaw: {raw_json[:500]}")

    songs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("song") or item.get("name") or ""
        artist = item.get("artist") or item.get("artists") or ""
        if not title:
            continue
        if isinstance(artist, list):
            artists = artist
        elif artist:
            artists = [artist]
        else:
            artists = []
        songs.append({"title": title.strip(), "artists": artists})

    return songs


def run(settings_dict, **kwargs):
    """Generate a playlist via LLM and return songs_dict (title+artists only, no IDs)."""
    database = kwargs["database"]

    provider = database.get("provider", settings_dict.get("provider", "gemini")).strip().lower()
    api_key = (database.get("api_key") or settings_dict.get("api_key", "")).strip()

    if not api_key:
        env_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "GEMINI_API_KEY"
        api_key = os.environ.get(env_var, "")
    if not api_key:
        raise Exception(
            f"No API key configured for {provider}. "
            f"Set it in plugin settings or as an environment variable."
        )

    genre = (database.get("genre") or settings_dict.get("genre", "")).strip()
    mood = (database.get("mood") or settings_dict.get("mood", "")).strip()
    decade = (database.get("decade") or settings_dict.get("decade", "")).strip()
    extra = (database.get("extra_prompt") or settings_dict.get("extra_prompt", "")).strip()
    playlist_name = (database.get("playlist_name") or settings_dict.get("playlist_name", "AI Generated Playlist")).strip()

    count = 20
    try:
        count = int(database.get("count") or settings_dict.get("count") or 20)
        count = max(1, min(count, 200))
    except (ValueError, TypeError):
        pass

    prompt = _build_prompt(genre, mood, decade, extra, count)
    log.info(f"Calling {provider} to generate {count} tracks...")

    try:
        if provider == "anthropic":
            raw = _call_anthropic(api_key, prompt)
        elif provider == "gemini":
            raw = _call_gemini(api_key, prompt)
        else:
            raise Exception(f"Unknown LLM provider: {provider}. Choose 'anthropic' or 'gemini'.")
    except Exception as e:
        raise Exception(f"LLM API call failed ({provider}): {e}")

    tracks = _parse_tracks(raw)

    if not tracks:
        raise Exception("LLM returned no tracks. Raw response:\n" + raw[:500])

    log.info(f"AI generator returned {len(tracks)} tracks.")

    return [{"name": playlist_name, "id": {"ai_generator": "generated"}, "songs": tracks}]


def builder(**kwargs):
    return [
        {
            "type": "string",
            "value": (
                "Configure genre, mood, decade, and count in the plugin settings. "
                "The AI will generate a list of real tracks; these will be matched "
                "against the destination adapter using fuzzy matching."
            ),
        },
    ]
