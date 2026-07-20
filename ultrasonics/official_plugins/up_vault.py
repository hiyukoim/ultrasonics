#!/usr/bin/env python3

"""
up_vault

First-class input and output plugin for the ultrasonics vault.
This makes the vault selectable in the applet builder just like any platform.

As INPUT:  returns vault playlists as a standard songs_dict with all known
           platform IDs pre-filled in each track's  id  dict.

As OUTPUT: receives songs_dict from any other adapter and imports it into
           the vault (upsert — never overwrites existing matched tracks).

This enables explicit applet patterns such as:
  [Spotify input]   → [vault output]         (backup Spotify to vault)
  [vault input]     → [Navidrome output]     (push vault to Navidrome)
  [vault input, main only] → [all outputs]  (source-of-truth push)
"""

from ultrasonics import logs
from ultrasonics.tools import vault

log = logs.create_log(__name__)

handshake = {
    "name": "vault",
    "description": "use the ultrasonics vault as an input (read) or output (write) in any applet",
    "type": ["inputs", "outputs"],
    "mode": ["playlists", "main only"],
    "version": "0.1",
    "settings": [
        {
            "type": "string",
            "value": (
                "When used as INPUT the vault returns playlists already stored in "
                "the vault, with all known platform IDs pre-filled so downstream "
                "adapters can skip re-matching. "
                "When used as OUTPUT the vault imports (upserts) the incoming "
                "playlists into the master library — no data is ever overwritten."
            ),
        },
        {
            "type": "radio",
            "label": "Filter (input mode only)",
            "name": "filter",
            "id": "vault_filter",
            "options": ["all playlists", "main only", "by tag"],
        },
        {
            "type": "text",
            "label": "Tag (when filter = by tag)",
            "name": "tag",
            "value": "",
        },
    ],
}


def run(settings_dict, **kwargs):
    database   = kwargs["database"]
    component  = kwargs["component"]
    songs_dict = kwargs.get("songs_dict", [])

    # ── OUTPUT: import incoming songs_dict into vault ─────────────────────────
    if component == "outputs":
        source = settings_dict.get("source_platform") or "vault"
        for playlist_item in songs_dict:
            try:
                vault.import_songs_dict(playlist_item, source)
            except Exception as exc:
                log.warning(f"up_vault output: import failed for '{playlist_item.get('name')}': {exc}")
        return

    # ── INPUT: export vault playlists to songs_dict ───────────────────────────
    filt = (database.get("filter") or "all playlists").strip().lower()
    tag  = (database.get("tag") or "").strip() or None

    main_only = (filt == "main only")
    tag_filter = tag if filt == "by tag" else None

    playlists = vault.list_playlists(tag=tag_filter, main_only=main_only)
    output = []
    for pl in playlists:
        cid    = pl["canonical_id"]
        tracks = vault.get_playlist_tracks_with_links(cid)
        songs  = []
        for t in tracks:
            song = {
                "title":   t["title"],
                "artists": t["artists"],
                "album":   t.get("album") or "",
            }
            if t.get("isrc"):
                song["isrc"] = t["isrc"]
            if t.get("date"):
                song["date"] = t["date"]
            if t.get("image"):
                song["image"] = t["image"]
            # Populate id dict from all known linked platform IDs
            id_map = {"vault": cid}
            for lnk in t.get("links", []):
                if lnk["status"] == "linked" and lnk.get("platform_id"):
                    id_map[lnk["platform"]] = lnk["platform_id"]
            song["id"] = id_map
            songs.append(song)

        # Build playlist id dict from playlist_links
        pl_id_map = {"vault": cid}
        for p in pl.get("platforms", []):
            pl_id_map[p] = p   # platform itself (we don't have per-platform playlist id here)
        # Use the actual platform_id from pl["platforms"] which are now full dicts
        # Re-query with proper get_playlist to get platform_ids
        pl_detail = vault.get_playlist(cid)
        if pl_detail:
            for p_entry in pl_detail.get("platforms", []):
                if isinstance(p_entry, dict):
                    pl_id_map[p_entry["platform"]] = p_entry.get("platform_id", p_entry["platform"])

        output.append({
            "name":    pl["name"],
            "id":      pl_id_map,
            "songs":   songs,
        })

    return output


def builder(**kwargs):
    return []


def test(database, **kwargs):
    try:
        stats = vault.stats()
        log.info(f"Vault test: {stats['playlists']} playlists, {stats['tracks']} tracks.")
        return True
    except Exception as exc:
        log.error(f"Vault test failed: {exc}")
        return False
