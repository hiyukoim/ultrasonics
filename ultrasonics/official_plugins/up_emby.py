#!/usr/bin/env python3

"""
up_emby

Input and output plugin for Emby media server.
Emby and Jellyfin share the same REST surface; this plugin
subclasses JellyfinAPI with the Emby ID key and branding.
"""

from ultrasonics import logs
from ultrasonics.official_plugins.up_jellyfin import JellyfinAPI, builder, _parse_ratio

log = logs.create_log(__name__)

handshake = {
    "name": "emby",
    "description": "sync playlists, favorites, albums, and artists to/from an emby server",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "favorites", "albums", "artists"],
    "version": "0.2",
    "settings": [
        {"type": "text",   "label": "Server URL", "name": "server_url", "value": "e.g. http://localhost:8096"},
        {"type": "text",   "label": "Username",   "name": "username",   "value": ""},
        {"type": "text",   "label": "Password",   "name": "password",   "value": ""},
        {"type": "select", "label": "Sync Mode",  "name": "sync_mode",
         "options": ["playlists", "favorites", "albums", "artists"], "value": "playlists"},
        {"type": "text", "label": "Fuzzy Ratio", "name": "fuzzy_ratio", "value": "85"},
    ],
}


class EmbyAPI(JellyfinAPI):
    """Emby variant — identical REST surface, separate ID key."""
    ID_KEY = "emby"


def run(settings_dict, **kwargs):
    """Delegates to up_jellyfin.run() with EmbyAPI substituted."""
    from ultrasonics.official_plugins import up_jellyfin

    database   = kwargs["database"]
    server_url = database.get("server_url", "").strip()
    username   = database.get("username",   "").strip()
    password   = database.get("password",   "")

    if not all([server_url, username, password]):
        raise Exception("Emby: server URL, username, and password are required.")

    _orig = up_jellyfin.JellyfinAPI
    up_jellyfin.JellyfinAPI = EmbyAPI
    try:
        return up_jellyfin.run(settings_dict, **kwargs)
    finally:
        up_jellyfin.JellyfinAPI = _orig


def test(database, **kwargs):
    server_url = database.get("server_url", "").strip()
    username   = database.get("username",   "").strip()
    password   = database.get("password",   "")
    if not all([server_url, username, password]):
        raise Exception("Server URL, username, and password are required.")
    api = EmbyAPI(server_url, username, password)
    api.authenticate()
    log.info(f"Emby connected: {server_url}")
