#!/usr/bin/env python3
"""Write MusicBrainz ids into an artist's files, so nothing has to guess again.

Every part of this system works out which band a folder belongs to by comparing
names: the bridge, Navidrome's metadata agent, the panel in the browser. They
guess separately and they disagree. Ten artists are called "Delirium" and one
showed an Italian prog discography for a Honduran metal band; the only artist
called "Nihilismo" in MusicBrainz is a punk band that shares nothing with the
one in this library.

An id in the file ends the argument. Navidrome reads these tags and reports the
id over Subsonic, and the bridge then skips name resolution entirely — which is
both exact and free. It also fixes the names no catalogue will ever match,
because the library spells them its own way: "AC-DC" is never going to find
"AC/DC" by asking.

Tag names follow the Picard convention, which is what Navidrome's mappings.yaml
already lists as aliases.

Usage:

    tag-mbids.py --artist <navidrome-artist-id>                 # print a plan
    tag-mbids.py --artist <navidrome-artist-id> --apply         # write it
    tag-mbids.py --artist <id> --artist-mbid <mbid> --apply     # when the
                                                                # lookup is wrong
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

try:
    from mutagen.id3 import ID3, TXXX, ID3NoHeaderError
    from mutagen.flac import FLAC
except ImportError:
    sys.exit("needs mutagen: pip install mutagen")

NAVIDROME = os.environ.get("NAVIDROME_URL", "http://localhost:4533").rstrip("/")
USER = os.environ.get("NAVIDROME_USER", "")
PASSWORD = os.environ.get("NAVIDROME_PASS", "")
# Navidrome reports paths relative to whatever it was told to index.
MUSIC_FOLDER = os.environ.get("NAVIDROME_MUSIC_FOLDER", "/mnt/Entretenimiento/Musica")
# MusicBrainz asks callers to identify themselves and answers 503 to those who
# do not, so this carries a contact the way the bridge's does.
UA = os.environ.get(
    "MB_USER_AGENT",
    "navidrome-lidarr-bridge-tagger/1.0 ( https://github.com/danielbanariba )")

# The three ids worth writing. A recording id would have to be matched track by
# track, which is a different and far less reliable job than naming the artist.
TAGS = {
    "artist": "MusicBrainz Artist Id",
    "albumartist": "MusicBrainz Album Artist Id",
    "releasegroup": "MusicBrainz Release Group Id",
}


def subsonic(endpoint: str, **params):
    if not (USER and PASSWORD):
        sys.exit("set NAVIDROME_USER and NAVIDROME_PASS")
    salt = secrets.token_hex(8)
    token = hashlib.md5((PASSWORD + salt).encode(), usedforsecurity=False).hexdigest()
    query = urllib.parse.urlencode({
        "u": USER, "t": token, "s": salt, "v": "1.16.1",
        "c": "mbid-tagger", "f": "json", **params,
    })
    with urllib.request.urlopen(f"{NAVIDROME}/rest/{endpoint}?{query}", timeout=60) as resp:
        body = json.load(resp)["subsonic-response"]
    if body.get("status") != "ok":
        sys.exit(f"{endpoint}: {(body.get('error') or {}).get('message')}")
    return body


_TOKEN: list[str] = []


def native(path: str):
    """Navidrome's own API, which is the only one that reports real paths.

    Subsonic's `path` is synthesised from tags — "Delirium/Abismo/01 - …" for a
    file that actually lives four directories deeper under a genre tree. Joining
    it to the music folder yields a path that does not exist, which is how an
    earlier version of this script found nothing to write to.
    """
    if not _TOKEN:
        body = json.dumps({"username": USER, "password": PASSWORD}).encode()
        req = urllib.request.Request(f"{NAVIDROME}/auth/login", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            _TOKEN.append(json.load(resp)["token"])
    req = urllib.request.Request(f"{NAVIDROME}{path}",
                                 headers={"X-ND-Authorization": f"Bearer {_TOKEN[0]}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def album_files(album_id: str) -> list[str]:
    songs = native(f"/api/song?album_id={urllib.parse.quote(album_id)}&_start=0&_end=500")
    paths = []
    for song in songs:
        rel = song.get("path")
        if not rel:
            continue
        full = rel if os.path.isabs(rel) else os.path.join(MUSIC_FOLDER, rel)
        # An index entry can outlive the file it describes — anything renamed
        # since the last scan is still listed.
        if os.path.exists(full):
            paths.append(full)
    return sorted(set(paths))


def norm(title: str) -> str:
    """Same folding the bridge uses: accents differ between catalogues."""
    folded = unicodedata.normalize("NFKD", title.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    stripped = re.sub(r"\(.*?\)|\[.*?\]", " ", folded)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", stripped).split())


def musicbrainz_groups(artist_mbid: str) -> dict[str, str]:
    """Normalised album title -> release-group id."""
    url = ("https://musicbrainz.org/ws/2/release-group?"
           + urllib.parse.urlencode({"artist": artist_mbid, "type": "album",
                                     "limit": "100", "fmt": "json"}))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    # MusicBrainz answers 503 when it is busy rather than queueing, and a
    # one-shot request against it fails often enough to be worth retrying.
    data = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or attempt == 3:
                raise
            time.sleep(2 + attempt * 3)
    if data is None:
        return {}
    groups = {}
    for group in data.get("release-groups", []):
        if group.get("primary-type") == "Album" and group.get("title"):
            groups.setdefault(norm(group["title"]), group["id"])
    return groups


def write_tags(path: str, values: dict[str, str]) -> bool:
    """Set the ids on one file, leaving everything else in it alone."""
    if path.lower().endswith(".flac"):
        audio = FLAC(path)
        for desc, value in values.items():
            audio[desc.upper().replace(" ", "")] = value
        audio.save()
        return True
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    for desc, value in values.items():
        tags.delall(f"TXXX:{desc}")
        tags.add(TXXX(encoding=3, desc=desc, text=value))
    # v2.3 rather than v2.4: it is what the rest of this library carries, and
    # what every player in the house reads without argument.
    tags.save(path, v2_version=3)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--artist", required=True, help="Navidrome artist id")
    ap.add_argument("--artist-mbid",
                    help="use this MusicBrainz artist id instead of looking one up")
    ap.add_argument("--apply", action="store_true", help="write; otherwise print a plan")
    args = ap.parse_args()

    info = subsonic("getArtist", id=args.artist)["artist"]
    name = info.get("name", "?")
    artist_mbid = args.artist_mbid or info.get("musicBrainzId")
    if not artist_mbid:
        sys.exit(f"{name}: no MusicBrainz artist id known — pass --artist-mbid")

    print(f"\n  {name}")
    print(f"  artist mbid: {artist_mbid}\n")
    groups = musicbrainz_groups(artist_mbid)

    plan = []
    for album in info.get("album", []):
        title = album.get("name") or ""
        rgid = groups.get(norm(title))
        files = album_files(album["id"])
        plan.append((title, rgid, files))
        note = rgid or "no release group in MusicBrainz"
        print(f"    {title[:38]:<40} {len(files):>3} files   {note}")

    total = sum(len(f) for _, _, f in plan)
    if not args.apply:
        print(f"\n  {total} files would be tagged — pass --apply to write")
        return

    print()
    written = 0
    for title, rgid, files in plan:
        # An album MusicBrainz does not list still gets the artist ids: knowing
        # who recorded it is useful even when the release is not in the catalogue.
        values = {TAGS["artist"]: artist_mbid, TAGS["albumartist"]: artist_mbid}
        if rgid:
            values[TAGS["releasegroup"]] = rgid
        done = 0
        for path in files:
            try:
                write_tags(path, values)
                done += 1
            except Exception as exc:
                print(f"    {os.path.basename(path)}: {type(exc).__name__}: {exc}")
        written += done
        print(f"    {title[:38]:<40} {done:>3} written")

    print(f"\n  {written} of {total} files tagged")
    print("  rescan in Navidrome for it to read them back")


if __name__ == "__main__":
    main()
