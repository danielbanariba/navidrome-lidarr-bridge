#!/usr/bin/env python3
"""Stop Lidarr hunting for records that are already on the shelf.

Lidarr only sees its own root folder. A library organised anywhere else is
invisible to it, so every album it catalogues and cannot find a file for reads
as missing — and it keeps searching indexers for it, forever. On this library
that was eighty-six of a hundred and forty-six wanted albums: fifty-nine per
cent of the work spent chasing music that was never gone.

Navidrome is the authority on what is owned, so this asks Navidrome and
unmonitors what Lidarr is looking for in vain.

What it will not touch:

  * An album somebody actually asked for. `/request` writes those down in
    `requested.json`, and they are exempt however they look from here — an
    upgrade request is, by definition, a request for a record already owned,
    so without that record the two are indistinguishable.
  * An album Lidarr does hold a file for. Nothing is missing there.
  * An album the library does not have. That is a real gap and the whole point.

Nothing is deleted, and monitoring is one click to restore.

Usage:

    reconcile-monitoring.py                 # print a plan, change nothing
    reconcile-monitoring.py --apply         # unmonitor them
    reconcile-monitoring.py --artist Spasm  # just one, to see what it does
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The same title rule the bridge and the panel use. Comparing titles a second
# way here would let this tool and the panel disagree about what is owned, and
# a disagreement in this direction unmonitors a record that really is missing.
from bridge import norm_title, owns_title  # noqa: E402

LIDARR = os.environ.get("LIDARR_URL", "http://localhost:8686").rstrip("/")
LIDARR_KEY = os.environ.get("LIDARR_API_KEY", "")
NAVIDROME = os.environ.get("NAVIDROME_URL", "http://localhost:4533").rstrip("/")
USER = os.environ.get("NAVIDROME_USER", "")
PASSWORD = os.environ.get("NAVIDROME_PASS", "")
# Written by the bridge; read here. A missing file means no request was ever
# recorded, which is the correct reading for a library that predates it.
REQUESTED_PATH = os.path.join(os.environ.get("STATE_DIR", "/state"), "requested.json")


def lidarr(path: str, method: str = "GET", body=None):
    if not LIDARR_KEY:
        sys.exit("set LIDARR_API_KEY")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{LIDARR}/api/v1/{path}", data=data, method=method,
        headers={"X-Api-Key": LIDARR_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def subsonic(endpoint: str, **params):
    if not (USER and PASSWORD):
        sys.exit("set NAVIDROME_USER and NAVIDROME_PASS")
    salt = secrets.token_hex(8)
    token = hashlib.md5((PASSWORD + salt).encode(), usedforsecurity=False).hexdigest()
    query = urllib.parse.urlencode({
        "u": USER, "t": token, "s": salt, "v": "1.16.1",
        "c": "reconcile", "f": "json", **params,
    })
    with urllib.request.urlopen(f"{NAVIDROME}/rest/{endpoint}?{query}", timeout=90) as resp:
        body = json.load(resp)["subsonic-response"]
    if body.get("status") != "ok":
        sys.exit(f"{endpoint}: {(body.get('error') or {}).get('message')}")
    return body


def navidrome_albums(name: str) -> set[str] | None:
    """Normalised titles Navidrome holds for this artist, or None if unknown.

    None and the empty set are different answers and the caller must not
    confuse them: an artist Navidrome has never heard of proves nothing about
    what is owned, while an artist it knows with no albums is a real statement.
    Treating the first as "owns nothing" is harmless here — nothing would be
    unmonitored — but treating a lookup failure as an empty library elsewhere
    is how a tool like this deletes the wrong thing.
    """
    found = subsonic("search3", query=name, artistCount="10",
                     albumCount="0", songCount="0")["searchResult3"]
    match = next((a for a in found.get("artist", [])
                  if norm_title(a["name"]) == norm_title(name)), None)
    if match is None:
        return None
    info = subsonic("getArtist", id=match["id"]).get("artist", {})
    return {norm_title(a["name"]) for a in info.get("album", []) if a.get("name")}


def has_file(album: dict) -> bool:
    stats = album.get("statistics") or {}
    return bool(stats.get("trackFileCount"))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="unmonitor them; without it nothing is written")
    ap.add_argument("--artist", help="only this artist, by the name Lidarr uses")
    args = ap.parse_args()

    try:
        asked = json.load(open(REQUESTED_PATH))
    except (OSError, ValueError):
        asked = {}
    print(f"\n  {len(asked)} request(s) on record — those are exempt\n")

    artists = lidarr("artist")
    if args.artist:
        artists = [a for a in artists
                   if norm_title(a["artistName"]) == norm_title(args.artist)]
        if not artists:
            sys.exit(f"Lidarr has no artist called {args.artist!r}")

    plan: list[tuple[str, dict]] = []
    unknown: list[str] = []
    for artist in sorted(artists, key=lambda a: a["artistName"]):
        name = artist["artistName"]
        owned = navidrome_albums(name)
        if owned is None:
            unknown.append(name)
            continue
        phantoms = []
        for album in lidarr(f"album?artistId={artist['id']}"):
            if not album.get("monitored") or has_file(album):
                continue
            if str(album["id"]) in asked:
                continue
            if owns_title(owned, album["title"]):
                phantoms.append(album)
        if phantoms:
            plan.append((name, phantoms))

    total = sum(len(p) for _, p in plan)
    for name, phantoms in plan:
        print(f"    {name[:34]:<36} {len(phantoms)}")
        for album in phantoms:
            year = (album.get("releaseDate") or "????")[:4]
            print(f"        {year}  {album['title'][:52]}")

    if unknown:
        print(f"\n  {len(unknown)} artist(s) Navidrome does not have — left alone:")
        print("    " + ", ".join(sorted(unknown)[:12]) + ("…" if len(unknown) > 12 else ""))

    if not total:
        print("\n  nothing to reconcile — Lidarr is not chasing anything you own")
        return

    if not args.apply:
        print(f"\n  {total} album(s) would be unmonitored — pass --apply to do it")
        return

    print()
    done = 0
    for name, phantoms in plan:
        for album in phantoms:
            album["monitored"] = False
            try:
                lidarr(f"album/{album['id']}", "PUT", album)
                done += 1
            except urllib.error.HTTPError as exc:
                print(f"    {album['title'][:40]}: HTTP {exc.code}")
        print(f"    {name[:34]:<36} {len(phantoms)} unmonitored")

    print(f"\n  {done} of {total} unmonitored. Nothing was deleted; monitoring one "
          f"again is a click in Lidarr,\n  or the panel's own button.")


if __name__ == "__main__":
    main()
