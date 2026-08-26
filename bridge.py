"""Navidrome -> Lidarr bridge.

Exposes the artists a user starred in Navidrome as a Lidarr "Custom List"
feed. Lidarr's CustomImport expects a JSON array of objects carrying a single
MusicBrainz artist id:

    [{"MusicBrainzId": "24e1b53c-3085-4581-8472-0b0088d2508c"}, ...]

Navidrome libraries frequently carry no MusicBrainz tags at all, so artist
names are resolved to ids through Lidarr's own /artist/lookup endpoint, which
already speaks to the metadata server Lidarr will use when adding the artist.

A name several artists share is settled by the library itself: unrelated bands
sharing a name do not share a back catalogue, so the candidate whose discography
contains the albums already owned is the right one. Only a name that nothing
distinguishes is reported on /status for a human to pin in overrides.json.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("bridge")

NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://host.docker.internal:4533").rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "")
LIDARR_URL = os.environ.get("LIDARR_URL", "http://lidarr:8686").rstrip("/")
LIDARR_API_KEY = os.environ.get("LIDARR_API_KEY", "")
# Which import list to refresh when the published set changes. Left empty the
# bridge discovers it, which only works while exactly one CustomImport exists.
LIDARR_IMPORTLIST_ID = os.environ.get("LIDARR_IMPORTLIST_ID", "").strip()
# How long to wait for Lidarr to list an album after its artist is imported.
IMPORT_WAIT_SECONDS = int(os.environ.get("IMPORT_WAIT_SECONDS", "45"))
# MusicBrainz asks for no more than one request a second, and identifies
# callers by User-Agent. Only used to break a tie between same-named artists.
MB_MIN_INTERVAL = float(os.environ.get("MB_MIN_INTERVAL", "1.1"))
MB_USER_AGENT = os.environ.get(
    "MB_USER_AGENT",
    "navidrome-lidarr-bridge/1.0 (+https://github.com/danielbanariba/navidrome-lidarr-bridge)")
# Discogs carries releases MusicBrainz has never heard of, so it is the better
# source for "what did this band actually put out". Read access needs no OAuth:
# key and secret go straight in a header, and doing so lifts the rate limit from
# 25 requests a minute to 60. Both empty still works, just slower.
DISCOGS_KEY = os.environ.get("DISCOGS_KEY", "").strip()
DISCOGS_SECRET = os.environ.get("DISCOGS_SECRET", "").strip()
DISCOGS_MIN_INTERVAL = float(os.environ.get(
    "DISCOGS_MIN_INTERVAL", "1.1" if DISCOGS_KEY and DISCOGS_SECRET else "2.5"))
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "900"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8687"))
STATE_DIR = os.environ.get("STATE_DIR", "/state")

# api.lidarr.audio is rate limited: pause between lookups, and back a failing
# name off exponentially instead of re-asking for it every single sync.
LOOKUP_DELAY_SECONDS = float(os.environ.get("LOOKUP_DELAY_SECONDS", "0.5"))
MAX_BACKOFF_SECONDS = int(os.environ.get("MAX_BACKOFF_SECONDS", str(6 * 3600)))

CACHE_PATH = os.path.join(STATE_DIR, "resolved.json")
OVERRIDES_PATH = os.path.join(STATE_DIR, "overrides.json")
# The set Lidarr was last told about. Persisted so a restart does not look like
# a change and refresh the list for nothing.
PUBLISHED_PATH = os.path.join(STATE_DIR, "published.json")
# The userscript that draws the panel inside Navidrome, served from /panel.user.js.
PANEL_PATH = os.environ.get("PANEL_PATH", "/app/panel.user.js")

# Where /userscript.js mirrors the panel from, and for how long a copy is kept.
# Serving it here rather than sending the browser to GitHub keeps the download
# on the same origin, which a blocklist or a shield cannot quietly break, and
# lets Tampermonkey update itself from a URL that is always reachable.
USERSCRIPT_SOURCE = os.environ.get(
    "USERSCRIPT_SOURCE",
    "https://raw.githubusercontent.com/danielbanariba/"
    "navidrome-missing-albums-userscript/main/navidrome-missing-albums.user.js")
USERSCRIPT_TTL = int(os.environ.get("USERSCRIPT_TTL", "600"))
# The URL the browser reaches /userscript.js at. Tampermonkey re-checks whatever
# @updateURL says, so this has to be the address the browser can actually use —
# not the one the container sees. Left empty it is derived from the request,
# which is right when the bridge is reached directly and wrong behind a proxy
# that strips a path prefix.
PUBLIC_SCRIPT_URL = os.environ.get("PUBLIC_SCRIPT_URL", "").strip()

# Only one sync may run at a time: the background loop and every GET /sync
# request (ThreadingHTTPServer serves each connection on its own thread) would
# otherwise interleave their writes to resolved.json.
SYNC_LOCK = threading.Lock()

# name -> {attempts, retry_at, reason, candidates}. In memory only, guarded by
# SYNC_LOCK; a restart simply retries everything once.
_BACKOFF: dict[str, dict] = {}


class BridgeError(RuntimeError):
    pass


# Errors that mean "this HTTP call did not produce usable JSON". urlopen only
# wraps *connection* failures in URLError; timeouts, resets and truncated or
# non-JSON bodies surface while the body is being read.
FETCH_ERRORS = (OSError, ValueError, http.client.HTTPException, BridgeError)


def _is_mbid(value: object) -> bool:
    """True for a canonical MusicBrainz id (a dashed UUID, any case)."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.strip().lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _get_json(url: str, headers: dict | None = None) -> object:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def subsonic(endpoint: str, **params) -> dict:
    """Call a Subsonic endpoint using salted-token auth."""
    if not (NAVIDROME_USER and NAVIDROME_PASS):
        raise BridgeError("NAVIDROME_USER / NAVIDROME_PASS are not set")
    salt = secrets.token_hex(8)
    digest = hashlib.md5((NAVIDROME_PASS + salt).encode(), usedforsecurity=False)
    query = urllib.parse.urlencode({
        "u": NAVIDROME_USER, "t": digest.hexdigest(), "s": salt,
        "v": "1.16.1", "c": "navidrome-lidarr-bridge", "f": "json",
        **params,
    })
    payload = _get_json(f"{NAVIDROME_URL}/rest/{endpoint}?{query}")
    if not isinstance(payload, dict):
        raise BridgeError(f"{endpoint}: unexpected response body")
    # Navidrome answers auth failures with HTTP 200 and status "failed", so the
    # envelope is the only place a bad password shows up.
    body = payload.get("subsonic-response", {})
    if body.get("status") != "ok":
        raise BridgeError(f"{endpoint}: {body.get('error', {}).get('message', 'unknown error')}")
    return body


