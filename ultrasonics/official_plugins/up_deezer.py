#!/usr/bin/env python3

"""
up_deezer

Official input and output plugin for Deezer. Can access a user's public or private playlists, and read songs from, or save songs to them.
Will not overwrite songs already in a playlist, so stuff like date added are kept accurate.

XDGFX, 2020
"""

import json
import os
import re
import time

import requests
from tqdm import tqdm

from app import _ultrasonics
from ultrasonics import logs
from ultrasonics.tools import api_key, fuzzymatch, name_filter

log = logs.create_log(__name__)

handshake = {
    "name": "deezer",
    "description": "sync your playlists, favorites, albums, and artists to and from deezer",
    "type": [
        "inputs",
        "outputs"
    ],
    "mode": [
        "playlists",
        "favorites",
        "albums",
        "artists"
    ],
    "version": "0.4",
    "settings": [
        {
            "type": "auth",
            "label": "Authorise Deezer",
            "path": "/deezer/auth/request"
        },
        {
            "type": "string",
            "value": "Songs will always attempt to be matched using fixed values like ISRC or Deezer URI, however if you're trying to sync music without these tags, fuzzy matching will be used instead."
        },
        {
            "type": "string",
            "value": "This means that the titles 'You & Me - Flume Remix', and 'You & Me (Flume Remix)' will probably qualify as the same song [with a fuzzy score of 96.85 if all other fields are identical]. However, 'You, Me, & the Log Flume Ride' probably won't 🎢 [the score was 88.45 assuming *all* other fields are identical]. The fuzzyness of this matching is determined with the below setting. A value of 100 means all song fields must be identical to pass as duplicates. A value of 0 means any song will quality as a match, even if they are completely different. 👽"
        },
        {
            "type": "text",
            "label": "Default Global Fuzzy Ratio",
            "name": "fuzzy_ratio",
            "value": "Recommended: 90"
        },
        {
            "type": "string",
            "value": "If you sync a playlist to Deezer which doesn't already exist, ultrasonics will create a new playlist for you automatically ✨. Would you like any new playlists to be public or private?"
        },
        {
            "type": "radio",
            "label": "Created Playlists",
            "name": "created_playlists",
            "id": "created_playlists",
            "options": [
                "Public",
                "Private"
            ]
        }
    ]
}


