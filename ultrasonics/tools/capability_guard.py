#!/usr/bin/env python3

"""
capability_guard

Validates applet plugin selections and enforces per-adapter track limits
before an applet runs. Checks:

1. Input/output capability compatibility — if an output adapter cannot
   write (e.g. Bandcamp is read-only), raises a clear error before running.
2. maxTracksInPlaylist — truncates incoming songs_dict with a warning rather
   than letting the adapter fail silently or partially.

Capability matrix is declared here for the adapters that are most constrained.
All values derived from the adapter spec (caps field `PpTtAaRr`).
"""

from ultrasonics import logs

log = logs.create_log(__name__)

# Per-adapter limits and write capabilities.
# Key = plugin `name` from handshake.
# write = False means the adapter is read-only (for its listed types).
# max_tracks = max tracks per playlist. None = effectively unlimited.
_ADAPTER_CAPS = {
    "spotify":       {"write": True,  "max_tracks": 10000},
    "deezer":        {"write": True,  "max_tracks": 5000},
    "tidal":         {"write": True,  "max_tracks": 10000},
    "qobuz":         {"write": True,  "max_tracks": 1999},
    "soundcloud":    {"write": True,  "max_tracks": 500},
    "navidrome":     {"write": True,  "max_tracks": 10000},
    "subsonic":      {"write": True,  "max_tracks": 10000},
    "jellyfin":      {"write": True,  "max_tracks": 10000},
    "emby":          {"write": True,  "max_tracks": 10000},
    "ytmusic":       {"write": True,  "max_tracks": 5000},
    "youtube":       {"write": True,  "max_tracks": 5000},
    "lastfm":        {"write": True,  "max_tracks": 10000},  # scrobble only
    "plex":          {"write": True,  "max_tracks": 10000},
    "bandcamp":      {"write": False, "max_tracks": None},   # read-only
    "csv":           {"write": True,  "max_tracks": None},
    "local playlists": {"write": True, "max_tracks": None},
    "url":           {"write": False, "max_tracks": None},   # input-only
    "ai generator":  {"write": False, "max_tracks": None},   # input-only
    "webhook":       {"write": False, "max_tracks": None},   # trigger-only
    "time trigger":  {"write": False, "max_tracks": None},   # trigger-only
}

# Plugin names that are input-only (cannot be used as outputs)
_INPUT_ONLY = {"url", "ai generator", "webhook", "time trigger"}

# Plugin names that are output-only (cannot be used as inputs with songs)
_OUTPUT_ONLY = set()

# Adapter names that are fully read-only (no write capability)
_READ_ONLY_ADAPTERS = {"bandcamp", "url", "ai generator"}


def validate_applet(applet_plans):
    """
    Validate that the applet's input/output plugin combination makes sense.

    Raises Exception with a human-readable message if:
    - An output plugin is read-only.
    - Input/output modes are incompatible (e.g. artist-mode output on a
      playlist-only adapter — future check).

    Returns silently if OK.
    """
    outputs = applet_plans.get("outputs", [])
    for plugin in outputs:
        name = plugin.get("plugin", "")
        caps = _ADAPTER_CAPS.get(name, {})
        if caps.get("write") is False:
            raise Exception(
                f"Cannot use '{name}' as an output: this adapter is read-only. "
                "Choose a writable adapter for outputs."
            )
        if name in _INPUT_ONLY:
            raise Exception(
                f"'{name}' can only be used as an input, not an output."
            )


def apply_track_limits(songs_dict, output_plugin_name):
    """
    Truncate playlists in songs_dict to the output adapter's max_tracks limit.

    Returns the (possibly truncated) songs_dict.
    Emits a warning for each truncated playlist.
    """
    caps = _ADAPTER_CAPS.get(output_plugin_name, {})
    max_tracks = caps.get("max_tracks")
    if max_tracks is None:
        return songs_dict

    result = []
    for playlist in songs_dict:
        songs = playlist.get("songs", [])
        if len(songs) > max_tracks:
            log.warning(
                f"Playlist '{playlist.get('name', '?')}' has {len(songs)} tracks "
                f"but '{output_plugin_name}' supports max {max_tracks}. "
                f"Truncating to {max_tracks}."
            )
            playlist = dict(playlist)
            playlist["songs"] = songs[:max_tracks]
        result.append(playlist)
    return result
