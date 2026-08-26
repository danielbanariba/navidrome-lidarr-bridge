#!/usr/bin/env python3
"""Find the albums with no cover art and fetch one for each.

Navidrome draws its own placeholder when a folder holds no image and the files
carry none embedded — a blue record with the word "navidrome" on it. It is not
wrong, but a shelf of them is unreadable: covers are how anyone finds an album
at a glance, and 169 of them here had none.

Lidarr writes cover art for what it manages, once its metadata provider is
switched on. Nothing writes it for the rest of a library, which is most of it.

Three sources, best first. The Cover Art Archive is asked by release-group id
whenever the files carry one, which is exact. Failing that MusicBrainz is
searched by artist and title to find the id. Discogs answers last, because its
images are smaller and its match is looser.

The largest image on offer wins: a 200-pixel thumbnail is worse than no cover
at all once it is stretched across a tile.

Usage:

    fetch-covers.py                      # list what is missing, write nothing
    fetch-covers.py --apply              # fetch and write cover.jpg
    fetch-covers.py --apply --limit 20   # a few at a time
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

NAVIDROME = os.environ.get("NAVIDROME_URL", "http://localhost:4533").rstrip("/")
USER = os.environ.get("NAVIDROME_USER", "")
PASSWORD = os.environ.get("NAVIDROME_PASS", "")
MUSIC_FOLDER = os.environ.get("NAVIDROME_MUSIC_FOLDER", "/mnt/Entretenimiento/Musica")
DISCOGS_KEY = os.environ.get("DISCOGS_KEY", "")
DISCOGS_SECRET = os.environ.get("DISCOGS_SECRET", "")
UA = os.environ.get(
    "MB_USER_AGENT",
    "navidrome-lidarr-bridge-covers/1.0 ( https://github.com/danielbanariba )")

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
AUDIO_EXT = (".flac", ".mp3", ".wav", ".ape", ".m4a", ".ogg", ".opus", ".wv")
# A cover smaller than this is worse than the placeholder once a tile stretches
# it, so it is refused rather than written. Held in a dict so the command line
# can lower it without the reassignment fighting the argument default above it.
LIMITS = {"min_edge": 400}

_LAST = {"mb": 0.0, "discogs": 0.0}


def throttle(who: str, gap: float) -> None:
    wait = gap - (time.monotonic() - _LAST[who])
    if wait > 0:
        time.sleep(wait)
    _LAST[who] = time.monotonic()


def get(url: str, headers: dict | None = None, timeout: int = 40) -> bytes | None:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 403):
                return None
            if attempt == 2:
                return None
            time.sleep(2 + attempt * 3)
        except Exception:
            if attempt == 2:
                return None
            time.sleep(2)
    return None


# ── what the library is missing ───────────────────────────────────────────

_TOKEN: list[str] = []


def navidrome(path: str):
    if not (USER and PASSWORD):
        sys.exit("set NAVIDROME_USER and NAVIDROME_PASS")
    if not _TOKEN:
        body = json.dumps({"username": USER, "password": PASSWORD}).encode()
        req = urllib.request.Request(f"{NAVIDROME}/auth/login", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            _TOKEN.append(json.load(resp)["token"])
    req = urllib.request.Request(f"{NAVIDROME}{path}",
                                 headers={"X-ND-Authorization": f"Bearer {_TOKEN[0]}"})
    with urllib.request.urlopen(req, timeout=240) as resp:
        return json.load(resp)


def embedded_art(path: str) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    return bool(out.stdout.strip())


def uncovered() -> list[dict]:
    """Every album with no image in its folder and none inside its files."""
    albums = navidrome("/api/album?_start=0&_end=10000")
    songs = navidrome("/api/song?_start=0&_end=100000")
    first: dict[str, dict] = {}
    for song in songs:
        first.setdefault(song.get("albumId"), song)

    out = []
    for album in albums:
        song = first.get(album["id"])
        if not song or not song.get("path"):
            continue
        folder = os.path.dirname(os.path.join(MUSIC_FOLDER, song["path"]))
        if not os.path.isdir(folder):
            continue
        if any(f.lower().endswith(IMAGE_EXT) for f in os.listdir(folder)):
            continue
        if embedded_art(os.path.join(MUSIC_FOLDER, song["path"])):
            continue
        out.append({
            "artist": (album.get("albumArtist") or album.get("artist") or "").strip(),
            "title": (album.get("name") or "").strip(),
            "year": album.get("maxYear"),
            "folder": folder,
            # Picard writes this when it has tagged the files, and it makes the
            # lookup exact instead of a search.
            "mbid": song.get("mbzAlbumId") or song.get("mbzReleaseGroupId") or "",
        })
    return out


# ── where a cover can come from ───────────────────────────────────────────

def edge(blob: bytes) -> int:
    """The image's shorter side, without decoding the whole thing."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", "-"], input=blob, capture_output=True)
    try:
        width, height = (int(x) for x in out.stdout.decode().strip().split(",")[:2])
        return min(width, height)
    except Exception:
        return 0


def from_caa(mbid: str) -> bytes | None:
    """The Cover Art Archive, asked by release-group id, largest first."""
    if not mbid:
        return None
    listing = get(f"https://coverartarchive.org/release-group/{mbid}")
    if not listing:
        return None
    try:
        images = json.loads(listing).get("images", [])
    except ValueError:
        return None
    fronts = [i for i in images if i.get("front")] or images
    for image in fronts:
        thumbs = image.get("thumbnails") or {}
        # Ordered by what they actually measure, not by the name they carry.
        for key in ("1200", "large", "500", "small", "250"):
            url = thumbs.get(key)
            if url:
                blob = get(url)
                if blob and edge(blob) >= LIMITS["min_edge"]:
                    return blob
        url = image.get("image")
        if url:
            blob = get(url)
            if blob and edge(blob) >= LIMITS["min_edge"]:
                return blob
    return None