def starred_artists() -> dict[str, dict]:
    """Starred artist names -> {"id": Navidrome id, "mbid": tagged id or None}.

    The Navidrome id is carried along because disambiguating a shared name needs
    to know which albums the library actually holds for it.
    """
    body = subsonic("getStarred2")
    out: dict[str, dict] = {}
    for artist in body.get("starred2", {}).get("artist", []) or []:
        name = (artist.get("name") or "").strip()
        if not name:
            continue
        tagged = artist.get("musicBrainzId")
        entry = out.setdefault(name, {"id": artist.get("id"), "mbid": None})
        # A duplicate name must not erase an id seen on an earlier entry.
        if _is_mbid(tagged):
            entry["mbid"] = tagged.strip().lower()
    return out


def owned_titles(nd_artist_id: str) -> set[str]:
    """Normalised album titles the library holds for a Navidrome artist."""
    artist = subsonic("getArtist", id=nd_artist_id).get("artist", {})
    return {norm_title(a["name"]) for a in artist.get("album", []) if a.get("name")}


_MB_LAST = [0.0]


def musicbrainz_albums(mbid: str) -> set[str]:
    """Normalised studio album titles MusicBrainz lists for an artist.

    Called only to break a tie between artists sharing a name, so the one
    request per second MusicBrainz asks for costs nothing in the common case.
    """
    wait = MB_MIN_INTERVAL - (time.monotonic() - _MB_LAST[0])
    if wait > 0:
        time.sleep(wait)
    _MB_LAST[0] = time.monotonic()
    url = ("https://musicbrainz.org/ws/2/release-group?"
           + urllib.parse.urlencode({"artist": mbid, "type": "album",
                                     "limit": "100", "fmt": "json"}))
    req = urllib.request.Request(url, headers={"User-Agent": MB_USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return {norm_title(g["title"]) for g in data.get("release-groups", [])
            if g.get("primary-type") == "Album" and g.get("title")}


_USERSCRIPT_CACHE: dict = {"body": "", "fetched": 0.0, "version": ""}


def userscript(public_url: str) -> str:
    """The panel userscript, mirrored from its repository.

    @updateURL and @downloadURL are rewritten to point back here. Tampermonkey
    re-checks whatever those lines say, so leaving them pointing at GitHub would
    mean the install updates from an address the browser may not reach — which
    is the whole reason for serving it at all.
    """
    age = time.monotonic() - _USERSCRIPT_CACHE["fetched"]
    if not _USERSCRIPT_CACHE["body"] or age > USERSCRIPT_TTL:
        req = urllib.request.Request(USERSCRIPT_SOURCE, headers={"User-Agent": MB_USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
        found = re.search(r"@version\s+(\S+)", body)
        version = found.group(1) if found else "?"
        if version != _USERSCRIPT_CACHE["version"]:
            log.info("userscript mirrored: %s -> %s",
                     _USERSCRIPT_CACHE["version"] or "(none)", version)
        _USERSCRIPT_CACHE.update(body=body, fetched=time.monotonic(), version=version)

    return re.sub(
        r"^(//\s*@(?:update|download)URL\s+)\S+$",
        lambda m: m.group(1) + public_url,
        _USERSCRIPT_CACHE["body"],
        flags=re.MULTILINE,
    )


_DISCOGS_LAST = [0.0]


def discogs_get(path: str) -> dict:
    """Call Discogs, throttled, identifying ourselves as it requires."""
    wait = DISCOGS_MIN_INTERVAL - (time.monotonic() - _DISCOGS_LAST[0])
    if wait > 0:
        time.sleep(wait)
    _DISCOGS_LAST[0] = time.monotonic()
    headers = {"User-Agent": MB_USER_AGENT}
    if DISCOGS_KEY and DISCOGS_SECRET:
        headers["Authorization"] = f"Discogs key={DISCOGS_KEY}, secret={DISCOGS_SECRET}"
    req = urllib.request.Request("https://api.discogs.com" + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def discogs_artist_id(name: str, owned: set[str]) -> int | None:
    """Find the Discogs artist by searching for a record it is known to have.

    Discogs lists 326 artists called "Delirium", so asking it by name is
    hopeless. Asking for one of their records is not: a release title that
    returns a single hit has identified the artist, because no other band by
    that name released a record by that title.

    Titles are tried until one is that specific. A title shared with other
    bands simply returns several hits and is skipped.
    """
    for title in sorted(owned):
        if not title:
            continue
        query = urllib.parse.urlencode({
            "type": "release", "artist": name, "release_title": title, "per_page": 5,
        })
        try:
            found = discogs_get(f"/database/search?{query}").get("results") or []
        except FETCH_ERRORS as exc:
            log.warning("discogs search failed for %r / %r: %s", name, title, exc)
            continue
        if len(found) != 1:
            continue
        try:
            release = discogs_get(f"/releases/{found[0]['id']}")
        except FETCH_ERRORS as exc:
            log.warning("discogs release %s unreadable: %s", found[0]["id"], exc)
            continue
        artists = release.get("artists") or []
        if artists:
            log.info("discogs: %r identified as %r via %r",
                     name, artists[0].get("name"), title)
            return int(artists[0]["id"])
    return None


def discogs_discography(artist_id: int) -> list[dict]:
    """Albums Discogs credits to this artist, newest title kept once.

    Only main-artist entries count: an appearance on somebody else's record is
    not part of a discography. Compilations are dropped for the same reason a
    "Greatest Hits" is not a missing album.
    """
    data = discogs_get(f"/artists/{artist_id}/releases?per_page=100&sort=year")
    out: dict[str, dict] = {}
    for rel in data.get("releases", []):
        if rel.get("role") != "Main":
            continue
        fmt = str(rel.get("format") or "")
        if "Comp" in fmt or "Single" in fmt:
            continue
        title = (rel.get("title") or "").strip()
        if not title:
            continue
        key = norm_title(title)
        year = str(rel.get("year") or "")
        # The same album appears as a master and again per pressing; keep the
        # earliest year, which is the one a discography should show.
        prev = out.get(key)
        if prev is None or (year and (not prev["year"] or year < prev["year"])):
            # The listing already carries a thumbnail per release, so cover art
            # costs nothing beyond this one request.
            out[key] = {"title": title, "year": year, "cover": rel.get("thumb") or ""}
    return sorted(out.values(), key=lambda a: a["year"] or "9999")


def owns_title(owned: set[str], title: str) -> bool:
    """Whether the library already holds this catalogue title.

    An owned copy often carries an edition suffix the catalogue title does not
    — "Raping Uranus: The Lost Tracks Of Alien Fucker" against plain "Raping
    Uranus" — and those suffixes are not always parenthesised, so a prefix
    match catches them. Only in that direction: an owned plain title must not
    satisfy a distinct catalogue entry that extends it, such as a live
    recording named after the studio album.

    Both the missing list and identity checking ask this question, and they
    have to answer it the same way. When they disagreed, an artist whose only
    shared record carried such a suffix looked like a different band entirely.
    """
    return owned_match(owned, title) is not None


def owned_match(owned, title: str) -> str | None:
    """The owned title that satisfies this catalogue title, or None.

    Same rule as owns_title, which is written in terms of this one so the two
    can never drift. Callers that need to know *which* copy matched — to name
    its Navidrome id, say — ask here instead of matching titles a second time.
    """
    key = norm_title(title)
    for have in owned:
        if have == key or have.startswith(key + " "):
            return have
    return None


def catalogue_overlap(mbid: str, owned: set[str]) -> tuple[set[str], int]:
    """What an artist's catalogue shares with the library, and its size.

    A catalogue that could not be read, or that lists nothing at all, reports
    zero for both. That distinction matters: absence of evidence is not
    evidence of a mismatch, and only a real discography that shares nothing
    with a real library says anything about identity.
    """
    try:
        albums = musicbrainz_albums(mbid)
    except FETCH_ERRORS as exc:
        log.warning("discography lookup failed for %s: %s", mbid, exc)
        return set(), 0
    return {t for t in albums if owns_title(owned, t)}, len(albums)


def disambiguate(candidates: list[dict], owned: set[str]) -> tuple[dict | None, list[dict]]:
    """Pick the artist whose discography contains the albums already owned.

    Several unrelated bands share a name, but they do not share a back
    catalogue. Holding "Abismo" and "Los signos del Fauno" identifies exactly
    one of the ten artists called Delirium, and no human has to be asked.

    Returns the winner and, either way, every candidate with its overlap so an
    unresolved name can show its working.
    """
    scored = []
    for cand in candidates:
        overlap, total = catalogue_overlap(cand["foreignArtistId"], owned)
        scored.append({"candidate": cand, "overlap": overlap, "total": total})
    scored.sort(key=lambda s: len(s["overlap"]), reverse=True)

    report = [
        {"mbid": s["candidate"]["foreignArtistId"],
         "disambiguation": s["candidate"].get("disambiguation") or "",
         "albums_in_common": sorted(s["overlap"])}
        for s in scored
    ]
    best = scored[0] if scored else None
    if not best or not best["overlap"]:
        return None, report
    # A tie is not an answer: two catalogues matching equally well means the
    # library cannot tell them apart either.
    runner_up = scored[1]["overlap"] if len(scored) > 1 else set()
    if len(best["overlap"]) == len(runner_up):
        return None, report
    return best["candidate"], report


def lidarr_lookup(name: str) -> list[dict]:
    if not LIDARR_API_KEY:
        raise BridgeError("LIDARR_API_KEY is not set")
    url = f"{LIDARR_URL}/api/v1/artist/lookup?" + urllib.parse.urlencode({"term": name})
    results = _get_json(url, {"X-Api-Key": LIDARR_API_KEY})
    if not isinstance(results, list):
        raise BridgeError("artist/lookup did not return a list")
    return results


def _send_json(url: str, body: object, method: str, headers: dict | None = None) -> object:
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def _post_json(url: str, body: object, headers: dict | None = None) -> object:
    return _send_json(url, body, "POST", headers)


def _put_json(url: str, body: object, headers: dict | None = None) -> object:
    return _send_json(url, body, "PUT", headers)


# Resolved once per process: the id only changes if the list is recreated.
_IMPORTLIST_ID: int | None = None


def importlist_id() -> int | None:
    """The Lidarr import list to refresh, from config or by discovery."""
    global _IMPORTLIST_ID
    if LIDARR_IMPORTLIST_ID.isdigit():
        return int(LIDARR_IMPORTLIST_ID)
    if _IMPORTLIST_ID is not None:
        return _IMPORTLIST_ID

    lists = _get_json(f"{LIDARR_URL}/api/v1/importlist", {"X-Api-Key": LIDARR_API_KEY})
    if not isinstance(lists, list):
        raise BridgeError("importlist did not return a list")
    custom = [item for item in lists
              if isinstance(item, dict) and item.get("implementation") == "CustomImport"]
    if len(custom) != 1:
        # Guessing between several would refresh somebody else's list.
        raise BridgeError(
            f"found {len(custom)} CustomImport lists; set LIDARR_IMPORTLIST_ID to pick one")
    _IMPORTLIST_ID = int(custom[0]["id"])
    log.info("lidarr import list discovered: id=%d", _IMPORTLIST_ID)
    return _IMPORTLIST_ID


def notify_lidarr() -> str:
    """Ask Lidarr to re-read the list now.

    A plain ImportListSync respects the list's MinRefreshInterval (6h for
    CustomImport) and would silently do nothing; passing definitionId takes the
    single-list path, which fetches immediately.
    """
    list_id = importlist_id()
    if list_id is None:
        return "no import list to refresh"
    _post_json(f"{LIDARR_URL}/api/v1/command",
               {"name": "ImportListSync", "definitionId": list_id},
               {"X-Api-Key": LIDARR_API_KEY})
    return f"refreshed import list {list_id}"


def lidarr_get(path: str) -> object:
    if not LIDARR_API_KEY:
        raise BridgeError("LIDARR_API_KEY is not set")
    return _get_json(f"{LIDARR_URL}{path}", {"X-Api-Key": LIDARR_API_KEY})


# Edition markers that distinguish pressings of one album, not different albums.
_EDITION = re.compile(
    r"\b(remaster(ed)?|deluxe|expanded|anniversary|edition|reissue|bonus|disc|cd\d*)\b")


# One word, spelled two ways on either side of the comparison. The library holds
# "Mr Patate" where MusicBrainz lists "M. Patate", and once the period is gone
# "mr" no longer looks anything like "m" — so the album the library already has
# is reported as missing, and offered for download a second time.
_ABBREV = {
    "m": "mr", "mister": "mr", "monsieur": "mr",
    "mme": "mrs", "madame": "mrs", "missus": "mrs",
    "st": "saint", "ste": "saint", "sainte": "saint",
    "dr": "doctor",
    "vol": "volume", "pt": "part", "no": "number", "num": "number",
}


def norm_title(title: str) -> str:
    """Compare album titles across catalogues that disagree on how to spell them.

    Navidrome reports whatever the files are tagged with, Lidarr reports
    MusicBrainz, Discogs transcribes the sleeve. They differ in case, in
    apostrophes (' vs U+2019), in parenthesised edition notes, and in accents —
    Discogs has "Xibalbá" where MusicBrainz has "Xibalba".

    Accents are folded rather than stripped: dropping the character outright
    would turn "Xibalbá" into "xibalb a" and stop it matching at all. An
    ampersand becomes the word for the same reason: deleting it leaves "rock
    roll" against "rock and roll", which are the same record.
    """
    folded = unicodedata.normalize("NFKD", title.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", folded)
    text = _EDITION.sub(" ", text)
    text = text.replace("&", " and ")
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    return " ".join(_ABBREV.get(w, w) for w in words)


# Secondary types that describe a record rather than add one to a discography.
_NOT_A_GAP = {
    "compilation", "live", "remix", "soundtrack", "dj-mix", "mixtape/street",
    "demo", "interview", "audiobook", "audio drama", "spokenword",
}


def is_studio(album: dict) -> bool:
    """Whether a Lidarr album counts as a gap when the library lacks it.

    Lidarr's metadata profile is deliberately wide, so that every record the
    library already holds — demos, EPs, live sets — is catalogued and can carry
    a quality badge. That same width would otherwise fill the missing list with
    singles nobody considers absent.
    """
    if (album.get("albumType") or "") != "Album":
        return False
    return not any((s or "").lower() in _NOT_A_GAP
                   for s in (album.get("secondaryTypes") or []))


def missing_albums(nd_artist_id: str) -> dict:
    """Albums Lidarr knows for this artist that Navidrome does not have.

    Lidarr's own "missing" count is useless here: it only sees its own root
    folder, so every album already in the wider library reads as missing.
    Navidrome is the authority on what is actually owned.
    """
    artist = subsonic("getArtist", id=nd_artist_id).get("artist", {})
    name = (artist.get("name") or "").strip()
    if not name:
        raise BridgeError(f"no Navidrome artist with id {nd_artist_id!r}")
    # Titles are how the catalogues are compared, but they are a poor way to
    # pair a browser tile with its entry afterwards: MusicBrainz files the 1999
    # demo as "Ultra Vomit" while the library calls the folder "Demo", and no
    # amount of normalising makes those two strings meet. The Navidrome id does
    # meet, exactly, so it travels with the answer.
    owned_ids: dict[str, str] = {}
    for album in artist.get("album", []):
        if album.get("name") and album.get("id"):
            owned_ids.setdefault(norm_title(album["name"]), album["id"])
    owned = set(owned_ids)

    def is_owned(title: str) -> bool:
        return owns_title(owned, title)

    overrides, _ = _read_overrides()
    mbid = overrides.get(name) or _load(CACHE_PATH, {}).get(name)

    catalogue = lidarr_get("/api/v1/artist")
    match = next((a for a in catalogue if a.get("foreignArtistId") == mbid), None) if mbid else None
    if match is None:
        # An artist can be in Lidarr without ever having been starred: pressing
        # Request on one imports it on the spot. Looking only in the starred
        # cache then answered "not monitored yet" about an artist Lidarr was
        # already holding and searching for — and hid, from the one page that
        # should show it, the fact that those albums had been asked for.
        match = next((a for a in catalogue
                      if norm_title(a.get("artistName", "")) == norm_title(name)), None)

    if match is None:
        return {"artist": name, "monitored": False, "owned": len(owned), "missing": [],
                "hint": ("starred, but Lidarr has not imported it yet" if mbid
                         else "not monitored yet — star this artist in Navidrome")}

    # Lidarr's catalogue is what can actually be requested, because everything
    # it does is keyed on MusicBrainz ids.
    entries = []
    for album in lidarr_get(f"/api/v1/album?artistId={match['id']}"):
        entries.append((album, {
            "id": album["id"], "title": album["title"],
            "year": (album.get("releaseDate") or "")[:4],
            "type": album.get("albumType", ""), "requestable": True,
            # The release-group id, so a caller can fetch cover art for an
            # album nobody owns a copy of.
            "mbid": album.get("foreignAlbumId"),
            # Monitored with nothing on disk means somebody asked for this and
            # Lidarr is still looking. That is the honest record of a request:
            # it survives a cleared browser, and it is the same answer on every
            # device — which a note kept in one browser's storage is not.
            "requested": bool(album.get("monitored")),
        }))

    # Pair each catalogue album with the copy on the shelf, exact titles first.
    # The prefix rule exists for edition suffixes, and it is greedy: "Ultra
    # Vomit" — MusicBrainz's name for the 1999 demo — is a prefix of "Ultra
    # Vomit et le pouvoir de la puissance", so on a single pass the demo claimed
    # the 2024 album and the button on that tile would have gone looking for the
    # wrong record. Letting exact matches take their copy first, and letting no
    # copy be claimed twice, keeps the pairing honest.
    claimed: dict[str, dict] = {}
    for _, entry in entries:
        key = norm_title(entry["title"])
        if key in owned_ids and key not in claimed:
            claimed[key] = entry
    for _, entry in entries:
        if entry.get("ndId"):
            continue
        matched = owned_match(owned, entry["title"])
        if matched is not None and claimed.get(matched) in (None, entry):
            claimed[matched] = entry
            entry["ndId"] = owned_ids[matched]
    for key, entry in claimed.items():
        entry["ndId"] = owned_ids[key]

    requestable, held = {}, []
    for album, entry in entries:
        if entry.get("ndId"):
            # Owned is not the same as finished. A record held only as MP3 can
            # still be asked for, and the panel cannot ask without an id — so
            # the ones already on the shelf are named too, and whoever draws
            # them decides which are worth offering to improve. Every type
            # belongs here: a demo held as a 160 kbps rip is still worth
            # offering to replace, even though nobody would call it a gap.
            held.append(entry)
        elif is_studio(album):
            # A gap in a collection is a studio album. Singles, live records
            # and demos are catalogued so the ones already owned can carry a
            # badge, but listing them as missing would bury the four records
            # that actually are under forty that never were.
            requestable[norm_title(entry["title"])] = entry
    missing = dict(requestable)

    # Discogs lists records MusicBrainz has never heard of — this band's 2017
    # album among them — so it decides what the discography really is. What it
    # adds cannot be requested, since Lidarr has no id for it, but a record you
    # did not know existed is worth naming even when nothing can fetch it.
    try:
        discogs_id = discogs_artist_id(name, owned)
        extra = discogs_discography(discogs_id) if discogs_id else []
    except FETCH_ERRORS as exc:
        log.warning("discogs lookup failed for %r: %s", name, exc)
        extra = []
    for album in extra:
        key = norm_title(album["title"])
        if is_owned(album["title"]):
            continue
        if key in missing:
            # Cover Art Archive only has what somebody uploaded, and for an
            # obscure pressing that is often nothing. Discogs came with a
            # thumbnail already, so keep it as the fallback.
            missing[key].setdefault("cover", album.get("cover") or "")
            continue
        missing[key] = {"id": None, "title": album["title"], "year": album["year"],
                        "type": "Album", "requestable": False, "source": "discogs",
                        "cover": album.get("cover") or ""}

    missing = sorted(missing.values(), key=lambda a: a["year"] or "9999")
    return {"artist": name, "monitored": True, "owned": len(owned),
            "artistMbid": mbid, "lidarrArtistId": match["id"],
            "missing": missing, "held": held}


def importlist_defaults() -> dict:
    """Quality profile, metadata profile and root folder for a newly added artist.

    Taken from the import list rather than from configuration of our own: an
    artist arriving through a request should land exactly where a starred one
    would, and there is no reason to have two places that can disagree.
    """
    list_id = importlist_id()
    for item in lidarr_get("/api/v1/importlist"):
        if item.get("id") == list_id:
            return {
                "qualityProfileId": item["qualityProfileId"],
                "metadataProfileId": item["metadataProfileId"],
                "rootFolderPath": item["rootFolderPath"],
            }
    raise BridgeError(f"import list {list_id} disappeared")


def import_artist_for(mbid: str) -> int:
    """Add the artist owning this album to Lidarr, and return the album's id.

    Lidarr resolves an album's MusicBrainz id to its artist even for artists it
    has never imported, so a request for an unmonitored artist does not have to
    bounce back to the user asking them to add it first.

    The artist is added with monitor "none": the caller monitors the one album
    that was actually requested. Adding with the usual "all" would queue the
    entire discography off a single click.
    """
    found = lidarr_get("/api/v1/album/lookup?" + urllib.parse.urlencode({"term": f"lidarr:{mbid}"}))
    if not isinstance(found, list) or not found:
        raise BridgeError(f"no album in Lidarr's catalogue for {mbid}")
    artist = (found[0] or {}).get("artist") or {}
    if not artist.get("foreignArtistId"):
        raise BridgeError(f"Lidarr's catalogue has no artist for album {mbid}")

    # The artist may already be there while this particular release is not:
    # Lidarr's metadata profile decides which release types it lists, and a
    # single, a bootleg or a live set is often left out of an artist it fully
    # holds. Adding the artist again answers "This artist has already been
    # added" with a 400, which reached the panel as a bare "Failed".
    held = next((a for a in lidarr_get("/api/v1/artist")
                 if a.get("foreignArtistId") == artist["foreignArtistId"]), None)
    if held is not None:
        log.info("artist %r already in Lidarr; refreshing for album %s",
                 held.get("artistName"), mbid)
        try:
            _post_json(f"{LIDARR_URL}/api/v1/command",
                       {"name": "RefreshArtist", "artistId": held["id"]},
                       {"X-Api-Key": LIDARR_API_KEY})
        except FETCH_ERRORS as exc:
            log.warning("could not refresh %s: %s", held.get("artistName"), exc)
        deadline = time.monotonic() + IMPORT_WAIT_SECONDS
        while time.monotonic() < deadline:
            album_id = album_id_for_mbid(mbid)
            if album_id is not None:
                return album_id
            time.sleep(1)
        raise BridgeError(
            f"Lidarr holds {held.get('artistName')!r} but does not list this "
            f"release — its metadata profile leaves out singles, bootlegs and "
            f"live sets, and nothing here can add one it will not carry")

    payload = dict(artist)
    payload.update(monitored=True, **importlist_defaults())
    payload["addOptions"] = {"monitor": "none", "searchForMissingAlbums": False}
    _post_json(f"{LIDARR_URL}/api/v1/artist", payload, {"X-Api-Key": LIDARR_API_KEY})
    log.info("imported artist %r for album %s", artist.get("artistName"), mbid)

    # The discography is fetched asynchronously, so the album is not there the
    # instant the artist is.
    deadline = time.monotonic() + IMPORT_WAIT_SECONDS
    while time.monotonic() < deadline:
        album_id = album_id_for_mbid(mbid)
        if album_id is not None:
            return album_id
        time.sleep(1)
    raise BridgeError(
        f"added {artist.get('artistName')!r}, but Lidarr has not listed the album yet")


def album_id_for_mbid(mbid: str) -> int | None:
    """The Lidarr album for a MusicBrainz release-group id, if it knows one.

    Lidarr's foreignAlbumId is the release-group id, which is also what the
    Navidrome panel carries, so no title matching is needed here. Lidarr only
    holds albums of artists it has imported, so an unknown id means the artist
    is not monitored rather than that the album does not exist.
    """
    if not _is_mbid(mbid):
        raise BridgeError(f"{mbid!r} is not a MusicBrainz id")
    found = lidarr_get("/api/v1/album?" + urllib.parse.urlencode({"foreignAlbumId": mbid}))
    return int(found[0]["id"]) if found else None


def ensure_monitored(kind: str, item_id: int, tries: int = 6) -> bool:
    """Set monitored on an artist or album, and confirm it actually stuck.

    Lidarr answers 202 to a write it has merely accepted. Right after an artist
    is imported it is also refreshing that artist, and a refresh landing after
    the write clears the flag again — so the write has to be read back rather
    than trusted. Backing off between attempts lets the refresh finish.
    """
    path = f"/api/v1/{kind}/{item_id}"
    for attempt in range(tries):
        item = lidarr_get(path)
        if item.get("monitored"):
            return True
        item["monitored"] = True
        _put_json(f"{LIDARR_URL}{path}", item, {"X-Api-Key": LIDARR_API_KEY})
        time.sleep(1 + attempt)
    return bool(lidarr_get(path).get("monitored"))


def request_album(album_id: int) -> dict:
    """Ask Lidarr to monitor an album and go look for it now."""
    album = lidarr_get(f"/api/v1/album/{album_id}")

    # An album will not stay monitored while its artist is not: Lidarr accepts
    # the write and drops the flag. An artist imported on demand arrives
    # unmonitored — that is what keeps its whole discography out of the queue —
    # so the artist has to be lifted before the one requested album can be.
    artist_id = album.get("artistId") or (album.get("artist") or {}).get("id")
    if artist_id and not ensure_monitored("artist", artist_id):
        raise BridgeError(f"could not keep artist {artist_id} monitored")

    # Searching an unmonitored album finds releases and grabs none of them, so
    # a request that could not monitor it has failed, however healthy it looks.
    if not ensure_monitored("album", album_id):
        raise BridgeError(f"could not keep album {album_id} monitored")
    _post_json(f"{LIDARR_URL}/api/v1/command",
               {"name": "AlbumSearch", "albumIds": [album_id]},
               {"X-Api-Key": LIDARR_API_KEY})
    return {"requested": album.get("title"), "albumId": album_id}


def resolve(name: str, nd_artist_id: str | None = None) -> tuple[str | None, str, list[dict]]:
    """Map an artist name to a MusicBrainz id.

    Returns (mbid, reason, candidates). The library decides, not the name: a
    candidate is right when its catalogue contains albums already owned. When
    several artists share a name, that picks the one; when only one artist
    carries the name, it still has to pass, because being the only match is not
    the same as being the right band. Only a name that nothing distinguishes,
    or that everything contradicts, is left for a human.

    The check is a veto, never a requirement. A catalogue that cannot be read,
    or that lists nothing, proves nothing and lets the match stand.

    A failed lookup must never abort the caller's whole sync, so every network
    and decoding error is turned into an unresolved result.
    """
    try:
        results = lidarr_lookup(name)
    except FETCH_ERRORS as exc:
        return None, f"lookup failed: {type(exc).__name__}: {exc}", []

    exact = [
        a for a in results
        if isinstance(a, dict)
        and a.get("artistName", "").strip().lower() == name.lower()
        and _is_mbid(a.get("foreignArtistId"))
    ]
    if not exact:
        return None, "not found in MusicBrainz", []

    if not nd_artist_id:
        if len(exact) == 1:
            return exact[0]["foreignArtistId"], "ok", []
        return None, f"ambiguous: {len(exact)} artists share this name", [
            {"mbid": a["foreignArtistId"], "disambiguation": a.get("disambiguation") or ""}
            for a in exact
        ]

    try:
        owned = owned_titles(nd_artist_id)
    except FETCH_ERRORS as exc:
        if len(exact) == 1:
            # Nothing to check it against, and nothing else it could be.
            return exact[0]["foreignArtistId"], "ok", []
        return None, f"ambiguous, and the library could not be read: {exc}", []

    if len(exact) == 1:
        only = exact[0]
        mbid = only["foreignArtistId"]
        overlap, catalogue = catalogue_overlap(mbid, owned)
        if overlap or not catalogue:
            return mbid, "ok", []
        return None, (
            f"the only MusicBrainz artist by this name lists {catalogue} albums "
            f"and shares none of the {len(owned)} the library holds"
        ), [{"mbid": mbid,
             "disambiguation": only.get("disambiguation") or "",
             "albums_in_common": []}]

    winner, report = disambiguate(exact, owned)
    if winner is None:
        return None, (f"ambiguous: {len(exact)} artists share this name, and none "
                      f"of their catalogues is a better match for what is owned"), report
    log.info("%r disambiguated by discography -> %s (%s)",
             name, winner["foreignArtistId"], winner.get("disambiguation") or "no note")
    return winner["foreignArtistId"], "ok", []


def _load(path: str, default):
    try:
        with open(path) as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return default
    return loaded if isinstance(loaded, type(default)) else default


def _save(path: str, data) -> None:
    """Atomically replace `path`, safely even with concurrent writers."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # A per-writer temp name: a shared "<path>.tmp" lets a second writer
    # truncate the file the first one is still streaming into.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f"{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class State:
    """Shared, lock-guarded view of the last successful sync."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mbids: list[str] = []
        self.unresolved: dict[str, dict] = {}
        self.last_sync: str | None = None
        self.last_error: str | None = None
        self.last_push: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "artists": len(self.mbids),
                "unresolved": self.unresolved,
                "last_sync": self.last_sync,
                "last_error": self.last_error,
                "last_push": self.last_push,
                # A failed push is not unhealthy: Lidarr still refreshes on its own.
                "healthy": self.last_sync is not None and self.last_error is None,
            }


STATE = State()


def _read_overrides() -> tuple[dict[str, str], dict[str, object]]:
    """Hand-pinned name -> mbid, split into usable and malformed entries."""
    raw = _load(OVERRIDES_PATH, {})
    good: dict[str, str] = {}
    bad: dict[str, object] = {}
    for name, mbid in raw.items():
        key = str(name).strip()
        if not key:
            continue
        if _is_mbid(mbid):
            good[key] = mbid.strip().lower()
        else:
            bad[key] = mbid
    return good, bad


def sync() -> None:
    with SYNC_LOCK:
        _sync_locked()


def _sync_locked() -> None:
    """Refresh the resolved-id cache from Navidrome's starred artists."""
    cache = _load(CACHE_PATH, {})          # name -> mbid, resolved once and kept
    overrides, bad_overrides = _read_overrides()
    unresolved: dict[str, dict] = {}

    starred = starred_artists()
    names = sorted(starred)
    log.info("navidrome: %d starred artists", len(names))

    now = time.monotonic()
    for name in names:
        # Overrides are merged at publish time, never written into the cache:
        # persisting them would keep a pin alive after it is deleted by hand.
        if name in overrides:
            continue
        if name in bad_overrides:
            unresolved[name] = {
                "reason": f"overrides.json: {bad_overrides[name]!r} is not a MusicBrainz id",
                "candidates": [],
            }
            continue
        tagged = starred[name].get("mbid")
        if tagged:
            cache[name] = tagged  # Navidrome is tagged: a free, exact answer
            continue
        if name in cache:
            continue

        pending = _BACKOFF.get(name)
        if pending and now < pending["retry_at"]:
            unresolved[name] = {
                "reason": pending["reason"],
                "candidates": pending["candidates"],
                "retry_in_seconds": int(pending["retry_at"] - now),
            }
            continue

        mbid, reason, candidates = resolve(name, starred[name].get("id"))
        if mbid:
            cache[name] = mbid
            _BACKOFF.pop(name, None)
            log.info("resolved %r -> %s", name, mbid)
        else:
            attempts = (pending["attempts"] + 1) if pending else 1
            delay = min(REFRESH_SECONDS * (2 ** (attempts - 1)), MAX_BACKOFF_SECONDS)
            _BACKOFF[name] = {
                "attempts": attempts,
                "retry_at": time.monotonic() + delay,
                "reason": reason,
                "candidates": candidates,
            }
            unresolved[name] = {
                "reason": reason,
                "candidates": candidates,
                "retry_in_seconds": int(delay),
            }
            log.warning("unresolved %r: %s (retrying in %ds)", name, reason, delay)
        if LOOKUP_DELAY_SECONDS > 0:
            time.sleep(LOOKUP_DELAY_SECONDS)

    # Drop artists that are no longer starred so unstarring actually removes them.
    cache = {k: v for k, v in cache.items() if k in starred}
    for name in list(_BACKOFF):
        if name not in starred:
            _BACKOFF.pop(name, None)

    try:
        _save(CACHE_PATH, cache)
    except OSError as exc:
        # A cache we could not persist only costs re-resolution after a restart;
        # it must not throw away the artists this pass just resolved.
        log.error("could not persist %s: %s", CACHE_PATH, exc)

    published = dict(cache)
    published.update({n: overrides[n] for n in names if n in overrides})
    mbids = sorted(set(published.values()))
    changed = mbids != _load(PUBLISHED_PATH, [])

    # Publish before telling Lidarr, so the refresh it runs reads the new list.
    with STATE.lock:
        STATE.mbids = mbids
        STATE.unresolved = unresolved
        STATE.last_sync = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        STATE.last_error = None

    if not changed:
        return
    try:
        _save(PUBLISHED_PATH, mbids)
    except OSError as exc:
        # Only costs one redundant refresh after a restart.
        log.error("could not persist %s: %s", PUBLISHED_PATH, exc)
    try:
        outcome = notify_lidarr()
        log.info("list changed (%d artists): %s", len(mbids), outcome)
    except FETCH_ERRORS as exc:
        # Lidarr still picks the change up on its own 6h refresh.
        outcome = f"{type(exc).__name__}: {exc}"
        log.warning("could not refresh lidarr: %s", outcome)
    with STATE.lock:
        STATE.last_push = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {outcome}"


def sync_loop() -> None:
    while True:
        try:
            sync()
        except Exception as exc:  # keep serving the last good list
            log.error("sync failed: %s", exc)
            with STATE.lock:
                STATE.last_error = str(exc)
        time.sleep(REFRESH_SECONDS)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Without this an idle keep-alive connection pins a thread forever.
    timeout = 30

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The panel runs on Navidrome's origin and calls this service on another
        # port, so every answer it reads is a cross-origin one.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _respond(self, payload: object, status: int = 200) -> None:
        self._send(json.dumps(payload, indent=2).encode(), "application/json", status)

    def _missing(self, query: dict) -> None:
        artist_id = (query.get("id") or [""])[0]
        if not artist_id:
            self._respond({"error": "missing 'id' (Navidrome artist id)"}, 400)
            return
        try:
            self._respond(missing_albums(artist_id))
        except FETCH_ERRORS as exc:
            self._respond({"error": f"{type(exc).__name__}: {exc}"}, 502)

    def _request(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._respond({"error": "body is not JSON"}, 400)
            return
        try:
            if body.get("albumId") is not None:
                album_id = int(body["albumId"])
            elif body.get("mbid"):
                mbid = str(body["mbid"])
                album_id = album_id_for_mbid(mbid)
                if album_id is None:
                    # Lidarr only holds albums of artists it imported, and being
                    # sent away to add the artist first is a poor answer to
                    # "fetch me this album". Import it and carry on.
                    album_id = import_artist_for(mbid)
            else:
                raise KeyError("albumId")
        except (ValueError, KeyError, TypeError, AttributeError):
            self._respond({"error": "expected a JSON body with albumId or mbid"}, 400)
            return
        except FETCH_ERRORS as exc:
            self._respond({"error": f"{type(exc).__name__}: {exc}"}, 502)
            return
        try:
            self._respond(request_album(album_id))
        except FETCH_ERRORS as exc:
            self._respond({"error": f"{type(exc).__name__}: {exc}"}, 502)

    def _userscript(self) -> None:
        # Tampermonkey follows @updateURL exactly, so it must be the address the
        # browser used to get here, not the one the container knows itself by.
        if PUBLIC_SCRIPT_URL:
            public = PUBLIC_SCRIPT_URL
        else:
            scheme = self.headers.get("X-Forwarded-Proto") or "http"
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
            public = f"{scheme}://{host}{self.path}"
        try:
            body = userscript(public).encode()
        except FETCH_ERRORS as exc:
            self._respond({"error": f"could not mirror the userscript: {exc}"}, 502)
            return
        self._send(body, "application/javascript; charset=utf-8")

    def _panel(self) -> None:
        try:
            with open(PANEL_PATH, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            self._respond({"error": f"panel unavailable: {exc}"}, 404)
            return
        self._send(body, "application/javascript; charset=utf-8")

    def _artists(self) -> None:
        with STATE.lock:
            synced, error, mbids = STATE.last_sync, STATE.last_error, list(STATE.mbids)
        if synced is None:
            # An empty 200 would tell Lidarr "nothing is starred" when the truth
            # is "Navidrome was never reached". Fail loudly instead.
            self._respond({"error": "no successful sync yet", "detail": error}, 503)
            return
        self._respond([{"MusicBrainzId": m} for m in mbids])

    def _status(self) -> None:
        snapshot = STATE.snapshot()
        # Drives the container HEALTHCHECK: a service whose syncs all fail is
        # not healthy, however long it has been answering requests.
        self._respond(snapshot, 200 if snapshot["healthy"] else 503)

    def _sync(self) -> None:
        try:
            sync()
        except Exception as exc:
            with STATE.lock:
                STATE.last_error = str(exc)
            self._respond({"error": str(exc)}, 500)
            return
        self._respond(STATE.snapshot())

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/artists.json"):
            self._artists()
        elif path == "/status":
            self._status()
        elif path == "/sync":
            self._sync()
        elif path == "/missing":
            self._missing(urllib.parse.parse_qs(parsed.query))
        elif path == "/panel.user.js":
            self._panel()
        elif path in ("/userscript.js", "/navidrome-missing-albums.user.js"):
            self._userscript()
        else:
            self._respond({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urllib.parse.urlparse(self.path).path
        if path == "/sync":
            self._sync()
        elif path == "/request":
            self._request()
        else:
            self._respond({"error": "not found"}, 404)

    def do_OPTIONS(self):  # noqa: N802 - CORS preflight for POST /request
        self._send(b"", "text/plain", 204)

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    threading.Thread(target=sync_loop, daemon=True).start()
    log.info("serving on :%d", LISTEN_PORT)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
