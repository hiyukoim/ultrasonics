#!/usr/bin/env python3

"""
resolver
Shared vault-aware track resolution used by non-Subsonic adapters.

resolve_track(song, platform_id_key, search_fn, to_songs_dict_fn,
              fuzzy_ratio, matchings_mod) -> platform_id | None

- Checks song["id"][id_key] first.
- Falls back to matchings store (cross-platform, then ISRC).
- Falls back to search_fn(query) -> list of raw platform track objects.
- On success, saves to matchings and flips vault orphan→linked if applicable.
"""

from ultrasonics import logs

log = logs.create_log(__name__)


def resolve_track(song, id_key, search_fn, to_songs_dict_fn, fuzzy_ratio, matchings_mod):
    """
    Resolve a platform_id for `song` on platform `id_key`.

    Parameters
    ----------
    song            : songs_dict item (may contain vault canonical_id and other platform IDs)
    id_key          : str, e.g. "ytmusic", "soundcloud"
    search_fn       : callable(query: str) -> list[raw_track]
    to_songs_dict_fn: callable(raw_track) -> songs_dict item
    fuzzy_ratio     : float threshold (0-100)
    matchings_mod   : the matchings module (or stub)

    Returns
    -------
    str platform_id, or None if unresolvable
    """
    from ultrasonics.tools import fuzzymatch

    # 1. Already known
    try:
        return song["id"][id_key]
    except KeyError:
        pass

    # 2. Matchings store (cross-platform)
    for plat, pid in (song.get("id") or {}).items():
        if plat in ("vault", id_key) or not pid:
            continue
        learned = matchings_mod.lookup(plat, str(pid), id_key)
        if learned:
            return learned

    # 3. ISRC lookup
    if song.get("isrc"):
        learned = matchings_mod.lookup_by_isrc(song["isrc"], id_key)
        if learned:
            return learned

    # 4. Fuzzy search
    title  = (song.get("title") or "").strip()
    artist = (song.get("artists") or [""])[0]
    q = " ".join(filter(None, [title, artist]))
    if not q:
        return None

    try:
        results = search_fn(q)
    except Exception as e:
        log.warning(f"resolver.resolve_track search failed ({id_key}): {e}")
        return None

    best_score, best_id = 0, None
    for r in results:
        try:
            candidate = to_songs_dict_fn(r)
            score = fuzzymatch.similarity(song, candidate)
            if score and score > best_score:
                best_score, best_id = score, candidate.get("id", {}).get(id_key)
        except Exception:
            continue

    if best_score >= fuzzy_ratio and best_id:
        best_id = str(best_id)
        # Persist to matchings
        for plat, pid in (song.get("id") or {}).items():
            if plat not in ("vault", id_key) and pid:
                try:
                    matchings_mod.save(plat, str(pid), id_key, best_id,
                                       src_isrc=song.get("isrc"),
                                       src_title=song.get("title"),
                                       src_artist="; ".join(song.get("artists") or []))
                except Exception:
                    pass
        # Vault orphan flip
        vault_cid = (song.get("id") or {}).get("vault")
        if vault_cid:
            try:
                from ultrasonics.tools import vault as _vault
                tl = _vault.get_link(vault_cid, id_key)
                if tl and tl["status"] == "orphan":
                    _vault.flip_orphan_to_linked(vault_cid, id_key, best_id)
            except Exception as ve:
                log.debug(f"vault flip skipped for {id_key}: {ve}")
        return best_id

    return None