def run(settings_dict, **kwargs):
    """
    Runs the up_deezer plugin.

    Important note: songs will only be appended to playlists if they are new!
    No songs will be removed from existing playlists, nothing will be over-written.
    This behaviour is different from some other plugins.
    """

    database = kwargs["database"]
    global_settings = kwargs["global_settings"]
    component = kwargs["component"]
    applet_id = kwargs["applet_id"]
    songs_dict = kwargs["songs_dict"]

    class Deezer:
        """
        Class for interactions with Deezer through the Deezer api.
        """

        def api(self, url, method="GET", params=None, data=None):
            """
            Make a request to the Deezer API, with error handling.

            @return: response JSON if successful
            """
            if method == "GET":
                r = requests.get(url, params=params)

                if r.status_code == 4:
                    time.sleep(5)
                    r = requests.get(url, params=params)

            elif method == "POST":
                r = requests.post(url, data=data)

                if r.status_code == 4:
                    time.sleep(5)
                    r = requests.post(url, data=data)

            elif method == "DELETE":
                r = requests.delete(url, params=params)

                if r.status_code == 4:
                    time.sleep(5)
                    r = requests.post(url, data=data)

            else:
                raise Exception(f"Unknown api method: {method}")

            if r.status_code != 200:
                log.error(f"Unexpected status code: {r.status_code}")
                log.error(r.text)
                raise Exception("Unexpected status code")

            try:
                if r.json().get("error"):
                    log.error(f"An error was returned from the Deezer API.")
                    raise UserWarning(r.json()["error"])
            except AttributeError:
                # Returned data is not in JSON format
                pass

            return r.json()

        def search(self, track):
            """
            Used to search the Deezer API for a song, supplied in standard songs_dict format.
            Will attempt to match using fixed values (Deezer ID, ISRC) before moving onto fuzzy values.

            @returns:
            Deezer ID, confidence score
            """

            cutoff_regex = [
                "[([](feat|ft|featuring|original|prod).+?[)\]]",
                "[ (\- )\-]+(feat|ft|featuring|original|prod).+?(?=[(\n])"
            ]

            # 1. Deezer ID
            try:
                deezer_id = track["id"]["deezer"]
                confidence = 100

                return deezer_id, confidence

            except KeyError:
                # Deezer ID was not supplied
                pass

            # 2. Other fields
            # Multiple searches are made as Deezer is more likely to return false negative (missing songs)
            # than false positive, when specifying many query parameters.

            results_list = []

            try:
                # If ISRC exists, only use that query
                url = f"https://api.deezer.com/2.0/track/isrc:{track['isrc']}"
                resp = self.api(url)

                if resp.get("error"):
                    # ISRC was not found in Deezer
                    raise KeyError

                results_list.append(self.deezer_to_songs_dict(track=resp))

            except KeyError:
                # If no ISRC, try all additional queries
                queries = []
                try:
                    title = re.sub(cutoff_regex[0], "",
                                   track['title'], flags=re.IGNORECASE) + "\n"

                    title = re.sub(cutoff_regex[1], " ",
                                   title, flags=re.IGNORECASE).strip()
                except KeyError:
                    pass

                try:
                    album = re.sub(cutoff_regex[0], "",
                                   track['album'], flags=re.IGNORECASE) + "\n"

                    album = re.sub(cutoff_regex[1], " ",
                                   album, flags=re.IGNORECASE).strip()
                except KeyError:
                    pass

                try:
                    queries.append(f'track:"{title}" album:"{album}"')
                except NameError:
                    pass

                try:
                    for artist in track["artists"]:
                        queries.append(
                            f'track:"{title}" artist:"{artist}"')
                except NameError:
                    pass

                # Execute all queries
                url = "https://api.deezer.com/search"

                for query in queries:
                    params = {
                        "q": query,
                        "limit": 20
                    }

                    results = self.api(url, params=params)["data"]

                    # Convert to ultrasonics format and append to results_list
                    for result in results:
                        result = self.deezer_to_songs_dict(result=result)
                        if result not in results_list:
                            results_list.append(result)

            if not results_list:
                # No items were found
                return "", 0

            # Check results with fuzzy matching
            confidence = 0

            for item in results_list:
                score = fuzzymatch.similarity(track, item)
                if score > confidence:
                    matched_track = item
                    confidence = score
                    if confidence > 100:
                        break

            deezer_id = matched_track["id"]["deezer"]

            return deezer_id, confidence

        def list_playlists(self):
            """
            Wrapper for Deezer `list_playlists` which overcomes the request item limit.
            """
            limit = 50

            url = "https://api.deezer.com/user/me/playlists"
            params = {
                "access_token": self.token,
                "limit": limit,
                "index": 0
            }

            playlists_response = self.api(url, params=params)
            playlists = playlists_response["data"]

            playlists_count = playlists_response["total"]

            # Get all playlists from the playlist
            while "next" in playlists_response:
                playlists_response = self.api(playlists_response["next"])
                playlists.extend(playlists_response["data"])

            log.info(f"Found {len(playlists)} playlist(s) on Deezer.")

            return playlists

        def playlist_tracks(self, playlist_id):
            """
            Returns a list of tracks in a playlist.
            """
            limit = 100

            url = f"https://api.deezer.com/playlist/{playlist_id}/tracks"
            params = {
                "access_token": self.token,
                "limit": limit,
                "index": 0
            }

            tracks_response = self.api(url, params=params)
            tracks = tracks_response["data"]

            tracks_count = tracks_response["total"]

            # Get all tracks from the playlist
            while "next" in tracks_response:
                tracks_response = self.api(tracks_response["next"])
                tracks.extend(tracks_response["data"])

            track_list = []

            # Convert from Deezer API format to ultrasonics format
            log.info("Converting tracks to ultrasonics format.")
            for track in tqdm(tracks, desc=f"Converting tracks in {playlist_id}"):
                try:
                    track_list.append(self.deezer_to_songs_dict(result=track))
                except UserWarning as e:
                    log.warning(
                        f"Unexpected response from Deezer for track {track}.")
                    log.warning(e)

            return track_list

        def remove_tracks_from_playlist(self, playlist_id, tracks):
            """
            Removes all occurrences of `tracks` from the specified playlist.
            """
            url = f"https://api.deezer.com/playlist/{playlist_id}/tracks"
            params = {
                "access_token": self.token,
                "songs": ",".join(tracks)
            }

            self.api(url, method="DELETE", params=params)

        def deezer_to_songs_dict(self, track=None, result=None):
            """
            Convert dictionary received from Deezer API to ultrasonics songs_dict format.
            Songs can be converted from search result format (result), or direct song format (data)
            Assumes title, artist(s), and id field are always present.
            """
            if result:
                # Get additional info about the track
                url = f"https://api.deezer.com/track/{result['id']}"

                track = self.api(url)

            artists = [item["name"] for item in track["contributors"]]

            try:
                album = track["album"]["title"]
            except KeyError:
                album = None

            try:
                date = track["release_date"]
            except KeyError:
                date = None

            try:
                isrc = track["isrc"]
            except KeyError:
                isrc = None

            item = {
                "title": track["title"],
                "artists": artists,
                "album": album,
                "date": date,
                "isrc": isrc,
                "id": {
                    "deezer": str(track["id"])
                }
            }

            # Remove any empty fields
            item = {k: v for k, v in item.items() if v}

            return item

    dz = Deezer()
    dz.token = re.match("access_token=([\w]+)&", database["auth"]).groups()[0]

    def _dz_album_to_dict(album):
        upc = album.get("upc") or album.get("UPC")
        artists = [a["name"] for a in album.get("artist", {}) if isinstance(a, dict)] if isinstance(album.get("artist"), list) else [album["artist"]["name"]] if album.get("artist") else []
        item = {
            "name": album.get("title", ""),
            "artists": artists,
            "id": {"deezer": str(album["id"])},
        }
        if upc:
            item["upc"] = upc
        if album.get("release_date"):
            item["date"] = album["release_date"]
        return {k: v for k, v in item.items() if v}

    def _dz_artist_to_dict(artist):
        item = {
            "name": artist.get("name", ""),
            "id": {"deezer": str(artist["id"])},
        }
        return {k: v for k, v in item.items() if v}

    mode = settings_dict.get("mode", "playlists")

    if component == "inputs":
        if mode == "favorites":
            # GET /user/me/tracks — liked tracks
            all_tracks = []
            url = "https://api.deezer.com/user/me/tracks"
            params = {"access_token": dz.token, "limit": 50, "index": 0}
            while True:
                resp = dz.api(url, params=params)
                items = resp.get("data", [])
                if not items:
                    break
                all_tracks.extend([dz.deezer_to_songs_dict(t) for t in items])
                params["index"] += len(items)
                if len(items) < 50:
                    break
            return [{"name": "Favorites", "id": {"deezer": "__favorites__"}, "songs": all_tracks}]

        elif mode == "albums":
            all_albums = []
            url = "https://api.deezer.com/user/me/albums"
            params = {"access_token": dz.token, "limit": 50, "index": 0}
            while True:
                resp = dz.api(url, params=params)
                items = resp.get("data", [])
                if not items:
                    break
                for alb in items:
                    # Fetch full album for UPC
                    try:
                        full = dz.api(f"https://api.deezer.com/album/{alb['id']}", params={"access_token": dz.token})
                        all_albums.append(_dz_album_to_dict(full))
                    except Exception:
                        all_albums.append(_dz_album_to_dict(alb))
                params["index"] += len(items)
                if len(items) < 50:
                    break
            return [{"name": "Albums", "id": {"deezer": "__albums__"}, "songs": all_albums}]

        elif mode == "artists":
            all_artists = []
            url = "https://api.deezer.com/user/me/artists"
            params = {"access_token": dz.token, "limit": 50, "index": 0}
            while True:
                resp = dz.api(url, params=params)
                items = resp.get("data", [])
                if not items:
                    break
                all_artists.extend([_dz_artist_to_dict(a) for a in items])
                params["index"] += len(items)
                if len(items) < 50:
                    break
            return [{"name": "Artists", "id": {"deezer": "__artists__"}, "songs": all_artists}]

        else:
            # playlists (default)
            playlists = dz.list_playlists()

            songs_dict = []

            for playlist in playlists:
                item = {
                    "name": playlist["title"],
                    "id": {
                        "deezer": playlist["id"]
                    }
                }

                songs_dict.append(item)

            # 2. Filter playlist titles
            songs_dict = name_filter.filter(songs_dict, settings_dict["filter"])

            # 3. Fetch songs from each playlist, build songs_dict
            log.info("Building songs_dict for playlists...")
            for i, playlist in tqdm(enumerate(songs_dict), desc="Building songs_dict"):
                tracks = dz.playlist_tracks(playlist["id"]["deezer"])

                songs_dict[i]["songs"] = tracks

            return songs_dict

    else:
        "Outputs mode"

        if mode == "favorites":
            # Add liked tracks — POST /user/me/tracks?track_id=...
            for playlist in songs_dict:
                for song in playlist.get("songs", []):
                    dz_id = song.get("id", {}).get("deezer")
                    if not dz_id:
                        result = dz.search(song)
                        if result and len(result) >= 2 and result[1] is not None:
                            dz_id = result[0]
                    if dz_id:
                        try:
                            dz.api(
                                "https://api.deezer.com/user/me/tracks",
                                method="POST",
                                data={"access_token": dz.token, "track_id": dz_id},
                            )
                        except Exception as e:
                            log.warning(f"Failed to favorite {song.get('title')}: {e}")
            return

        if mode == "albums":
            # Save albums — POST /user/me/albums?album_id=...
            for playlist in songs_dict:
                for album_item in playlist.get("songs", []):
                    dz_id = album_item.get("id", {}).get("deezer")
                    if not dz_id:
                        upc = album_item.get("upc")
                        if upc:
                            try:
                                resp = dz.api(
                                    f"https://api.deezer.com/album/upc:{upc}",
                                    params={"access_token": dz.token},
                                )
                                dz_id = str(resp.get("id", ""))
                            except Exception:
                                pass
                        if not dz_id:
                            name = album_item.get("name", "")
                            artist = album_item.get("artists", [""])[0] if album_item.get("artists") else ""
                            query = f"{artist} {name}".strip()
                            try:
                                resp = dz.api(
                                    "https://api.deezer.com/search/album",
                                    params={"q": query, "access_token": dz.token, "limit": 5},
                                )
                                results = resp.get("data", [])
                                if results:
                                    dz_id = str(results[0]["id"])
                            except Exception:
                                pass
                    if dz_id:
                        try:
                            dz.api(
                                "https://api.deezer.com/user/me/albums",
                                method="POST",
                                data={"access_token": dz.token, "album_id": dz_id},
                            )
                        except Exception as e:
                            log.warning(f"Failed to save album {album_item.get('name')}: {e}")
            return

        if mode == "artists":
            # Follow artists — POST /user/me/artists?artist_id=...
            for playlist in songs_dict:
                for artist_item in playlist.get("songs", []):
                    dz_id = artist_item.get("id", {}).get("deezer")
                    if not dz_id:
                        name = artist_item.get("name", "")
                        try:
                            resp = dz.api(
                                "https://api.deezer.com/search/artist",
                                params={"q": name, "access_token": dz.token, "limit": 1},
                            )
                            results = resp.get("data", [])
                            if results:
                                dz_id = str(results[0]["id"])
                        except Exception:
                            pass
                    if dz_id:
                        try:
                            dz.api(
                                "https://api.deezer.com/user/me/artists",
                                method="POST",
                                data={"access_token": dz.token, "artist_id": dz_id},
                            )
                        except Exception as e:
                            log.warning(f"Failed to follow artist {artist_item.get('name')}: {e}")
            return

        # playlists (default)
        # Get a list of current user playlists
        current_playlists = dz.list_playlists()

        for playlist in songs_dict:
            # Check the playlist already exists in Deezer
            playlist_id = ""
            try:
                if playlist["id"]["deezer"] in [item["id"] for item in current_playlists]:
                    playlist_id = playlist["id"]["deezer"]
            except KeyError:
                if playlist["name"] in [item["title"] for item in current_playlists]:
                    playlist_id = [
                        item["id"] for item in current_playlists if item["title"] == playlist["name"]][0]
            if playlist_id:
                log.info(
                    f"Playlist {playlist['name']} already exists, updating that one.")
            else:
                # Playlist must be created
                log.info(
                    f"Playlist {playlist['name']} does not exist, creating it now...")

                url = "https://api.deezer.com/user/me/playlists"
                data = {
                    "access_token": dz.token,
                    "title": playlist["name"]
                }

                playlist_id = dz.api(url, method="POST", data=data)["id"]

                public = "true" if database["created_playlists"] == "Public" else "false"
                description = "Created automatically by ultrasonics with 💖"

                url = f"https://api.deezer.com/playlist/{playlist_id}"
                data = {
                    "access_token": dz.token,
                    "public": public,
                    "description": description
                }

                response = dz.api(url, method="POST", data=data)

                if not response is True:
                    raise Exception(
                        f"Unexpected response while updating playlist: {response}")

                existing_tracks = []
                existing_ids = []

            # Get all tracks already in the playlist
            if "existing_tracks" not in vars():
                existing_tracks = dz.playlist_tracks(playlist_id)
                existing_ids = [str(item["id"]["deezer"])
                                for item in existing_tracks]

            # Add songs which don't already exist in the playlist
            new_ids = []
            duplicate_ids = []

            log.info("Searching for matching songs in Deezer.")
            for song in tqdm(playlist["songs"], desc=f"Searching Deezer for songs from {playlist['name']}"):
                # First check for fuzzy duplicate without Deezer api search
                duplicate = False
                for item in existing_tracks:
                    score = fuzzymatch.similarity(song, item)

                    if score > float(database.get("fuzzy_ratio") or 90):
                        # Duplicate was found
                        duplicate_ids.append(item['id']['deezer'])
                        duplicate = True
                        break

                if duplicate:
                    continue

                try:
                    deezer_id, confidence = dz.search(song)
                except UserWarning:
                    # Likely no data was returned
                    log.warning(
                        f"No data was returned when searching for song: {song}")
                    continue

                if deezer_id in existing_ids:
                    duplicate_ids.append(deezer_id)

                if confidence > float(database.get("fuzzy_ratio") or 90):
                    new_ids.append(str(deezer_id))
                else:
                    log.debug(
                        f"Could not find song {song['title']} in Deezer; will not add to playlist.")

            if settings_dict["existing_playlists"] == "Update":
                # Remove any songs which aren't in `uris` from the playlist
                remove_ids = [
                    deezer_id for deezer_id in existing_ids if deezer_id not in new_ids + duplicate_ids]

                # Skip if no songs are to be removed
                if remove_ids:
                    dz.remove_tracks_from_playlist(playlist_id, remove_ids)

            # Remove duplicates from the list of new ids
            new_ids = list(set(new_ids))

            # Add tracks to playlist in batches of 100
            url = f"https://api.deezer.com/playlist/{playlist_id}/tracks"
            data = {
                "access_token": dz.token
            }

            while len(new_ids) > 100:
                data["songs"] = ",".join(new_ids[0:100])
                dz.api(url, method="POST", data=data)
                new_ids = new_ids[100:]

            # Add all remaining tracks
            if new_ids:
                data["songs"] = ",".join(new_ids)
                dz.api(url, method="POST", data=data)


def builder(**kwargs):
    component = kwargs["component"]

    if component == "inputs":
        settings_dict = [
            {
                "type": "string",
                "value": "You can use regex style filters to only select certain playlists. For example, 'disco' would sync playlists 'Disco 2010' and 'nu_disco', or '2020$' would only sync playlists which ended with the value '2020'."
            },
            {
                "type": "string",
                "value": "Leave it blank to sync everything 🤓."
            },
            {
                "type": "text",
                "label": "Filter",
                "name": "filter",
                "value": ""
            }
        ]

        return settings_dict

    else:
        settings_dict = [
            {
                "type": "string",
                "value": "Do you want to update any existing playlists with the same name (replace any songs already in the playlist), or append to them?"
            },
            {
                "type": "radio",
                "label": "Existing Playlists",
                "name": "existing_playlists",
                "id": "existing_playlists",
                "options": [
                    "Append",
                    "Update"
                ],
                "required": True
            }
        ]

        return settings_dict
