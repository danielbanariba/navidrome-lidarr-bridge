#!/usr/bin/env python3
"""Audition one lossy album, and keep a record so the next run takes another.

`best-release.py` answers "is there a better copy of this?" and somebody has
to ask it, album by album. That is the one part of this system a person still
has to drive, and it is the part they most want driven: a library gains a
badly-encoded record now and then and nobody notices, because a 128 kbps rip
looks exactly like a 24-bit master on the shelf.

So this asks the question once per run, on whichever album is most overdue,
and writes down what it heard. A timer runs it. The pace is the point rather
than a limitation: twenty auditions run back to back tripped the indexers'
rate limits and got two of them disabled, after which every answer was "no
better copy" about a search that never happened.

An album is re-asked about after RETRY_DAYS, because the answer expires. The
one release proven to hold this album in flac had five seeders when its file
list was read and none an hour later — that is not "no flac exists", it is
"nobody was sharing it just then".

Usage:

    audit-queue.py                 # audition the most overdue album
    audit-queue.py --dry-run       # say which one, and stop
    audit-queue.py --status        # what has been asked, and what came back
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from bridge import norm_title  # noqa: E402

LIDARR = os.environ.get("LIDARR_URL", "http://localhost:8686").rstrip("/")
LIDARR_KEY = os.environ.get("LIDARR_API_KEY", "")
NAVIDROME = os.environ.get("NAVIDROME_URL", "http://localhost:4533").rstrip("/")
USER = os.environ.get("NAVIDROME_USER", "")
PASSWORD = os.environ.get("NAVIDROME_PASS", "")
STATE_DIR = os.environ.get("STATE_DIR", os.path.join(ROOT, "state"))
LEDGER = os.path.join(STATE_DIR, "audited.json")
PYTHON = os.environ.get("AUDIT_PYTHON", os.path.join(ROOT, ".venv/bin/python"))
RETRY_DAYS = float(os.environ.get("AUDIT_RETRY_DAYS", "30"))
# A verdict keeps for a month. "Could not ask" keeps for hours, because it is
# not a verdict at all — it is a note that the question failed, and an outage
# that lasted an afternoon should not silence an album until September.
UNANSWERED_HOURS = float(os.environ.get("AUDIT_UNANSWERED_HOURS", "6"))
# Long enough for a slow swarm, short enough that a timer firing every half
# hour never overlaps with itself.
TIMEOUT = int(os.environ.get("AUDIT_TIMEOUT", "1500"))

LOSSY = {"mp3", "aac", "m4a", "ogg", "opus", "wma"}


def lidarr(path: str):
    if not LIDARR_KEY:
        sys.exit("set LIDARR_API_KEY")
    req = urllib.request.Request(f"{LIDARR}/api/v1/{path}",
                                 headers={"X-Api-Key": LIDARR_KEY})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def subsonic(endpoint: str, **params):
    if not (USER and PASSWORD):
        sys.exit("set NAVIDROME_USER and NAVIDROME_PASS")
    salt = secrets.token_hex(8)
    token = hashlib.md5((PASSWORD + salt).encode(), usedforsecurity=False).hexdigest()
    query = urllib.parse.urlencode({
        "u": USER, "t": token, "s": salt, "v": "1.16.1",
        "c": "audit-queue", "f": "json", **params,
    })
    with urllib.request.urlopen(f"{NAVIDROME}/rest/{endpoint}?{query}", timeout=90) as resp:
        body = json.load(resp)["subsonic-response"]
    if body.get("status") != "ok":
        sys.exit(f"{endpoint}: {(body.get('error') or {}).get('message')}")
    return body


_TOKEN: list[str] = []


def native(path: str):
    """Navidrome's own API, the only one that reports a file's real format."""
    if not _TOKEN:
        body = json.dumps({"username": USER, "password": PASSWORD}).encode()
        req = urllib.request.Request(f"{NAVIDROME}/auth/login", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            _TOKEN.append(json.load(resp)["token"])
    req = urllib.request.Request(f"{NAVIDROME}{path}",
                                 headers={"X-ND-Authorization": f"Bearer {_TOKEN[0]}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def lossy_albums() -> list[dict]:
    """Every album the library holds in a lossy format that Lidarr can act on.

    Navidrome decides what is owned and in what format, because it is the only
    one that has read the files. Lidarr supplies the id, because it is the only
    thing that can be asked to fetch anything.
    """
    out = []
    for artist in lidarr("artist"):
        name = artist["artistName"]
        found = subsonic("search3", query=name, artistCount="8",
                         albumCount="0", songCount="0")["searchResult3"]
        match = next((a for a in found.get("artist", [])
                      if norm_title(a["name"]) == norm_title(name)), None)
        if match is None:
            continue
        songs = native(f"/api/song?artist_id={match['id']}&_start=0&_end=2000")
        by_album: dict[str, list] = {}
        for song in songs:
            by_album.setdefault(song["album"], []).append(song)
        catalogue = {norm_title(a["title"]): a for a in lidarr(f"album?artistId={artist['id']}")}
        for title, tracks in by_album.items():
            if (tracks[0].get("suffix") or "").lower() not in LOSSY:
                continue
            album = catalogue.get(norm_title(title))
            if not album:
                continue
            out.append({
                "albumId": album["id"], "artist": name, "album": title,
                "bitrate": round(sum(t.get("bitRate", 0) for t in tracks) / len(tracks)),
            })
    # One entry per Lidarr album: two folders differing only in capitalisation
    # are one record to Lidarr, and auditioning it twice asks the same question
    # twice while something else waits.
    seen, unique = set(), []
    for row in sorted(out, key=lambda r: r["bitrate"]):
        if row["albumId"] in seen:
            continue
        seen.add(row["albumId"])
        unique.append(row)
    return unique


def load_ledger() -> dict:
    try:
        with open(LEDGER) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_ledger(data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, LEDGER)


def due(albums: list[dict], ledger: dict) -> dict | None:
    """The album most worth asking about now.

    Never-asked first, worst encoding first among those, because a 128 kbps rip
    has the most to gain. Then whatever was asked about longest ago, once its
    answer has had time to go stale.
    """
    fresh = [a for a in albums if str(a["albumId"]) not in ledger]
    if fresh:
        return fresh[0]

    now = time.time()

    def expired(album: dict) -> bool:
        entry = ledger.get(str(album["albumId"]), {})
        age = now - entry.get("at", 0)
        if entry.get("verdict") == "unanswered":
            return age > UNANSWERED_HOURS * 3600
        return age > RETRY_DAYS * 86400

    # Thirty-four albums here were filed unanswered while every indexer was
    # disabled. Treating that like a verdict would have left them unasked for a
    # month over an outage that lasted an afternoon — which is the whole reason
    # not having asked is recorded separately from having no answer.
    stale = [a for a in albums if expired(a)]
    if not stale:
        return None
    return min(stale, key=lambda a: ledger.get(str(a["albumId"]), {}).get("at", 0))


def read_verdict(output: str) -> tuple[str, str]:
    """What the audition concluded, as a word and the line it came from.

    "Nothing was searched" is not a verdict about the music and must never be
    recorded as one — an indexer that is not being asked cannot tell you the
    release is not there. It is written down as unanswered so the album comes
    back around rather than being taken as settled.
    """
    if "NOTHING WAS SEARCHED" in output:
        return "unanswered", "indexers were disabled; nothing was asked"
    for line in output.splitlines():
        stripped = line.strip()
        # Matched against what the audition actually prints. These were written
        # against an imagined output — "-> handed to Lidarr", with an arrow that
        # appears nowhere — so four successful upgrades were filed as failures,
        # one of them an album that is FLAC on disk today because of the very
        # run recorded as not having taken it.
        if stripped.startswith("handed to Lidarr"):
            return "replaced", stripped
        if "the best on offer is itself a transcode" in stripped:
            return "replaced-with-a-transcode", stripped
        if "nothing on offer beats what the library already has" in stripped:
            return "kept", stripped
    if re.search(r"\d+ proven lossless", output):
        return "found-not-taken", "a lossless copy was proven but not imported"
    if "no lossless copy found" in output:
        return "no-lossless", "no lossless copy in what could be read"
    if "nothing worth auditioning" in output:
        return "no-candidates", "no candidate release worth downloading"
    return "unclear", "the audition ended without a recognisable verdict"


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="name the album that is due and stop")
    ap.add_argument("--status", action="store_true",
                    help="print what has been asked and what came back")
    ap.add_argument("--candidates", type=int, default=4)
    args = ap.parse_args()

    ledger = load_ledger()

    if args.status:
        counts: dict[str, int] = {}
        for entry in ledger.values():
            counts[entry.get("verdict", "?")] = counts.get(entry.get("verdict", "?"), 0) + 1
        print(f"\n  {len(ledger)} album(s) audited\n")
        for verdict, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {verdict:<18} {n}")
        recent = sorted(ledger.items(), key=lambda kv: -kv[1].get("at", 0))[:12]
        print("\n  most recent:")
        for key, entry in recent:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("at", 0)))
            print(f"    {when}  {entry.get('label', key)[:44]:<46} {entry.get('verdict')}")
        return

    albums = lossy_albums()
    target = due(albums, ledger)
    if target is None:
        print(f"  nothing due: all {len(albums)} lossy album(s) asked about "
              f"within the last {RETRY_DAYS:g} days")
        return

    label = f"{target['artist']} — {target['album']} ({target['bitrate']} kbps)"
    print(f"  auditioning {label}  [album id {target['albumId']}]")
    if args.dry_run:
        print("  --dry-run: nothing was run")
        return

    started = time.time()
    try:
        done = subprocess.run(
            [PYTHON, os.path.join(HERE, "best-release.py"),
             "--album-id", str(target["albumId"]),
             "--candidates", str(args.candidates), "--download"],
            capture_output=True, text=True, timeout=TIMEOUT, cwd=ROOT)
        output = done.stdout + done.stderr
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        output += "\n  the audition ran past its timeout and was stopped"

    verdict, note = read_verdict(output)
    print(output.strip()[-1500:])
    ledger[str(target["albumId"])] = {
        "at": int(started), "label": label, "verdict": verdict, "note": note,
        "seconds": int(time.time() - started),
    }
    save_ledger(ledger)
    print(f"\n  recorded: {verdict} — {note}")


if __name__ == "__main__":
    main()
