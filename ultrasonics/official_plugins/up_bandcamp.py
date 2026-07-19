#!/usr/bin/env python3

"""
up_bandcamp

Read-only input plugin for Bandcamp fan collections.
Reads the public fan page (purchases + wishlist) for a given username.
No ISRC — tracks rely on title/artist fuzzy matching downstream.

Auth: none for public collections; optional browser cookies for private
      wishlists (paste the `bcsid` + `js_logged_in` cookie values).

Vault contract: upserts tracks via vault.import_songs_dict on the
ultrasonics pipeline; this adapter only produces a songs_dict as an input.
"""

import json
import re
import time

from ultrasonics import logs
from ultrasonics.tools import http_retry

log = logs.create_log(__name__)

handshake = {
    "name": "bandcamp",
    "description": "import your bandcamp collection (purchases & wishlist) — read only",
    "type": ["inputs"],
    "mode": ["collection", "wishlist"],
    "version": "0.2",
    "settings": [
        {
            "type": "string",
            "value": (
                "Bandcamp is read-only: tracks are imported from your public fan page "
                "and matched to other platforms by title + artist. "
                "Your Bandcamp username is the part of your profile URL: bandcamp.com/<username>"
            ),
        },
        {"type": "text",  "label": "Bandcamp Username", "name": "username",  "value": ""},
        {
            "type": "radio",
            "label": "What to import",
            "name": "collection_type",
            "id": "collection_type",
            "options": ["collection", "wishlist"],
        },
        {
            "type": "string",
            "value": (
                "If your wishlist is private, paste your Bandcamp session cookies "
                "below so the scraper can access them."
            ),
        },
        {"type": "text", "label": "Cookie: bcsid (optional)",      "name": "cookie_bcsid",       "value": ""},
        {"type": "text", "label": "Cookie: js_logged_in (optional)","name": "cookie_js_logged_in","value": ""},
    ],
}

_FAN_API = "https://bandcamp.com/api/fancollection/1/{collection_type}_items"
_PAGE_SIZE = 50


def _get_fan_id(username, session_headers):
    """Resolve Bandcamp fan_id from the public profile page."""
    resp = http_retry.get(f"https://bandcamp.com/{username}", headers=session_headers)
    resp.raise_for_status()
    m = re.search(r'"fan_id"\s*:\s*(\d+)', resp.text)
    if not m:
        raise Exception(
            f"Could not find fan_id for Bandcamp user '{username}'. "
            "Check that the username is correct and the profile is public."
        )
    return int(m.group(1))


def _fetch_items(collection_type, fan_id, session_headers):
    """
    Fetch all items from a Bandcamp collection or wishlist via the fan API.
    Handles pagination automatically.
    """
    items = []
    older_than_token = f"9999999999:0:a::"  # start sentinel

    while True:
        payload = {
            "fan_id": fan_id,
            "older_than_token": older_than_token,
            "count": _PAGE_SIZE,
        }
        url = _FAN_API.format(collection_type=collection_type)
        resp = http_retry.post(url, json=payload, headers=session_headers)
        resp.raise_for_status()
        data = resp.json()

        page_items = data.get("items", [])
        items.extend(page_items)

        if not data.get("more_available", False):
            break

        # Advance token: last item's token
        if page_items:
            older_than_token = page_items[-1].get("token", older_than_token)
        else:
            break

        time.sleep(0.4)  # be polite

    return items


def _item_to_song(item):
    """Convert a Bandcamp fan-API item to a songs_dict entry."""
    title   = item.get("album_title") or item.get("item_title") or ""
    artist  = item.get("band_name") or item.get("artist") or ""
    album   = item.get("album_title") or ""
    image   = item.get("item_art_url") or item.get("art_id") or None

    # Bandcamp item_id is an integer album/track id — store it per-platform
    item_id = str(item.get("item_id") or item.get("album_id") or "")

    song = {
        "title":   title,
        "artists": [artist] if artist else [],
        "album":   album,
    }
    if item_id:
        song["id"] = {"bandcamp": item_id}
    if image and isinstance(image, str) and image.startswith("http"):
        song["image"] = image
    return song


def run(settings_dict, **kwargs):
    database  = kwargs["database"]
    component = kwargs["component"]

    if component != "inputs":
        raise Exception("up_bandcamp is an input-only plugin.")

    username        = database.get("username", "").strip()
    collection_type = database.get("collection_type", "collection").lower()
    cookie_bcsid    = database.get("cookie_bcsid", "").strip()
    cookie_js       = database.get("cookie_js_logged_in", "").strip()

    if not username:
        raise Exception("Bandcamp username is required.")

    if collection_type not in ("collection", "wishlist"):
        collection_type = "collection"

    session_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ultrasonics/1.0)",
        "Accept": "application/json",
    }
    if cookie_bcsid and cookie_js:
        session_headers["Cookie"] = f"bcsid={cookie_bcsid}; js_logged_in={cookie_js}"

    log.info(f"Bandcamp: fetching {collection_type} for user '{username}'")

    fan_id = _get_fan_id(username, session_headers)
    log.info(f"Bandcamp: fan_id={fan_id}")

    items = _fetch_items(collection_type, fan_id, session_headers)
    log.info(f"Bandcamp: fetched {len(items)} items")

    songs = [_item_to_song(item) for item in items if item.get("item_title") or item.get("album_title")]

    return [{
        "name": f"Bandcamp {collection_type.title()} — {username}",
        "id": {"bandcamp": f"{username}_{collection_type}"},
        "songs": songs,
    }]


def builder(**kwargs):
    return []


def test(database, **kwargs):
    username = database.get("username", "").strip()
    if not username:
        return False
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ultrasonics/1.0)"}
        fan_id = _get_fan_id(username, headers)
        log.info(f"Bandcamp test: fan_id={fan_id} resolved for '{username}'")
        return True
    except Exception as exc:
        log.error(f"Bandcamp test failed: {exc}")
        return False
