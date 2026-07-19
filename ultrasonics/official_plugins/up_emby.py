#!/usr/bin/env python3

"""
up_emby

Input and output plugin for Emby media server.
Emby and Jellyfin share an almost identical REST API; this plugin
subclasses JellyfinAPI with the Emby branding and any divergences.
"""

from ultrasonics import logs
from ultrasonics.official_plugins.up_jellyfin import JellyfinAPI, builder

log = logs.create_log(__name__)

handshake = {
    "name": "emby",
    "description": "sync playlists, favorites, albums, and artists to/from an emby server",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites", "albums", "artists"],
    "version": "0.1",
    "settings": [
        {
            "type": "text",
            "label": "Server URL",
            "name": "server_url",
            "value": "e.g. http://localhost:8096",
        },
        {
            "type": "text",
            "label": "Username",
            "name": "username",
            "value": "",
        },
        {
            "type": "text",
            "label": "Password",
            "name": "password",
            "value": "",
        },
        {
            "type": "select",
            "label": "Sync Mode",
            "name": "sync_mode",
            "options": ["playlists", "favorites", "albums", "artists"],
            "value": "playlists",
        },
        {
            "type": "text",
            "label": "Fuzzy Ratio",
            "name": "fuzzy_ratio",
            "value": "Recommended: 85",
        },
    ],
}


class EmbyAPI(JellyfinAPI):
    """Emby variant — identical REST surface, different ID key."""
    ID_KEY = "emby"


def run(settings_dict, **kwargs):
    """Delegates to up_jellyfin.run() with an EmbyAPI instance patched in."""
    from ultrasonics.official_plugins import up_jellyfin

    database = kwargs["database"]
    server_url = database.get("server_url", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")

    if not all([server_url, username, password]):
        raise Exception("Emby plugin requires server URL, username, and password.")

    # Patch the API class used inside up_jellyfin.run()
    _orig = up_jellyfin.JellyfinAPI

    class _Patched(EmbyAPI):
        pass

    up_jellyfin.JellyfinAPI = _Patched
    try:
        return up_jellyfin.run(settings_dict, **kwargs)
    finally:
        up_jellyfin.JellyfinAPI = _orig


def test(database, **kwargs):
    server_url = database.get("server_url", "").strip()
    username = database.get("username", "").strip()
    password = database.get("password", "")
    if not all([server_url, username, password]):
        raise Exception("Server URL, username, and password are required.")
    api = EmbyAPI(server_url, username, password)
    api.authenticate()
    log.info(f"Emby connection OK: {server_url}")