def norm(text: str) -> str:
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    stripped = re.sub(r"\(.*?\)|\[.*?\]", " ", folded)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", stripped).split())


def bare(title: str) -> str:
    """A title without what the pressing added to it.

    Rips carry the catalogue number in the folder name and it ends up in the
    tag: "Zero Days (SPV279182CD)", "QR III [32DP 469]". Handing that to a
    catalogue search matches nothing at all, which is how ten of the first
    twelve albums here found no cover.
    """
    stripped = re.sub(r"\(.*?\)|\[.*?\]", " ", title or "")
    return " ".join(stripped.split()).strip(" -–—")


def _search_mb(query: str, title: str, artist: str) -> str | None:
    throttle("mb", 1.2)
    body = get("https://musicbrainz.org/ws/2/release-group?" +
               urllib.parse.urlencode({"query": query, "fmt": "json", "limit": 8}))
    if not body:
        return None
    try:
        groups = json.loads(body).get("release-groups", [])
    except ValueError:
        return None
    wanted, who = norm(title), norm(artist)
    for group in groups:
        if norm(group.get("title", "")) != wanted:
            continue
        credited = " ".join(norm(a["artist"]["name"])
                            for a in group.get("artist-credit", []))
        # The artist has to agree when there is one to agree with. A
        # compilation credits each track to somebody else, so the album artist
        # Navidrome reports is one of the guests rather than the record — for
        # those the title carries the match on its own, and only when it is
        # the single answer.
        if not who or who in credited or credited in who:
            return group["id"]
    if len(groups) == 1 and norm(groups[0].get("title", "")) == wanted:
        return groups[0]["id"]
    return None


def musicbrainz_id(artist: str, title: str) -> str | None:
    """The release-group id for an album the files do not name."""
    plain = bare(title)
    attempts = [(f'releasegroup:"{title}" AND artist:"{artist}"', title)]
    if plain and plain != title:
        attempts.append((f'releasegroup:"{plain}" AND artist:"{artist}"', plain))
        attempts.append((f'releasegroup:"{plain}"', plain))
    else:
        attempts.append((f'releasegroup:"{title}"', title))
    for query, used in attempts:
        found = _search_mb(query, used, artist)
        if found:
            return found
    return None


def from_discogs(artist: str, title: str) -> bytes | None:
    """Discogs, last, because its images are smaller and its match looser."""
    if not (DISCOGS_KEY and DISCOGS_SECRET):
        return None
    throttle("discogs", 1.2)
    query = urllib.parse.urlencode({
        "type": "release", "artist": artist,
        "release_title": bare(title) or title, "per_page": 3,
    })
    headers = {"User-Agent": UA,
               "Authorization": f"Discogs key={DISCOGS_KEY}, secret={DISCOGS_SECRET}"}
    body = get(f"https://api.discogs.com/database/search?{query}", headers)
    if not body:
        return None
    try:
        results = json.loads(body).get("results", [])
    except ValueError:
        return None
    for result in results:
        for key in ("cover_image", "thumb"):
            url = result.get(key)
            if not url or "spacer.gif" in url:
                continue
            blob = get(url, headers)
            if blob and edge(blob) >= LIMITS["min_edge"]:
                return blob
    return None


def find_cover(album: dict) -> tuple[bytes | None, str]:
    blob = from_caa(album["mbid"])
    if blob:
        return blob, "Cover Art Archive (tagged id)"
    found = musicbrainz_id(album["artist"], album["title"])
    if found:
        blob = from_caa(found)
        if blob:
            return blob, "Cover Art Archive (searched)"
    blob = from_discogs(album["artist"], album["title"])
    if blob:
        return blob, "Discogs"
    return None, "nothing found"


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write; otherwise only list")
    ap.add_argument("--limit", type=int, help="stop after this many albums")
    ap.add_argument("--min-edge", type=int, default=LIMITS["min_edge"],
                    help="refuse images shorter than this on either side "
                         "(default %(default)s)")
    args = ap.parse_args()
    LIMITS["min_edge"] = args.min_edge

    missing = uncovered()
    if args.limit:
        missing = missing[:args.limit]
    print(f"\n  {len(missing)} albums with no cover\n")
    if not args.apply:
        for album in missing[:40]:
            print(f"    {album['artist'][:22]:<24} {album['title'][:30]:<32} "
                  f"{album['folder'].replace(MUSIC_FOLDER + '/', '')[:40]}")
        if len(missing) > 40:
            print(f"    … and {len(missing) - 40} more")
        print("\n  nothing was written — pass --apply to fetch them")
        return

    written, failed = 0, []
    for album in missing:
        blob, where = find_cover(album)
        label = f"{album['artist'][:18]:<20} {album['title'][:26]:<28}"
        if not blob:
            failed.append(album)
            print(f"    {label} —")
            continue
        target = os.path.join(album["folder"], "cover.jpg")
        try:
            with open(target, "wb") as fh:
                fh.write(blob)
            # Navidrome decides what to revisit from the folder's own timestamp,
            # so writing a file inside it is not enough to be noticed.
            now = time.time()
            os.utime(album["folder"], (now, now))
            written += 1
            print(f"    {label} {edge(blob)}px  {where}")
        except OSError as exc:
            failed.append(album)
            print(f"    {label} could not write: {exc}")

    print(f"\n  {written} covers written, {len(failed)} still without one")
    if failed:
        print("  the ones nothing had:")
        for album in failed[:12]:
            print(f"    {album['artist'][:22]:<24} {album['title'][:34]}")
    if written:
        print("\n  rescan in Navidrome to see them")


if __name__ == "__main__":
    main()
